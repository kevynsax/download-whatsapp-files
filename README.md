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

Use environment variables (or a local `.env` file) to avoid editing code every run.

`main.py` loads `.env` automatically from the project root.

- `WA_CHAT_NAME` (default: `61 9904-5559`)
- `WA_USER_DATA_DIR` (default: `./wa_user_data`)
- `DOWNLOADS_DIR` (default: `./downloads`)
- `MAX_DOWNLOADS_PER_EXECUTION` (default: `100`)
- `CLICK_WAIT_MS` (default: `1000`)

Backward compatibility:

- `MAX_DOWNLOADS_PER_RUN` is also accepted as an alias for `MAX_DOWNLOADS_PER_EXECUTION`.

Example `.env` template is available in `.env.example`.

## Windows Config UI (User Friendly)

If you do not want to edit `.env` manually, use the built-in GUI:

1. On Windows, double-click `run_config_ui_windows.bat`.
2. Fill in the fields.
3. Click **Save .env** or **Run Downloader**.

The UI script is `config_ui.py`.

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

## Windows Deployment

You have two good options on Windows.

### Option A - Run with Python (easiest)

Use this if you are fine installing Python on the Windows computer.

1. Install Google Chrome.
2. Install Python 3.10+ from python.org (enable "Add Python to PATH").
3. Open the project folder.
4. Recommended: run `run_config_ui_windows.bat` and launch from the UI.
5. Or open PowerShell in the project folder and run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

Notes:

- This project launches the installed Chrome channel, so you usually do not need `playwright install chromium`.
- First run will require WhatsApp QR login.

### Option B - Executable (no Python on target machine)

If you want to run on Windows without installing Python there, build an EXE.

1. Build on a Windows machine (PyInstaller does not produce a Windows EXE from macOS).
2. In the project root, run `build_windows_exe.bat`.
3. After build, copy the whole folder `dist\whatsapp_downloader` to the target Windows computer.
4. Run `dist\whatsapp_downloader\whatsapp_downloader_config.exe` to configure values in a GUI.
5. Click **Run Downloader** in the GUI (or run `dist\whatsapp_downloader\whatsapp_downloader.exe` directly).

Target machine still needs:

- Google Chrome installed.
- Internet access to WhatsApp Web.
- Write permission in the app folder (for `downloads/` and `wa_user_data/`).

Summary:

- Python workflow: Python is required.
- EXE workflow: Python is not required on the target machine.
