"""
CSM Diploma Lookup — Monday.com Poller
========================================
Board : Board 3 (id 5937371669)
Trigger: Button sets "CSM Lookup" status to "Opzoeken"
         → this server polls every 60 seconds and processes those items

Flow:
  1. User clicks "Update Attesten" button → CSM Lookup status becomes "Opzoeken"
  2. Server polls Monday every 60 s for items where CSM Lookup = "Opzoeken"
  3. For each item: reads Name + Geboortedatum, searches csm-examen.be/cdr
  4. Routes results to the matching column set:
       VCA   → VCA-Nummer  (text0)   + VCA einddatum  (date)   + VCA Diploma  (files7)
       AV-011→ AV-011-nummer(text9)  + AV-011 einddatum(date2) + AV-011-Attest(files0)
       IS-007→ IS-007-nummer(text7)  + IS-007 einddatum(date9) + IS-007-Attest(files6)
       IS-081→ IS-081-nummer(text3)  + IS-081 einddatum(date41)+ IS-081-attest(files4)
       IS-013→ IS-013-nummer(tekst)  + IS-013-datum   (datum7) + IS-013-attest(bestanden3)
  5. Screenshots each diploma card and uploads to the matching file column
  6. Sets CSM Lookup to "Gedaan" (found) or "Niet gevonden" (not found)

No webhooks or Monday automations required — the button action is the only
Monday-side setup needed.

INSTALL
-------
pip install fastapi uvicorn requests beautifulsoup4 python-dotenv playwright
playwright install chromium

CONFIGURE
---------
Create a .env file next to this script:
  MONDAY_API_KEY=your_monday_personal_api_token

RUN
---
python csm_monday_webhook.py
"""

import os
import io
import re
import logging
import asyncio
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

MONDAY_API_KEY  = os.getenv("MONDAY_API_KEY")
BOARD_ID        = "5937371669"        # Board 3
BUTTON_COLUMN   = "button_mm5264yz"   # "Update Attesten" button
STATUS_COLUMN   = "color_mm52mjjp"    # "CSM Lookup" status column
STATUS_TRIGGER  = "Opzoeken"          # label that fires the lookup
STATUS_DONE     = "Gedaan"            # label set after successful lookup
STATUS_ERROR    = "Niet gevonden"     # label set when no diploma found — add this label to the CSM Lookup column in Monday
CSM_URL         = "https://csm-examen.be/cdr"

MONDAY_HEADERS  = {
    "Authorization": MONDAY_API_KEY,
    "Content-Type":  "application/json",
    "API-Version":   "2024-01",
}

# ── Column IDs (from board inspection) ────────────────────────────────────────
# Input
COL_NAME       = "name"                # "Familienaam, Voornaam"
COL_BIRTHDATE  = "dup__of_verjaardag5" # Geboortedatum (Date column → YYYY-MM-DD)

# Per certificate type: (number_col, enddate_col, file_col)
CERT_COLUMNS = {
    "VCA":    ("text0",  "date",  "files7"),
    "AV-011": ("text9",  "date2", "files0"),
    "IS-007": ("text7",  "date9", "files6"),
    "IS-081": ("text3",  "date41","files4"),
    "IS-013": ("tekst",  "datum7","bestanden3"),
}

