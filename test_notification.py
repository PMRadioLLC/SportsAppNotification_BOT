#!/usr/bin/env python3
"""
Test script for the Soccer notification bot.
ALL sends go ONLY to the dev device (sankalpsingh6@gmail.com).

Usage:
    python test_notification.py firebase     # Firebase connection check
    python test_notification.py tokens       # FCM token count (no send)
    python test_notification.py kickoff      # mock kick-off notification
    python test_notification.py goal         # mock goal notification
    python test_notification.py halftime     # mock half-time notification
    python test_notification.py fulltime     # mock full-time notification
    python test_notification.py sequence     # full match sequence (all 4 events)
    python test_notification.py api          # Football API connection check
    python test_notification.py live         # print live matches (no send)
    python test_notification.py all          # run all tests
"""

import sys
import time
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

import firebase_admin
from firebase_admin import credentials, messaging, firestore

# ── Dev device ────────────────────────────────────────────────────────────────
DEV_USER_EMAIL  = "sankalpsingh6@gmail.com"
DEV_USER_DOC_ID = "7LG8OW9hOYf6fBgpqONb4yEHW1k2"

if not firebase_admin._apps:
    cred = credentials.Certificate(
        "/Users/sankalpsingh/bigfan-8295c-firebase-adminsdk-fbsvc-1ce354fecb.json"
    )
    firebase_admin.initialize_app(cred)

db = firestore.client()

import soccer_notification_bot as bot

# Force test mode regardless of .env
bot.TEST_MODE = True


# ── Dev-only send ─────────────────────────────────────────────────────────────
def _dev_token() -> str:
    doc = db.collection("users").document(DEV_USER_DOC_ID).get()
    return (doc.to_dict() or {}).get("fcmToken", "")

def _send(title: str, body: str, data: dict):
    token = _dev_token()
    if not token:
        log.error("Dev token not found")
        return
    msg = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data={k: str(v) for k, v in data.items()},
        apns=messaging.APNSConfig(
            payload=messaging.APNSPayload(aps=messaging.Aps(sound="default", badge=1))
        ),
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(sound="default", channel_id="soccer_scores"),
        ),
        token=token,
    )
    resp = messaging.send(msg)
    log.info("Sent → %s | %s", title, body)
    log.info("FCM response: %s", resp)


# ── Tests ─────────────────────────────────────────────────────────────────────
def test_firebase():
    log.info("=== Firebase connection ===")
    app = firebase_admin.get_app()
    log.info("App: %s | OK", app.name)


def test_tokens():
    log.info("=== FCM token count (no send) ===")
    tokens = bot._get_all_tokens()
    log.info("Total FCM tokens in DB: %d", len(tokens))
    token = _dev_token()
    log.info("Dev user (%s) token: %s...", DEV_USER_EMAIL, token[:40])


def test_kickoff():
    log.info("=== Kick-off notification → dev only ===")
    _send(
        title="⚽ Match Starting Now",
        body="Canada vs South Africa | Kick Off! | Round of 32",
        data={
            "type": "soccer_kickoff",
            "match_id": "TEST_KO_001",
            "home_team": "Canada",
            "away_team": "South Africa",
            "home_score": "0",
            "away_score": "0",
            "event": "kickoff",
            "round": "Round of 32",
        },
    )


def test_goal():
    log.info("=== Goal notification → dev only ===")
    _send(
        title="⚽ Soccer Score Update",
        body="Canada 1 - 0 South Africa | GOAL! Alphonso Davies (23')",
        data={
            "type": "soccer_goal",
            "match_id": "TEST_GOAL_001",
            "home_team": "Canada",
            "away_team": "South Africa",
            "home_score": "1",
            "away_score": "0",
            "event": "goal",
            "scorer": "Alphonso Davies",
            "minute": "23",
        },
    )


def test_halftime():
    log.info("=== Half-time notification → dev only ===")
    _send(
        title="⚽ Soccer Score Update",
        body="Canada 1 - 0 South Africa | Half Time | Round of 32",
        data={
            "type": "soccer_halftime",
            "match_id": "TEST_HT_001",
            "home_team": "Canada",
            "away_team": "South Africa",
            "home_score": "1",
            "away_score": "0",
            "event": "halftime",
            "round": "Round of 32",
        },
    )


def test_fulltime():
    log.info("=== Full-time notification → dev only ===")
    _send(
        title="⚽ Soccer Score Update",
        body="Canada 2 - 0 South Africa | Full Time | Canada win! | Round of 32",
        data={
            "type": "soccer_fulltime",
            "match_id": "TEST_FT_001",
            "home_team": "Canada",
            "away_team": "South Africa",
            "home_score": "2",
            "away_score": "0",
            "event": "fulltime",
            "result": "Canada win!",
            "round": "Round of 32",
        },
    )


def test_sequence():
    """Send the full match event sequence with 3-second gaps."""
    log.info("=== Full match sequence → dev only ===")
    log.info("Kick off...")
    test_kickoff();   time.sleep(3)
    log.info("Goal!")
    test_goal();      time.sleep(3)
    log.info("Half time...")
    test_halftime();  time.sleep(3)
    log.info("Second-half goal...")
    _send(
        title="⚽ Soccer Score Update",
        body="Canada 2 - 0 South Africa | GOAL! Jonathan David (67')",
        data={
            "type": "soccer_goal", "match_id": "TEST_GOAL_002",
            "home_team": "Canada", "away_team": "South Africa",
            "home_score": "2", "away_score": "0",
            "event": "goal", "scorer": "Jonathan David", "minute": "67",
        },
    )
    time.sleep(3)
    log.info("Full time...")
    test_fulltime()


def test_api():
    log.info("=== Football API connection (no send) ===")
    key = os.getenv("FOOTBALL_API_KEY", "")
    if not key or key == "your_api_football_key_here":
        log.warning("FOOTBALL_API_KEY not set in .env — skipping")
        return
    resp = __import__("requests").get(
        f"{bot.FOOTBALL_API_BASE}/status",
        headers=bot._HEADERS,
        timeout=10,
    )
    data = resp.json().get("response", {})
    log.info("Account: %s", data.get("account", {}).get("email", "?"))
    quota = data.get("requests", {})
    log.info("Requests today: %s / %s", quota.get("current"), quota.get("limit_day"))


def test_live():
    log.info("=== Live matches (no send) ===")
    key = os.getenv("FOOTBALL_API_KEY", "")
    if not key or key == "your_api_football_key_here":
        log.warning("FOOTBALL_API_KEY not set in .env — skipping")
        return
    matches = bot.get_live_matches()
    if not matches:
        log.info("No live matches right now")
        return
    for m in matches:
        info = bot._parse_match(m)
        log.info(
            "  [%s] %s %d-%d %s | %s",
            info["status"], info["home_team"], info["home_score"],
            info["away_score"], info["away_team"], info["round"],
        )


TESTS = {
    "firebase": test_firebase,
    "tokens":   test_tokens,
    "kickoff":  test_kickoff,
    "goal":     test_goal,
    "halftime": test_halftime,
    "fulltime": test_fulltime,
    "sequence": test_sequence,
    "api":      test_api,
    "live":     test_live,
}

if __name__ == "__main__":
    targets = sys.argv[1:] or ["all"]
    if "all" in targets:
        targets = list(TESTS.keys())
    for name in targets:
        fn = TESTS.get(name)
        if fn:
            fn()
            print()
        else:
            log.error("Unknown test: %s  (choices: %s)", name, ", ".join(TESTS))
