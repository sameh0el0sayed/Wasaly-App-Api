"""Periodic, fail-closed review of submitted identity and BenefitPay proof documents."""
import asyncio
import base64
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

import httpx
from sqlalchemy import select

from ..config import settings
from ..constants import AccountStatus, Role
from ..database import SessionLocal
from ..models import DriverProfile, Order, Payment, User, UploadedFile
from ..security import decrypt
from ..services import settings_service
from ..services.files import get_bytes

log = logging.getLogger("ai-review")
_DIGIT_TRANSLATION = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _normalize_alnum(value: str | None) -> str:
    value = (value or "").translate(_DIGIT_TRANSLATION).upper()
    return "".join(ch for ch in value if ch.isalnum())


def _normalize_name(value: str | None) -> str:
    value = (value or "").translate(_DIGIT_TRANSLATION).casefold()
    return " ".join(re.findall(r"[^\W_]", value, flags=re.UNICODE))


def _account_matches(receipt_account: str | None, registered_account: str | None) -> bool:
    receipt_raw = (receipt_account or "").translate(_DIGIT_TRANSLATION).upper().strip()
    expected = _normalize_alnum(registered_account)
    observed = _normalize_alnum(receipt_raw)
    if not expected or not observed:
        return False
    if observed == expected:
        return True
    masked = any(c in receipt_raw for c in "*•Xx…")
    return masked and len(observed) >= 4 and expected.endswith(observed)


def _amount_matches(receipt_amount, expected_amount, decimals: int) -> bool:
    try:
        raw = str(receipt_amount).translate(_DIGIT_TRANSLATION).replace(",", "").strip()
        quantum = Decimal(1).scaleb(-decimals)
        return Decimal(raw).quantize(quantum) == Decimal(str(expected_amount)).quantize(quantum)
    except (InvalidOperation, ValueError, TypeError):
        return False


def compare_receipt_fields(extracted: dict, expected_account: str,
                           expected_amount, currency: str, decimals: int) -> tuple[bool, list[str]]:
    """Decide only from server-side comparisons; never trust a model-provided pass/fail field."""
    reasons = []
    if not _account_matches(extracted.get("beneficiary_account"), expected_account):
        reasons.append("ACCOUNT_MISMATCH")
    if not _amount_matches(extracted.get("amount"), expected_amount, decimals):
        reasons.append("AMOUNT_MISMATCH")
    if (extracted.get("currency") or "").upper() != currency.upper():
        reasons.append("CURRENCY_MISMATCH")
    if not _normalize_alnum(extracted.get("reference")):
        reasons.append("REFERENCE_MISSING")
    return not reasons, reasons


async def _ai_token(client: httpx.AsyncClient) -> str:
    if not settings.ai_service_phone or not settings.ai_service_password:
        raise RuntimeError("AI_SERVICE_PHONE/PASSWORD not configured")
    base = settings.ai_service_base_url.rstrip("/")
    response = await client.post(f"{base}/api/auth/login", json={
        "phone": settings.ai_service_phone, "password": settings.ai_service_password,
    })
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("AI service login did not return a token")
    return token


async def _gemini_json(prompt: str, files: list[tuple[str, bytes]]) -> dict:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")
    parts = [{"text": prompt}]
    parts.extend({"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}}
                 for mime, data in files)
    generation = {"responseMimeType": "application/json", "maxOutputTokens": 512}
    if settings.gemini_model.startswith("gemini-3"):
        generation["thinkingConfig"] = {"thinkingLevel": settings.gemini_thinking_level}
    else:
        generation["temperature"] = 0.1
    payload = {"contents": [{"role": "user", "parts": parts}], "generationConfig": generation}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent"
    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.gemini_timeout_seconds)) as client:
        for attempt in range(3):
            try:
                response = await client.post(
                    url, headers={"x-goog-api-key": settings.gemini_api_key}, json=payload,
                )
                if response.status_code not in {429, 500, 502, 503, 504}:
                    response.raise_for_status()
                    break
                if attempt == 2:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(30.0, max(1.0, float(retry_after))) if retry_after else 2.0 ** (attempt + 1)
                except ValueError:
                    delay = 2.0 ** (attempt + 1)
                log.warning("Gemini transient HTTP %s; retry %s/2 in %.1fs",
                            response.status_code, attempt + 1, delay)
                await asyncio.sleep(delay)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                delay = 2.0 ** (attempt + 1)
                log.warning("Gemini transport error; retry %s/2 in %.1fs", attempt + 1, delay)
                await asyncio.sleep(delay)
    response.raise_for_status()
    text = "".join(part.get("text", "") for part in response.json()["candidates"][0]["content"]["parts"] if "text" in part)
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("Gemini did not return a JSON object")
    return result


