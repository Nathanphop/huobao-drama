import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from io import BytesIO
from urllib.parse import urlsplit, urlunsplit

from PIL import Image
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from google_flow_rpc import FlowRpcClient


for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.environ.get("GOOGLE_FLOW_SESSION_PATH") or os.path.join(CURRENT_DIR, "session.json")
FLOW_HOME_URL = "https://flow.google.com/"
SUPPORTED_ASPECT_RATIOS = {"16:9", "4:3", "1:1", "3:4", "9:16"}
SUPPORTED_IMAGE_MODELS = {"Nano Banana Pro", "Nano Banana 2 Lite", "Nano Banana 2.1"}
MAX_REFERENCE_IMAGES = 10
IMAGE_RESOLUTION_CONFIG = {
    "1K": {"minimum_long_edge": 1000, "rendition_size": 1600},
    "2K": {"minimum_long_edge": 2000, "rendition_size": 3200},
    "4K": {"minimum_long_edge": 4000, "rendition_size": 6400},
}
IMAGE_GENERATION_RPC_ID = "ogiZ0b"
TRANSIENT_NAVIGATION_MARKERS = (
    "ERR_SOCKET_NOT_CONNECTED",
    "ERR_TIMED_OUT",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_RESET",
    "ERR_NETWORK_CHANGED",
    "Timeout 90000ms exceeded",
)


def load_session_cookies():
    if not os.path.exists(SESSION_FILE):
        raise RuntimeError("ไม่พบ Google session กรุณาเปิด Chrome 9222 และรีเฟรชบัญชีอีกครั้ง")
    with open(SESSION_FILE, "r", encoding="utf-8") as handle:
        session = json.load(handle)
    cookies = session.get("cookies") or []
    allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    return [{key: value for key, value in cookie.items() if key in allowed} for cookie in cookies]


def with_authuser(url, authuser):
    if authuser is None or authuser == "":
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}authuser={authuser}"


def close_optional_ui(page):
    for name in ("Dismiss banner", "Close"):
        locator = page.get_by_role("button", name=name, exact=True)
        if locator.count():
            try:
                locator.first.click(timeout=1500)
            except Exception:
                pass


def goto_with_retry(page, url, attempts=4):
    last_error = None
    for attempt in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            return
        except Exception as error:
            last_error = error
            detail = str(error)
            if attempt + 1 >= attempts or not any(
                marker.lower() in detail.lower() for marker in TRANSIENT_NAVIGATION_MARKERS
            ):
                raise
            delay_ms = min(10000, 1500 * (2 ** attempt))
            print(
                f"Google Flow navigation retry {attempt + 2}/{attempts} after: {detail}",
                file=sys.stderr,
                flush=True,
            )
            page.wait_for_timeout(delay_ms)
    raise last_error


def open_or_create_project(page, project_url, authuser):
    target = project_url if project_url and "/project/" in project_url else FLOW_HOME_URL
    goto_with_retry(page, with_authuser(target, authuser))
    page.wait_for_timeout(5000)
    close_optional_ui(page)
    if "/project/" in page.url:
        return page.url.split("?", 1)[0]

    new_project = page.get_by_role("button", name=re.compile(r"New project", re.I))
    if not new_project.count():
        new_project = page.get_by_text("New project", exact=True)
    if not new_project.count():
        raise RuntimeError("ไม่พบปุ่ม New project ใน Google Flow กรุณาตรวจสอบว่าบัญชีนี้เข้าใช้ Flow ได้")
    new_project.first.click(timeout=15000)
    page.wait_for_url(re.compile(r"flow\.google\.com/project/"), timeout=90000)
    page.wait_for_timeout(5000)
    close_optional_ui(page)
    return page.url.split("?", 1)[0]


def click_radio(page, label, occurrence=0):
    radios = page.get_by_role("radio", name=label, exact=True)
    visible = [radios.nth(index) for index in range(radios.count()) if radios.nth(index).is_visible()]
    if len(visible) <= occurrence:
        radios = page.get_by_role("radio", name=re.compile(re.escape(label), re.I))
        visible = [
            radios.nth(index) for index in range(radios.count()) if radios.nth(index).is_visible()
        ]
    if len(visible) <= occurrence:
        raise RuntimeError(f"ไม่พบตัวเลือก {label} ใน Google Flow")
    visible[occurrence].click(timeout=10000)


def wait_for_visible(page, locator, label, timeout_ms=30000):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if candidate.is_visible():
                return candidate
        page.wait_for_timeout(250)
    raise RuntimeError(f"ไม่พบ {label} ที่มองเห็นได้ใน Google Flow")


def first_visible(locator):
    for index in range(locator.count()):
        candidate = locator.nth(index)
        if candidate.is_visible():
            return candidate
    return None


