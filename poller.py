#!/usr/bin/env python3
"""
Ticket-booking watcher.

Polls a URL (a BookMyShow / District showtimes page, or an internal API
request you grabbed from your browser's DevTools) and sends a Telegram
message the moment a given theatre appears with booking open.

State is tracked in state.json so you get alerted on the *transition*
to "available" instead of on every run.

Everything is driven by config.json (and/or environment variables), so
nothing site-specific is hardcoded -- if BookMyShow/District change their
markup you only edit config, not code.
"""


#!/usr/bin/env python3

import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state.json"))

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
}


DISTRICT_HEADERS = {
    "User-Agent": DEFAULT_HEADERS["User-Agent"],
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "api_source": "district",
    "Referer": (
        "https://www.district.in/movies/"
        "avengers-doomsday-movie-tickets-in-chennai-MV176735"
    ),
    "x-app-type": "ed_mweb",
    "x-country-id": "1",
}


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def load_config():
    cfg = load_json(CONFIG_PATH, default={}) or {}

    env_map = {
        "TARGET_URL": "target_url",
        "THEATRE": "theatre",
        "MOVIE": "movie",
        "REQUESTED_DATE": "requested_date",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
    }

    for env_key, cfg_key in env_map.items():
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]

    if os.environ.get("HEADERS_JSON"):
        cfg["headers"] = json.loads(os.environ["HEADERS_JSON"])

    if cfg.get("url_template") and cfg.get("requested_date"):
        cfg["target_url"] = cfg["url_template"].format(
            date=cfg["requested_date"]
        )

    required = [
        "target_url",
        "telegram_bot_token",
        "telegram_chat_id",
        "requested_date",
    ]

    missing = [key for key in required if not cfg.get(key)]
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}")

    if not cfg.get("venue_codes") and not cfg.get("venue_code"):
        sys.exit("Missing venue_codes in config.json")

    return cfg


