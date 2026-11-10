"""v23: tool-calling AI assistant. READS the caller's own data freely; every WRITE is only PROPOSED (ChatAction)
and runs after the user confirms in the app, through the same handlers as the normal API."""
import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..constants import OrderStatus as S
from ..constants import Role
from ..models import ChatAction, DriverPayout, Order, SavedPlace, User, utcnow
from . import settings_service
from .audit import audit

ACTION_TTL = 600
MAX_ACTIONS = 3
ROUTE_PREFIXES = ("/c", "/d", "/profile", "/notifications")
QUOTE_TOLERANCE = Decimal("0.05")


class ToolError(Exception):
    pass


class Ctx:
    def __init__(self, db: AsyncSession, user: User, conv_id: str, lang: str, lat, lng):
        self.db, self.user, self.conv_id, self.lang, self.lat, self.lng = db, user, conv_id, lang, lat, lng
        self.actions: list[dict] = []
        self.links: list[dict] = []

    def t(self, ar: str, en: str) -> str:
        return en if self.lang == "en" else ar


def _P(type_: str, desc: str) -> dict:
    return {"type": type_, "description": desc}


S_, N_, I_, B_ = "STRING", "NUMBER", "INTEGER", "BOOLEAN"


def _fn(name: str, desc: str, props: dict | None = None, required: list[str] | None = None) -> dict:
    d = {"name": name, "description": desc}
    if props:
        d["parameters"] = {"type": "OBJECT", "properties": {k: _P(*v) for k, v in props.items()}, "required": required or []}
    return d


_PLACE = {"place": (S_, "Saved place label/name (HOME, WORK or its name)"), "lat": (N_, "Latitude"), "lng": (N_, "Longitude"),
          "address": (S_, "Address text")}
COMMON = [
    _fn("get_profile", "The signed-in user's profile."),
    _fn("get_notifications", "Latest notifications.", {"limit": (I_, "1-20")}),
    _fn("suggest_link", "Show an in-app shortcut chip. Route must start with /c, /d, /profile or /notifications.",
        {"label": (S_, "Chip text"), "route": (S_, "e.g. /c/order/123, /c/orders, /c/places, /d/earnings, /d/trip/45")}, ["label", "route"]),
]
CUSTOMER = COMMON + [
    _fn("list_saved_places", "The customer's saved places."),
    _fn("list_orders", "Recent orders.", {"status": (S_, "Optional order status filter"), "limit": (I_, "1-20")}),
    _fn("get_order", "One of the customer's orders.", {"id": (I_, "Order id")}, ["id"]),
    _fn("get_active_order", "The customer's currently open order, if any."),
    _fn("list_services", "Service types and categories."),
    _fn("get_quote", "Estimated price/ETA for a trip. Pickup defaults to the user's current location.",
        {**{f"pickup_{k}": v for k, v in _PLACE.items()}, **{f"destination_{k}": v for k, v in _PLACE.items()},
         "scheduled_at": (S_, "ISO datetime, optional"), "promo_code": (S_, "Optional")}, ["destination_place"]),
    _fn("get_wallet", "Wallet balance and latest entries."),
    _fn("list_payments", "Latest payments."),
    _fn("get_payment_methods", "Payment methods currently allowed."),
    _fn("validate_promo", "Check a promo/referral code.", {"code": (S_, "Code")}, ["code"]),
    _fn("create_order", "PROPOSE a new trip (needs user confirmation).",
        {**{f"pickup_{k}": v for k, v in _PLACE.items()}, **{f"destination_{k}": v for k, v in _PLACE.items()},
         "service_type": (S_, "PERSON or DELIVERY"), "passenger_count": (I_, "PERSON, default 1"),
         "cargo_description": (S_, "DELIVERY only"), "scheduled_at": (S_, "ISO datetime, optional"),
         "payment_method": (S_, "TAP or BENEFITPAY, optional"), "promo_code": (S_, "Optional"), "note": (S_, "Optional note for the driver")},
        ["destination_place", "service_type"]),
    _fn("cancel_order", "PROPOSE cancelling the order (default: the active one).", {"order_id": (I_, "Optional")}),
    _fn("raise_price", "PROPOSE raising the price of a searching order.", {"percent": (I_, "Percent over the original estimate"), "order_id": (I_, "Optional")}, ["percent"]),
    _fn("save_place", "PROPOSE saving a place.", {"label": (S_, "HOME, WORK or OTHER"), "name": (S_, "Name"), **_PLACE}, ["label", "name"]),
    _fn("edit_place", "PROPOSE editing a saved place.", {"place_id": (I_, "Id"), "label": (S_, "HOME|WORK|OTHER"), "name": (S_, "New name"),
                                                         "address": (S_, "New address"), "lat": (N_, ""), "lng": (N_, "")}, ["place_id"]),
    _fn("delete_place", "PROPOSE deleting a saved place.", {"place_id": (I_, "Id")}, ["place_id"]),
    _fn("update_profile", "PROPOSE changing full name and/or language (never phone/password).", {"full_name": (S_, "Name"), "language": (S_, "ar or en")}),
    _fn("rate_order", "PROPOSE rating a completed order's driver.", {"order_id": (I_, "Order id"), "stars": (I_, "1-5"), "comment": (S_, "Optional")}, ["order_id", "stars"]),
]
DRIVER = COMMON + [
    _fn("get_availability", "The driver's online mode."),
    _fn("get_earnings", "Earnings, including the last 7 days."),
    _fn("get_balance_and_debt", "Debt balance and latest payouts."),
    _fn("get_current_trip", "The driver's accepted/active trips."),
    _fn("list_offers", "Live trip offers."),
    _fn("get_documents", "Licence/documents status and work blockers."),
    _fn("list_work_areas", "Work areas and which are selected."),
    _fn("set_mode", "PROPOSE availability change.", {"mode": (S_, "LIVE, NORMAL or OFF")}, ["mode"]),
    _fn("set_work_areas", "PROPOSE the work areas.", {"area_ids": (S_, "Comma separated area ids, e.g. 1,2")}, ["area_ids"]),
    _fn("accept_offer", "PROPOSE accepting an offer (offers live only seconds).", {"offer_id": (I_, "Offer id")}, ["offer_id"]),
]


