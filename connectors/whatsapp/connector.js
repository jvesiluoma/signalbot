/**
 * WhatsApp connector for signalbot — built on Baileys (WhatsApp Web multi-device).
 *
 * Runs in its own container; the linked-device session lives in SESSION_DIR
 * (a Docker volume) and never leaves it. Read-only and low-profile by default:
 *   - markOnlineOnConnect: false
 *   - never sends read/delivery receipts
 *   - never broadcasts presence/typing
 *   - never auto-subscribes to other users' presence
 * The only time it sends anything is when WA_ACTIVITY_TRACKER_ENABLED=1 and the
 * app calls POST /v1/probe (transient reaction / presence subscription).
 *
 * Normalizes WhatsApp events into the project's CanonicalEvent JSON shape and
 * POSTs each to INGEST_URL (push transport). Also keeps a small ring buffer
 * exposed at GET /v1/events?since= for replay.
 *
 * HTTP API (Bearer <WA_API_KEY> on /v1/*):
 *   GET  /healthz
 *   GET  /qr                         -> HTML page with the pairing QR
 *   GET  /v1/auth/qr                 -> { qr, dataUrl, connected }
 *   GET  /v1/me                      -> { id, name }
 *   GET  /v1/events?since=&limit=     -> { events:[...], next_cursor }
 *   GET  /v1/chats                    -> [ { id, title, kind, members_count, is_public } ]
 *   GET  /v1/chats/:jid/participants  -> [ { id, phone, display_name, role } ]
 *   GET  /v1/files/:mediaId           -> raw decrypted media bytes
 *   POST /v1/probe                    -> { ok } (activity probe; gated by WA_ACTIVITY_TRACKER_ENABLED)
 */

import express from 'express';
import pino from 'pino';
import QRCode from 'qrcode';
import { existsSync, mkdirSync, readdirSync, unlinkSync, readFileSync, writeFileSync } from 'fs';
import { join as pathJoin } from 'path';
import {
  default as makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  DisconnectReason,
  downloadMediaMessage,
  jidNormalizedUser,
} from '@whiskeysockets/baileys';

const log = pino({ level: process.env.LOG_LEVEL || 'info' });

const PORT          = parseInt(process.env.CONNECTOR_PORT || '8082', 10);
const SESSION_DIR   = process.env.SESSION_DIR || '/data/session';
const WA_API_KEY    = (process.env.WA_API_KEY || '').trim();
const CONNECTOR_ID  = process.env.CONNECTOR_ID || 'wa-1';
const INGEST_URL    = (process.env.INGEST_URL || '').trim();
const INGEST_TOKEN  = (process.env.INGEST_WEBHOOK_TOKEN || '').trim();
const TARGET_CHATS  = new Set((process.env.WA_TARGET_CHAT_IDS || '').split(',').map(s => s.trim()).filter(Boolean));
const ACTIVITY_ON   = (process.env.WA_ACTIVITY_TRACKER_ENABLED || '0') === '1';
const EVENT_BUFFER  = parseInt(process.env.WA_EVENT_BUFFER || '5000', 10);

if (!existsSync(SESSION_DIR)) mkdirSync(SESSION_DIR, { recursive: true });

// ── shared state ──
let sock = null;
let me = null;
let lastQR = null;            // raw QR string from Baileys
let connected = false;
let resetting = false;        // true while /v1/auth/reset is wiping & restarting
let seq = 0;                  // monotonically increasing event cursor
const events = [];            // [{ seq, ev }]
const chats = new Map();      // jid -> { id, title, kind, members_count, is_public }
const mediaCache = new Map(); // mediaId -> { msg }  (for /v1/files)
const MEDIA_CACHE_MAX = 2000;

// The chats Map is the only source of group *titles* for outgoing events, but
// Baileys only delivers group metadata reactively. Persist it across restarts
// so a freshly-restarted connector doesn't emit messages with null titles
// (which the app would store as "Unknown") until it relearns every subject.
const CHATS_FILE = pathJoin(SESSION_DIR, 'chats.json');
let persistTimer = null;
function loadChatsFromDisk() {
  try {
    if (!existsSync(CHATS_FILE)) return;
    const arr = JSON.parse(readFileSync(CHATS_FILE, 'utf8'));
    if (Array.isArray(arr)) for (const rec of arr) if (rec && rec.id) chats.set(rec.id, rec);
    log.info({ chats: chats.size }, 'loaded chats from disk');
  } catch (e) { log.warn({ err: String(e) }, 'loadChatsFromDisk failed'); }
}
function persistChatsNow() {
  try { writeFileSync(CHATS_FILE, JSON.stringify([...chats.values()]), 'utf8'); }
  catch (e) { log.warn({ err: String(e) }, 'persistChats failed'); }
}
function schedulePersistChats() {
  if (persistTimer) return;
  persistTimer = setTimeout(() => { persistTimer = null; persistChatsNow(); }, 2000);
}
loadChatsFromDisk();

