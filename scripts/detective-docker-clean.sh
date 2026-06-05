#!/bin/bash
# Cleanup disk Charlie AI (HANDOVER §13.3) — dimanche 4h.
# docker system prune -af : supprime tout ce qui n'est pas utilisé.
# SAFETY : abort si un container de prod est stopped (évite de détruire Charlie).
# Best-effort : si Docker est down, on log et on quitte sans alerte.

set -e

LOG=/var/log/detective-docker-clean.log
ENV_FILE=/opt/DETECTIVE/.env.production
# Safety list : préfixes de containers de PROD qu'on ne doit PAS supprimer même stopped.
# ⚠️ Si tu ajoutes un nouveau container prod, ajoute son préfixe ici.
PROD_PREFIXES=(
    "detective-"
    "cerveau2-"
    "cdal2-"
    "magicreator-"
    "mondayupartner-"
    "icoonebali-"
    "photobooth"
    "scrappingtool"
    "n8n"
    "traefik"
)

log() { echo "$(date -Iseconds) $*" >> "$LOG"; }

log "=== Cleanup démarré ==="

# Charger env pour le webhook Slack
if [ -f "$ENV_FILE" ]; then
    set -a
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
        export "$key"="$value"
    done < "$ENV_FILE"
    set +a
fi

# --- SAFETY CHECK : abort si un container de prod est stopped ---
STOPPED_PROD=()
for prefix in "${PROD_PREFIXES[@]}"; do
    # docker ps -a : tous (running + stopped), --filter name=$prefix, --format nom
    while IFS= read -r cname; do
        [ -z "$cname" ] && continue
        status=$(docker inspect -f '{{.State.Status}}' "$cname" 2>/dev/null || echo "unknown")
        if [[ "$status" == "exited" || "$status" == "dead" || "$status" == "created" || "$status" == "stopped" ]]; then
            STOPPED_PROD+=("$cname ($status)")
        fi
    done < <(docker ps -a --filter "name=^${prefix}" --format '{{.Names}}' 2>/dev/null)
done

if [ ${#STOPPED_PROD[@]} -gt 0 ]; then
    log "ABORT: ${#STOPPED_PROD[@]} container(s) de prod stopped :"
    printf '  - %s\n' "${STOPPED_PROD[@]}" >> "$LOG"

    # Notif Slack (best-effort)
    if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
        LIST=$(printf '• %s\n' "${STOPPED_PROD[@]}")
        TEXT=$(printf ':no_entry: *Charlie AI — cleanup ABORT* : %d container(s) de prod stopped, prune NON effectué pour éviter destruction.\n%s\n_Action : investiguer pourquoi stopped (crash? debug?). Relancer manuellement puis cleanup dimanche prochain._' "${#STOPPED_PROD[@]}" "$LIST")
        PAYLOAD=$(FROM="$RESEND_FROM" TEXT="$TEXT" python3 -c '
import json, os
payload = {"text": os.environ["TEXT"]}
print(json.dumps(payload, ensure_ascii=False))
')
        curl --max-time 5 -fsS -X POST "$SLACK_WEBHOOK_URL" \
            -H "Content-Type: application/json" \
            -d "$PAYLOAD" 2>/dev/null || true
    fi
    log "=== Cleanup ABORTED (safety) ==="
    exit 0  # Exit 0 : abort intentionnel, pas une erreur cron
fi

# --- Mesure avant ---
BEFORE=$(df -BG / | tail -1 | awk '{print $4}' | tr -d 'G')
log "Disk libre avant : ${BEFORE}G"

# --- Prune (timeout 5 min, best-effort) ---
if timeout 300 docker system prune -af 2>>"$LOG" >>"$LOG"; then
    log "docker system prune OK"
else
    EXIT=$?
    log "ERREUR docker system prune exit=$EXIT"
    # Notifier quand même (best-effort)
    if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
        TEXT=$(printf ':warning: Charlie AI — cleanup disk dimanche a échoué (exit=%d). Voir /var/log/detective-docker-clean.log' "$EXIT")
        PAYLOAD=$(FROM="$RESEND_FROM" TEXT="$TEXT" python3 -c '
import json, os
payload = {"text": os.environ["TEXT"]}
print(json.dumps(payload, ensure_ascii=False))
')
        curl --max-time 5 -fsS -X POST "$SLACK_WEBHOOK_URL" \
            -H "Content-Type: application/json" \
            -d "$PAYLOAD" 2>/dev/null || true
    fi
    exit 1
fi

# --- Mesure après ---
AFTER=$(df -BG / | tail -1 | awk '{print $4}' | tr -d 'G')
DELTA=$((AFTER - BEFORE))
log "Disk libre après : ${AFTER}G (delta : +${DELTA}G)"

# --- Notif Slack résultat ---
if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
    if [ "$DELTA" -lt 1 ]; then
        TEXT=$(printf ':recycle: *Charlie AI — cleanup hebdo* : aucun gain significatif (%sG → %sG, +%sG). Le VPS est propre.' "$BEFORE" "$AFTER" "$DELTA")
    else
        TEXT=$(printf ':recycle: *Charlie AI — cleanup hebdo* : +%sG libérés (%sG → %sG)' "$DELTA" "$BEFORE" "$AFTER")
    fi
    PAYLOAD=$(FROM="$RESEND_FROM" TEXT="$TEXT" python3 -c '
import json, os
payload = {"text": os.environ["TEXT"]}
print(json.dumps(payload, ensure_ascii=False))
')
    curl --max-time 5 -fsS -X POST "$SLACK_WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" 2>/dev/null || true
fi

log "=== Cleanup terminé ==="
exit 0
