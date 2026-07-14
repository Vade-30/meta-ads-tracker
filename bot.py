"""
bot.py — Interactive Telegram bot commands for Meta Ads Gmail Monitor.

This module processes incoming commands (e.g. /help, /summary, /history)
sent to the Telegram bot, queries the SQLite database, and replies.

Since this tool runs serverless, command processing occurs at the
beginning of the 10-minute check_gmail.py execution.

Reuses:
- db.py         : get_charges_since(), get_state(), set_state()
- alert.py      : get_updates(), send_message()
- summary.py      : compute_stats(), format_summary_message()
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from db import get_state, set_state, get_charges_since
from alert import get_updates, send_message
from summary import compute_stats, format_summary_message

logger = logging.getLogger(__name__)

# State Key
STATE_LAST_UPDATE_ID = "last_telegram_update_id"

# Timezone
PHT_TZ = timezone(timedelta(hours=8))


def _handle_help() -> str:
    """Return the help menu text."""
    return (
        "🤖 <b>Meta Ads Tracker Bot</b>\n"
        "\n"
        "Available commands:\n"
        "  /summary — Show 12-hour summary on-demand\n"
        "  /history — Show transaction history since 3:00 PM PHT\n"
        "  /help    — Show this help menu"
    )


def _handle_summary(conn) -> str:
    """Generate the on-demand 12-hour summary message."""
    now_utc = datetime.now(timezone.utc)
    # The summary is always for the last 12 hours
    period_start = now_utc - timedelta(hours=12)

    charges = get_charges_since(conn, period_start)
    stats = compute_stats(charges, period_start, now_utc)
    return format_summary_message(stats, period_start, now_utc)


def _handle_history(conn) -> str:
    """
    Generate transaction history since the most recent 3:00 PM PHT.
    
    Resets at 3:00 PM PHT every day:
    - If requested after 3:00 PM PHT: start is 3:00 PM PHT today.
    - If requested before 3:00 PM PHT: start is 3:00 PM PHT yesterday.
    """
    now_pht = datetime.now(PHT_TZ)
    today_3pm = now_pht.replace(hour=15, minute=0, second=0, microsecond=0)

    if now_pht >= today_3pm:
        start_pht = today_3pm
    else:
        start_pht = today_3pm - timedelta(days=1)

    # Convert to UTC for SQLite comparison
    start_utc = start_pht.astimezone(timezone.utc)
    charges = get_charges_since(conn, start_utc)

    def fmt_amt(val: float) -> str:
        return f"{int(val)}" if val.is_integer() else f"{val:.2f}"

    ewma_str = get_state(conn, "ewma_rate")
    ewma_val_formatted = "N/A"
    if ewma_str:
        try:
            ewma_val_formatted = f"{float(ewma_str):.2f}"
        except ValueError:
            pass

    if not charges:
        ewma_line = f"\n14-Day Rolling Average: {ewma_val_formatted} charges/hour" if ewma_val_formatted != "N/A" else ""
        return f"Total: $0{ewma_line}\nNo transactions recorded."

    total = sum(float(row["amount"]) for row in charges)
    
    lines = [
        f"Total: ${fmt_amt(total)}",
        f"14-Day Rolling Average: {ewma_val_formatted} charges/hour"
    ]

    for row in charges:
        try:
            ts_utc = datetime.fromisoformat(row["timestamp"]).astimezone(timezone.utc)
            ts_pht = ts_utc.astimezone(PHT_TZ)
            time_str = ts_pht.strftime("%I:%M%p").lower()
            if time_str.startswith("0"):
                time_str = time_str[1:]
        except Exception:
            time_str = "unknown"

        amount = float(row["amount"])
        lines.append(f"{time_str} - ${fmt_amt(amount)}")

    return "\n".join(lines)


def process_telegram_commands(conn) -> None:
    """
    Poll for new Telegram messages, execute commands, send replies,
    and persist the last processed update ID.
    """
    # Verify environment variables
    auth_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not auth_chat_id:
        logger.warning("TELEGRAM_CHAT_ID is not set. Skipping command polling.")
        return

    # Retrieve last processed update ID
    last_update_id_str = get_state(conn, STATE_LAST_UPDATE_ID)
    last_update_id = int(last_update_id_str) if last_update_id_str else None

    logger.info("Polling Telegram commands (offset = %s)...", last_update_id)

    offset = (last_update_id + 1) if last_update_id is not None else None
    updates = get_updates(offset=offset)

    if not updates:
        logger.info("No new Telegram messages found.")
        return

    logger.info("Found %d new update(s) to check.", len(updates))
    new_last_update_id = last_update_id

    for update in updates:
        uid = update.get("update_id")
        if uid is not None:
            new_last_update_id = max(new_last_update_id or 0, uid)

        # Retrieve message block
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = (msg.get("text") or "").strip()

        if not text:
            continue

        # SECURITY: Only reply to commands from the authorized CHAT_ID
        if chat_id != auth_chat_id:
            logger.warning(
                "Ignoring message from unauthorized chat_id: %s (expected %s)",
                chat_id,
                auth_chat_id,
            )
            continue

        # We only process command triggers starting with a slash '/'
        if not text.startswith("/"):
            continue

        logger.info("Processing command: %s (update_id: %s)", text, uid)

        parts = text.lower().split()
        command = parts[0]

        if command == "/help":
            reply = _handle_help()
        elif command == "/summary":
            reply = _handle_summary(conn)
        elif command == "/history":
            reply = _handle_history(conn)
        else:
            # Silent ignore or friendly unrecognized command
            continue

        # Send response
        send_message(reply)

    # Persist the update ID so we don't process these commands again
    if new_last_update_id is not None and new_last_update_id != last_update_id:
        set_state(conn, STATE_LAST_UPDATE_ID, str(new_last_update_id))
        logger.info("Persisted last_telegram_update_id = %d", new_last_update_id)
