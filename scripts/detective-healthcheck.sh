#!/bin/bash
# Watchdog externe Charlie AI (HANDOVER §13.1 — niveau 3 anti-crash silencieux).
# Ping /health toutes les 60s, alerte Resend si 3 checks consécutifs échouent.
# Anti-spam : 1 alerte max par heure. Pas d'auto-restart (décision CDAL).
#
# Install : voir HANDOVER §13.1 procédure one-shot SSH.
# Rollback : rm /etc/cron.d/detective-healthcheck /usr/local/bin/detective-healthcheck.sh

# --- Config ---
LOG=/var/log/detective-healthcheck.log
STATE_DIR=/var/lib/detective-healthcheck
ENV_FILE=/opt/DETECTIVE/.env.production
HEALTHCHECK_CONSECUTIVE_THRESHOLD=3
ALERT_COOLDOWN_SECONDS=3600

mkdir -p "$STATE_DIR"

# Charger le .env prod pour récupérer RESEND_API_KEY, RESEND_FROM, PUBLIC_BASE_URL
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    # Filtrer : on ne charge que les lignes KEY=VALUE non-commentées, pour éviter
    # que bash exécute des fragments de texte dans le .env (ex: "# Belgique")
    while IFS='=' read -r key value; do
        # Skip lignes vides, commentaires, et lignes sans '='
        [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        # Exporter la variable (en supprimant d'éventuels guillemets autour de la valeur)
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
        export "$key"="$value"
    done < "$ENV_FILE"
    set +a
else
    echo "$(date -Iseconds) ERREUR: $ENV_FILE introuvable" >> "$LOG"
    exit 1
fi

if [ -z "${RESEND_API_KEY:-}" ] || [ -z "${RESEND_FROM:-}" ] || [ -z "${PUBLIC_BASE_URL:-}" ]; then
    echo "$(date -Iseconds) ERREUR: RESEND_API_KEY / RESEND_FROM / PUBLIC_BASE_URL manquants dans $ENV_FILE" >> "$LOG"
    exit 1
fi

HEALTH_URL="${PUBLIC_BASE_URL}/health"

# --- Ping /health ---
# --max-time 10 : ne pas bloquer le cron si le site est down
# -fsS : fail silently, show error. On récupère juste le code HTTP, le body on s'en fiche
RESP=$(curl --max-time 10 -fsS -o /dev/null -w "%{http_code}" "$HEALTH_URL" 2>/dev/null)

if [ "$RESP" = "200" ]; then
    # Succès : reset le compteur
    echo "1" > "$STATE_DIR/counter"
    exit 0
fi

# --- Échec : incrémenter le compteur ---
PREV=$(cat "$STATE_DIR/counter" 2>/dev/null || echo 0)
# S'assurer que PREV est numérique (au cas où le fichier serait corrompu)
if ! [[ "$PREV" =~ ^[0-9]+$ ]]; then
    PREV=0
fi
COUNT=$((PREV + 1))
echo "$COUNT" > "$STATE_DIR/counter"

# Pas encore 3 échecs consécutifs ? On attend.
if [ "$COUNT" -lt "$HEALTHCHECK_CONSECUTIVE_THRESHOLD" ]; then
    echo "$(date -Iseconds) check failed (HTTP $RESP), count=$COUNT (seuil=$HEALTHCHECK_CONSECUTIVE_THRESHOLD)" >> "$LOG"
    exit 0
fi

# --- 3+ échecs consécutifs : alerte Resend (anti-spam 1h) ---
NOW=$(date +%s)
LAST=$(cat "$STATE_DIR/last_alert" 2>/dev/null || echo 0)
if ! [[ "$LAST" =~ ^[0-9]+$ ]]; then
    LAST=0
fi

if [ $((NOW - LAST)) -lt "$ALERT_COOLDOWN_SECONDS" ]; then
    echo "$(date -Iseconds) ALERTE throttled (count=$COUNT, code=$RESP, dernière il y a $((NOW - LAST))s)" >> "$LOG"
    exit 0
fi

# Construction du payload JSON (échapper les guillemets dans le sujet/message)
SUBJECT="🚨 Charlie AI — Watchdog VPS : $COUNT échecs /health consécutifs"
HTML_BODY="<html><body style='font-family:Arial,sans-serif;max-width:600px;margin:20px;'>
<h2 style='color:#dc2626;'>🚨 Charlie AI injoignable</h2>
<p>Le watchdog VPS a détecté <strong>$COUNT échecs consécutifs</strong> du endpoint <code>/health</code> (code HTTP $RESP).</p>
<p><strong>URL pingée :</strong> $HEALTH_URL</p>
<p><strong>Action immédiate :</strong></p>
<ol>
<li><code>docker ps -a</code> (état du conteneur)</li>
<li><code>docker logs --tail=200 detective-agent</code> (logs Charlie)</li>
<li><code>systemctl status docker</code> (état Docker)</li>
</ol>
<p><strong>Pour relancer Charlie :</strong> <code>cd /opt/DETECTIVE && docker compose up -d</code></p>
<hr style='border:none;border-top:1px solid #ddd;margin:20px 0;'>
<p style='font-size:12px;color:#666;'>Cette alerte est envoyée 1 fois par heure max (anti-spam).
Le compteur repart à 1 dès que Charlie répond à nouveau 200.</p>
</body></html>"

PAYLOAD=$(cat <<EOF
{"from":"$RESEND_FROM","to":["cdal@digitalhs.biz"],"subject":"$SUBJECT","html":"$HTML_BODY"}
EOF
)

# Envoi Resend (best-effort, on log mais on ne fail pas le script)
SEND_RESP=$(curl --max-time 15 -fsS -X POST https://api.resend.com/emails \
    -H "Authorization: Bearer $RESEND_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" 2>&1)

SEND_EXIT=$?
if [ $SEND_EXIT -eq 0 ]; then
    echo "$NOW" > "$STATE_DIR/last_alert"
    echo "$(date -Iseconds) ALERTE envoyée (count=$COUNT, code=$RESP, resend_ok)" >> "$LOG"
else
    echo "$(date -Iseconds) ALERTE failed (count=$COUNT, code=$RESP, resend_error=$SEND_RESP)" >> "$LOG"
fi

exit 0