def send_telegram(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
if response.status_code != 200:
    print(f"[District] HTTP Status: {response.status_code}")
    print(f"[District] Response: {response.text[:1000]}")
    print(f"[District] Server: {response.headers.get('server')}")
    print(f"[District] Content-Type: {response.headers.get('content-type')}")
return None


# --------------------------------------------------
# BOOKMYSHOW
# --------------------------------------------------

def fetch_bms(cfg):
    headers = dict(DEFAULT_HEADERS)
    headers.update(cfg.get("headers", {}))

    scraper_key = os.environ.get("SCRAPERAPI_KEY")

    if scraper_key:
        api_url = "https://api.scraperapi.com/?" + urllib.parse.urlencode({
            "api_key": scraper_key,
            "country_code": "in",
            "url": cfg["target_url"],
        })

        response = requests.get(api_url, timeout=90)
        response.raise_for_status()
        return response.text

    proxy = os.environ.get("PROXY_URL")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    session = requests.Session()
    session.headers.update(headers)

    try:
        session.get(
            "https://in.bookmyshow.com/",
            timeout=30,
            proxies=proxies,
        )
    except requests.RequestException:
        pass

    response = session.get(
        cfg["target_url"],
        timeout=45,
        proxies=proxies,
        headers={
            "Referer": "https://in.bookmyshow.com/explore/movies-chennai"
        },
    )

    response.raise_for_status()
    return response.text


def is_available_venue_date(page_text, cfg):
    date = cfg["requested_date"]
    codes = cfg.get("venue_codes") or [cfg["venue_code"]]

    return any(
        f"/{code}/{date}" in page_text
        for code in codes
    )


def is_available_bms_date(page_text, cfg):
    requested = cfg["requested_date"]
    floor = cfg.get("min_references", 10)

    tokens = re.findall(r"20\d{6}", page_text)
    if not tokens:
        return False

    counts = Counter(tokens)
    top_date, _ = counts.most_common(1)[0]

    return (
        top_date == requested
        and counts.get(requested, 0) >= floor
    )


# --------------------------------------------------
# DISTRICT
# --------------------------------------------------

def fetch_district(cfg):
    headers = dict(DISTRICT_HEADERS)
    headers.update(cfg.get("district_headers", {}))

    response = requests.get(
        cfg["district_api_url"],
        headers=headers,
        timeout=45,
    )

    response.raise_for_status()
    return response.json()


def find_cinema_entries(data):
    """
    Find cinema objects recursively.

    Supports a list of cinemas or an API response
    containing cinemas inside nested data objects.
    """
    found = []

    def walk(item):
        if isinstance(item, list):
            for child in item:
                walk(child)

        elif isinstance(item, dict):
            if (
                isinstance(item.get("cinemaInfo"), dict)
                and isinstance(item.get("sessions"), list)
            ):
                found.append(item)
                return

            for value in item.values():
                if isinstance(value, (dict, list)):
                    walk(value)

    walk(data)
    return found


def get_available_seats(session):
    available = session.get("avail")

    if available is not None:
        try:
            return int(available)
        except (TypeError, ValueError):
            pass

    return sum(
        int(area.get("sAvail", 0) or 0)
        for area in session.get("areas", [])
    )



def detect_district_sessions(data, cfg):
    requested_date = cfg["requested_date"]
    alerts = []

    # Only monitor theatres listed in config.json.
    selected_theatres = [
        name.strip().lower()
        for name in cfg.get("district_theatres", [])
        if name.strip()
    ]

    if not selected_theatres:
        print("[District] No theatres configured.")
        return alerts

    cinemas = find_cinema_entries(data)

    for cinema in cinemas:
        cinema_info = cinema.get("cinemaInfo", {})
        cinema_name = cinema_info.get("name", "Unknown theatre")
        cinema_name_lower = cinema_name.lower()

        # Case-insensitive partial name matching.
        if not any(
            selected in cinema_name_lower
            for selected in selected_theatres
        ):
            continue

        for session in cinema.get("sessions", []):
            if session.get("showDate") != requested_date:
                continue

            if session.get("lang", "").strip().lower() != "english":
                continue

            available = get_available_seats(session)

            if available <= 0:
                continue

            alerts.append({
                "cinema": cinema_name,
                "time": session.get("showTimeText", "Unknown time"),
                "available": available,
                "session_id": session.get("sid", ""),
                "show_date": session.get("showDate", ""),
            })

    return alerts



# --------------------------------------------------
# MAIN MONITOR
# --------------------------------------------------

def main():
    cfg = load_config()

    state = load_json(
        STATE_PATH,
        default={
            "bms_available": False,
            "district_sessions": {},
        },
    ) or {}

    state.setdefault("district_sessions", {})

    movie = cfg.get("movie", "Avengers: Doomsday")
    date = cfg["requested_date"]
    pretty_date = f"{date[6:8]}-{date[4:6]}-{date[:4]}"

    # BOOKMYSHOW CHECK
    try:
        page = fetch_bms(cfg)

        detector = cfg.get("detector", "venue_date")

        if detector == "bms_date":
            bms_available = is_available_bms_date(page, cfg)
        else:
            bms_available = is_available_venue_date(page, cfg)

        previous = state.get("bms_available", False)

        print(
            f"[BookMyShow] available={bms_available} "
            f"(was {previous})"
        )

        if bms_available and not previous:
            message = (
                f"🎬 BookMyShow booking detected!\n\n"
                f"{movie}\n"
                f"Theatres: {cfg.get('venue_label', 'Configured theatres')}\n"
                f"Date: {pretty_date}\n\n"
                f"Book here: {cfg['target_url']}"
            )

            send_telegram(
                cfg["telegram_bot_token"],
                cfg["telegram_chat_id"],
                message,
            )
            print("[BookMyShow] Telegram alert sent")

        state["bms_available"] = bms_available

    except requests.RequestException as exc:
        print(f"[BookMyShow] Fetch failed: {exc}")

    # DISTRICT CHECK
    if cfg.get("district_api_url"):
        try:
            district_data = fetch_district(cfg)
            sessions = detect_district_sessions(district_data, cfg)

            print(
                f"[District] English sessions with seats available: "
                f"{len(sessions)}"
            )

            previous_sessions = state["district_sessions"]
            new_sessions = []

            for session in sessions:
                sid = session["session_id"]

                if not sid:
                    continue

                previous_available = int(
                    previous_sessions.get(sid, 0)
                )
                current_available = session["available"]

                print(
                    f"[District] {session['cinema']} | "
                    f"{session['time']} | "
                    f"seats={current_available} "
                    f"(was {previous_available})"
                )

                if current_available > previous_available:
                    new_sessions.append(session)

                previous_sessions[sid] = current_available

            if new_sessions:
                lines = [
                    "🎬 District booking availability detected!",
                    "",
                    movie,
                    f"Date: {pretty_date}",
                    "",
                ]

                for session in new_sessions:
                    lines.extend([
                        f"Theatre: {session['cinema']}",
                        f"Showtime: {session['time']}",
                        f"Available seats: {session['available']}",
                        "",
                    ])

                lines.append(
                    f"Book here: {cfg.get('district_booking_url', cfg['district_api_url'])}"
                )

                send_telegram(
                    cfg["telegram_bot_token"],
                    cfg["telegram_chat_id"],
                    "\n".join(lines),
                )

                print("[District] Telegram alert sent")

            state["district_sessions"] = previous_sessions

        except requests.RequestException as exc:
            print(f"[District] Fetch failed: {exc}")

        except (ValueError, TypeError) as exc:
            print(f"[District] Invalid API response: {exc}")

    state["checked_at"] = int(time.time())
    save_json(STATE_PATH, state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
