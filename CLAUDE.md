# Wasaly API - instructions for Claude sessions

## Server rules (mandatory, from PCC incident 2026-11-22)
Incident: the shared server (1 CPU core, 3.8 GB RAM, 6 systems, 30+ containers) went down for hours: autoheal containers restarted everything under load, a crashing image restarted endlessly, a wrong-compose rollback created empty databases, and after the hard reboot nothing came back (restart=no).
These rules can only be changed by the owner through PCC.

- Your system is one app on a shared server. Never touch the server, docker.sock, other systems' containers, networks, volumes or .env files. Server-level needs go to PCC as a handoff.
- No autoheal, no watchdog containers, no privileged/host-pid/host-network containers.
- NEVER create a new database, volume or compose project for an existing system. Always use the existing compose project name (-p) and compose file; never run a dev/root compose file on the server.
- Rollback is the plugin's own `rollback` argument (header `# phx-rollback: yes`): retag :previous and recreate only this service with the exact project/file/env.
- Restart policy: unless-stopped for long-running services; on-failure:5 for workers/consumers. Never `no` on production services.
- Healthchecks: interval 30s, timeout 5s, retries 3, start_period 30s.
- CPU/memory limits on every service (API 0.5 CPU / 384M, workers 0.25 CPU / 256M, static/nginx 64M, redis 96M with maxmemory 64mb).
- 1 API replica, 1 worker process.
- Every image is import-tested during the build (`python -c 'import app.main'`); a broken image must fail the build, never reach production.
- Logs: json-file max-size 10m, max-file 3.
- Heavy jobs (backups, reports, OCR, AI batches) only between 02:00 and 05:00 Bahrain time, with nice.
- Migrations run automatically in the deploy and must be backward compatible (no drop/rename of columns in the same release).
- Before any change to docker-compose, volumes or DATABASE_URL: stop and send PCC a handoff instead.

## Working rules (owner)
- Replies to the owner: short, Modern Standard Arabic. JSON handoffs for other agents: English.
- Deploys are done by the owner from PCC; after a push just say it is pushed and ready to deploy from PCC.
- Work starts only on the word «موافق» (except when a JSON spec/handoff is received).

## Language rules (mandatory, owner 2026-11-24)
- English is the default everywhere. All API responses (detail, message, errors, validation) are English; no Arabic in responses, logs or codes.
- Error shape: `{"detail": ..., "code": "STABLE_UPPER_SNAKE_CODE", "status": 422, "message": "English sentence"}`. Clients translate by code, never by parsing text. Codes list: docs/ERROR_CODES.md.
- Accept-Language (en|ar) only affects end-user texts (notifications, SMS/WhatsApp/e-mail, receipts, PDFs); default en. Users have a saved `language` (default en).
- New strings are written in English; add an Arabic pair in app/i18n.py PAIRS when Arabic users must see it.
- Apps/frontends: English first launch, language switch, LTR/RTL automatic, no hard-coded UI text, Western digits, BHD 3 decimals, VAT 10% shown separately.
