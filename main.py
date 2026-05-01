import asyncio
import os
import sys
import traceback
from pathlib import Path
from playwright.async_api import async_playwright


def load_env_file(env_path: Path):
    """Load simple KEY=VALUE pairs from a .env file without extra dependencies."""
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[len("export "):].strip()

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
            value = value.replace('\\"', '"').replace('\\\\', '\\')

        # Keep explicit process env vars as highest priority.
        os.environ.setdefault(key, value)


def get_env_value(names, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip() != "":
            return value
    return default


def get_env_int(names, default: int, minimum: int | None = None) -> int:
    raw = get_env_value(names, "")
    if raw == "":
        return default

    try:
        value = int(raw)
    except ValueError:
        first_name = names[0] if names else "ENV_INT"
        print(f"Invalid integer for {first_name}: {raw!r}; using default {default}.")
        return default

    if minimum is not None and value < minimum:
        first_name = names[0] if names else "ENV_INT"
        print(
            f"Value for {first_name} must be >= {minimum}; "
            f"got {value}. Using default {default}."
        )
        return default

    return value


def get_app_root() -> Path:
    """
    Return the directory that should contain runtime config and data.

    In PyInstaller frozen mode, __file__ points inside bundle internals
    (or a temp _MEI folder for onefile). Using sys.executable keeps .env,
    downloads and profile data next to the built executable.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_runtime_path(raw_path: str, base_dir: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def should_keep_console_open() -> bool:
    value = os.getenv("WA_KEEP_CONSOLE_OPEN", "")
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def maybe_pause_before_exit():
    if os.name != "nt" or not should_keep_console_open():
        return
    try:
        input("Press Enter to close this window...")
    except EOFError:
        pass


app_root = get_app_root()
env_file_path = app_root / ".env"
load_env_file(env_file_path)

chat_name = get_env_value(("WA_CHAT_NAME",), "61 9904-5559")
user_data_dir = resolve_runtime_path(
    get_env_value(("WA_USER_DATA_DIR",), "./wa_user_data"),
    app_root,
)
downloads_dir = resolve_runtime_path(
    get_env_value(("DOWNLOADS_DIR", "WA_DOWNLOADS_DIR"), "./downloads"),
    app_root,
)
max_downloads_per_execution = get_env_int(
    ("MAX_DOWNLOADS_PER_EXECUTION", "MAX_DOWNLOADS_PER_RUN"),
    100,
    minimum=1,
)
click_wait_ms = get_env_int(("CLICK_WAIT_MS", "WA_CLICK_WAIT_MS"), 1000, minimum=0)


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
    await page.keyboard.press("ControlOrMeta+A")
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


async def right_click_next_undownloaded_after_starred(
    page,
    save_dir: Path,
    starred_boundary_confirmed: bool = False,
):
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

    max_scan_rounds = 220
    max_no_progress_rounds = 14
    no_progress_rounds = 0
    passed_starred_boundary = starred_boundary_confirmed
    saw_any_candidates = False
    seen_candidate_ids = set()
    scan_round = 0

    for scan_round in range(1, max_scan_rounds + 1):
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

    const isOutgoing = (row) => {
        const rowClass = (row.getAttribute('class') || '').toLowerCase();
        if (rowClass.includes('message-out') || rowClass.includes('msg-out') || rowClass.includes('outgoing')) {
            return true;
        }

        const outgoingHint = row.querySelector([
            '.message-out',
            '[class*="message-out"]',
            '[class*="msg-out"]',
            '[data-testid*="msg-out" i]',
            '[data-testid*="outgoing" i]',
            '[aria-label*="you sent" i]',
            '[data-icon="msg-check"]',
            '[data-icon="msg-dblcheck"]',
            '[data-icon*="msg-check" i]',
            '[data-icon*="dblcheck" i]'
        ].join(','));

        return !!outgoingHint;
    };

    const starredIndex = rows.findIndex(isStarred);
    const idsAfterStarred = [];
    const allDownloadableIds = [];

    for (let i = 0; i < rows.length; i++) {
        if (!isDownloadable(rows[i])) continue;
        const id = rows[i].getAttribute('data-id');
        if (!id) continue;
        if (isOutgoing(rows[i])) continue;
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

        new_candidate_ids = [cid for cid in candidate_ids if cid not in seen_candidate_ids]
        if new_candidate_ids:
            seen_candidate_ids.update(new_candidate_ids)

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
            "el => el.scrollBy(0, Math.max(320, Math.floor(el.clientHeight * 0.45)))"
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

    if scan_round >= max_scan_rounds and no_progress_rounds < max_no_progress_rounds:
        return {
            "clicked": False,
            "reason": "scan_round_limit_reached",
            "round": scan_round,
        }

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
        print(f"Config file: {env_file_path}")
        print(f"User data dir: {user_data_dir}")
        print(f"Downloads dir: {downloads_dir}")

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
            scan_limit_retries = 0
            while downloaded_this_run < max_downloads_per_execution:
                click_result = await right_click_next_undownloaded_after_starred(
                    page,
                    downloads_dir,
                    starred_boundary_confirmed=True,
                )
                if not click_result.get("clicked"):
                    reason = click_result.get("reason")
                    if reason == "scan_round_limit_reached" and scan_limit_retries < 5:
                        scan_limit_retries += 1
                        print(
                            "Continuing scan after round limit "
                            f"(retry={scan_limit_retries}, round={click_result.get('round')}, "
                            f"downloaded={downloaded_this_run})."
                        )
                        await page.wait_for_timeout(250)
                        continue

                    print(
                        "Stopping download loop "
                        f"(reason={reason}, downloaded={downloaded_this_run})."
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
                    scan_limit_retries = 0
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


def main() -> int:
    try:
        asyncio.run(run())
        return 0
    except KeyboardInterrupt:
        print("Interrupted by user.")
        return 130
    except Exception:
        print("Fatal error while running downloader:")
        traceback.print_exc()
        return 1
    finally:
        maybe_pause_before_exit()


if __name__ == "__main__":
    raise SystemExit(main())