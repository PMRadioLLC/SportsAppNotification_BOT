#!/usr/bin/env python3
"""
FIFA Match Notification Bot for BigFan app.

Polls live FIFA matches every POLL_INTERVAL_SECONDS seconds and sends
Firebase push notifications to all BigFan users at halftime and fulltime.

Delivery: fetches FCM tokens from Firestore `users` collection (field: fcmToken)
          and sends via multicast (max 500 tokens per batch).
State:    tracks sent notifications in Firestore `fifa_notification_log` collection
          so restarts never double-send.
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, messaging, firestore

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
FIREBASE_CREDENTIALS_PATH = os.getenv(
    "FIREBASE_CREDENTIALS_PATH",
    "/Users/sankalpsingh/bigfan-8295c-firebase-adminsdk-fbsvc-1ce354fecb.json",
)
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "")
FOOTBALL_API_BASE = "https://v3.football.api-sports.io"
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# API-Football league IDs for FIFA competitions
# 1  = FIFA World Cup
# 15 = FIFA Club World Cup
# 29 = FIFA Confederations Cup
FIFA_LEAGUE_IDS = [1, 15, 29]

# FCM multicast limit
FCM_BATCH_SIZE = 500

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Firebase init ─────────────────────────────────────────────────────────────
def _init_firebase():
    if not firebase_admin._apps:
        if not os.path.exists(FIREBASE_CREDENTIALS_PATH):
            log.error("Firebase credentials not found: %s", FIREBASE_CREDENTIALS_PATH)
            sys.exit(1)
        cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
        firebase_admin.initialize_app(cred)
        log.info("Firebase initialised (project: bigfan-8295c)")

_init_firebase()
db = firestore.client()

# ── Firestore notification log ────────────────────────────────────────────────
LOG_COLLECTION = "fifa_notification_log"

def _already_sent(match_id: str, event: str) -> bool:
    """Return True if we already sent this notification."""
    doc_id = f"{match_id}_{event}"
    doc = db.collection(LOG_COLLECTION).document(doc_id).get()
    return doc.exists

def _mark_sent(match_id: str, event: str, home: str, away: str, score: str):
    """Write a record to Firestore so we never send this notification twice."""
    doc_id = f"{match_id}_{event}"
    db.collection(LOG_COLLECTION).document(doc_id).set({
        "match_id": match_id,
        "event": event,
        "home_team": home,
        "away_team": away,
        "score": score,
        "sent_at": firestore.SERVER_TIMESTAMP,
    })

# ── FCM token fetching ────────────────────────────────────────────────────────
def get_all_fcm_tokens() -> list[str]:
    """Fetch all valid FCM tokens from the Firestore `users` collection."""
    tokens = []
    try:
        for doc in db.collection("users").stream():
            data = doc.to_dict() or {}
            token = data.get("fcmToken") or data.get("fcm_token")
            if token and isinstance(token, str):
                tokens.append(token)
    except Exception as exc:
        log.error("Failed to fetch FCM tokens: %s", exc)
    log.info("Fetched %d FCM tokens from Firestore", len(tokens))
    return tokens

# ── Notification sending ──────────────────────────────────────────────────────
def send_push_notification(title: str, body: str, data: dict) -> bool:
    """
    Send a push notification to all BigFan users.
    Uses multicast (token-based) in batches of 500.
    """
    tokens = get_all_fcm_tokens()
    if not tokens:
        log.warning("No FCM tokens found — notification not sent")
        return False

    # Ensure all data values are strings (FCM requirement)
    str_data = {k: str(v) for k, v in data.items()}

    success_total = 0
    failure_total = 0

    for i in range(0, len(tokens), FCM_BATCH_SIZE):
        batch = tokens[i : i + FCM_BATCH_SIZE]
        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            data=str_data,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    sound="default",
                    channel_id="soccer_scores",
                ),
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound="default", badge=1)
                )
            ),
            tokens=batch,
        )
        try:
            resp = messaging.send_each_for_multicast(message)
            success_total += resp.success_count
            failure_total += resp.failure_count
            if resp.failure_count:
                for idx, r in enumerate(resp.responses):
                    if not r.success:
                        log.debug("Token failed (%s): %s", batch[idx][:20], r.exception)
        except Exception as exc:
            log.error("Multicast batch error: %s", exc)
            failure_total += len(batch)

    log.info(
        "Notification sent — success: %d, failed: %d", success_total, failure_total
    )
    return success_total > 0

# ── Football API ──────────────────────────────────────────────────────────────
_API_HEADERS = {
    "x-rapidapi-host": "v3.football.api-sports.io",
    "x-rapidapi-key": FOOTBALL_API_KEY,
}

def get_live_fifa_matches() -> list[dict]:
    """Return all live fixtures across FIFA league IDs."""
    if not FOOTBALL_API_KEY:
        log.error("FOOTBALL_API_KEY not set in .env")
        return []

    matches = []
    for league_id in FIFA_LEAGUE_IDS:
        try:
            resp = requests.get(
                f"{FOOTBALL_API_BASE}/fixtures",
                headers=_API_HEADERS,
                params={"live": "all", "league": league_id},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            league_matches = data.get("response", [])
            if league_matches:
                log.info("League %d: %d live matches", league_id, len(league_matches))
            matches.extend(league_matches)
        except requests.RequestException as exc:
            log.error("API error for league %d: %s", league_id, exc)
    return matches

# ── Notification builders ─────────────────────────────────────────────────────
def _extract_match_info(match: dict) -> dict:
    fixture = match.get("fixture", {})
    teams   = match.get("teams", {})
    goals   = match.get("goals", {})
    return {
        "match_id":   str(fixture.get("id", "")),
        "home_team":  teams.get("home", {}).get("name", "Home"),
        "away_team":  teams.get("away", {}).get("name", "Away"),
        "home_score": str(goals.get("home") or 0),
        "away_score": str(goals.get("away") or 0),
        "status":     fixture.get("status", {}).get("short", ""),
    }

def send_halftime_notification(match: dict) -> bool:
    m = _extract_match_info(match)
    score_str = f"{m['home_team']} {m['home_score']} - {m['away_score']} {m['away_team']}"

    if _already_sent(m["match_id"], "halftime"):
        log.info("HT notification already sent for match %s", m["match_id"])
        return False

    log.info("Sending halftime notification: %s", score_str)
    sent = send_push_notification(
        title="⚽ Soccer Score Update",
        body=f"{score_str} | Half Time",
        data={**m, "event": "halftime"},
    )
    if sent:
        _mark_sent(m["match_id"], "halftime", m["home_team"], m["away_team"], score_str)
    return sent

def send_fulltime_notification(match: dict) -> bool:
    m = _extract_match_info(match)
    home_s = int(m["home_score"])
    away_s = int(m["away_score"])

    if home_s > away_s:
        result = f"{m['home_team']} wins!"
    elif away_s > home_s:
        result = f"{m['away_team']} wins!"
    else:
        result = "It's a draw!"

    score_str = f"{m['home_team']} {home_s} - {away_s} {m['away_team']}"
    body = f"{score_str} | Full Time | {result}"

    if _already_sent(m["match_id"], "fulltime"):
        log.info("FT notification already sent for match %s", m["match_id"])
        return False

    log.info("Sending fulltime notification: %s", body)
    sent = send_push_notification(
        title="⚽ Soccer Score Update",
        body=body,
        data={**m, "event": "fulltime", "result": result},
    )
    if sent:
        _mark_sent(m["match_id"], "fulltime", m["home_team"], m["away_team"], score_str)
    return sent

# ── Main loop ─────────────────────────────────────────────────────────────────
def check_and_notify():
    log.info("── Polling FIFA matches ──")
    matches = get_live_fifa_matches()
    log.info("Total live FIFA matches: %d", len(matches))

    for match in matches:
        info = _extract_match_info(match)
        status = info["status"]
        label = f"{info['home_team']} vs {info['away_team']}"
        log.info("  %s — status: %s", label, status)

        if status == "HT":
            send_halftime_notification(match)
        elif status in ("FT", "AET", "PEN"):
            send_fulltime_notification(match)

def run():
    log.info("FIFA Notification Bot started (polling every %ds)", POLL_INTERVAL)
    while True:
        try:
            check_and_notify()
        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
