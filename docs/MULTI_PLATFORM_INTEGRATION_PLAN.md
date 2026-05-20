# Multi‑Platform Integration Plan — Telegram & WhatsApp

**Status:** Superseded by the approved implementation plan — see below.
**Author:** (drafted with Claude Code)
**Scope:** Add Telegram and WhatsApp as additional observation sources alongside Signal, reuse the existing web UI, tag every message with its origin platform, and add new *Intelligence Center* tabs that surface cross‑platform intelligence (linked identities, cross‑platform URL spread, shared chat membership, etc.).

> ℹ️ **This document is the original survey/options draft.** The version that was
> reviewed and approved for implementation is in the plan file
> `~/.claude/plans/lets-modify-the-plan-stateless-harp.md`. Key decisions that
> the approved plan locks in (and which override the corresponding "options"
> discussions below):
> - **Docker Compose** ships the whole stack (`signalbot` + `mysql` + `signal-api` + the two connector sidecars), every bundled dependency optional via `COMPOSE_PROFILES` and `.env`‑overridable bases. *(Phase 0 of the approved plan — partially implemented: `Dockerfile`, `.dockerignore`, `docker-compose.yml`, updated `.env.example`/`README.md`, `SIGNAL_CLI_DB_LOCAL_PATH` plumbing, `connectors/base.py`, `url_norm.py`, `.platform-badge` CSS.)*
> - **Telegram = Bot API only** (no MTProto/userbot) — chosen for stealth: an innocuous, read‑only bot with privacy mode disabled, in its own container. Bot‑API blind spots (no pre‑join history, no other users' DMs, channels need admin, no presence/receipts for other users) are accepted.
> - **WhatsApp = a custom connector built on Baileys** (`@whiskeysockets/baileys`, MIT, Node, no headless browser, no paid tooling) in its own container; session stays in the connector volume.
> - **Outbound / activity‑tracker parity** for the new platforms, modelled on the Signal activity tracker, **disabled by default** (`TG_ACTIVITY_TRACKER_ENABLED=0`, `WA_ACTIVITY_TRACKER_ENABLED=0`); on Telegram the RTT/presence probe is documented as not implementable via Bot API.
>
> Everything else below (canonical event schema, `ingest_event()`, `platform` columns, `chats`/`identities`/`identity_links`/`connector_cursors`/`url_observations` tables, identity engine, the four new Intelligence Center tabs, badge/filter retrofit, phasing) still stands.

> ⚠️ **Ethics / legal note.** This repository is explicitly a *privacy‑awareness demonstration* meant to be run against **your own** groups. Telegram's and WhatsApp's Terms of Service prohibit unofficial automation/scraping; the linked‑device and Bot‑API techniques below can get the underlying account banned. Treat this plan the same way the project treats the Signal integration: a controlled, authorized, self‑observed demonstration. Add explicit per‑platform warnings to the README before shipping any of this.

---

## 1. Executive summary

The current system is a three‑tier pipeline:

```
Signal groups ──▶ signal-cli-rest-api (sidecar container, holds the linked session)
                       │  HTTP  (GET /v1/receive/{phone}, /v1/groups, /v1/attachments, …)
                       ▼
                  poller.py ──▶ MySQL (messages, reactions, group_members, page_snapshots, …)
                       │
                  app.py (Flask :5581) ◀──▶ Ollama (summaries / per‑URL analysis)
                       ▼
                  Web dashboard + Intelligence Center
```

We keep this exact shape and **add two more sidecar "connector" containers** — one for Telegram, one for WhatsApp — each holding *only* that platform's login/session, exactly as `signal-cli-rest-api` does for Signal. The main app never sees platform credentials; it only talks HTTP to the connectors (and/or receives webhooks from them) using a shared bearer token.

Inside the app we introduce:

* a **canonical event schema** that all connectors normalize to;
* a single **`ingest_event()`** path that everything (including Signal, refactored) flows through;
* a **`platform` column** on `messages` (and the related tables) so every row knows where it came from — the existing `group_id` / `group_name` / `sender_phone` / `sender_name` columns stay populated so all current queries and views keep working;
* an **identity‑linking subsystem** (`identities` + `identity_links` tables + a background worker) that proposes "this Telegram account == this WhatsApp account == this Signal account";
* **four new Intelligence Center tabs** plus a platform filter/badge retrofit on the existing tabs and message views.

Rollout is phased so each phase ships independently with no behavior regression.

---

## 2. Current architecture — what we're building on

| Component | File | Role |
|---|---|---|
| Combined entry point | `app.py` (~7.3k lines) | Flask `:5581` + spawns ~10 daemon threads (poller, group‑sync, LLM queue worker, sentiment, page tracker, watchlist, behavioral profiler, intel brief, rollups, activity tracker, recipient sync). 80+ routes incl. `/intel?tab=…` and ~40 `/api/intel/*` endpoints. |
| Message poller | `poller.py` | `run_poller()` loop: `poll_messages()` → `poll_attachments()` → `ai_main()` every `POLL_INTERVAL`s. Parses signal‑cli envelopes, persists `messages`/`reactions`/`remote_deletes`/`message_quotes`/`message_mentions`/`message_attachments`/`page_snapshots`. `run_group_sync_loop()` snapshots `/v1/groups`. |
| Outbound Signal helper | `signal_api.py` | Thin wrapper for `POST/DELETE /v1/reactions/{number}` (used by the activity tracker). |
| Config | `config.py` | All settings via env vars / `.env`. |
| LLM task queue | `llm_queue.py` | MySQL‑backed task queue (`llm_tasks`): `summary`, `sentiment`, `cross_group`, `monthly_summarize`, `yearly_summarize`. |
| Schema | `schema.sql` | `messages`, `attachments`, `message_attachments`, `reactions`, `remote_deletes`, `message_quotes`, `message_mentions`, `message_entities`, `group_members`, `group_snapshots`, `group_membership_events`, `signal_recipients`, `page_snapshots`/`page_changes`/`tracked_urls`, `keyword_watchlist`/`watchlist_hits`, `daily/monthly/yearly_summaries`, `intel_briefs`, `llm_tasks`, `sender_profiles`, `activity_enrollment/probes/samples`. |
| Schema bootstrap | `app.ensure_db_indexes()` + `_alter_with_fallback()` | Idempotent on‑boot migrations (INSTANT→INPLACE→COPY ALTERs, FULLTEXT index checks, UUID‑hygiene migration). This is where new columns/tables will be added. |
| UI | `templates/base.html` (sidebar), `dashboard.html`, `messages.html`, `filtered.html`, `intel.html` (2.5k lines, 12 tabs), `message_stream.html`, `pages*.html`, `analytics_*.html`, `search.html`, `topics.html`. |

**Key fact for the data model:** every intel query keys off `messages.group_id` (`varchar(255)`), `messages.group_name`, `messages.sender_phone`, `messages.sender_name`, plus `messages.url` (pipe‑joined URLs) and `messages.raw_envelope` (JSON). The `idx_msg_dedup` unique index is `(sender_phone(20), group_id(64), sent_timestamp)`. We must preserve these semantics, so the rule is: **non‑Signal connectors map their native chat/user IDs into those columns (as synthetic strings) AND also store the native IDs in new columns.**

The `messages` table already carries `raw_envelope JSON`, `message_type VARCHAR(24)`, `source_uuid`, `source_device`, `server_received_ts`, `server_delivered_ts`, `expires_in_seconds` — these are reusable for the canonical envelope.

---

## 3. Connector strategy per platform

The design constraint from the brief: **the actual platform API/login lives in a separate app/container** (like `signal-cli-rest-api` → `signalbot`), so the credentials never touch the intelligence app.

### 3.1 Signal — no change

Keep `bbernhard/signal-cli-rest-api`. We *refactor* the app side (poller's inline `requests.get` calls move behind a `SignalAdapter`), but the container, the link flow, and the wire protocol are untouched.

### 3.2 Telegram

Two viable connector models; we recommend supporting both, leading with the MTProto user client because it matches the "observe arbitrary groups/channels you belong to" use case (the same thing Signal's linked device gives us).

| Model | What it sees | Login | Connector implementation |
|---|---|---|---|
| **MTProto user client ("userbot")** — *recommended primary* | Everything the *user account* can see: every group/channel/DM the account is a member of, full history, edits, deletions, reactions. Mirrors the Signal linked‑device model. | Phone number + login code (+ 2FA password). Produces a `.session` file / session string stored **only in the connector volume**. | A small `tg-connector` service (FastAPI/Flask) wrapping **Telethon** (Python) or **GramJS / mtcute** (Node). Or wrap an existing project (e.g. a TDLib‑based bridge). |
| **Bot API** — *secondary / lighter* | Only chats the bot was *added to*; for groups, only messages if "privacy mode" is off **or** the bot is an admin; no history before it joined; no other users' DMs. | A `@BotFather` token — no phone, no session, lowest risk. | Run the official local **`telegram-bot-api`** server as the sidecar and long‑poll `getUpdates`, or just have a thin connector long‑poll `api.telegram.org` directly. |

**`tg-connector` interface (the "internal API"):**

```
POST /v1/login/start        { phone }                       -> { state: "code_required" }
POST /v1/login/code         { phone, code, password? }      -> { state: "ready" }
GET  /v1/me                                                 -> { id, username, phone }
GET  /v1/chats                                              -> [ { id, title, kind, is_public, members_count } ]
GET  /v1/chats/{id}/members                                 -> [ { id, username, phone?, name } ]
GET  /v1/events?since=<cursor>&limit=N                      -> { events: [CanonicalEvent...], next_cursor }
GET  /v1/files/{file_id}                                    -> raw bytes (downloaded media)
# optional push:
(connector POSTs)  ->  app:  POST /ingest/telegram   Authorization: Bearer <INGEST_WEBHOOK_TOKEN>
```

Notes: Telegram chat IDs are signed 64‑bit ints (channels/supergroups are large negatives like `-100…`); store as strings. Handle Telethon `FloodWaitError` with backoff (mirror the existing `_GROUP_SYNC_BACKOFF` pattern). Media (photos/docs) are downloaded by the connector and exposed via `/v1/files/{id}` so the app's attachment pipeline stays platform‑agnostic.

### 3.3 WhatsApp

No official API exists for "read all my groups." The realistic option is the **WhatsApp Web multi‑device protocol** via a linked companion device — the exact same model as Signal (scan a QR, you're a linked device, you receive everything in real time including disappearing/"view once" messages while linked). Several mature libraries implement it:

* **`whatsmeow`** (Go) — the protocol library used by Mautrix's `mautrix-whatsapp` bridge and by **WAHA** (WhatsApp HTTP API).
* **WAHA** (`devlikeapro/waha`) — a self‑hostable Docker image that wraps the above behind a **REST API + webhooks**. This is the closest analogue to `signal-cli-rest-api`: drop‑in container, QR pairing endpoint, message webhook. *Recommended.*
* **Baileys** (`@whiskeysockets/baileys`, Node) / **wppconnect** — if we'd rather build a thin connector ourselves.

> Not suitable: the *official* **WhatsApp Business Cloud API** (Meta) — it only delivers conversations for *your own business number* and cannot observe arbitrary groups.

**`wa-connector` interface** (when using WAHA, it already provides most of this):

```
POST /api/sessions/{session}/start
GET  /api/sessions/{session}/auth/qr          -> QR image / pairing code
GET  /api/sessions/{session}/me
GET  /api/{session}/chats                     -> [ { id: "<jid>", name, isGroup, participantsCount } ]
GET  /api/{session}/chats/{chatId}/messages?since=...
GET  /api/{session}/groups/{groupId}/participants
GET  /api/{session}/files/{mediaId}           -> raw bytes
# push (WAHA's native model):
WAHA -> app:  POST /ingest/whatsapp   Authorization: Bearer <INGEST_WEBHOOK_TOKEN>   body = WAHA "message" event
```

Notes: WhatsApp chat JIDs look like `123456789-1610000000@g.us` (groups) or `<number>@s.whatsapp.net` (DMs); user IDs are `<number>@s.whatsapp.net`. Phone numbers come as **bare digits** — normalize to `+E.164` for cross‑platform matching against Signal/WhatsApp/Telegram phones. Media is encrypted; the bridge lib decrypts and the connector serves bytes via `/files`.

### 3.4 Security model for connectors (shared)

* Each connector container holds **only** its own platform credentials (Telegram `.session` / API id+hash, WhatsApp linked‑device session) in a private Docker volume.
* The main app holds **only** bearer tokens: one per connector for *outbound* calls (`TG_CONNECTOR_TOKEN`, `WA_API_KEY`) and one shared `INGEST_WEBHOOK_TOKEN` it uses to *verify inbound* webhooks.
* All inter‑service traffic stays on a private compose network; connectors expose **no public ports** (except a localhost‑bound debug port if you want the QR page).
* If a connector is compromised, the blast radius is one platform account — the intelligence DB and the other platforms are unaffected.

---

## 4. Canonical event schema (the contract)

Every adapter converts native payloads into this shape; `ingest_event()` is the only writer to `messages` & friends.

```jsonc
{
  "schema": 1,
  "platform": "signal" | "telegram" | "whatsapp",
  "connector_id": "signal-1",                 // which connector instance produced this
  "event_type": "message" | "edit" | "delete" | "reaction" | "reaction_remove"
              | "join" | "leave" | "admin_grant" | "admin_revoke"
              | "chat_rename" | "chat_meta",
  "platform_msg_id": "string|int-as-string",  // native message id (Signal: sourceUuid+timestamp synthetic)
  "platform_chat_id": "string",               // Signal: groupId; TG: -100123…; WA: …@g.us
  "chat": {
    "title": "…", "kind": "group" | "channel" | "dm", "is_public": false,
    "members_count": 42
  },
  "sender": {
    "platform_user_id": "string",             // Signal: ACI/UUID or phone; TG: int; WA: …@s.whatsapp.net
    "display_name": "…",
    "username": "…|null",                     // TG @handle, etc.
    "phone": "+358…|null"
  },
  "timestamp_ms": 1716000000000,
  "text": "message text with urls",
  "urls": ["https://…"],                       // pre-extracted; app re-extracts too as a safety net
  "reply_to": { "platform_msg_id": "…", "author_user_id": "…", "text": "…" } | null,
  "mentions": [ { "platform_user_id": "…", "username": "…" } ],
  "reaction": { "emoji": "👍", "target_msg_id": "…", "target_author_id": "…", "is_remove": false } | null,
  "attachments": [
    { "id": "…", "content_type": "image/jpeg", "file_name": "…", "size": 12345,
      "fetch_url": "/v1/files/<id>" }
  ],
  "edit_of": { "platform_msg_id": "…" } | null,
  "delete_of": { "platform_msg_id": "…" } | null,
  "raw": { /* the original platform payload, stored verbatim in messages.raw_envelope */ }
}
```

Two transports, both funnel into `ingest_event()`:

* **Pull** — `adapter.fetch_events(since_cursor)` → list of canonical events. Used for Signal (unchanged poll loop) and as the default for Telegram (`/v1/events?since=`). Cursor persisted in a tiny `connector_cursors(connector_id, cursor, updated_at)` table.
* **Push** — `POST /ingest/<platform>` (bearer‑guarded) with one canonical event (or a WAHA/Bot‑API native event that the route adapts). Used for WhatsApp (WAHA webhooks) and optionally Telegram.

---

## 5. Database changes

Principle: **add, don't restructure.** All changes go through `app.ensure_db_indexes()` so they apply on boot to existing installs; `schema.sql` is regenerated afterwards.

### 5.1 Columns added to existing tables

`messages` (via `_MESSAGES_EXTRA_COLUMNS`, INSTANT ALTER):

| Column | Type | Notes |
|---|---|---|
| `platform` | `varchar(16) NOT NULL DEFAULT 'signal'` | origin platform |
| `connector_id` | `varchar(64) DEFAULT NULL` | which connector instance |
| `platform_chat_id` | `varchar(190) DEFAULT NULL` | native chat id (also mirrored into `group_id` as `"<platform>:<chat_id>"` for Signal we keep the bare base64 id for back‑compat) |
| `platform_msg_id` | `varchar(190) DEFAULT NULL` | native message id |
| `platform_user_id` | `varchar(190) DEFAULT NULL` | native sender id |
| `sender_username` | `varchar(190) DEFAULT NULL` | TG @handle etc. |
| `edited_at` | `datetime(3) DEFAULT NULL` | last edit time (Telegram/WhatsApp edits) |

New idempotency index, replacing the Signal‑only one: `idx_msg_platform_dedup UNIQUE (platform, platform_chat_id, platform_msg_id, platform_user_id(64))` — keep `idx_msg_dedup` too for old Signal rows. Plus `idx_msg_platform (platform, sent_timestamp)`.

The same `platform` (+ `platform_chat_id` / `platform_user_id` where relevant) columns get added to: `reactions`, `message_attachments`, `attachments` (or via a `attachment_sources` join), `message_quotes`, `message_mentions`, `message_entities`, `group_members`, `group_snapshots`, `group_membership_events`, `page_snapshots`, `sender_profiles`, `daily/monthly/yearly_summaries`, `intel_briefs`. Default `'signal'` everywhere so the migration is a no‑op for current data.

### 5.2 New tables

```sql
-- Unified registry of every monitored chat across platforms (generalizes the Signal "group" notion).
CREATE TABLE chats (
  id              bigint AUTO_INCREMENT PRIMARY KEY,
  platform        varchar(16)  NOT NULL,
  platform_chat_id varchar(190) NOT NULL,
  connector_id    varchar(64),
  title           varchar(255),
  kind            enum('group','channel','dm') DEFAULT 'group',
  is_public       tinyint(1) DEFAULT 0,
  member_count    int DEFAULT 0,
  first_seen_at   datetime, last_seen_at datetime,
  is_monitored    tinyint(1) DEFAULT 1,
  raw_meta        json,
  UNIQUE KEY uq_chat (platform, platform_chat_id)
);

-- Canonical persons (one row per real human, possibly spanning platforms).
CREATE TABLE identities (
  id           bigint AUTO_INCREMENT PRIMARY KEY,
  label        varchar(255),            -- human-chosen / best-guess display label
  notes        text,
  is_confirmed tinyint(1) DEFAULT 0,    -- a human merged/confirmed this identity
  created_at   datetime DEFAULT CURRENT_TIMESTAMP
);

-- Maps a per-platform account to a canonical identity, with evidence.
CREATE TABLE identity_links (
  id              bigint AUTO_INCREMENT PRIMARY KEY,
  identity_id     bigint NOT NULL,
  platform        varchar(16) NOT NULL,
  platform_user_id varchar(190) NOT NULL,
  link_method     enum('manual','phone_exact','username_exact','displayname_fuzzy',
                       'url_cooccurrence','behavioral','reply_pattern') NOT NULL,
  confidence      float DEFAULT 0,       -- 0..1
  evidence        json,                  -- e.g. {"phone":"+358…"} or {"shared_urls":[…],"window_s":120}
  status          enum('proposed','confirmed','rejected') DEFAULT 'proposed',
  created_at      datetime DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_link (platform, platform_user_id, identity_id),
  KEY idx_il_identity (identity_id),
  KEY idx_il_status (status, confidence)
);

-- Per-connector ingest cursor for the pull transport.
CREATE TABLE connector_cursors (
  connector_id varchar(64) PRIMARY KEY,
  cursor       varchar(190),
  updated_at   datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Denormalized URL appearances — makes "which URL/domain spread to which chats across platforms" fast.
-- Populated by ingest_event() (and a backfill job over existing messages.url).
CREATE TABLE url_observations (
  id            bigint AUTO_INCREMENT PRIMARY KEY,
  message_id    int,
  normalized_url varchar(2083),          -- lowercased host, stripped tracking params, no fragment
  domain        varchar(255),
  platform      varchar(16),
  platform_chat_id varchar(190),
  chat_title    varchar(255),
  platform_user_id varchar(190),
  observed_at   datetime,
  KEY idx_uo_norm (normalized_url(191), observed_at),
  KEY idx_uo_domain (domain, observed_at),
  KEY idx_uo_chat (platform, platform_chat_id, observed_at)
);
```

### 5.3 Migration & backfill

1. `ensure_db_indexes()` adds columns/indexes/tables (idempotent, INSTANT where possible).
2. One‑time backfill jobs (guarded by a marker row): populate `chats` from existing `group_snapshots`/`messages` (`platform='signal'`); populate `url_observations` from existing `messages.url`; create one `identities` + `identity_links(link_method='phone_exact')` per known Signal `sender_phone`.
3. Regenerate `schema.sql` with the documented `mysqldump --no-data` command.

---

## 6. App‑side code changes

### 6.1 New modules

* **`connectors/base.py`** — `CanonicalEvent` dataclass; `Adapter` ABC with `fetch_events(cursor) -> (events, next_cursor)`, `list_chats()`, `list_members(chat_id)`, `fetch_file(file_ref) -> bytes`.
* **`connectors/signal_adapter.py`** — move poller's inline `requests.get('/v1/receive/…')`, `_fetch_groups_list`, attachment fetching here; produce `CanonicalEvent`s. (`poller.py` keeps its loop but calls the adapter.)
* **`connectors/telegram_adapter.py`** — talks to `TG_CONNECTOR_BASE` with `TG_CONNECTOR_TOKEN`.
* **`connectors/whatsapp_adapter.py`** — talks to `WA_CONNECTOR_BASE` with `WA_API_KEY`; also a translation function for WAHA webhook payloads → `CanonicalEvent`.
* **`ingest.py`** — `ingest_event(conn, evt)`: dedup, write `messages` (+ `chats` upsert, `reactions`, `message_quotes`, `message_mentions`, `message_attachments`, `group_membership_events`, `url_observations`), trigger screenshot/AI for URLs (reuse `poller.take_screenshot` + `poller.ai_main`'s per‑URL logic — already keyed off `messages.url`, so platform‑agnostic). Handles `edit`/`delete` events (update `edited_at` / `deleted_at`).
* **`identity_engine.py`** — `propose_links(conn)`: phone‑exact, username‑exact, displayname‑fuzzy, url‑cooccurrence (same normalized URL by two accounts within N minutes across platforms), behavioral (reuse `sender_profiles` features generalized across platforms). Writes `identity_links(status='proposed')`. `merge_identities()`, `split_identity()`, `confirm_link()`, `reject_link()` for the UI.
* **`url_norm.py`** — `normalize_url()` / `extract_domain()` shared by ingest and the analytics endpoints.

### 6.2 Threads added to `app.main()`

* `connector-poller` — for each *pull* adapter (Signal stays in `poller.run_poller`; Telegram via its adapter), loop `fetch_events(cursor) → ingest_event(...) → save cursor → sleep`.
* `identity-worker` — periodically `identity_engine.propose_links()` (default every ~30 min); cheap.
* (no new thread for WhatsApp ingest — it arrives via the `/ingest/whatsapp` webhook handled in the Flask request thread; just enqueue+commit quickly.)
* `chat-sync` — generalize `run_group_sync_loop` to also pull `list_chats()`/`list_members()` from Telegram/WhatsApp connectors into `chats` + `group_members` + `group_membership_events` (events get a `platform` value).

### 6.3 New routes

| Route | Purpose |
|---|---|
| `POST /ingest/<platform>` | Bearer‑guarded webhook ingest (WhatsApp/WAHA, optional Telegram). |
| `GET /api/intel/platforms` | Per‑platform counts: messages, chats, senders, linked‑identity %, volume time series. |
| `GET /api/intel/identities` | List canonical identities + their per‑platform accounts + best‑known phone/username. |
| `GET /api/intel/identity/<id>` | One identity: all accounts, all chats it appears in (any platform), evidence for each link. |
| `GET /api/intel/link_candidates` | Proposed `identity_links` with evidence, ranked by confidence (for the merge UI). |
| `POST /api/intel/identity/merge` / `…/split` / `…/confirm_link` / `…/reject_link` | Identity curation. |
| `GET /api/intel/url_spread` | Per normalized URL/domain: appearances `[ {platform, chat, sender_identity, ts} ]`, first‑mover platform, propagation edges, cross‑platform reach score. |
| `GET /api/intel/chat_bridge` | Bipartite users↔chats data across platforms; "these N chats share M people"; co‑membership clusters. |
| `GET /api/intel/cross_platform_dossier/<identity_id>` | A dossier (like the existing `/api/intel/dossier/<phone>`) aggregated over *all* of an identity's platform accounts. |
| (existing `/api/intel/*`) | Gain an optional `?platform=` filter param; responses include a `platform` field on every node/edge/row. |

### 6.4 Config additions (`config.py` / `.env.example`)

```bash
# ── Telegram connector ────────────────────────────
TELEGRAM_ENABLED=0
TG_CONNECTOR_BASE=http://tg-connector:8081
TG_CONNECTOR_TOKEN=changeme-outbound
TG_TARGET_CHAT_IDS=                 # comma-separated native chat ids; empty = all chats the account is in
TG_POLL_INTERVAL=5

# ── WhatsApp connector (WAHA) ─────────────────────
WHATSAPP_ENABLED=0
WA_CONNECTOR_BASE=http://waha:3000
WA_API_KEY=changeme-outbound
WA_SESSION=default
WA_TARGET_CHAT_IDS=                 # comma-separated JIDs; empty = all
# WAHA pushes to us:
INGEST_WEBHOOK_TOKEN=changeme-shared-secret

# ── Identity engine ───────────────────────────────
IDENTITY_LINK_INTERVAL=1800
IDENTITY_AUTOCONFIRM_THRESHOLD=0.97   # only auto-confirm extremely strong evidence (e.g. exact phone)
IDENTITY_URL_COOCCURRENCE_WINDOW_S=600
```

Credentials that must live **only in the connector containers**, never in the main app: `TG_API_ID` / `TG_API_HASH` / `TG_PHONE` (Telethon), and the WhatsApp linked‑device session (created via WAHA's QR endpoint, stored in WAHA's volume).

---

## 7. UI changes

### 7.1 Platform badges & filters (retrofit)

* Add a `.platform-badge` CSS component in `static/style.css` — small colored chip: `SG` (Signal), `TG` (Telegram), `WA` (WhatsApp), with distinct accent colors. Render it next to every message/row in: `dashboard.html` (recent messages), `messages.html`, `filtered.html`, `message_stream.html`, `search.html`, `topics.html`, and inside `intel.html` (dossiers, network node tooltips, reactions, membership, info‑flow).
* Add a **Platform** dropdown to the existing filter bars (next to Group/Sender/Date) on `/messages`, `/filtered`, `/search`, `/analytics/domains`, and as a query param on the `/api/intel/*` endpoints.
* `/api/groups` (used by filter dropdowns) → return `platform` per group and group the list by platform.

### 7.2 New Intelligence Center tabs

`intel.html`'s tab bar (currently: Network, Info Flow, Entities, Dossiers, Coordination, Intel Brief, Narratives, Behavioral, Membership, Reactions, Devices, Activity) gains:

1. **Cross‑Platform** — overview dashboard: stacked‑area message volume per platform over time (`chart.js`); cards for # chats / # senders / # linked identities per platform; "top cross‑platform actors" table (identities active on ≥2 platforms, ranked by combined message count); "platform overlap" Venn‑ish summary.
2. **Identity Graph** — `vis-network` graph: large nodes = canonical `identities`, small satellite nodes = per‑platform accounts colored by platform, edges = link evidence (thickness = confidence, dashed = `proposed`). Side panel: click a node → its accounts, the chats it's in on each platform, and the evidence. Below the graph: **Link Candidates** table (proposed links with evidence) with **Confirm / Reject / Merge into…** buttons → the `/api/intel/identity/*` POST routes. Also a manual "merge these two accounts" search box.
3. **Cross‑Platform URL Spread** — pick a URL or domain (or browse "most‑spread URLs") → timeline of every appearance across platforms/chats/senders; a small propagation graph (platform→platform edges with the time lag of first appearance); "first‑mover" badge on the originating platform/chat; tables: *top URLs by cross‑platform reach* (# distinct platforms × # distinct chats), *domains amplified on platform X but absent on Y*, *URLs that jumped Telegram→WhatsApp within <1h*. This is the literal "what URLs are posted most to what groups between platforms" requirement.
4. **Chat Bridge** — bipartite `vis-network` of users↔chats across platforms (chat nodes colored by platform); a co‑membership matrix ("Telegram channel A and WhatsApp group B share 8 of the same identities"); clustering of chats by shared‑member overlap (Jaccard) to reveal cross‑platform "rooms" run by the same crowd.

Plus: the existing **Dossiers** tab becomes identity‑aware — if a sender is part of a linked identity, show a "Cross‑platform identity" banner and merge the stats across all their accounts (calls `/api/intel/cross_platform_dossier/<id>`).

---

## 8. Phased delivery

Each phase is shippable and reversible; nothing after Phase 0 changes Signal behavior.

### Phase 0 — Abstractions & schema (no behavior change, all data `platform='signal'`)
* Add `platform` (+ native‑id) columns to `messages` and friends via `ensure_db_indexes()`; add `chats`, `identities`, `identity_links`, `connector_cursors`, `url_observations`; backfill from existing data; regenerate `schema.sql`.
* Introduce `connectors/base.py` (`CanonicalEvent`, `Adapter`), `ingest.py` (`ingest_event`), `url_norm.py`.
* Refactor `poller.py` Signal HTTP into `connectors/signal_adapter.py`; route Signal messages through `ingest_event()`. Verify byte‑for‑byte equivalent rows.
* Add the `.platform-badge` component and the (Signal‑only for now) Platform filter to the UI.
* **Exit criteria:** existing app behaves identically; new columns populated; tests green.

### Phase 1 — Telegram
* Build `connectors/telegram/` sidecar (Telethon‑based, MTProto userbot) — `Dockerfile`, login endpoints, `/v1/events`, `/v1/chats`, `/v1/files`, flood‑wait backoff, `.session` volume. Provide a `docker-compose.override.yml` snippet.
* `connectors/telegram_adapter.py` + `connector-poller` thread + `chat-sync` extension.
* Config: `TELEGRAM_ENABLED`, `TG_*` vars; `TG_TARGET_CHAT_IDS`.
* README section mirroring the Signal setup (BotFather/Bot‑API alternative documented too).
* **Exit criteria:** Telegram messages appear in `/messages`, `/filtered`, `/search`, `/intel` existing tabs with a `TG` badge; screenshots + AI analysis run on Telegram‑posted URLs; `group_membership_events` records Telegram joins/leaves.

### Phase 2 — WhatsApp
* `connectors/whatsapp/` — `docker-compose` service using **WAHA** (or a Baileys micro‑service) with a webhook pointed at `POST /ingest/whatsapp`; QR pairing documented.
* `POST /ingest/<platform>` route (bearer‑guarded) + WAHA‑payload→`CanonicalEvent` translator in `connectors/whatsapp_adapter.py`.
* Config: `WHATSAPP_ENABLED`, `WA_*`, `INGEST_WEBHOOK_TOKEN`.
* **Exit criteria:** WhatsApp messages/reactions/edits/deletes flow in via webhook with a `WA` badge; media stored as attachments; group‑participant changes recorded.

### Phase 3 — Cross‑platform intelligence
* `identity_engine.py` + `identity-worker` thread; phone‑exact / username‑exact / displayname‑fuzzy / url‑cooccurrence / behavioral linkers.
* New routes: `/api/intel/platforms`, `/identities`, `/identity/<id>`, `/link_candidates`, `/identity/merge|split|confirm_link|reject_link`, `/url_spread`, `/chat_bridge`, `/cross_platform_dossier/<id>`.
* New `intel.html` tabs: **Cross‑Platform**, **Identity Graph**, **Cross‑Platform URL Spread**, **Chat Bridge**; identity‑aware Dossiers; `?platform=` filter on existing intel endpoints + a Platform dropdown in the intel UI.
* **Exit criteria:** an analyst can (a) see which accounts are the same person across platforms with evidence and confirm/reject, (b) trace a URL's spread across Signal/Telegram/WhatsApp groups and see which platform/group seeded it, (c) see which groups across platforms share the same people.

### Phase 4 — Polish & hardening
* Per‑platform attachment/screenshot/AI parity; retention/PII controls per platform; perf indexes on `url_observations`/`identity_links`; rate‑limit/backoff tuning; reconnect supervision for connectors; expand `tests/` (fixture connectors that replay canned canonical events for each platform — `--fixture-connector` mode); finalize README + `.env.example`; regenerate `schema.sql`.

---

## 9. Effort estimate (rough)

| Phase | Work | Est. |
|---|---|---|
| 0 | Schema migration + abstraction layer + Signal refactor + badge/filter UI | 3–5 days |
| 1 | Telegram connector container + adapter + ingest + docs | 4–6 days |
| 2 | WhatsApp (WAHA) connector + webhook ingest + adapter + docs | 3–5 days |
| 3 | Identity engine + 4 new intel tabs + cross‑platform APIs | 6–10 days |
| 4 | Polish, tests, hardening, docs | 3–5 days |
| | **Total** | **~3–5 weeks** of focused work |

(If only the Bot‑API flavor of Telegram and a thin Baileys WhatsApp service are acceptable, Phases 1–2 shrink; the MTProto userbot is the bigger lift but the higher‑fidelity, more "Signal‑like" option.)

---

## 10. Risks, caveats & open questions

* **ToS / account bans (high).** Telegram userbots and WhatsApp linked‑device automation violate the platforms' ToS and risk the underlying account being banned. Keep the project's "demo against your own groups, with consent" framing front‑and‑center; add per‑platform warnings; consider a throwaway account for demos.
* **Legality (must‑confirm).** Observing groups you're not a member of, or retaining others' messages beyond what they expect, can be unlawful depending on jurisdiction and consent. Same posture as the existing Signal README: authorized, self‑observed only.
* **Telegram Bot API privacy mode** limits group visibility; the MTProto userbot avoids it but is higher‑risk. Document the trade‑off; let the operator choose per deployment (`TG_MODE=userbot|bot`).
* **WhatsApp "view once" / disappearing messages** are received while the device is linked — that's precisely the privacy point the demo makes, but call it out.
* **Identity false positives.** Never auto‑merge on weak signals. Auto‑confirm only on `phone_exact` (or ≥ `IDENTITY_AUTOCONFIRM_THRESHOLD`); everything else stays `proposed` until a human confirms. Always keep the `evidence` JSON.
* **ID normalization edge cases.** Telegram int IDs (huge negatives), WhatsApp JIDs, mixed phone formats (`+E.164` vs bare digits), Signal UUID‑only users with no phone. The `url_norm`/phone‑norm helpers and wide `varchar(190)` columns mitigate this; needs unit tests.
* **Query/view audit.** ~40 `/api/intel/*` endpoints + several templates read `messages` directly. Phase 0 keeps `group_id`/`group_name`/`sender_phone`/`sender_name` semantically intact so nothing breaks, but each endpoint should be eyeballed and, where it makes sense, gain a `platform` filter.
* **Volume / storage.** Adding two more platforms multiplies message + screenshot + page‑snapshot volume; revisit BLOB storage (`messages.screenshot`, `page_snapshots.html_content`, `attachments.file_content`) and retention. Possibly move BLOBs to disk/object storage in Phase 4.
* **Open questions for the operator:**
  1. Telegram: userbot (full visibility, higher risk) or Bot API (limited, low risk) — or selectable per deployment? *(Plan assumes selectable, default userbot.)*
  2. WhatsApp: WAHA (fastest, drop‑in) vs a self‑built Baileys service (more control, more code)? *(Plan recommends WAHA.)*
  3. Ingest transport: webhook‑only for the new platforms, or also expose pull for parity/replayability? *(Plan does pull for Telegram, webhook for WhatsApp; both go through `ingest_event()`.)*
  4. Do you want outbound capability (the bot reacting/sending) on the new platforms like the Signal activity tracker has, or strictly read‑only? *(Plan is read‑only for TG/WA in scope; the connector interfaces leave room to add it later.)*

---

## 11. Concrete first PR (Phase 0 checklist)

- [ ] `ensure_db_indexes()`: add `platform`+native‑id columns to `messages` (INSTANT ALTER) and `idx_msg_platform_dedup`, `idx_msg_platform`.
- [ ] `ensure_db_indexes()`: add `platform` column (default `'signal'`) to `reactions`, `message_attachments`, `message_quotes`, `message_mentions`, `message_entities`, `group_members`, `group_snapshots`, `group_membership_events`, `page_snapshots`, `sender_profiles`, `daily/monthly/yearly_summaries`, `intel_briefs`.
- [ ] `ensure_db_indexes()`: `CREATE TABLE IF NOT EXISTS chats / identities / identity_links / connector_cursors / url_observations`.
- [ ] Backfill jobs (guarded): `chats` from `group_snapshots`; `url_observations` from `messages.url`; one `identities`+`identity_links(phone_exact)` per known `sender_phone`.
- [ ] `connectors/base.py` (`CanonicalEvent`, `Adapter`), `url_norm.py`, `ingest.py` (`ingest_event`).
- [ ] `connectors/signal_adapter.py`: extract Signal HTTP from `poller.py`; `poller.run_poller` now does `events = signal_adapter.fetch_events(); for e in events: ingest_event(conn, e)` (or keep `poll_messages` and have it call `ingest_event` per envelope — smaller diff).
- [ ] `static/style.css`: `.platform-badge` (SG/TG/WA chips); render in `messages.html`, `filtered.html`, `dashboard.html`, `message_stream.html`, `search.html`.
- [ ] `/api/groups`: include `platform`; add `?platform=` to `/messages`, `/filtered`, `/search`.
- [ ] Regenerate `schema.sql`; update `README.md` ("Multi‑platform" section stub) and `.env.example`.
- [ ] Tests: `ingest_event()` idempotency; Signal envelope → canonical event → DB row equivalence vs. the old path.
