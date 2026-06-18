#!/usr/bin/env python3
"""
apthunt.py — Sync StreetEasy listings from iMessage + Gmail to Google Sheets.

Usage:
  .venv/bin/python apthunt.py              # normal sync
  .venv/bin/python apthunt.py --backfill   # re-fetch missing listing details
  .venv/bin/python apthunt.py --auth-gmail # one-time Gmail OAuth setup

Gmail setup (first time only):
  1. Go to console.cloud.google.com → APIs & Services → Credentials
  2. Create OAuth 2.0 Client ID → Desktop application → download JSON
  3. Save as gmail_oauth_creds.json in the same folder as this script
  4. Run: .venv/bin/python apthunt.py --auth-gmail
"""

import sqlite3, re, json, ssl, gzip, time, base64
from pathlib import Path
from datetime import datetime
import urllib.request

import gspread
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────

CHAT_DB    = Path.home() / "Library/Messages/chat.db"
CHAT_ID    = 0           # Find yours: sqlite3 ~/Library/Messages/chat.db "SELECT chat_id, display_name FROM chat_message_join JOIN chat USING(chat_id) LIMIT 50;"
SHEET_ID   = "YOUR_GOOGLE_SHEET_ID"
CREDS_FILE = Path(__file__).parent / "service_account.json"
STATE_FILE = Path(__file__).parent / "state.json"

GMAIL_TOKEN = Path(__file__).parent / "gmail_token.json"
GMAIL_CREDS = Path(__file__).parent / "gmail_oauth_creds.json"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
YOUR_EMAIL   = "you@gmail.com"  # Used to skip your own sent messages when scanning Gmail

ROOMMATE_1_HANDLE = "+1XXXXXXXXXX"  # E.164 phone number of roommate 1
ROOMMATE_2_HANDLE = "+1XXXXXXXXXX"  # E.164 phone number of roommate 2

SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "🏠 Listing", "Address", "Neighborhood", "Beds", "$/mo",
    "Date Shared", "Status", "Sam", "Molly", "Ellie", "ID", "Date Added",
]

TOURED_KW = [
    "touring this", "touring rn", "touring now", "toured this", "toured it",
    "saw this one", "saw it today", "checked it out", "went to see",
    "just toured", "we toured",
]
REQUESTED_KW = [
    "requested tour", "requested a tour", "open house", "going to see",
    "wanted to view", "scheduled", "asked if i wanted to view",
    "there's an open house", "can go to if",
]

TAPBACK    = {2000: "❤️", 2001: "👍", 2002: "👎", 2003: "😂", 2004: "‼️", 2005: "❓"}
SE_RE      = re.compile(r'https?://streeteasy\.com/(?:rental|building|for-sale)/(\d+)(?=[?/\s]|$)', re.I)
STATUS_RANK = {
    "Available": 0, "Tour Requested": 1, "Tour Scheduled": 2, "Toured": 3,
    "Off Market": 10,  # auto-detected from StreetEasy (in contract / rented / expired)
    "Rejected": 99, "Passed": 99,  # terminal — never overwrite automatically
}


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text())
        s.setdefault("formatted", False)
        return s
    return {"last_rowid": 0, "listings": {}, "formatted": False}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))


# ── iMessage ──────────────────────────────────────────────────────────────────