function pushEvent(ev) {
  seq += 1;
  events.push({ seq, ev });
  while (events.length > EVENT_BUFFER) events.shift();
  if (INGEST_URL) {
    fetch(INGEST_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(INGEST_TOKEN ? { Authorization: `Bearer ${INGEST_TOKEN}` } : {}) },
      body: JSON.stringify(ev),
    }).catch(e => log.warn({ err: String(e) }, 'ingest push failed'));
  }
}

const URL_RE = /https?:\/\/\S+/g;

function isGroup(jid) { return typeof jid === 'string' && jid.endsWith('@g.us'); }
function isUser(jid)  { return typeof jid === 'string' && jid.endsWith('@s.whatsapp.net'); }
function phoneFromJid(jid) {
  if (!jid || typeof jid !== 'string') return null;
  const num = jid.split('@')[0].split(':')[0];
  return /^\d{7,15}$/.test(num) ? '+' + num : null;
}
function rememberChat(jid, title, membersCount) {
  if (!jid) return;
  const existing = chats.get(jid);
  const rec = existing || { id: jid };
  rec.id = jid;
  rec.kind = isGroup(jid) ? 'group' : 'dm';
  if (title) rec.title = title;
  if (typeof membersCount === 'number') rec.members_count = membersCount;
  rec.is_public = false;
  chats.set(jid, rec);
  // Only touch the disk when something durable actually changed — not on the
  // title-less rememberChat() that fires for every inbound message.
  const changed = !existing
    || (title && existing.title !== rec.title)
    || (typeof membersCount === 'number' && existing.members_count !== rec.members_count);
  if (changed) schedulePersistChats();
}
// On (re)connect, proactively pull every participating group's metadata so the
// chats Map has real subjects immediately — Baileys won't push groups.upsert
// for already-known groups when syncFullHistory is off, so without this every
// message after a restart would carry a null title.
async function refreshAllGroups() {
  if (!sock) return;
  try {
    const groups = await sock.groupFetchAllParticipating();
    let n = 0;
    for (const meta of Object.values(groups || {})) {
      if (!meta || !meta.id) continue;
      rememberChat(meta.id, meta.subject, (meta.participants || []).length);
      n += 1;
    }
    persistChatsNow();
    log.info({ groups: n, chats: chats.size }, 'refreshed group metadata');
  } catch (e) {
    log.warn({ err: String(e) }, 'groupFetchAllParticipating failed');
  }
}
function cacheMedia(mediaId, msg) {
  mediaCache.set(mediaId, { msg });
  while (mediaCache.size > MEDIA_CACHE_MAX) mediaCache.delete(mediaCache.keys().next().value);
}

// Extract plain text + URLs + the message-content node from a WAMessage.
function textOf(m) {
  const c = m.message || {};
  return (
    c.conversation ||
    c.extendedTextMessage?.text ||
    c.imageMessage?.caption ||
    c.videoMessage?.caption ||
    c.documentMessage?.caption ||
    ''
  );
}
function urlsOf(text) {
  const out = [];
  for (const u of (text || '').match(URL_RE) || []) if (!out.includes(u)) out.push(u);
  return out;
}
function attachmentsOf(m, mediaIdBase) {
  const c = m.message || {};
  const out = [];
  const add = (node, type, defaultCt) => {
    if (!node) return;
    const mid = `${mediaIdBase}:${type}`;
    cacheMedia(mid, m);
    out.push({
      id: mid,
      content_type: node.mimetype || defaultCt || null,
      file_name: node.fileName || null,
      size: node.fileLength ? Number(node.fileLength) : null,
      fetch_url: `/v1/files/${encodeURIComponent(mid)}`,
    });
  };
  add(c.imageMessage, 'image', 'image/jpeg');
  add(c.videoMessage, 'video', 'video/mp4');
  add(c.audioMessage, 'audio', 'audio/ogg');
  add(c.documentMessage, 'document', null);
  add(c.stickerMessage, 'sticker', 'image/webp');
  add(c.documentWithCaptionMessage?.message?.documentMessage, 'document', null);
  return out;
}
function mentionsOf(m) {
  const ctx = m.message?.extendedTextMessage?.contextInfo;
  const ids = ctx?.mentionedJid || [];
  return ids.map(jid => ({ platform_user_id: jid, username: null }));
}
function replyOf(m, chatJid) {
  const ctx = m.message?.extendedTextMessage?.contextInfo;
  if (!ctx || !ctx.stanzaId) return null;
  return {
    platform_msg_id: `${chatJid}:${ctx.stanzaId}`,
    author_user_id: ctx.participant || null,
    text: (ctx.quotedMessage?.conversation || ctx.quotedMessage?.extendedTextMessage?.text || '')?.slice(0, 2048) || null,
  };
}