async def _admin_post(token: str, path: str, body: dict | None = None) -> None:
    base = settings.ai_service_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{base}{path}", headers={"Authorization": f"Bearer {token}"}, json=body or {})
    response.raise_for_status()


async def review_payment(payment_id: int, token: str) -> bool:
    async with SessionLocal() as db:
        payment = await db.get(Payment, payment_id)
        if not payment or payment.payment_method != "BENEFITPAY" or payment.payment_status != "PENDING_VERIFICATION":
            return False
        if payment.kind != "ORDER" or not payment.order_id or not payment.proof_file:
            return False
        order = await db.get(Order, payment.order_id)
        proof = await db.get(UploadedFile, payment.proof_file)
        if not order or not proof or proof.content_type not in {
                "image/jpeg", "image/png", "image/webp", "application/pdf"}:
            return False
        app_settings = await settings_service.get_all(db)
        target_account = app_settings.get("benefitpay_account_number", "")
        if not target_account:
            return False
        expected_amount = Decimal(payment.amount) - Decimal(payment.wallet_amount or 0)
        currency, decimals = payment.currency, payment.currency_decimals
        proof_key, content_type = payment.proof_file, proof.content_type

    extracted = await _gemini_json(
        "Read the attached BenefitPay transfer receipt. Extract only fields visibly present: transfer reference/Ref#, "
        "beneficiary account, transferred amount, and currency. Ignore dates completely. "
        "Return strict JSON with reference as the exact visible reference string, beneficiary_account as the visible "
        "account string (preserve masking characters), amount as a decimal number, and currency as ISO 4217 code. "
        "Use null for unclear or missing fields. Do not infer missing digits, reference characters, currency, or values. "
        "Do not decide whether payment is valid. JSON keys: reference, beneficiary_account, amount, currency.",
        [(content_type, await get_bytes(proof_key))],
    )
    accepted, reasons = compare_receipt_fields(extracted, target_account, expected_amount, currency, decimals)
    if not accepted:
        log.info("AI receipt field mismatch payment_id=%s codes=%s", payment_id, ",".join(reasons))
        return False
    review_notes = {
        "beneficiary_account": extracted.get("beneficiary_account"),
        "amount": str(extracted.get("amount")) if extracted.get("amount") is not None else None,
        "currency": extracted.get("currency"),
        "reference": extracted.get("reference"),
        "expected_account": target_account,
        "expected_amount": str(expected_amount),
        "expected_currency": currency,
        "account_match": True,
        "amount_match": True,
        "currency_match": True,
    }
    await _admin_post(token, f"/api/admin/payments/{payment_id}/confirm", {
        "reference": extracted["reference"], "review_notes": review_notes,
    })
    log.info("AI receipt field match payment_id=%s", payment_id)
    return True


def _name_matches(candidate: str | None, expected: str | None) -> bool:
    a = _normalize_name(candidate)
    b = _normalize_name(expected)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.9