def scan_new_messages(last_rowid):
    try:
        conn = sqlite3.connect(str(CHAT_DB))
    except Exception as e:
        print(f"  ⚠  Cannot open chat.db ({e}) — skipping iMessage scan")
        return None  # None signals FDA failure, [] would mean "no new messages"
    cur = conn.cursor()
    try:
        cur.execute('''
            SELECT m.ROWID, m.guid, m.text, m.payload_data,
                   datetime(m.date/1000000000+978307200,"unixepoch","localtime")
            FROM message m
            JOIN chat_message_join cm ON m.ROWID = cm.message_id
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE cm.chat_id=? AND m.ROWID>?
              AND (m.is_from_me=1 OR h.id IN (?, ?))
              AND m.associated_message_type = 0
              AND (m.text LIKE "%streeteasy%"
                   OR (m.payload_data IS NOT NULL AND (m.text IS NULL OR m.text = "")))
            ORDER BY m.date
        ''', (CHAT_ID, last_rowid, ROOMMATE_1_HANDLE, ROOMMATE_2_HANDLE))
        rows = cur.fetchall()
    except Exception as e:
        print(f"  ⚠  chat.db query failed ({e}) — Full Disk Access may be needed")
        conn.close()
        return []
    conn.close()
    results = []
    seen_lids = set()
    for rowid, guid, text, payload_data, sent_at in rows:
        # Extract URLs from plain text and from payload_data blob (iOS rich-link shares)
        combined = (text or "")
        if payload_data:
            combined += bytes(payload_data).decode("utf-8", errors="replace")
        for lid in SE_RE.findall(combined):
            if (rowid, lid) not in seen_lids:
                seen_lids.add((rowid, lid))
                results.append((rowid, guid, lid, sent_at))
    return results


def get_thread_data(guid):
    try:
        conn = sqlite3.connect(str(CHAT_DB))
    except Exception:
        return ("Available", "", "", "")  # can't read chat.db, leave status unchanged
    cur = conn.cursor()
    cur.execute('''
        SELECT m.associated_message_type, m.is_from_me, h.id
        FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.associated_message_guid=?
          AND m.associated_message_type BETWEEN 2000 AND 2005
        ORDER BY m.date
    ''', (guid,))
    tapbacks = cur.fetchall()
    cur.execute('''
        SELECT m.text, m.is_from_me, h.id
        FROM message m
        JOIN chat_message_join cm ON m.ROWID = cm.message_id
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE cm.chat_id=? AND m.thread_originator_guid=?
          AND m.text IS NOT NULL AND m.text != ""
        ORDER BY m.date
    ''', (CHAT_ID, guid))
    replies = cur.fetchall()
    conn.close()

    sam_items, r1_items, r2_items = [], [], []
    for tap_type, is_me, handle in tapbacks:
        emoji = TAPBACK.get(tap_type, "")
        if is_me:            sam_items.append(emoji)
        elif handle == ROOMMATE_1_HANDLE: r1_items.append(emoji)
        elif handle == ROOMMATE_2_HANDLE: r2_items.append(emoji)

    all_text = []
    for text, is_me, handle in replies:
        t = text.strip()
        if not t: continue
        all_text.append(t.lower())
        if is_me:            sam_items.append(t)
        elif handle == ROOMMATE_1_HANDLE: r1_items.append(t)
        elif handle == ROOMMATE_2_HANDLE: r2_items.append(t)

    combined = " ".join(all_text)
    status = "Available"
    for kw in TOURED_KW:
        if kw in combined: status = "Toured"; break
    if status == "Available":
        for kw in REQUESTED_KW:
            if kw in combined: status = "Tour Requested"; break

    return (status, _best_quotes(sam_items), _best_quotes(r1_items), _best_quotes(r2_items))


def _best_quotes(items):
    if not items: return ""
    emojis = [i for i in items if len(i) <= 3]
    texts  = [i for i in items if len(i) > 3]
    expressive = [t for t in texts if "!" in t or "?" in t or any(ord(c) > 127 for c in t) or len(t) >= 8]
    chosen = expressive if expressive else texts
    seen, deduped = set(), []
    for t in chosen:
        key = t.lower().strip()
        if key not in seen:
            seen.add(key); deduped.append(t)
    parts = (emojis[:1] + deduped[:2]) if emojis else deduped[:2]
    return " · ".join(p[:80] for p in parts)[:160]


# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_gmail_service():
    """Return an authenticated Gmail API service, or None if not configured."""
    if not GMAIL_CREDS.exists():
        return None
    try:
        from google.oauth2.credentials import Credentials as UserCreds
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if GMAIL_TOKEN.exists():
            creds = UserCreds.from_authorized_user_file(str(GMAIL_TOKEN), GMAIL_SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(GMAIL_CREDS), GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
            GMAIL_TOKEN.write_text(creds.to_json())
        return build("gmail", "v1", credentials=creds)
    except Exception as e:
        print(f"  Gmail unavailable: {e}")
        return None


def _gmail_text(payload):
    """Recursively extract plain text from a Gmail API message payload."""
    if payload.get('mimeType', '').startswith('text/plain'):
        data = payload.get('body', {}).get('data', '')
        if data:
            return base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
    return ''.join(_gmail_text(p) for p in payload.get('parts', []))


def _norm_addr(s):
    s = s.lower()
    s = re.sub(r'\b(\d+)(st|nd|rd|th)\b', r'\1', s)
    s = re.sub(r'[#.,]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _addr_in(addr, text):
    """Check if the first 3 tokens of addr all appear in text (loose match)."""
    if not addr: return False
    tokens = _norm_addr(addr).split()[:3]
    nt = _norm_addr(text)
    return all(t in nt for t in tokens)


def scan_gmail_tours(gmail_svc, listing_addrs):
    """
    Scan Gmail for broker tour signals.

    listing_addrs: {listing_id: address_string}
    Returns: {listing_id: (status, broker_note)}
    """
    if gmail_svc is None:
        return {}

    updates = {}

    # Query 1: StreetEasy inquiry threads (Sam sent tour request; broker may have replied)
    queries = [
        'subject:"StreetEasy Inquiry" newer_than:90d',
        'subject:("limited tour windows" OR "schedule your tour") newer_than:90d',
    ]
    seen_thread_ids = set()

    for q in queries:
        try:
            res = gmail_svc.users().threads().list(userId='me', q=q, maxResults=50).execute()
        except Exception as e:
            print(f"  Gmail query failed: {e}")
            continue

        for t in res.get('threads', []):
            if t['id'] in seen_thread_ids:
                continue
            seen_thread_ids.add(t['id'])

            try:
                thread = gmail_svc.users().threads().get(
                    userId='me', id=t['id'], format='full'
                ).execute()
            except Exception:
                continue

            msgs = thread['messages']

            # Collect subject from any message
            subject = ''
            for m in msgs:
                for h in m['payload'].get('headers', []):
                    if h['name'] == 'Subject' and h['value']:
                        subject = h['value']

            # Find the most recent broker reply (skip Sam + StreetEasy noreply)
            broker_body = ''
            broker_sender = ''
            for m in reversed(msgs):
                hdrs = {h['name']: h['value'] for h in m['payload'].get('headers', [])}
                sender = hdrs.get('From', '')
                if YOUR_EMAIL in sender.lower() or 'noreply' in sender.lower():
                    continue
                broker_body = _gmail_text(m['payload'])
                broker_sender = re.sub(r'<.*?>', '', sender).strip().strip('"')
                break

            full_text = subject + ' ' + broker_body
            bl = broker_body.lower()

            # Classify status from broker message
            if broker_body:
                if any(kw in bl for kw in ['will be showing', 'showing this', 'confirmed', 'see you at']):
                    # Extract date/time if present
                    dm = re.search(r'((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d+|'
                                   r'(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))',
                                   bl, re.I)
                    tm = re.search(r'\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b', bl, re.I)
                    when = ' '.join(filter(None, [
                        dm.group(0).title() if dm else '',
                        tm.group(1).upper() if tm else '',
                    ]))
                    note = f"📧 {broker_sender}{' — showing ' + when if when else ' — showing confirmed'}"
                    status = 'Tour Scheduled'
                elif any(kw in bl for kw in ['can you view', 'available to view', 'when are you', 'move-in', 'book']):
                    first_line = next((l.strip() for l in broker_body.splitlines() if l.strip()), '')
                    note = f"📧 {broker_sender} — {first_line[:90]}"
                    status = 'Tour Requested'
                else:
                    note = f"📧 {broker_sender} replied"
                    status = 'Tour Requested'
            else:
                note = "📧 Tour request sent via StreetEasy"
                status = 'Tour Requested'

            # Match to a listing by address
            for lid, addr in listing_addrs.items():
                if _addr_in(addr, full_text):
                    existing = updates.get(lid)
                    if not existing or STATUS_RANK.get(status, 0) > STATUS_RANK.get(existing[0], 0):
                        updates[lid] = (status, note)

    return updates


def scan_gmail_off_market(gmail_svc, listing_addrs):
    """
    Scan StreetEasy notification emails for listings that went off market.

    StreetEasy sends emails from *@streeteasy.com when a saved/inquired listing
    is rented or taken off market. Match by listing ID embedded in the URL first
    (most reliable), then fall back to address matching.
    Returns a set of listing IDs confirmed off-market.
    """
    if gmail_svc is None:
        return set()

    off_market_ids = set()
    queries = [
        'from:(streeteasy.com) "no longer available" newer_than:90d',
        'from:(streeteasy.com) "no longer on the market" newer_than:90d',
        'from:(streeteasy.com) "has been rented" newer_than:90d',
    ]
    seen = set()

    for q in queries:
        try:
            res = gmail_svc.users().threads().list(userId='me', q=q, maxResults=50).execute()
        except Exception as e:
            print(f"  Gmail off-market query failed: {e}")
            continue

        for t in res.get('threads', []):
            if t['id'] in seen:
                continue
            seen.add(t['id'])
            try:
                thread = gmail_svc.users().threads().get(
                    userId='me', id=t['id'], format='full'
                ).execute()
            except Exception:
                continue

            for m in thread['messages']:
                subject = ''
                for h in m['payload'].get('headers', []):
                    if h['name'] == 'Subject':
                        subject = h['value']
                body = _gmail_text(m['payload'])
                full_text = subject + ' ' + body

                # Match by listing ID in StreetEasy URL (most reliable)
                matched_by_url = False
                for lid in SE_RE.findall(full_text):
                    if lid in listing_addrs:
                        off_market_ids.add(lid)
                        matched_by_url = True

                # Fall back to address matching
                if not matched_by_url:
                    for lid, addr in listing_addrs.items():
                        if _addr_in(addr, full_text):
                            off_market_ids.add(lid)

    return off_market_ids


def apply_gmail_updates(ws, gmail_updates, listing_addrs):
    """Write Gmail-derived status upgrades and broker notes into the sheet."""
    if not gmail_updates:
        return

    all_vals = ws.get_all_values()
    id_to_row = {row[HEADERS.index("ID")]: i + 1
                 for i, row in enumerate(all_vals)
                 if i > 0 and HEADERS.index("ID") < len(row) and row[HEADERS.index("ID")]}
    status_col = HEADERS.index("Status") + 1
    sam_col    = HEADERS.index("Sam") + 1

    for lid, (g_status, g_note) in gmail_updates.items():
        if lid not in id_to_row:
            continue
        row_num = id_to_row[lid]
        row     = all_vals[row_num - 1]

        current_status = row[HEADERS.index("Status")] if HEADERS.index("Status") < len(row) else ""
        if STATUS_RANK.get(g_status, 0) > STATUS_RANK.get(current_status, 0):
            ws.update_cell(row_num, status_col, g_status)
            print(f"  {lid} ({listing_addrs.get(lid, '?')}): {current_status!r} → {g_status!r} [Gmail]")

        current_sam = row[HEADERS.index("Sam")] if HEADERS.index("Sam") < len(row) else ""
        if g_note and g_note not in current_sam:
            new_sam = f"{current_sam} · {g_note}".lstrip(" · ") if current_sam else g_note
            ws.update_cell(row_num, sam_col, new_sam[:200])


# ── StreetEasy ────────────────────────────────────────────────────────────────

_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

def fetch_listing(listing_id):
    url = f"https://streeteasy.com/rental/{listing_id}"
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://streeteasy.com/",
    })
    ctx = ssl.create_default_context()
    html = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                data = r.read()
                if r.info().get("Content-Encoding") == "gzip": data = gzip.decompress(data)
                html = data.decode("utf-8", errors="ignore")
            break
        except Exception as e:
            wait = (attempt + 1) * 4
            print(f"    ⚠  attempt {attempt+1} failed ({e}) — retrying in {wait}s")
            if attempt < 2: time.sleep(wait)
    if not html: return {}

    address = neighborhood = borough = ""
    m = re.search(r'property="og:title" content="([^"]+)"', html)
    if m:
        loc = re.match(r'^(.+?)\s+in\s+(.+?),\s+(.+?)\s*\|', m.group(1))
        if loc: address, neighborhood, borough = loc.group(1), loc.group(2), loc.group(3)
        else: address = m.group(1).split("|")[0].strip()

    price = None
    pm = re.search(r'"price":\s*(\d+)', html)
    if pm: price = int(pm.group(1))

    beds = None
    bm = re.search(r'"bedrooms":\s*([1-9]\d?)\b', html)
    if not bm: bm = re.search(r'\b([1-9])\s*-\s*[Bb]edroom', html)
    if bm: beds = int(bm.group(1))

    # Availability: schema.org/OutOfStock covers in-contract, rented, expired
    off_market = bool(re.search(r'schema\.org/OutOfStock', html, re.I)
                      or re.search(r'data-testid="availableStatus"', html))

    return {"address": address, "neighborhood": neighborhood, "borough": borough,
            "price": price, "beds": beds, "off_market": off_market}


