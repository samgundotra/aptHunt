# aptHunt

A Python script that automatically tracks NYC apartment listings from an iMessage group chat and syncs them to a color-coded Google Sheet — updated every 2 hours via macOS launchd.

## What it does

- **Reads iMessage** — scans a group chat's SQLite database for StreetEasy links you've shared
- **Scrapes StreetEasy** — fetches address, neighborhood, price, and bed count for each listing
- **Syncs to Google Sheets** — writes each unique listing as a row, never duplicating
- **Detects tour activity** — parses reply threads and tapback reactions to auto-update status (`Available → Tour Requested → Tour Scheduled → Toured`)
- **Scans Gmail** — finds broker reply emails and surfaces tour scheduling info as notes
- **Marks off-market listings** — re-checks StreetEasy periodically and flags anything in-contract or rented
- **Logs every run** — a "Run Log" tab tracks timestamps, new listings added, and iMessage/Gmail status
- **Runs automatically** — a launchd agent wakes it every 2 hours in the background

## Sheet layout

| 🏠 Listing | Address | Neighborhood | Beds | $/mo | Date Shared | Status | Sam | Molly | Ellie | ID | Date Added |
|---|---|---|---|---|---|---|---|---|---|---|---|

Status column uses a pastel color palette with dropdown validation:
- `Available` — mint
- `Tour Requested` — yellow
- `Tour Scheduled` — peach
- `Toured` — sage
- `Off Market` / `Rejected` / `Passed` — lavender

## Tech stack

| Layer | Tool |
|---|---|
| Listing source | iMessage (`~/Library/Messages/chat.db` via SQLite) |
| Listing data | StreetEasy HTML scraping (mobile User-Agent) |
| Spreadsheet | Google Sheets API v4 via `gspread` + service account |
| Email | Gmail API via OAuth2 |
| Scheduling | macOS `launchd` (plist in `~/Library/LaunchAgents/`) |
| Runtime | Python 3.12, stdlib-only HTTP (`urllib`) |

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/samgundotra/aptHunt.git
cd aptHunt
python3 -m venv .venv
.venv/bin/pip install gspread google-auth google-auth-oauthlib google-api-python-client
```

### 2. Google Sheets service account

1. Go to [Google Cloud Console](https://console.cloud.google.com) → Create a project
2. Enable the **Google Sheets API** and **Google Drive API**
3. Create a **Service Account** → download the JSON key → save as `service_account.json` in the project folder
4. Create a new Google Sheet and share it with the service account email (Editor)
5. Copy the sheet ID from the URL: `https://docs.google.com/spreadsheets/d/**SHEET_ID**/edit`

### 3. Configure `apthunt.py`

Edit the config section at the top of the script:

```python
CHAT_ID    = 0           # Find your group chat ID in chat.db (see below)
SHEET_ID   = "YOUR_GOOGLE_SHEET_ID"
CREDS_FILE = Path(__file__).parent / "service_account.json"
YOUR_EMAIL = "you@gmail.com"

ROOMMATE_1_HANDLE = "+1XXXXXXXXXX"  # Phone number in E.164 format
ROOMMATE_2_HANDLE = "+1XXXXXXXXXX"
```

**Finding your chat ID:** Open Terminal and run:
```bash
sqlite3 ~/Library/Messages/chat.db "SELECT chat_id, display_name FROM chat_message_join JOIN chat USING(chat_id) LIMIT 50;"
```

### 4. Grant Full Disk Access to Python

macOS blocks access to `chat.db` by default. Grant FDA to the Python binary:

1. System Settings → Privacy & Security → Full Disk Access
2. Click `+` → press `Cmd+Shift+G` → paste the path to your venv Python:
   ```
   /path/to/aptHunt/.venv/bin/python
   ```

### 5. Gmail (optional)

1. In Google Cloud Console, create an **OAuth 2.0 Client ID** (Desktop application)
2. Download the JSON → save as `gmail_oauth_creds.json` in the project folder
3. Run once to authorize:
   ```bash
   .venv/bin/python apthunt.py --auth-gmail
   ```

### 6. Run manually

```bash
.venv/bin/python apthunt.py              # normal sync
.venv/bin/python apthunt.py --backfill   # re-fetch missing listing details
```

### 7. Schedule with launchd (runs every 2 hours)

Create `~/Library/LaunchAgents/com.yourname.apthunt.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.yourname.apthunt</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/aptHunt/.venv/bin/python</string>
        <string>/path/to/aptHunt/apthunt.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/aptHunt</string>
    <key>StartInterval</key>
    <integer>7200</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/path/to/aptHunt/apthunt.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/aptHunt/apthunt.log</string>
</dict>
</plist>
```

Load it:
```bash
launchctl load ~/Library/LaunchAgents/com.yourname.apthunt.plist
```

> **Note:** The agent runs while your Mac is awake. It survives sleep — macOS fires it on wake if a scheduled run was missed.

## How status detection works

**iMessage threads** — when a listing URL is replied to in the group chat, the script reads the `thread_originator_guid` field in `chat.db` to find all replies. It keyword-matches reply text (e.g. "toured this", "requested a tour") and upgrades the status. Status only ever goes *up* — `Toured` and `Passed` are never auto-overwritten.

**Gmail** — searches for StreetEasy inquiry threads and broker reply emails. Extracts tour confirmation language and maps emails back to listings by address matching.

**StreetEasy availability** — checks `schema.org/OutOfStock` in the HTML to detect in-contract or rented listings. Rate-limited to 3 listings per run with a 24-hour cooldown per listing.

## Files

```
aptHunt/
├── apthunt.py               # Main script
├── service_account.json     # Google service account (gitignored)
├── gmail_oauth_creds.json   # Gmail OAuth credentials (gitignored)
├── gmail_token.json         # Gmail token after auth (gitignored)
├── state.json               # Tracks last seen iMessage ROWID + listing cache (gitignored)
└── apthunt.log              # Background run output
```
