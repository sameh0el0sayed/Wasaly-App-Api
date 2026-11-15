# Bahrain Transport & Delivery API (FastAPI)

Backend for the customer app, driver app and admin dashboard described in the spec.
WhatsApp reverse-OTP verifies new phone registrations; no OTP is sent by the backend. All prices/times/permissions are enforced on the server.

## Run

```bash
cp .env.example .env            # set SECRET_KEY, FERNET_KEY, first admin
docker compose up --build       # API on :8000, docs on /docs
# or locally (SQLite by default):
pip install -r requirements.txt && uvicorn app.main:app --reload
pytest                          # 117 tests
```
First super admin is created from `FIRST_ADMIN_*` on startup (must change password).
Production: use PostgreSQL and Alembic (`alembic revision --autogenerate -m init && alembic upgrade head`);
`create_all` on startup is a dev convenience.

## Spec rules -> where enforced

| Rule | Implementation |
|---|---|
| Phone verification | New registrations must send a reverse-OTP through WhatsApp before login/JWT issuance; existing accounts are retained as phone-verified by migration |
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
| Auth | `POST /api/auth/register, login, logout, change-password, forgot-password`, `POST /api/auth/whatsapp/request`, `GET /api/auth/whatsapp/status`, `GET /api/account/status`; inbound webhook `GET/POST /webhooks/whatsapp` |
| Profile | `GET/PATCH /api/profile`, `POST /api/profile/documents, license, deletion-request, sensitive-change-request` |
| Uploads | `POST /api/uploads/registration` (anonymous, claimed on register), `POST /api/uploads`, `POST /api/files/sign` |
| Customer orders | `POST /api/orders`, `GET /api/orders/my-orders, {id}, {id}/status-history`, `POST {id}/cancel, retry, dispute`, `GET /api/pricing` |
| Driver | `POST /api/driver/availability`, `GET offers?list=new\|unaccepted`, `POST offers/{id}/accept\|decline`, `POST orders/{id}/on-the-way\|arrived-at-pickup\|start-trip\|arrived-at-destination\|cancel`, `GET orders/accepted\|active\|completed\|cancelled`, `work-sessions`, `earnings`, `PATCH vehicle` |
| Admin | dashboard, users (approve/reject/request-correction/suspend/reactivate/reset-password), drivers, document review, driver service areas, orders (cancel, adjust-price), service-areas, settings, complaints, audit-logs, `reports/{summary,orders_by_period,per_driver,per_area,no_driver,driver_hours,trips,payments,payouts,subscriptions}?format=json\|csv\|xlsx` |
| Payments | `GET /api/payments/mine, {id}`, `POST {id}/tap, {id}/benefitpay`, `POST /api/payments/tap/webhook`, admin `GET /api/admin/payments`, `POST .../confirm, reject`, `GET /api/admin/payouts`, driver `PUT/GET /api/driver/payout-account`, `GET /api/driver/payouts` |
| Subscriptions | `POST/GET /api/subscriptions`, `GET {id}, {id}/trips`, `POST {id}/renew, cancel`; admin `/api/admin/subscriptions` (list, quote, reject, pause, resume, change-driver); driver `GET /api/driver/subscriptions, subscription-trips` |
| Misc | `GET /api/notifications`, `POST /api/devices` (FCM token), `POST /api/complaints`, `GET /health` |

## Registration flow (mobile)
1. Call `POST /api/auth/whatsapp/request` with the phone number entered by the user. Open the returned `whatsapp_url`; the user manually sends the prefilled `VERIFY 123456` message to Wasaly's WhatsApp Business number.
2. Poll `GET /api/auth/whatsapp/status?verification_id=<verification_id>`. For an unregistered number, success returns `verified=true` and `requires_registration=true`.
3. Upload photos/docs with `POST /api/uploads/registration` -> `file_key`s, then call `POST /api/auth/register` with the verified `whatsapp_verification_id` and registration fields (driver: license + vehicle block). The account is created `phone_verified=true` and the response returns a bearer JWT.
4. If a previously-created account is awaiting phone verification, its status response returns a bearer JWT after webhook verification; if that response is lost, the user can log in with the registered phone and password.
5. Phone verification does not replace the existing admin account review. The new account remains `PENDING_APPROVAL` until an admin approves it.