function senderRef(jid, pushName) {
  return {
    platform_user_id: jid || null,
    display_name: pushName || null,
    username: null,
    phone: phoneFromJid(jid),
  };
}

function chatRef(jid, title) {
  return {
    platform_chat_id: jid,
    title: title || chats.get(jid)?.title || null,
    kind: isGroup(jid) ? 'group' : 'dm',
    is_public: false,
    members_count: chats.get(jid)?.members_count ?? null,
  };
}

function baseEvent(type) {
  return {
    schema: 1, platform: 'whatsapp', connector_id: CONNECTOR_ID, event_type: type,
    platform_msg_id: null, timestamp_ms: null, chat: null, sender: null,
    text: null, urls: [], reply_to: null, mentions: [], attachments: [],
    reaction: null, edit_of: null, delete_of: null, raw: null,
  };
}

// A WAMessageKey: { remoteJid, fromMe, id, participant? }
function messageEvent(m) {
  const key = m.key || {};
  const chatJid = key.remoteJid;
  if (!chatJid || chatJid === 'status@broadcast') return null;
  // Only group chats are of interest — never private 1:1 chats.
  if (!isGroup(chatJid)) return null;
  // Own (outgoing) messages echoed from the linked phone are kept here; the
  // app side gates them on the `save_own_messages` toggle. Sender is "me".
  const fromMe = !!key.fromMe;
  const senderJid = fromMe ? (me?.id || null) : (key.participant || jidNormalizedUser(key.participant || ''));
  const pmid = `${chatJid}:${key.id}`;
  const text = textOf(m);
  const ts = Number(m.messageTimestamp || 0) * 1000;
  rememberChat(chatJid, undefined);
  const ev = baseEvent('message');
  ev.platform_msg_id = pmid;
  ev.timestamp_ms = ts || Date.now();
  ev.chat = chatRef(chatJid);
  ev.sender = senderRef(senderJid || chatJid, m.pushName);
  ev.text = text;
  ev.urls = urlsOf(text);
  ev.reply_to = replyOf(m, chatJid);
  ev.mentions = mentionsOf(m);
  ev.attachments = attachmentsOf(m, pmid);
  // edited message? Baileys delivers edits via messages.update; if this is an
  // editedMessage payload, mark it.
  if (m.message?.editedMessage || m.message?.protocolMessage?.type === 14 /* MESSAGE_EDIT */) {
    ev.event_type = 'edit';
    ev.edit_of = { platform_msg_id: `${chatJid}:${m.message?.protocolMessage?.key?.id || key.id}` };
  }
  ev.raw = { whatsapp_kind: 'messages.upsert', key, messageTimestamp: m.messageTimestamp, pushName: m.pushName };
  return ev;
}

function reactionEvent(m) {
  const r = m.message?.reactionMessage;
  if (!r) return null;
  const key = m.key || {};
  const chatJid = key.remoteJid;
  if (!chatJid) return null;
  const senderJid = isGroup(chatJid) ? key.participant : chatJid;
  const ev = baseEvent(r.text ? 'reaction' : 'reaction_remove');
  ev.timestamp_ms = Number(m.messageTimestamp || 0) * 1000 || Date.now();
  ev.chat = chatRef(chatJid);
  ev.sender = senderRef(senderJid || chatJid, m.pushName);
  ev.reaction = {
    emoji: r.text || '',
    target_msg_id: `${chatJid}:${r.key?.id}`,
    target_author_id: r.key?.participant || (r.key?.fromMe ? (me?.id || null) : null),
    is_remove: !r.text,
  };
  ev.raw = { whatsapp_kind: 'reaction', reactionMessage: r, key };
  return ev;
}