def retry_dynamic_ui(page, action, label, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            return action()
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                page.wait_for_timeout(750)
                close_optional_ui(page)
    print(f"Google Flow dynamic UI error ({label}): {last_error}", file=sys.stderr, flush=True)
    raise RuntimeError(f"หน้า Google Flow เปลี่ยน{label} กรุณากดลองใหม่")


def click_start_generation(page):
    """Submit through the live button even when Flow replaces its DOM mid-click."""
    start = wait_for_visible(
        page,
        page.get_by_role("button", name="Start generation", exact=True),
        "ปุ่ม Start generation",
    )
    for _ in range(20):
        if start.is_enabled():
            break
        page.wait_for_timeout(250)
        start = wait_for_visible(
            page,
            page.get_by_role("button", name="Start generation", exact=True),
            "ปุ่ม Start generation",
        )
    if not start.is_enabled():
        raise RuntimeError("ปุ่ม Start generation ยังไม่พร้อมใช้งานหลังกรอก Prompt")
    try:
        start.scroll_into_view_if_needed(timeout=3000)
        start.click(timeout=6000)
    except Exception:
        # Flow can replace the Angular button between the actionability check
        # and the click. Resolve the current node and dispatch its native click.
        fresh_start = wait_for_visible(
            page,
            page.get_by_role("button", name="Start generation", exact=True),
            "ปุ่ม Start generation",
        )
        if not fresh_start.is_enabled():
            raise RuntimeError("ปุ่ม Start generation ถูกปิดก่อนเริ่มสร้างภาพ")
        fresh_start.evaluate("element => element.click()")


def submit_generation_with_retry(page, rpc_client, previous_workflow_ids, attempts=2):
    """Submit once reliably without treating a slow Flow response as an immediate failure."""
    last_error = None
    for attempt in range(attempts):
        try:
            with page.expect_response(
                lambda response: (
                    f"rpcids={IMAGE_GENERATION_RPC_ID}" in response.url
                    or f"rpcids%3D{IMAGE_GENERATION_RPC_ID}" in response.url
                ),
                timeout=45000,
            ) as response_info:
                retry_dynamic_ui(
                    page,
                    lambda: click_start_generation(page),
                    "ขณะเริ่มสร้างภาพ",
                )
            validate_generation_response(response_info.value)
            return
        except PlaywrightTimeoutError as error:
            last_error = error
            # The response event can be lost while Flow replaces its UI. Before
            # clicking again, verify that the backend did not already create a job.
            for _ in range(5):
                current_ids = {
                    asset.get("workflowId")
                    for asset in rpc_client.list_image_assets()
                    if asset.get("workflowId")
                }
                if current_ids - previous_workflow_ids:
                    return
                page.wait_for_timeout(1000)
            if attempt + 1 < attempts:
                close_optional_ui(page)
                page.wait_for_timeout(1500)
    raise RuntimeError(
        "กดสร้างแล้ว แต่ Google Flow ไม่ได้ส่งคำขอสร้างภาพออกจากหน้าเว็บหลังลองใหม่"
    ) from last_error


def select_image_model(page, model, compact=False):
    button_name = r"Select model family" if compact else r"Image generation default model"
    model_button = wait_for_visible(
        page,
        page.get_by_role("button", name=re.compile(button_name, re.I)),
        "ตัวเลือกโมเดลภาพ",
    )
    model_button.click(timeout=10000)
    option = page.get_by_role("menuitem", name=re.compile(rf"{re.escape(model)}$", re.I))
    if not option.count():
        raise RuntimeError(f"บัญชีนี้ไม่มีโมเดลภาพ {model} ใน Google Flow")
    option.first.click(timeout=10000)


def configure_compact_image_settings(page, trigger, aspect_ratio, model):
    trigger.click(timeout=10000)
    page.wait_for_timeout(300)
    click_radio(page, aspect_ratio, 0)
    click_radio(page, "x1", 0)
    select_image_model(page, model, compact=True)
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)


def configure_image_defaults(page, aspect_ratio, model):
    compact_trigger = first_visible(
        page.get_by_role("button", name=re.compile(r"Settings trigger", re.I))
    )
    if compact_trigger:
        configure_compact_image_settings(page, compact_trigger, aspect_ratio, model)
        return
    settings = wait_for_visible(
        page, page.get_by_role("button", name="Settings", exact=True), "ปุ่ม Settings"
    )
    settings.click(timeout=10000)
    page.get_by_text("Agent settings", exact=True).wait_for(state="visible", timeout=15000)

    click_radio(page, "Never", 0)
    click_radio(page, aspect_ratio, 0)
    click_radio(page, "x1", 0)
    select_image_model(page, model)
    save = wait_for_visible(page, page.get_by_role("button", name="Save", exact=True), "ปุ่ม Save")
    save.click(timeout=10000)
    page.wait_for_timeout(1000)


def configure_agent_mode(page, enabled):
    # Flow currently exposes the Agent chip as a checkbox in its accessibility
    # tree, but the live DOM no longer has a role=checkbox selector. Keep the
    # old selector for compatibility and fall back to the visible chip label.
    legacy_checkbox = first_visible(
        page.get_by_role("checkbox", name="Agent", exact=True)
    )
    if legacy_checkbox:
        if legacy_checkbox.is_checked() != enabled:
            legacy_checkbox.click(timeout=10000)
            page.wait_for_timeout(750)
        return

    agent_label = wait_for_visible(
        page,
        page.get_by_text("Agent", exact=True),
        "ตัวเลือก Agent",
    )
    chip = agent_label.locator("..")
    class_name = (chip.get_attribute("class") or "").lower()
    aria_checked = (chip.get_attribute("aria-checked") or "").lower()
    data_state = (chip.get_attribute("data-state") or "").lower()
    is_enabled = (
        "agent-mode-chip-checked" in class_name
        or aria_checked == "true"
        or data_state in {"checked", "on"}
    )
    if is_enabled != enabled:
        agent_label.click(timeout=10000)
        page.wait_for_timeout(750)


def find_prompt_editor(page):
    editor = page.locator('.ProseMirror[contenteditable="true"]')
    if not editor.count():
        editor = page.locator('[contenteditable="true"]').last
    editor.wait_for(state="visible", timeout=30000)
    return editor


def upload_reference_images(page, reference_paths):
    paths = [os.path.abspath(path) for path in reference_paths if path]
    if not paths:
        return
    if len(paths) > MAX_REFERENCE_IMAGES:
        raise RuntimeError(f"Google Flow รองรับรูปอ้างอิงได้สูงสุด {MAX_REFERENCE_IMAGES} รูปต่อคำสั่ง")
    for path in paths:
        if not os.path.isfile(path):
            raise RuntimeError(f"ไม่พบไฟล์รูปอ้างอิง: {path}")

    file_inputs = page.locator('input[type="file"]')
    if file_inputs.count():
        target = file_inputs.last
        try:
            target.set_input_files(paths, timeout=30000)
        except Exception:
            for path in paths:
                target.set_input_files(path, timeout=30000)
                page.wait_for_timeout(500)
        page.wait_for_timeout(2500)
        return

    add_button = wait_for_visible(
        page,
        page.get_by_role(
            "button",
            name=re.compile(
                r"Add (?:media|element|ingredient)|เพิ่ม(?:สื่อ|องค์ประกอบ)", re.I
            ),
        ),
        "ปุ่มเพิ่มรูปอ้างอิง",
    )
    add_button.click(timeout=10000)
    page.wait_for_timeout(300)
    upload = first_visible(
        page.get_by_role(
            "menuitem",
            name=re.compile(r"Upload(?: media)?|อัปโหลด", re.I),
        )
    ) or first_visible(page.get_by_text(re.compile(r"Upload(?: media)?|อัปโหลด", re.I)))
    if not upload:
        raise RuntimeError("ไม่พบเมนู Upload media ใน Google Flow")
    try:
        with page.expect_file_chooser(timeout=10000) as chooser_info:
            upload.click(timeout=10000)
        chooser_info.value.set_files(paths)
    except Exception:
        file_inputs = page.locator('input[type="file"]')
        if not file_inputs.count():
            raise RuntimeError("Google Flow ไม่เปิดตัวเลือกรูปอ้างอิง")
        file_inputs.last.set_input_files(paths, timeout=30000)
    page.wait_for_timeout(3000)


