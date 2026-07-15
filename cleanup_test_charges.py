"""
cleanup_test_charges.py — One-time script to delete test charges from the database.

Deletes all charges recorded on 2026-07-15 (the test date) since those were
injected by the test sender email and are not real Meta Ads charges.
"""

import logging
from db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("cleanup")

TEST_DATE = "2026-07-15"  # Date of test charges to remove

def cleanup():
    conn = init_db()  # init_db() opens the DB and returns the connection

    # Find test charges first
    rows = conn.execute(
        "SELECT id, timestamp, amount, merchant FROM charges WHERE timestamp LIKE ?",
        (f"{TEST_DATE}%",),
    ).fetchall()

    if not rows:
        logger.info("No test charges found for %s. Nothing to delete.", TEST_DATE)
        conn.close()
        return

    logger.info("Found %d test charge(s) to delete:", len(rows))
    for row in rows:
        logger.info("  id=%s  timestamp=%s  amount=%s  merchant=%s", *row)

    conn.execute(
        "DELETE FROM charges WHERE timestamp LIKE ?",
        (f"{TEST_DATE}%",),
    )
    conn.commit()
    logger.info("Deleted %d test charge(s) from the database.", len(rows))
    conn.close()

if __name__ == "__main__":
    cleanup()