function deleteEvent(chatJid, msgId, byJid) {
  const ev = baseEvent('delete');
  ev.timestamp_ms = Date.now();
  ev.chat = chatRef(chatJid);
  ev.sender = senderRef(byJid || chatJid, null);
  ev.delete_of = { platform_msg_id: `${chatJid}:${msgId}` };
  ev.raw = { whatsapp_kind: 'messages.update.revoke' };
  return ev;
}

const PART_ACTION_EVENT = { add: 'join', remove: 'leave', promote: 'admin_grant', demote: 'admin_revoke' };
function membershipEvents(update) {
  const { id: chatJid, participants, action } = update;
  const et = PART_ACTION_EVENT[action];
  if (!et || !chatJid) return [];
  rememberChat(chatJid, undefined);
  return (participants || []).map(jid => {
    const ev = baseEvent(et);
    ev.timestamp_ms = Date.now();
    ev.chat = chatRef(chatJid);
    ev.sender = senderRef(jid, null);
    ev.text = action;
    ev.raw = { whatsapp_kind: 'group-participants.update', update };
    return ev;
  });
}

function maybeEmit(ev) {
  if (!ev) return;
  // Never surface private 1:1 chats — group chats only.
  if (ev.chat && (ev.chat.kind === 'dm' || (ev.chat.platform_chat_id && !ev.chat.platform_chat_id.endsWith('@g.us')))) return;
  if (TARGET_CHATS.size && ev.chat && !TARGET_CHATS.has(ev.chat.platform_chat_id)) return;
  pushEvent(ev);
}

// ──────────────────────────────────────────────
// Baileys socket
// ──────────────────────────────────────────────

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: undefined }));
  sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: true,
    markOnlineOnConnect: false,
    syncFullHistory: false,
    logger: pino({ level: 'silent' }),
    emitOwnEvents: false,
    getMessage: async () => undefined,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) { lastQR = qr; log.info('QR updated — open /qr to scan'); }
    if (connection === 'open') {
      connected = true; lastQR = null; me = sock.user || null;
      log.info({ me }, 'WhatsApp connected');
      refreshAllGroups();
    } else if (connection === 'close') {
      connected = false;
      const code = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = code === DisconnectReason.loggedOut;
      log.warn({ code, loggedOut }, 'WhatsApp connection closed');
      // While /v1/auth/reset is wiping the session and re-starting, this close
      // event is *expected* — the reset path will call start() itself, so
      // suppress the auto-reconnect to avoid a double-start race.
      if (!loggedOut && !resetting) setTimeout(() => start().catch(e => log.error({ err: String(e) }, 'reconnect failed')), 3000);
    }
  });

  sock.ev.on('messages.upsert', ({ messages, type }) => {
    if (type !== 'notify' && type !== 'append') return;
    for (const m of messages) {
      try {
        if (m.message?.reactionMessage) { maybeEmit(reactionEvent(m)); continue; }
        if (m.message?.protocolMessage?.type === 0 /* REVOKE */) {
          const pm = m.message.protocolMessage;
          maybeEmit(deleteEvent(m.key.remoteJid, pm.key?.id, m.key.participant));
          continue;
        }
        maybeEmit(messageEvent(m));
      } catch (e) { log.warn({ err: String(e) }, 'messages.upsert handler error'); }
    }
  });

  sock.ev.on('messages.update', (updates) => {
    for (const up of updates) {
      try {
        const key = up.key || {};
        // revoke / delete
        const pm = up.update?.message?.protocolMessage;
        if (pm?.type === 0) { maybeEmit(deleteEvent(key.remoteJid, pm.key?.id || key.id, key.participant)); continue; }
        if (up.update?.messageStubType === 1 /* REVOKE */) { maybeEmit(deleteEvent(key.remoteJid, key.id, key.participant)); continue; }
        // edit
        const edited = up.update?.message?.editedMessage?.message || pm?.editedMessage?.message;
        if (edited && key.remoteJid) {
          const ev = baseEvent('edit');
          ev.platform_msg_id = `${key.remoteJid}:${key.id}`;
          ev.edit_of = { platform_msg_id: `${key.remoteJid}:${key.id}` };
          ev.timestamp_ms = Date.now();
          ev.chat = chatRef(key.remoteJid);
          ev.sender = senderRef(isGroup(key.remoteJid) ? key.participant : key.remoteJid, null);
          ev.text = edited.conversation || edited.extendedTextMessage?.text || '';
          ev.urls = urlsOf(ev.text);
          ev.raw = { whatsapp_kind: 'messages.update.edit', update: up };
          maybeEmit(ev);
        }
      } catch (e) { log.warn({ err: String(e) }, 'messages.update handler error'); }
    }
  });

  sock.ev.on('group-participants.update', (update) => {
    try { membershipEvents(update).forEach(maybeEmit); } catch (e) { log.warn({ err: String(e) }, 'gp.update error'); }
  });

  sock.ev.on('groups.upsert', (gs) => { for (const g of gs) rememberChat(g.id, g.subject, (g.participants || []).length); });
  sock.ev.on('groups.update', (gs) => { for (const g of gs) if (g.id) rememberChat(g.id, g.subject); });
  sock.ev.on('chats.upsert', (cs) => { for (const c of cs) rememberChat(c.id, c.name); });

  // Activity probe receipts (Phase 4): when enabled we send transient reactions
  // and time the delivery receipts.
  if (ACTIVITY_ON) {
    sock.ev.on('messages.receipt.update', (rcs) => {
      for (const rc of rcs) {
        try {
          const ev = baseEvent('activity');
          ev.timestamp_ms = Date.now();
          ev.chat = chatRef(rc.key?.remoteJid || '');
          ev.sender = senderRef(rc.key?.participant || rc.userJid || rc.key?.remoteJid, null);
          ev.raw = { whatsapp_kind: 'messages.receipt.update', receipt: rc };
          ev.text = rc.receipt?.readTimestamp ? 'read' : (rc.receipt?.receiptTimestamp ? 'delivered' : 'receipt');
          maybeEmit(ev);
        } catch (e) { log.warn({ err: String(e) }, 'receipt handler error'); }
      }
    });
  }
}

