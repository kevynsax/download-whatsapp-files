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
post_stop_wait_ms = get_env_int(
    ("WA_POST_STOP_WAIT_MS", "POST_STOP_WAIT_MS"),
    10_000,
    minimum=0,
)
pre_start_wait_ms = get_env_int(
    ("WA_PRE_START_WAIT_MS", "PRE_START_WAIT_MS"),
    5_000,
    minimum=0,
)

review_starred_wait_ms = get_env_int(
    ("WA_REVIEW_STARRED_WAIT_MS", "REVIEW_STARRED_WAIT_MS"),
    15_000,
    minimum=0,
)

download_option_retry_attempts = get_env_int(
    ("WA_DOWNLOAD_OPTION_RETRY_ATTEMPTS", "DOWNLOAD_OPTION_RETRY_ATTEMPTS"),
    3,
    minimum=1,
)


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


def get_download_folder_stats(base_dir: Path) -> dict:
    if not base_dir.exists():
        return {
            "file_count": 0,
            "message_count": 0,
        }

    files = [entry for entry in base_dir.iterdir() if entry.is_file()]
    message_ids = set()
    for file_path in files:
        if "_" not in file_path.name:
            continue
        maybe_message_id, _ = file_path.name.split("_", 1)
        if maybe_message_id:
            message_ids.add(maybe_message_id)

    return {
        "file_count": len(files),
        "message_count": len(message_ids),
    }


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


async def find_visible_starred_messages(page):
    """Return info about all visible starred messages."""
    return await page.evaluate(
        """
() => {
  const rows = Array.from(document.querySelectorAll('[data-testid^="conv-msg-"]'));
  const starredMessages = [];
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

    if (starred) {
      const dataId = row.getAttribute('data-id') || '';
      const timeEl = row.querySelector('[data-testid="msg-meta"] span span');
      const textEl = row.querySelector('[data-testid="selectable-text"], [data-testid*="caption"], .copyable-text');
      starredMessages.push({
        dataId,
        time: (timeEl?.textContent || '').trim(),
        text: (textEl?.textContent || '').trim().slice(0, 120)
      });
    }
  }
  return starredMessages;
}
        """
    )


async def scroll_until_penultimate_starred(page, max_rounds=500, no_progress_limit=10):
    """
    Scroll message history from newest to oldest until penultimate starred.
    """
    panel = page.locator('[data-testid="conversation-panel-messages"]')
    await panel.wait_for(timeout=30_000)

    no_progress_rounds = 0
    # List to store starred messages in discovery order (Newest -> Oldest)
    seen_starred = []
    seen_ids = set()

    for i in range(1, max_rounds + 1):
        found = await find_visible_starred_messages(page)
        
        for msg in found:
            if msg["dataId"] not in seen_ids:
                seen_ids.add(msg["dataId"])
                seen_starred.append(msg)
        
        # Discovery order when scrolling UP is Newest -> Oldest.
        # seen_starred[0] = Last Starred (most recent)
        # seen_starred[1] = Penultimate Starred
        if len(seen_starred) >= 2:
            return {
                "reason": "penultimate_found",
                "round": i,
                "penultimate": seen_starred[1],
                "last_starred": seen_starred[0],
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
    start_after_id: str | None = None,
    stop_at_id: str | None = None,
    starred_boundary_confirmed: bool = False,
):
    """
    Right-click the next downloadable message after start_after_id until stop_at_id
    is reached (inclusive), that is not already downloaded.
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
    passed_start_boundary = starred_boundary_confirmed
    hit_stop_boundary = False
    saw_any_candidates = False
    seen_candidate_ids = set()
    observed_between_ids = set()
    observed_text_only_ids = set()
    observed_sent_by_me_ids = set()
    observed_downloadable_ids = set()
    observed_already_downloaded_ids = set()
    scan_round = 0

    def build_result(clicked: bool, reason: str, **extra):
        result = {
            "clicked": clicked,
            "reason": reason,
            "observed_between_ids": list(observed_between_ids),
            "observed_text_only_ids": list(observed_text_only_ids),
            "observed_sent_by_me_ids": list(observed_sent_by_me_ids),
            "observed_downloadable_ids": list(observed_downloadable_ids),
            "observed_already_downloaded_ids": list(observed_already_downloaded_ids),
            "passed_start_boundary": passed_start_boundary,
            "hit_stop_boundary": hit_stop_boundary,
        }
        result.update(extra)
        return result

    for scan_round in range(1, max_scan_rounds + 1):
        row_count = await page.locator('[data-testid^="conv-msg-"]').count()
        if row_count == 0:
            return build_result(False, "no_visible_messages")

        scan_result = await page.evaluate(
            """
