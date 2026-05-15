#!/bin/bash
set -euo pipefail

# --- Config ---
VPS_USER="root"
VPS_HOST="69.62.110.165"
VPS_DIR="/opt/DETECTIVE"

# --- 1. Pull latest code on VPS ---
echo ">>> Pulling latest code on VPS ..."
ssh "${VPS_USER}@${VPS_HOST}" "cd ${VPS_DIR} && git pull origin main"

# --- 2. Sync data/ (DB SQLite + agent_state) ---
echo ">>> Syncing data/ ..."
rsync -avz --delete ./data/ "${VPS_USER}@${VPS_HOST}:${VPS_DIR}/data/"

# --- 3. Sync .env as .env.production ---
echo ">>> Syncing .env as .env.production ..."
rsync -avz ./.env "${VPS_USER}@${VPS_HOST}:${VPS_DIR}/.env.production"

# --- 4. Build & run ---
echo ">>> Building and starting container ..."
ssh "${VPS_USER}@${VPS_HOST}" "cd ${VPS_DIR} && docker compose up -d --build"

# --- 5. Check status ---
echo ">>> Container status :"
ssh "${VPS_USER}@${VPS_HOST}" "cd ${VPS_DIR} && docker compose ps && docker compose logs --tail 20"

echo ""
echo "✅ Déploiement terminé !"
echo "   Cockpit : https://detective.digitalhs.biz"
echo "   Pour suivre les logs : ssh ${VPS_USER}@${VPS_HOST} 'cd ${VPS_DIR} && docker compose logs -f'"
