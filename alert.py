"""
alert.py — Persistent Telegram alerting for Meta Ads Gmail Monitor.

Sends an alert message and repeats it in a tiered loop until the user
sends /ack or the 30-minute hard cap is reached.

Resend schedule
---------------
  0 – 2 min  : every 10 seconds
  2 – 10 min : every 30 seconds
  10 – 30 min: every 60 seconds
  30 min     : hard stop, send final notice

Acknowledgment
--------------
Between each send, poll Telegram getUpdates for a message that is exactly
"/ack" (case-insensitive) sent AFTER the alert began.  On match, send a
confirmation message and exit immediately.

Usage
-----
Called from check_gmail.py; can also be run standalone for testing:
    python alert.py --test
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Alert loop timing (seconds)
TIER_1_INTERVAL = 5     # 0 – 5 min (every 5 seconds)
TIER_2_INTERVAL = 30    # 5 – 10 min (every 30 seconds)
TIER_3_INTERVAL = 60    # 10 – 30 min (every 60 seconds)
TIER_1_END      = 300   # 5 min (300 seconds)
TIER_2_END      = 600   # 10 min (600 seconds)
HARD_CAP        = 1800  # 30 min (1800 seconds)

HTTP_TIMEOUT = 15  # seconds per Telegram request


# ── Telegram helpers ───────────────────────────────────────────────────────

def _bot_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise EnvironmentError("TELEGRAM_BOT_TOKEN env var is not set.")
    return token


def _chat_id() -> str:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        raise EnvironmentError("TELEGRAM_CHAT_ID env var is not set.")
    return chat_id


def _api_url(method: str) -> str:
    return TELEGRAM_API.format(token=_bot_token(), method=method)


import re

def html_to_markdown(html_text: str) -> str:
    """Convert basic HTML tags (b, code, pre, br) to Markdown for Discord."""
    text = html_text
    # Replace bold tags
    text = re.sub(r'</?b>', '**', text)
    # Replace code tags
    text = re.sub(r'</?code>', '`', text)
    # Replace pre tags
    text = re.sub(r'</?pre>', '```', text)
    # Replace br tags
    text = re.sub(r'<br\s*/?>', '\n', text)
    # Remove any remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    return text


def send_discord_message(text: str) -> bool:
    """
    Send a message to Discord via webhook.
    Converts HTML format to Markdown.
    Returns True on success, False if webhook URL is not set or failed.
    """
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        logger.debug("DISCORD_WEBHOOK_URL is not set. Skipping Discord alert.")
        return False

    markdown_text = html_to_markdown(text)
    try:
        resp = requests.post(
            webhook_url,
            json={"content": markdown_text},
            timeout=HTTP_TIMEOUT,
        )
        if not resp.ok:
            logger.warning(
                "Discord webhook failed: HTTP %d — %s",
                resp.status_code,
                resp.text[:200],
            )
            return False
        logger.debug("Discord webhook message sent successfully.")
        return True
    except requests.RequestException as exc:
        logger.warning("Discord webhook request error: %s", exc)
        return False


def send_message(text: str) -> bool:
    """
    Send a Telegram message and a Discord message (if webhook is configured).
    Returns True on Telegram success, False on Telegram error.
    Never raises — errors are logged and swallowed so the alert loop continues.
    """
    # Try sending to Discord (ignored if webhook URL not set)
    send_discord_message(text)

    # Try sending to Telegram
    try:
        resp = requests.post(
            _api_url("sendMessage"),
            json={"chat_id": _chat_id(), "text": text, "parse_mode": "HTML"},
            timeout=HTTP_TIMEOUT,
        )
        if not resp.ok:
            logger.warning(
                "Telegram sendMessage failed: HTTP %d — %s",
                resp.status_code,
                resp.text[:200],
            )
            return False
        logger.debug("Telegram message sent (len=%d).", len(text))
        return True
    except requests.RequestException as exc:
        logger.warning("Telegram sendMessage request error: %s", exc)
        return False


def get_updates(offset: Optional[int] = None) -> list[dict]:
    """
    Fetch updates from Telegram getUpdates.  Returns list of update dicts.
    Never raises — errors are logged and an empty list is returned.
    """
    params: dict = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(
            _api_url("getUpdates"),
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        if not resp.ok:
            logger.warning(
                "Telegram getUpdates failed: HTTP %d — %s",
                resp.status_code,
                resp.text[:200],
            )
            return []
        data = resp.json()
        return data.get("result", [])
    except requests.RequestException as exc:
        logger.warning("Telegram getUpdates request error: %s", exc)
        return []


def _check_for_ack(
    last_update_id: Optional[int],
    alert_start_epoch: float,
) -> tuple[bool, Optional[int]]:
    """
    Poll for a /ack message sent after alert_start_epoch.

    Returns (acknowledged, new_last_update_id).
    """
    offset = (last_update_id + 1) if last_update_id is not None else None
    updates = get_updates(offset=offset)

    new_last_update_id = last_update_id
    for update in updates:
        uid = update.get("update_id")
        if uid is not None:
            new_last_update_id = max(new_last_update_id or 0, uid)

        msg = update.get("message") or update.get("channel_post") or {}
        msg_date = msg.get("date", 0)           # Unix timestamp from Telegram
        msg_text = (msg.get("text") or "").strip()

        if msg_date >= alert_start_epoch and msg_text.lower() == "/ack":
            logger.info("ACK received (update_id=%s).", uid)
            return True, new_last_update_id

    return False, new_last_update_id


# ── Alert message builder ──────────────────────────────────────────────────

def _build_alert_text(
    triggered_reasons: list[str],
    charge_info: dict,
    send_index: int,
    elapsed_seconds: float,
) -> str:
    """
    Build the Telegram alert message.

    charge_info keys: amount, timestamp, card_name, merchant, message_id
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    elapsed_min = int(elapsed_seconds // 60)
    elapsed_sec = int(elapsed_seconds % 60)

    reasons_block = "\n".join(f"  ⚠️ {r}" for r in triggered_reasons)

    amount    = charge_info.get("amount", 0.0)
    timestamp = charge_info.get("timestamp", "unknown")
    card_name = charge_info.get("card_name", "unknown")
    merchant  = charge_info.get("merchant", "unknown")

    text = (
        f"🚨 <b>META ADS SUSPICIOUS CHARGE — ALERT #{send_index}</b> 🚨\n"
        f"Sent at: {now_str}  (elapsed: {elapsed_min}m {elapsed_sec}s)\n"
        f"\n"
        f"<b>Charge details</b>\n"
        f"  Merchant : {merchant}\n"
        f"  Amount   : ${amount:.2f}\n"
        f"  Time     : {timestamp}\n"
        f"  Card     : {card_name}\n"
        f"\n"
        f"<b>Triggered rules</b>\n"
        f"{reasons_block}\n"
        f"\n"
        f"Reply <code>/ack</code> to stop these alerts.\n"
        f"If you did not authorize this, check Meta Ads Manager and your "
        f"Extend virtual card immediately."
    )
    return text


# ── Main alert loop ────────────────────────────────────────────────────────

def run_alert_loop(
    triggered_reasons: list[str],
    charge_info: dict,
) -> None:
    """
    Send an alert and repeat it in a tiered loop until /ack or 30-min cap.

    Parameters
    ----------
    triggered_reasons : list[str]
        Human-readable descriptions of which rules fired.
    charge_info : dict
        Keys: amount, timestamp, card_name, merchant, message_id.
    """
    alert_start = time.monotonic()
    alert_start_epoch = datetime.now(timezone.utc).timestamp()

    last_update_id: Optional[int] = None
    send_index = 0

    logger.info(
        "Starting alert loop. Reasons: %s", " | ".join(triggered_reasons)
    )

    # Drain any stale updates that pre-date this alert so we don't
    # accidentally ACK on an old /ack command.
    updates = get_updates()
    for update in updates:
        uid = update.get("update_id")
        if uid is not None:
            last_update_id = max(last_update_id or 0, uid)
    if last_update_id is not None:
        logger.debug("Drained stale updates; last_update_id=%d", last_update_id)

    while True:
        elapsed = time.monotonic() - alert_start

        if elapsed >= HARD_CAP:
            final = (
                "⏰ <b>META ADS MONITOR — Alert timeout</b>\n"
                "30-minute alert window has elapsed with no /ack received.\n"
                "Please check Gmail and Meta Ads Manager manually when you are able."
            )
            send_message(final)
            logger.info("Hard cap reached (30 min). Alert loop ending.")
            break

        # ── Determine current sleep interval ─────────────────────────────
        if elapsed < TIER_1_END:
            interval = TIER_1_INTERVAL
            tier = 1
        elif elapsed < TIER_2_END:
            interval = TIER_2_INTERVAL
            tier = 2
        else:
            interval = TIER_3_INTERVAL
            tier = 3

        # ── Send alert ────────────────────────────────────────────────────
        send_index += 1
        alert_text = _build_alert_text(
            triggered_reasons, charge_info, send_index, elapsed
        )
        logger.info(
            "Sending alert #%d (tier=%d, elapsed=%.0fs)",
            send_index,
            tier,
            elapsed,
        )
        send_message(alert_text)

        # ── Sleep in short increments and poll for /ack ───────────────────
        poll_interval = 5   # check for /ack every 5 seconds during sleep
        sleep_remaining = interval

        while sleep_remaining > 0:
            sleep_chunk = min(poll_interval, sleep_remaining)
            time.sleep(sleep_chunk)
            sleep_remaining -= sleep_chunk

            acked, last_update_id = _check_for_ack(last_update_id, alert_start_epoch)
            if acked:
                confirm = (
                    "✅ <b>META ADS MONITOR — Acknowledged</b>\n"
                    "Alert loop stopped. Stay safe and verify the charge in "
                    "Meta Ads Manager if anything looks off."
                )
                send_message(confirm)
                logger.info("Alert acknowledged by user. Loop exiting.")
                return

            # Re-check hard cap inside the sleep loop
            if time.monotonic() - alert_start >= HARD_CAP:
                break

    logger.info("Alert loop finished.")


# ── Standalone test entry point ────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    if "--test" in sys.argv:
        print("Running standalone alert test…")
        run_alert_loop(
            triggered_reasons=[
                "AMOUNT RULE: charge of $1200.00 exceeds the $900.00 fixed threshold",
                "OVERALL FREQUENCY RULE: 8 charges this hour exceeds 2× the rolling EWMA baseline of 3.2 charges/hour (threshold: 6.4)",
            ],
            charge_info={
                "amount": 1200.00,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "card_name": "VIZOYA REWARDS GRAPHITE",
                "merchant": "FACEBOOK ADVERTISING",
                "message_id": "test-000",
            },
        )
    else:
        print("Usage: python alert.py --test")
        sys.exit(1)