(args) => {
    const { startAfterId, stopAtId, boundaryConfirmed } = args;
    const rows = Array.from(document.querySelectorAll('[data-testid^="conv-msg-"]'));
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

    // When boundaryConfirmed=true the start marker may have been virtualized out
    // of the DOM (WhatsApp Web evicts older nodes as you scroll down). Only skip
    // the marker lookup when it is genuinely absent from the DOM, so messages
    // before the boundary are never included when the marker is still visible.
    const startMarkerPresent = startAfterId
        ? !!document.querySelector(`[data-testid^="conv-msg-"][data-id="${startAfterId}"]`)
        : false;
    let passedStart = !startAfterId || (boundaryConfirmed && !startMarkerPresent);
    let reachedStop = false;
    const allDownloadableIds = [];
    const betweenMessageIds = [];
    const textOnlyIds = [];
    const sentByMeIds = [];

    for (let i = 0; i < rows.length; i++) {
        const id = rows[i].getAttribute('data-id');
        if (!id) continue;

        if (!passedStart && id === startAfterId) {
            passedStart = true;
        }

        if (passedStart && !reachedStop) {
            if (id === stopAtId) {
                reachedStop = true;
                break;
            }

            betweenMessageIds.push(id);

            const outgoing = isOutgoing(rows[i]);
            const downloadable = isDownloadable(rows[i]);

            if (outgoing) {
                sentByMeIds.push(id);
                continue;
            }

            if (downloadable) {
                allDownloadableIds.push(id);
                continue;
            }

            textOnlyIds.push(id);
        }
    }

    return {
        passedStart,
        reachedStop,
        allDownloadableIds,
        betweenMessageIds,
        textOnlyIds,
        sentByMeIds,
    };
}
            """,
            {"startAfterId": start_after_id, "stopAtId": stop_at_id, "boundaryConfirmed": passed_start_boundary}
        )

        if scan_result.get("passedStart"):
            passed_start_boundary = True
        
        if scan_result.get("reachedStop"):
            hit_stop_boundary = True

        observed_between_ids.update(scan_result.get("betweenMessageIds", []))
        observed_text_only_ids.update(scan_result.get("textOnlyIds", []))
        observed_sent_by_me_ids.update(scan_result.get("sentByMeIds", []))

        candidate_ids = scan_result.get("allDownloadableIds", [])
        observed_downloadable_ids.update(candidate_ids)

        if candidate_ids:
            saw_any_candidates = True

        new_candidate_ids = [cid for cid in candidate_ids if cid not in seen_candidate_ids]
        if new_candidate_ids:
            seen_candidate_ids.update(new_candidate_ids)

        for candidate_id in candidate_ids:
            if is_message_already_downloaded(save_dir, candidate_id):
                observed_already_downloaded_ids.add(candidate_id)
                continue

            row = page.locator(f'[data-testid^="conv-msg-"][data-id="{candidate_id}"]').first
            if await row.count() == 0:
                continue

            thumb = row.locator(thumb_selector).first
            if await thumb.count() == 0:
                continue

            await thumb.scroll_into_view_if_needed()
            await thumb.click(button="right")
            return build_result(
                True,
                "ok",
                message_id=candidate_id,
            )

        if hit_stop_boundary and not any(cid not in seen_candidate_ids for cid in candidate_ids):
             # We processed all candidates in the visible range up to stop boundary.
             break

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
        return build_result(
            False,
            "scan_round_limit_reached",
            round=scan_round,
        )

    if hit_stop_boundary:
        return build_result(False, "reached_stop_boundary")

    if saw_any_candidates:
        return build_result(False, "all_downloadables_already_downloaded")

    return build_result(False, "no_downloadable_after_starred")


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


async def right_click_message_thumb(page, message_id: str) -> bool:
    thumb_selector = (
        '[data-testid="document-thumb"], '
        '[data-testid="image-thumb"], '
        '[data-testid="video-thumb"], '
        '[title^="Download "], '
        '[aria-label*="download" i]'
    )

    row = page.locator(f'[data-testid^="conv-msg-"][data-id="{message_id}"]').first
    if await row.count() == 0:
        return False

    thumb = row.locator(thumb_selector).first
    if await thumb.count() == 0:
        return False

    await thumb.scroll_into_view_if_needed()
    await thumb.click(button="right")
    return True


async def run():
    async with async_playwright() as p:
        print(f"Config file: {env_file_path}")
        print(f"User data dir: {user_data_dir}")
        print(f"Downloads dir: {downloads_dir}")

        user_data_dir.mkdir(parents=True, exist_ok=True)
        launch_args = [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=DownloadBubble,DownloadBubbleV2",
        ]
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir.resolve()),
            channel="chrome",
            headless=False,
            accept_downloads=True,
            args=launch_args,
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
            if post_stop_wait_ms > 0:
                print(
                    "Waiting before close "
                    f"({post_stop_wait_ms} ms)."
                )
                await page.wait_for_timeout(post_stop_wait_ms)
            return

        await open_chat_from_search(page, search_box_selector, chat_name)

        print(f"Chat '{chat_name}' found and opened!")
        await page.wait_for_timeout(pre_start_wait_ms)

        result = await scroll_until_penultimate_starred(page)
        if result["reason"] == "penultimate_found":
            penultimate = result["penultimate"]
            last_starred = result["last_starred"]
            print(
                "Stopped scrolling: reached penultimate starred message.\n"
                f"  Penultimate: {penultimate.get('time', '')} - {penultimate.get('text', '')!r} (ID: {penultimate.get('dataId', '')})\n"
                f"  Last Starred: {last_starred.get('time', '')} - {last_starred.get('text', '')!r} (ID: {last_starred.get('dataId', '')})"
            )

            await page.wait_for_timeout(review_starred_wait_ms)

            folder_stats_before = get_download_folder_stats(downloads_dir)
            print(
                "Download folder status before loop "
                f"(files={folder_stats_before['file_count']}, "
                f"messages={folder_stats_before['message_count']})."
            )

            downloaded_this_run = 0
            scan_limit_retries = 0
            observed_between_ids = set()
            observed_text_only_ids = set()
            observed_sent_by_me_ids = set()
            observed_downloadable_ids = set()
            observed_already_downloaded_ids = set()
            while downloaded_this_run < max_downloads_per_execution:
                click_result = await right_click_next_undownloaded_after_starred(
                    page,
                    downloads_dir,
                    start_after_id=penultimate.get("dataId"),
                    stop_at_id=last_starred.get("dataId"),
                    starred_boundary_confirmed=True,
                )

                observed_between_ids.update(click_result.get("observed_between_ids", []))
                observed_text_only_ids.update(click_result.get("observed_text_only_ids", []))
                observed_sent_by_me_ids.update(click_result.get("observed_sent_by_me_ids", []))
                observed_downloadable_ids.update(click_result.get("observed_downloadable_ids", []))
                observed_already_downloaded_ids.update(
                    click_result.get("observed_already_downloaded_ids", [])
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

                message_id = click_result.get("message_id", "unknown")
                download_click = {"clicked": False, "reason": "download_option_not_found"}
                for attempt in range(1, download_option_retry_attempts + 1):
                    download_click = await click_download_in_context_menu(
                        page,
                        downloads_dir,
                        message_id,
                    )
                    if download_click.get("clicked"):
                        break

                    if download_click.get("reason") != "download_option_not_found":
                        break

                    if attempt < download_option_retry_attempts:
                        print(
                            "Download option not visible yet; retrying "
                            f"(attempt={attempt + 1}/{download_option_retry_attempts}, "
                            f"message_id={message_id})."
                        )
                        await page.wait_for_timeout(600)
                        reopened = await right_click_message_thumb(page, message_id)
                        if not reopened:
                            download_click = {
                                "clicked": False,
                                "reason": "message_not_visible_for_retry",
                            }
                            break

                if download_click.get("clicked"):
                    downloaded_this_run += 1
                    scan_limit_retries = 0
                    print(
                        "Downloaded file successfully "
                        f"(count={downloaded_this_run}, message_id={message_id}, "
                        f"file={download_click.get('saved_path')})."
                    )
                    await page.wait_for_timeout(click_wait_ms)
                else:
                    print(
                        "Could not click Download option "
                        f"(reason={download_click.get('reason')}, message_id={message_id}, "
                        f"attempts={download_option_retry_attempts})."
                    )
                    break

            if downloaded_this_run >= max_downloads_per_execution:
                print(f"Reached max downloads per execution ({max_downloads_per_execution}).")

            print(
                "Message range summary (between penultimate and last starred, "
                "including penultimate and excluding last starred): "
                f"total_messages={len(observed_between_ids)}, "
                f"skipped_text_only={len(observed_text_only_ids)}, "
                f"skipped_sent_by_me={len(observed_sent_by_me_ids)}, "
                f"downloadable_messages={len(observed_downloadable_ids)}, "
                f"already_downloaded_messages={len(observed_already_downloaded_ids)}, "
                f"downloaded_this_run={downloaded_this_run}."
            )

            folder_stats_after = get_download_folder_stats(downloads_dir)
            print(
                "Download folder status after loop "
                f"(files={folder_stats_after['file_count']}, "
                f"messages={folder_stats_after['message_count']}, "
                f"delta_files={folder_stats_after['file_count'] - folder_stats_before['file_count']}, "
                f"delta_messages={folder_stats_after['message_count'] - folder_stats_before['message_count']})."
            )
        else:
            print(f"Stopped: {result['reason']} (round={result['round']})")
            if result.get("older_messages_hint_visible"):
                print("WhatsApp is showing 'older messages from your phone' in this chat.")

        if post_stop_wait_ms > 0:
            print(
                "Waiting before close "
                f"({post_stop_wait_ms} ms)."
            )
            await page.wait_for_timeout(post_stop_wait_ms)
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