# ── Google Sheets ─────────────────────────────────────────────────────────────

def connect_sheet():
    creds = Credentials.from_service_account_file(str(CREDS_FILE), scopes=SHEETS_SCOPES)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SHEET_ID).sheet1
    existing = ws.get_all_values()
    if not existing or existing[0] != HEADERS:
        if existing: ws.clear()
        ws.append_row(HEADERS, value_input_option="RAW")
    return ws


def id_row_map(ws):
    id_col = HEADERS.index("ID") + 1
    vals = ws.col_values(id_col)
    return {v: i + 1 for i, v in enumerate(vals) if v and v != "ID"}


def append_listing_row(ws, listing_id, url, details, sent_at, status, sam, r1, r2):
    hood    = details.get("neighborhood", "")
    borough = details.get("borough", "")
    location = f"{hood}, {borough}" if hood and borough else hood or borough
    ws.append_row([
        f'=HYPERLINK("{url}","🏠 View")',
        details.get("address", ""), location,
        details.get("beds", ""), details.get("price", ""),
        sent_at, status, sam, r1, r2, listing_id,
        datetime.now().strftime("%Y-%m-%d %H:%M"),
    ], value_input_option="USER_ENTERED")


def update_row(ws, row_num, status, sam, r1, r2):
    status_col = HEADERS.index("Status") + 1
    sam_col    = HEADERS.index("Sam")    + 1
    r1_col     = HEADERS.index("Molly") + 1
    r2_col     = HEADERS.index("Ellie") + 1
    ws.update_cell(row_num, status_col, status)
    for col, val in [(sam_col, sam), (r1_col, r1), (r2_col, r2)]:
        if val: ws.update_cell(row_num, col, val)


