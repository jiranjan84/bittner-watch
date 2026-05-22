# Bittner Watch

Každou hodinu kontroluje volné termíny **VSTUPNÍHO vyšetření u Dr. Bittnera**
na <https://www.andrologickaklinika.cz/objednejte-se.html> a pošle Telegram
zprávu, jakmile se objeví nový volný slot.

## Jak to běží

`check.py` (Python + Playwright headless Chromium) otevře Reenio booking widget,
proklikne dny v kalendáři, posbírá volné časy ve VSTUPNÍ sekci a porovná je se
`state.json`. Nové sloty → Telegram.

GitHub Actions cron spouští skript každou celou hodinu zdarma.

## Setup (3 kroky, ~5 min)

### 1) Telegram bot

1. V Telegramu najdi `@BotFather` → `/newbot` → dostaneš token
   `1234567890:ABCdef...`
2. Najdi svého bota podle jména, dej **Start**, napiš `ahoj`
3. Otevři v prohlížeči `https://api.telegram.org/bot<TOKEN>/getUpdates` a
   najdi `"chat":{"id": …}` — to je tvoje `chat_id`

### 2) GitHub repo

```bash
cd bittner-watch
git init -b main
git add .
git commit -m "init"
gh repo create bittner-watch --private --source=. --push
```

(nebo přes web github.com → New repo → upload files)

### 3) Secrets

V repu: **Settings → Secrets and variables → Actions → New repository secret**

- `TELEGRAM_TOKEN` = token z BotFather
- `TELEGRAM_CHAT_ID` = chat ID z kroku 1

### 4) První spuštění

Tab **Actions → Bittner Watch → Run workflow**. Pokud chyba — viz Troubleshooting.

## Co když selektor přestane fungovat

Reenio může změnit HTML. V `check.py` jsou robustní fallbacky, ale pokud kalendář
změní třídy, uprav:

- `day_locators` – kliknutelné dny v kalendáři
- `sections` xpath – sekce s nadpisem „VSTUPNÍ"
- detekce „obsazený slot" – list klíčových slov v `cls`

Otevři Actions log, koukni co skript vidí, a uprav selektory.

## Lokální test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
TELEGRAM_TOKEN=xxx TELEGRAM_CHAT_ID=yyy python check.py
```

## Změna nastavení

- **Frekvence**: `.github/workflows/check.yml` → `cron: "0 * * * *"` (např.
  `"*/30 * * * *"` = každých 30 min)
- **Rozsah dní**: `check.py` → `LOOKAHEAD_DAYS = 90`
- **Jiný doktor**: `DOCTOR_TAB_TEXT` a `SECTION_TITLE_RE`