async def review_user(user_id: int, token: str) -> bool:
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        if not user or user.role not in {Role.CUSTOMER, Role.DRIVER} or user.account_status != AccountStatus.PENDING_APPROVAL:
            return False
        if not user.cpr_photo or not user.cpr_encrypted or not user.cpr_issue_date or not user.cpr_expiry_date:
            return False
        cpr_doc = await db.get(UploadedFile, user.cpr_photo)
        if not cpr_doc:
            return False
        files = [(cpr_doc.content_type, await get_bytes(user.cpr_photo))]
        labels = ["Bahrain CPR card"]
        expected = {"name": user.full_name, "cpr": decrypt(user.cpr_encrypted),
                    "cpr_issue_date": user.cpr_issue_date.isoformat(),
                    "cpr_expiry_date": user.cpr_expiry_date.isoformat()}
        driver = (await db.execute(select(DriverProfile).where(DriverProfile.user_id == user.id))).scalar_one_or_none()
        if user.role == Role.DRIVER:
            if not driver or not driver.license_document:
                return False
            license_doc = await db.get(UploadedFile, driver.license_document)
            if not license_doc:
                return False
            files.append((license_doc.content_type, await get_bytes(driver.license_document)))
            labels.append("driver license")
            expected["license_number"] = decrypt(driver.license_number_encrypted)
            expected["license_expiry_date"] = driver.license_expiry_date.isoformat()
        role = user.role

    extracted = await _gemini_json(
        "Read the attached identity documents for an account review. Document order: " + json.dumps(labels) + ". "
        "Return strict JSON only with cpr_name, cpr_number, cpr_issue_date, cpr_expiry_date, "
        "license_number, license_expiry_date, confidence. Dates must be YYYY-MM-DD. Copy only visible text; "
        "do not infer. confidence must be high, medium, or low.", files,
    )
    matches = (
        extracted.get("confidence") == "high"
        and _normalize_alnum(extracted.get("cpr_number")) == _normalize_alnum(expected["cpr"])
        and _name_matches(extracted.get("cpr_name"), expected["name"])
        and extracted.get("cpr_issue_date") == expected["cpr_issue_date"]
        and extracted.get("cpr_expiry_date") == expected["cpr_expiry_date"]
    )
    if role == Role.DRIVER:
        matches = matches and (
            _normalize_alnum(extracted.get("license_number")) == _normalize_alnum(expected["license_number"])
            and extracted.get("license_expiry_date") == expected["license_expiry_date"]
        )
    if not matches:
        log.info("AI identity field mismatch user_id=%s", user_id)
        return False
    await _admin_post(token, f"/api/admin/users/{user_id}/approve", {"approve_pending_documents": False})
    log.info("AI identity fields matched user_id=%s role=%s", user_id, role)
    return True


async def run_review_cycle() -> None:
    if not settings.ai_service_phone or not settings.ai_service_password or not settings.gemini_api_key:
        log.warning("AI review disabled: configure AI_SERVICE_PHONE, AI_SERVICE_PASSWORD, GEMINI_API_KEY")
        return
    async with httpx.AsyncClient(timeout=20) as client:
        token = await _ai_token(client)
    async with SessionLocal() as db:
        user_ids = list((await db.execute(select(User.id).where(
            User.role.in_([Role.CUSTOMER, Role.DRIVER]), User.account_status == AccountStatus.PENDING_APPROVAL
        ).order_by(User.created_at).limit(settings.ai_review_batch_size))).scalars())
        payment_ids = list((await db.execute(select(Payment.id).where(
            Payment.payment_method == "BENEFITPAY", Payment.payment_status == "PENDING_VERIFICATION"
        ).order_by(Payment.created_at).limit(settings.ai_review_batch_size))).scalars())
    for user_id in user_ids:
        try:
            await review_user(user_id, token)
        except Exception:
            log.exception("AI identity review failed user_id=%s", user_id)
    for payment_id in payment_ids:
        try:
            await review_payment(payment_id, token)
        except Exception:
            log.exception("AI receipt review failed payment_id=%s", payment_id)


async def review_loop() -> None:
    interval = max(60, settings.ai_review_interval_seconds)
    while True:
        try:
            await run_review_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("AI review cycle failed")
        await asyncio.sleep(interval)