def normalize_flow_asset_url(value):
    url = str(value or "").strip().split("?", 1)[0]
    if "/asb/" in url:
        url = re.sub(r"=s\d+(?:-[a-z]+)*$", "", url, flags=re.I)
    return url


def count_prompt_reference_images(page):
    """Count reference chips already attached to the active Flow prompt."""
    editor = find_prompt_editor(page)
    return editor.evaluate(
        r"""(node) => {
            const promptBox = node.closest('.base-prompt-box')
            if (!promptBox) return 0
            return promptBox.querySelectorAll(
                'flow-ingredient-bar flow-image-ingredient-chip .chip-container'
            ).length
        }"""
    )


def select_existing_reference_image(page, asset_url, media_id="", display_name=""):
    target = normalize_flow_asset_url(asset_url)
    target_media_id = str(media_id or "").strip()
    target_display_name = str(display_name or "").strip().lower()
    if not target and not target_media_id and not target_display_name:
        return False
    add_button = first_visible(
        page.get_by_role(
            "button",
            name=re.compile(r"Add ingredients to the prompt box|เพิ่ม.*(?:รูป|สื่อ)", re.I),
        )
    )
    if not add_button:
        return False
    previous_reference_count = count_prompt_reference_images(page)
    add_button.click(timeout=10000)
    options = page.locator('[role="option"]')
    try:
        options.first.wait_for(state="visible", timeout=10000)

        def find_matching_option():
            return options.evaluate_all(
                r"""(items, target) => {
                    const normalize = (value) => {
                        let url = String(value || '').trim().split('?', 1)[0]
                        if (url.includes('/asb/')) url = url.replace(/=s\d+(?:-[a-z]+)*$/i, '')
                        return url
                    }
                    return items.findIndex((item) => {
                        const title = (
                            item.querySelector('.asset-title')?.textContent ||
                            item.textContent ||
                            ''
                        )
                            .trim().toLowerCase()
                        const imageMatches = [...item.querySelectorAll('img')].some((image) =>
                            (target.mediaId && image.dataset.mediaId === target.mediaId) ||
                            (target.url && normalize(image.src) === target.url)
                        )
                        const titleMatches = target.displayName && (
                            title === target.displayName ||
                            title === `${target.displayName} image` ||
                            title.startsWith(`${target.displayName}\n`)
                        )
                        return imageMatches || titleMatches
                    })
                }""",
                {
                    "url": target,
                    "mediaId": target_media_id,
                    "displayName": target_display_name,
                },
            )

        matching_index = find_matching_option()
        if matching_index < 0 and target_display_name:
            # The ingredient picker virtualizes the recent list, so an asset
            # visible in All media may not exist in the option DOM yet. Search
            # by its renamed character tag to materialize the exact option.
            search = first_visible(
                page.get_by_role("textbox", name=re.compile(r"Search assets", re.I))
            )
            if search:
                search.fill(target_display_name)
                page.wait_for_timeout(900)
                options.first.wait_for(state="visible", timeout=10000)
                matching_index = find_matching_option()
        if matching_index < 0:
            page.keyboard.press("Escape")
            return False
        options.nth(matching_index).click(timeout=10000)

        # Current Flow attaches an asset immediately when its option is clicked.
        # Older Flow builds used a second "Add to prompt" confirmation button,
        # so support both behaviours and verify success from the actual chip.
        for _ in range(20):
            if count_prompt_reference_images(page) > previous_reference_count:
                return True
            page.wait_for_timeout(100)

        confirm = first_visible(
            page.get_by_role("button", name=re.compile(r"Add to prompt|เพิ่ม.*พรอมต์", re.I))
        )
        if not confirm:
            page.keyboard.press("Escape")
            return False
        for _ in range(20):
            if confirm.is_enabled():
                break
            page.wait_for_timeout(200)
        if not confirm.is_enabled():
            page.keyboard.press("Escape")
            return False
        confirm.click(timeout=10000)
        for _ in range(20):
            if count_prompt_reference_images(page) > previous_reference_count:
                return True
            page.wait_for_timeout(100)
        return False
    except Exception:
        page.keyboard.press("Escape")
        return False


def attach_reference_images(page, reference_specs):
    specs = [spec for spec in reference_specs if spec.get("path")]
    if len(specs) > MAX_REFERENCE_IMAGES:
        raise RuntimeError(f"Google Flow รองรับรูปอ้างอิงได้สูงสุด {MAX_REFERENCE_IMAGES} รูปต่อคำสั่ง")
    for spec in specs:
        has_existing_asset = spec.get("assetUrl") or spec.get("mediaId") or spec.get("displayName")
        if has_existing_asset:
            if select_existing_reference_image(
                page,
                spec.get("assetUrl", ""),
                spec.get("mediaId", ""),
                spec.get("displayName", ""),
            ):
                continue
            label = spec.get("displayName") or spec.get("mediaId") or spec.get("assetUrl")
            raise RuntimeError(
                f"ไม่พบ Asset อ้างอิง {label} ใน Google Flow จึงหยุดเพื่อป้องกันการอัปโหลดซ้ำ"
            )
        upload_reference_images(page, [spec["path"]])


