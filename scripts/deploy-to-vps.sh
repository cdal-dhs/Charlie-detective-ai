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

# --- Détection déploiement léger (code Python uniquement) ---
# Si seuls des fichiers sous app/ changent, le volume ./app:/app/app:ro
# dans docker-compose.yml rend le rebuild Docker inutile.
CHANGED_FILES=$(git diff --name-only HEAD~1 HEAD)
LIGHT_DEPLOY=true
for f in $CHANGED_FILES; do
    if [[ "$f" == pyproject.toml ]] || [[ "$f" == Dockerfile* ]] || [[ "$f" == docker-compose.yml ]] || [[ "$f" == scripts/* ]] || [[ "$f" == requirements* ]]; then
        LIGHT_DEPLOY=false
        break
    fi
done
if [[ "$LIGHT_DEPLOY" == true ]]; then
    echo ">>> 🚀 Déploiement LÉGER détecté (code Python uniquement)"
    echo "    Fichiers modifiés :"
    echo "$CHANGED_FILES" | sed 's/^/      - /'
    echo ">>> Pull + restart sur le VPS (pas de rebuild Docker) ..."
    ssh "${VPS_USER}@${VPS_HOST}" "cd ${VPS_DIR} && git pull origin main && docker compose restart"
    echo ""
    echo "✅ Déploiement léger terminé !"
    echo "   Le conteneur redémarre avec le nouveau code (volume app/ monté)."
    exit 0
fi

# Vérifier que les répertoires montés dans docker-compose.yml existent (INC-001)
echo ">>> Vérification des répertoires montés (docker-compose.yml) ..."
MOUNTED_DIRS=$(grep -o '\- \./[^:]*' docker-compose.yml | sed 's/- \.\///' | sort -u || true)
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

# --- 1. Smoke test Docker local (validation syntaxe + static dir) ---
echo ">>> Smoke test Docker local ..."
# Validation syntaxe docker-compose (rapide, pas de build)
if ! docker compose config > /dev/null 2>&1; then
    echo "❌ ERREUR : docker-compose.yml invalide. Corrige avant de déployer."
    exit 1
fi
# Vérifier que les répertoires montés existent
for dir in app web/static data logs; do
    if [[ ! -d "$dir" ]]; then
        echo "❌ ERREUR : le répertoire '$dir' est requis mais n'existe pas."
        exit 1
    fi
done
echo "   ✅ Config Docker valide, répertoires OK"

# --- 2. Pull latest code on VPS ---
echo ">>> Pulling latest code on VPS ..."
ssh "${VPS_USER}@${VPS_HOST}" "cd ${VPS_DIR} && git pull origin main"

# --- 3. Sync data/ (DB SQLite + agent_state) ---
# ⚠️ agent_state.db NE DOIT PAS être écrasée — elle contient les catégories,
# priorités et statuts modifiés via le cockpit. Backup automatique + exclusion.
echo ">>> Backup agent_state.db on VPS ..."
ssh "${VPS_USER}@${VPS_HOST}" \
    "cd ${VPS_DIR}/data && test -f agent_state.db && cp agent_state.db agent_state.db.backup-\$(date +%Y%m%d-%H%M%S) || true"

echo ">>> Syncing data/ (agent_state.db exclue) ..."
rsync -avz --delete --exclude='agent_state.db' ./data/ "${VPS_USER}@${VPS_HOST}:${VPS_DIR}/data/"

# --- 4. Sync .env as .env.production ---
echo ">>> Syncing .env as .env.production ..."
rsync -avz ./.env "${VPS_USER}@${VPS_HOST}:${VPS_DIR}/.env.production"

# --- Architecture check ---
# ⚠️  Le VPS Hostinger est x86_64 (amd64). Il N'EST JAMAIS sous ARM64.
#    Le Mac M4 Max est ARM64 (aarch64). Une image ARM64 ne peut PAS
#    tourner sur un VPS x86_64 → crash en boucle immédiat.
#    Quand les architectures diffèrent, on build sur le VPS directement.
echo ">>> Vérification de l'architecture ..."
LOCAL_ARCH=$(docker version --format '{{.Client.Arch}}' 2>/dev/null || uname -m)
VPS_ARCH=$(ssh "${VPS_USER}@${VPS_HOST}" 'docker version --format "{{.Server.Arch}}" 2>/dev/null || uname -m')
echo "   Local : $LOCAL_ARCH | VPS : $VPS_ARCH"

if [[ "$LOCAL_ARCH" != "$VPS_ARCH" ]]; then
    echo "   ⚠️  MISMATCH ARCHITECTURE ($LOCAL_ARCH ≠ $VPS_ARCH)"
    echo "      Le VPS est x86_64 (amd64) — JAMAIS ARM64."
    echo "   → Build natif sur le VPS (obligatoire quand architectures différentes)"
    ssh "${VPS_USER}@${VPS_HOST}" "cd ${VPS_DIR} && docker compose build --no-cache && docker compose up -d --force-recreate"
else
    # --- 5. Build en LOCAL puis push vers VPS ---
    # ⚠️ Avec torch supprimé, le build local est rapide. On pousse l'image compilée.
    echo ">>> Build local de l'image base (si pyproject.toml changé) ..."
    if ! docker images --format '{{.Repository}}:{{.Tag}}' | grep -q '^detective-agent:base$'; then
        echo "   Image base absente en local — build complet ..."
        docker build -f Dockerfile.base -t detective-agent:base .
    else
        echo "   Image base déjà présente en local — skip base build."
    fi

    echo ">>> Build local de l'image applicative ..."
    docker build -t detective_detective .

    echo ">>> Push de l'image vers le VPS (docker save | ssh docker load) ..."
    docker save detective_detective | ssh "${VPS_USER}@${VPS_HOST}" 'docker load'

    echo ">>> Démarrage du service sur le VPS ..."
    ssh "${VPS_USER}@${VPS_HOST}" "cd ${VPS_DIR} && docker compose up -d"
fi

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
