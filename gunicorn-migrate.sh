#!/usr/bin/env bash
# One-shot migration of inventory.service: Flask dev server -> gunicorn.
# Applies a systemd drop-in overriding ExecStart, restarts, then health-checks
# BOTH surfaces (API + admin) via their real hostnames. On failure it removes
# the drop-in and restarts to fall back to the Flask dev server. Idempotent and
# safe to re-run. Scheduled via a one-shot systemd timer.
set -u
LOG=/root/inventory/gunicorn-migrate.log
DROPIN=/etc/systemd/system/inventory.service.d
STATUS=UNKNOWN
ts(){ date -u +'%Y-%m-%dT%H:%M:%SZ'; }
log(){ echo "[$(ts)] $*" >> "$LOG"; }

verify(){
  local i api adm
  for i in 1 2 3 4 5; do
    api=$(curl -s --max-time 15 https://api.mdrlighting.co.nz/v1/status)
    adm=$(curl -s -o /dev/null --max-time 15 -w '%{http_code}' https://tools.tankway.co.nz/inventory/)
    if echo "$api" | grep -qE '"status"[[:space:]]*:[[:space:]]*"ok"' && { [ "$adm" = "302" ] || [ "$adm" = "200" ]; }; then
      log "verify OK (attempt $i): admin_http=$adm"; return 0
    fi
    log "verify retry $i: api='${api:0:50}' admin=$adm"; sleep 6
  done
  return 1
}

log "=== migration start ==="
log "pre  ExecStart: $(systemctl show -p ExecStart --value inventory.service | tr '\n' ' ' | head -c 200)"

mkdir -p "$DROPIN"
printf '%s\n' \
  '[Service]' \
  'ExecStart=' \
  'ExecStart=/root/inventory/venv/bin/gunicorn -w 1 --threads 8 --timeout 180 -b 127.0.0.1:5004 app:app' \
  > "$DROPIN/gunicorn.conf"

systemctl daemon-reload && systemctl restart inventory.service
sleep 4

if verify; then
  STATUS=PASS
  grep -q '^gunicorn' /root/inventory/requirements.txt 2>/dev/null || echo 'gunicorn>=21.0' >> /root/inventory/requirements.txt
  log "STATUS=PASS — gunicorn live and healthy"
else
  log "verification FAILED — rolling back to Flask dev server"
  rm -rf "$DROPIN"
  systemctl daemon-reload && systemctl restart inventory.service
  sleep 4
  if verify; then
    STATUS=ROLLED_BACK; log "STATUS=ROLLED_BACK — recovered on Flask dev server"
  else
    STATUS=ROLLED_BACK_RECOVERY_FAILED; log "STATUS=ROLLED_BACK_RECOVERY_FAILED — needs manual attention"
  fi
fi

log "post ExecStart: $(systemctl show -p ExecStart --value inventory.service | tr '\n' ' ' | head -c 200)"
/root/inventory/venv/bin/python /root/inventory/gunicorn-notify.py "$STATUS" >> "$LOG" 2>&1 || log "notify step skipped/failed"
log "=== migration end ($STATUS) ==="
[ "$STATUS" = "PASS" ] && exit 0 || exit 1
