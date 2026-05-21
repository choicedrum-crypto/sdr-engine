#!/usr/bin/env bash
# PB3 Phase 3 cutover: swap sdr-engine.service from old layout to monorepo layout.
# Live tunnel + port unchanged. ~30s downtime. Run as: sudo bash pb3-cutover.sh
set -euo pipefail

OLD_UNIT=/etc/systemd/system/sdr-engine.service
NEW_UNIT_SRC=/home/admin1/tcia/sdr-engine-monorepo/apps/sdr-engine/scripts/systemd/sdr-engine.service.new
BACKUP=/etc/systemd/system/sdr-engine.service.bak.pb3-$(date -u +%Y%m%dT%H%M%SZ)

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root (use: sudo bash $0)" >&2
  exit 1
fi
if [[ ! -f "$NEW_UNIT_SRC" ]]; then
  echo "ERROR: new unit file not found at $NEW_UNIT_SRC" >&2
  exit 1
fi
if [[ ! -x /home/admin1/tcia/sdr-engine-monorepo/apps/sdr-engine/.venv/bin/gunicorn ]]; then
  echo "ERROR: gunicorn not found in monorepo venv" >&2
  exit 1
fi

echo "==> 1. Backing up current unit to $BACKUP"
cp "$OLD_UNIT" "$BACKUP"

echo "==> 2. Pre-cutover /health check (should be 200)"
curl -fsS http://127.0.0.1:5679/health > /dev/null && echo "    OK"

echo "==> 3. Installing new unit (monorepo layout)"
cp "$NEW_UNIT_SRC" "$OLD_UNIT"

echo "==> 4. systemctl daemon-reload + restart"
systemctl daemon-reload
systemctl restart sdr-engine.service
sleep 4

echo "==> 5. Post-cutover /health check"
for i in 1 2 3 4 5; do
  if curl -fsS http://127.0.0.1:5679/health > /dev/null; then
    echo "    /health OK on attempt $i"
    break
  fi
  echo "    attempt $i failed, retrying in 2s..."
  sleep 2
done

echo "==> 6. Verify gunicorn is running from new path"
if pgrep -af 'gunicorn.*5679' | grep -q sdr-engine-monorepo; then
  echo "    gunicorn running from monorepo path"
else
  echo "    WARNING: gunicorn cmdline doesn't show monorepo path" >&2
  pgrep -af 'gunicorn.*5679' >&2 || true
fi

echo "==> 7. systemctl status"
systemctl status sdr-engine.service --no-pager -n 5 | head -20

echo
echo "Cutover complete. Backup at: $BACKUP"
echo "Rollback: sudo bash pb3-rollback.sh $BACKUP"
echo "Tail logs: tail -f /home/admin1/tcia/sdr-engine-monorepo/apps/sdr-engine/gunicorn.log"
