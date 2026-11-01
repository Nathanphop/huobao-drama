import argparse
import json
import os
import re
import sys
import time

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from google_flow_image_auto import (
    attach_reference_images,
    close_optional_ui,
    first_visible,
    find_prompt_editor,
    load_session_cookies,
    normalize_flow_asset_url,
    open_or_create_project,
    wait_for_visible,
)


for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


SUPPORTED_ASPECT_RATIOS = {"16:9", "9:16"}
SUPPORTED_VIDEO_MODELS = {
    "Omni 1.1 Flash",
    "Veo 3.1 - Lite",
    "Veo 3.1 - Fast",
    "Veo 3.1 - Quality",
}


def emit_flow_progress(percent, stage="rendering"):
    value = max(0, min(100, int(round(float(percent)))))
    print(
        "FLOW_PROGRESS: "
        + json.dumps({"percent": value, "stage": stage}, ensure_ascii=False),
        flush=True,
    )


def click_radio(page, label):
    radio = page.get_by_role("radio", name=label, exact=True)
    # Flow can append helper copy to an option's accessible name (for example,
    # "360p generates faster at lower resolution"). Keep exact matching as the
    # preferred path, then fall back to a boundary-safe prefix match.
    if not radio.count():
        radio = page.get_by_role(
            "radio",
            name=re.compile(rf"^{re.escape(label)}(?:\s|$)", re.I),
        )
    radio = wait_for_visible(
        page,
        radio,
        f"ตัวเลือก {label}",
    )
    if radio.get_attribute("aria-checked") != "true":
        radio.click(timeout=10000)
        page.wait_for_timeout(350)


def select_video_model(page, model):
    trigger = wait_for_visible(
        page,
        page.get_by_role("button", name="Select model family", exact=True),
        "ตัวเลือกโมเดลวิดีโอ",
    )
    trigger.click(timeout=10000)
    option = page.get_by_role("menuitem", name=re.compile(rf"^{re.escape(model)}$", re.I))
    if not option.count():
        option = page.get_by_role("menuitem", name=re.compile(re.escape(model), re.I))
    if not option.count():
        raise RuntimeError(f"บัญชีนี้ไม่มีโมเดลวิดีโอ {model} ใน Google Flow")
    option.first.click(timeout=10000)
    page.wait_for_timeout(350)


def find_video_settings_trigger(page, timeout_ms=30000):
    """Find the compact generation-settings button across Flow UI rollouts.

    Flow occasionally replaces the prompt composer while an Angular chunk is
    loading. During that window an exact accessible-name lookup can return no
    matches even though the button appears a moment later. Prefer the stable
    accessible name, accept an appended summary, and reload the project once
    before reporting a real UI incompatibility.
    """
    for attempt in range(2):
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            candidates = (
                page.get_by_role(
                    "button",
                    name=re.compile(r"Settings\s*trigger", re.I),
                ),
                page.locator(
                    'button[aria-label*="settings" i]:not([aria-label="Settings"])'
                ),
                page.locator(
                    'button:has-text("Veo"), button:has-text("Omni"), '
                    'button:has-text("Video ·")'
                ),
            )
            for locator in candidates:
                trigger = first_visible(locator)
                if trigger:
                    return trigger
            page.wait_for_timeout(250)
        if attempt == 0:
            page.reload(wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(5000)
            close_optional_ui(page)
    raise RuntimeError("ไม่พบปุ่มตั้งค่าวิดีโอที่มองเห็นได้ใน Google Flow หลังรีโหลดแล้ว")


def configure_video_settings(page, aspect_ratio, model, video_type, resolution, duration):
    # Waiting for the editor first distinguishes a composer that has not loaded
    # from a genuine settings-button rollout.
    find_prompt_editor(page)
    trigger = find_video_settings_trigger(page)
    trigger.click(timeout=10000)
    page.wait_for_timeout(300)
    click_radio(page, "Video")
    click_radio(page, video_type)
    click_radio(page, aspect_ratio)
    select_video_model(page, model)
    if model == "Omni 1.1 Flash":
        click_radio(page, resolution)
        click_radio(page, f"{duration}s")
    click_radio(page, "x1")
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)


