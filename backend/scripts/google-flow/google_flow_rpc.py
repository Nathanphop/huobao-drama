"""Small RPC client for Google Flow's internal batchexecute services.

The browser is used only to establish the authenticated Flow document and read
its anti-CSRF bootstrap values. No DOM selectors or simulated user input are
used by this module.
"""

import json
import os
import random
import re
import time
from contextlib import contextmanager

from playwright.sync_api import sync_playwright


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.environ.get("GOOGLE_FLOW_SESSION_PATH") or os.path.join(CURRENT_DIR, "session.json")
RPC_MAX_ATTEMPTS = 5


class FlowRpcTransientError(RuntimeError):
    """A retryable empty or incomplete response from Flow's batchexecute RPC."""


def load_session_cookies():
    if not os.path.exists(SESSION_FILE):
        raise RuntimeError("ไม่พบ Google session กรุณารีเฟรชบัญชี Google Flow อีกครั้ง")
    with open(SESSION_FILE, encoding="utf-8") as handle:
        session = json.load(handle)
    allowed = {
        "name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"
    }
    return [
        {key: value for key, value in cookie.items() if key in allowed}
        for cookie in session.get("cookies", [])
    ]


def with_authuser(url, authuser):
    if authuser is None or str(authuser) == "":
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}authuser={authuser}"


def project_id_from_url(project_url):
    match = re.search(r"/project/([^/?#]+)", project_url or "")
    if not match:
        raise RuntimeError("ต้องมี Google Flow project URL สำหรับเรียก RPC")
    return match.group(1)


def _extract_rpc_payload(response_text, rpc_id):
    for line in response_text.replace(")]}'", "", 1).splitlines():
        if not line.lstrip().startswith("["):
            continue
        try:
            rows = json.loads(line)
        except json.JSONDecodeError:
            continue
        for row in rows:
            if (
                isinstance(row, list)
                and len(row) > 2
                and row[0] == "wrb.fr"
                and row[1] == rpc_id
            ):
                if len(row) > 5 and row[5]:
                    detail = json.dumps(row[5], ensure_ascii=False)
                    if "PUBLIC_ERROR_UNUSUAL_ACTIVITY" in detail:
                        raise RuntimeError(
                            "Google Flow ปฏิเสธคำขอสร้างสื่อที่ไม่ได้ผ่าน security lifecycle ของหน้า Flow"
                        )
                    raise RuntimeError(f"Google Flow RPC {rpc_id} ตอบกลับเป็นข้อผิดพลาด: {detail}")
                value = row[2]
                if not isinstance(value, str):
                    return value
                if not value.strip():
                    raise FlowRpcTransientError(
                        f"Google Flow RPC {rpc_id} ส่ง payload ว่างกลับมา"
                    )
                try:
                    return json.loads(value)
                except json.JSONDecodeError as error:
                    raise FlowRpcTransientError(
                        f"Google Flow RPC {rpc_id} ส่ง payload ไม่สมบูรณ์"
                    ) from error
    raise FlowRpcTransientError(f"Google Flow RPC {rpc_id} ไม่ส่งผลลัพธ์กลับมา")


