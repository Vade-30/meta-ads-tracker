"""
rules.py — Detection rule logic for Meta Ads Gmail Monitor.

Rules
-----
1. FIXED AMOUNT RULE   : any charge > $900.00 is flagged immediately.
2a. OVERALL EWMA RULE  : current hour count > SPIKE_MULTIPLIER × EWMA baseline.
2b. PER-HOUR-OF-DAY    : current hour count > SPIKE_MULTIPLIER × same-hour avg.
   Cold-start guard    : if fewer than MIN_DAYS_FOR_HOUR_BASELINE days of
                         data exist for this specific hour, skip Rule 2b.

All thresholds are named constants at the top of this file — easy to tune.

Returns
-------
evaluate_rules() → list[str]
    A list of human-readable rule-trigger descriptions.
    An empty list means everything looks normal.
"""

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Tuneable constants ─────────────────────────────────────────────────────

AMOUNT_THRESHOLD: float = 900.00       # Rule 1: flag any charge above this
SPIKE_MULTIPLIER: float = 2.0           # Rules 2a/2b: flag if count > N× baseline
EWMA_ALPHA: float = 0.30               # EWMA smoothing factor (higher = more reactive)
EWMA_HISTORY_DAYS: int = 14            # How many days of history to use for EWMA
HOUR_BASELINE_HISTORY_DAYS: int = 14   # Days of same-hour history to average
MIN_DAYS_FOR_HOUR_BASELINE: int = 5    # Minimum distinct days needed for Rule 2b


# ── Helper: bucket charges into calendar hours ─────────────────────────────

def _charges_to_hourly_buckets(
    charges: list,
    reference_tz=timezone.utc,
) -> dict[tuple[int, int, int, int], int]:
    """
    Return a dict mapping (year, month, day, hour) → count.
    All timestamps are interpreted in UTC.
    """
    buckets: dict[tuple[int, int, int, int], int] = defaultdict(int)
    for row in charges:
        ts_str = row["timestamp"] if hasattr(row, "__getitem__") else row.timestamp
        try:
            ts = datetime.fromisoformat(ts_str).astimezone(timezone.utc)
        except (ValueError, TypeError):
            logger.warning("Unparseable timestamp in charge record: %s", ts_str)
            continue
        key = (ts.year, ts.month, ts.day, ts.hour)
        buckets[key] += 1
    return dict(buckets)


# ── Rule 1: Fixed amount ───────────────────────────────────────────────────

def rule_amount(amount: float) -> Optional[str]:
    """
    Return a trigger description if *amount* exceeds AMOUNT_THRESHOLD,
    otherwise None.
    """
    if amount > AMOUNT_THRESHOLD:
        description = (
            f"AMOUNT RULE: charge of ${amount:.2f} exceeds the "
            f"${AMOUNT_THRESHOLD:.2f} fixed threshold"
        )
        logger.warning("Rule 1 TRIGGERED: %s", description)
        return description
    logger.debug("Rule 1 OK: amount=%.2f <= threshold=%.2f", amount, AMOUNT_THRESHOLD)
    return None


# ── Rule 2a: Overall EWMA ─────────────────────────────────────────────────