def attach_frame_image(page, spec, slot):
    """Attach an existing Flow asset to the Start or End frame picker."""
    slot_button = wait_for_visible(
        page,
        page.get_by_role("button", name=slot, exact=True),
        f"ปุ่ม {slot} frame",
    )
    slot_button.click(timeout=10000)
    wait_for_visible(
        page,
        page.get_by_role("heading", name="Select a frame image", exact=True),
        "หน้าต่างเลือกภาพ Frame",
    )

    local_path = os.path.abspath(str(spec.get("path") or ""))
    display_name = str(spec.get("displayName") or "").strip()
    media_id = str(spec.get("mediaId") or "").strip()
    asset_url = normalize_flow_asset_url(spec.get("assetUrl", ""))
    options = page.locator('[role="option"]')

    # References originating in Huobao are local temporary files and do not
    # have a Flow asset identity yet. Upload them from inside the frame picker;
    # Flow attaches the uploaded file to the selected Start/End slot directly.
    if local_path and not (display_name or media_id or asset_url):
        if not os.path.isfile(local_path):
            raise RuntimeError(f"ไม่พบไฟล์ภาพสำหรับ {slot} frame")
        upload = first_visible(
            page.get_by_role(
                "button",
                name=re.compile(r"^Upload media$|^อัปโหลด(?:สื่อ)?$", re.I),
            )
        ) or first_visible(
            page.get_by_text(
                re.compile(r"^Upload media$|^อัปโหลด(?:สื่อ)?$", re.I),
                exact=True,
            )
        )
        if not upload:
            raise RuntimeError(f"Google Flow ไม่พบปุ่ม Upload media สำหรับ {slot} frame")
        previous_urls = set(
            options.evaluate_all(
                "items => items.flatMap(item => "
                "[...item.querySelectorAll('img')].map(image => image.currentSrc || image.src)"
                ").filter(Boolean)"
            )
        )
        try:
            with page.expect_file_chooser(timeout=10000) as chooser_info:
                upload.click(timeout=10000)
            chooser_info.value.set_files(local_path)
        except Exception:
            file_inputs = page.locator('input[type="file"]')
            if not file_inputs.count():
                raise RuntimeError(f"Google Flow ไม่เปิดตัวเลือกไฟล์สำหรับ {slot} frame")
            file_inputs.last.set_input_files(local_path, timeout=30000)

        # Current Flow keeps the picker open. Wait for the uploading tile to
        # become a real asset, prefer its new media URL over same-name older
        # uploads, then select/confirm it as the requested frame.
        file_name = os.path.basename(local_path).lower()
        matching_index = -1
        fallback_index = -1
        deadline = time.time() + 90
        while time.time() < deadline:
            match = options.evaluate_all(
                r"""(items, target) => {
                    const candidates = items.map((item, index) => ({
                        index,
                        text: (item.textContent || '').trim().toLowerCase(),
                        urls: [...item.querySelectorAll('img')]
                            .map(image => image.currentSrc || image.src)
                            .filter(Boolean),
                    })).filter(item =>
                        !item.text.startsWith('uploading') &&
                        item.text.includes(target.fileName)
                    )
                    const fresh = candidates.find(item =>
                        item.urls.some(url => !target.previousUrls.includes(url))
                    )
                    return {
                        freshIndex: fresh?.index ?? -1,
                        fallbackIndex: candidates[0]?.index ?? -1,
                    }
                }""",
                {"fileName": file_name, "previousUrls": list(previous_urls)},
            )
            matching_index = int(match.get("freshIndex", -1))
            fallback_index = int(match.get("fallbackIndex", fallback_index))
            if matching_index >= 0:
                text = options.nth(matching_index).inner_text().strip().lower()
                if not text.startswith("uploading"):
                    break
            page.wait_for_timeout(1000)
        if matching_index < 0:
            matching_index = fallback_index
        if matching_index < 0:
            raise RuntimeError(f"Google Flow อัปโหลดภาพ {slot} frame แล้วแต่ไม่พบในรายการ Asset")

        options.nth(matching_index).click(timeout=10000)
        page.wait_for_timeout(700)
        if not first_visible(
            page.get_by_role("heading", name="Select a frame image", exact=True)
        ):
            return
        confirm = first_visible(
            page.get_by_role("button", name="Add to prompt", exact=True)
        )
        for _ in range(60):
            if confirm and confirm.is_enabled():
                confirm.click(timeout=10000)
                page.wait_for_timeout(700)
                return
            page.wait_for_timeout(500)
            confirm = first_visible(
                page.get_by_role("button", name="Add to prompt", exact=True)
            )
        raise RuntimeError(f"Google Flow ยังไม่พร้อมแนบภาพเป็น {slot} frame")

    search = first_visible(page.get_by_role("textbox", name="Search assets", exact=True))
    if search and display_name:
        search.fill(display_name)
        page.wait_for_timeout(700)

    try:
        options.first.wait_for(state="visible", timeout=15000)
    except Exception:
        options = page.locator("flow-asset-picker-item")
        options.first.wait_for(state="visible", timeout=15000)

    matching_index = options.evaluate_all(
        r"""(items, target) => {
            const normalize = (value) => {
                let url = String(value || '').trim().split('?', 1)[0]
                if (url.includes('/asb/')) url = url.replace(/=s\d+(?:-[a-z]+)*$/i, '')
                return url
            }
            return items.findIndex((item) => {
                const title = (
                    item.querySelector('.asset-title')?.textContent ||
                    item.getAttribute('aria-label') ||
                    item.textContent || ''
                ).trim().toLowerCase()
                const images = [...item.querySelectorAll('img')]
                const imageMatches = images.some((image) =>
                    (target.mediaId && image.dataset.mediaId === target.mediaId) ||
                    (target.url && normalize(image.src) === target.url)
                )
                return imageMatches || (
                    target.displayName &&
                    (title === target.displayName || title.includes(target.displayName))
                )
            })
        }""",
        {
            "url": asset_url,
            "mediaId": media_id,
            "displayName": display_name.lower(),
        },
    )
    if matching_index < 0:
        page.keyboard.press("Escape")
        label = display_name or media_id or asset_url
        raise RuntimeError(
            f"ไม่พบ Asset {label} ในตัวเลือก {slot} frame ของ Google Flow"
        )

    confirm = first_visible(
        page.get_by_role("button", name="Add to prompt", exact=True)
    )
    # Exact search currently preselects the single matching asset. Older Flow
    # builds require clicking the option first, so do that only when the
    # confirmation button is not already enabled.
    confirm_enabled = False
    if confirm:
        try:
            confirm_enabled = confirm.is_enabled(timeout=500)
        except Exception:
            confirm_enabled = False
    if not confirm_enabled:
        options.nth(matching_index).click(timeout=10000)
        page.wait_for_timeout(350)
        # Some builds attach immediately when the option is clicked.
        if not first_visible(
            page.get_by_role("heading", name="Select a frame image", exact=True)
        ):
            page.wait_for_timeout(500)
            return
        confirm = first_visible(
            page.get_by_role("button", name="Add to prompt", exact=True)
        )

    if not confirm:
        raise RuntimeError(f"Google Flow ไม่พบปุ่มยืนยัน {slot} frame")
    for _ in range(30):
        try:
            if confirm.is_enabled(timeout=500):
                break
        except Exception:
            pass
        page.wait_for_timeout(200)
    try:
        confirm_enabled = confirm.is_enabled(timeout=500)
    except Exception:
        confirm_enabled = False
    if not confirm_enabled:
        raise RuntimeError(f"Google Flow ยังไม่พร้อมแนบภาพเป็น {slot} frame")
    confirm.click(timeout=10000)
    page.wait_for_timeout(700)


