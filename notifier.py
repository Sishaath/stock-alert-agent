"""
notifier.py — Sends push notifications to ntfy.sh.

ntfy.sh is a free, open-source push notification service.
Subscribe to your topic in the ntfy app (iOS/Android) or at https://ntfy.sh
"""

import logging
import requests
from config import NTFY_URL

logger = logging.getLogger(__name__)

# Every alert uses this title — visible as the notification header on your phone
ALERT_TITLE = "DASHBOARD ALERT"


def send_alert(message: str, priority: str = "high") -> bool:
    """
    Send a push notification via ntfy.sh.

    Args:
        message:  The body of the notification (can be multi-line).
        priority: Notification priority. Options:
                  "low"     — delivered silently
                  "default" — normal notification sound
                  "high"    — high-priority sound (default for most alerts)
                  "urgent"  — max priority, bypasses Do Not Disturb

    Returns:
        True if the notification was delivered, False otherwise.
    """
    try:
        response = requests.post(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers={
                "Title":        ALERT_TITLE,
                "Priority":     priority,
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
        response.raise_for_status()
        logger.info(f"Notification sent successfully: {message[:80].strip()}...")
        return True

    except requests.exceptions.Timeout:
        logger.error("Notification timed out — ntfy.sh did not respond within 10s.")
        return False
    except requests.exceptions.ConnectionError:
        logger.error("Could not connect to ntfy.sh — check your internet connection.")
        return False
    except requests.exceptions.HTTPError as e:
        logger.error(f"ntfy.sh returned an error: {e}")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Unexpected error sending notification: {e}")
        return False
