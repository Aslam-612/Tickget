
#!/usr/bin/env python3

"""
Tickget - Movie Ticket Availability Monitor

Monitors:
1. BookMyShow - configured venue codes
2. District - selected theatres from config.json

Filters:
- Movie: Avengers: Doomsday
- Date: 17 December 2026
- Language: English

Sends Telegram notifications when availability is detected.
"""

import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import requests


# --------------------------------------------------
# PATHS
# --------------------------------------------------

ROOT = Path(__file__).resolve().parent

CONFIG_PATH = Path(
    os.environ.get("CONFIG_PATH", ROOT / "config.json")
)

STATE_PATH = Path(
    os.environ.get("STATE_PATH", ROOT / "state.json")
)


# --------------------------------------------------
# HEADERS
# --------------------------------------------------

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


# --------------------------------------------------
# JSON HELPERS
# --------------------------------------------------

def load_json(path, default=None):
    if not path.exists():
        return default

    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

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
        cfg["headers"] = json.loads(
            os.environ["HEADERS_JSON"]
        )

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

    missing = [
        key for key in required
        if not cfg.get(key)
    ]

    if missing:
        sys.exit(
            f"Missing required config: {', '.join(missing)}"
        )

    if not cfg.get("venue_codes") and not cfg.get("venue_code"):
        sys.exit("Missing venue_codes in config.json")

    return cfg


# --------------------------------------------------
# TELEGRAM
# --------------------------------------------------

def send_telegram(token, chat_id, message):
    url = (
        f"https://api.telegram.org/bot{token}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )

    response.raise_for_status()


# --------------------------------------------------
# BOOKMYSHOW
# --------------------------------------------------

def fetch_bms(cfg):
    headers = dict(DEFAULT_HEADERS)
    headers.update(cfg.get("headers", {}))

    scraper_key = os.environ.get("SCRAPERAPI_KEY")

    if scraper_key:
        api_url = (
            "https://api.scraperapi.com/?"
            + urllib.parse.urlencode({
                "api_key": scraper_key,
                "country_code": "in",
                "url": cfg["target_url"],
            })
        )

        response = requests.get(
            api_url,
            timeout=90,
        )

        response.raise_for_status()
        return response.text

    proxy = os.environ.get("PROXY_URL")

    proxies = (
        {"http": proxy, "https": proxy}
        if proxy else None
    )

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
            "Referer": (
                "https://in.bookmyshow.com/"
                "explore/movies-chennai"
            )
        },
    )

    response.raise_for_status()

    return response.text


def is_available_venue_date(page_text, cfg):
    date = cfg["requested_date"]

    codes = (
        cfg.get("venue_codes")
        or [cfg["venue_code"]]
    )

    return any(
        f"/{code}/{date}" in page_text
        for code in codes
    )


def is_available_bms_date(page_text, cfg):
    requested = cfg["requested_date"]

    floor = cfg.get("min_references", 10)

    tokens = re.findall(
        r"20\d{6}",
        page_text,
    )

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

    headers.update(
        cfg.get("district_headers", {})
    )

    response = requests.get(
        cfg["district_api_url"],
        headers=headers,
        timeout=45,
    )

    # Print diagnostic information if District rejects the request.
    if response.status_code != 200:
        print(
            f"[District] HTTP Status: "
            f"{response.status_code}"
        )

        print(
            f"[District] Response: "
            f"{response.text[:1000]}"
        )

        print(
            f"[District] Server: "
            f"{response.headers.get('server')}"
        )

        print(
            f"[District] Content-Type: "
            f"{response.headers.get('content-type')}"
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

    # Read selected theatres from config.json.
    selected_theatres = [
        name.strip().casefold()
        for name in cfg.get("district_theatres", [])
        if isinstance(name, str) and name.strip()
    ]

    if not selected_theatres:
        print(
            "[District] No theatres configured."
        )
        return alerts

    cinemas = find_cinema_entries(data)

    print(
        f"[District] Cinemas returned by API: "
        f"{len(cinemas)}"
    )

    matched_cinemas = 0

    for cinema in cinemas:

        cinema_info = cinema.get("cinemaInfo", {})

        cinema_name = cinema_info.get(
            "name",
            "Unknown theatre"
        )

        cinema_name_lower = cinema_name.casefold()

        # Match only theatres configured by the user.
        if not any(
            selected in cinema_name_lower
            for selected in selected_theatres
        ):
            continue

        matched_cinemas += 1

        print(
            f"[District] Matched theatre: {cinema_name}"
        )

        for session in cinema.get("sessions", []):

            if session.get("showDate") != requested_date:
                continue

            if (
                session.get("lang", "").strip().casefold()
                != "english"
            ):
                continue

            available = get_available_seats(session)

            if available <= 0:
                continue

            alerts.append({
                "cinema": cinema_name,
                "time": session.get(
                    "showTimeText",
                    "Unknown time"
                ),
                "available": available,
                "session_id": session.get("sid", ""),
                "show_date": session.get("showDate", ""),
            })

    if matched_cinemas == 0:
        print(
            "[District] No configured theatres found "
            "in the API response."
        )

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

    state.setdefault(
        "district_sessions",
        {}
    )

    movie = cfg.get(
        "movie",
        "Avengers: Doomsday"
    )

    date = cfg["requested_date"]

    pretty_date = (
        f"{date[6:8]}-{date[4:6]}-{date[:4]}"
    )

    # --------------------------------------------------
    # BOOKMYSHOW CHECK
    # --------------------------------------------------

    try:

        page = fetch_bms(cfg)

        detector = cfg.get(
            "detector",
            "venue_date"
        )

        if detector == "bms_date":

            bms_available = is_available_bms_date(
                page,
                cfg
            )

        else:

            bms_available = is_available_venue_date(
                page,
                cfg
            )

        previous = state.get(
            "bms_available",
            False
        )

        print(
            f"[BookMyShow] available={bms_available} "
            f"(was {previous})"
        )

        if bms_available and not previous:

            message = (
                "🎬 BookMyShow booking detected!\n\n"
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

            print(
                "[BookMyShow] Telegram alert sent"
            )

        state["bms_available"] = bms_available

    except requests.RequestException as exc:

        print(
            f"[BookMyShow] Fetch failed: {exc}"
        )

    # --------------------------------------------------
    # DISTRICT CHECK
    # --------------------------------------------------

    if cfg.get("district_api_url"):

        try:

            district_data = fetch_district(cfg)

            sessions = detect_district_sessions(
                district_data,
                cfg
            )

            print(
                "[District] English sessions with "
                f"seats available: {len(sessions)}"
            )

            previous_sessions = state[
                "district_sessions"
            ]

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

                # Alert when seats first appear or increase.
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
                    "Book here: "
                    + cfg.get(
                        "district_booking_url",
                        cfg["district_api_url"]
                    )
                )

                send_telegram(
                    cfg["telegram_bot_token"],
                    cfg["telegram_chat_id"],
                    "\n".join(lines),
                )

                print(
                    "[District] Telegram alert sent"
                )

            state["district_sessions"] = previous_sessions

        except requests.RequestException as exc:

            print(
                f"[District] Fetch failed: {exc}"
            )

        except (ValueError, TypeError) as exc:

            print(
                f"[District] Invalid API response: {exc}"
            )

    # --------------------------------------------------
    # SAVE STATE
    # --------------------------------------------------

    state["checked_at"] = int(time.time())

    save_json(
        STATE_PATH,
        state
    )

    return 0


# --------------------------------------------------
# ENTRY POINT
# --------------------------------------------------

if __name__ == "__main__":
    raise SystemExit(main())