def declarations(user: User) -> list[dict]:
    return DRIVER if user.role == Role.DRIVER else CUSTOMER


def _clip(data, limit: int = 6000):
    s = json.dumps(data, ensure_ascii=False, default=str)
    return json.loads(s) if len(s) <= limit else {"truncated": True, "data": s[:limit]}


def _http_err(e: HTTPException) -> dict:
    d = e.detail
    return {"error": d if isinstance(d, (dict, str)) else str(d)}


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def _parse_dt(v: str | None):
    if not v:
        return None
    from datetime import datetime
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError("Invalid scheduled_at; use ISO format") from None
    return d if d.tzinfo else d.replace(tzinfo=_tz())


# ---------------- place resolution ----------------
async def _point(ctx: Ctx, args: dict, prefix: str, default_here: bool = False) -> dict:
    lat, lng, addr, place = args.get(f"{prefix}_lat"), args.get(f"{prefix}_lng"), args.get(f"{prefix}_address"), args.get(f"{prefix}_place")
    if lat is not None and lng is not None:
        return {"lat": float(lat), "lng": float(lng), "address": addr or place or ctx.t("الموقع المحدد", "Selected location")}
    if place:
        rows = (await ctx.db.execute(select(SavedPlace).where(SavedPlace.user_id == ctx.user.id))).scalars().all()
        key = str(place).strip().lower()
        for p in rows:
            if key in (p.label.lower(), p.name.lower()) or (key in p.name.lower() and len(key) >= 2):
                return {"lat": p.latitude, "lng": p.longitude, "address": p.address or p.name}
        if place.upper() in ("CURRENT", "HERE") and ctx.lat is not None:
            return {"lat": ctx.lat, "lng": ctx.lng, "address": ctx.t("موقعي الحالي", "My current location")}
        raise ToolError(f"Place '{place}' is not among the saved places and no coordinates were given. "
                        "Ask the user to pick a saved place or send the location (suggest_link /c/places).")
    if default_here:
        if ctx.lat is None:
            raise ToolError("Current location unknown. Ask the user to enable location or choose a saved place.")
        return {"lat": ctx.lat, "lng": ctx.lng, "address": ctx.t("موقعي الحالي", "My current location")}
    raise ToolError(f"{prefix} is required")


