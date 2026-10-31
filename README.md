# Bahrain Transport & Delivery API (FastAPI)

Backend for the customer app, driver app and admin dashboard described in the spec.
No OTP, no live tracking, all prices/times/permissions enforced on the server.

## Run

```bash
cp .env.example .env            # set SECRET_KEY, FERNET_KEY, first admin
docker compose up --build       # API on :8000, docs on /docs
# or locally (SQLite by default):
pip install -r requirements.txt && uvicorn app.main:app --reload
pytest                          # 87 tests
```
First super admin is created from `FIRST_ADMIN_*` on startup (must change password).
Production: use PostgreSQL and Alembic (`alembic revision --autogenerate -m init && alembic upgrade head`);
`create_all` on startup is a dev convenience.

## Spec rules -> where enforced

| Rule | Implementation |
|---|---|
| No OTP | Phone + password only. Recovery = `POST /api/auth/forgot-password` -> admin `reset-password` |
| Account review | `PENDING_APPROVAL` until admin approves; `approved_user` dependency returns 403 `ACCOUNT_NOT_APPROVED` |
| Billing window | `trip_started_at` set on `start-trip`, `trip_arrived_at` on `arrived-at-destination`, server clock only |
| Price | `services/pricing.py`: `ceil(seconds/unit_seconds) x unit_price`, min `min_price`, integer math, `Decimal`. Defaults: 300 s, 0.500, 0.500 |
| Currency | Setting `currency_code` (default BHD). Each order snapshots currency, decimals and prices at creation |
| Price snapshot | Each order stores unit price/duration/min at creation; settings changes affect new orders only |
| 10 s offers | `order_offers.expires_at`; expiry sweeper (2 s loop + lazy on API calls) -> `EXPIRED` = "unaccepted" list |
| One winner | Conditional `UPDATE orders ... WHERE driver_id IS NULL` + row locks; tested with concurrent accepts |
| State machine | `services/orders.py::transition` is the only writer; every change in `order_status_history` |
| Network retries | start/arrive/on-the-way endpoints are idempotent (never overwrite timestamps/price) |
| Docs expiry | Latest doc per type must be APPROVED and unexpired; sweeper forces expired drivers offline |
| Sensitive data | CPR/license Fernet-encrypted, CPR keyed-hash for uniqueness, masked in admin, reveal = super admin + audit |
| Files | Magic-byte validation, ownership check, 5-minute signed download links |
| Audit | Approvals, price/settings changes, CPR reveal, cancellations, adjustments -> `admin_audit_logs` |

## Multi-currency (by setting)

Super admin changes the active currency with `PATCH /api/admin/settings`:

```json
{"currency_code": "USD", "unit_price": "1.25", "min_price": "1.25", "reason": "Expansion"}
```

- Supported: BHD, KWD, OMR, JOD (3 decimals), SAR, AED, QAR, EGP, USD, EUR, GBP (2), JPY (0). List: `GET /api/currencies`. Add more in `app/constants.py::CURRENCIES`.
- Changing currency requires `unit_price` and `min_price` in the same request, and they must fit the currency's decimals.
- Applies to new orders only; old orders keep their own currency and price.
- Reports never add different currencies together (grouped by currency).
- API field names are currency-neutral (`final_price`, `unit_price`, `min_price`) plus `currency` and `currency_decimals`.

## Night surcharge by setting

Super admin: `PATCH /api/admin/settings`

```json
{"night_surcharge_enabled": true, "night_surcharge_percent": "25", "night_start": "00:00", "night_end": "06:00", "reason": "..."}
```

- Local time is Asia/Bahrain. Window `[night_start, night_end)`; it may cross midnight (e.g. `22:00` to `06:00`).
- Applies to the WHOLE trip price when the trip starts (`trip_started_at`) inside the window: base 2.000 + 25% = 2.500.
- The company commission is calculated on the price including the surcharge.
- Example, base 2.000, night, commission 30% (the default):

| Mode | Customer pays | Driver takes | Company |
|---|---|---|---|
| `ADD` (default) | 3.250 | 2.500 | 0.750 |
| `DEDUCT` | 2.500 | 1.750 | 0.750 |

- Snapshotted per order (new orders only). Responses include `base_price`, `night_surcharge`, `night_applied`; `final_price` includes the surcharge.

## Platform profit (commission) by setting

Super admin: `PATCH /api/admin/settings`

```json
{"commission_type": "PERCENT", "commission_value": "30", "commission_mode": "ADD", "reason": "..."}
```

