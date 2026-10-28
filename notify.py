"""Telegram trade alerts. Silent no-op unless TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.

  python notify.py "hello"     sends a test message (used by the workflow's test switch)
"""
import json
import logging
import os
import sys
import urllib.request

log = logging.getLogger("bot")


def send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return False
    body = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:  # an alert failure must never stop trading logic
        log.warning("telegram alert failed: %s", type(e).__name__)
        return False


if __name__ == "__main__":
    ok = send(" ".join(sys.argv[1:]) or "Delta paper bot: test alert")
    print("sent" if ok else "not sent (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing or invalid)")
    sys.exit(0 if ok else 1)