// ──────────────────────────────────────────────
// HTTP API
// ──────────────────────────────────────────────

const app = express();
app.use(express.json({ limit: '2mb' }));
app.use((req, res, next) => {
  if (req.path.startsWith('/v1/') && WA_API_KEY) {
    if (req.headers.authorization !== `Bearer ${WA_API_KEY}`) return res.status(401).json({ error: 'unauthorized' });
  }
  next();
});

app.get('/healthz', (req, res) => res.json({ ok: true, connected, me: me?.id || null, buffered_events: events.length, chats: chats.size }));

app.get('/qr', async (req, res) => {
  if (connected) return res.send('<h2>WhatsApp connector</h2><p>Connected.</p>');
  if (!lastQR) return res.send('<h2>WhatsApp connector</h2><p>No QR yet — wait a moment and refresh.</p>');
  try {
    const dataUrl = await QRCode.toDataURL(lastQR);
    res.send(`<h2>WhatsApp connector — scan to link</h2><img src="${dataUrl}" alt="QR"/><p>WhatsApp → Linked Devices → Link a Device</p>`);
  } catch (e) { res.status(500).send('QR render failed: ' + String(e)); }
});

app.get('/v1/auth/qr', async (req, res) => {
  let dataUrl = null;
  if (lastQR) { try { dataUrl = await QRCode.toDataURL(lastQR); } catch (_e) { /* ignore */ } }
  res.json({ qr: lastQR, dataUrl, connected });
});