# Keywords to detect certificate type from scraped diploma card text
CERT_KEYWORDS = {
    "VCA":    ["vca"],
    "AV-011": ["av-011", "av011", "av 011"],
    "IS-007": ["is-007", "is007", "is 007"],
    "IS-081": ["is-081", "is081", "is 081"],
    "IS-013": ["is-013", "is013", "is 013"],
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI()


# ─── Monday API helpers ───────────────────────────────────────────────────────

def monday_query(query: str, variables: dict = None) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    r = requests.post("https://api.monday.com/v2", json=payload,
                      headers=MONDAY_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"Monday API error: {data['errors']}")
    return data


def get_item_columns(item_id: int) -> dict:
    """Return {column_id: text_value} for all columns on the item."""
    q = """
    query ($itemId: [ID!]) {
      items(ids: $itemId) {
        name
        column_values { id text value }
      }
    }
    """
    data = monday_query(q, {"itemId": [str(item_id)]})
    item = data["data"]["items"][0]
    cols = {"name": item["name"]}
    for cv in item["column_values"]:
        cols[cv["id"]] = cv["text"] or ""
    return cols


def update_monday_columns(item_id: int, col_values: dict):
    """Write multiple column values in one API call."""
    import json
    q = """
    mutation ($boardId: ID!, $itemId: ID!, $columnValues: JSON!) {
      change_multiple_column_values(
        board_id: $boardId
        item_id: $itemId
        column_values: $columnValues
        create_labels_if_missing: true
      ) { id }
    }
    """
    monday_query(q, {
        "boardId":      str(BOARD_ID),
        "itemId":       str(item_id),
        "columnValues": json.dumps(col_values),
    })
    log.info(f"  ✓ Updated item {item_id}: {list(col_values.keys())}")


def clear_file_column(item_id: int, column_id: str):
    """Clear all existing files from a Monday file column."""
    q = """
    mutation ($boardId: ID!, $itemId: ID!, $columnId: String!) {
      change_column_value(
        board_id: $boardId
        item_id: $itemId
        column_id: $columnId
        value: "{}"
      ) { id }
    }
    """
    try:
        monday_query(q, {
            "boardId":  str(BOARD_ID),
            "itemId":   str(item_id),
            "columnId": column_id,
        })
        log.info(f"  ✓ Cleared file column {column_id}")
    except Exception as e:
        log.warning(f"  Could not clear file column {column_id}: {e}")


def upload_file_to_column(item_id: int, column_id: str,
                           file_bytes: bytes, filename: str):
    """Upload a file to a Monday file column via multipart upload."""
    mutation = """
    mutation ($file: File!) {
      add_file_to_column(
        item_id: %s
        column_id: "%s"
        file: $file
      ) { id }
    }
    """ % (item_id, column_id)

    response = requests.post(
        "https://api.monday.com/v2/file",
        headers={"Authorization": MONDAY_API_KEY},
        files={
            "variables[file]": (filename, io.BytesIO(file_bytes), "image/png"),
        },
        data={"query": mutation},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if "errors" in result:
        log.warning(f"  File upload error for column {column_id}: {result['errors']}")
    else:
        log.info(f"  ✓ Uploaded {filename} to column {column_id}")


# ─── CSM scraper ─────────────────────────────────────────────────────────────

def search_csm(lastname: str, dateofbirth_ddmmyyyy: str) -> list[dict]:
    """
    POST to csm-examen.be/cdr and return a list of diploma dicts.
    Each dict has: 'type', 'fields' {label: value}, 'card_html' (raw HTML of card)
    Returns [] if nothing found.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; TSA-Safety-Bot/1.0)"
    })

    # 1. GET → CSRF token
    r = session.get(CSM_URL, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    token_input = soup.find("input", {"name": "_token"})
    if not token_input:
        raise RuntimeError("CSRF token not found on csm-examen.be")

    # 2. POST search
    r2 = session.post(CSM_URL, data={
        "_token":      token_input["value"],
        "lastname":    lastname.strip(),
        "dateofbirth": dateofbirth_ddmmyyyy.strip(),
    }, allow_redirects=True, timeout=15)
    r2.raise_for_status()

    soup2 = BeautifulSoup(r2.text, "html.parser")
    main = soup2.find("main")
    if not main or "geen diploma gevonden" in main.get_text().lower():
        return []

    diplomas = []
    for card in soup2.select(".diploma"):
        card_text = card.get_text(" ", strip=True).lower()

        # Detect certificate type
        cert_type = None
        for ctype, keywords in CERT_KEYWORDS.items():
            if any(kw in card_text for kw in keywords):
                cert_type = ctype
                break
        if cert_type is None:
            cert_type = "VCA"   # default fallback

        # Extract field key/value pairs from dl > dt + dd
        fields = {}
        for dl in card.select("dl"):
            for dt, dd in zip(dl.select("dt"), dl.select("dd")):
                key   = dt.get_text(strip=True).lower().rstrip(":")
                value = dd.get_text(strip=True)
                fields[key] = value

        diplomas.append({
            "type":      cert_type,
            "fields":    fields,
            "card_html": str(card),
        })

    return diplomas


def find_field(fields: dict, *keywords: str) -> str:
    """Return the first field value whose key contains any of the keywords."""
    for kw in keywords:
        for k, v in fields.items():
            if kw in k:
                return v
    return ""


def parse_date(raw: str) -> str | None:
    """Convert Belgian date formats to YYYY-MM-DD for Monday."""
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# ─── Screenshot helper (Playwright) ──────────────────────────────────────────

async def screenshot_diploma_card(lastname: str, dob_ddmmyyyy: str,
                                   cert_type: str) -> bytes | None:
    """
    Open csm-examen.be in a headless browser, search, and screenshot
    the diploma card matching cert_type. Returns PNG bytes or None.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright not installed — skipping screenshot. Run: playwright install chromium")
        return None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--no-zygote",
                    "--disable-gpu",
                ],
            )
            page = await browser.new_page(viewport={"width": 1200, "height": 900})

            await page.goto(CSM_URL, wait_until="networkidle")
            await page.fill('input[name="lastname"]',    lastname)
            await page.fill('input[name="dateofbirth"]', dob_ddmmyyyy)
            await page.click('input[type="submit"]')
            await page.wait_for_load_state("networkidle")

            # Find the card matching the cert type
            type_lower = cert_type.lower().replace("-", "")
            cards = await page.query_selector_all(".diploma")
            target = None
            for card in cards:
                text = (await card.inner_text()).lower().replace("-", "")
                if type_lower in text:
                    target = card
                    break

            if not target:
                # Fall back to first card
                target = cards[0] if cards else None

            if not target:
                await browser.close()
                return None

            screenshot = await target.screenshot(type="png")
            await browser.close()
            return screenshot

    except Exception as e:
        log.exception(f"Screenshot failed for {cert_type}: {e}")
        return None


