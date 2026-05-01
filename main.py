import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

chat_name = "61 9904-5559"
user_data_dir = Path("./wa_user_data")
downloads_dir = Path("./downloads")
max_downloads_per_execution = 100
click_wait_ms = 1000


def is_message_already_downloaded(base_dir: Path, message_id: str) -> bool:
    if not message_id:
        return False
    if not base_dir.exists():
        return False
    return any(base_dir.glob(f"{message_id}_*"))


def normalize_for_match(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def build_download_save_path(base_dir: Path, message_id: str, suggested_name: str) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)

    clean_name = suggested_name.strip() or "download.bin"
    clean_name = clean_name.replace("/", "_").replace("\\", "_")
    target = base_dir / f"{message_id}_{clean_name}"

    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    counter = 2
    while True:
        candidate = base_dir / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


async def open_chat_from_search(page, search_box_selector: str, target_chat: str):
    search_box = page.locator(search_box_selector).first
    await search_box.click()
    await page.keyboard.press("Meta+A")
    await page.keyboard.press("Backspace")
    await search_box.fill(target_chat)
    await page.wait_for_timeout(500)

    # Strategy 1: Enter opens first filtered result in most WhatsApp Web builds.
    await page.keyboard.press("Enter")
    conversation_body = page.locator('[data-testid="conversation-panel-body"]')
    try:
        await conversation_body.wait_for(timeout=5000)
        return
    except Exception:
        pass

    # Strategy 2: Try to click an exact title match (if available).
    exact_title = page.locator(f'span[title="{target_chat}"]').first
    if await exact_title.count() > 0:
        await exact_title.click()
        await conversation_body.wait_for(timeout=7000)
        return

    # Strategy 3: Normalized title contains match (+55 61 9904-5559 vs 6199045559).
    normalized_target = normalize_for_match(target_chat)
    title_locator = page.locator("#pane-side span[title]")
    title_count = min(await title_locator.count(), 80)

    for i in range(title_count):
        candidate = title_locator.nth(i)
        title_text = (await candidate.get_attribute("title")) or ""
        if normalized_target and normalized_target in normalize_for_match(title_text):
            await candidate.click()
            await conversation_body.wait_for(timeout=7000)
            return

    # Strategy 4: Click first visible row in filtered chat list.
    fallback_selectors = [
        "#pane-side [role='listitem']",
        "[data-testid='cell-frame-container']",
        "#pane-side div[tabindex='-1'][role='row']",
    ]
    for selector in fallback_selectors:
        row = page.locator(selector).first
        if await row.count() > 0:
            await row.click()
            try:
                await conversation_body.wait_for(timeout=7000)
                return
            except Exception:
                pass

    raise RuntimeError(
        f"Could not open chat from search results for '{target_chat}'. "
        "Try using the exact chat title shown in WhatsApp sidebar."
    )


async def find_visible_starred_message(page):
    """Return info about the first visible starred message, or None."""
    return await page.evaluate(
        """
() => {
  const rows = Array.from(document.querySelectorAll('[data-testid^="conv-msg-"]'));
  for (const row of rows) {
    const starred = row.querySelector(
      [
        '[data-icon*="star"]',
        '[aria-label*="star" i]',
        '[aria-label*="starred" i]',
        '[title*="star" i]',
        '[data-testid*="star" i]'
      ].join(',')
    );

    if (!starred) {
      continue;
    }

    const dataId = row.getAttribute('data-id') || '';
    const timeEl = row.querySelector('[data-testid="msg-meta"] span span');
    const textEl = row.querySelector('[data-testid="selectable-text"], [data-testid*="caption"], .copyable-text');

    return {
      dataId,
      time: (timeEl?.textContent || '').trim(),
      text: (textEl?.textContent || '').trim().slice(0, 120)
    };
  }
  return null;
}
        """
    )


