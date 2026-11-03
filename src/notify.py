"""Push notifications to ntfy.

Deliberately best-effort: a notification failure must never take down a scrape
that has been running for hours, so every send swallows its exceptions and the
timeout is short. Nothing here is on the hot path -- send only on start, finish,
rate-limit stalls and fatal errors, not per video.
"""

import json
import os
import urllib.request

# Priorities ntfy understands, lowest to highest: min, low, default, high, urgent.
_TIMEOUT = 5


def _topic_url(config=None):
    """ntfy topic URL from the environment or config; None disables notifications."""
    url = os.environ.get("NTFY_URL")
    if url:
        return url.strip()
    if config:
        return (config.get("notifications") or {}).get("ntfy_url")
    return None


def notify(config, title, message, priority="default", tags=None):
    """Post one notification. Returns True if it was accepted."""
    url = _topic_url(config)
    if not url:
        return False

    headers = {
        "Title": title,
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if tags:
        headers["Tags"] = ",".join(tags)

    try:
        req = urllib.request.Request(
            url, data=message.encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def notify_error(config, title, message):
    return notify(config, title, message, priority="high", tags=["rotating_light"])


def notify_done(config, title, message):
    return notify(config, title, message, priority="default", tags=["white_check_mark"])
