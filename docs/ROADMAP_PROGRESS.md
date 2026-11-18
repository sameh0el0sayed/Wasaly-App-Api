# Wasaly master plan - progress

| Phase | Status |
|---|---|
| 1 Audit | DONE (summary sent to the owner) |
| 2 Unified architecture (service categories, vehicle categories, pricing profiles, provider categories, quotes, eligibility, fixed-price completion) | DONE - migration 0023 |
| 3 Pickup | BACKEND DONE (disabled until the owner enables it; Flutter screens pending) |
| 4 Recovery | BACKEND DONE on the same engine (vehicle info / can_move / emergency / equipment) |
| 5 Freight + quotations | BACKEND DONE (auto price + manual quote over thresholds, admin pricing of quotes, manual dispatch) |
| 6 Corporate accounts | BACKEND DONE - migration 0024 (members OWNER/MANAGER/EMPLOYEE, monthly statement with discount, monthly limit, bulk booking) |
| 6b Partner integrations (v32: kinds, pricing modes, keys, batch, sub-merchants, services via API) | BACKEND DONE - migration 0025 | |
| 6c Partner sandbox, granular webhook events, live location, quote-priced webhook (v34) | BACKEND DONE - migration 0027 |
| 6d Partner portal API (v35: staff accounts, own deliveries/invoices/keys/team, admin grants access) | BACKEND DONE - migration 0028 |
| 6e Tap fees by card type (customer pays, visible, editable; +200 fils per Tap transaction; VAT 10%) | BACKEND DONE - migration 0029 |
| 7 AI operations manager + intelligent dispatch | BACKEND DONE - migration 0026 (rule-based scan every 3 min: no driver, late driver, silent driver, long trip, waiting quotes, price anomalies, demand surge; suggests only; audited as AI; daily report) |
| 8 Reports, reconciliation, analytics | BACKEND DONE - /api/admin/finance (reconciliation with issue detection, receivables from partners and corporates, daily series, CSV) |
| 9 Tests, security review, compliance | PARTIAL - docs/SECURITY_REVIEW.md done (route audit passed, checklist); automated tests and external pen-test left to the owner (he runs tests himself) |

Rules kept: Rides pricing untouched; no new service is live until an admin enables it AND configures its pricing; indicative seed rates are editable;
unapproved providers never receive bookings of a service; any price change needs customer approval (quote -> accept).