class FlowRpcClient:
    def __init__(self, page, project_url):
        self.page = page
        self.project_url = project_url.split("?", 1)[0]
        self.project_id = project_id_from_url(self.project_url)

    def call(self, rpc_id, payload):
        last_error = None
        for attempt in range(1, RPC_MAX_ATTEMPTS + 1):
            try:
                result = self.page.evaluate(
                    r"""async ({rpcId, payload}) => {
                const globals = window.WIZ_global_data || {}
                const at = globals.SNlM0e
                const bl = globals.cfb2h
                const sid = globals.FdrFJe
                if (!at || !bl) {
                    return {ok: false, detail: 'Google Flow RPC bootstrap is not ready.'}
                }
                const accountMatch = location.pathname.match(/^\/u\/(\d+)\//)
                const accountPrefix = accountMatch ? `/u/${accountMatch[1]}` : ''
                const body =
                    'f.req=' + encodeURIComponent(JSON.stringify([[[
                        rpcId, JSON.stringify(payload), null, 'generic'
                    ]]])) + '&at=' + encodeURIComponent(at) + '&'
                const url = location.origin + accountPrefix +
                    '/_/AiSandboxAngularFrontend/data/batchexecute' +
                    '?rpcids=' + encodeURIComponent(rpcId) +
                    '&source-path=' + encodeURIComponent(location.pathname) +
                    '&bl=' + encodeURIComponent(bl) +
                    '&f.sid=' + encodeURIComponent(sid || '') +
                    '&hl=en&_reqid=' + (1000000 + Math.floor(8000000 * Math.random())) + '&rt=c'
                const response = await fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                        'X-Same-Domain': '1',
                    },
                    body,
                })
                return {ok: response.ok, status: response.status, text: await response.text()}
            }""",
                    {"rpcId": rpc_id, "payload": payload},
                )
                if not result or not result.get("ok"):
                    status = (result or {}).get("status", "unknown")
                    detail = (result or {}).get("detail", "")
                    retryable = status == "unknown" or status == 429 or (
                        isinstance(status, int) and status >= 500
                    )
                    if not retryable:
                        raise RuntimeError(
                            f"Google Flow RPC {rpc_id} ล้มเหลว (HTTP {status})"
                        )
                    raise FlowRpcTransientError(
                        detail or f"Google Flow RPC {rpc_id} ล้มเหลวชั่วคราว (HTTP {status})"
                    )

                response_text = result.get("text", "")
                if not response_text.strip():
                    raise FlowRpcTransientError(
                        f"Google Flow RPC {rpc_id} ส่งคำตอบว่างกลับมา"
                    )
                self.last_response_text = response_text
                return _extract_rpc_payload(response_text, rpc_id)
            except FlowRpcTransientError as error:
                last_error = error
            except RuntimeError:
                raise
            except Exception as error:
                last_error = error

            if attempt < RPC_MAX_ATTEMPTS:
                # Parallel image jobs can briefly receive an empty batchexecute
                # response. Jitter keeps their retries from colliding again.
                delay = min(4.0, 0.45 * (2 ** (attempt - 1)))
                time.sleep(delay + random.uniform(0.05, 0.35))

        raise RuntimeError(
            f"Google Flow RPC {rpc_id} ตอบกลับว่างหรือไม่สมบูรณ์ "
            f"หลังลองใหม่ {RPC_MAX_ATTEMPTS} ครั้ง: {last_error}"
        ) from last_error

    def get_project_contents(self):
        return self.call("Zzl0ze", [f"projects/{self.project_id}", None, None, None, [1]])

    @staticmethod
    def _asset_urls(value):
        if isinstance(value, str) and (
            value.startswith("https://flow-content.google/image/")
            or "googleusercontent.com/asb/" in value
        ):
            return [value]
        if isinstance(value, list):
            urls = []
            for child in value:
                urls.extend(FlowRpcClient._asset_urls(child))
            return urls
        return []

    @staticmethod
    def _first_asset_url(value):
        urls = FlowRpcClient._asset_urls(value)
        # RPC media records can contain both the 512 px lh3/asb preview and the
        # signed full-resolution flow-content URL. The preview often appears
        # first in the payload, so selecting the first URL silently downgraded
        # every imported image. This matches the asset used by Flow's 1K view.
        return next(
            (url for url in urls if url.startswith("https://flow-content.google/image/")),
            urls[0] if urls else "",
        )

    def list_image_assets(self):
        contents = self.get_project_contents()
        workflows = contents[1] if isinstance(contents, list) and len(contents) > 1 else []
        media = contents[2] if isinstance(contents, list) and len(contents) > 2 else []
        workflow_metadata = {}
        for workflow in workflows or []:
            if not isinstance(workflow, list) or len(workflow) < 4:
                continue
            metadata = workflow[3] if isinstance(workflow[3], list) else []
            workflow_metadata[str(workflow[0])] = {
                "displayName": str(metadata[0] or "") if metadata else "",
                "archived": bool(metadata[2]) if len(metadata) > 2 else False,
                "mediaId": str(metadata[4] or "") if len(metadata) > 4 else "",
                "batchId": str(metadata[5] or "") if len(metadata) > 5 else "",
                "createdAt": metadata[1] if len(metadata) > 1 else None,
                "updatedAt": metadata[6] if len(metadata) > 6 else None,
            }
        assets = []
        for record in media or []:
            if not isinstance(record, list) or len(record) < 3:
                continue
            url = self._first_asset_url(record)
            if not url:
                continue
            workflow_id = str(record[2] or "")
            metadata = workflow_metadata.get(workflow_id, {})
            assets.append({
                "mediaId": str(record[0] or ""),
                "workflowId": workflow_id,
                "assetUrl": url,
                **metadata,
            })
        return assets

    def wait_for_new_images(
        self,
        previous_workflow_ids,
        expected=1,
        timeout_seconds=600,
        on_poll=None,
    ):
        previous = set(previous_workflow_ids or [])
        started_at = time.time()
        deadline = time.time() + timeout_seconds
        first_asset_at = None
        last_change_at = None
        last_asset_keys = ()
        while time.time() < deadline:
            assets = [
                asset for asset in self.list_image_assets()
                if asset.get("workflowId") not in previous and not asset.get("archived")
            ]
            if assets:
                now = time.time()
                asset_keys = tuple(
                    sorted(
                        str(asset.get("workflowId") or asset.get("mediaId") or "")
                        for asset in assets
                    )
                )
                if first_asset_at is None:
                    first_asset_at = now
                    last_change_at = now
                if asset_keys != last_asset_keys:
                    last_asset_keys = asset_keys
                    last_change_at = now
                stable_for = now - (last_change_at or now)
                visible_for = now - (first_asset_at or now)
                if (
                    len(assets) >= expected and stable_for >= 2
                ) or (stable_for >= 30 and visible_for >= 60):
                    return assets
            if on_poll:
                on_poll(time.time() - started_at, timeout_seconds, assets)
            self.page.wait_for_timeout(1200)
        raise RuntimeError("รอข้อมูลภาพใหม่จาก Google Flow RPC เกินเวลาที่กำหนด")

    def update_workflow(self, workflow_id, field_mask, metadata):
        return self.call(
            "mYWVGd",
            [[workflow_id, None, None, metadata, self.project_id], [[field_mask]]],
        )

    def rename_workflow(self, workflow_id, display_name):
        return self.update_workflow(
            workflow_id, "metadata.display_name", [str(display_name)[:120]]
        )

    def list_agent_sessions(self, limit=100):
        return self.call("mrlkwd", [self.project_id, limit])

    def get_agent_session(self, session_id):
        return self.call("GN0Bre", [session_id])

    def delete_agent_session(self, session_id):
        return self.call("Dcn2Le", [session_id])

    def set_workflow_archived(self, workflow_id, archived):
        return self.update_workflow(
            workflow_id, "metadata.archived", [None, None, bool(archived)]
        )

    def delete_workflow_permanently(self, workflow_id):
        # BatchDeleteAssets request fields: workflow IDs=2, project ID=3.
        return self.call("cz8Z4b", [None, [workflow_id], self.project_id])


@contextmanager
def open_flow_rpc(project_url, authuser=None):
    project_id_from_url(project_url)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        context.add_cookies(load_session_cookies())
        page = context.new_page()
        try:
            page.goto(
                with_authuser(project_url, authuser),
                wait_until="domcontentloaded",
                timeout=90000,
            )
            page.wait_for_function(
                "() => Boolean(window.WIZ_global_data?.SNlM0e && window.WIZ_global_data?.cfb2h)",
                timeout=30000,
            )
            if "/project/" not in page.url:
                raise RuntimeError("Google session ไม่มีสิทธิ์เข้าถึง Flow project นี้")
            yield FlowRpcClient(page, page.url)
        finally:
            context.close()
            browser.close()
