# Security and compliance review (phase 9, backend) - 2026-11-17

## Automated route audit
154 admin routes inspected: every one is behind `admin_user` / `super_admin_user`, the AI-review role (`ai_review_user`), or an explicit in-function permission check (`/api/admin/ai/knowledge`). No open admin route found.

## New surfaces, controls
| Area | Control |
|---|---|
| Merchant / partner API | key hashed (sha256), several revocable keys, per-account + per-IP rate limit, HTTPS-only webhooks with SSRF filter, HMAC-signed webhooks, idempotent on external_ref |
| Corporate accounts | membership check on every booking, owner only by admin, monthly credit limit, audit on member changes |
| Unified services | disabled until admin enables AND pricing configured; only APPROVED providers receive bookings; price change needs customer approval |
| AI operations manager | suggest-only; no money/ban/order mutation; every finding audited under actor AI; admin ack/dismiss audited |
| Finance | admin-only; reconciliation flags completed-without-payment, unpaid over 48 h, driver+company split mismatch |
| Auth | token_version, refresh tokens with rotation and reuse detection (SEC-001, off until apps adopt) |
| Deploy | pg_dump backup before migrate, migrations in the pipeline, health check |

## Open items (owner decision / not code)
1. Enable `access_token_minutes=30` only after every app uses `/auth/token/refresh`.
2. Licences and permits: verify the Bahrain licence for each regulated service (Freight, Recovery, ride-hailing) BEFORE enabling it; do not present Wasaly as a licensed operator without it.
3. Insurance documents per provider category are collected in the provider flow; confirm the admin review checklist matches the regulator's list.
4. PDPL: location data only while LIVE and without history (already so); add a retention period for merchant/corporate order data and the ops reports; privacy notice wording for corporate employees.
5. Run the full test suite and an external penetration test before production traffic of the new services (not run by the assistant by owner instruction).
6. Sandbox environment for partners and extra webhook events are still to build.