def wait_for_uploaded_reference(page, previous_assets, file_name, timeout_seconds=90):
    deadline = time.time() + timeout_seconds
    normalized_name = os.path.basename(file_name).lower()
    latest_records = []
    while time.time() < deadline:
        latest_records = read_reference_asset_records(page)
        candidates_by_identity = {
            record["identity"]: record
            for record in latest_records
            if record["identity"] not in previous_assets
        }
        candidates = list(candidates_by_identity.values())
        named = [
            record
            for record in candidates
            if normalized_name and normalized_name in record.get("label", "").lower()
        ]
        if named:
            return named[0]
        if len(candidates) == 1:
            return candidates[0]
        page.wait_for_timeout(750)
    raise RuntimeError(f"อัปโหลดรูปอ้างอิงเข้า Google Flow แล้ว แต่ไม่พบ Asset: {normalized_name}")


def sync_reference_image(reference_path, metadata_path, authuser, project_url):
    path = os.path.abspath(reference_path)
    if not os.path.isfile(path):
        raise RuntimeError(f"ไม่พบไฟล์รูปอ้างอิง: {path}")
    cookies = load_session_cookies()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies(cookies)
        page = context.new_page()
        try:
            resolved_project_url = open_or_create_project(page, project_url, authuser)
            previous_assets = {
                record["identity"] for record in read_reference_asset_records(page)
            }
            upload_reference_images(page, [path])
            asset = wait_for_uploaded_reference(page, previous_assets, path)
            workflow_ids = read_asset_workflow_ids(page, [asset.get("mediaId", "")])
            metadata = {
                "projectUrl": resolved_project_url,
                "assetUrl": asset.get("url", ""),
                "mediaId": asset.get("mediaId", ""),
                "workflowId": workflow_ids.get(asset.get("mediaId", ""), ""),
                "assetLabel": asset.get("label", ""),
            }
            with open(metadata_path, "w", encoding="utf-8") as handle:
                json.dump(metadata, handle, ensure_ascii=False)
        finally:
            context.close()
            browser.close()


def read_asset_records(page):
    return page.locator(
        'flow-grid-tile-container img[data-media-id][src*="flow.google.com/asb/"], '
        'flow-grid-tile-container img[data-media-id][src^="https://flow-content.google/image/"], '
        'flow-grid-tile-container img[data-media-id][src*="googleusercontent.com/asb/"]'
    ).evaluate_all(
        """images => images
            .filter((image) => Math.min(image.naturalWidth, image.naturalHeight) >= 300
                && Math.max(image.naturalWidth, image.naturalHeight) >= 512)
            .map((image) => {
                const tile = image.closest('flow-image-tile, flow-tile-container')
                return {
                    url: image.src,
                    mediaId: image.dataset.mediaId || '',
                    identity: image.dataset.mediaId || image.src,
                    label: (tile?.textContent || image.alt || '').trim(),
                }
            })"""
    )


def read_reference_asset_records(page):
    return page.locator(
        'img[data-media-id][src*="flow.google.com/asb/"], '
        'img[data-media-id][src^="https://flow-content.google/image/"], '
        'img[data-media-id][src*="googleusercontent.com/asb/"]'
    ).evaluate_all(
        """images => images.map((image) => {
                const tile = image.closest(
                    'flow-image-tile, flow-tile-container, flow-grid-tile-container'
                )
                return {
                    url: image.src,
                    mediaId: image.dataset.mediaId || '',
                    identity: image.dataset.mediaId || image.src,
                    label: (tile?.textContent || image.alt || '').trim(),
                }
            })"""
    )


def read_asset_urls(page):
    return {record["identity"] for record in read_asset_records(page)}


def read_asset_workflow_ids(page, media_ids):
    unique_media_ids = list(dict.fromkeys(media_id for media_id in media_ids if media_id))
    if not unique_media_ids:
        return {}
    try:
        client = FlowRpcClient(page, page.url)
        result = {}

        def find_workflow_id(value, media_id):
            if not isinstance(value, list):
                return ""
            if len(value) > 2 and value[0] == media_id and isinstance(value[2], str):
                return value[2]
            for child in value:
                found = find_workflow_id(child, media_id)
                if found:
                    return found
            return ""

        for media_id in unique_media_ids:
            workflow_id = find_workflow_id(client.call("Iyc41d", [[media_id]]), media_id)
            if workflow_id:
                result[media_id] = workflow_id
        return result
    except Exception:
        return {}


def rename_flow_workflow(page, workflow_id, display_name):
    workflow_id = str(workflow_id or "").strip()
    display_name = str(display_name or "").strip()
    if not workflow_id:
        raise RuntimeError("Google Flow ไม่ส่ง Workflow ID สำหรับเปลี่ยนชื่อรูป")
    if not display_name:
        raise RuntimeError("ต้องระบุชื่อรูปใน Google Flow")
    FlowRpcClient(page, page.url).rename_workflow(workflow_id, display_name)


def rename_workflow_asset(metadata_path, authuser, project_url, workflow_id, display_name):
    cookies = load_session_cookies()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies(cookies)
        page = context.new_page()
        try:
            resolved_project_url = open_or_create_project(page, project_url, authuser)
            rename_flow_workflow(page, workflow_id, display_name)
            with open(metadata_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "projectUrl": resolved_project_url,
                        "workflowId": workflow_id,
                        "displayName": display_name[:120],
                        "renamed": True,
                    },
                    handle,
                    ensure_ascii=False,
                )
        finally:
            context.close()
            browser.close()


MATCH_STOP_WORDS = {
    "about", "after", "again", "against", "also", "and", "are", "background",
    "character", "create", "exactly", "flow", "for", "from", "full", "generate",
    "image", "into", "only", "prompt", "reference", "render", "sheet", "show",
    "style", "that", "the", "this", "use", "view", "with",
    "favorite", "redo", "more_vert",
}