### WhatsApp webhook setup (incoming messages only)
- Configure Meta WhatsApp Cloud API webhook callback as `https://<public-host>/wasaly-api/webhooks/whatsapp` and subscribe to the `messages` field.
- Set `WHATSAPP_BUSINESS_NUMBER` (international digits, no `+`), `WHATSAPP_WEBHOOK_VERIFY_TOKEN` (same value entered in Meta webhook setup), and `WHATSAPP_APP_SECRET` (Meta app secret) in `.env`.
- The backend validates `X-Hub-Signature-256`, accepts only messages from the pending registration phone, stores only a keyed OTP hash, expires codes after 180 seconds, allows five attempts, and deduplicates provider message IDs.
- The backend does not send WhatsApp messages and does not need a WhatsApp access token for this inbound-only flow. The Flutter app launches WhatsApp for the user to send the message manually.
- Apply the migration with `docker compose exec api alembic upgrade head` (revision `0014`).

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

## Account deletion
`POST /api/account/delete {reason?}` soft-deletes (status `DELETED`, all tokens revoked, devices/FCM tokens removed, driver forced OFF). Refused with `ACTIVE_ORDER_EXISTS`, `UNPAID_ORDER_EXISTS` (+ `order_id`, `payment_id`), `ACTIVE_SUBSCRIPTION_EXISTS`, `DEBT_OUTSTANDING`. Login afterwards: 403 `ACCOUNT_DELETED`. Admin can `POST /api/admin/users/{id}/restore` inside `account_deletion_grace_days` (30). A daily job then anonymizes (name, phone, CPR, photos/documents, locations, logins); orders and payments (and payment proofs) are kept. A banned user keeps phone + CPR hash so deleting cannot escape the ban.

## Customer preferences & airport pricing
- Order fields (also in `POST /api/orders/estimate`): `pref_ac` (bool), `pref_smoking` (ANY|ALLOWED|NOT_ALLOWED), `pref_driver_gender` (ANY|FEMALE|MALE), `pref_large_car` (bool). They are **hard requirements**: only matching drivers get the offer, otherwise the order is `NO_DRIVER_AVAILABLE`.
- Matching data: AC = vehicle `has_ac`; large car = `vehicle_type` in setting `large_car_vehicle_types` (default `SUV,VAN`); smoking = driver `PATCH /api/driver/preferences {smoking_allowed}`; gender = set at registration (`driver.gender`), changed only by an admin (`PATCH /api/admin/drivers/{id}/gender`, reason required).
- Fees: fixed amount per preference from settings `pref_fee_ac|smoking_allowed|non_smoking|female_driver|male_driver|large_car` (default 0) + an airport fee. Airport zones: `GET/POST/PATCH/DELETE /api/admin/airport-zones` (super admin; circle or polygon, `fee`). A trip that starts or ends inside a zone pays that zone's fee once (the highest if several). All fees are snapshotted on the order, shown as `extras_fee` + `extras[]`, and added to the trip price (the night surcharge applies to the time price only).
- Defaults (all admin settings): `default_driver_gender` (MALE) is used for drivers with no gender; `default_pref_ac|smoking|driver_gender|large_car` apply when the app does not send a preference (factory: no restriction). Shown to the app in `GET /api/app-config` → `preference_defaults`.

## Driver proof photos
Before `start-trip` (pickup proof, body `pickup_photo_file_key`) and `arrived-at-destination` (delivery proof, body `delivery_photo_file_key`) the driver uploads via `POST /api/uploads` with `kind=PICKUP_PROOF|DELIVERY_PROOF`. Settings `require_pickup_photo`, `require_delivery_photo` (default true) and `proof_photos_apply_to` (`DELIVERY` = parcel orders only, default; `ALL` = every order, including passengers). Errors 422: `PICKUP_PHOTO_REQUIRED`, `DELIVERY_PHOTO_REQUIRED`, `INVALID_FILE_KEY`. Orders expose `pickup_photo`, `pickup_photo_at`, `delivery_photo`, `delivery_photo_at`; the customer, driver and admin of the order can sign the files. Notifications `ORDER_PICKED_UP` / `ORDER_DELIVERED`. Proof photos are kept when an account is anonymized.

## Driver proof photos
`start-trip` accepts `pickup_photo_file_key` (kind `PICKUP_PROOF`), `arrived-at-destination` accepts `delivery_photo_file_key` (kind `DELIVERY_PROOF`). Required by `require_pickup_photo` / `require_delivery_photo`; by default only for DELIVERY orders (`proof_photos_apply_to=DELIVERY`, set `ALL` to include passenger trips). Only the customer, the driver and admins can sign these files.

