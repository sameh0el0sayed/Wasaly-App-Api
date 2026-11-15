"""
N+1 Acceptance Tests
====================

Measures SQL query count per API endpoint at 1, 10, 50 and 1000 records.
Verifies that query count remains bounded (does not grow linearly with collection size).

Usage:
    pytest tests/test_n_plus_one_acceptance.py -v
"""

from datetime import UTC, date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from app.database import Base, engine, SessionLocal
from app.main import app, bootstrap
from app.constants import Role, AccountStatus, OrderStatus
from app.models import User, Order, Payment, DriverPayout, Referral, utcnow
from app.security import hash_password, normalize_phone

# ---------------------------------------------------------------------------
# SQL query counter
# ---------------------------------------------------------------------------

class QueryCounter:
    """Counts SQL queries executed during a context."""

    def __init__(self):
        self.count = 0
        self._listener_id = None

    def __enter__(self):
        self.count = 0

        def listener(conn, cursor, statement, parameters, context, executemany):
            self.count += 1

        self._listener_id = event.listen(
            engine.sync_engine, "before_cursor_execute", listener
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._listener_id is not None:
            event.remove(
                engine.sync_engine, "before_cursor_execute", self._listener_id
            )
            self._listener_id = None
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def client():
    global _customer_counter, _driver_counter
    _customer_counter = {"i": 0}
    _driver_counter = {"i": 0}
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await bootstrap()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def auth(token):
    return {"Authorization": f"Bearer {token}"}


async def login(client, phone, password):
    r = await client.post("/api/auth/login", json={"phone": phone, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def create_admin(client, phone="+97300000000", password="ChangeMe!12345"):
    """Create a super admin directly in the DB (fast, no WhatsApp flow)."""
    async with SessionLocal() as db:
        admin = User(
            full_name="Super Admin",
            phone=normalize_phone(phone),
            role=Role.SUPER_ADMIN,
            password_hash=hash_password(password),
            account_status=AccountStatus.APPROVED,
            approved_at=utcnow(),
            must_change_password=False,
        )
        db.add(admin)
        await db.commit()
        await db.refresh(admin)
        return admin


async def create_customer(idx, phone=None, cpr=None, name=None):
    """Create a customer directly in the DB."""
    _customer_counter["i"] += 1
    n = _customer_counter["i"]
    phone = phone or f"33{n:06d}"
    cpr = cpr or f"9001{n:05d}"
    name = name or f"Customer {n}"
    async with SessionLocal() as db:
        u = User(
            full_name=name,
            phone=f"+973{phone}",
            cpr_hash=f"hash_{cpr}",
            cpr_last3=cpr[-3:],
            role=Role.CUSTOMER,
            password_hash=hash_password("Passw0rd!"),
            account_status=AccountStatus.APPROVED,
            phone_verified=True,
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u


async def create_driver(idx, phone=None, cpr=None, name=None, vehicle=True):
    """Create a driver directly in the DB with profile + vehicle."""
    _driver_counter["i"] += 1
    n = _driver_counter["i"]
    phone = phone or f"34{n:06d}"
    cpr = cpr or f"8501{n:05d}"
    name = name or f"Driver {n}"
    from app.models import DriverProfile, Vehicle
    async with SessionLocal() as db:
        u = User(
            full_name=name,
            phone=f"+973{phone}",
            cpr_hash=f"hash_{cpr}",
            cpr_last3=cpr[-3:],
            role=Role.DRIVER,
            password_hash=hash_password("Passw0rd!"),
            account_status=AccountStatus.APPROVED,
            phone_verified=True,
        )
        db.add(u)
        await db.flush()
        p = DriverProfile(
            user_id=u.id,
            license_number_encrypted=b"enc-license",
            license_expiry_date=date(2099, 1, 1),
            driver_status=AccountStatus.APPROVED,
            is_online=True,
            mode="LIVE",
            gender="MALE",
        )
        db.add(p)
        await db.flush()
        if vehicle:
            v = Vehicle(
                driver_id=u.id,
                make="Toyota",
                model="Camry",
                year=2020,
                color="White",
                plate_number=f"{idx}12345",
                vehicle_type="SEDAN",
                passenger_capacity=4,
                has_ac=True,
                vehicle_status=AccountStatus.APPROVED,
            )
            db.add(v)
        await db.commit()
        await db.refresh(u)
        return u


async def create_order(idx, customer_id, driver_id=None, status=OrderStatus.COMPLETED):
    """Create an order directly in the DB."""
    async with SessionLocal() as db:
        o = Order(
            customer_id=customer_id,
            driver_id=driver_id,
            service_type="PERSON",
            pickup_address=f"Pickup {idx}",
            pickup_latitude=26.22,
            pickup_longitude=50.57,
            destination_address=f"Destination {idx}",
            destination_latitude=26.21,
            destination_longitude=50.58,
            order_status=status,
            currency="BHD",
            currency_decimals=3,
            price_per_unit=Decimal("0.100"),
            unit_duration_seconds=60,
            min_price=Decimal("0.100"),
            final_price=Decimal("5.100"),
            base_price=Decimal("5.100"),
            night_enabled=False,
            night_percent=Decimal("25"),
            night_start="00:00",
            night_end="06:00",
            night_applied=False,
            night_surcharge=Decimal("0"),
            commission_type="PERCENT",
            commission_value=Decimal("30"),
            commission_mode="ADD",
            commission=Decimal("1.530"),
            customer_total=Decimal("6.630"),
            driver_earning=Decimal("5.100"),
            payment_status="PAID",
            completed_at=utcnow(),
        )
        db.add(o)
        await db.commit()
        await db.refresh(o)
        return o


async def seed_users_and_orders(client, count, driver_ratio=0.3, offset=0):
    """Create `count` customers and drivers + orders.
    Uses the super admin already created by bootstrap() (+97300000000).
    Returns (admin_token, customer_ids, driver_ids, order_ids, first_customer_phone)."""
    admin_token = await login(client, "+97300000000", "ChangeMe!12345")

    customer_ids = []
    first_customer_phone = None
    for i in range(count):
        u = await create_customer(i + offset * 100000)
        customer_ids.append(u.id)
        if i == 0:
            first_customer_phone = u.phone

    driver_count = max(1, int(count * driver_ratio))
    driver_ids = []
    for i in range(driver_count):
        u = await create_driver(i + offset * 100000)
        driver_ids.append(u.id)

    order_ids = []
    for i in range(count):
        did = driver_ids[i % len(driver_ids)] if driver_ids else None
        o = await create_order(i + offset * 100000, customer_ids[i], driver_id=did)
        order_ids.append(o.id)

    return admin_token, customer_ids, driver_ids, order_ids, first_customer_phone


# ---------------------------------------------------------------------------
# Helper: measure query count for an endpoint
# ---------------------------------------------------------------------------

async def measure_queries(client, method, path, headers=None, json=None, params=None):
    """Execute one request and return (response, query_count)."""
    with QueryCounter() as qc:
        if method == "GET":
            r = await client.get(path, headers=headers, params=params)
        elif method == "POST":
            r = await client.post(path, headers=headers, json=json, params=params)
        elif method == "PATCH":
            r = await client.patch(path, headers=headers, json=json, params=params)
        elif method == "PUT":
            r = await client.put(path, headers=headers, json=json, params=params)
        else:
            raise ValueError(method)
    return r, qc.count


# ---------------------------------------------------------------------------
# Acceptance tests: bounded query count independent of collection size
# ---------------------------------------------------------------------------

SCALES = [1, 10, 50, 1000]


class TestOrdersEndpoints:
    """Customer order endpoints must not suffer N+1."""

    @pytest.mark.parametrize("scale", SCALES)
    async def test_my_orders_query_count_bounded(self, client, scale):
        """GET /api/orders/my-orders: query count must be bounded regardless of order count."""
        admin_token, customer_ids, driver_ids, order_ids, _ = await seed_users_and_orders(client, scale)
        ctoken = await login(client, "+97333000001", "Passw0rd!")

        r, n = await measure_queries(client, "GET", "/api/orders/my-orders",
                                     headers=auth(ctoken), params={"limit": 50, "offset": 0})
        assert r.status_code == 200, r.text

        # Baseline: auth + settings + count + orders + eager relationships = bounded
        assert n <= 20, f"my-orders: {n} queries at scale={scale} (too many)"

    @pytest.mark.parametrize("scale", SCALES)
    async def test_get_order_query_count_bounded(self, client, scale):
        """GET /api/orders/{id}: single order, constant query count."""
        _, customer_ids, _, order_ids = await seed_users_and_orders(client, scale)
        ctoken = await login(client, "+97333000001", "Passw0rd!")

        r, n = await measure_queries(client, "GET", f"/api/orders/{order_ids[0]}",
                                     headers=auth(ctoken))
        assert r.status_code == 200, r.text
        assert n <= 6, f"get_order: {n} queries at scale={scale} (too many)"

    @pytest.mark.parametrize("scale", SCALES)
    async def test_driver_orders_query_count_bounded(self, client, scale):
        """GET /api/driver/orders/completed: driver orders with customer eager loaded."""
        admin_token, customer_ids, driver_ids, order_ids, _ = await seed_users_and_orders(client, scale)
        dtoken = await login(client, "+97334000001", "Passw0rd!")

        r, n = await measure_queries(client, "GET", "/api/driver/orders/completed",
                                     headers=auth(dtoken), params={"limit": 50, "offset": 0})
        assert r.status_code == 200, r.text
        assert n <= 8, f"driver orders: {n} queries at scale={scale} (too many)"


class TestAdminEndpoints:
    """Admin endpoints are the most N+1-prone (lists of users, drivers, orders)."""

    @pytest.mark.parametrize("scale", SCALES)
    async def test_admin_list_users_query_count_bounded(self, client, scale):
        """GET /api/admin/users: must not lazy-load driver_profile/driver_application per user."""
        admin_token, customer_ids, driver_ids, order_ids, _ = await seed_users_and_orders(client, scale)

        r, n = await measure_queries(client, "GET", "/api/admin/users",
                                     headers=auth(admin_token), params={"limit": 50, "offset": 0})
        assert r.status_code == 200, r.text
        # Baseline: settings, count, users + eager relationships = bounded
        assert n <= 10, f"admin users: {n} queries at scale={scale} (too many)"

    @pytest.mark.parametrize("scale", SCALES)
    async def test_admin_list_drivers_query_count_bounded(self, client, scale):
        """GET /api/admin/drivers: must not lazy-load vehicle per driver."""
        admin_token, customer_ids, driver_ids, order_ids, _ = await seed_users_and_orders(client, scale)

        r, n = await measure_queries(client, "GET", "/api/admin/drivers",
                                     headers=auth(admin_token),
                                     params={"limit": 50, "offset": 0})
        assert r.status_code == 200, r.text
        assert n <= 10, f"admin drivers: {n} queries at scale={scale} (too many)"

    @pytest.mark.parametrize("scale", SCALES)
    async def test_admin_list_orders_query_count_bounded(self, client, scale):
        """GET /api/admin/orders: must not lazy-load customer/driver/vehicle per order."""
        admin_token, customer_ids, driver_ids, order_ids, _ = await seed_users_and_orders(client, scale)

        r, n = await measure_queries(client, "GET", "/api/admin/orders",
                                     headers=auth(admin_token), params={"limit": 50, "offset": 0})
        assert r.status_code == 200, r.text
        assert n <= 10, f"admin orders: {n} queries at scale={scale} (too many)"

    @pytest.mark.parametrize("scale", SCALES)
    async def test_admin_get_order_query_count_bounded(self, client, scale):
        """GET /api/admin/orders/{id}: single order detail, constant queries."""
        admin_token, customer_ids, driver_ids, order_ids, _ = await seed_users_and_orders(client, scale)

        r, n = await measure_queries(client, "GET", f"/api/admin/orders/{order_ids[0]}",
                                     headers=auth(admin_token))
        assert r.status_code == 200, r.text
        assert n <= 6, f"admin get_order: {n} queries at scale={scale} (too many)"

    @pytest.mark.parametrize("scale", SCALES)
    async def test_admin_get_user_activity_query_count_bounded(self, client, scale):
        """GET /api/admin/users/{id}/activity: batched order stats, not 6 separate count queries."""
        admin_token, customer_ids, driver_ids, order_ids, _ = await seed_users_and_orders(client, scale)

        r, n = await measure_queries(client, "GET", f"/api/admin/users/{customer_ids[0]}/activity",
                                     headers=auth(admin_token))
        assert r.status_code == 200, r.text
        # Before fix: 6+ separate count queries. After: batched grouped queries.
        assert n <= 12, f"user activity: {n} queries at scale={scale} (too many)"


class TestPaymentEndpoints:
    """Admin payment/payout views must not do db.get() per row."""

    @pytest.mark.parametrize("scale", [1, 10, 50])
    async def test_admin_payments_query_count_bounded(self, client, scale):
        """GET /api/admin/payments: eager load customer/driver/order/subscription."""
        admin_token, customer_ids, driver_ids, order_ids, _ = await seed_users_and_orders(client, scale)

        async with SessionLocal() as db:
            for i, oid in enumerate(order_ids[:scale]):
                p = Payment(
                    kind="ORDER", order_id=oid, customer_id=customer_ids[i],
                    driver_id=driver_ids[i] if driver_ids else None,
                    amount=Decimal("5.100"), currency="BHD", currency_decimals=3,
                    payment_method="TAP", payment_status="PAID",
                )
                db.add(p)
            await db.commit()

        r, n = await measure_queries(client, "GET", "/api/admin/payments",
                                     headers=auth(admin_token), params={"limit": 50, "offset": 0})
        assert r.status_code == 200, r.text
        assert n <= 8, f"admin payments: {n} queries at scale={scale} (too many)"

    @pytest.mark.parametrize("scale", [1, 10, 50])
    async def test_admin_payouts_query_count_bounded(self, client, scale):
        """GET /api/admin/payouts: eager load driver + driver_profile."""
        admin_token, customer_ids, driver_ids, order_ids, _ = await seed_users_and_orders(client, scale)

        async with SessionLocal() as db:
            for i, did in enumerate(driver_ids[:scale]):
                r = DriverPayout(
                    payment_id=i + 1, driver_id=did, amount=Decimal("5.100"),
                    currency="BHD", currency_decimals=3, status="PENDING",
                )
                db.add(r)
            await db.commit()

        r, n = await measure_queries(client, "GET", "/api/admin/payouts",
                                     headers=auth(admin_token), params={"limit": 50, "offset": 0})
        assert r.status_code == 200, r.text
        assert n <= 8, f"admin payouts: {n} queries at scale={scale} (too many)"


class TestV11Endpoints:
    """v11 endpoints (live map, nearby drivers, referrals) had severe N+1."""

    @pytest.mark.parametrize("scale", [1, 10, 50])
    async def test_live_map_query_count_bounded(self, client, scale):
        """GET /api/admin/live-map: batched driver stats, eager loaded customer/vehicle."""
        admin_token, customer_ids, driver_ids, order_ids, _ = await seed_users_and_orders(client, scale)

        r, n = await measure_queries(client, "GET", "/api/admin/live-map",
                                     headers=auth(admin_token))
        assert r.status_code == 200, r.text
        assert n <= 15, f"live map: {n} queries at scale={scale} (too many)"

    @pytest.mark.parametrize("scale", [1, 10, 50])
    async def test_nearby_drivers_query_count_bounded(self, client, scale):
        """GET /api/nearby-drivers: batch load users + vehicle + rating stats."""
        admin_token, customer_ids, driver_ids, order_ids, _ = await seed_users_and_orders(client, scale)
        ctoken = await login(client, "+97333000001", "Passw0rd!")

        r, n = await measure_queries(client, "GET", "/api/nearby-drivers",
                                     headers=auth(ctoken),
                                     params={"lat": 26.22, "lng": 50.57, "radius_km": 5})
        assert r.status_code == 200, r.text
        assert n <= 10, f"nearby drivers: {n} queries at scale={scale} (too many)"

    @pytest.mark.parametrize("scale", [1, 10, 50])
    async def test_admin_referrals_query_count_bounded(self, client, scale):
        """GET /api/admin/referrals: batch load users, not 2 db.get() per referral."""
        admin_token, customer_ids, driver_ids, order_ids, _ = await seed_users_and_orders(client, scale)

        async with SessionLocal() as db:
            for i in range(scale):
                r = Referral(
                    inviter_id=customer_ids[0],
                    invitee_id=customer_ids[min(i + 1, scale - 1)] if scale > 1 else customer_ids[0],
                    status="REWARDED" if i % 2 == 0 else "REGISTERED",
                )
                db.add(r)
            await db.commit()

        r, n = await measure_queries(client, "GET", "/api/admin/referrals",
                                     headers=auth(admin_token), params={"limit": 50, "offset": 0})
        assert r.status_code == 200, r.text
        assert n <= 6, f"admin referrals: {n} queries at scale={scale} (too many)"


class TestDriverEndpoints:
    """Driver endpoints must not lazy-load customer per order."""

    @pytest.mark.parametrize("scale", [1, 10, 50])
    async def test_driver_offers_query_count_bounded(self, client, scale):
        """GET /api/driver/offers: eager load order + counter per offer."""
        admin_token, customer_ids, driver_ids, order_ids, _ = await seed_users_and_orders(client, scale)
        dtoken = await login(client, "+97334000001", "Passw0rd!")

        r, n = await measure_queries(client, "GET", "/api/driver/offers",
                                     headers=auth(dtoken), params={"limit": 50, "offset": 0})
        assert r.status_code == 200, r.text
        assert n <= 10, f"driver offers: {n} queries at scale={scale} (too many)"


# ---------------------------------------------------------------------------
# Regression: query count must NOT grow linearly with collection size
# ---------------------------------------------------------------------------

class TestQueryGrowthRegression:
    """Verify query count stays bounded as collection size grows."""

    @pytest.mark.parametrize("endpoint,method,path,params", [
        ("my-orders", "GET", "/api/orders/my-orders", {"limit": 50, "offset": 0}),
        ("admin-users", "GET", "/api/admin/users", {"limit": 50, "offset": 0}),
        ("admin-drivers", "GET", "/api/admin/drivers", {"limit": 50, "offset": 0}),
        ("admin-orders", "GET", "/api/admin/orders", {"limit": 50, "offset": 0}),
    ])
    async def test_query_count_does_not_grow_linearly(self, client, endpoint, method, path, params):
        """Query count at scale=1000 must not be significantly higher than at scale=1."""
        _, _, _, _ = await seed_users_and_orders(client, 1)
        token = await login(client, "+97333000001", "Passw0rd!")

        # Measure at small scale
        _, n_small = await measure_queries(client, method, path, headers=auth(token), params=params)

        # Re-seed at large scale
        await seed_users_and_orders(client, 1000)
        _, n_large = await measure_queries(client, method, path, headers=auth(token), params=params)

        # Allow some growth (settings reload, count query) but not linear (e.g. 1000x more queries)
        growth_factor = n_large / max(n_small, 1)
        assert growth_factor < 5.0, (
            f"{endpoint}: query count grew {growth_factor:.1f}x from scale=1 ({n_small}) "
            f"to scale=1000 ({n_large}) - possible N+1 regression"
        )