def asset_prompt_score(label, prompt):
    label = re.sub(r"^(?:favorite|redo|more_vert|image)+", "", label, flags=re.I)
    label_tokens = {
        token
        for token in re.findall(r"[a-z0-9_]+", label.lower())
        if len(token) > 2 and token not in MATCH_STOP_WORDS
    }
    prompt_tokens = set(re.findall(r"[a-z0-9_]+", prompt.lower()))
    return sum(max(1, len(token) - 2) for token in label_tokens if token in prompt_tokens)


def emit_progress(value):
    print(f"FLOW_PROGRESS:{max(0, min(100, int(value)))}", flush=True)


def validate_generation_response(response):
    """Fail immediately when Flow rejects the UI-submitted generation request."""
    try:
        body = response.text()
    except Exception:
        body = ""
    if response.status >= 400:
        raise RuntimeError(
            f"Google Flow ปฏิเสธคำสั่งสร้างภาพ (HTTP {response.status})"
        )
    if "PUBLIC_ERROR_UNUSUAL_ACTIVITY" in body:
        raise RuntimeError(
            "Google Flow ปฏิเสธคำสั่งเนื่องจากตรวจพบกิจกรรมผิดปกติ "
            "กรุณาลองสร้างจากหน้า Flow หนึ่งครั้งแล้วรีเฟรชบัญชี"
        )
    error_markers = (
        "PUBLIC_ERROR_GENERATION_FAILED",
        "PUBLIC_ERROR_INSUFFICIENT_CREDITS",
        "PUBLIC_ERROR_UNSUPPORTED",
    )
    marker = next((value for value in error_markers if value in body), "")
    if marker:
        raise RuntimeError(f"Google Flow ปฏิเสธคำสั่งสร้างภาพ: {marker}")


def make_rpc_progress_reporter(page):
    last_progress = 32
    last_page_check = 0.0

    def report(elapsed, timeout_seconds, _assets):
        nonlocal last_progress, last_page_check
        # Generation usually finishes within a few minutes. Keep the visible ring
        # moving while leaving the final 6% for download and persistence.
        progress_window = min(max(timeout_seconds, 1), 240)
        estimated = min(93, 32 + int((elapsed / progress_window) * 61))
        if estimated > last_progress:
            emit_progress(estimated)
            last_progress = estimated
        if elapsed - last_page_check < 4:
            return
        last_page_check = elapsed
        try:
            body_text = page.locator("body").inner_text(timeout=3000).lower()
        except Exception:
            return
        if any(
            marker in body_text
            for marker in (
                "generation failed",
                "couldn't generate",
                "unable to generate",
                "unusual activity",
            )
        ):
            raise RuntimeError(
                "Google Flow แจ้งว่าสร้างภาพไม่สำเร็จ กรุณาตรวจ Prompt เครดิต และบัญชี"
            )

    return report


def wait_for_batch_baseline(batch_id, expected_assets):
    if not batch_id or expected_assets <= 1:
        return
    safe_batch_id = re.sub(r"[^A-Za-z0-9_-]", "", batch_id)[:120]
    barrier_dir = os.path.join(tempfile.gettempdir(), f"cvb-flow-baseline-{safe_batch_id}")
    os.makedirs(barrier_dir, exist_ok=True)
    participant_path = os.path.join(barrier_dir, f"participant-{os.getpid()}")
    ready_path = os.path.join(barrier_dir, "ready")
    with open(participant_path, "w", encoding="utf-8") as handle:
        handle.write(str(time.time()))
    deadline = time.time() + 15
    while time.time() < deadline:
        participants = [
            name for name in os.listdir(barrier_dir) if name.startswith("participant-")
        ]
        if len(participants) >= expected_assets:
            with open(ready_path, "a", encoding="utf-8"):
                pass
        if os.path.exists(ready_path):
            return
        time.sleep(0.1)
    # A worker may fail or arrive late before reaching this barrier. Requiring
    # every participant would make all otherwise healthy generations fail as a
    # group. Release the workers that did arrive and let RPC asset assignment
    # de-duplicate their results after generation.
    with open(ready_path, "a", encoding="utf-8"):
        pass


