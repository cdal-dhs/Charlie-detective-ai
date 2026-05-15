#!/bin/bash
set -euo pipefail

# --- Config ---
VPS_USER="root"
VPS_HOST="69.62.110.165"
VPS_DIR="/opt/DETECTIVE"

# --- 1. Sync code (sans data, venv, secrets) ---
echo ">>> Syncing code to ${VPS_USER}@${VPS_HOST}:${VPS_DIR} ..."
rsync -avz --delete \
  --exclude='venv' \
  --exclude='.venv' \
  --exclude='.git' \
  --exclude='data' \
  --exclude='.env' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.ruff_cache' \
  --exclude='.DS_Store' \
  --exclude='*.png' \
  --exclude='*.jpg' \
  --exclude='logs' \
  ./ "${VPS_USER}@${VPS_HOST}:${VPS_DIR}/"

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