## Customer UI (wasaly-customer-ui-v1)
- Banners: `GET /api/banners` (optional auth, audience defaults to caller role, anonymous = ALL), admin `GET/POST/PATCH/DELETE /api/admin/banners` (writes SUPER_ADMIN). Banner images can be signed via `/api/files/sign` by any signed-in user.
- Live tracking: `GET /api/orders/{id}/driver-location` (owner customer, active statuses, latest point only, no history). Kill switch `customer_live_tracking_enabled`; stale after `live_location_max_age_seconds`. Orders expose `driver_live`. The driver consent text must say customers can see the location during an active order.
- Saved places: `/api/places` (HOME/WORK upsert, max 20, deleted with the account data).
- Service categories: `service_categories` in `/api/app-config`, edited with the `service_categories` setting (SUPER_ADMIN, validated).

## Architecture & migrations
- **Migrations:** Alembic (`alembic upgrade head`). With `APP_ENV=production` the app runs migrations on start; dev/tests use `create_all`. After changing a model: `alembic revision --autogenerate -m "..."` (a test fails if models and migrations diverge).
- **Admin API** is a package (`app/routers/admin/`: dashboard, users, drivers, orders, areas, settings, support, reports).
- **Order state machine:** `services/orders.py` (`ALLOWED` table, `transition()` is the only writer of `order_status`).
- **Storage strategy:** `services/storage_backends.py` (`CloudinaryStorage`, `LocalStorage`, `BACKENDS` registry).
- **Events:** `services/events.py` in-process bus; `transition()` publishes `OrderStatusChanged`. Handlers can later move to a queue (outbox) when a module is split out.
- **GPS map matching:** `services/gps.py` (validation/rolling window), `services/osrm.py` (self-hosted OSRM client), `services/map_matching.py` (workers, Redis, SSE broadcast), `services/eta.py` (cached OSRM ETA). No database migration is required: raw/matched locations live in Redis.
- **Lint:** `ruff check app tests`.

## Promo codes
- Admin: `GET /api/admin/promo-codes` (ADMIN); `POST/PATCH/DELETE` (SUPER_ADMIN). Fields: `code` (upper-cased), `percent` 1-100, `starts_at`, `ends_at`, `min_order_amount`, `is_active`. Master switch: setting `promo_enabled`.
- Customer sends `promo_code` to `POST /api/orders/estimate` and `POST /api/orders`. Errors (422): `PROMO_INVALID`, `PROMO_EXPIRED`, `PROMO_NOT_STARTED`, `PROMO_MIN_ORDER`, `PROMO_DISABLED`.
- The discount applies to the trip price only (base + night surcharge, never airport/preference extras) and is **paid by the platform**: the customer pays less, the driver's earning is unchanged, the company commission (`commission`) is reduced by the discount. The code is snapshotted on the order (`promo_code`, `promo_percent`, `discount_amount`).

## Real-time stream (SSE)
`GET /api/stream` (any signed-in user, `Authorization: Bearer`, `Accept: text/event-stream`). Admins receive every order/subscription event, customers and drivers only their own.
Events (small notifications, the app refetches the resource): `order.created`, `order.status` (`old_status`, `new_status`), `subscription.created`, `subscription.status`; plus `: ping` comments every 15 s.
Events are published **after the database commit** (never on rollback). In-process hub: with more than one server instance put Redis pub/sub behind `Hub.publish` (`services/realtime.py`).
Live GPS adds `driver.location.matched` (see below) and keeps the existing `admin.driver_location`.

## GPS map matching (self-hosted OSRM + OpenStreetMap)

Real-time map matching with **no paid API, no subscription and no per-request charge**. The road
network is the **Bahrain OpenStreetMap extract** (Geofabrik), processed locally by a self-hosted
**OSRM** container on the same VPS. Nothing is sent to a third party. OSM data © OpenStreetMap
contributors.

Pipeline (the GPS endpoint never waits for OSRM):

1. `POST /api/driver/location` validates the fix (`services/gps.py`): ranges, null-island, speed,
   heading, accuracy, impossible jumps, out-of-order timestamps, stationary detection.
