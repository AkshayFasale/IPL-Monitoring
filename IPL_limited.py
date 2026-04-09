#!/usr/bin/env python3
"""
District.in — Test Monitor (5 matches before RCB on Apr 27)
Monitors button text changes for these specific matches:
  1. Apr 18 — SRH vs CSK
  2. Apr 19 — PBKS vs LSG
  3. Apr 21 — SRH vs DC
  4. Apr 25 — DC vs PBKS
  5. Apr 25 — RR vs SRH

Logic: Stores the button text ("Coming soon") on first run.
       If button text changes to ANYTHING else → instant Telegram alert.
       This catches: timer/countdown appearing, "Sale is live", or any other state.
"""

import os
import time
import json
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────
#  CONFIGURATION — Set these in Railway Variables
#  (never hardcode tokens in code!)
# ─────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL_SECONDS = 30 * 60   # 30 minutes normally
STATE_FILE = "test_monitor_state.json"
TARGET_URL = "https://www.district.in/events/ipl-ticket-booking"

# ─────────────────────────────────────────────
#  THE 5 TEST MATCHES — identified by "vs" string
#  Script will ONLY monitor these, ignore everything else
# ─────────────────────────────────────────────
WATCH_MATCHES = [
    {"teams": "Sunrisers Hyderabad vs Chennai Super Kings", "date": "18 Apr", "venue": "Rajiv Gandhi International Cricket Stadium, Hyderabad"},
    {"teams": "Punjab Kings vs Lucknow Super Giants",       "date": "19 Apr", "venue": "Maharaja Yadavindra Singh Cricket Stadium, Chandigarh"},
    {"teams": "Sunrisers Hyderabad vs Delhi Capitals",      "date": "21 Apr", "venue": "Rajiv Gandhi International Cricket Stadium, Hyderabad"},
    {"teams": "Delhi Capitals vs Punjab Kings",             "date": "25 Apr", "venue": "Arun Jaitley Stadium, Delhi"},
    {"teams": "Rajasthan Royals vs Sunrisers Hyderabad",    "date": "25 Apr", "venue": "Sawai Mansingh Stadium, Jaipur"},
]

def is_watch_match(teams_str: str) -> bool:
    """Check if this match is in our watch list."""
    for w in WATCH_MATCHES:
        # Match both directions e.g. "SRH vs CSK" or "CSK vs SRH"
        parts_page  = [p.strip().lower() for p in teams_str.split(" vs ")]
        parts_watch = [p.strip().lower() for p in w["teams"].split(" vs ")]
        if set(parts_page) == set(parts_watch):
            return True
    return False

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("test_monitor.log"),
    ],
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.district.in/",
}

# ─────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────
def load_state() -> dict:
    if Path(STATE_FILE).exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ─────────────────────────────────────────────
