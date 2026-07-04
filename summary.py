"""
summary.py — 12-hour periodic charge summary for Meta Ads Gmail Monitor.

Reads the existing SQLite charge history (same cache as check_gmail.py),
computes stats for the last 12 hours, and sends ONE Telegram message.

This is a routine status report — NOT a fraud alert.  It sends a single
message with no repeat loop and requires no acknowledgment.

Triggered by .github/workflows/summary.yml (cron: 12:00 AM and 12:00 PM UTC).

Reuses:
- db.py         : init_db(), get_charges_since()
- rules.py      : charges_to_hourly_buckets()
- alert.py      : send_message()

No new secrets or dependencies required.
"""

import logging
import sys
from datetime import datetime, timedelta, timezone

from db import init_db, get_charges_since
from rules import charges_to_hourly_buckets
from alert import send_message

# ── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("summary")

# ── Tuneable constants ─────────────────────────────────────────────────────

SUMMARY_PERIOD_HOURS: int = 12   # Length of the summary window (hours)


# ── Stats computation ──────────────────────────────────────────────────────

def compute_stats(charges: list, period_start: datetime, period_end: datetime) -> dict:
    """
    Compute summary statistics over a list of charge rows.

    Parameters
    ----------
    charges     : list of sqlite3.Row — already filtered to the period
    period_start: datetime (UTC) — start of the summary window
    period_end  : datetime (UTC) — end of the summary window (≈ now)

    Returns
    -------
    dict with keys:
        count, total, avg_amount,
        avg_gap_minutes (None if < 2 charges),
        busiest_hour_label, busiest_hour_count
    """
    count = len(charges)

    if count == 0:
        return {
            "count": 0,
            "total": 0.0,
            "avg_amount": None,
            "avg_gap_minutes": None,
            "busiest_hour_label": None,
            "busiest_hour_count": 0,
        }

    # Total and average amount
    total = sum(float(row["amount"]) for row in charges)
    avg_amount = total / count

    # Average gap between consecutive charges
    timestamps = []
    for row in charges:
        try:
            ts = datetime.fromisoformat(row["timestamp"]).astimezone(timezone.utc)
            timestamps.append(ts)
        except (ValueError, TypeError):
            logger.warning("Unparseable timestamp in summary: %s", row["timestamp"])

    timestamps.sort()

    if len(timestamps) >= 2:
        gaps = [
            (timestamps[i + 1] - timestamps[i]).total_seconds() / 60.0
            for i in range(len(timestamps) - 1)
        ]
        avg_gap_minutes = sum(gaps) / len(gaps)
    else:
        avg_gap_minutes = None  # only 1 charge — no gap to compute

    # Busiest hour within the period
    PHT_TZ = timezone(timedelta(hours=8))
    buckets = charges_to_hourly_buckets(charges)
    if buckets:
        busiest_key = max(buckets, key=lambda k: buckets[k])
        busiest_hour_count = buckets[busiest_key]
        year, month, day, hour = busiest_key
        # Convert UTC bucket hour to PHT
        try:
            start_dt = datetime(year, month, day, hour, tzinfo=timezone.utc)
            end_dt   = start_dt + timedelta(hours=1)
            
            start_pht = start_dt.astimezone(PHT_TZ)
            end_pht   = end_dt.astimezone(PHT_TZ)
            
            busiest_hour_label = (
                f"{start_pht.strftime('%-I:%M %p')}–{end_pht.strftime('%-I:%M %p')} PHT"
            )
        except ValueError:
            # Fallback for Windows or systems where strftime %-I is not supported
            h_pht = (hour + 8) % 24
            start_hour_str = f"{h_pht % 12 or 12}:00 {'AM' if h_pht < 12 else 'PM'}"
            end_h = (h_pht + 1) % 24
            end_hour_str   = f"{end_h % 12 or 12}:00 {'AM' if end_h < 12 else 'PM'}"
            busiest_hour_label = f"{start_hour_str}–{end_hour_str} PHT"
    else:
        busiest_hour_label = None
        busiest_hour_count = 0

    return {
        "count": count,
        "total": total,
        "avg_amount": avg_amount,
        "avg_gap_minutes": avg_gap_minutes,
        "busiest_hour_label": busiest_hour_label,
        "busiest_hour_count": busiest_hour_count,
    }


# ── Message formatter ──────────────────────────────────────────────────────

def format_summary_message(
    stats: dict,
    period_start: datetime,
    period_end: datetime,
) -> str:
    """
    Build the Telegram summary message from computed stats.
    """
    # Period label in Philippines Time (PHT)
    PHT_TZ = timezone(timedelta(hours=8))
    start_pht = period_start.astimezone(PHT_TZ)
    end_pht   = period_end.astimezone(PHT_TZ)
    
    fmt = "%I:%M %p"
    period_label = (
        f"{start_pht.strftime(fmt).lstrip('0')} – "
        f"{end_pht.strftime(fmt).lstrip('0')} PHT"
    )

    if stats["count"] == 0:
        return (
            f"📊 <b>{SUMMARY_PERIOD_HOURS}-Hour Summary</b>\n"
            f"No charges recorded in this period.\n"
            f"Period: {period_label}"
        )

    # Average frequency line
    avg_gap = stats["avg_gap_minutes"]
    if avg_gap is None:
        freq_line = "Average frequency: only 1 charge (no gap to compute)"
    elif avg_gap < 60:
        freq_line = f"Average frequency: 1 charge every {avg_gap:.0f} min"
    else:
        freq_line = f"Average frequency: 1 charge every {avg_gap / 60:.1f} hr"

    # Busiest hour line
    if stats["busiest_hour_label"]:
        busiest_line = (
            f"Busiest hour: {stats['busiest_hour_label']} "
            f"({stats['busiest_hour_count']} charge"
            f"{'s' if stats['busiest_hour_count'] != 1 else ''})"
        )
    else:
        busiest_line = "Busiest hour: N/A"

    lines = [
        f"📊 <b>{SUMMARY_PERIOD_HOURS}-Hour Summary</b>",
        f"Charges: {stats['count']}",
        f"Total: ${stats['total']:,.2f}",
        f"Average amount: ${stats['avg_amount']:,.2f}/charge",
        freq_line,
        busiest_line,
        f"Period: {period_label}",
    ]
    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    now_utc      = datetime.now(timezone.utc)
    period_start = now_utc - timedelta(hours=SUMMARY_PERIOD_HOURS)

    logger.info(
        "Summary window: %s → %s",
        period_start.isoformat(),
        now_utc.isoformat(),
    )

    # Open DB (read-only; init_db() is safe to call even on an existing DB)
    conn = init_db()

    charges = get_charges_since(conn, period_start)
    conn.close()

    logger.info("Charges found in period: %d", len(charges))

    stats = compute_stats(charges, period_start, now_utc)

    logger.info(
        "Stats — count=%d  total=%.2f  avg_gap=%s min  busiest=%s (%d)",
        stats["count"],
        stats["total"],
        f"{stats['avg_gap_minutes']:.1f}" if stats["avg_gap_minutes"] is not None else "N/A",
        stats["busiest_hour_label"] or "N/A",
        stats["busiest_hour_count"],
    )

    message = format_summary_message(stats, period_start, now_utc)
    logger.info("Sending summary to Telegram.")

    success = send_message(message)
    if success:
        logger.info("Summary sent successfully.")
    else:
        logger.error("Failed to send summary — check Telegram credentials and logs above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
