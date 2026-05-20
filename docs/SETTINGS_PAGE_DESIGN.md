# Settings page — design & implementation notes

> Status: **MVP implemented** (`/settings`, `settings.py`, `app_settings` table).
> This document is both the design rationale and the spec for the parts that are
> deliberately left for later. It supersedes nothing; the multi-platform plan in
> `MULTI_PLATFORM_INTEGRATION_PLAN.md` still describes the connectors themselves.

## 1. Problem statement

Everything in this app was configured by environment variables read once at
process start (`config.py` → `.env`). That works for a headless deployment but is
hostile to a human operator who just wants to:

- **link Signal** — today: SSH in, `curl … /v1/qrcodelink`, open the PNG, scan,
  then hand-edit `SIGNAL_PHONE_NUMBER` / `TARGET_GROUP_IDS` / the account-db path
  into `.env`, `docker compose restart`;
- **pick which groups/chats to watch** — today: `curl … /v1/groups/<number>`,
  read base64 ids out of JSON, paste them into `TARGET_GROUP_IDS`, restart;
- **set up Telegram / WhatsApp** — today: edit several `TG_*` / `WA_*` vars, read
  a QR out of `docker compose logs`, restart;
- **decide whether the bot's own messages are archived** — today: not possible at
  all, and the answer was effectively "no" (see §5).

So: a **Settings page** that does all of the above from the browser, backed by a
small writable config layer. It's intended to grow into the home for most
configuration over time; the MVP covers the integrations + the own-messages fix.

## 2. Where settings live — `app_settings` table + `settings.py`

A new MySQL table:

```sql
CREATE TABLE app_settings (
  setting_key   VARCHAR(128) PRIMARY KEY,
  setting_value TEXT,
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
```

It's in `schema.sql` for fresh installs and is also created lazily by
`settings._ensure_table()`, so existing databases pick it up with no migration
step.

`settings.py` is a thin, cached key/value overlay:

- `get / get_bool / get_int / get_list / get_set(key, default)` — typed reads;
  when the key is absent the caller's `default` (normally the value from
  `config.py`) is returned, so **deleting a row reverts to the `.env` default**.
- `save(key, value)` / `save_many({...})` — upserts; a `None` value deletes.
- Reads are cached for `_CACHE_TTL` (5 s) and the cache is force-refreshed on
  every write, so it's cheap to call inside the poll loop.
- Convenience helpers the running process calls each cycle:
  `signal_target_group_ids()`, `save_own_messages_enabled()`, `poll_interval()`.

Design choices:

| Decision | Rationale |
|---|---|
| **DB table**, not rewriting `.env` | Survives restarts, works in read-only containers, no race with manual edits, and the DB is already the single dependency every component has. |
| `config.py` stays the **default layer** | The DB is the *overlay*; if MySQL is down or a key is unset, behaviour is exactly the pre-existing env-var behaviour. Nothing regresses. |
| **Whitelist** of writable keys (`settings.KNOWN_KEYS`, mirrored in `app.py`'s `_SETTINGS_WRITABLE_KEYS`) | The `POST /api/settings` handler ignores anything not on the list, so a stray field can't poison config. |
| No secret encryption in the MVP | The Telegram bot token is stored in plaintext, same trust level as `.env`/the DB already has. If that's not acceptable, keep the token in `.env` and don't enter it in the UI — the UI never *requires* it (blank = keep current). A follow-up could add app-level encryption keyed off `AUTH_SECRET`. |

## 3. What's live-reloadable vs. restart-required

| Setting (`app_settings` key) | Effect | Applies |
|---|---|---|
| `save_own_messages` | poller archives the bot account's own sent messages | **immediately** (read each poll cycle) |
| `signal_target_group_ids` | which Signal groups the poller keeps | **immediately** (read each poll cycle; overrides `TARGET_GROUP_IDS`) |
| `poll_interval` | seconds between Signal poll cycles | **immediately** (used for the loop's sleep) |
| `telegram_target_chat_ids` | which Telegram chats to ingest | next ingest pass picks it up if the connector code reads it; otherwise next restart |
| `whatsapp_target_chat_ids` | which WhatsApp chats to ingest | as above |
| `telegram_enabled` / `whatsapp_enabled` | whether the connector poller/sync threads run | **restart required** — the threads are spawned once in `app.main()` |
| `telegram_bot_token` | the token the `tg-connector` sidecar authenticates with | **restart required** — the sidecar reads it at start |

The `POST /api/settings` response includes `restart_required: [...]` listing any
saved keys in the restart bucket, and the UI surfaces that in its toast.

> Note on `*_target_chat_ids`: the Signal poller already re-reads its target set
> each cycle (now via `settings.signal_target_group_ids()`). The Telegram/WhatsApp
> ingest paths in `connector_runtime.py` / `ingest.py` currently take "no filter
> = all chats"; wiring them to honour `*_target_chat_ids` live is a small
> follow-up (read the set at the top of `connector_poller_loop`'s cycle and skip
> events whose `chat.platform_chat_id` isn't in it).

## 4. Integration setup flows

### 4.1 Signal — QR device linking

`signal-cli-rest-api` (bbernhard) is the only moving part. Endpoints used:

- `GET /v1/accounts` → `["+358…"]` — is the daemon holding a session? Is *our*
  number among them? → drives the "LINKED / not linked" badge.
- `GET /v1/about` → version / mode — informational.
- `GET /v1/qrcodelink?device_name=…` → a **PNG** of a linking QR. The operator
  opens Signal → Settings → Linked devices → "Link new device" → scans it; the
  daemon becomes a *linked device* of the account. (Single-use, expires fast — the
  UI lets you re-request.)
- `GET /v1/groups/<number-url-encoded>` → array of groups, each with `id`
  (base64), `internal_id` (hex), `name`, `members`, `blocked`, `invite_link`, …

The app proxies the QR and the group list (so the browser only ever talks to the
app, not directly to the signal daemon): `/api/settings/signal/{status,qrlink,groups}`.

**Group-id gotcha:** the poller filters incoming `dataMessage.groupInfo.groupId`,
which is the **base64** `id`, against the target set — so the picker stores
`id`, *not* `internal_id`. (`connectors/signal_adapter.py`'s `list_chats()`
happens to use `internal_id` for the `chats` registry; that pre-existing
inconsistency is out of scope here. The picker is consistent with the poller.)

**Registering a brand-new number** (`POST /v1/register/<number>` →
`POST /v1/verify/<number>/<code>`) is *not* in the MVP — linking an existing
account is the common case and far simpler. Adding a "register a new number"
sub-flow later is straightforward (two form fields + two proxied POSTs).

### 4.2 Telegram — bot token, no QR

Telegram is read through the **Bot API** (chosen for stealth — see the
multi-platform plan), so there is **no QR**. Flow:

1. Operator creates a bot with @BotFather, copies the token, and disables privacy
   mode (`/setprivacy` → Disable) so the bot sees all group messages.
2. In Settings → Telegram: paste the token (stored as `telegram_bot_token`),
   tick "enable the Telegram connector" (`telegram_enabled`), save. → both are in
   the restart bucket; the operator restarts the stack so the `tg-connector`
   picks up the token.
3. With the connector running, "Fetch chats" hits `GET {TG_CONNECTOR_BASE}/v1/chats`
   (the same endpoint `connectors/telegram_adapter.list_chats()` uses), shows a
   checklist, and saves the selection as `telegram_target_chat_ids`.

App endpoints: `/api/settings/telegram/{status,chats}` (+ the generic
`/api/settings` POST for the token/enable flag). `status` probes the connector
for `/healthz` `/v1/health` `/status` `/` (whichever answers).

### 4.3 WhatsApp — QR device pairing via the connector

WhatsApp goes through the `wa-connector` Baileys sidecar, which pairs as a
linked device of a real WhatsApp account. Flow:

1. In Settings → WhatsApp: tick "enable the WhatsApp connector"
   (`whatsapp_enabled`), save → restart bucket; restart the stack.
2. "Get pairing QR" proxies the connector's QR (`/qr.png` → `/qr` → `/v1/qr`,
   first that answers). The connector may serve it as a PNG **or** as JSON
   (`{"qr": "<data-uri | raw string>"}`); the UI handles either (renders an
   `<img>`, or the raw string in a `<pre>` as an ASCII QR fallback).
3. Operator scans it from WhatsApp → Settings → Linked devices → Link a device.
   Session lives in the `wa-session` volume — pair once.
4. "Fetch chats" hits `GET {WA_CONNECTOR_BASE}/v1/chats` (WhatsApp can enumerate
   participants), checklist → `whatsapp_target_chat_ids`.

App endpoints: `/api/settings/whatsapp/{status,qr,chats}`. All connector calls
are best-effort and degrade to a friendly "connector unreachable / not built yet"
message — the WhatsApp connector image is a later phase.

### 4.4 Why proxy everything through the app

The browser never talks to `signal-api` / `tg-connector` / `wa-connector`
directly: those are on the internal Docker network, may be unreachable from the
operator's browser, and authenticate with bearer tokens the page shouldn't hold.
Every Settings call is `browser → /api/settings/* → sidecar`, with the app adding
`Authorization` headers from `config` where needed and clamping timeouts.

## 5. The "own messages" fix

**Bug:** messages the bot's *own* Signal account sent were never stored. They
don't arrive as `dataMessage` — they come back from the account's other linked
devices as `envelope.syncMessage.sentMessage`, and `poll_messages()` explicitly
dropped every non-`dataMessage` envelope ("ignore syncMessage/typingMessage").
So the operator's own contributions were invisible in every dashboard, the social
graph, dossiers, summaries, etc.

**Fix** (`poller.py`):

- In `poll_messages()`, when an envelope has no `dataMessage`, also look for
  `syncMessage.sentMessage` and — if `settings.save_own_messages_enabled()` (the
  default) — hand it to the new `insert_own_sent_message()`.
- `insert_own_sent_message()` mirrors the inbound path: only **group** messages
  to a **monitored** group are stored; the row's author is the bot's own
  number/UUID and `sourceName`; text/urls/attachments/quotes/mentions are
  extracted the same way; URLs get a screenshot + page snapshot + `url_observations`
  row + `tracked_urls` entry just like inbound URLs; `platform_msg_id` is
  `{uuid}:{sentMessage.timestamp}` (unique, so `INSERT IGNORE` makes re-polls
  idempotent). Sent **reactions** and **remote-deletes** also come through
  `sentMessage` and are skipped (reactions intentionally stay out of the
  `reactions` table — same as the existing self-reaction filter).
- Group name backfill: `sentMessage.groupInfo` rarely carries the name, so
  `_resolve_signal_group_name()` looks it up from prior `messages` rows →
  `group_snapshots` → `chats`, falling back to `"Unknown"`.
- New `app_settings` key `save_own_messages` (default **true**) flips it; the
  Settings → General toggle writes it; the poller reads it each cycle.

**Caveat:** this only works when the bot runs as a *linked device* of the
account (the normal `signal-cli-rest-api` setup). If it were the *primary*
device there are no self-syncs to capture — but then it also wouldn't be
"the bot's own messages" in the sense people mean.

**Telegram/WhatsApp own messages:** the Bot API never echoes the bot's own sends
back as updates, so there's nothing to capture there. A WhatsApp linked device
*does* see `fromMe` messages; honouring `save_own_messages` for those is a small
follow-up in the WhatsApp connector + `ingest._ingest_message()` (don't drop
`fromMe`).

## 6. HTTP surface (implemented)

| Method & path | Purpose |
|---|---|
| `GET /settings` | the page (`templates/settings.html`) |
| `GET /api/settings` | effective snapshot (DB overlay on top of `config.py`) |
| `POST /api/settings` | write whitelisted keys; returns `{ok, saved, restart_required, snapshot}` |
| `GET /api/settings/signal/status` | reachable? registered? known accounts; `/v1/about` |
| `GET /api/settings/signal/qrlink?device_name=…` | proxied linking-QR PNG |
| `GET /api/settings/signal/groups` | group list + which are currently monitored |
| `POST /api/settings/signal/groups` `{group_ids:[…]}` | set `signal_target_group_ids` |
| `GET /api/settings/telegram/status` | connector reachable? enabled? token set? |
| `GET /api/settings/telegram/chats` | chat list + selection |
| `POST /api/settings/telegram/chats` `{chat_ids:[…]}` | set `telegram_target_chat_ids` |
| `GET /api/settings/whatsapp/status` | connector reachable? enabled? linked? |
| `GET /api/settings/whatsapp/qr` | proxied pairing QR (PNG or JSON) |
| `GET /api/settings/whatsapp/chats` | chat list + selection |
| `POST /api/settings/whatsapp/chats` `{chat_ids:[…]}` | set `whatsapp_target_chat_ids` |

All endpoints sit behind the existing `before_request` auth gate (active only
when `AUTH_SECRET` is set). The page itself is a static `templates/settings.html`
+ vanilla JS that talks to the above; sidebar gets a new "System → Settings" link.

## 7. Security / hardening notes

- **Auth:** the Settings page is as protected as the rest of the dashboard — i.e.
  only if `AUTH_SECRET` is set. **Anyone who can reach the dashboard can re-link
  Signal / WhatsApp and read a fresh linking QR.** That's the same exposure the
  rest of the surveillance UI already has, but it's worth calling out: if this is
  reachable beyond localhost, set `AUTH_SECRET`. A future improvement: a separate,
  stronger gate (or read-only-by-default) for the integration controls.
- **SSRF surface:** the proxy endpoints only ever call the *configured*
  `SIGNAL_API_BASE` / `TG_CONNECTOR_BASE` / `WA_CONNECTOR_BASE` with fixed paths;
  no user-supplied URL is fetched. `device_name` is the only user input that
  reaches a sidecar and it's a harmless query param.
- **Plaintext token:** see §2 — acceptable at current trust level; isolate it in
  `.env` if not.
- **No CSRF token** on the POST endpoints — consistent with the rest of the app's
  existing POST endpoints (`/api/intel/*` etc.). If CSRF protection is added it
  should be app-wide, not just here.

## 8. Roadmap (not in the MVP)

- Wire `telegram_target_chat_ids` / `whatsapp_target_chat_ids` into the live
  ingest filter (currently restart-effective at best).
- Honour `save_own_messages` for WhatsApp `fromMe` messages.
- Signal "register a new number" sub-flow (register + verify).
- Move more `config.py` knobs into the page (Ollama models/timeouts, summary
  interval, activity-tracker enable + enrolment, watchlist seed, intel tuning).
- Per-setting "reset to default" buttons (delete the `app_settings` row).
- Optional encryption of secret-typed settings (key derived from `AUTH_SECRET`).
- A stricter auth tier (or audit log) for the integration controls.
- "Export current settings to `.env`" for portability.