#  FETCH
# ─────────────────────────────────────────────
def fetch_page() -> str | None:
    try:
        resp = requests.get(TARGET_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        log.warning(f"Fetch failed: {e}")
        return None

# ─────────────────────────────────────────────
#  PARSE — extract button text per match
# ─────────────────────────────────────────────
def parse_matches(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    matches = []

    # Look for all status indicators on the page
    status_keywords = ["Sale is live", "Coming soon", "Opens in", "Sale starts",
                       "Opening", "Countdown", "Available", "Notify", "Book tickets"]

    for tag in soup.find_all(string=lambda t: t and (
        "Sale is live" in t or "Coming soon" in t
    )):
        card = tag.find_parent()

        # Walk up to find the match card (must contain "vs")
        for _ in range(10):
            if card is None:
                break
            card_text = card.get_text(separator=" ", strip=True)
            if " vs " in card_text and any(
                m in card_text
                for m in ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
            ):
                break
            card = card.find_parent()

        if card is None:
            continue

        card_text = card.get_text(separator=" | ", strip=True)
        lines = [l.strip() for l in card_text.split("|") if l.strip()]

        # Extract fields
        teams_str = venue_str = date_str = time_str = ""
        for line in lines:
            if " vs " in line and not teams_str:
                teams_str = line
            elif any(v in line for v in [
                "Stadium","Ground","Cricket","Oval","Eden","Wankhede","Chepauk",
                "Narendra","Rajiv","Sawai","Arun","Ekana","Himachal","Maharaja",
                "Barsapara","Mohali","International"
            ]) and not venue_str:
                venue_str = line
            elif ("AM" in line or "PM" in line) and not time_str:
                time_str = line
            elif any(m in line for m in [
                "Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"
            ]) and not date_str:
                date_str = line

        if not teams_str:
            continue

        # ── Capture the exact button text ──────────────
        # This is the key: we store whatever text appears on the CTA button.
        # "Coming soon"      → not on sale yet
        # Anything else      → something changed! Alert immediately.
        # "Sale is live"     → tickets open
        # "Opens in 08:30:00"→ countdown timer appeared (JS might not render this
        #                       but if District ever puts it in static HTML, we catch it)
        if "Sale is live" in card_text:
            btn_text = "Sale is live"
        else:
            # Default to "Coming soon", but check if any other status text exists
            btn_text = "Coming soon"
            for line in lines:
                line_lower = line.lower()
                if line_lower == "coming soon":
                    btn_text = line
                    break
                elif any(kw in line_lower for kw in [
                    "opens in", "sale starts", "opening soon", "countdown",
                    "notify me", "register", "available soon", "hours", "minutes"
                ]):
                    btn_text = line  # Something new appeared!
                    break

        # Booking URL
        book_link = card.find("a", string=lambda t: t and "Book" in t)
        booking_url = TARGET_URL
        if book_link and book_link.get("href"):
            href = book_link["href"]
            booking_url = href if href.startswith("http") else f"https://www.district.in{href}"

        match_id = teams_str.lower().replace(" ", "_").replace("/", "_")

        matches.append({
            "id":       match_id,
            "teams":    teams_str,
            "date":     date_str,
            "venue":    venue_str,
            "time":     time_str,
            "btn_text": btn_text,
            "url":      booking_url,
        })

    # Deduplicate
    seen, unique = set(), []
    for m in matches:
        if m["id"] not in seen:
            seen.add(m["id"])
            unique.append(m)
    return unique

# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=10)
        resp.raise_for_status()
        log.info("✅ Telegram alert sent")
        return True
    except Exception as e:
        log.error(f"Telegram failed: {e}")
        return False

# ─────────────────────────────────────────────
#  ALERTS
# ─────────────────────────────────────────────
def alert_changed(match: dict, prev_text: str, new_text: str) -> None:
    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    is_live = "sale is live" in new_text.lower()

    if is_live:
        header = "🎟️ <b>TICKETS ARE LIVE!</b>"
        footer = "⚡ <b>Book immediately — IPL tickets sell out in minutes!</b>"
    else:
        header = "⏳ <b>BUTTON CHANGED — Countdown may have started!</b>"
        footer = (
            "👀 Sale could go live very soon.\n"
            "🔁 Monitor is now checking more frequently!"
        )

    msg = (
        f"{header}\n\n"
        f"🏏 <b>{match['teams']}</b>\n"
        f"📅 <b>Date:</b> {match['date']}\n"
        f"🏟️ <b>Venue:</b> {match['venue']}\n"
        f"⏰ <b>Match Time:</b> {match['time']}\n\n"
        f"🔄 <b>Button changed:</b>\n"
        f"   Before: <i>{prev_text}</i>\n"
        f"   After:  <b>{new_text}</b>\n\n"
        f"🔗 {match['url']}\n"
        f"🕐 <i>Detected at {now_str}</i>\n\n"
        f"{footer}"
    )
    send_telegram(msg)

# ─────────────────────────────────────────────
#  MAIN CHECK
# ─────────────────────────────────────────────
def check_once(state: dict) -> tuple[dict, bool]:
    """
    Returns (updated_state, countdown_detected).
    countdown_detected=True means we should speed up the check interval.
    """
    log.info("Fetching District.in page...")
    html = fetch_page()
    if not html:
        log.warning("Fetch failed, skipping cycle.")
        return state, False

    matches   = parse_matches(html)
    countdown = False

    log.info(f"Parsed {len(matches)} total matches. Filtering for watch list...")

    for match in matches:
        if not is_watch_match(match["teams"]):
            continue

        mid      = match["id"]
        btn_text = match["btn_text"]
        prev     = state.get(mid)

        if prev is None:
            # First time seeing this match — store baseline
            state[mid] = btn_text
            log.info(f"[INIT] {match['teams']} → \"{btn_text}\"")
            continue

        if prev == btn_text:
            log.info(f"[NO CHANGE] {match['teams']} → \"{btn_text}\"")
            continue

        # ── Button text changed! ──────────────────────
        log.info(f"[CHANGED] {match['teams']} → was \"{prev}\" now \"{btn_text}\"")
        state[mid] = btn_text
        alert_changed(match, prev, btn_text)

        # If it's not live yet, something intermediate appeared → speed up checks
        if "sale is live" not in btn_text.lower():
            countdown = True

    return state, countdown


def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first!")
        return

    state = load_state()
    log.info("🚀 Test Monitor started for 5 pre-RCB matches!")

    match_list = "\n".join(
        f"📅 {w['date']} — {w['teams']}" for w in WATCH_MATCHES
    )
    send_telegram(
        "🧪 <b>District.in Test Monitor is LIVE!</b>\n\n"
        "Watching <b>5 matches</b> before RCB on Apr 27:\n\n"
        f"{match_list}\n\n"
        f"⏱ Checking every <b>{CHECK_INTERVAL_SECONDS // 60} minutes</b>\n\n"
        "You'll get an alert the moment any button text changes from "
        "<b>\"Coming soon\"</b> to anything else 🔔\n\n"
        "<i>This is a dry run to validate the monitoring logic before RCB matches.</i>"
    )

    interval = CHECK_INTERVAL_SECONDS

    while True:
        log.info(f"\n{'─'*50}")
        log.info(f"⏰ Check at {datetime.now().strftime('%d %b %Y, %I:%M:%S %p')}")

        state, countdown_detected = check_once(state)
        save_state(state)

        # Auto speed-up: if a countdown was detected, check every 5 mins instead
        if countdown_detected:
            interval = 5 * 60
            log.info("⚡ Countdown detected — switching to 5-minute checks!")
            send_telegram(
                "⚡ <b>Check interval reduced to 5 minutes!</b>\n"
                "A countdown timer was detected on one of the matches.\n"
                "Monitoring more aggressively until sale goes live. 🎟️"
            )
        else:
            interval = CHECK_INTERVAL_SECONDS  # reset to 30 min if no countdown

        log.info(f"💤 Next check in {interval // 60} minutes...")
        time.sleep(interval)


if __name__ == "__main__":
    main()