"""
check_gmail.py — Main detection script for Meta Ads Gmail Monitor.

Workflow
--------
1. Authenticate to Gmail API via OAuth refresh token (env vars only, no files).
2. Search for purchase notifications from support@paywithextend.com.
3. Only process messages newer than the last processed message ID.
4. Parse each email: extract merchant, amount, timestamp, card name.
5. Only keep charges where merchant contains "FACEBOOK" (case-insensitive).
6. Append valid charges to the SQLite database.
7. Run detection rules against the updated history.
8. If any rule fires, invoke the Telegram alert loop (blocks up to 30 min).

CLI flags
---------
--test-alert   Skip Gmail entirely; fire a fake flagged charge through the
               full alert pipeline so you can verify Telegram/ack flow.
--debug        Set log level to DEBUG.

Environment variables required (set as GitHub Secrets)
-------------------------------------------------------
GMAIL_CLIENT_ID
GMAIL_CLIENT_SECRET
GMAIL_REFRESH_TOKEN
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
"""

import argparse
import base64
import email
import email.policy
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

# Third-party (installed via requirements.txt)
import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Local modules
from db import init_db, insert_charge, get_all_charges, get_state, set_state
from rules import evaluate_rules
from alert import run_alert_loop

# ── Logging setup ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("check_gmail")

# ── Constants ──────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SENDER_FILTER = "support@paywithextend.com"
SUBJECT_FILTER = '"A purchase was made"'
SEARCH_QUERY = f'from:{SENDER_FILTER} subject:{SUBJECT_FILTER}'
FACEBOOK_MERCHANT_PATTERN = re.compile(r"facebook", re.IGNORECASE)

# State keys
STATE_LAST_MSG_ID = "last_message_id"
STATE_EWMA        = "ewma_rate"

# Labeled field patterns (case-insensitive, flexible whitespace)
_FIELD_RE = {
    "merchant": re.compile(r"Merchant\s+Name\s*:\s*(.+)", re.IGNORECASE),
    "amount":   re.compile(r"Authorization\s+Amount\s*:\s*\$?([\d\W]+)", re.IGNORECASE),
    "date":     re.compile(r"^Date\s*:\s*(.+)", re.IGNORECASE | re.MULTILINE),
    "card":     re.compile(r"Virtual\s+Card\s+Name\s*:\s*(.+)", re.IGNORECASE),
}

# ── Gmail authentication ───────────────────────────────────────────────────

def _build_gmail_service():
    """
    Build an authenticated Gmail API service using env-var credentials only.
    No token files are read or written — everything lives in env vars.
    """
    client_id     = os.environ.get("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN", "").strip()

    missing = [
        name for name, val in [
            ("GMAIL_CLIENT_ID", client_id),
            ("GMAIL_CLIENT_SECRET", client_secret),
            ("GMAIL_REFRESH_TOKEN", refresh_token),
        ] if not val
    ]
    if missing:
        logger.error("Missing required env vars: %s", ", ".join(missing))
        sys.exit(1)

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=GMAIL_SCOPES,
    )

    # Refresh to get a valid access token
    try:
        creds.refresh(GoogleAuthRequest())
    except Exception as exc:
        logger.error("Failed to refresh Gmail OAuth token: %s", exc)
        sys.exit(1)

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    logger.info("Gmail API authenticated successfully.")
    return service


# ── Email parsing ──────────────────────────────────────────────────────────

def _strip_html_tags(html: str) -> str:
    """Very lightweight HTML tag remover — good enough for plain-text extraction."""
    return re.sub(r"<[^>]+>", " ", html)


def _decode_part(part) -> Optional[str]:
    """
    Decode a single MIME part's payload to a UTF-8 string.
    Handles quoted-printable and base64 automatically via get_payload(decode=True).
    """
    try:
        raw_bytes = part.get_payload(decode=True)
        if raw_bytes is None:
            return None
        charset = part.get_content_charset() or "utf-8"
        return raw_bytes.decode(charset, errors="replace")
    except Exception as exc:
        logger.debug("Could not decode MIME part: %s", exc)
        return None