async def _trip_points(ctx: Ctx, a: dict) -> tuple[dict, dict]:
    return await _point(ctx, a, "pickup", default_here=True), await _point(ctx, a, "destination")


async def _quote(ctx: Ctx, pick: dict, dest: dict, scheduled_at, promo: str | None) -> dict:
    from ..routers import orders as r
    from ..schemas import EstimateIn
    body = EstimateIn(pickup_latitude=pick["lat"], pickup_longitude=pick["lng"], destination_latitude=dest["lat"],
                      destination_longitude=dest["lng"], scheduled_at=scheduled_at, promo_code=promo or None)
    return await r.estimate(body, ctx.user, ctx.db, ctx.lang)


# ---------------- read tools ----------------
async def _read(ctx: Ctx, name: str, a: dict):
    db, u = ctx.db, ctx.user
    lim = max(1, min(int(a.get("limit") or 5), 20))
    if name == "get_profile":
        from ..routers import profile
        return await profile.get_profile(u, db)
    if name == "get_notifications":
        from ..routers import profile
        return await profile.list_notifications(False, (lim, 0), u, db)
    if name == "list_saved_places":
        from ..routers import places
        return await places.list_places(u, db)
    if name == "list_orders":
        from ..routers import orders
        return await orders.my_orders(a.get("status") or None, (lim, 0), u, db)
    if name == "get_order":
        from ..routers import orders
        return await orders.get_order(int(a["id"]), u, db)
    if name == "get_active_order":
        oid = await _active_order_id(ctx)
        if not oid:
            return {"active_order": None}
        from ..routers import orders
        return await orders.get_order(oid, u, db)
    if name == "list_services":
        from ..routers.orders import _categories
        s = await settings_service.get_all(db)
        return {"service_types": ["PERSON", "DELIVERY"], "categories": _categories(s["service_categories"])}
    if name == "get_quote":
        pick, dest = await _trip_points(ctx, a)
        return await _quote(ctx, pick, dest, _parse_dt(a.get("scheduled_at")), a.get("promo_code"))
    if name == "get_wallet":
        from ..routers import v11
        return await v11.my_wallet(10, 0, u, db)
    if name == "list_payments":
        from ..routers import payments
        return await payments.my_payments(None, (lim, 0), u, db)
    if name == "get_payment_methods":
        from ..routers import payments
        return await payments.payment_methods(u, db)
    if name == "validate_promo":
        from .promo import resolve_code
        promo, ref = await resolve_code(db, await settings_service.get_all(db), a["code"], Decimal(1000), u.id)
        return {"valid": True, "percent": promo.percent if promo else None, "fixed_amount": str(ref.amount) if ref else None,
                "note": "Final discount is shown in the quote"}
    if name == "get_availability":
        from ..routers import driver
        return await driver.get_availability(u, db)
    if name == "get_earnings":
        from ..routers import driver
        out = await driver.earnings(u, db)
        wk = (await db.execute(select(func.coalesce(func.sum(Order.driver_earning), 0), func.count()).where(
            Order.driver_id == u.id, Order.order_status == S.COMPLETED, Order.completed_at >= utcnow() - timedelta(days=7)))).one()
        out["last_7_days"] = {"driver_earning": str(wk[0]), "trips": wk[1]}
        return out
    if name == "get_balance_and_debt":
        from ..routers import driver
        rows = (await db.execute(select(DriverPayout).where(DriverPayout.driver_id == u.id).order_by(DriverPayout.id.desc()).limit(5))).scalars().all()
        return {**await driver.my_balance(u, db), "latest_payouts": [{"amount": str(p.amount), "currency": p.currency, "status": p.status} for p in rows]}
    if name == "get_current_trip":
        from ..routers import driver
        return await driver.accepted((5, 0), u, db)
    if name == "list_offers":
        from ..routers import driver
        return await driver.offers("new", (10, 0), u, db)
    if name == "get_documents":
        from ..routers import profile
        from .dispatch import work_blockers
        p = await profile.get_profile(u, db)
        return {"driver": p.get("driver"), "work_blockers": work_blockers(u), "required_corrections": p.get("required_corrections")}
    if name == "list_work_areas":
        from ..routers import driver
        out = await driver.available_service_areas(u, db)
        return {"items": [{"id": i["id"], "name": i["name"], "selected": i["selected"]} for i in out["items"]]}
    if name == "suggest_link":
        route = str(a.get("route", ""))
        if not route.startswith(ROUTE_PREFIXES) or ".." in route or "://" in route:
            raise ToolError("Route not allowed")
        ctx.links.append({"label": str(a.get("label", ""))[:40], "route": route})
        return {"ok": True}
    raise ToolError("Unknown tool")