# ── Formatting ────────────────────────────────────────────────────────────────

def apply_formatting(ws):
    sh  = ws.spreadsheet
    sid = ws._properties["sheetId"]
    n   = len(HEADERS)

    col_widths  = [130, 240, 165, 50, 75, 105, 125, 200, 200, 200, 70, 100]
    status_col  = HEADERS.index("Status")
    status_vals = ["Available", "Tour Requested", "Tour Scheduled", "Toured", "Off Market", "Rejected", "Passed"]
    # Pastel colour palette: #e1e6fc #f8edc5 #ece1ef #f2e1d6 #e4eece #d9f3f2
    status_colors = {
        "Available":      {"red": 0.851, "green": 0.953, "blue": 0.949},  # #d9f3f2 mint
        "Tour Requested": {"red": 0.973, "green": 0.929, "blue": 0.773},  # #f8edc5 yellow
        "Tour Scheduled": {"red": 0.949, "green": 0.882, "blue": 0.839},  # #f2e1d6 peach
        "Toured":         {"red": 0.894, "green": 0.933, "blue": 0.808},  # #e4eece sage
        "Off Market":     {"red": 0.925, "green": 0.882, "blue": 0.937},  # #ece1ef lavender
        "Rejected":       {"red": 0.925, "green": 0.882, "blue": 0.937},  # #ece1ef lavender
        "Passed":         {"red": 0.925, "green": 0.882, "blue": 0.937},  # #ece1ef lavender
    }

    def dim(col):
        return {"sheetId": sid, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1}

    requests = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 44}, "fields": "pixelSize"}},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": n},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.882, "green": 0.902, "blue": 0.988},  # #e1e6fc periwinkle
                "textFormat": {"bold": True,
                               "foregroundColor": {"red": 0.239, "green": 0.239, "blue": 0.361},  # dark navy
                               "fontSize": 10},
                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"}},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": n},
            "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat(verticalAlignment,wrapStrategy)"}},
        *[{"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1,
                      "startColumnIndex": ci, "endColumnIndex": ci + 1},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat.horizontalAlignment",
        }} for ci in [HEADERS.index("Beds"), HEADERS.index("$/mo"), HEADERS.index("ID")]],
        *[{"updateDimensionProperties": {
            "range": dim(i), "properties": {"pixelSize": w}, "fields": "pixelSize",
        }} for i, w in enumerate(col_widths)],
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1,
                      "startColumnIndex": HEADERS.index("ID"),
                      "endColumnIndex": HEADERS.index("ID") + 1},
            "cell": {"userEnteredFormat": {
                "textFormat": {"foregroundColor": {"red": 0.7, "green": 0.7, "blue": 0.7}, "fontSize": 9},
                "horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat(textFormat,horizontalAlignment)"}},
        {"addBanding": {
            "bandedRange": {
                "range": {"sheetId": sid, "startRowIndex": 1},
                "rowProperties": {
                    "firstBandColor":  {"red": 1.0,   "green": 1.0,   "blue": 1.0},       # white
                    "secondBandColor": {"red": 0.965, "green": 0.969, "blue": 0.996},      # #f6f7fd faint periwinkle
                }}}},
        {"setDataValidation": {
            "range": {"sheetId": sid, "startRowIndex": 1,
                      "startColumnIndex": status_col, "endColumnIndex": status_col + 1},
            "rule": {
                "condition": {"type": "ONE_OF_LIST",
                              "values": [{"userEnteredValue": v} for v in status_vals]},
                "showCustomUi": True, "strict": False}}},
        *[{"addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": sid, "startRowIndex": 1,
                            "startColumnIndex": status_col, "endColumnIndex": status_col + 1}],
                "booleanRule": {
                    "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": label}]},
                    "format": {
                        "backgroundColor": color,
                        "textFormat": {"bold": label in ("Toured", "Tour Requested", "Tour Scheduled")},
                    }}},
            "index": 0,
        }} for label, color in status_colors.items()],
    ]

    sh.batch_update({"requests": requests})
    print("  Sheet formatting applied.")


