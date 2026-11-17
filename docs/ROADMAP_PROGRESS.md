# Wasaly master plan - progress

| Phase | Status |
|---|---|
| 1 Audit | DONE (summary sent to the owner) |
| 2 Unified architecture (service categories, vehicle categories, pricing profiles, provider categories, quotes, eligibility, fixed-price completion) | DONE - migration 0023 |
| 3 Pickup | BACKEND DONE (disabled until the owner enables it; Flutter screens pending) |
| 4 Recovery | BACKEND DONE on the same engine (vehicle info / can_move / emergency / equipment) |
| 5 Freight + quotations | BACKEND DONE (auto price + manual quote over thresholds, admin pricing of quotes, manual dispatch) |
| 6 Corporate accounts | BACKEND DONE - migration 0024 (members OWNER/MANAGER/EMPLOYEE, monthly statement with discount, monthly limit, bulk booking) |
| 6b Partner integrations (v32: kinds, pricing modes, keys, batch, sub-merchants, services via API) | BACKEND DONE - migration 0025 |
| 7 AI operations manager + intelligent dispatch | TODO (proposal approved: suggests, never changes money/bans; every action audited) |
| 8 Reports, reconciliation, analytics | TODO |
| 9 Tests, security review, compliance, production | TODO |

Rules kept: Rides pricing untouched; no new service is live until an admin enables it AND configures its pricing; indicative seed rates are editable;
unapproved providers never receive bookings of a service; any price change needs customer approval (quote -> accept).
