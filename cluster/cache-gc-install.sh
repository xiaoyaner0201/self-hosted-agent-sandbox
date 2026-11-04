#!/bin/bash
# Install the cache GC as a daily systemd timer on a node. Re-runnable.
set -euo pipefail
install -m 0755 "$(dirname "$0")/cache-gc.sh" /usr/local/sbin/sbx-cache-gc
cat > /etc/systemd/system/sbx-cache-gc.service <<'UNIT'
[Unit]
Description=Prune agent sandbox package caches
[Service]
Type=oneshot
Nice=10
IOSchedulingClass=idle
ExecStart=/usr/local/sbin/sbx-cache-gc
UNIT
cat > /etc/systemd/system/sbx-cache-gc.timer <<'UNIT'
[Unit]
Description=Daily agent sandbox cache prune
[Timer]
OnCalendar=*-*-* 04:20:00
RandomizedDelaySec=30m
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now sbx-cache-gc.timer
