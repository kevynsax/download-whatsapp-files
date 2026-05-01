# Download WhatsApp Files (CrossManual 🤖)

This project automates downloading files from a WhatsApp Web chat using Playwright + Python.

It is designed to:

- Keep a persistent browser profile so you do not log in every run.
- Open the chat `CrossManual 🤖`.
- Scan messages from newest to oldest.
- Download files using WhatsApp's native **Baixar** button.
- Avoid duplicate downloads with a hash marker.
- Stop safely based on clear exit conditions.

## Goal

Download all file attachments from a target chat while preserving metadata and skipping already processed messages.

## Strategy (High Level)

1. Open Chrome/Chromium with persistent profile.
2. Go to `https://web.whatsapp.com`.
3. Wait for user to be logged in.
4. Open/select chat `CrossManual 🤖`.
5. Scan visible messages from newest to oldest.
6. For each file message:
   - Read sender name, phone, caption/text, hour.
   - Compute hash:
     - `sha256(name + phone + text + hour)`
   - If hash already stored: skip.
   - If message is starred: stop.
   - Open file/media preview.
   - Click WhatsApp's real **Baixar** button.
   - Wait for Playwright `download` event.
   - Save file to `downloads/`.
   - Store `hash = 1` in local marker store.
7. Scroll older.
8. Stop when one of these conditions is reached:
   - Downloaded enough files for this run.
   - Reached a starred message.
   - Scanned 200 files with no existing marker.
   - Cannot scroll older.

## Project Structure

- `main.py`: entry point with Playwright setup.
- `wa_user_data/`: persistent browser profile used by Playwright.
- `downloads/`: downloaded files (to be created by the script).
- `state/markers.json`: dedupe store (`hash -> 1`) (recommended).

## Prerequisites

- macOS/Linux/Windows
- Python 3.10+
- Google Chrome installed (or Chromium)

## Setup

1. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install Playwright:

```bash
pip install playwright
python3 -m playwright install chromium
```

3. Run once to authenticate WhatsApp Web and persist session:

```bash
python3 main.py
```

4. Scan QR code when prompted (first run only).

## Recommended Runtime Config

Use environment variables to avoid editing code every run:

- `WA_CHAT_NAME` (default: `CrossManual 🤖`)
- `MAX_DOWNLOADS_PER_RUN` (example: `50`)
- `MAX_NO_MARKER_SCAN` (default: `200`)
- `DOWNLOADS_DIR` (default: `./downloads`)
- `MARKERS_FILE` (default: `./state/markers.json`)

## Core Algorithm (Pseudo-code)

```text
load markers map from MARKERS_FILE (or empty map)
open persistent context (wa_user_data)
open whatsapp web
wait until logged in
open chat by WA_CHAT_NAME

downloaded = 0
no_marker_scanned = 0
reached_starred = false

loop:
  messages = visible messages ordered newest -> oldest

  for msg in messages:
    if msg is not file/media with downloadable content:
      continue

    metadata = {sender_name, phone, text_or_caption, hour}
    h = sha256(sender_name + phone + text_or_caption + hour)

    if markers[h] exists:
      continue

    no_marker_scanned += 1

    if msg is starred:
      reached_starred = true
      break

    open preview
    click real "Baixar" button
    wait for playwright download event
    save file to DOWNLOADS_DIR

    markers[h] = 1
    persist markers map
    downloaded += 1

    if downloaded >= MAX_DOWNLOADS_PER_RUN:
      break

  if reached_starred:
    break

  if downloaded >= MAX_DOWNLOADS_PER_RUN:
    break

  if no_marker_scanned >= MAX_NO_MARKER_SCAN:
    break

  if cannot scroll older:
    break

save markers map
close browser context
```

## Data Model

### Marker key

```text
sha256(name + phone + text + hour)
```

### Marker storage example (`state/markers.json`)

```json
{
  "e3b0c44298fc1c149afbf4c8996fb924...": 1,
  "43b2f6f9a1f4be28e17d34fdd8cf902a...": 1
}
```

## Selector Guidance (WhatsApp Web is dynamic)

WhatsApp selectors change over time. Prefer robust selectors:

- Chat search/input by accessible role/name or stable attributes.
- Message container by semantic grouping + fallback selectors.
- Download button by text/aria label (`Baixar` / `Download`).
- Starred indicator by icon label/tooltip.

Use small helper functions with multiple selector fallbacks.

## Reliability Tips

- Keep `headless=False` for debugging while developing.
- Add retries for click and preview open actions.
- Debounce scrolling and waits to avoid stale elements.
- Persist markers immediately after each successful download.
- Log each decision (`skip`, `download`, `stop reason`).

## Logging Recommendation

Log one line per processed candidate:

```text
[SKIP] hash_exists sender=... hour=...
[STOP] starred_message sender=... hour=...
[OK] downloaded file=... sender=... hour=...
```

## Known Limitations

- WhatsApp Web UI changes can break selectors.
- Some media may not expose immediate download buttons.
- Storage persistence warnings can appear; often non-fatal.

## Next Implementation Steps

1. Parameterize `main.py` with env vars listed above.
2. Implement message parser + metadata extraction.
3. Implement marker store load/save (`state/markers.json`).
4. Implement download handler with `page.expect_download()`.
5. Implement scrolling loop and stop conditions.
6. Add structured logs.

## Security & Privacy

- This automation interacts with personal chat data.
- Keep `wa_user_data/`, `downloads/`, and `state/` private.
- Do not commit chat content, cookies, or profile folders to git.