async def scroll_until_starred(page, max_rounds=500, no_progress_limit=10):
    """
    Scroll message history from newest to oldest until a starred message is visible.
    Returns a dict with stop reason and optional message info.
    """
    panel = page.locator('[data-testid="conversation-panel-messages"]')
    await panel.wait_for(timeout=30_000)

    no_progress_rounds = 0

    for i in range(1, max_rounds + 1):
        starred = await find_visible_starred_message(page)
        if starred:
            return {
                "reason": "starred_found",
                "round": i,
                "message": starred,
            }

        older_hint_visible = await page.locator(
            'button:has-text("older messages from your phone")'
        ).count()

        prev_top = await panel.evaluate("el => el.scrollTop")

        await panel.evaluate(
            "el => el.scrollBy(0, -Math.max(700, Math.floor(el.clientHeight * 0.9)))"
        )
        await page.wait_for_timeout(350)

        new_top = await panel.evaluate("el => el.scrollTop")

        if new_top == prev_top:
            await panel.hover()
            await page.mouse.wheel(0, -1800)
            await page.wait_for_timeout(350)
            newest_top = await panel.evaluate("el => el.scrollTop")
            if newest_top == new_top:
                no_progress_rounds += 1
            else:
                no_progress_rounds = 0
        else:
            no_progress_rounds = 0

        if no_progress_rounds >= no_progress_limit:
            return {
                "reason": "cannot_scroll_older",
                "round": i,
                "older_messages_hint_visible": bool(older_hint_visible),
            }

    return {
        "reason": "max_rounds_reached",
        "round": max_rounds,
    }


async def right_click_next_undownloaded_after_starred(page, save_dir: Path):
    """
    Find the first visible starred message, then right-click the next downloadable
    message after it in DOM order that is not already downloaded.
    """
    panel = page.locator('[data-testid="conversation-panel-messages"]')
    await panel.wait_for(timeout=10_000)

    thumb_selector = (
        '[data-testid="document-thumb"], '
        '[data-testid="image-thumb"], '
        '[data-testid="video-thumb"], '
        '[title^="Download "], '
        '[aria-label*="download" i]'
    )

    max_scan_rounds = 40
    max_no_progress_rounds = 8
    no_progress_rounds = 0
    passed_starred_boundary = False
    saw_any_candidates = False

    for _ in range(max_scan_rounds):
        row_count = await page.locator('[data-testid^="conv-msg-"]').count()
        if row_count == 0:
            return {"clicked": False, "reason": "no_visible_messages"}

        scan_result = await page.evaluate(
            """
() => {
    const rows = Array.from(document.querySelectorAll('[data-testid^="conv-msg-"]'));
    const isStarred = (row) => !!row.querySelector([
        '[data-icon*="star"]',
        '[aria-label*="star" i]',
        '[aria-label*="starred" i]',
        '[title*="star" i]',
        '[data-testid*="star" i]'
    ].join(','));
    const isDownloadable = (row) => !!row.querySelector([
        '[data-testid="document-thumb"]',
        '[data-testid="image-thumb"]',
        '[data-testid="video-thumb"]',
        '[title^="Download "]',
        '[aria-label*="download" i]'
    ].join(','));

    const starredIndex = rows.findIndex(isStarred);
    const idsAfterStarred = [];
    const allDownloadableIds = [];

    for (let i = 0; i < rows.length; i++) {
        if (!isDownloadable(rows[i])) continue;
        const id = rows[i].getAttribute('data-id');
        if (!id) continue;
        allDownloadableIds.push(id);
        if (starredIndex >= 0 && i > starredIndex) {
            idsAfterStarred.push(id);
        }
    }

    return {
        starredVisible: starredIndex >= 0,
        idsAfterStarred,
        allDownloadableIds,
    };
}
            """
        )

        if scan_result.get("starredVisible"):
            passed_starred_boundary = True

        candidate_ids = (
            scan_result.get("idsAfterStarred", [])
            if scan_result.get("starredVisible")
            else (scan_result.get("allDownloadableIds", []) if passed_starred_boundary else [])
        )

        if candidate_ids:
            saw_any_candidates = True

        for candidate_id in candidate_ids:
            if is_message_already_downloaded(save_dir, candidate_id):
                continue

            row = page.locator(f'[data-testid^="conv-msg-"][data-id="{candidate_id}"]').first
            if await row.count() == 0:
                continue

            thumb = row.locator(thumb_selector).first
            if await thumb.count() == 0:
                continue

            await thumb.scroll_into_view_if_needed()
            await thumb.click(button="right")
            return {
                "clicked": True,
                "reason": "ok",
                "message_id": candidate_id,
            }

        prev_top = await panel.evaluate("el => el.scrollTop")
        await panel.evaluate(
            "el => el.scrollBy(0, Math.max(700, Math.floor(el.clientHeight * 0.9)))"
        )
        await page.wait_for_timeout(300)
        new_top = await panel.evaluate("el => el.scrollTop")

        if new_top == prev_top:
            await panel.hover()
            await page.mouse.wheel(0, 1800)
            await page.wait_for_timeout(300)
            newest_top = await panel.evaluate("el => el.scrollTop")
            if newest_top == new_top:
                no_progress_rounds += 1
            else:
                no_progress_rounds = 0
        else:
            no_progress_rounds = 0

        if no_progress_rounds >= max_no_progress_rounds:
            break

    if saw_any_candidates:
        return {"clicked": False, "reason": "all_downloadables_already_downloaded"}

    return {"clicked": False, "reason": "no_downloadable_after_starred"}