| Setting | Values | Default |
|---|---|---|
| `commission_type` | `PERCENT` (value = %) or `FIXED` (value = amount in the active currency) | `PERCENT` |
| `commission_value` | 30 = 30% | `30` |
| `commission_mode` | `DEDUCT` (taken from the trip price) or `ADD` (added on top) | `ADD` |

Example, daytime trip price 2.000 at 30%:

| Mode | Customer pays (`customer_total`) | Driver gets (`driver_earning`) | Company (`commission`) |
|---|---|---|---|
| ADD (default) | 2.600 | 2.000 | 0.600 |
| DEDUCT | 2.000 | 1.400 | 0.600 |

Snapshotted per order, so changes apply to new orders only. `final_price` stays the time-based trip price.
Customers see `customer_total` (and `service_fee` only in ADD mode); drivers see `driver_earning` (and `platform_fee` only in DEDUCT mode); admins see everything. Payments store `customer_total`.
Reports add `company_profit`, `driver_earning`, `customer_total`, always grouped by currency.

## Outside Bahrain
If the destination is outside the `country_boundary` polygon (setting; default is an approximate box around Bahrain), the customer pays the normal commission (30%) AND the company deducts `outside_bahrain_driver_percent` (25%) of the trip price from the driver.
Example, base 20 (day): customer 26, driver 15, company 11. At night (price 25): customer 32.5, driver 18.75, company 13.75. Destination coordinates are mandatory for every order. Replace the box with a real border polygon for production.

## Payments (no cash)
All money goes to the company account first; the driver share is transferred afterwards.
- On trip completion a `payment` is created (`order.payment_id`). The customer pays by **Tap** (`POST /api/payments/{id}/tap` -> `tap_url`; fully automatic, the Tap fee `tap_fee_percent`/`tap_fee_fixed` is added to what the customer pays) or **BenefitPay** (`POST /api/payments/{id}/benefitpay` with the screenshot `proof_file_key`; the company account is in `GET /api/payments/{id}.benefitpay_account`, set via settings).
- Tap webhook (`POST /api/payments/tap/webhook`) never trusts its body: the charge is re-fetched from Tap and must be CAPTURED with the same reference, amount and currency. Idempotent.
- BenefitPay: admin `POST /api/admin/payments/{id}/confirm` or `/reject`.
- After PAID a `driver_payouts` row is created and transferred (`PAYOUT_PROVIDER`). The driver sets the account with `PUT /api/driver/payout-account` (encrypted). Admin: `GET /api/admin/payouts`, `POST .../retry`, `POST .../mark-transferred`.
- **`PAYOUT_PROVIDER=simulated` moves NO money** (rows are flagged `simulated: true`; tests only). The default is `none` (admin transfers manually, then `mark-transferred`); use `http` (your provider) in production. Tap needs your own `TAP_SECRET_KEY`; it was not tested against the live gateway (tests mock it).
- `block_new_order_if_unpaid` (default true) refuses new orders (402 `UNPAID_ORDER_EXISTS`) while a completed trip is unpaid.

## Monthly subscriptions (work / business / school)
Holder: an approved customer (individual or parent). Flow: `POST /api/subscriptions` (route, weekdays, times, student/school) -> admin `POST /api/admin/subscriptions/{id}/quote` (monthly price per route/distance + driver; company profit `subscription_commission_value`, default 30%, mode ADD/DEDUCT as trips) -> customer pays the `open_payment` (Tap/BenefitPay) -> ACTIVE for 30 days, trip rows generated -> driver `GET /api/driver/subscription-trips`, `POST .../{id}/complete` (today only) -> `POST /api/subscriptions/{id}/renew`. Admin can reject, pause, resume, change driver. Expiry and missed trips are swept hourly. No automatic refund on cancel.

## Settings (all editable by the super admin: `PATCH /api/admin/settings`, `reason` required)
Offer time `offer_ttl_seconds` (default 10, allowed 5-120), `offer_max_drivers`, `order_timeout_seconds`, `max_order_retries` (3), `driver_auto_offline_minutes` (0 = off), `subscription_period_days`, `subscription_renewal_reminder_days`, `login_max_failed_attempts`, `login_lockout_minutes`, `upload_max_mb`, `doc_expiry_warning_days`, `cancel_after_accept_allowed`, `maintenance_mode` + message, `min_app_version_android/ios`, support phone/whatsapp/email, `online_window_minutes` (15), `activity_retention_days` (90), `location_snapshots_enabled`, plus the pricing/commission/night/outside/payment/estimate settings above. The app reads the public ones from `GET /api/app-config` at start-up. Changes apply to NEW orders; existing orders keep their snapshot.

