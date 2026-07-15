"""
reset_last_msg_id.py — One-time script to clear the last_message_id state
so the script re-processes all emails from the last 7 days on the next run.
"""

import logging
from db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("reset")

def reset():
    conn = init_db()
    conn.execute("DELETE FROM state WHERE key = 'last_message_id'")
    conn.commit()
    logger.info("Cleared last_message_id from state table. Script will re-process all emails from the last 7 days on next run.")
    conn.close()

if __name__ == "__main__":
    reset()