def wait_for_new_asset(page, previous_urls, timeout_seconds, prompt, expected_assets):
    deadline = time.time() + timeout_seconds
    started_at = time.time()
    refresh_deadline = time.time() + 45
    refresh_count = 0
    last_status = "Google Flow รับคำสั่งสร้างภาพแล้ว"
    last_progress = 32
    first_candidate_at = None
    last_candidate_change_at = None
    last_candidate_urls = ()
    candidate_snapshot = []
    while time.time() < deadline:
        page.wait_for_timeout(750)
        candidates = [
            record
            for record in read_asset_records(page)
            if record["identity"] not in previous_urls
        ]
        if candidates:
            if first_candidate_at is None:
                first_candidate_at = time.time()
                last_candidate_change_at = first_candidate_at
            candidate_urls = tuple(record["url"] for record in candidates)
            if candidate_urls != last_candidate_urls:
                last_candidate_urls = candidate_urls
                last_candidate_change_at = time.time()
            candidate_snapshot = candidates
            # Concurrent Flow requests often finish a few seconds apart. Let the media grid
            # settle so every worker can see the whole batch before choosing its own result.
            candidate_age = time.time() - first_candidate_at
            stable_age = time.time() - (last_candidate_change_at or first_candidate_at)
            if (
                len(candidate_snapshot) >= expected_assets
                and candidate_age >= 8
                and stable_age >= 2
            ) or candidate_age >= 18:
                ranked = sorted(
                    enumerate(candidate_snapshot),
                    key=lambda item: (
                        asset_prompt_score(item[1].get("label", ""), prompt),
                        item[0],
                    ),
                    reverse=True,
                )
                ranked_records = [
                    {
                        **record,
                        "score": asset_prompt_score(record.get("label", ""), prompt),
                    }
                    for _, record in ranked
                ]
                emit_progress(94)
                return ranked_records[0], ranked_records
        body_text = page.locator("body").inner_text(timeout=5000)
        lowered = body_text.lower()
        for marker in ("generation failed", "couldn't generate", "unable to generate"):
            if marker in lowered:
                raise RuntimeError("Google Flow แจ้งว่าสร้างภาพไม่สำเร็จ กรุณาตรวจ Prompt และเครดิต")
        visible_progress = [
            int(value) for value in re.findall(r"(?<!\d)(\d{1,3})%", body_text)
            if 0 <= int(value) <= 100
        ]
        progress_visible = bool(visible_progress)
        if progress_visible or any(
            marker in lowered for marker in ("generating", "creating", "thinking", "analyzing")
        ):
            last_status = "Google Flow ยังสร้างภาพอยู่"
        elapsed = time.time() - started_at
        estimated_progress = min(90, 32 + int((elapsed / max(timeout_seconds, 1)) * 65))
        observed_progress = max(visible_progress, default=0)
        next_progress = max(last_progress, observed_progress, estimated_progress)
        if next_progress != last_progress:
            emit_progress(next_progress)
            last_progress = next_progress
        # Flow sometimes finishes server-side without updating the headless page's media grid.
        # Reloading the same project reveals the completed tile without starting another generation.
        if time.time() >= refresh_deadline and refresh_count < 2:
            page.reload(wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(3000)
            close_optional_ui(page)
            refresh_count += 1
            refresh_deadline = time.time() + 45
    suffix = f" ({last_status})" if last_status else ""
    raise RuntimeError(f"รอภาพจาก Google Flow เกินเวลาที่กำหนด{suffix}")


def flow_source_rendition_urls(asset_url, resolution="1K"):
    """Return authenticated Google image URLs ordered from best to fallback.

    Flow's RPC currently returns a bare ``lh3.googleusercontent.com/asb/...`` URL.
    Fetching that URL directly is only a 512 px preview. The Flow UI's
    ``Download > 1K Original size`` action requests a larger rendition instead.
    Older Flow responses already included a suffix such as ``=s512-rw``, so this
    helper deliberately supports both URL shapes and preserves query parameters.
    """
    parsed = urlsplit(asset_url)
    rendition_pattern = r"=(?:s\d+|w\d+(?:-h\d+)?|h\d+)(?:-[a-z0-9]+)*$"
    has_rendition = bool(re.search(rendition_pattern, parsed.path, flags=re.IGNORECASE))
    if (
        parsed.netloc == "flow-content.google"
        and parsed.path.startswith("/image/")
        and not has_rendition
    ):
        return [asset_url]
    source_path = re.sub(
        rendition_pattern,
        "",
        parsed.path,
        flags=re.IGNORECASE,
    )

    def with_rendition(rendition):
        return urlunsplit(parsed._replace(path=f"{source_path}={rendition}"))

    config = IMAGE_RESOLUTION_CONFIG[resolution]
    candidates = [
        with_rendition(f"s{config['rendition_size']}-rw"),
        with_rendition("s0-rw"),
        asset_url,
    ]
    return list(dict.fromkeys(candidates))


def download_as_png(context, asset_url, output_path, resolution="1K"):
    config = IMAGE_RESOLUTION_CONFIG[resolution]
    last_error = "unknown error"
    for full_size_url in flow_source_rendition_urls(asset_url, resolution):
        for attempt in range(3):
            try:
                response = context.request.get(
                    full_size_url,
                    headers={"Referer": "https://flow.google.com/"},
                    timeout=120000,
                )
                if not response.ok:
                    last_error = f"HTTP {response.status}"
                else:
                    content_type = (response.headers.get("content-type") or "").lower()
                    if content_type and not content_type.startswith("image/"):
                        last_error = f"ชนิดข้อมูล {content_type}"
                    else:
                        with Image.open(BytesIO(response.body())) as image:
                            image.load()
                            converted = (
                                image.convert("RGBA")
                                if image.mode in ("RGBA", "LA")
                                else image.convert("RGB")
                            )
                        if max(converted.width, converted.height) < config["minimum_long_edge"]:
                            last_error = (
                                f"Flow ส่งภาพพรีวิว {converted.width}x{converted.height} "
                                f"แทนไฟล์ {resolution}"
                            )
                            break
                        converted.save(output_path, format="PNG", optimize=True)
                        # Flow can expose the same generated asset through multiple URLs/renditions.
                        # A difference hash gives the companion a stable visual identity so a batch
                        # cannot assign the same picture to more than one card.
                        fingerprint_image = converted.convert("L").resize(
                            (17, 16), Image.Resampling.LANCZOS
                        )
                        pixels = list(fingerprint_image.getdata())
                        fingerprint_bits = []
                        for row in range(16):
                            offset = row * 17
                            for column in range(16):
                                fingerprint_bits.append(
                                    "1"
                                    if pixels[offset + column] > pixels[offset + column + 1]
                                    else "0"
                                )
                        fingerprint_bytes = int("".join(fingerprint_bits), 2).to_bytes(32, "big")
                        content_hash = hashlib.sha256(fingerprint_bytes).hexdigest()
                        return converted.width, converted.height, content_hash
            except Exception as error:
                last_error = str(error) or error.__class__.__name__
            if attempt < 2:
                time.sleep(min(4, 1 + (attempt * 1.5)))
    raise RuntimeError(
        f"ดาวน์โหลดภาพ {resolution} จาก Google Flow ไม่สำเร็จ "
        f"(บัญชีอาจไม่มีสิทธิ์ใช้ความละเอียดนี้) และจะไม่บันทึกภาพขนาดต่ำกว่าแทน: {last_error}"
    )


def generate_image(
    prompt,
    output_path,
    metadata_path,
    aspect_ratio,
    model,
    image_resolution,
    agent_mode,
    authuser,
    project_url,
    timeout_seconds,
    reference_specs,
    expected_assets,
    batch_id,
):
    cookies = load_session_cookies()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies(cookies)
        page = context.new_page()
        try:
            emit_progress(3)
            resolved_project_url = open_or_create_project(page, project_url, authuser)
            emit_progress(10)
            retry_dynamic_ui(
                page,
                lambda: configure_agent_mode(page, agent_mode),
                "ขณะเลือกโหมด",
            )
            prompt_editor = find_prompt_editor(page)
            retry_dynamic_ui(
                page,
                lambda: configure_image_defaults(page, aspect_ratio, model),
                "ขณะตั้งค่าภาพ",
            )
            emit_progress(18)
            prompt_editor.fill(prompt)
            page.wait_for_timeout(500)
            if not prompt_editor.inner_text().strip():
                raise RuntimeError("Google Flow ไม่รับข้อความ Prompt กรุณาลองใหม่")
            attach_reference_images(page, reference_specs)
            emit_progress(24)
            rpc_client = FlowRpcClient(page, resolved_project_url)
            previous_workflow_ids = {
                asset.get("workflowId") for asset in rpc_client.list_image_assets()
                if asset.get("workflowId")
            }
            # All workers in a concurrent batch must snapshot the same media grid
            # before any one of them submits. Otherwise an early result can appear
            # in another worker's baseline and be incorrectly ignored.
            wait_for_batch_baseline(batch_id, expected_assets)
            submit_generation_with_retry(page, rpc_client, previous_workflow_ids)
            emit_progress(32)
            rpc_assets = rpc_client.wait_for_new_images(
                previous_workflow_ids,
                expected=expected_assets,
                timeout_seconds=timeout_seconds,
                on_poll=make_rpc_progress_reporter(page),
            )
            candidates = sorted(
                (
                    {
                        "url": candidate.get("assetUrl", ""),
                        "mediaId": candidate.get("mediaId", ""),
                        "workflowId": candidate.get("workflowId", ""),
                        "label": candidate.get("displayName", ""),
                        "score": asset_prompt_score(candidate.get("displayName", ""), prompt),
                    }
                    for candidate in rpc_assets
                    if candidate.get("assetUrl")
                ),
                key=lambda candidate: (candidate["score"], candidate["workflowId"]),
                reverse=True,
            )
            if not candidates:
                raise RuntimeError("Google Flow สร้างภาพแล้ว แต่ RPC ไม่ส่งข้อมูล Asset กลับมา")
            candidate_metadata = []
            for index, candidate in enumerate(candidates):
                candidate_path = f"{output_path}.candidate-{index}.png"
                try:
                    candidate_width, candidate_height, candidate_content_hash = download_as_png(
                        context, candidate["url"], candidate_path, image_resolution
                    )
                except Exception:
                    continue
                candidate_metadata.append(
                    {
                        "assetUrl": candidate["url"],
                        "mediaId": candidate.get("mediaId", ""),
                        "workflowId": candidate.get("workflowId", ""),
                        "assetLabel": candidate.get("label", ""),
                        "score": candidate.get("score", 0),
                        "localFile": os.path.basename(candidate_path),
                        "width": candidate_width,
                        "height": candidate_height,
                        "contentHash": candidate_content_hash,
                    }
                )
            if not candidate_metadata:
                raise RuntimeError("Google Flow สร้างภาพแล้ว แต่ดาวน์โหลด Asset ที่สร้างใหม่ไม่ได้")
            primary = candidate_metadata[0]
            primary_path = os.path.join(os.path.dirname(output_path), primary["localFile"])
            os.replace(primary_path, output_path)
            primary["localFile"] = os.path.basename(output_path)
            asset_url = primary["assetUrl"]
            asset = next(
                candidate for candidate in candidates if candidate["url"] == asset_url
            )
            width = primary["width"]
            height = primary["height"]
            emit_progress(98)
            metadata = {
                "projectUrl": resolved_project_url,
                "assetUrl": asset_url,
                "mediaId": asset.get("mediaId", ""),
                "workflowId": asset.get("workflowId", ""),
                "width": width,
                "height": height,
                "aspectRatio": aspect_ratio,
                "model": model,
                "resolution": image_resolution,
                "mode": "Agent" if agent_mode else "Standard",
                "referenceCount": len(reference_specs),
                "assetLabel": asset.get("label", ""),
                "candidates": candidate_metadata,
            }
            with open(metadata_path, "w", encoding="utf-8") as handle:
                json.dump(metadata, handle, ensure_ascii=False)
            emit_progress(100)
            print(f"สร้างภาพ Google Flow สำเร็จ: {output_path}")
        except PlaywrightTimeoutError as error:
            raise RuntimeError(f"หน้า Google Flow ไม่ตอบสนองตามเวลาที่กำหนด: {error}") from error
        finally:
            browser.close()


def ensure_project(metadata_path, authuser, project_url):
    cookies = load_session_cookies()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies(cookies)
        page = context.new_page()
        try:
            resolved_project_url = open_or_create_project(page, project_url, authuser)
            with open(metadata_path, "w", encoding="utf-8") as handle:
                json.dump({"projectUrl": resolved_project_url}, handle, ensure_ascii=False)
        finally:
            browser.close()


def check_resolution_access(metadata_path, authuser, project_url):
    if not project_url or "/project/" not in project_url:
        raise RuntimeError("ต้องมี Google Flow Project ที่มีภาพอย่างน้อย 1 ภาพก่อนตรวจสิทธิ์")
    cookies = load_session_cookies()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies(cookies)
        page = context.new_page()
        try:
            resolved_project_url = open_or_create_project(page, project_url, authuser)
            assets = FlowRpcClient(page, resolved_project_url).list_image_assets()
            asset = next(
                (candidate for candidate in reversed(assets) if candidate.get("mediaId")),
                None,
            )
            if not asset:
                raise RuntimeError("โปรเจกต์นี้ยังไม่มีภาพสำหรับเปิดเมนูตรวจสิทธิ์ความละเอียด")
            detail_url = f"{resolved_project_url.rstrip('/')}/edit/{asset['mediaId']}"
            goto_with_retry(page, with_authuser(detail_url, authuser))
            page.wait_for_timeout(10000)
            download = first_visible(
                page.get_by_role("button", name=re.compile(r"Download|ดาวน์โหลด", re.I))
            )
            if not download:
                download = first_visible(
                    page.locator(
                        'button[aria-label*="download" i], button[title*="download" i], '
                        '[role="button"][aria-label*="download" i]'
                    )
                )
            if not download:
                visible_buttons = page.locator("button").evaluate_all(
                    """buttons => buttons
                        .filter(button => button.offsetParent !== null)
                        .map(button => button.getAttribute('aria-label') || button.title || button.innerText)
                        .map(value => (value || '').trim())
                        .filter(Boolean)
                        .slice(0, 30)"""
                )
                raise RuntimeError(
                    "ไม่พบเมนู Download สำหรับตรวจสิทธิ์ความละเอียด "
                    f"(URL: {page.url}; ปุ่มที่พบ: {', '.join(visible_buttons)}; "
                    f"ข้อความ: {page.locator('body').inner_text(timeout=5000)[:500]})"
                )
            download.click(timeout=10000)
            page.get_by_text("1K", exact=True).first.wait_for(state="visible", timeout=10000)

            access = {}
            for resolution in ("1K", "2K", "4K"):
                menu_item = first_visible(
                    page.get_by_role(
                        "menuitem",
                        name=re.compile(rf"^{re.escape(resolution)}\b", re.I),
                    )
                )
                if menu_item:
                    access[resolution] = bool(menu_item.is_enabled())
                    continue
                labels = page.get_by_text(resolution, exact=True)
                label = first_visible(labels)
                if not label:
                    access[resolution] = False
                    continue
                access[resolution] = bool(label.evaluate(
                    """(node) => {
                        const menuItem = node.closest('[role="menuitem"]')
                        if (menuItem) {
                            return !menuItem.matches(':disabled') &&
                                menuItem.getAttribute('aria-disabled') !== 'true'
                        }
                        let row = node
                        for (let i = 0; i < 6 && row; i += 1, row = row.parentElement) {
                            const text = (row.innerText || '').trim()
                            if (!text.includes(node.textContent.trim())) continue
                            if (!/(Original size|Upscaled|ขนาดต้นฉบับ)/i.test(text)) continue
                            const disabled = row.matches(':disabled') ||
                                row.getAttribute('aria-disabled') === 'true' ||
                                /disabled/i.test(row.className || '')
                            return !disabled && !/Upgrade|อัปเกรด/i.test(text)
                        }
                        return true
                    }"""
                ))

            available = [resolution for resolution in ("1K", "2K", "4K") if access[resolution]]
            with open(metadata_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {"projectUrl": resolved_project_url, "resolutions": available},
                    handle,
                    ensure_ascii=False,
                )
        finally:
            context.close()
            browser.close()


def main():
    parser = argparse.ArgumentParser(description="Generate one Google Flow image through a signed-in session")
    parser.add_argument("--ensure-project", action="store_true")
    parser.add_argument("--check-resolution-access", action="store_true")
    parser.add_argument("--sync-reference", action="store_true")
    parser.add_argument("--rename-workflow", action="store_true")
    prompt_source = parser.add_mutually_exclusive_group(required=False)
    prompt_source.add_argument("--prompt")
    prompt_source.add_argument("--prompt-file")
    parser.add_argument("--out", default="")
    parser.add_argument("--meta", required=True)
    parser.add_argument("--aspect", default="16:9", choices=sorted(SUPPORTED_ASPECT_RATIOS))
    parser.add_argument("--model", default="Nano Banana 2.1", choices=sorted(SUPPORTED_IMAGE_MODELS))
    parser.add_argument("--resolution", default="1K", choices=sorted(IMAGE_RESOLUTION_CONFIG))
    parser.add_argument("--agent", action="store_true")
    parser.add_argument("--authuser", default=None)
    parser.add_argument("--project-url", default="")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--reference", action="append", default=[])
    parser.add_argument("--references-file", default="")
    parser.add_argument("--expected-assets", type=int, default=1)
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--workflow-id", default="")
    parser.add_argument("--display-name", default="")
    args = parser.parse_args()
    if args.ensure_project:
        ensure_project(args.meta, args.authuser, args.project_url)
        return
    if args.check_resolution_access:
        check_resolution_access(args.meta, args.authuser, args.project_url)
        return
    if args.rename_workflow:
        rename_workflow_asset(
            args.meta,
            args.authuser,
            args.project_url,
            args.workflow_id,
            args.display_name,
        )
        return
    if args.sync_reference:
        if len(args.reference) != 1:
            raise RuntimeError("การซิงก์รูปอ้างอิงต้องระบุไฟล์รูป 1 ไฟล์")
        sync_reference_image(
            args.reference[0],
            args.meta,
            args.authuser,
            args.project_url,
        )
        return
    if not args.prompt and not args.prompt_file:
        raise RuntimeError("ต้องระบุ Prompt สำหรับการสร้างภาพ")
    if not args.out:
        raise RuntimeError("ต้องระบุไฟล์ผลลัพธ์สำหรับการสร้างภาพ")
    prompt = args.prompt
    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as handle:
            prompt = handle.read()
    reference_specs = [
        {"path": path, "assetUrl": "", "mediaId": "", "displayName": ""}
        for path in args.reference
    ]
    if args.references_file:
        with open(args.references_file, "r", encoding="utf-8") as handle:
            loaded_references = json.load(handle)
        if not isinstance(loaded_references, list):
            raise RuntimeError("ข้อมูลรูปอ้างอิงไม่ถูกต้อง")
        reference_specs = [
            {
                "path": str(reference.get("path") or ""),
                "assetUrl": str(reference.get("assetUrl") or ""),
                "mediaId": str(reference.get("mediaId") or ""),
                "displayName": str(reference.get("displayName") or ""),
            }
            for reference in loaded_references
            if isinstance(reference, dict)
        ]
    generate_image(
        prompt,
        args.out,
        args.meta,
        args.aspect,
        args.model,
        args.resolution,
        args.agent,
        args.authuser,
        args.project_url,
        max(60, min(args.timeout, 900)),
        reference_specs,
        max(1, min(args.expected_assets, 20)),
        args.batch_id,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FLOW_IMAGE_ERROR: {error}", file=sys.stderr)
        sys.exit(1)