def _extract_body(raw_bytes: bytes) -> Optional[str]:
    """
    Parse a raw RFC-822 email and return the best plain-text body.
    Prefers text/plain; falls back to text/html (tags stripped).
    """
    try:
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.compat32)
    except Exception as exc:
        logger.warning("email.message_from_bytes failed: %s", exc)
        return None

    plain_text = None
    html_text  = None

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and plain_text is None:
                plain_text = _decode_part(part)
            elif ct == "text/html" and html_text is None:
                html_text = _decode_part(part)
    else:
        ct = msg.get_content_type()
        decoded = _decode_part(msg)
        if ct == "text/plain":
            plain_text = decoded
        elif ct == "text/html":
            html_text = decoded

    if plain_text:
        return plain_text
    if html_text:
        return _strip_html_tags(html_text)
    return None


def _parse_amount(raw: str) -> Optional[float]:
    """
    Strip invisible Unicode characters and non-numeric junk, then parse float.
    Handles U+2060 WORD JOINER and similar zero-width chars embedded by Extend.
    """
    # Remove everything that is not a digit or a literal period
    cleaned = re.sub(r"[^\d.]", "", raw)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_date(raw: str) -> Optional[datetime]:
    """
    Parse Extend's date format, e.g. "July 4, 2026".
    Returns a UTC-midnight datetime on success, None on failure.
    """
    raw = raw.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.debug("Could not parse date string: %r", raw)
    return None


def _parse_email(msg_id: str, raw_bytes: bytes, email_timestamp: datetime) -> Optional[dict]:
    """
    Extract charge fields from a raw email.

    Returns a dict with keys: message_id, merchant, amount, timestamp, card_name.
    Returns None if parsing fails or merchant is not Facebook.
    """
    body = _extract_body(raw_bytes)
    if body is None:
        logger.warning("[%s] Could not extract body from email.", msg_id)
        return None

    # Extract fields via regex
    parsed = {}
    for field, pattern in _FIELD_RE.items():
        m = pattern.search(body)
        if m:
            parsed[field] = m.group(1).strip()
        else:
            logger.debug("[%s] Field %r not found in body.", msg_id, field)

    # Merchant check (bail early if not Facebook)
    merchant = parsed.get("merchant", "")
    if not FACEBOOK_MERCHANT_PATTERN.search(merchant):
        logger.info(
            "[%s] Skipping: merchant %r is not Facebook.", msg_id, merchant
        )
        return None

    # Amount
    raw_amount = parsed.get("amount", "")
    amount = _parse_amount(raw_amount)
    if amount is None:
        logger.warning(
            "[%s] Could not parse amount from %r — skipping.", msg_id, raw_amount
        )
        return None

    # Timestamp: Use the precise email receipt/sent time
    timestamp = email_timestamp

    card_name = parsed.get("card", "unknown card")

    logger.info(
        "[%s] Parsed: merchant=%r  amount=%.2f  date=%s  card=%r",
        msg_id,
        merchant,
        amount,
        timestamp.strftime("%Y-%m-%d %H:%M:%S UTC"),
        card_name,
    )

    return {
        "message_id": msg_id,
        "merchant":   merchant,
        "amount":     amount,
        "timestamp":  timestamp,
        "card_name":  card_name,
    }


# ── Gmail fetching ─────────────────────────────────────────────────────────