async def _active_order_id(ctx: Ctx) -> int | None:
    return (await ctx.db.execute(select(Order.id).where(Order.customer_id == ctx.user.id, Order.order_status.not_in(S.FINAL))
                                 .order_by(Order.id.desc()).limit(1))).scalar_one_or_none()


# ---------------- proposals ----------------
def card_out(a: ChatAction) -> dict:
    c = a.card or {}
    return {"id": a.id, "type": a.type, "title": c.get("title"), "summary": c.get("summary"), "details": c.get("details", []),
            "danger": bool(c.get("danger")), "expires_at": a.expires_at.isoformat()}


async def _store(ctx: Ctx, type_: str, params: dict, title: str, summary: str, details: list, danger: bool = False, **extra) -> dict:
    if len(ctx.actions) >= MAX_ACTIONS:
        raise ToolError("Maximum 3 actions per reply")
    s = await settings_service.get_all(ctx.db)
    if s["maintenance_mode"] == "true":
        raise ToolError(s["maintenance_message"] or "Service is under maintenance")
    a = ChatAction(id=str(uuid.uuid4()), conversation_id=ctx.conv_id, user_id=ctx.user.id, type=type_, params=params,
                   card={"title": title, "summary": summary, "details": details, "danger": danger, "lang": ctx.lang, **extra},
                   expires_at=utcnow() + timedelta(seconds=ACTION_TTL))
    ctx.db.add(a)
    await ctx.db.flush()
    ctx.actions.append(card_out(a))
    return {"proposed": True, "action_id": a.id, "note": "Shown to the user as a card; NOT executed until they confirm."}


def _d(label: str, value) -> dict:
    return {"label": label, "value": str(value)}


async def _allowed_methods(ctx: Ctx) -> list[str]:
    from ..routers import payments
    return (await payments.payment_methods(ctx.user, ctx.db))["methods"]


async def _propose_create_order(ctx: Ctx, a: dict) -> dict:
    st = str(a.get("service_type", "")).upper()
    if st not in ("PERSON", "DELIVERY"):
        raise ToolError("service_type must be PERSON or DELIVERY")
    if st == "DELIVERY" and not a.get("cargo_description"):
        raise ToolError("cargo_description is required for DELIVERY; ask the user what is being sent")
    if await _active_order_id(ctx):
        raise ToolError("The user already has an open order; it must finish or be cancelled first")
    pick, dest = await _trip_points(ctx, a)
    when = _parse_dt(a.get("scheduled_at"))
    method = (a.get("payment_method") or "").upper() or None
    if method and method not in await _allowed_methods(ctx):
        raise ToolError(f"Payment method {method} is not available now (allowed: {await _allowed_methods(ctx)})")
    q = await _quote(ctx, pick, dest, when, a.get("promo_code"))
    if q.get("pickup_in_service_area") is False:
        raise ToolError("The pickup point is outside the service areas")
    params = {"service_type": st, "pickup": pick, "destination": dest, "scheduled_at": when.isoformat() if when else None,
              "promo_code": a.get("promo_code") or None, "note": a.get("note"), "payment_method": method,
              "passenger_count": int(a.get("passenger_count") or 1), "cargo_description": a.get("cargo_description")}
    details = [_d(ctx.t("من", "From"), pick["address"]), _d(ctx.t("إلى", "To"), dest["address"]),
               _d(ctx.t("الخدمة", "Service"), ctx.t("توصيل راكب", "Ride") if st == "PERSON" else ctx.t("توصيل شحنة", "Parcel")),
               _d(ctx.t("السعر التقديري", "Estimated price"), f"{q['estimated_total']} {q['currency']}"),
               _d(ctx.t("الوقت", "When"), when.astimezone(_tz()).strftime("%Y-%m-%d %H:%M") if when else ctx.t("الآن", "Now"))]
    if method:
        details.append(_d(ctx.t("طريقة الدفع (بعد الرحلة)", "Payment method (after the trip)"), method))
    if params["promo_code"] and q.get("discount_amount"):
        details.append(_d(ctx.t("الخصم", "Discount"), f"{q['discount_amount']} {q['currency']}"))
    if st == "DELIVERY":
        details.append(_d(ctx.t("الشحنة", "Parcel"), params["cargo_description"]))
        details.append(_d(ctx.t("ملاحظة", "Note"), ctx.t("القبول مرهون بسعة المركبة والأنظمة", "Acceptance depends on vehicle capacity and law")))
    return await _store(ctx, "create_order", params, ctx.t("طلب رحلة جديدة", "New trip request"),
                        ctx.t("سيُرسل الطلب للسائقين عند التأكيد", "The request is sent to drivers once confirmed"), details,
                        quote_total=q["estimated_total"])