# ─── Webhook handler ──────────────────────────────────────────────────────────

@app.get("/monday-webhook")
async def monday_webhook_verify():
    """Monday pings this with GET to verify the URL is reachable."""
    return {"status": "ok"}


@app.post("/monday-webhook")
async def monday_webhook(request: Request):
    body_bytes = await request.body()

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Monday challenge handshake (first-time URL verification)
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}

    event     = payload.get("event", {})
    item_id   = event.get("pulseId") or event.get("itemId")
    column_id = event.get("columnId", "")

    if not item_id:
        log.warning(f"No item ID in payload: {payload}")
        return {"ok": False, "reason": "no item_id"}

    # Only process when the CSM Lookup status column changes to "Opzoeken"
    if column_id and column_id != STATUS_COLUMN:
        log.info(f"Ignoring webhook from column {column_id!r}")
        return {"ok": False, "reason": "wrong column"}

    # Check the new label value
    new_label = (
        event.get("value", {}).get("label", {}).get("text", "")
        or event.get("value", {}).get("label", "")
    )
    if new_label and new_label != STATUS_TRIGGER:
        log.info(f"Ignoring — status set to {new_label!r}, not {STATUS_TRIGGER!r}")
        return {"ok": False, "reason": "not a trigger value"}

    log.info(f"▶ Button clicked — item {item_id}")

    try:
        # ── 1. Read Monday columns ────────────────────────────────────────────
        cols = get_item_columns(item_id)

        # Name is stored as "Familienaam, Voornaam" → extract last name
        full_name = cols.get("name", "").strip()
        if "," in full_name:
            lastname = full_name.split(",", 1)[0].strip()
        else:
            # Fallback: take the last word if no comma
            parts    = full_name.split()
            lastname = parts[-1] if parts else full_name

        dob_raw  = cols.get(COL_BIRTHDATE, "").strip()

        log.info(f"  Name: {full_name!r}  →  lastname: {lastname!r}")
        log.info(f"  Geboortedatum raw: {dob_raw!r}")

        if not lastname or not dob_raw:
            log.warning("  Missing last name or birth date — aborting")
            return {"ok": False, "reason": "missing input data"}

        # Monday Date columns return text as "YYYY-MM-DD"; convert for CSM
        dob_iso  = parse_date(dob_raw)
        dob_csm  = datetime.strptime(dob_iso, "%Y-%m-%d").strftime("%d-%m-%Y") if dob_iso else dob_raw

        log.info(f"  Searching CSM: {lastname!r} / {dob_csm!r}")

        # ── 2. Search csm-examen.be ───────────────────────────────────────────
        diplomas = search_csm(lastname, dob_csm)

        if not diplomas:
            log.info(f"  No diploma found for {lastname} / {dob_csm}")
            update_monday_columns(item_id, {STATUS_COLUMN: {"label": STATUS_ERROR}})
            return {"ok": True, "found": False, "diplomas": 0}

        log.info(f"  Found {len(diplomas)} diploma(s): {[d['type'] for d in diplomas]}")

        # ── 3. Build Monday update payload ────────────────────────────────────
        col_values = {}

        for diploma in diplomas:
            ctype  = diploma["type"]
            fields = diploma["fields"]

            if ctype not in CERT_COLUMNS:
                log.warning(f"  Unknown cert type {ctype!r} — skipping")
                continue

            col_nr, col_date, col_file = CERT_COLUMNS[ctype]

            # Certificate number
            cert_nr = find_field(fields, "nummer", "number", "diplom", "referentie", "ref")
            if cert_nr:
                col_values[col_nr] = cert_nr
                log.info(f"  {ctype} nummer: {cert_nr}")

            # Expiry date
            expiry_raw = find_field(fields, "geldig tot", "vervalt", "expir", "einddatum", "until", "tot")
            if expiry_raw:
                expiry_iso = parse_date(expiry_raw)
                if expiry_iso:
                    col_values[col_date] = {"date": expiry_iso}
                    log.info(f"  {ctype} einddatum: {expiry_iso}")

        # Write text + date columns in one call
        if col_values:
            update_monday_columns(item_id, col_values)

        # ── 4. Screenshots → file columns ─────────────────────────────────────
        for diploma in diplomas:
            ctype = diploma["type"]
            if ctype not in CERT_COLUMNS:
                continue
            _, _, col_file = CERT_COLUMNS[ctype]

            screenshot = await screenshot_diploma_card(lastname, dob_csm, ctype)
            if screenshot:
                clear_file_column(item_id, col_file)
                filename = f"{lastname}_{ctype}_{datetime.now().strftime('%Y%m%d')}.png"
                upload_file_to_column(item_id, col_file, screenshot, filename)
            else:
                log.info(f"  No screenshot for {ctype}")

        # ── 5. Set status to "Gedaan" ─────────────────────────────────────────
        update_monday_columns(item_id, {STATUS_COLUMN: {"label": STATUS_DONE}})

        return {"ok": True, "found": True, "diplomas": len(diplomas),
                "types": [d["type"] for d in diplomas]}

    except Exception as e:
        log.exception(f"Error processing item {item_id}: {e}")
        # Reset status so user knows something went wrong
        try:
            update_monday_columns(int(item_id), {STATUS_COLUMN: {"label": STATUS_ERROR}})
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("csm_monday_webhook_board3:app", host="0.0.0.0", port=8000, reload=True)