## User activity (admin)
- **Online** = any authenticated request within `online_window_minutes` (15). No heartbeat. Otherwise offline. (Separate from the driver's "receive offers" switch.)
- The app calls `POST /api/app/open` on every start with device info (`device_id` generated once per install, model, OS, app version, locale, optional `fcm_token`); login accepts `device_id` too. Counts: `app_open_count`, `login_count`, per-device `open_count`.
- `GET /api/admin/users/{id}/activity`: online, last seen/login/IP, counts, devices, recent logins (with failed attempts and `new_device`), push enabled, customer or driver stats (orders, offers, acceptance rate, online hours). `GET /api/admin/users?online=true|false` and `users_online_now` on the dashboard.
- **Location is not tracked continuously.** With the user's consent (`POST /api/privacy/location-consent`) the app sends one snapshot at key moments (`POST /api/location/snapshot`: app open, login, order created, offer accepted, on the way, arrived, trip started/ended, driver online). Admin sees them only with `?include_location=true` and every view is audit-logged. Withdrawing consent deletes the user's snapshots. Login history and snapshots are deleted after `activity_retention_days`. Show a clear consent screen and privacy policy in the app (Bahrain PDPL).

## Estimates (before the trip)
The final price always comes from the real trip time. Before that the app shows server estimates labelled "تقديري":
- `POST /api/orders/estimate` (customer; pickup + destination coordinates) -> `distance_km`, `duration_min`, `estimated_trip_price`, `night_surcharge`, `service_fee`, `estimated_total`, `is_outside_bahrain`, `pickup_in_service_area`, `is_estimate: true`.
- `GET /api/driver/offers` now also returns `distance_km`, `duration_min`, `estimated_driver_earning` (after commission and the outside-Bahrain cut), `currency`, `is_outside_bahrain`, `is_estimate: true`.
There is no routing engine: distance = straight line x `estimate_road_factor` (1.35), duration = distance / `estimate_avg_speed_kmh` (35), both settings. Plug in a maps/routing API later by replacing `services/estimate.py::route_guess`. The app never calculates prices itself.

## Payment details are settings (editable from the dashboard)
`PATCH /api/admin/settings` (super admin, with `reason`): `benefitpay_account_name`, `benefitpay_account_number`, `benefitpay_instructions`, `tap_enabled`, `benefitpay_enabled`, `tap_fee_percent`, `tap_fee_fixed`. The app reads them from `GET /api/payments/methods` (enabled methods + Tap fee + BenefitPay account) and in every `GET /api/payments/{id}`. A disabled method is refused with `409 METHOD_DISABLED`.

## Files and announcements
- Files: `STORAGE_BACKEND=cloudinary` (default; set `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`). Files are uploaded as **authenticated (private)** assets: no public Cloudinary URL ever exists; the API checks type/size and serves downloads only through short-lived signed API links (`POST /api/files/sign`). `local` (disk) is for development/tests only.
- Admin announcements: `POST /api/admin/notifications` with `audience` = `CUSTOMERS` | `DRIVERS` | `ALL`, or `user_id` for one person. Stored as in-app notifications (type `ANNOUNCEMENT`) and sent as push.

## Main endpoints

| Area | Endpoints |
|---|---|
| Auth | `POST /api/auth/register, login, logout, change-password, forgot-password`, `GET /api/account/status` |
| Profile | `GET/PATCH /api/profile`, `POST /api/profile/documents, license, deletion-request, sensitive-change-request` |
| Uploads | `POST /api/uploads/registration` (anonymous, claimed on register), `POST /api/uploads`, `POST /api/files/sign` |
| Customer orders | `POST /api/orders`, `GET /api/orders/my-orders, {id}, {id}/status-history`, `POST {id}/cancel, retry, dispute`, `GET /api/pricing` |
| Driver | `POST /api/driver/availability`, `GET offers?list=new\|unaccepted`, `POST offers/{id}/accept\|decline`, `POST orders/{id}/on-the-way\|arrived-at-pickup\|start-trip\|arrived-at-destination\|cancel`, `GET orders/accepted\|active\|completed\|cancelled`, `work-sessions`, `earnings`, `PATCH vehicle` |
| Admin | dashboard, users (approve/reject/request-correction/suspend/reactivate/reset-password), drivers, document review, driver service areas, orders (cancel, adjust-price), service-areas, settings, complaints, audit-logs, `reports/{summary,orders_by_period,per_driver,per_area,no_driver,driver_hours,trips,payments,payouts,subscriptions}?format=json\|csv\|xlsx` |
| Payments | `GET /api/payments/mine, {id}`, `POST {id}/tap, {id}/benefitpay`, `POST /api/payments/tap/webhook`, admin `GET /api/admin/payments`, `POST .../confirm, reject`, `GET /api/admin/payouts`, driver `PUT/GET /api/driver/payout-account`, `GET /api/driver/payouts` |
| Subscriptions | `POST/GET /api/subscriptions`, `GET {id}, {id}/trips`, `POST {id}/renew, cancel`; admin `/api/admin/subscriptions` (list, quote, reject, pause, resume, change-driver); driver `GET /api/driver/subscriptions, subscription-trips` |
| Misc | `GET /api/notifications`, `POST /api/devices` (FCM token), `POST /api/complaints`, `GET /health` |

## Registration flow (mobile)
1. Upload photos/docs with `POST /api/uploads/registration` -> `file_key`s.
2. `POST /api/auth/register` with the keys (driver: license + vehicle block). Returns a token with status `PENDING_APPROVAL`.
3. The app polls `GET /api/account/status`; approved-only endpoints return 403 until approved.

## Notes and open decisions (spec section 23)
- Push: real FCM through the Firebase Admin SDK (`services/notifications.py`). Set `FIREBASE_CREDENTIALS` to the service-account JSON path; empty = push disabled (in-app list still works). Device tokens come from `POST /api/devices`; dead tokens are deleted automatically. Push is fire-and-forget and only a hint: the app re-fetches state from the API.
- Service areas: polygon or circle JSON (`[lat,lng]`), no PostGIS needed. Orders outside every active area are rejected.
- Cancellation: customer may cancel before the trip starts, no fee (`cancel_after_accept_allowed` setting). Later cancellation = admin.
- Payment: Tap and BenefitPay only. Cash does not exist. Refunds are not modelled in profit reports.
- Customer confirmation of start/end is not enforced; `POST /api/orders/{id}/dispute` records objections for admin review.
- Add rate limiting at the reverse proxy and put the API behind HTTPS.

## Wasaly v3: ratings, LIVE mode, nearest-first dispatch

- **Ratings**: `POST /api/orders/{id}/rating` (customer→driver) and `POST /api/driver/orders/{id}/rating` (driver→customer). 1–5 stars, tags, comment. Errors: `RATINGS_DISABLED`, `ORDER_NOT_COMPLETED`, `RATING_WINDOW_CLOSED`, `ALREADY_RATED`. `GET /api/orders/{id}` returns `my_rating`.
- **Auto-ban**: a driver is suspended automatically after `auto_suspend_driver_one_star_count` (default 1) one-star ratings; customers `auto_suspend_customer_one_star_count` (default 0 = off). Deferred while the driver has an active trip. Admin reactivation resets the counter.
- **Driver modes**: `POST /api/driver/availability {"mode": "OFF|NORMAL|LIVE"}` (`{"online": true}` still works). LIVE needs `live_mode_enabled` (default **false**, set by admin) and the driver's location consent. `POST /api/driver/location` keeps the latest position only.
- **Dispatch**: `dispatch_nearest_first` ranks LIVE drivers by distance to pickup; `dispatch_max_pickup_km` (0 = no limit). `offer_max_drivers=1` = strictly nearest.
- `GET /api/app-config` (public) now uses the Wasaly shape (`maintenance`, `offer_timeout_seconds`, `support{}`, `live_mode_enabled`, ...).
- `GET /api/admin/users/{id}/activity` adds `is_online`, `stats`, `logins`, `app_opens`, `locations`.

## Bans, reinstatement fee, driver debt
- 2nd automatic suspension (`auto_ban_after_auto_suspensions`, default 2) = `BANNED`: admins cannot reactivate; the CPR/phone cannot register again (`ACCOUNT_BANNED`).
- Only the **super admin** can lift it: `POST /api/admin/users/{id}/reinstate` with the fee from setting `ban_reinstatement_fee` (default 50). A driver's fee becomes a **debt (negative balance)**: every payout is reduced by the debt until it reaches 0 (payout status `OFFSET` when fully kept; `debt_offset` shows the amount). `GET /api/driver/balance` shows it; `POST /api/admin/drivers/{id}/debt/settle` records a direct payment. A customer's fee must be collected first (`fee_received: true`).

## Demo seed & environments
- `cp .env.example .env`, fill the values. With `SEED_DEMO_DATA=true` the first start creates a super admin (`FIRST_ADMIN_*`), a normal admin, a demo customer, and a demo driver (approved, vehicle approved, IBAN, service area "Manama"). Everything is read from `.env`; nothing is hard-coded.
- `APP_ENV=production` makes the API refuse to start with demo seed enabled, the default `SECRET_KEY`/`FIRST_ADMIN_PASSWORD`, or an empty `FERNET_KEY`.
