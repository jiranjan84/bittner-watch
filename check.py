"""
Hlídač volných termínů u Dr. Bittnera (VSTUPNÍ vyšetření)
na https://andrologickaklinika.reenio.cz

Volá přímo Reenio JSON API – žádný headless browser.
Spouští se cronem v GitHub Actions. Nový volný slot → Telegram zpráva.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = "https://andrologickaklinika.reenio.cz"
DOCTOR_PATH = "dr-bittner-1166"
DOCTOR_NAME_KEYWORD = "Bittner"
SERVICE_KEYWORD = "VSTUPN"
LOOKAHEAD_DAYS = 90
STATE_PATH = Path("state.json")
PRAGUE = ZoneInfo("Europe/Prague")

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


def notify(msg: str) -> None:
    print(msg)
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "disable_web_page_preview": True},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"telegram err: {e}", file=sys.stderr)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def open_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "cs-CZ,cs;q=0.9",
    })
    s.get(f"{BASE}/cs/iframe", timeout=20).raise_for_status()
    today_local = datetime.now(PRAGUE).strftime("%Y-%m-%d")
    referer = f"{BASE}/cs/iframe/employee/{DOCTOR_PATH}/{today_local};viewMode=day"
    s.get(referer, timeout=20).raise_for_status()
    s.headers.update({
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
    })
    xsrf = s.cookies.get("XSRF-TOKEN")
    if xsrf:
        s.headers["X-XSRF-TOKEN"] = xsrf
    return s


def get_open_days(s: requests.Session) -> list[str]:
    now_local = datetime.now(PRAGUE).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = (now_local - timedelta(days=1)).astimezone(timezone.utc)
    end_utc = (now_local + timedelta(days=LOOKAHEAD_DAYS + 1)).astimezone(timezone.utc)
    r = s.post(
        f"{BASE}/cs/api/Term/DaysWithTerms",
        files={
            "start": (None, start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")),
            "end": (None, end_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")),
            "onlyAvailable": (None, "false"),
        },
        timeout=20,
    )
    r.raise_for_status()
    days_dict = r.json().get("data", {}).get("data", {}) or {}
    out: set[str] = set()
    for key, info in days_dict.items():
        if not info.get("isOpen"):
            continue
        utc_dt = datetime.strptime(key, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        out.add(utc_dt.astimezone(PRAGUE).date().isoformat())
    return sorted(out)


def get_bittner_vstupni_slots(s: requests.Session, ymd: str) -> list[str]:
    r = s.post(
        f"{BASE}/cs/api/Term/List",
        files={
            "date": (None, ymd),
            "viewMode": (None, "day"),
            "page": (None, "0"),
            "includeColors": (None, "false"),
            "findNearestAvailable": (None, "false"),
        },
        timeout=20,
    )
    r.raise_for_status()
    events = r.json().get("data", {}).get("events", []) or []
    out: set[str] = set()
    for ev in events:
        resources = ev.get("eventResources") or []
        if not any(
            DOCTOR_NAME_KEYWORD in (er.get("name") or "")
            and SERVICE_KEYWORD in (er.get("name") or "").upper()
            for er in resources
        ):
            continue
        interval = ev.get("reservationIntervalSize") or 0
        if interval <= 0:
            continue
        try:
            ev_start = datetime.strptime(ev["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            ev_end = datetime.strptime(ev["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        booked = {res["start"] for res in (ev.get("reservations") or []) if "start" in res}
        cursor = ev_start
        step = timedelta(minutes=interval)
        while cursor + step <= ev_end:
            iso = cursor.strftime("%Y-%m-%dT%H:%M:%SZ")
            if iso not in booked:
                out.add(cursor.astimezone(PRAGUE).strftime("%H:%M"))
            cursor += step
    return sorted(out)


def collect_available() -> dict[str, list[str]]:
    s = open_session()
    days = get_open_days(s)
    print(f"otevřené dny v rozsahu {LOOKAHEAD_DAYS} dní: {len(days)}")
    current: dict[str, list[str]] = {}
    for d in days:
        slots = get_bittner_vstupni_slots(s, d)
        if slots:
            current[d] = slots
            print(f"  {d}: {', '.join(slots)}")
    total = sum(len(v) for v in current.values())
    print(f"Bittner VSTUPNÍ volných slotů: {total} ({len(current)} dnů)")
    return current


def main() -> int:
    print(f"[{datetime.now(PRAGUE).isoformat(timespec='seconds')}] start")
    try:
        current = collect_available()
    except Exception as e:
        notify(f"⚠️ Bittner watcher chyba: {e}")
        raise

    prev = load_state()
    new_slots: dict[str, list[str]] = {}
    for day, times in current.items():
        diff = sorted(set(times) - set(prev.get(day, [])))
        if diff:
            new_slots[day] = diff

    if new_slots:
        lines = ["🟢 NOVÝ volný termín – Dr. Bittner VSTUPNÍ vyšetření:\n"]
        for day, times in sorted(new_slots.items()):
            lines.append(f"• {day}: {', '.join(times)}")
        lines.append("\nObjednej: https://www.andrologickaklinika.cz/objednejte-se.html")
        notify("\n".join(lines))
    else:
        print("Beze změny.")

    save_state(current)
    return 0


if __name__ == "__main__":
    sys.exit(main())