async def _order_for(ctx: Ctx, a: dict) -> Order:
    from ..routers.orders import load_order
    oid = int(a["order_id"]) if a.get("order_id") else await _active_order_id(ctx)
    o = await load_order(ctx.db, oid) if oid else None
    if not o or o.customer_id != ctx.user.id:
        raise ToolError("Order not found")
    return o


async def _propose(ctx: Ctx, name: str, a: dict) -> dict:
    u, t = ctx.user, ctx.t
    if name == "create_order":
        return await _propose_create_order(ctx, a)
    if name == "cancel_order":
        from ..routers.orders import customer_cancel_fee
        o = await _order_for(ctx, a)
        if o.order_status in S.FINAL:
            raise ToolError("The order is already finished")
        fee = customer_cancel_fee(o, await settings_service.get_all(ctx.db))
        fee_txt = f"{fee['fee']:.{o.currency_decimals}f} {o.currency}" if not fee["free"] else t("بدون رسوم", "Free")
        return await _store(ctx, "cancel_order", {"order_id": o.id}, t("إلغاء الطلب", "Cancel order"), t(f"طلب رقم {o.id}", f"Order #{o.id}"),
                            [_d(t("الطلب", "Order"), f"#{o.id}"), _d(t("الحالة", "Status"), o.order_status), _d(t("رسوم الإلغاء", "Cancellation fee"), fee_txt)], danger=True)
    if name == "raise_price":
        from . import price_increase as pi
        o = await _order_for(ctx, a)
        pct = int(a["percent"])
        if o.order_status not in S.SEARCHING:
            raise ToolError("The order is not searching for a driver")
        pi.check_percent(pi.config(await settings_service.get_all(ctx.db)), pct, o.price_increase_percent)
        try:
            est = pi.new_estimate(o, pct)
        except Exception:  # noqa: BLE001
            est = None
        d = [_d(t("الطلب", "Order"), f"#{o.id}"), _d(t("نسبة الزيادة", "Increase"), f"{pct}%")]
        if est:
            d.append(_d(t("السعر الجديد", "New price"), est if isinstance(est, str) else json.dumps(est, default=str)))
        return await _store(ctx, "raise_price", {"order_id": o.id, "percent": pct}, t("رفع سعر الطلب", "Raise the price"), t("يُعاد إرسال الطلب للسائقين", "Drivers get the request again"), d)
    if name == "save_place":
        pt = await _point(ctx, {"p_lat": a.get("lat"), "p_lng": a.get("lng"), "p_place": a.get("place"), "p_address": a.get("address")}, "p", default_here=True)
        lab = str(a["label"]).upper()
        if lab not in ("HOME", "WORK", "OTHER"):
            raise ToolError("label must be HOME, WORK or OTHER")
        return await _store(ctx, "save_place", {"label": lab, "name": a["name"], "address": a.get("address") or pt["address"], "lat": pt["lat"], "lng": pt["lng"]},
                            t("حفظ مكان", "Save place"), a["name"], [_d(t("الاسم", "Name"), a["name"]), _d(t("النوع", "Type"), lab), _d(t("العنوان", "Address"), a.get("address") or pt["address"])])
    if name in ("edit_place", "delete_place"):
        p = await ctx.db.get(SavedPlace, int(a["place_id"]))
        if not p or p.user_id != u.id:
            raise ToolError("Place not found")
        if name == "delete_place":
            return await _store(ctx, "delete_place", {"place_id": p.id}, t("حذف مكان", "Delete place"), p.name, [_d(t("المكان", "Place"), p.name), _d(t("العنوان", "Address"), p.address)], danger=True)
        ch = {k: a[k] for k in ("label", "name", "address", "lat", "lng") if a.get(k) is not None}
        if not ch:
            raise ToolError("Nothing to change")
        return await _store(ctx, "edit_place", {"place_id": p.id, **ch}, t("تعديل مكان", "Edit place"), p.name, [_d(t("المكان", "Place"), p.name)] + [_d(k, v) for k, v in ch.items()])
    if name == "update_profile":
        ch = {k: a[k] for k in ("full_name", "language") if a.get(k)}
        if not ch or ch.get("language", "ar") not in ("ar", "en"):
            raise ToolError("Provide full_name and/or language (ar|en)")
        return await _store(ctx, "update_profile", ch, t("تعديل الملف الشخصي", "Update profile"), "", [_d(k, v) for k, v in ch.items()])
    if name == "rate_order":
        o = await _order_for(ctx, a)
        if o.order_status != S.COMPLETED:
            raise ToolError("Only completed orders can be rated")
        stars = int(a["stars"])
        if not 1 <= stars <= 5:
            raise ToolError("stars must be 1-5")
        return await _store(ctx, "rate_order", {"order_id": o.id, "stars": stars, "comment": a.get("comment")}, t("تقييم السائق", "Rate the driver"), f"#{o.id}",
                            [_d(t("الطلب", "Order"), f"#{o.id}"), _d(t("النجوم", "Stars"), "★" * stars)] + ([_d(t("تعليق", "Comment"), a["comment"])] if a.get("comment") else []))
    if name == "set_mode":
        from .dispatch import work_blockers
        mode = str(a["mode"]).upper()
        if mode not in ("LIVE", "NORMAL", "OFF"):
            raise ToolError("mode must be LIVE, NORMAL or OFF")
        if mode != "OFF" and work_blockers(u):
            raise ToolError(f"The driver cannot go online now: {work_blockers(u)}")
        return await _store(ctx, "set_mode", {"mode": mode}, t("تغيير حالة العمل", "Change availability"), mode, [_d(t("الوضع", "Mode"), mode)], danger=mode == "OFF")
    if name == "set_work_areas":
        ids = sorted({int(x) for x in re.findall(r"\d+", str(a["area_ids"]))})
        if not ids:
            raise ToolError("area_ids required")
        return await _store(ctx, "set_work_areas", {"service_area_ids": ids}, t("تحديد مناطق العمل", "Set work areas"), "", [_d(t("المناطق", "Areas"), ", ".join(map(str, ids)))])
    if name == "accept_offer":
        return await _store(ctx, "accept_offer", {"offer_id": int(a["offer_id"])}, t("قبول عرض رحلة", "Accept trip offer"), f"#{a['offer_id']}",
                            [_d(t("العرض", "Offer"), f"#{a['offer_id']}"), _d(t("تنبيه", "Note"), t("العروض تنتهي خلال ثوانٍ", "Offers expire within seconds"))])
    raise ToolError("Unknown tool")