async def click_download_in_context_menu(page, save_dir: Path, message_id: str):
    """Click Download/Baixar and force-save file into the configured folder."""
    selectors = [
        '[role="menuitem"]:has-text("Download")',
        '[role="menuitem"]:has-text("Baixar")',
        'div[role="button"]:has-text("Download")',
        'div[role="button"]:has-text("Baixar")',
        'button:has-text("Download")',
        'button:has-text("Baixar")',
    ]

    for selector in selectors:
        option = page.locator(selector).first
        try:
            await option.wait_for(timeout=1200)
            async with page.expect_download(timeout=20_000) as download_info:
                await option.click()

            download = await download_info.value
            suggested_name = download.suggested_filename or "download.bin"
            save_path = build_download_save_path(save_dir, message_id, suggested_name)
            await download.save_as(str(save_path.resolve()))
            return {
                "clicked": True,
                "selector": selector,
                "saved_path": str(save_path),
                "suggested_name": suggested_name,
            }
        except Exception:
            continue

    return {"clicked": False, "reason": "download_option_not_found"}


async def run():
    async with async_playwright() as p:
        user_data_dir.mkdir(parents=True, exist_ok=True)
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir.resolve()),
            channel="chrome",
            headless=False,
            accept_downloads=True,
            args=["--no-first-run", "--no-default-browser-check"]
        )
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto("https://web.whatsapp.com/")

        try:
            cdp = await context.new_cdp_session(page)
            await cdp.send(
                "Browser.grantPermissions",
                {
                    "origin": "https://web.whatsapp.com",
                    "permissions": ["durableStorage", "notifications"],
                },
            )
        except Exception:
            pass

        print("Waiting for Whatsapp Web to load...")

        try:
            search_box_selector = 'input[data-tab="3"]'
            await page.wait_for_selector(search_box_selector, timeout=120000)
            print("Whatsapp Web loaded successfully!")
        except Exception:
            print("Login timed out Did you scan the QR code?")
            return

        await open_chat_from_search(page, search_box_selector, chat_name)

        print(f"Chat '{chat_name}' found and opened!")

        result = await scroll_until_starred(page)
        if result["reason"] == "starred_found":
            msg = result["message"]
            print(
                "Stopped: starred message found "
                f"(round={result['round']}, data-id={msg.get('dataId', '')}, "
                f"time={msg.get('time', '')}, text={msg.get('text', '')!r})"
            )

            downloaded_this_run = 0
            while downloaded_this_run < max_downloads_per_execution:
                click_result = await right_click_next_undownloaded_after_starred(page, downloads_dir)
                if not click_result.get("clicked"):
                    print(
                        "Stopping download loop "
                        f"(reason={click_result.get('reason')}, downloaded={downloaded_this_run})."
                    )
                    break

                print(
                    "Right-clicked next undownloaded message "
                    f"(data-id={click_result.get('message_id', '')})."
                )

                await page.wait_for_timeout(click_wait_ms)

                download_click = await click_download_in_context_menu(
                    page,
                    downloads_dir,
                    click_result.get("message_id", "unknown"),
                )
                if download_click.get("clicked"):
                    downloaded_this_run += 1
                    print(
                        "Downloaded file successfully "
                        f"(count={downloaded_this_run}, message_id={click_result.get('message_id', '')}, "
                        f"file={download_click.get('saved_path')})."
                    )
                    await page.wait_for_timeout(click_wait_ms)
                else:
                    print(
                        "Could not click Download option "
                        f"(reason={download_click.get('reason')})."
                    )
                    break

            if downloaded_this_run >= max_downloads_per_execution:
                print(f"Reached max downloads per execution ({max_downloads_per_execution}).")
        else:
            print(f"Stopped: {result['reason']} (round={result['round']})")
            if result.get("older_messages_hint_visible"):
                print("WhatsApp is showing 'older messages from your phone' in this chat.")

        await page.wait_for_timeout(10_000)
        await context.close()
        
asyncio.run(run())