def _fetch_new_messages(service, last_msg_id: Optional[str]) -> list[dict]:
    """
    Search Gmail and return raw message dicts for messages not yet processed.

    We fetch up to 7 days of messages (recent enough for any gap), then
    filter client-side by internalDate to only include those newer than the
    last_msg_id we've seen.  This guards against Gmail IDs not being strictly
    time-monotonic.
    """
    messages = []
    page_token = None
    query = SEARCH_QUERY + " newer_than:7d"

    logger.info("Gmail search query: %r", query)

    # Collect all matching message IDs (paginated)
    while True:
        try:
            kwargs: dict = {"userId": "me", "q": query, "maxResults": 100}
            if page_token:
                kwargs["pageToken"] = page_token
            result = service.users().messages().list(**kwargs).execute()
        except HttpError as exc:
            logger.error("Gmail list error: %s", exc)
            break

        batch = result.get("messages", [])
        messages.extend(batch)
        logger.info("Fetched %d message IDs (page).", len(batch))

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    if not messages:
        logger.info("No matching messages found.")
        return []

    logger.info("Total matching messages: %d", len(messages))

    # Find the internalDate of the last processed message so we can filter
    last_internal_date: Optional[int] = None
    if last_msg_id:
        try:
            meta = (
                service.users()
                .messages()
                .get(userId="me", id=last_msg_id, format="metadata")
                .execute()
            )
            last_internal_date = int(meta.get("internalDate", 0))
            logger.info(
                "Last processed message internalDate: %s ms", last_internal_date
            )
        except HttpError as exc:
            logger.warning(
                "Could not fetch metadata for last_msg_id=%s: %s — "
                "will fall back to processing all returned messages.",
                last_msg_id,
                exc,
            )

    # Fetch full content for each candidate message
    new_messages = []
    for msg_stub in messages:
        msg_id = msg_stub["id"]

        # Skip the last processed message itself
        if msg_id == last_msg_id:
            continue

        try:
            full = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="raw")
                .execute()
            )
        except HttpError as exc:
            logger.warning("[%s] Failed to fetch full message: %s — skipping.", msg_id, exc)
            continue

        # Filter by internalDate if we have a reference
        if last_internal_date is not None:
            msg_date = int(full.get("internalDate", 0))
            if msg_date <= last_internal_date:
                logger.debug(
                    "[%s] internalDate %s ≤ last processed %s — skipping.",
                    msg_id,
                    msg_date,
                    last_internal_date,
                )
                continue

        new_messages.append(full)

    logger.info("%d new (unprocessed) messages to evaluate.", len(new_messages))
    return new_messages


def _decode_raw_message(full_msg: dict) -> Optional[bytes]:
    """Decode the base64url-encoded raw RFC-822 bytes from the Gmail API response."""
    raw_b64 = full_msg.get("raw", "")
    if not raw_b64:
        return None
    try:
        return base64.urlsafe_b64decode(raw_b64 + "==")
    except Exception as exc:
        logger.warning("base64 decode failed for message: %s", exc)
        return None


# ── Test alert mode ────────────────────────────────────────────────────────

