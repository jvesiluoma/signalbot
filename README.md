# Signal Bot — Group Surveillance Demo

A Flask application that observes Signal Messenger groups via a bridged
[signal-cli REST API](https://github.com/bbernhard/signal-cli-rest-api) and
silently archives every message, attachment, reaction, shared link, and
membership event into MySQL — independently of whatever retention policy the
source group is using.

## ⚠️ Demonstration / awareness purpose only

This project exists to make a privacy point that should be obvious but rarely is:

> The "disappearing messages" or "1-week retention" toggle in any chat app only
> controls what *that app* shows you. **Any participant — or any device linked
> to a participant's account — can quietly mirror every message into a private
> store the moment it arrives.** Once a group has more than two members, you
> have to assume someone is keeping a copy.

Run this against **your own** groups to see exactly how much state can be
reconstructed from a single linked-device session: who said what to whom, when
they were online, what links they shared, what those pages looked like at the
time, and which topics keep recurring.

**Do not run this against groups you have not been authorized to observe.** The
purpose is to evaluate your own threat model — then have an honest conversation
with your group about which signals you're comfortable being collectible.

## Features

- **Continuous polling** — every 10 seconds, fetches all new messages from
  configured Signal groups via signal-cli REST API.
- **Persistent archive** — messages, attachments, reactions, mentions, quotes,
  remote-deletes, group-membership changes — all retained indefinitely in
  MySQL, ignoring source-side retention.
- **Page archiving** — Playwright captures a full screenshot **and** HTML
  snapshot of every shared URL; tracked URLs can be re-checked on a schedule
  and diffed over time.
- **AI analysis** — local [Ollama](https://ollama.ai) LLM analyzes shared URLs
  inline (per-message) and generates 24-hour group thread summaries (batch).
- **Intel dashboard** — entity extraction (NER), keyword watchlists, reaction
  burst detection, per-sender behavioral profiles, group rosters and
  membership-event history, automatic intel briefs.
- **Activity probing (opt-in)** — measures whether a target's device is online
  by sending a transient reaction and timing the delivery receipt — without
  the target receiving a visible notification.
- **Identity resolution** — mirrors signal-cli's recipient cache so UUID-only
  Signal accounts (no shared phone) still resolve to display names.
- **Full-text search** — MySQL FULLTEXT indexes across messages, AI analysis,
  and archived page bodies.
- **Web dashboard** — Flask UI on port 5581 with statistics, search, filtered
  views, daily/monthly/yearly summaries, attachments browser, page diff, a
  large-font live message stream, and the intel console.
- **Settings page** (`/settings`) — point-and-click setup for the Signal /
  Telegram / WhatsApp integrations: link Signal by scanning a QR, pair WhatsApp,
  paste a Telegram bot token, fetch the group/chat list and tick which ones to
  monitor, and toggle general behaviour (e.g. whether the bot's *own* outgoing
  messages are archived — on by default). Settings are stored in the
  `app_settings` table and override the matching `.env` values; see
  [`docs/SETTINGS_PAGE_DESIGN.md`](docs/SETTINGS_PAGE_DESIGN.md).

## Prerequisites

- **Docker + Docker Compose v2.20+** — the recommended way to run the whole stack
- An [Ollama](https://ollama.ai) instance with the models pulled (external by default; an `ollama` compose profile is provided if you want it bundled)
- *(only for a non-Docker install)* Python 3.10+, MySQL 8.0+, [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api), Playwright with Chromium

## Quick start (Docker Compose)

`docker-compose.yml` brings up the app plus, as **optional** sidecars, MySQL, the
`signal-cli-rest-api` daemon, and the Telegram & WhatsApp connectors. Every
bundled dependency can be skipped (drop its name from `COMPOSE_PROFILES`) and
replaced with an already-running external instance — just point the matching
`*_BASE` / `DB_HOST` / `OLLAMA_API_URL` in `.env` at it.

The `signalbot` service image is built from the included [`Dockerfile`](Dockerfile)
(based on `mcr.microsoft.com/playwright/python`, so Chromium and its system
libraries are already present for the screenshot/HTML-snapshot pipeline); it just
installs `requirements.txt` and copies the source. `docker compose up --build`
builds it — plus the `connectors/telegram/` and `connectors/whatsapp/` images for
whichever of those profiles are enabled.

```bash
git clone <repo-url> && cd signalbot
cp .env.example .env          # then edit: set DB_PASSWORD, DB_ROOT_PASSWORD,
                              # SIGNAL_PHONE_NUMBER, OLLAMA_API_URL, TZ, secrets, …
docker compose up --build -d  # builds the app image (and any enabled connector
                              # images) and starts everything in COMPOSE_PROFILES
```

Rebuild after pulling code changes with `docker compose up --build -d` again
(or `docker compose build signalbot` to rebuild just the app image).

`COMPOSE_PROFILES` (set in `.env`, default `db,signal`) is the master switch for
the bundled services:

| Profile | Bundled service | Skip it by… |
|---|---|---|
| `db` | MySQL (`mysql`) | removing `db`, setting `DB_HOST=<your-mysql-host>` |
| `signal` | `signal-cli-rest-api` (`signal-api`) | removing `signal`, setting `SIGNAL_API_BASE=http://<host>:8080` |
| `telegram` | Telegram Bot-API connector (`tg-connector`, `connectors/telegram/`) | removing `telegram` (the app also ignores it unless `TELEGRAM_ENABLED=1`) |
| `whatsapp` | WhatsApp / Baileys connector (`wa-connector`, `connectors/whatsapp/`) | removing `whatsapp` (the app also ignores it unless `WHATSAPP_ENABLED=1`) |
| `ollama` | Ollama (`ollama`) — **off by default** | leave it off and point `OLLAMA_API_URL` at an external Ollama |
| `telegram-local-api` | official local Telegram Bot API server — off by default | leave it off (uses `api.telegram.org`) |

The dashboard is then on `http://localhost:5581` (override with `PORT`). The
default `COMPOSE_PROFILES` is `db,signal`; add `telegram` / `whatsapp` to also
build and run those connectors, and set `TELEGRAM_ENABLED=1` / `WHATSAPP_ENABLED=1`
(plus the `TG_*` / `WA_*` settings) so the app actually polls/ingests them.

### Linking Signal

With the `signal` profile running, link the device once:

```bash
curl -s 'http://localhost:8080/v1/qrcodelink?device_name=signal-api' --output qr.png
```

Open `qr.png`, scan it from **Signal → Settings → Linked Devices → Link New
Device**, then set `SIGNAL_PHONE_NUMBER`, `TARGET_GROUP_IDS`, and
`SIGNAL_CLI_DB_LOCAL_PATH` in `.env` and `docker compose restart signalbot`. Find
`<ACCOUNT_ID>` for `SIGNAL_CLI_DB_LOCAL_PATH` with
`docker compose exec signal-api ls /home/.local/share/signal-cli/data/`.

> **Using an *external* signal-api** (not the bundled `signal` profile)? The app
> can't `docker cp` from it, but it can read its data dir if you bind-mount it:
> set `SIGNAL_CLI_DATA_DIR=/path/to/that/container/data/dir` in `.env` (e.g.
> `/root/.local/share/signal-api`) — that gets mounted read-only at
> `/signal-cli-data` — then point `SIGNAL_CLI_DB_LOCAL_PATH` at
> `/signal-cli-data/data/<ACCOUNT_ID>.d/account.db`. See `.env.example`.

### Telegram

The `tg-connector` container (`connectors/telegram/`) uses the **Bot API only**
(chosen for stealth) — an innocuous, read-only bot. To enable:

1. Create a bot with [@BotFather](https://t.me/BotFather); copy the token.
2. **Disable privacy mode**: BotFather → `/setprivacy` → *Disable* (so the bot
   sees all group messages). For **channels**, add the bot as an **admin**.
3. Add the bot to your target groups/channels.
4. In `.env`: `COMPOSE_PROFILES=db,signal,telegram`, `TELEGRAM_ENABLED=1`,
   `TG_BOT_TOKEN=…`, `TG_TARGET_CHAT_IDS=-100123…,…` (empty = all chats the bot is in).
5. `docker compose up -d --build`. Telegram messages now appear in all the
   message views and the Intelligence Center with a **TG** badge.

*Bot-API limitations (by design of "Bot API only"): the bot is a visible group
member; it can't read history from before it joined; it can't see other users'
DMs; and Bot API exposes no delivery/read receipts or presence for other users,
so the device-activity probe is a no-op on Telegram.*

### WhatsApp

The `wa-connector` container (`connectors/whatsapp/`) is a small custom service
on [Baileys](https://github.com/WhiskeySockets/Baileys) (the WhatsApp Web
multi-device protocol — no headless browser, no paid tooling). The linked-device
session lives only in the `wa-session` volume. To enable:

1. In `.env`: `COMPOSE_PROFILES=db,signal,whatsapp`, `WHATSAPP_ENABLED=1`,
   `WA_TARGET_CHAT_IDS=…@g.us,…` (empty = all), and set `INGEST_WEBHOOK_TOKEN`
   and `WA_API_KEY` to non-empty secrets.
2. `docker compose up -d --build`, then pair the device once: read the QR from
   `docker compose logs wa-connector` (or temporarily publish port `8082` and
   open `http://localhost:8082/qr`), and scan it from **WhatsApp → Linked
   Devices → Link a Device**.
3. WhatsApp messages/reactions/edits/deletes now flow in (via the connector's
   webhook to `/ingest/whatsapp`) with a **WA** badge; media is downloaded on
   demand. The connector is read-only by default — it sends nothing unless
   `WA_ACTIVITY_TRACKER_ENABLED=1` (transient reaction / presence probe).

> ⚠️ Telegram and WhatsApp Terms of Service prohibit unofficial automation; the
> bot/linked-device accounts you use here can be banned. Run this only against
> **your own, authorized** groups — the same posture as the Signal warning above.

### Settings page (`/settings`)

Most of the setup above can also be done from the web UI once the app is up:

- **Signal** — see whether the daemon is linked, generate a linking QR (no more
  `curl … qrcodelink`), fetch the account's group list and tick which groups to
  monitor. The selection (`signal_target_group_ids`) overrides `TARGET_GROUP_IDS`
  and is picked up by the poller within a few seconds — no restart.
- **Telegram** — paste the BotFather token, enable the connector, and (once the
  `tg-connector` sidecar is running) fetch & select chats. The token and enable
  flag take effect on the next stack restart.
- **WhatsApp** — view link status, fetch the pairing QR from the `wa-connector`,
  enable the connector, and select chats.
- **General** — toggle "save my own messages" (whether the bot account's own
  outgoing messages are archived — **on by default**) and the Signal poll
  interval.

Settings are persisted in the `app_settings` table (created automatically) and
layer on top of the env-var defaults in `config.py`. Full design notes:
[`docs/SETTINGS_PAGE_DESIGN.md`](docs/SETTINGS_PAGE_DESIGN.md).

### Using external services instead of the bundled ones

Example — you already run `signal-cli-rest-api` and MySQL elsewhere, and want a
local Ollama too:

```ini
# .env
COMPOSE_PROFILES=telegram,whatsapp,ollama
DB_HOST=10.0.0.5
SIGNAL_API_BASE=http://10.0.0.6:8080
SIGNAL_CLI_DB_LOCAL_PATH=          # leave empty -> falls back to `docker cp` against SIGNAL_CLI_CONTAINER
OLLAMA_API_URL=http://ollama:11434/api/generate
```

### Non-Docker install

You can still run the app directly (`python3 app.py`) against externally-managed
MySQL / signal-cli-rest-api / Ollama — see *Installation* and *Configuration*
below; `config.py` reads the same environment variables.

## Signal REST API Setup

> If you used the Docker Compose quick start above, the `signal-api` service is
> already running — skip straight to step 2 (linking via QR). This section is the
> manual / non-Compose path and also documents how to point the app at an
> externally-managed `signal-cli-rest-api`.

The bot requires a running [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) container linked to your Signal account.

### 1. Start the container

```bash
docker run -d --name signal-api --restart=unless-stopped \
  -p 8080:8080 \
  -v /root/.local/share/signal-api:/home/.local/share/signal-cli \
  -e MODE=normal \
  bbernhard/signal-cli-rest-api:latest
```

### 2. Link to your Signal account via QR code

Generate a QR code from the REST API:

```bash
curl -s 'http://localhost:8080/v1/qrcodelink?device_name=signal-api' --output /tmp/qr.png
```

Open `/tmp/qr.png` on your screen, then scan it with your Signal app:

1. Open **Signal** on your phone
2. Go to **Settings** > **Linked Devices**
3. Tap **Link New Device**
4. Scan the QR code from `/tmp/qr.png`

### 3. Verify the link

```bash
# Should return your phone number in a JSON array
curl -s http://localhost:8080/v1/accounts
```

Expected output: `["+123456789"]` (your linked Signal phone number in E.164 format)

If the accounts list is empty, the linking was not successful — repeat step 2.

### 4. Test receiving messages

```bash
curl -s 'http://localhost:8080/v1/receive/<YOUR_NUMBER_URL_ENCODED>?timeout=5'
```

Replace `<YOUR_NUMBER_URL_ENCODED>` with your URL-encoded phone number (replace the leading `+` with `%2B`, e.g. `+123456789` becomes `%2B123456789`).

### 5. Get Signal group IDs

The bot needs group IDs to know which groups to monitor. List all groups your account belongs to:

```bash
curl -s 'http://localhost:8080/v1/groups/<YOUR_NUMBER_URL_ENCODED>' | python3 -m json.tool
```

This returns a JSON array of groups. Each group has an `"id"` field (base64-encoded string) and a `"name"` field. Copy the `id` values for the groups you want to monitor and set them as a comma-separated list:

```bash
export TARGET_GROUP_IDS='abc123def=,xyz789ghi='
```

Or add them to your `.env` file:

```
TARGET_GROUP_IDS=abc123def=,xyz789ghi=
```

### 6. Locate your signal-cli account database path

The dashboard mirrors signal-cli's `recipient` table from a SQLite file inside the
container so it can resolve names for UUID-only Signal users. The file lives at
`/home/.local/share/signal-cli/data/<ACCOUNT_ID>.d/account.db` inside the
container, where `<ACCOUNT_ID>` is signal-cli's per-account directory name (a
numeric ID assigned at link time, **not** your phone number).

Find yours by listing the data directory inside the container:

```bash
docker exec signal-api ls /home/.local/share/signal-cli/data/
```

You should see one or more entries ending in `.d` (e.g. `123456.d`). Set the
full path in your `.env`:

```
SIGNAL_CLI_DB_PATH=/home/.local/share/signal-cli/data/123456.d/account.db
```

The default in `config.py` is a placeholder (`+CHANGEME.d`) — you **must**
override it with `SIGNAL_CLI_DB_PATH` (or edit `config.py` directly), otherwise
the recipient sync worker will log `docker cp failed` every cycle.

### Troubleshooting

- **"User not registered" error**: The account link was lost (e.g., container recreated). Re-link using steps 2-3 above.
- **Container shows healthy but no accounts**: Check that the volume mount path exists and has the correct permissions.
- **QR code expired**: QR codes are single-use and expire quickly. Generate a new one and scan immediately.
- **`recipient sync unavailable` / `FileNotFoundError: 'docker'` in the logs**: the app image has no Docker socket — use the local-path mode instead of `docker cp`: set `SIGNAL_CLI_DB_LOCAL_PATH` (and `SIGNAL_CLI_DATA_DIR` for an external signal-api) per the box above. Or set `SIGNAL_RECIPIENTS_SYNC_ENABLED=0` if you don't need UUID→name resolution.
- **`ADD UNIQUE INDEX idx_msg_dedup ... 1062 Duplicate entry` at startup**: the `messages` table has duplicate rows from before that index existed (common after migrating from an older/external DB). Run the one-off cleanup [`scripts/dedup-messages.sql`](scripts/dedup-messages.sql) (it backs up nothing — take a `mysqldump` first), then `docker compose up -d`. `INSERT IGNORE` prevents new duplicates once the index is in place.

## Installation

```bash
git clone <repo-url> && cd signalbot
pip install -r requirements.txt
playwright install chromium
```

## Configuration

All settings live in `config.py` and can be overridden with environment variables (or a `.env` file):

```bash
# Database
export DB_HOST=localhost
export DB_USER=signalbot
export DB_PASSWORD=your_password
export DB_NAME=messages_db

# Signal REST API
export SIGNAL_API_BASE=http://localhost:8080
export SIGNAL_PHONE_NUMBER=+1234567890
export TARGET_GROUP_IDS=groupId1,groupId2   # comma-separated

# signal-cli account.db path inside the signal-api container.
# See "Signal REST API Setup → step 6" for how to find <ACCOUNT_ID>.
export SIGNAL_CLI_DB_PATH=/home/.local/share/signal-cli/data/<ACCOUNT_ID>.d/account.db

# Ollama
export OLLAMA_API_URL=http://localhost:11434/api/generate
export OLLAMA_SUMMARY_MODEL=qwen3:4b-instruct-2507-q8_0
export OLLAMA_ANALYSIS_MODEL=llama3.2:3b-instruct-q4_1
export OLLAMA_RETRY_ATTEMPTS=5

# Application
export PORT=5581
export POLL_INTERVAL=10        # seconds between poll cycles
export SUMMARY_INTERVAL=3600   # seconds between summary refreshes
export LOG_LEVEL=DEBUG
export TZ=Europe/Helsinki      # container timezone; the poller stores Signal
                               # timestamps as local wall-clock time, so set
                               # this to your zone or message times will be off
                               # (default UTC). With Docker Compose, TZ in .env
                               # is applied to the app, MySQL, signal-api, and
                               # the connectors.
```

## Usage

```bash
# Start both web dashboard and message poller (recommended)
python3 app.py

# Web dashboard only (no message polling)
python3 app.py --no-poller

# Poller only (headless, no Flask)
python3 app.py --no-web

# Verbose logging
python3 app.py --debug

# Override Flask port
python3 app.py --port 8080
```

The web dashboard is available at `http://localhost:5581` by default.

### Dashboard Pages

| Route | Description |
|-------|-------------|
| `/` | Dashboard with statistics, charts, and tag clouds |
| `/messages` | Browse all messages with group/sender/search filters |
| `/filtered` | Messages with URLs, screenshots, and AI analysis (last 30 days) |
| `/summary` | AI-generated group message summaries (last 24 hours) |
| `/attachments` | Screenshots and file attachments (images, videos, PDFs) |
| `/pages` | Archived HTML page snapshots with search and diff comparison |
| `/search` | Global full-text search across all data |
| `/intel` | Intel console: entities, watchlist hits, reaction graphs, briefs |
| `/stream` | Message Stream View — large-font live feed (in-car / wall display) |
| `/settings` | Configure Signal / Telegram / WhatsApp (QR linking, group & chat selection) and general behaviour |

## Architecture

```
Main Thread (Flask on :5581)
    ├── poller daemon thread
    │     └── poll_messages → poll_attachments → ai_main → sleep(10s)
    ├── summary-worker daemon thread
    │     └── fetch_messages_last_24h → update_all_summaries → sleep(1h)
    ├── fulltext-builder daemon thread (one-time, on startup)
    └── Flask app.run()
```

### Data Flow

```
Signal Groups → Signal REST API → poller.py → MySQL
                                      │
                                      ├── Playwright → screenshots (PNG)
                                      ├── Playwright → HTML snapshots
                                      └── Ollama → AI analysis
                                                       ↓
                                        app.py (Flask) ←→ Ollama (summaries)
                                             ↓
                                        Web Browser (:5581)
```

## Database Setup

The full schema is checked in as [`schema.sql`](schema.sql) — `CREATE TABLE` statements only, no data. It covers all tables the app uses (messages, attachments, reactions, group membership, page snapshots, AI summaries, intel/watchlist, activity probing, signal-cli recipient mirror, `app_settings`, …) along with their indexes (including the FULLTEXT indexes used by `/search`).

### Bundled MySQL container (recommended)

The simplest setup is the bundled `mysql` service (Compose profile `db`): set `DB_HOST=mysql` and add `db` to `COMPOSE_PROFILES` in `.env`, then `docker compose up -d`. On first start it creates the `${DB_NAME}` database, the `${DB_USER}` user, and runs `schema.sql` automatically — no manual SQL needed. (Also set `DB_ROOT_PASSWORD` in `.env`; it defaults to `changeme`.)

**Migrating an existing database into it:** if you've been running against an external MySQL and want to move to the bundled container, use:

```bash
./scripts/migrate-db-to-bundled-mysql.sh            # interactive — confirms each step
./scripts/migrate-db-to-bundled-mysql.sh --yes      # non-interactive
./scripts/migrate-db-to-bundled-mysql.sh --help     # --from-host, --out, --dump-file, --skip-dump, --update-env, --cleanup
```

It reads the credentials from `.env` and walks through the steps, asking you to confirm each one (use `--yes` to skip the prompts): (1) start the bundled `mysql` container; (2) `mysqldump` the source DB to a `.sql` file (using a local `mysqldump`, or the `mysql:8.0` image with `--network host` if you don't have one); (3) load that file into the bundled container's database; (4) verify table & row counts; (5) point `.env` at the bundled container — set `DB_HOST=mysql` and add `db` to `COMPOSE_PROFILES`, with a `.env.bak` backup (`--update-env` auto-confirms this step; declining just prints the lines for you). It does **not** add `signal` to `COMPOSE_PROFILES` — if you already run a `signal-api` container the bundled one would clash on port 8080, so leave `SIGNAL_API_BASE` pointed at the existing one. The dump file is kept by default (pass `--cleanup` to delete it). If the source MySQL refuses the connection (e.g. it only grants `localhost`), produce the dump there yourself and pass `--dump-file`.

### 1. Create the database and user (external MySQL only)

Connect to MySQL as an admin user and run:

```sql
CREATE DATABASE messages_db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'signalbot'@'%' IDENTIFIED BY 'your_password';
GRANT ALL PRIVILEGES ON messages_db.* TO 'signalbot'@'%';
FLUSH PRIVILEGES;
```

Adjust the host (`'%'` → `'localhost'` or a specific IP) and password to match your environment, then update `DB_HOST` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` in your `.env` accordingly.

### 2. Load the schema

```bash
mysql -h <DB_HOST> -u signalbot -p messages_db < schema.sql
```

That's it — the bot can be started immediately afterwards (`python3 app.py`). On startup the app will only create the additional `page_snapshots` table if it's missing and verify the FULLTEXT indexes; it will not drop or modify any existing tables.

### Updating the schema later

If you change a table definition during development, regenerate `schema.sql` from a live database with:

```bash
mysqldump -h <DB_HOST> -u signalbot -p \
  --no-data --skip-comments --routines --triggers --events \
  messages_db > schema.sql
```

## License

Private project.