def attach_frame_images(page, reference_specs):
    specs = [spec for spec in reference_specs if spec.get("path")]
    if not specs:
        return
    if len(specs) > 2:
        raise RuntimeError("โหมด Frames รองรับภาพ Start และ End รวมสูงสุด 2 ภาพ")
    for index, spec in enumerate(specs):
        attach_frame_image(page, spec, "Start" if index == 0 else "End")


def read_video_tiles(page):
    """Return stable URLs for generated videos currently present in the media grid.

    Flow has shipped both image-thumbnail and inline-video implementations for
    completed video tiles. Supporting both keeps a UI rollout from making a
    completed generation appear to be pending forever.
    """
    return {
        url
        for url in page.locator(
            'video[aria-label="Generated video"], img[alt="Generated video thumbnail"]'
        ).evaluate_all(
            "elements => elements.map(element => element.currentSrc || element.src).filter(Boolean)"
        )
    }


def wait_for_new_video_tile(page, previous_urls, timeout_seconds):
    deadline = time.time() + timeout_seconds
    refresh_deadline = time.time() + 60
    refresh_count = 0
    last_status = "Google Flow รับคำสั่งสร้างวิดีโอแล้ว"
    while time.time() < deadline:
        page.wait_for_timeout(2500)
        candidates = [url for url in read_video_tiles(page) if url not in previous_urls]
        if candidates:
            return candidates[-1]
        body_text = page.locator("body").inner_text(timeout=5000)
        lowered = body_text.lower()
        progress_texts = page.locator("flow-grid-tile-container").all_inner_texts()
        progress_source = next(
            (text for text in progress_texts if re.search(r"(?<!\d)\d{1,3}%", text)),
            body_text,
        )
        progress_values = [
            int(value)
            for value in re.findall(r"(?<!\d)(\d{1,3})%", progress_source)
            if 0 <= int(value) <= 100
        ]
        if progress_values:
            emit_flow_progress(progress_values[-1], "rendering")
        for marker in (
            "video generation failed",
            "generation failed",
            "couldn't generate",
            "unable to generate",
        ):
            if marker in lowered:
                raise RuntimeError(
                    "Google Flow แจ้งว่าสร้างวิดีโอไม่สำเร็จ กรุณาตรวจ Prompt เครดิต และข้อจำกัดของโมเดล"
                )
        if progress_values or any(
            marker in lowered
            for marker in ("generating", "creating", "thinking", "analyzing")
        ):
            last_status = "Google Flow ยังสร้างวิดีโออยู่"
        if time.time() >= refresh_deadline and refresh_count < 3:
            page.reload(wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(4000)
            close_optional_ui(page)
            refresh_count += 1
            refresh_deadline = time.time() + 60
    raise RuntimeError(f"รอวิดีโอจาก Google Flow เกินเวลาที่กำหนด ({last_status})")


def download_video_tile(page, asset_url, output_path, resolution):
    escaped_url = asset_url.replace('\\', '\\\\').replace('"', '\\"')
    media = page.locator(
        f'video[aria-label="Generated video"][src="{escaped_url}"], '
        f'img[alt="Generated video thumbnail"][src="{escaped_url}"]'
    )
    media = wait_for_visible(page, media, "วิดีโอที่สร้างเสร็จ")
    card = media.locator("xpath=ancestor::flow-grid-tile-container[1]")
    media.hover()
    more = wait_for_visible(
        page,
        card.get_by_role("button", name="More options", exact=True),
        "เมนูวิดีโอ",
    )
    more.click(timeout=10000)
    download_item = wait_for_visible(
        page,
        page.get_by_role("menuitem", name="Download", exact=True),
        "เมนู Download",
    )
    download_item.hover()
    download_item.click(timeout=10000)
    original = wait_for_visible(
        page,
        page.get_by_role(
            "menuitem",
            name=re.compile(rf"^{re.escape(resolution)}\s+Original size$", re.I),
        ),
        f"ตัวเลือกดาวน์โหลด {resolution} Original size",
    )
    with page.expect_download(timeout=180000) as download_info:
        original.click(timeout=10000)
    download = download_info.value
    download.save_as(output_path)
    size = os.path.getsize(output_path)
    if size <= 0:
        raise RuntimeError("Google Flow ดาวน์โหลดไฟล์วิดีโอเปล่ากลับมา")
    return "video/mp4", size


def generate_video(
    prompt,
    output_path,
    metadata_path,
    aspect_ratio,
    model,
    authuser,
    project_url,
    timeout_seconds,
    video_type,
    resolution,
    duration,
    reference_specs,
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
            emit_flow_progress(0, "opening")
            resolved_project_url = open_or_create_project(page, project_url, authuser)
            emit_flow_progress(0, "configuring")
            configure_video_settings(page, aspect_ratio, model, video_type, resolution, duration)
            emit_flow_progress(0, "preparing")
            if reference_specs:
                if video_type == "Frames":
                    attach_frame_images(page, reference_specs)
                else:
                    attach_reference_images(page, reference_specs)
            emit_flow_progress(0, "submitting")
            prompt_editor = find_prompt_editor(page)
            previous_urls = read_video_tiles(page)
            prompt_editor.fill(prompt)
            page.wait_for_timeout(500)
            if not prompt_editor.inner_text().strip():
                raise RuntimeError("Google Flow ไม่รับข้อความ Prompt กรุณาลองใหม่")
            start = wait_for_visible(
                page,
                page.get_by_role("button", name="Start generation", exact=True),
                "ปุ่ม Start generation",
            )
            for _ in range(20):
                if start.is_enabled():
                    break
                page.wait_for_timeout(250)
            if not start.is_enabled():
                raise RuntimeError("ปุ่ม Start generation ยังไม่พร้อมหลังตั้งค่าวิดีโอ")
            start.click(timeout=15000)
            emit_flow_progress(0, "rendering")
            asset_url = wait_for_new_video_tile(page, previous_urls, timeout_seconds)
            emit_flow_progress(98, "downloading")
            content_type, size = download_video_tile(
                page, asset_url, output_path, resolution
            )
            emit_flow_progress(99, "saving")
            if resolution == "360p":
                width, height = ((640, 360) if aspect_ratio == "16:9" else (360, 640))
            else:
                width, height = ((1280, 720) if aspect_ratio == "16:9" else (720, 1280))
            metadata = {
                "projectUrl": resolved_project_url,
                "assetUrl": asset_url,
                "width": width,
                "height": height,
                "duration": float(duration),
                "size": size,
                "contentType": content_type,
                "aspectRatio": aspect_ratio,
                "model": model,
                "videoType": video_type,
                "outputCount": 1,
            }
            with open(metadata_path, "w", encoding="utf-8") as handle:
                json.dump(metadata, handle, ensure_ascii=False)
            print(f"สร้างวิดีโอ Google Flow สำเร็จ: {output_path}")
        except PlaywrightTimeoutError as error:
            raise RuntimeError(f"หน้า Google Flow ไม่ตอบสนองตามเวลาที่กำหนด: {error}") from error
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(
        description="Generate one Google Flow video through a signed-in session"
    )
    prompt_source = parser.add_mutually_exclusive_group(required=True)
    prompt_source.add_argument("--prompt")
    prompt_source.add_argument("--prompt-file")
    parser.add_argument("--out", required=True)
    parser.add_argument("--meta", required=True)
    parser.add_argument("--aspect", default="16:9", choices=sorted(SUPPORTED_ASPECT_RATIOS))
    parser.add_argument("--model", default="Veo 3.1 - Lite", choices=sorted(SUPPORTED_VIDEO_MODELS))
    parser.add_argument("--authuser", default=None)
    parser.add_argument("--project-url", default="")
    parser.add_argument("--video-type", default="Ingredients", choices=["Frames", "Ingredients"])
    parser.add_argument("--resolution", default="720p", choices=["360p", "720p"])
    parser.add_argument("--duration", type=int, default=8, choices=[4, 6, 8, 10])
    parser.add_argument("--references-file", default="")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    prompt = args.prompt
    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as handle:
            prompt = handle.read()
    reference_specs = []
    if args.references_file:
        with open(args.references_file, "r", encoding="utf-8") as handle:
            reference_specs = json.load(handle)
    generate_video(
        prompt,
        args.out,
        args.meta,
        args.aspect,
        args.model,
        args.authuser,
        args.project_url,
        max(120, min(args.timeout, 1200)),
        args.video_type,
        args.resolution,
        args.duration,
        reference_specs,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FLOW_VIDEO_ERROR: {error}", file=sys.stderr)
        sys.exit(1)