2. The **raw** fix is stored in Redis (`wasaly:driver:raw_location:{id}`) and the driver is enqueued.
3. Background workers (`services/map_matching.py`) build a rolling window
   (`MAP_MATCH_WINDOW_SECONDS`, default 30 s) and call `OSRM /match/v1/drive`.
4. Application confidence = OSRM confidence × GPS accuracy × snap distance. Below
   `MAP_MATCH_CONFIDENCE_THRESHOLD`, or when OSRM is down, the **validated raw fix is kept** and the
   status is `DEGRADED` — trip tracking never stops.
5. The **matched** fix, road name, heading and H3 cell are stored separately
   (`wasaly:driver:matched_location:{id}`, `wasaly:driver:road_state:{id}`) and broadcast as
   `driver.location.matched` to the customer on the active trip (admins receive it too). A late
   result can never overwrite a newer fix (Redis compares the observation timestamp) and unchanged
   positions are not re-broadcast (sequence numbers are included for ordering).

Redis keys: `wasaly:driver:raw_location:{id}`, `wasaly:driver:matched_location:{id}`,
`wasaly:driver:road_state:{id}`, `wasaly:driver:location_history:{id}` (rolling diagnostics),
`wasaly:driver:customer:{id}` (who is allowed to receive the matched stream).

Matched coordinates are used by **nearest-first dispatch** (`services/dispatch.py`, H3 cell stored
alongside) and by **live ETA** (`ETA_ENABLED=true` -> OSRM `/route/v1/drive`, cached per order + H3
cell). `GET /api/orders/{id}/driver-location` returns the matched position with `confidence`,
`gps_status`, `road_name` and `eta_source` (`OSRM` or `STRAIGHT_LINE` fallback).

### OSRM deployment (existing VPS, Docker)

`docker-compose.yml` has an `osrm` service (`docker/osrm`). On first start it downloads
`https://download.geofabrik.de/asia/bahrain-latest.osm.pbf`, runs `osrm-extract -p /opt/car.lua` and
`osrm-contract`, stores the graph in the `osrmdata` volume and serves it on `127.0.0.1:5000`
(in-container port 5000). Restarts reuse the volume. Set in `.env`:

```bash
MAP_MATCHING_ENABLED=true
OSRM_BASE_URL=http://osrm:5000      # compose sets this automatically
ETA_ENABLED=true
```

Bare-metal alternative (no Docker): `sudo apt-get install -y osrm-backend` then
`sudo ./scripts/osrm_setup.sh` (same Geofabrik extract, same pipeline). The API falls back to raw
GPS whenever OSRM is unreachable, so a missing/failed OSRM container never blocks the app.

Configuration (`.env.example`): `MAP_MATCHING_ENABLED`, `OSRM_BASE_URL`, `OSRM_TIMEOUT_SECONDS`,
`OSRM_MATCH_WORKERS`, `OSRM_MATCH_MIN_INTERVAL_SECONDS`, `MAP_MATCH_WINDOW_SECONDS`,
`MAP_MATCH_MIN_POINTS`, `MAP_MATCH_MAX_POINTS`, `MAP_MATCH_MAX_ACCURACY_M`,
`MAP_MATCH_CONFIDENCE_THRESHOLD`, `MAP_MATCH_MAX_SNAP_DISTANCE_M`, `ETA_ENABLED`,
`ETA_CACHE_SECONDS`, `H3_RESOLUTION`.