// Wipe the linked-device session and start a fresh Baileys socket so a new QR
// is emitted. Used by the /settings page when pairing is stuck — e.g. the
// device was unlinked from the phone but the connector still tries to resume
// with the now-invalid creds and never falls back to QR pairing on its own.
app.post('/v1/auth/reset', async (req, res) => {
  if (resetting) return res.status(409).json({ error: 'reset already in progress' });
  resetting = true;
  const prevSock = sock;
  try {
    // 1. Tell WhatsApp to deauthorize this device cleanly (best-effort; if it
    //    can't reach the server we still proceed — the on-disk wipe is what
    //    matters for getting a fresh QR).
    if (prevSock && connected) {
      try { await prevSock.logout(); } catch (e) { log.warn({ err: String(e) }, 'logout failed (continuing)'); }
    }
    // 2. Stop the existing socket so it can't keep writing creds back to disk
    //    while we wipe it.
    if (prevSock) { try { prevSock.end?.(undefined); } catch (_e) { /* ignore */ } }
    sock = null;

    // 3. Wipe SESSION_DIR. We delete files individually rather than the
    //    directory itself because it's a Docker volume mount point.
    let wiped = 0;
    try {
      for (const name of readdirSync(SESSION_DIR)) {
        try { unlinkSync(pathJoin(SESSION_DIR, name)); wiped += 1; } catch (e) { log.warn({ err: String(e), name }, 'unlink failed'); }
      }
    } catch (e) {
      log.error({ err: String(e) }, 'session dir wipe failed');
      return res.status(500).json({ error: 'session wipe failed: ' + String(e) });
    }

    // 4. Reset in-memory state and start a fresh socket. The new QR appears
    //    via the connection.update handler within a few seconds; the client
    //    polls /v1/auth/qr to pick it up.
    me = null; lastQR = null; connected = false;
    start().catch(e => log.error({ err: String(e) }, 'post-reset start failed'));

    log.info({ wiped }, 'WhatsApp session reset — fresh QR incoming');
    res.json({ ok: true, wiped });
  } finally {
    resetting = false;
  }
});

app.get('/v1/me', (req, res) => res.json({ id: me?.id || null, name: me?.name || null, connected }));

app.get('/v1/events', (req, res) => {
  const since = parseInt(req.query.since || '0', 10) || 0;
  const limit = Math.min(parseInt(req.query.limit || '1000', 10) || 1000, 5000);
  const sel = events.filter(e => e.seq > since).slice(0, limit);
  res.json({ events: sel.map(e => e.ev), next_cursor: sel.length ? sel[sel.length - 1].seq : since });
});

app.get('/v1/chats', (req, res) => res.json([...chats.values()]));

app.get('/v1/chats/:jid/participants', async (req, res) => {
  const jid = req.params.jid;
  if (!isGroup(jid) || !sock) return res.json([]);
  try {
    const meta = await sock.groupMetadata(jid);
    rememberChat(jid, meta.subject, (meta.participants || []).length);
    res.json((meta.participants || []).map(p => ({
      id: p.id, phone: phoneFromJid(p.id), display_name: null,
      role: p.admin === 'superadmin' || p.admin === 'admin' ? 'admin' : 'member',
    })));
  } catch (e) { res.json({ error: String(e), participants: [] }); }
});

app.get('/v1/files/:mediaId', async (req, res) => {
  const mediaId = decodeURIComponent(req.params.mediaId);
  const entry = mediaCache.get(mediaId);
  if (!entry) return res.status(404).json({ error: 'media not in cache (too old or never seen)' });
  try {
    const buf = await downloadMediaMessage(entry.msg, 'buffer', {}, { reuploadRequest: sock.updateMediaMessage });
    res.setHeader('Content-Type', 'application/octet-stream');
    res.end(buf);
  } catch (e) { res.status(502).json({ error: String(e) }); }
});

// Activity probe (Phase 4): app posts { target_jid, chat_jid, target_msg_id?, emoji? }.
app.post('/v1/probe', async (req, res) => {
  if (!ACTIVITY_ON) return res.status(403).json({ error: 'WA_ACTIVITY_TRACKER_ENABLED is 0' });
  if (!sock || !connected) return res.status(503).json({ error: 'not connected' });
  const { chat_jid, target_jid, target_msg_id, emoji } = req.body || {};
  try {
    if (target_msg_id && chat_jid) {
      const key = { remoteJid: chat_jid, id: String(target_msg_id).split(':').pop(), fromMe: false, participant: target_jid };
      const e = emoji || '\u{1FAE5}';   // 🫥 — inconspicuous
      await sock.sendMessage(chat_jid, { react: { text: e, key } });
      setTimeout(() => sock.sendMessage(chat_jid, { react: { text: '', key } }).catch(() => {}), 1500);
      return res.json({ ok: true, mode: 'reaction' });
    }
    if (target_jid) { await sock.presenceSubscribe(target_jid); return res.json({ ok: true, mode: 'presence' }); }
    res.status(400).json({ error: 'need target_jid or (chat_jid + target_msg_id)' });
  } catch (e) { res.status(502).json({ error: String(e) }); }
});

app.listen(PORT, () => log.info(`wa-connector HTTP on :${PORT}`));
start().catch(e => { log.error({ err: String(e) }, 'failed to start Baileys'); process.exit(1); });