# ── Run Log ───────────────────────────────────────────────────────────────────

LOG_HEADERS = ["Timestamp", "New Listings", "Status Updates", "Off Market", "Gmail Updates", "iMessage"]
LOG_TAB     = "Run Log"

def get_log_sheet(spreadsheet):
    """Return the Run Log worksheet, creating it if needed."""
    try:
        ws = spreadsheet.worksheet(LOG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=LOG_TAB, rows=500, cols=len(LOG_HEADERS))
        ws.append_row(LOG_HEADERS, value_input_option="RAW")
        # Light formatting: freeze header, periwinkle header row
        sid = ws._properties["sheetId"]
        spreadsheet.batch_update({"requests": [
            {"updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": len(LOG_HEADERS)},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.882, "green": 0.902, "blue": 0.988},
                    "textFormat": {"bold": True,
                                   "foregroundColor": {"red": 0.239, "green": 0.239, "blue": 0.361}},
                    "horizontalAlignment": "CENTER"}},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"}},
            *[{"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": w}, "fields": "pixelSize",
            }} for i, w in enumerate([180, 100, 110, 90, 110, 90])],
        ]})
    return ws

def log_run(spreadsheet, added, status_updated, off_market, gmail_updated, imessage_ok):
    ws = get_log_sheet(spreadsheet)
    ws.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        added,
        status_updated,
        off_market,
        gmail_updated,
        "✓" if imessage_ok else "✗ no FDA",
    ], value_input_option="RAW")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    state = load_state()
    print(f"Last seen ROWID: {state['last_rowid']}  |  Tracked: {len(state['listings'])} listings")

    # counters for run log
    added = updated = off_market_count = gmail_count = 0
    imessage_ok = True

    # 1. iMessage: find new listing URLs
    raw = scan_new_messages(state["last_rowid"])
    if raw is None:
        raw = []
        imessage_ok = False
    print(f"\nNew StreetEasy messages from group: {len(raw)}")

    new_items = []
    for rowid, guid, lid, sent_at in raw:
        if lid not in state["listings"]:
            url = f"https://streeteasy.com/rental/{lid}"
            state["listings"][lid] = {"guid": guid, "url": url}
            new_items.append({"lid": lid, "guid": guid, "url": url, "sent_at": sent_at})
        if rowid > state["last_rowid"]:
            state["last_rowid"] = rowid

    print(f"New unique listings to add: {len(new_items)}")

    # 2. Connect sheet
    print("\nConnecting to Google Sheets...")
    ws = connect_sheet()

    if not state["formatted"]:
        print("  Applying formatting...")
        apply_formatting(ws)
        state["formatted"] = True
        save_state(state)

    id_to_row = id_row_map(ws)
    print(f"Rows in sheet: {len(id_to_row)}")

    # 3. Add new listings
    added = 0
    for item in new_items:
        lid = item["lid"]
        if lid in id_to_row:
            print(f"  Already in sheet: listing {lid}")
            continue
        print(f"  Fetching listing {lid}...")
        details = fetch_listing(lid)
        status, sam, r1, r2 = get_thread_data(item["guid"])
        print(f"    {details.get('address') or '—'} | ${details.get('price') or '?'}/mo | {details.get('beds') or '?'}br | {status}")
        append_listing_row(ws, lid, item["url"], details, item["sent_at"],
                           status, "", "", "")
        added += 1
        time.sleep(3)

    # 4. Refresh iMessage status + opinions on tracked listings
    print("\nChecking iMessage threads for updates...")
    id_to_row   = id_row_map(ws)
    status_vals = ws.col_values(HEADERS.index("Status") + 1)

    for lid, info in state["listings"].items():
        if lid not in id_to_row: continue
        row_num = id_to_row[lid]
        current = status_vals[row_num - 1] if row_num - 1 < len(status_vals) else ""
        if STATUS_RANK.get(current, 0) >= 3: continue  # skip Toured/Rejected/Passed
        new_status, _, _, _ = get_thread_data(info["guid"])
        if STATUS_RANK.get(new_status, 0) > STATUS_RANK.get(current, 0):
            print(f"  {current!r} → {new_status!r}: listing {lid}")
            update_row(ws, row_num, new_status, "", "", "")
            updated += 1

    # 5. Gmail: off-market notifications + broker email signals
    print("\nChecking Gmail...")
    gmail_svc = get_gmail_service()
    if gmail_svc is None:
        if not GMAIL_CREDS.exists():
            print("  (Gmail not configured — run with --auth-gmail to set up)")
    else:
        # Build {listing_id: address} map from current sheet state
        all_vals = ws.get_all_values()
        listing_addrs = {}
        for i, row in enumerate(all_vals):
            if i == 0: continue
            lid  = row[HEADERS.index("ID")]      if HEADERS.index("ID")      < len(row) else ""
            addr = row[HEADERS.index("Address")] if HEADERS.index("Address") < len(row) else ""
            if lid and addr:
                listing_addrs[lid] = addr

        # Off-market detection via StreetEasy notification emails
        skip_statuses = {"Toured", "Off Market", "Rejected", "Passed"}
        id_to_row  = id_row_map(ws)
        status_col = HEADERS.index("Status") + 1
        status_vals_now = ws.col_values(status_col)
        off_market_ids = scan_gmail_off_market(gmail_svc, listing_addrs)
        for lid in off_market_ids:
            if lid not in id_to_row: continue
            row_num = id_to_row[lid]
            current = status_vals_now[row_num - 1] if row_num - 1 < len(status_vals_now) else ""
            if current in skip_statuses: continue
            ws.update_cell(row_num, status_col, "Off Market")
            print(f"  {lid}: Off Market [StreetEasy email]")
            off_market_count += 1
        if not off_market_ids:
            print("  No off-market notifications found.")

        # Broker tour signals
        gmail_updates = scan_gmail_tours(gmail_svc, listing_addrs)
        gmail_count = len(gmail_updates)
        if gmail_updates:
            print(f"  Found broker signals for {gmail_count} listing(s)")
            apply_gmail_updates(ws, gmail_updates, listing_addrs)
        else:
            print("  No new broker signals found.")

    # 6. Append run summary to Run Log tab
    log_run(ws.spreadsheet, added, updated, off_market_count, gmail_count, imessage_ok)

    save_state(state)
    print(f"\nDone. Added {added} new listings, updated {updated} iMessage statuses.")


