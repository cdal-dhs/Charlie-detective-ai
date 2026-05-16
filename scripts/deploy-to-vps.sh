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

# Vérifier que les répertoires montés dans docker-compose.yml existent (INC-001)
echo ">>> Vérification des répertoires montés (docker-compose.yml) ..."
MOUNTED_DIRS=$(grep -oP '(?<=\- \./)[^:]+' docker-compose.yml | sort -u || true)
for dir in $MOUNTED_DIRS; do
    # Ignorer les fichiers (ex: .env.production)
    if [[ -f "$dir" ]]; then
        continue
    fi
    if [[ ! -d "$dir" ]]; then
        echo "❌ ERREUR : le répertoire '$dir' est monté dans docker-compose.yml mais n'existe pas localement."
        echo "   Crée-le ou corrige docker-compose.yml. Voir docs/RUNBOOK.md#INC-001."
        exit 1
    fi
    echo "   ✅ $dir OK"
done

# Vérifier/créer app/web/static (INC-001)
if [[ ! -d "app/web/static" ]]; then
    echo ">>> Création de app/web/static (dossier vide requis par FastAPI StaticFiles) ..."
    mkdir -p app/web/static/css app/web/static/js
    touch app/web/static/.gitkeep
fi

# --- 1. Smoke test Docker local (dry-run build) ---
echo ">>> Smoke test Docker local ..."
if ! docker compose build --no-cache; then
    echo "❌ ERREUR : le build Docker local a échoué. Corrige avant de déployer."
    exit 1
fi

# Vérifier que l'image contient bien le dossier static (INC-001)
if ! docker run --rm detective-detective ls app/web/static/ >/dev/null 2>&1; then
    echo "❌ ERREUR : le dossier app/web/static n'est pas présent dans l'image Docker."
    echo "   Vérifie que le dossier n'est pas exclu par .dockerignore. Voir docs/RUNBOOK.md#INC-001."
    exit 1
fi

echo "   ✅ Build local OK, image cohérente"

# --- 2. Pull latest code on VPS ---
echo ">>> Pulling latest code on VPS ..."
ssh "${VPS_USER}@${VPS_HOST}" "cd ${VPS_DIR} && git pull origin main"

# --- 3. Sync data/ (DB SQLite + agent_state) ---
echo ">>> Syncing data/ ..."
rsync -avz --delete ./data/ "${VPS_USER}@${VPS_HOST}:${VPS_DIR}/data/"

# --- 4. Sync .env as .env.production ---
echo ">>> Syncing .env as .env.production ..."
rsync -avz ./.env "${VPS_USER}@${VPS_HOST}:${VPS_DIR}/.env.production"

# --- 5. Build & run ---
echo ">>> Building and starting container ..."
ssh "${VPS_USER}@${VPS_HOST}" "cd ${VPS_DIR} && docker compose up -d --build"

# --- 6. Post-deploy healthcheck ---
echo ">>> Waiting for container to be healthy ..."
sleep 5

HEALTH_URL="https://detective.digitalhs.biz/health"
LOGIN_URL="https://detective.digitalhs.biz/auth/login"
MAX_RETRIES=12
RETRY_DELAY=5

for i in $(seq 1 $MAX_RETRIES); do
    HEALTH_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" || echo "000")
    LOGIN_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$LOGIN_URL" || echo "000")

    if [[ "$HEALTH_CODE" == "200" && "$LOGIN_CODE" == "200" ]]; then
        echo "✅ Healthcheck OK — /health=$HEALTH_CODE, /auth/login=$LOGIN_CODE"
        break
    fi

    if [[ $i -eq $MAX_RETRIES ]]; then
        echo "❌ ERREUR : le cockpit ne répond pas correctement après ${MAX_RETRIES} tentatives."
        echo "   /health = $HEALTH_CODE"
        echo "   /auth/login = $LOGIN_CODE"
        echo ""
        echo ">>> Derniers logs du conteneur :"
        ssh "${VPS_USER}@${VPS_HOST}" "cd ${VPS_DIR} && docker compose logs --tail 50"
        exit 1
    fi

    echo "   ... tentative $i/$MAX_RETRIES (health=$HEALTH_CODE, login=$LOGIN_CODE)"
    sleep $RETRY_DELAY
done

# --- 7. Check status ---
echo ">>> Container status :"
ssh "${VPS_USER}@${VPS_HOST}" "cd ${VPS_DIR} && docker compose ps && docker compose logs --tail 10"

echo ""
echo "✅ Déploiement terminé et vérifié !"
echo "   Cockpit : https://detective.digitalhs.biz"
echo "   Pour suivre les logs : ssh ${VPS_USER}@${VPS_HOST} 'cd ${VPS_DIR} && docker compose logs -f'"
