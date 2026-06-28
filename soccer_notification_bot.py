#!/usr/bin/env python3
"""
Soccer Match Notification Bot for BigFan app.

Polls live matches every POLL_INTERVAL_SECONDS and sends push notifications for:
  - Match kick off
  - Every goal (with scorer name and minute)
  - Half time
  - Full time (with result)

State is persisted in Firestore so the bot can restart without double-sending.
In TEST_MODE, all notifications go only to the dev device (sankalpsingh6@gmail.com).
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, messaging, firestore
import urllib3

# Suppress connection pool warnings — harmless with multicast to 151 tokens
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging_urllib3 = __import__("logging").getLogger("urllib3")
logging_urllib3.setLevel(__import__("logging").ERROR)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# ── Config ────────────────────────────────────────────────────────────────────
FOOTBALL_API_KEY  = os.getenv("FOOTBALL_API_KEY", "")
FOOTBALL_API_BASE = "https://v3.football.api-sports.io"
POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Set TEST_MODE=false in .env / Render env vars to send to all users in production
TEST_MODE = os.getenv("TEST_MODE", "true").lower() != "false"

# Dev device — sankalpsingh6@gmail.com
DEV_USER_DOC_ID = "7LG8OW9hOYf6fBgpqONb4yEHW1k2"

# API-Football league IDs to monitor
# 1=World Cup, 15=Club World Cup, 29=Confederations Cup
# Add more as needed: https://www.api-football.com/documentation-v3#tag/Leagues
LEAGUE_IDS = list(map(int, os.getenv("LEAGUE_IDS", "1,15,29").split(",")))

FCM_BATCH_SIZE = 500

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Firebase ──────────────────────────────────────────────────────────────────
def _init_firebase():
    if not firebase_admin._apps:
        # On Render (or any server): set FIREBASE_CREDENTIALS_JSON env var to the
        # full contents of the service account JSON file.
        # Locally: set FIREBASE_CREDENTIALS_PATH to the file path.
        creds_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
        if creds_json:
            import json, tempfile
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
            tmp.write(creds_json)
            tmp.flush()
            cred = credentials.Certificate(tmp.name)
        else:
            creds_path = os.getenv(
                "FIREBASE_CREDENTIALS_PATH",
                "/Users/sankalpsingh/bigfan-8295c-firebase-adminsdk-fbsvc-1ce354fecb.json",
            )
            if not os.path.exists(creds_path):
                log.error("Firebase credentials not found: %s", creds_path)
                sys.exit(1)
            cred = credentials.Certificate(creds_path)
        firebase_admin.initialize_app(cred)
        log.info("Firebase initialised")

_init_firebase()
db = firestore.client()

# ── FCM sending ───────────────────────────────────────────────────────────────
def _get_dev_token() -> str | None:
    doc = db.collection("users").document(DEV_USER_DOC_ID).get()
    return (doc.to_dict() or {}).get("fcmToken") if doc.exists else None

def _get_all_tokens() -> list[str]:
    tokens = []
    for doc in db.collection("users").stream():
        t = (doc.to_dict() or {}).get("fcmToken")
        if t and isinstance(t, str):
            tokens.append(t)
    return tokens

def _build_message(title: str, body: str, data: dict, token: str) -> messaging.Message:
    return messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data={k: str(v) for k, v in data.items()},
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
        token=token,
    )

def send_notification(title: str, body: str, data: dict) -> bool:
    """
    In TEST_MODE: sends only to the dev device.
    In production: sends to all users via multicast.
    """
    if TEST_MODE:
        token = _get_dev_token()
        if not token:
            log.error("Dev token not found")
            return False
        try:
            resp = messaging.send(_build_message(title, body, data, token))
            log.info("[TEST] Sent to dev device: %s", resp)
            return True
        except Exception as exc:
            log.error("Send failed: %s", exc)
            return False
    else:
        tokens = _get_all_tokens()
        if not tokens:
            log.warning("No FCM tokens found")
            return False
        success = 0
        for i in range(0, len(tokens), FCM_BATCH_SIZE):
            batch = tokens[i:i + FCM_BATCH_SIZE]
            mm = messaging.MulticastMessage(
                notification=messaging.Notification(title=title, body=body),
                data={k: str(v) for k, v in data.items()},
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(
                        sound="default", channel_id="soccer_scores"
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
                r = messaging.send_each_for_multicast(mm)
                success += r.success_count
            except Exception as exc:
                log.error("Multicast error: %s", exc)
        log.info("Sent to %d/%d devices", success, len(tokens))
        return success > 0

# ── Firestore match state ─────────────────────────────────────────────────────
MATCH_STATE_COLLECTION = "soccer_match_state"

def _get_match_state(match_id: str) -> dict:
    doc = db.collection(MATCH_STATE_COLLECTION).document(match_id).get()
    return doc.to_dict() if doc.exists else {}

def _save_match_state(match_id: str, state: dict):
    db.collection(MATCH_STATE_COLLECTION).document(match_id).set(
        state, merge=True
    )

# ── Football API ──────────────────────────────────────────────────────────────
_HEADERS = {
    "x-rapidapi-host": "v3.football.api-sports.io",
    "x-rapidapi-key": FOOTBALL_API_KEY,
}

def get_live_matches() -> list[dict]:
    if not FOOTBALL_API_KEY or FOOTBALL_API_KEY == "your_api_football_key_here":
        log.error("FOOTBALL_API_KEY not configured in .env")
        return []

    seen_ids = set()
    matches = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for league_id in LEAGUE_IDS:
        # 1. Live matches
        try:
            r = requests.get(
                f"{FOOTBALL_API_BASE}/fixtures",
                headers=_HEADERS,
                params={"live": "all", "league": league_id},
                timeout=15,
            )
            r.raise_for_status()
            for m in r.json().get("response", []):
                fid = m.get("fixture", {}).get("id")
                if fid and fid not in seen_ids:
                    seen_ids.add(fid)
                    matches.append(m)
        except requests.RequestException as exc:
            log.error("API error live (league %d): %s", league_id, exc)

        # 2. Today's fixtures — catches matches that finished during the sleep window
        try:
            r = requests.get(
                f"{FOOTBALL_API_BASE}/fixtures",
                headers=_HEADERS,
                params={"date": today, "league": league_id, "season": 2026},
                timeout=15,
            )
            r.raise_for_status()
            for m in r.json().get("response", []):
                fid = m.get("fixture", {}).get("id")
                status = m.get("fixture", {}).get("status", {}).get("short", "")
                # Only include finished matches not already in the live list
                if fid and fid not in seen_ids and status in ("FT", "AET", "PEN"):
                    seen_ids.add(fid)
                    matches.append(m)
        except requests.RequestException as exc:
            log.error("API error today (league %d): %s", league_id, exc)

    if matches:
        log.info("League %d: %d matches to process", league_id, len(matches))
    return matches

def get_match_events(fixture_id: str) -> list[dict]:
    """Fetch goal events for a fixture (returns goals only)."""
    try:
        r = requests.get(
            f"{FOOTBALL_API_BASE}/fixtures/events",
            headers=_HEADERS,
            params={"fixture": fixture_id, "type": "Goal"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("response", [])
    except requests.RequestException as exc:
        log.error("Events API error (fixture %s): %s", fixture_id, exc)
        return []

# ── Match info helpers ────────────────────────────────────────────────────────
def _parse_match(match: dict) -> dict:
    fixture = match.get("fixture", {})
    teams   = match.get("teams", {})
    goals   = match.get("goals", {})
    league  = match.get("league", {})
    return {
        "match_id":   str(fixture.get("id", "")),
        "home_team":  teams.get("home", {}).get("name", "Home"),
        "away_team":  teams.get("away", {}).get("name", "Away"),
        "home_score": int(goals.get("home") or 0),
        "away_score": int(goals.get("away") or 0),
        "status":     fixture.get("status", {}).get("short", ""),
        "round":      league.get("round", ""),
    }

def _score_line(m: dict) -> str:
    return f"{m['home_team']} {m['home_score']} - {m['away_score']} {m['away_team']}"

_BANNED_WORDS = ["fifa", "world cup", "worldcup", "uefa", "concacaf", "conmebol"]

def _sanitize(text: str) -> str:
    """Strip any trademarked/banned terms from a string coming out of the API."""
    out = text
    for word in _BANNED_WORDS:
        import re
        out = re.sub(re.escape(word), "", out, flags=re.IGNORECASE).strip(" -–|")
    return out.strip() or "International"

def _round_label(round_str: str) -> str:
    """Convert API round string to a clean, trademark-free label."""
    r = round_str.lower()
    if "round of 32"  in r: return "Round of 32"
    if "round of 16"  in r: return "Round of 16"
    if "quarter"      in r: return "Quarter-final"
    if "semi"         in r: return "Semi-final"
    if "3rd"          in r: return "3rd Place"
    if "final"        in r: return "Final"
    if "group"        in r: return _sanitize(round_str)
    return _sanitize(round_str)

# ── Notification senders ──────────────────────────────────────────────────────
def notify_kickoff(m: dict) -> bool:
    round_label = _round_label(m["round"])
    body = f"{m['home_team']} vs {m['away_team']} | Kick Off! | {round_label}"
    log.info("KICKOFF: %s", body)
    sent = send_notification(
        title="⚽ Match Starting Now",
        body=body,
        data={**{k: str(v) for k, v in m.items()}, "event": "kickoff"},
    )
    if sent:
        _save_match_state(m["match_id"], {"notified_kickoff": True})
    return sent

def notify_goal(m: dict, scorer: str, minute: str, is_own_goal: bool = False) -> bool:
    score = _score_line(m)
    goal_label = "Own Goal" if is_own_goal else "GOAL"
    body = f"{score} | {goal_label}! {scorer} ({minute}')"
    log.info("GOAL: %s", body)
    return send_notification(
        title="⚽ Soccer Score Update",
        body=body,
        data={**{k: str(v) for k, v in m.items()}, "event": "goal", "scorer": scorer, "minute": minute},
    )

def notify_halftime(m: dict) -> bool:
    round_label = _round_label(m["round"])
    body = f"{_score_line(m)} | Half Time | {round_label}"
    log.info("HALFTIME: %s", body)
    sent = send_notification(
        title="⚽ Soccer Score Update",
        body=body,
        data={**{k: str(v) for k, v in m.items()}, "event": "halftime"},
    )
    if sent:
        _save_match_state(m["match_id"], {"notified_halftime": True})
    return sent

def notify_fulltime(m: dict) -> bool:
    round_label = _round_label(m["round"])
    h, a = m["home_score"], m["away_score"]
    if h > a:
        result = f"{m['home_team']} win!"
    elif a > h:
        result = f"{m['away_team']} win!"
    else:
        result = "Draw!"
    body = f"{_score_line(m)} | Full Time | {result} | {round_label}"
    log.info("FULLTIME: %s", body)
    sent = send_notification(
        title="⚽ Soccer Score Update",
        body=body,
        data={**{k: str(v) for k, v in m.items()}, "event": "fulltime", "result": result},
    )
    if sent:
        _save_match_state(m["match_id"], {"notified_fulltime": True})
    return sent

# ── Core match processor ──────────────────────────────────────────────────────
def process_match(match: dict):
    m     = _parse_match(match)
    mid   = m["match_id"]
    state = _get_match_state(mid)

    prev_status     = state.get("status", "NS")
    prev_home_score = int(state.get("home_score", 0))
    prev_away_score = int(state.get("away_score", 0))
    notified_goals  = set(state.get("notified_goal_ids", []))

    log.info(
        "  %s vs %s | %s | %d-%d",
        m["home_team"], m["away_team"], m["status"],
        m["home_score"], m["away_score"],
    )

    # ── Kick off ──────────────────────────────────────────────────────────────
    if m["status"] in ("1H", "2H") and not state.get("notified_kickoff"):
        notify_kickoff(m)

    # ── Goal detection ────────────────────────────────────────────────────────
    score_changed = (
        m["home_score"] != prev_home_score or
        m["away_score"] != prev_away_score
    )
    if score_changed and m["status"] in ("1H", "2H", "ET", "P"):
        events = get_match_events(mid)
        new_goal_ids = []
        for event in events:
            event_id = str(event.get("time", {}).get("elapsed", "")) + "_" + str(event.get("team", {}).get("id", ""))
            if event_id in notified_goals:
                continue
            player    = event.get("player", {}).get("name", "Unknown")
            minute    = str(event.get("time", {}).get("elapsed", "?"))
            detail    = event.get("detail", "").lower()
            is_own    = "own goal" in detail
            notify_goal(m, player, minute, is_own_goal=is_own)
            notified_goals.add(event_id)
            new_goal_ids.append(event_id)

        _save_match_state(mid, {
            "home_score": m["home_score"],
            "away_score": m["away_score"],
            "notified_goal_ids": list(notified_goals),
        })

    # ── Half time ─────────────────────────────────────────────────────────────
    if m["status"] == "HT" and not state.get("notified_halftime"):
        notify_halftime(m)

    # ── Full time ─────────────────────────────────────────────────────────────
    if m["status"] in ("FT", "AET", "PEN") and not state.get("notified_fulltime"):
        notify_fulltime(m)
        _save_match_state(mid, {"home_score": m["home_score"], "away_score": m["away_score"]})

    # Always persist latest status
    _save_match_state(mid, {"status": m["status"]})

# ── Main loop ─────────────────────────────────────────────────────────────────
def run():
    mode = "TEST (dev device only)" if TEST_MODE else "PRODUCTION (all users)"
    log.info("Soccer Notification Bot started | mode: %s | poll: %ds", mode, POLL_INTERVAL)

    while True:
        log.info("── Polling live matches ──")
        try:
            matches = get_live_matches()
            log.info("Live matches found: %d", len(matches))
            for match in matches:
                try:
                    process_match(match)
                except Exception as exc:
                    log.error("Error processing match: %s", exc, exc_info=True)
        except Exception as exc:
            log.error("Poll error: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