def backfill():
    print("Backfill mode: looking for rows with missing details...")
    ws = connect_sheet()
    all_vals = ws.get_all_values()
    addr_idx = HEADERS.index("Address")
    id_idx   = HEADERS.index("ID")
    patched  = 0
    for i, row in enumerate(all_vals):
        if i == 0 or not row[id_idx]: continue
        if row[addr_idx]: continue
        lid = row[id_idx]
        print(f"  Fetching listing {lid} (row {i+1})...")
        details = fetch_listing(lid)
        if not details.get("address"):
            print("    Still unavailable.")
            continue
        hood    = details.get("neighborhood", "")
        borough = details.get("borough", "")
        location = f"{hood}, {borough}" if hood and borough else hood or borough
        print(f"    → {details['address']} | {location} | {details.get('beds')}br | ${details.get('price')}/mo")
        ws.update(values=[[details.get("address",""), location,
                           details.get("beds",""), details.get("price","")]],
                  range_name=f"B{i+1}:E{i+1}")
        patched += 1
        time.sleep(4)
    print(f"Done. Patched {patched} rows.")


if __name__ == "__main__":
    import sys
    if "--backfill" in sys.argv:
        backfill()
    elif "--auth-gmail" in sys.argv:
        print("Opening browser for Gmail authorization...")
        svc = get_gmail_service()
        if svc:
            print("Gmail authorized successfully! Token saved to gmail_token.json")
        else:
            print("Authorization failed. Check that gmail_oauth_creds.json exists.")
    else:
        main()
