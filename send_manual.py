#!/usr/bin/env python3
"""
Manual notification sender for BigFan app.
Use this to send match notifications to all devices without API polling.

Usage:
    python send_manual.py goal      "Germany" "Paraguay" 0 1 "J. Enciso" 42 "Round of 32"
    python send_manual.py kickoff   "Germany" "Paraguay" "Round of 32"
    python send_manual.py halftime  "Germany" "Paraguay" 0 1 "Round of 32"
    python send_manual.py fulltime  "Germany" "Paraguay" 0 2 "Round of 32"
"""

import sys
import firebase_admin
from firebase_admin import credentials, messaging, firestore

if not firebase_admin._apps:
    cred = credentials.Certificate(
        "/Users/sankalpsingh/bigfan-8295c-firebase-adminsdk-fbsvc-1ce354fecb.json"
    )
    firebase_admin.initialize_app(cred)

db = firestore.client()

def get_all_tokens():
    tokens = []
    for doc in db.collection("users").stream():
        t = (doc.to_dict() or {}).get("fcmToken")
        if t and isinstance(t, str):
            tokens.append(t)
    return tokens

def send_to_all(title, body, data):
    tokens = get_all_tokens()
    print(f"Sending to {len(tokens)} devices...")
    mm = messaging.MulticastMessage(
        notification=messaging.Notification(title=title, body=body),
        data={k: str(v) for k, v in data.items()},
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(sound="default", channel_id="soccer_scores"),
        ),
        apns=messaging.APNSConfig(
            payload=messaging.APNSPayload(aps=messaging.Aps(sound="default", badge=1))
        ),
        tokens=tokens,
    )
    r = messaging.send_each_for_multicast(mm)
    print(f"Done — success: {r.success_count}, failed: {r.failure_count}")

def usage():
    print(__doc__)
    sys.exit(1)

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        usage()

    event = args[0].lower()

    try:
        if event == "kickoff":
            # kickoff <home> <away> <round>
            home, away, rnd = args[1], args[2], args[3]
            send_to_all(
                title="⚽ Match Starting Now",
                body=f"{home} vs {away} | Kick Off! | {rnd}",
                data={"event": "kickoff", "home_team": home, "away_team": away, "round": rnd},
            )

        elif event == "goal":
            # goal <home> <away> <home_score> <away_score> <scorer> <minute> <round>
            home, away = args[1], args[2]
            home_score, away_score = args[3], args[4]
            scorer, minute, rnd = args[5], args[6], args[7]
            send_to_all(
                title="⚽ Soccer Score Update",
                body=f"{home} {home_score} - {away_score} {away} | GOAL! {scorer} ({minute}')",
                data={
                    "event": "goal", "home_team": home, "away_team": away,
                    "home_score": home_score, "away_score": away_score,
                    "scorer": scorer, "minute": minute, "round": rnd,
                },
            )

        elif event == "halftime":
            # halftime <home> <away> <home_score> <away_score> <round>
            home, away = args[1], args[2]
            home_score, away_score = args[3], args[4]
            rnd = args[5]
            send_to_all(
                title="⚽ Soccer Score Update",
                body=f"{home} {home_score} - {away_score} {away} | Half Time | {rnd}",
                data={
                    "event": "halftime", "home_team": home, "away_team": away,
                    "home_score": home_score, "away_score": away_score, "round": rnd,
                },
            )

        elif event == "fulltime":
            # fulltime <home> <away> <home_score> <away_score> <round>
            home, away = args[1], args[2]
            home_score, away_score = int(args[3]), int(args[4])
            rnd = args[5]
            if home_score > away_score:
                result = f"{home} win!"
            elif away_score > home_score:
                result = f"{away} win!"
            else:
                result = "Draw!"
            send_to_all(
                title="⚽ Soccer Score Update",
                body=f"{home} {home_score} - {away_score} {away} | Full Time | {result} | {rnd}",
                data={
                    "event": "fulltime", "home_team": home, "away_team": away,
                    "home_score": str(home_score), "away_score": str(away_score),
                    "result": result, "round": rnd,
                },
            )

        else:
            print(f"Unknown event: {event}")
            usage()

    except IndexError:
        print("Missing arguments.")
        usage()
