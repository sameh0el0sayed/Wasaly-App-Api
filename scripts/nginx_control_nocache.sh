#!/usr/bin/env bash
# Adds "Cache-Control: no-cache" to the PCC /control/ location in nginx. Idempotent; never fails the deploy; reverts on a bad config.
command -v nginx >/dev/null 2>&1 || { echo "nginx: not installed on this host, skipped"; exit 0; }
F=$(grep -rl "location /control/ {" /etc/nginx/ 2>/dev/null | head -1)
[ -n "$F" ] || { echo "nginx: no 'location /control/' block found, skipped"; exit 0; }
grep -q 'Cache-Control "no-cache"' "$F" && { echo "nginx: header already present in $F"; exit 0; }
mkdir -p /opt/wasaly-backups && cp "$F" /opt/wasaly-backups/nginx-control.bak
sed -i '/location \/control\/ {/a\    add_header Cache-Control "no-cache" always;' "$F"
if nginx -t >/dev/null 2>&1; then
  systemctl reload nginx && echo "nginx: no-cache added to $F"
else
  cp /opt/wasaly-backups/nginx-control.bak "$F"; echo "nginx: config test failed, reverted"
fi
exit 0
