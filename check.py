"""
Hlídač volných termínů u Dr. Bittnera (VSTUPNÍ vyšetření)
na https://andrologickaklinika.reenio.cz/cs/iframe

Spouští se cronem. Pokud se objeví NOVÝ volný slot, pošle Telegram zprávu.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

IFRAME_URL = "https://andrologickaklinika.reenio.cz/cs/iframe"
DOCTOR_TAB_TEXT = "Dr. Bittner"
SECTION_TITLE_RE = re.compile(r"Dr\.\s*Bittner\s+VSTUPN[ÍI]", re.IGNORECASE)

LOOKAHEAD_DAYS = 90
STATE_PATH = Path("state.json")

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


def notify(text: str) -> None:
    print(text)
    if not (TG_TOKEN and TG_CHAT):
        print("(Telegram není nastaven – přeskakuji odeslání)", file=sys.stderr)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"Telegram chyba: {e}", file=sys.stderr)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def scrape_available_slots() -> dict[str, list[str]]:
    """Vrátí dict {YYYY-MM-DD: ["13:20", "13:40", ...]} pouze pro VOLNÉ VSTUPNÍ sloty Bittner."""
    result: dict[str, list[str]] = {}
    cutoff = (datetime.now() + timedelta(days=LOOKAHEAD_DAYS)).date()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="cs-CZ", timezone_id="Europe/Prague")
        page = ctx.new_page()
        page.set_default_timeout(20000)

        page.goto(IFRAME_URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")

        # Klikni na záložku "Dr. Bittner"
        try:
            page.get_by_text(DOCTOR_TAB_TEXT, exact=False).first.click()
            page.wait_for_load_state("networkidle")
        except PlaywrightTimeout:
            print("Nepodařilo se najít záložku Dr. Bittner", file=sys.stderr)
            browser.close()
            return result

        # Reenio kalendář: dny S DOSTUPNOSTÍ jsou typicky <a> s class containing 'avail' nebo data-attr.
        # Použijeme robustní strategii: vezmeme všechny "kliknutelné" buňky dnů (mají odkaz/role button)
        # a postupně je proklikneme.
        seen_days: set[str] = set()

        for _ in range(60):  # bezpečnostní limit iterací
            # Najdi všechny dny v kalendáři, které vypadají kliknutelné (mají href nebo tabindex)
            day_locators = page.locator(
                "css=[class*='calendar'] a[href], [class*='calendar'] [role='button']:not([disabled])"
            )
            count = day_locators.count()
            picked = None
            picked_iso = None

            for i in range(count):
                el = day_locators.nth(i)
                try:
                    txt = (el.inner_text(timeout=1000) or "").strip()
                except PlaywrightTimeout:
                    continue
                if not txt.isdigit():
                    continue
                # Zkus zjistit datum z aria-label nebo title
                iso = None
                for attr in ("aria-label", "title", "data-date"):
                    v = el.get_attribute(attr)
                    if not v:
                        continue
                    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", v)
                    if m:
                        iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                        break
                    # české "15. května 2026"
                    m2 = re.search(r"(\d{1,2})\.\s*(\w+)\s*(\d{4})", v)
                    if m2:
                        mon = {
                            "ledna": 1, "února": 2, "března": 3, "dubna": 4, "května": 5,
                            "června": 6, "července": 7, "srpna": 8, "září": 9, "října": 10,
                            "listopadu": 11, "prosince": 12,
                        }.get(m2.group(2).lower())
                        if mon:
                            iso = f"{int(m2.group(3)):04d}-{mon:02d}-{int(m2.group(1)):02d}"
                            break
                if not iso or iso in seen_days:
                    continue
                if datetime.fromisoformat(iso).date() > cutoff:
                    continue
                picked = el
                picked_iso = iso
                break

            if not picked:
                # Žádný další neviděný den v aktuálním view – zkus prokliknout vpřed
                next_btn = page.locator("button[aria-label*='další'], button[aria-label*='Next'], .next-month, [class*='next']").first
                if next_btn.count() and next_btn.is_visible():
                    try:
                        next_btn.click()
                        page.wait_for_load_state("networkidle")
                        continue
                    except Exception:
                        break
                break

            seen_days.add(picked_iso)
            try:
                picked.click()
                page.wait_for_load_state("networkidle")
            except Exception:
                continue

            # Najdi sekci "Dr. Bittner VSTUPNÍ vyšetření" a vytáhni z ní časy slotů,
            # které NEJSOU označené jako obsazené.
            slots = []
            sections = page.locator("xpath=//*[self::section or self::div or self::article][.//*[contains(translate(text(),'íÍ','iI'),'VSTUPNI') or contains(text(),'VSTUPN')]]")
            sec_count = sections.count()
            for s in range(sec_count):
                sec = sections.nth(s)
                try:
                    header = sec.inner_text(timeout=1000)
                except PlaywrightTimeout:
                    continue
                if not SECTION_TITLE_RE.search(header):
                    continue
                # Sloty: hledáme prvky s textem HH:MM, které nejsou disabled / "obsazený"
                slot_elements = sec.locator("xpath=.//*[self::a or self::button or self::div][string-length(normalize-space(text()))<=5]")
                sc = slot_elements.count()
                for k in range(sc):
                    se = slot_elements.nth(k)
                    try:
                        t = (se.inner_text(timeout=500) or "").strip()
                    except PlaywrightTimeout:
                        continue
                    if not re.fullmatch(r"\d{1,2}:\d{2}", t):
                        continue
                    cls = (se.get_attribute("class") or "").lower()
                    disabled = se.get_attribute("disabled") is not None
                    aria_disabled = (se.get_attribute("aria-disabled") or "").lower() == "true"
                    # Filtrujeme obsazené sloty
                    if disabled or aria_disabled:
                        continue
                    if any(x in cls for x in ("disabled", "obsazen", "occupied", "taken", "unavailable")):
                        continue
                    slots.append(t)
            if slots:
                result[picked_iso] = sorted(set(slots))

        browser.close()
    return result


def main() -> int:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] start scrape")
    try:
        current = scrape_available_slots()
    except Exception as e:
        notify(f"⚠️ Bittner watcher chyba: {e}")
        raise

    prev = load_state()
    new_slots: dict[str, list[str]] = {}
    for day, times in current.items():
        old = set(prev.get(day, []))
        diff = sorted(set(times) - old)
        if diff:
            new_slots[day] = diff

    if new_slots:
        lines = ["🟢 NOVÝ volný termín – Dr. Bittner VSTUPNÍ vyšetření:\n"]
        for day, times in sorted(new_slots.items()):
            lines.append(f"• {day}: {', '.join(times)}")
        lines.append("\nRezervuj: https://www.andrologickaklinika.cz/objednejte-se.html")
        notify("\n".join(lines))
    else:
        print(f"Beze změny. Aktuálně {sum(len(v) for v in current.values())} volných slotů.")

    save_state(current)
    return 0


if __name__ == "__main__":
    sys.exit(main())
