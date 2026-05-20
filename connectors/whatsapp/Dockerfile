# WhatsApp connector — Baileys (WhatsApp Web multi-device). No headless browser.
#
# Two-stage build: Baileys pulls `libsignal` straight from GitHub (a git: URL)
# and that package builds a small native addon, so the install stage needs
# `git` + a C/C++ toolchain. The runtime stage carries only node_modules and the
# app, so the final image stays slim and toolchain-free.

# ── install stage ────────────────────────────────────────────────────────────
FROM node:20-slim AS deps
WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates python3 make g++ \
 && rm -rf /var/lib/apt/lists/*
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

# ── runtime stage ────────────────────────────────────────────────────────────
FROM node:20-slim
ENV NODE_ENV=production
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY package.json connector.js ./

EXPOSE 8082
VOLUME ["/data/session"]
CMD ["node", "connector.js"]