def _run_test_alert() -> None:
    """
    Fire a fake alert through the full Telegram pipeline.
    No Gmail or DB access required — useful for end-to-end testing.
    """
    logger.info("=== TEST ALERT MODE — no real Gmail data used ===")
    fake_reasons = [
        "AMOUNT RULE: charge of $1,500.00 exceeds the $900.00 fixed threshold [TEST]",
        "OVERALL FREQUENCY RULE: 12 charges this hour exceeds 2× the rolling EWMA "
        "baseline of 4.0 charges/hour (threshold: 8.0) [TEST]",
        "PER-HOUR FREQUENCY RULE: 12 charges at hour 14:xx exceeds 2× the "
        "14:xx historical average of 3.5 charges (threshold: 7.0, based on 6 days) [TEST]",
    ]
    fake_charge = {
        "amount":     1500.00,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "card_name":  "TEST CARD",
        "merchant":   "FACEBOOK ADVERTISING [TEST]",
        "message_id": "test-alert-0001",
    }
    run_alert_loop(fake_reasons, fake_charge)
    logger.info("Test alert loop completed.")


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Meta Ads Gmail Monitor")
    parser.add_argument(
        "--test-alert",
        action="store_true",
        help="Skip Gmail; fire a fake flagged charge to test Telegram alerts.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Test mode shortcut ─────────────────────────────────────────────────
    if args.test_alert:
        _run_test_alert()
        return

    # ── Database init ──────────────────────────────────────────────────────
    conn = init_db()

    # ── Load persisted state ───────────────────────────────────────────────
    last_msg_id = get_state(conn, STATE_LAST_MSG_ID)
    saved_ewma_str = get_state(conn, STATE_EWMA)
    saved_ewma: Optional[float] = float(saved_ewma_str) if saved_ewma_str else None

    logger.info("Last processed message ID: %s", last_msg_id or "(none — first run)")
    logger.info("Saved EWMA: %s", saved_ewma if saved_ewma is not None else "(none)")

    # ── Gmail ──────────────────────────────────────────────────────────────
    service = _build_gmail_service()
    new_messages = _fetch_new_messages(service, last_msg_id)

    if not new_messages:
        logger.info("Nothing new to process. Exiting cleanly.")
        conn.close()
        return

    # ── Process each new message ───────────────────────────────────────────
    any_alert_fired = False
    newest_msg_id = last_msg_id  # track to persist after loop

    for full_msg in new_messages:
        msg_id = full_msg.get("id", "unknown")
        logger.info("Processing message: %s", msg_id)

        raw_bytes = _decode_raw_message(full_msg)
        if raw_bytes is None:
            logger.warning("[%s] No raw bytes — skipping.", msg_id)
            continue

        # Get receipt timestamp from internalDate (ms since epoch)
        try:
            timestamp_ms = int(full_msg.get("internalDate", 0))
            email_timestamp = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        except Exception:
            email_timestamp = datetime.now(timezone.utc)

        # Parse email
        charge = _parse_email(msg_id, raw_bytes, email_timestamp)
        if charge is None:
            # Not a Facebook charge or unparseable — still update last_msg_id
            newest_msg_id = msg_id
            continue

        # Store in DB (INSERT OR IGNORE on duplicate message_id)
        inserted = insert_charge(
            conn,
            message_id=charge["message_id"],
            timestamp=charge["timestamp"],
            amount=charge["amount"],
            merchant=charge["merchant"],
            card_name=charge["card_name"],
        )

        if not inserted:
            logger.info("[%s] Already in DB — skipping rule evaluation.", msg_id)
            newest_msg_id = msg_id
            continue

        # Load full history for rule evaluation
        all_charges = get_all_charges(conn)

        # Evaluate detection rules
        triggered, updated_ewma = evaluate_rules(
            new_charge_amount=charge["amount"],
            all_charges=all_charges,
            saved_ewma=saved_ewma,
            charge_timestamp=charge["timestamp"],
        )

        # Persist updated EWMA
        set_state(conn, STATE_EWMA, str(updated_ewma))
        saved_ewma = updated_ewma

        newest_msg_id = msg_id

        if triggered:
            logger.warning(
                "[%s] ALERT: %d rule(s) fired — invoking Telegram loop.",
                msg_id,
                len(triggered),
            )
            any_alert_fired = True

            charge_info = {
                "amount":     charge["amount"],
                "timestamp":  charge["timestamp"].isoformat(),
                "card_name":  charge["card_name"],
                "merchant":   charge["merchant"],
                "message_id": charge["message_id"],
            }
            run_alert_loop(triggered, charge_info)
            # Alert loop blocks until ACK or 30-min cap — continue processing
            # remaining messages after it returns.
        else:
            logger.info("[%s] All rules passed — no alert needed.", msg_id)

    # ── Persist last processed message ID ──────────────────────────────────
    if newest_msg_id and newest_msg_id != last_msg_id:
        set_state(conn, STATE_LAST_MSG_ID, newest_msg_id)
        logger.info("Persisted last_message_id = %s", newest_msg_id)

    conn.close()

    if not any_alert_fired:
        logger.info("Run complete — no suspicious charges detected.")
    else:
        logger.info("Run complete — alert(s) were fired.")


if __name__ == "__main__":
    main()