PROPOSALS = {"create_order", "cancel_order", "raise_price", "save_place", "edit_place", "delete_place", "update_profile", "rate_order",
             "set_mode", "set_work_areas", "accept_offer"}


async def run_tool(ctx: Ctx, name: str, args: dict) -> dict:
    allowed = {d["name"] for d in declarations(ctx.user)}
    if name not in allowed:
        return {"error": "Tool not available for this account"}
    try:
        if name in PROPOSALS:
            return await _propose(ctx, name, args or {})
        return _clip(await _read(ctx, name, args or {}))
    except ToolError as e:
        return {"error": str(e)}
    except HTTPException as e:
        await ctx.db.rollback()
        return _http_err(e)
    except (KeyError, ValueError, TypeError) as e:
        return {"error": f"Invalid arguments: {e}"}
    except Exception as e:  # noqa: BLE001  validation errors etc. are reported to the model, never to the user raw
        from pydantic import ValidationError
        if isinstance(e, ValidationError):
            return {"error": "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())[:400]}
        raise


# ---------------- execution (after the user confirms) ----------------
DONE = {"create_order": ("تم إرسال طلبك", "Your trip request was sent"), "cancel_order": ("تم إلغاء الطلب", "The order was cancelled"),
        "raise_price": ("تم رفع السعر", "The price was raised"), "save_place": ("تم حفظ المكان", "Place saved"),
        "edit_place": ("تم تعديل المكان", "Place updated"), "delete_place": ("تم حذف المكان", "Place deleted"),
        "update_profile": ("تم تحديث الملف الشخصي", "Profile updated"), "rate_order": ("شكرًا لتقييمك", "Thanks for your rating"),
        "set_mode": ("تم تغيير حالة العمل", "Availability updated"), "set_work_areas": ("تم تحديث مناطق العمل", "Work areas updated"),
        "accept_offer": ("تم قبول العرض", "Offer accepted")}


async def _execute(db: AsyncSession, user: User, type_: str, p: dict, action_id: str) -> tuple[dict, str | None]:
    from ..routers import driver, orders, places, profile, ratings
    from ..schemas import AvailabilityIn, DriverAreasIn, OptionalReasonIn, PlaceIn, PlacePatch, PriceRaiseIn, ProfilePatch, RatingIn
    if type_ == "create_order":
        from ..schemas import OrderCreateIn
        st = p["service_type"]
        body = OrderCreateIn(
            service_type=st, pickup_address=p["pickup"]["address"][:300], pickup_latitude=p["pickup"]["lat"], pickup_longitude=p["pickup"]["lng"],
            destination_address=p["destination"]["address"][:300], destination_latitude=p["destination"]["lat"], destination_longitude=p["destination"]["lng"],
            scheduled_at=p.get("scheduled_at"), promo_code=p.get("promo_code"), customer_notes=p.get("note"), client_request_id=action_id,
            passenger_count=p.get("passenger_count") if st == "PERSON" else None, cargo_description=p.get("cargo_description") if st == "DELIVERY" else None,
            accept_capacity_terms=st == "DELIVERY")
        out = await orders.create_order(body, user, db)
        oid = out.get("id") or out.get("order_id")
        return {"order_id": oid}, f"/c/order/{oid}/searching" if not p.get("scheduled_at") else f"/c/order/{oid}"
    if type_ == "cancel_order":
        out = await orders.cancel_order(p["order_id"], OptionalReasonIn(reason="Cancelled via assistant"), user, db)
        return {"order_id": p["order_id"], "cancel_fee_info": out.get("cancel_fee_info")}, f"/c/order/{p['order_id']}"
    if type_ == "raise_price":
        await orders.raise_order_price(p["order_id"], PriceRaiseIn(percent=p["percent"]), user, db)
        return {"order_id": p["order_id"]}, f"/c/order/{p['order_id']}/searching"
    if type_ == "save_place":
        out = await places.create_place(PlaceIn(**p), user, db)
        return {"place_id": out["id"]}, "/c/places"
    if type_ == "edit_place":
        data = {k: v for k, v in p.items() if k != "place_id"}
        await places.patch_place(p["place_id"], PlacePatch(**data), user, db)
        return {"place_id": p["place_id"]}, "/c/places"
    if type_ == "delete_place":
        await places.delete_place(p["place_id"], user, db)
        return {"place_id": p["place_id"]}, "/c/places"
    if type_ == "update_profile":
        await profile.patch_profile(ProfilePatch(**p), user, db)
        return {}, "/profile"
    if type_ == "rate_order":
        await ratings.customer_rates_driver(p["order_id"], RatingIn(stars=p["stars"], comment=p.get("comment")), user, db)
        return {"order_id": p["order_id"]}, None
    if type_ == "set_mode":
        out = await driver.availability(AvailabilityIn(mode=p["mode"]), user, db)
        return out, None
    if type_ == "set_work_areas":
        await driver.set_my_service_areas(DriverAreasIn(service_area_ids=p["service_area_ids"]), user, db)
        return {"service_area_ids": p["service_area_ids"]}, None
    if type_ == "accept_offer":
        out = await driver.accept_offer(p["offer_id"], user, db)
        oid = out.get("id") or out.get("order_id")
        return {"offer_id": p["offer_id"], "order_id": oid}, f"/d/trip/{oid}" if oid else None
    raise HTTPException(400, {"code": "UNKNOWN_ACTION"})


async def _reset(db: AsyncSession, action_id: str) -> None:
    await db.rollback()
    a = await db.get(ChatAction, action_id)
    if a and a.status == "RUNNING":
        a.status = "PENDING"  # the user can retry until it expires
        await db.commit()


async def confirm(db: AsyncSession, user: User, action_id: str) -> dict:
    a = (await db.execute(select(ChatAction).where(ChatAction.id == action_id).with_for_update())).scalar_one_or_none()
    if not a or a.user_id != user.id:
        raise HTTPException(404, {"code": "ACTION_NOT_FOUND"})
    if a.status != "PENDING":
        raise HTTPException(409, {"code": "ACTION_ALREADY_DONE"})
    if a.expires_at <= utcnow():
        raise HTTPException(410, {"code": "ACTION_EXPIRED"})
    uid, type_, params, card, conv = user.id, a.type, dict(a.params or {}), dict(a.card or {}), a.conversation_id
    lang = card.get("lang", "ar")
    a.status = "RUNNING"  # blocks a double confirm while the handlers (which commit) run
    await db.commit()
    if type_ == "create_order":  # re-validate the quote
        ctx = Ctx(db, user, conv, lang, None, None)
        try:
            q = await _quote(ctx, params["pickup"], params["destination"], _parse_dt(params.get("scheduled_at")), params.get("promo_code"))
        except HTTPException:
            await _reset(db, action_id)
            raise
        old, new = Decimal(card.get("quote_total") or 0), Decimal(q["estimated_total"])
        if old > 0 and abs(new - old) / old > QUOTE_TOLERANCE:
            a = await db.get(ChatAction, action_id)
            a.status = "CANCELLED"
            args = {"service_type": params["service_type"], "pickup_lat": params["pickup"]["lat"], "pickup_lng": params["pickup"]["lng"],
                    "pickup_address": params["pickup"]["address"], "destination_lat": params["destination"]["lat"],
                    "destination_lng": params["destination"]["lng"], "destination_address": params["destination"]["address"],
                    "scheduled_at": params.get("scheduled_at"), "promo_code": params.get("promo_code"), "note": params.get("note"),
                    "payment_method": params.get("payment_method"), "passenger_count": params.get("passenger_count"),
                    "cargo_description": params.get("cargo_description")}
            ctx = Ctx(db, user, conv, lang, None, None)
            await _propose_create_order(ctx, args)
            await db.commit()
            raise HTTPException(409, {"code": "QUOTE_CHANGED", "action": ctx.actions[0]})
    try:
        result, route = await _execute(db, user, type_, params, action_id)
    except HTTPException:
        await _reset(db, action_id)
        raise
    except Exception as e:  # noqa: BLE001
        await _reset(db, action_id)
        from pydantic import ValidationError
        if isinstance(e, ValidationError):
            raise HTTPException(422, {"code": "ACTION_INVALID", "message": str(e.errors()[0]["msg"])}) from e
        raise
    a = await db.get(ChatAction, action_id)
    a.status, a.result, a.executed_at = "DONE", result, utcnow()
    audit(db, uid, "CHAT_ACTION_EXECUTED", "chat_action", action_id, details={"type": type_, "params": params, "result": result})
    await db.commit()
    ar, en = DONE.get(type_, ("تم التنفيذ", "Done"))
    return {"ok": True, "message": en if lang == "en" else ar, "route": route, "result": result}


async def cancel(db: AsyncSession, user: User, action_id: str) -> dict:
    a = (await db.execute(select(ChatAction).where(ChatAction.id == action_id).with_for_update())).scalar_one_or_none()
    if not a or a.user_id != user.id:
        raise HTTPException(404, {"code": "ACTION_NOT_FOUND"})
    if a.status != "PENDING":
        raise HTTPException(409, {"code": "ACTION_ALREADY_DONE"})
    a.status = "CANCELLED"
    await db.commit()
    return {"ok": True}