## Languages (Arabic / English)
- `users.language` (`ar` default | `en`): language of notifications and pushes (written in the recipient's language when created). Set at register (`language`, else the `Accept-Language` header) and changed with `PATCH /api/profile {"language":"en"}`.
- Error messages, notes and `message` fields follow the request header `Accept-Language: en|ar` (default `ar`). Error `code`s never change.
- All texts live in `app/i18n.py` (`PAIRS`: Arabic -> English, with `{placeholders}`); unknown or user-written text passes through unchanged. A new language = a new column there + `LANGUAGES`.
- Migration `0004` adds `users.language`. If the server has an untracked `0003_*` file, delete it before `alembic upgrade head` (two heads otherwise).

## AI review role and chatbot

Create a dedicated reviewer from a super-admin session with `POST /api/admin/admins` and `role: "AI"`. This role is limited to pending account review, BenefitPay receipt review, and chatbot knowledge-base maintenance; it does not grant general admin access.

- `GET /api/admin/ai/users` lists up to 100 pending customer/driver registrations.
- `GET /api/admin/ai/users/{user_id}` returns review data and document file keys. Sign each file with `POST /api/files/sign` using `{ "file_key": "..." }`, then load the returned relative URL with the same bearer token.
- Approve/reject with `POST /api/admin/users/{user_id}/approve` (optional `{ "approve_pending_documents": true }`) or `/reject` with `{ "reason": "..." }`.
- BenefitPay receipts: `GET /api/admin/payments?status=PENDING_VERIFICATION&method=BENEFITPAY`, then `GET /api/admin/payments/{payment_id}` to inspect the signed proof URL and expected amount. Confirm using `POST /api/admin/payments/{payment_id}/confirm`, or reject with `/reject` and a reason. Confirmation uses the existing payment finalization and payout logic.

Customers can use `POST /api/chat` only after their account is approved. Send `Authorization: Bearer <access_token>` and JSON `{ "message": "كيف أطلب رحلة؟", "conversation_id": null }`. Response: `{ "success": true, "conversation_id": "<uuid>", "reply": "..." }`. Send the returned conversation ID on later messages; conversation ownership is enforced. Requests allow 1–4000 characters, preserve the last 20 messages as context, and are limited to `CHATBOT_RATE_LIMIT` requests per user per minute (default 10; in-process per API worker).

Set `GEMINI_API_KEY` on the backend only. Optional settings: `GEMINI_MODEL` (default `gemini-3.7-flash`), `GEMINI_TIMEOUT_SECONDS` (30), `GEMINI_THINKING_LEVEL` (`low` for Gemini 3 models), and `CHATBOT_RATE_LIMIT` (10). Gemini 3 requests use `thinkingLevel` and omit temperature for lower-latency generation; Gemini 2.x uses temperature 0.4. Run `alembic upgrade head` before production deploy. The knowledge base lives at `app/data/wasaly_knowledge.json` and is loaded for each request. AI role and super admins can read/update it via `GET`/`PUT /api/admin/ai/knowledge`; PUT accepts `{ "content": { ... } }` and the file update is atomic.

Flutter example using `package:http`:

```dart
final response = await http.post(
  Uri.parse('$baseUrl/api/chat'),
  headers: {
    'Authorization': 'Bearer $accessToken',
    'Content-Type': 'application/json',
    'Accept-Language': 'ar',
  },
  body: jsonEncode({'message': message, 'conversation_id': conversationId}),
);
if (response.statusCode == 200) {
  final data = jsonDecode(response.body) as Map<String, dynamic>;
  conversationId = data['conversation_id'] as String;
  final reply = data['reply'] as String;
  // Display reply and clear the loading state.
} else {
  // Display a retry/support state; 401 requires re-authentication,
  // 403 means the account is not approved, 429 means wait and retry,
  // 502/503/504 are temporary AI service errors.
}
```

### Scheduled AI review of CPRs and BenefitPay receipts

The API can run a scheduled review cycle at startup and every 15 minutes (`AI_REVIEW_INTERVAL_SECONDS=900`, bounded batch size `AI_REVIEW_BATCH_SIZE=20`). Provision one dedicated account through the super-admin `POST /api/admin/admins` endpoint with `role: "AI"`, then place that account's phone and password in backend-only `AI_SERVICE_PHONE` and `AI_SERVICE_PASSWORD`. The worker logs in through `/api/auth/login`, uses the returned short-lived token only for the scoped AI review endpoints, and never sends credentials or tokens to Gemini. `AI_SERVICE_BASE_URL` defaults to `http://127.0.0.1:8000` for the API container.

For pending customer registrations, Gemini reads the CPR image and the backend compares visible name, CPR number, issue date, and expiry date against the registration record. Driver review also compares license number and expiry. For pending BenefitPay payments on completed orders, Gemini extracts the receipt date, visible beneficiary account, amount, and currency; the backend compares these with the order's local completion date, configured BenefitPay account, and amount due (after wallet credit). All fields must match before the existing AI-only approval/confirmation endpoints are called. Unclear fields, provider failures, subscriptions without a trip date, and unmatched documents remain pending for manual review. Vehicle registration documents are not auto-approved by this pass.

This is image-field matching only; it cannot prove that a transfer actually settled or authenticate that an ID is genuine. A masked beneficiary account is accepted only when its visible suffix (at least four characters) matches the configured account. Configure the AI service account, Gemini key, model and account settings before expecting automatic review. Redeploy the updated application code, not just the `.env`, for the 15-minute worker to take effect.
