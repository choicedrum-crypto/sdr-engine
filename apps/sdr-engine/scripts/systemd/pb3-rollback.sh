#!/usr/bin/env bash
# PB3 Phase 3 rollback: restore sdr-engine.service from a pre-cutover backup.
# Run as: sudo bash pb3-rollback.sh /etc/systemd/system/sdr-engine.service.bak.pb3-<timestamp>
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root (use: sudo bash $0 <backup-path>)" >&2
  exit 1
fi
if [[ $# -ne 1 ]]; then
  echo "Usage: sudo bash $0 <backup-path>" >&2
  echo "Available backups:" >&2
  ls -la /etc/systemd/system/sdr-engine.service.bak.pb3-* 2>&1 >&2 || true
  exit 1
fi

BACKUP=$1
TARGET=/etc/systemd/system/sdr-engine.service

if [[ ! -f "$BACKUP" ]]; then
  echo "ERROR: backup file not found: $BACKUP" >&2
  exit 1
fi

echo "==> 1. Restoring unit from $BACKUP"
cp "$BACKUP" "$TARGET"

echo "==> 2. systemctl daemon-reload + restart"
systemctl daemon-reload
systemctl restart sdr-engine.service
sleep 4

echo "==> 3. Post-rollback /health check"
for i in 1 2 3 4 5; do
  if curl -fsS http://127.0.0.1:5679/health > /dev/null; then
    echo "    /health OK on attempt $i"
    break
  fi
  echo "    attempt $i failed, retrying in 2s..."
  sleep 2
done

echo "==> 4. systemctl status"
systemctl status sdr-engine.service --no-pager -n 5 | head -20
echo
echo "Rollback complete. Old layout restored."