def compute_ewma(
    all_charges: list,
    current_hour_key: tuple[int, int, int, int],
    saved_ewma: Optional[float],
) -> tuple[float, float]:
    """
    Compute an EWMA of hourly charge counts over the last EWMA_HISTORY_DAYS days,
    excluding the current (incomplete) hour.

    Returns
    -------
    (ewma_value, current_hour_count)
    """
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=EWMA_HISTORY_DAYS)

    buckets = _charges_to_hourly_buckets(all_charges)

    # Separate historical hours from the current hour
    historical_counts = []
    current_hour_count = 0

    for key, count in sorted(buckets.items()):
        year, month, day, hour = key
        try:
            ts = datetime(year, month, day, hour, tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if key == current_hour_key:
            current_hour_count = count
        else:
            historical_counts.append(count)

    if not historical_counts:
        if saved_ewma is not None:
            logger.info(
                "No historical hourly data in window; using saved EWMA=%.3f",
                saved_ewma,
            )
            return saved_ewma, current_hour_count
        logger.info("No historical data at all; EWMA defaults to 0.")
        return 0.0, current_hour_count

    # Build EWMA over sorted historical hourly counts
    ewma = float(historical_counts[0])
    for count in historical_counts[1:]:
        ewma = EWMA_ALPHA * count + (1 - EWMA_ALPHA) * ewma

    logger.info(
        "EWMA computed from %d historical hours: ewma=%.3f  current_hour=%d",
        len(historical_counts),
        ewma,
        current_hour_count,
    )
    return ewma, current_hour_count


def rule_overall_ewma(
    all_charges: list,
    current_hour_key: tuple[int, int, int, int],
    saved_ewma: Optional[float],
) -> tuple[Optional[str], float, float]:
    """
    Evaluate Rule 2a.

    Returns (trigger_description_or_None, new_ewma_value, current_hour_count).
    """
    ewma, current_hour_count = compute_ewma(all_charges, current_hour_key, saved_ewma)

    if ewma <= 0:
        logger.info("Rule 2a SKIPPED: EWMA baseline is 0 (insufficient history).")
        return None, ewma, current_hour_count

    threshold = SPIKE_MULTIPLIER * ewma
    logger.info(
        "Rule 2a: current_hour_count=%d  ewma=%.3f  threshold=%.3f",
        current_hour_count,
        ewma,
        threshold,
    )

    if current_hour_count > threshold:
        description = (
            f"OVERALL FREQUENCY RULE: {current_hour_count} charges this hour "
            f"exceeds {SPIKE_MULTIPLIER:.0f}× the rolling EWMA baseline "
            f"of {ewma:.1f} charges/hour (threshold: {threshold:.1f})"
        )
        logger.warning("Rule 2a TRIGGERED: %s", description)
        return description, ewma, current_hour_count

    logger.debug("Rule 2a OK: %d charges ≤ threshold %.3f", current_hour_count, threshold)
    return None, ewma, current_hour_count


# ── Rule 2b: Per-hour-of-day ───────────────────────────────────────────────

def rule_hour_of_day(
    all_charges: list,
    current_hour_key: tuple[int, int, int, int],
    current_hour_count: int,
) -> Optional[str]:
    """
    Evaluate Rule 2b.

    Collects historical charge counts for the SAME hour-of-day as the current
    hour (e.g., all 14:xx hours from the past HOUR_BASELINE_HISTORY_DAYS days),
    averages them, and flags if current_hour_count > SPIKE_MULTIPLIER × average.

    Falls back gracefully when there are fewer than MIN_DAYS_FOR_HOUR_BASELINE
    distinct days of data for this hour.
    """
    _, _, _, target_hour = current_hour_key
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=HOUR_BASELINE_HISTORY_DAYS)

    buckets = _charges_to_hourly_buckets(all_charges)

    # Collect counts for matching hour-of-day, excluding the current hour key
    same_hour_counts = []
    for key, count in buckets.items():
        year, month, day, hour = key
        if hour != target_hour:
            continue
        if key == current_hour_key:
            continue
        try:
            ts = datetime(year, month, day, hour, tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        same_hour_counts.append(count)

    distinct_days = len(same_hour_counts)
    logger.info(
        "Rule 2b: hour=%02d:xx  distinct historical days=%d  min_required=%d",
        target_hour,
        distinct_days,
        MIN_DAYS_FOR_HOUR_BASELINE,
    )

    if distinct_days < MIN_DAYS_FOR_HOUR_BASELINE:
        logger.info(
            "Rule 2b SKIPPED (cold start): only %d day(s) of data for hour %02d:xx "
            "(need %d). Using overall EWMA only.",
            distinct_days,
            target_hour,
            MIN_DAYS_FOR_HOUR_BASELINE,
        )
        return None

    hour_avg = sum(same_hour_counts) / len(same_hour_counts)
    threshold = SPIKE_MULTIPLIER * hour_avg

    logger.info(
        "Rule 2b: current_hour_count=%d  hour_avg=%.3f  threshold=%.3f",
        current_hour_count,
        hour_avg,
        threshold,
    )

    if current_hour_count > threshold:
        description = (
            f"PER-HOUR FREQUENCY RULE: {current_hour_count} charges at hour "
            f"{target_hour:02d}:xx exceeds {SPIKE_MULTIPLIER:.0f}× the "
            f"{target_hour:02d}:xx historical average of {hour_avg:.1f} "
            f"charges (threshold: {threshold:.1f}, based on {distinct_days} days)"
        )
        logger.warning("Rule 2b TRIGGERED: %s", description)
        return description

    logger.debug(
        "Rule 2b OK: %d charges ≤ threshold %.3f for hour %02d:xx",
        current_hour_count,
        threshold,
        target_hour,
    )
    return None


# ── Master evaluator ───────────────────────────────────────────────────────

def evaluate_rules(
    new_charge_amount: float,
    all_charges: list,
    saved_ewma: Optional[float],
    charge_timestamp: Optional[datetime] = None,
) -> tuple[list[str], float]:
    """
    Run all detection rules for a newly observed charge.

    Parameters
    ----------
    new_charge_amount : float
        The dollar amount of the charge being evaluated.
    all_charges : list
        All charge rows from the DB (including this new charge).
    saved_ewma : Optional[float]
        The previously persisted EWMA value (from state table), if any.
    charge_timestamp : Optional[datetime]
        The timestamp of the new charge; defaults to now (UTC).

    Returns
    -------
    (triggered_descriptions, updated_ewma)
        triggered_descriptions : list[str] — empty = no anomaly detected
        updated_ewma            : float    — persist this back to state table
    """
    triggered: list[str] = []

    if charge_timestamp is None:
        charge_timestamp = datetime.now(timezone.utc)

    ts_utc = charge_timestamp.astimezone(timezone.utc)
    current_hour_key = (ts_utc.year, ts_utc.month, ts_utc.day, ts_utc.hour)

    # Rule 1: Fixed amount
    r1 = rule_amount(new_charge_amount)
    if r1:
        triggered.append(r1)

    # Rule 2a: Overall EWMA
    r2a, updated_ewma, current_hour_count = rule_overall_ewma(
        all_charges, current_hour_key, saved_ewma
    )
    if r2a:
        triggered.append(r2a)

    # Rule 2b: Per-hour-of-day
    r2b = rule_hour_of_day(all_charges, current_hour_key, current_hour_count)
    if r2b:
        triggered.append(r2b)

    if not triggered:
        logger.info("All rules passed — no anomaly detected for this charge.")

    return triggered, updated_ewma
