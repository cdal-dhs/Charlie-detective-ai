#!/bin/bash
set -euo pipefail

# --- Config ---
VPS_USER="root"
VPS_HOST="69.62.110.165"
VPS_DIR="/opt/DETECTIVE"

# --- 0. Pre-flight checks ---
echo ">>> Pre-flight checks ..."

# Vérifier qu'on est sur main
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "main" ]]; then
    echo "❌ ERREUR : tu n'es pas sur la branche main (actuellement : $CURRENT_BRANCH)."
    exit 1
fi

# Vérifier qu'il n'y a pas de modifications non-commitées
if ! git diff-index --quiet HEAD --; then
    echo "❌ ERREUR : il reste des modifications non commitées. Fais un git add + git commit d'abord."
    git status --short
    exit 1
fi

# Vérifier qu'il n'y a pas de commits non-poussés
UNPUSHED=$(git log origin/main..HEAD --oneline 2>/dev/null || echo "")
if [[ -n "$UNPUSHED" ]]; then
    echo ">>> Commits locaux non poussés détectés :"
    echo "$UNPUSHED"
    echo ">>> Push automatique en cours ..."
    git push origin main
fi

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
