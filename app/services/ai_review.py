"""Periodic, fail-closed review of submitted identity and BenefitPay proof documents."""
import asyncio
import base64
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

import httpx
from sqlalchemy import select

from ..config import settings
from ..constants import AccountStatus, Role
from ..database import SessionLocal
from ..models import AdminAuditLog, DriverProfile, Order, Payment, ProfileChange, UploadedFile, User
from ..security import decrypt, normalize_phone
from ..services import corrections as correction_service
from ..services import settings_service
from ..services.audit import audit
from ..services.files import get_bytes
from ..services.notifications import notify

log = logging.getLogger("ai-review")
_DIGIT_TRANSLATION = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_SENSITIVE_CORRECTION_FIELDS = {
    "cpr_photo", "cpr_number", "cpr_dates", "license", "license.photo_front",
    "license.photo_back", "license.number", "license.expiry_date",
}


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
    for attempt in range(5):
        try:
            response = await client.post(f"{base}/api/auth/login", json={
                "phone": settings.ai_service_phone, "password": settings.ai_service_password,
            })
            break
        except httpx.ConnectError:
            if attempt == 4:
                raise
            delay = min(4, 0.5 * (2 ** attempt))
            log.warning("AI service login connection failed; retry %s/4 in %.1fs", attempt + 1, delay)
            await asyncio.sleep(delay)
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("AI service login did not return a token")
    return token


async def _gemini_json(prompt: str, files: list[tuple[str, bytes]]) -> dict:
    from . import command
    g = await command.gemini()
    if not g["key"]:
        raise RuntimeError("GEMINI_API_KEY not configured")
    parts = [{"text": prompt}]
    parts.extend({"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}}
                 for mime, data in files)
    generation = {"responseMimeType": "application/json", "maxOutputTokens": 512}
    if g["model"].startswith("gemini-3"):
        generation["thinkingConfig"] = {"thinkingLevel": settings.gemini_thinking_level}
    else:
        generation["temperature"] = 0.1
    payload = {"contents": [{"role": "user", "parts": parts}], "generationConfig": generation}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{g['model']}:generateContent"
    async with httpx.AsyncClient(timeout=httpx.Timeout(g["timeout"])) as client:
        for attempt in range(3):
            try:
                response = await client.post(
                    url, headers={"x-goog-api-key": g["key"]}, json=payload,
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


async def _write_worker_audit(action: str, details: dict, *,
                              entity_type: str = "system", entity_id: str = "ai-review-worker") -> None:
    try:
        async with SessionLocal() as db:
            db.add(AdminAuditLog(
                admin_id=None,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                details=details,
            ))
            await db.commit()
    except Exception:
        log.exception("Could not write AI worker audit action=%s", action)


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
        await _write_worker_audit("AI_RECEIPT_REVIEW", {
            "payment_id": payment_id,
            "result": "not_confirmed",
            "reason_codes": reasons,
            "reference": extracted.get("reference"),
        }, entity_type="payment", entity_id=str(payment_id))
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
    await _write_worker_audit("AI_RECEIPT_REVIEW", {
        "payment_id": payment_id,
        "result": "confirmed",
        "review_notes": review_notes,
    }, entity_type="payment", entity_id=str(payment_id))
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
        if (not user or user.role not in {Role.CUSTOMER, Role.DRIVER}
                or user.account_status != AccountStatus.PENDING_APPROVAL or not user.phone_verified):
            await _write_worker_audit("AI_IDENTITY_REVIEW", {
                "user_id": user_id, "result": "not_approved", "reason_codes": ["USER_NOT_PENDING_REVIEW"],
            }, entity_type="user", entity_id=str(user_id))
            return False
        if not user.cpr_photo or not user.cpr_encrypted or not user.cpr_issue_date or not user.cpr_expiry_date:
            missing = [name for name, value in {
                "cpr_photo": user.cpr_photo, "cpr_number": user.cpr_encrypted,
                "cpr_issue_date": user.cpr_issue_date, "cpr_expiry_date": user.cpr_expiry_date,
            }.items() if not value]
            await _write_worker_audit("AI_IDENTITY_REVIEW", {
                "user_id": user_id, "result": "not_approved", "reason_codes": ["MISSING_CPR_DATA"],
                "missing_fields": missing,
            }, entity_type="user", entity_id=str(user_id))
            return False
        cpr_doc = await db.get(UploadedFile, user.cpr_photo)
        if not cpr_doc:
            await _write_worker_audit("AI_IDENTITY_REVIEW", {
                "user_id": user_id, "result": "not_approved", "reason_codes": ["CPR_FILE_NOT_FOUND"],
            }, entity_type="user", entity_id=str(user_id))
            return False
        files = [(cpr_doc.content_type, await get_bytes(user.cpr_photo))]
        labels = ["Bahrain CPR card"]
        expected = {"name": user.full_name, "cpr": decrypt(user.cpr_encrypted),
                    "cpr_issue_date": user.cpr_issue_date.isoformat(),
                    "cpr_expiry_date": user.cpr_expiry_date.isoformat()}
        driver = (await db.execute(select(DriverProfile).where(DriverProfile.user_id == user.id))).scalar_one_or_none()
        if user.role == Role.DRIVER:
            if not driver or not driver.license_document:
                await _write_worker_audit("AI_IDENTITY_REVIEW", {
                    "user_id": user_id, "result": "not_approved", "reason_codes": ["MISSING_DRIVER_LICENSE"],
                }, entity_type="user", entity_id=str(user_id))
                return False
            license_doc = await db.get(UploadedFile, driver.license_document)
            if not license_doc:
                await _write_worker_audit("AI_IDENTITY_REVIEW", {
                    "user_id": user_id, "result": "not_approved", "reason_codes": ["LICENSE_FILE_NOT_FOUND"],
                }, entity_type="user", entity_id=str(user_id))
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
    field_matches = {
        "confidence_high": extracted.get("confidence") == "high",
        "cpr_number_match": _normalize_alnum(extracted.get("cpr_number")) == _normalize_alnum(expected["cpr"]),
        "cpr_name_match": _name_matches(extracted.get("cpr_name"), expected["name"]),
        "cpr_issue_date_match": extracted.get("cpr_issue_date") == expected["cpr_issue_date"],
        "cpr_expiry_date_match": extracted.get("cpr_expiry_date") == expected["cpr_expiry_date"],
    }
    if role == Role.DRIVER:
        field_matches["license_number_match"] = (
            _normalize_alnum(extracted.get("license_number")) == _normalize_alnum(expected["license_number"])
        )
        field_matches["license_expiry_date_match"] = (
            extracted.get("license_expiry_date") == expected["license_expiry_date"]
        )
    matches = all(field_matches.values())
    if not matches:
        log.info("AI identity field mismatch user_id=%s", user_id)
        mismatch_fields = [name for name, matched in field_matches.items() if not matched]
        if role == Role.CUSTOMER:
            reason = (
                "تم اعتماد الحساب، لكن بيانات البطاقة لم تتطابق بالكامل مع البيانات المسجلة. "
                "يرجى تحديث الصورة الشخصية وإعادة رفع صورة واضحة للبطاقة لاستكمال التحقق. "
                "حقول المراجعة غير المتطابقة: " + ", ".join(mismatch_fields)
            )
            await _admin_post(token, f"/api/admin/users/{user_id}/request-correction", {
                "reason": reason,
                "fields": [
                    {"field": "profile_photo", "reason": "يرجى رفع صورة شخصية واضحة وحديثة لصاحب الحساب."},
                    {"field": "cpr_photo", "reason": reason},
                ],
            })
            await _admin_post(token, f"/api/admin/users/{user_id}/approve", {
                "approve_pending_documents": False,
            })
            await _write_worker_audit("AI_IDENTITY_REVIEW", {
                "user_id": user_id, "result": "approved_with_correction_request",
                "reason_codes": mismatch_fields,
                "corrections_requested": ["profile_photo", "cpr_photo"],
                "correction_reason": reason,
                "field_matches": field_matches,
            }, entity_type="user", entity_id=str(user_id))
            return True
        await _write_worker_audit("AI_IDENTITY_REVIEW", {
            "user_id": user_id, "result": "not_approved",
            "reason_codes": mismatch_fields,
            "field_matches": field_matches,
        }, entity_type="user", entity_id=str(user_id))
        return False
    await _admin_post(token, f"/api/admin/users/{user_id}/approve", {"approve_pending_documents": False})
    log.info("AI identity fields matched user_id=%s role=%s", user_id, role)
    await _write_worker_audit("AI_IDENTITY_REVIEW", {
        "user_id": user_id, "result": "approved", "field_matches": field_matches,
    }, entity_type="user", entity_id=str(user_id))
    return True


async def _sensitive_correction_match(field: str, value: str, expected: dict,
                                      proof: tuple[str, bytes] | None) -> tuple[bool, list[str], dict]:
    matches: dict[str, bool] = {}
    if field == "cpr_number":
        matches["cpr_number_match"] = _normalize_alnum(value) == _normalize_alnum(expected.get("cpr"))
    elif field == "cpr_dates":
        try:
            supplied = json.loads(value)
        except (TypeError, ValueError):
            supplied = {}
        if supplied.get("issue_date"):
            matches["cpr_issue_date_match"] = supplied["issue_date"] == expected.get("cpr_issue_date")
        if supplied.get("expiry_date"):
            matches["cpr_expiry_date_match"] = supplied["expiry_date"] == expected.get("cpr_expiry_date")
        if not matches:
            return False, ["NO_CPR_DATES_TO_COMPARE"], matches
    elif field == "license.number":
        matches["license_number_match"] = _normalize_alnum(value) == _normalize_alnum(expected.get("license_number"))
    elif field == "license.expiry_date":
        matches["license_expiry_date_match"] = value == expected.get("license_expiry_date")
    elif field == "license":
        try:
            supplied = json.loads(value)
        except (TypeError, ValueError):
            return False, ["INVALID_LICENSE_CORRECTION"], matches
        if supplied.get("license_number"):
            matches["license_number_match"] = (
                _normalize_alnum(supplied["license_number"]) == _normalize_alnum(expected.get("license_number"))
            )
        if supplied.get("expiry_date"):
            matches["license_expiry_date_match"] = supplied["expiry_date"] == expected.get("license_expiry_date")
        if supplied.get("issue_date"):
            matches["license_issue_date_match"] = supplied["issue_date"] == expected.get("license_issue_date")
        if proof:
            extracted = await _gemini_json(
                "Read this Bahrain driving license image. Return JSON only with license_number, "
                "license_expiry_date (YYYY-MM-DD), confidence (high/medium/low). Copy only visible values; "
                "use null if unclear.", [proof],
            )
            matches["license_photo_number_match"] = (
                _normalize_alnum(extracted.get("license_number")) == _normalize_alnum(expected.get("license_number"))
            )
            matches["license_photo_expiry_match"] = (
                extracted.get("license_expiry_date") == expected.get("license_expiry_date")
            )
            matches["license_photo_confidence_high"] = extracted.get("confidence") == "high"
        if not matches:
            return False, ["NO_LICENSE_DATA_TO_COMPARE"], matches
    elif field in {"cpr_photo", "license.photo_front", "license.photo_back"}:
        if not proof:
            return False, ["CORRECTION_DOCUMENT_MISSING"], matches
        if field == "cpr_photo":
            extracted = await _gemini_json(
                "Read this Bahrain CPR card. Return JSON only with cpr_name, cpr_number, cpr_issue_date, "
                "cpr_expiry_date (dates YYYY-MM-DD), confidence (high/medium/low). Copy only visible values; "
                "use null if unclear.", [proof],
            )
            matches = {
                "confidence_high": extracted.get("confidence") == "high",
                "cpr_name_match": _name_matches(extracted.get("cpr_name"), expected.get("name")),
                "cpr_number_match": _normalize_alnum(extracted.get("cpr_number")) == _normalize_alnum(expected.get("cpr")),
                "cpr_issue_date_match": extracted.get("cpr_issue_date") == expected.get("cpr_issue_date"),
                "cpr_expiry_date_match": extracted.get("cpr_expiry_date") == expected.get("cpr_expiry_date"),
            }
        else:
            extracted = await _gemini_json(
                "Read this Bahrain driving license. Return JSON only with license_number, "
                "license_expiry_date (YYYY-MM-DD), confidence (high/medium/low). Copy only visible values; "
                "use null if unclear.", [proof],
            )
            matches = {
                "confidence_high": extracted.get("confidence") == "high",
                "license_number_match": _normalize_alnum(extracted.get("license_number")) == _normalize_alnum(expected.get("license_number")),
                "license_expiry_date_match": extracted.get("license_expiry_date") == expected.get("license_expiry_date"),
            }
    reasons = [code for code, matched in matches.items() if not matched]
    return not reasons, reasons, matches


async def review_correction(change_id: int, reviewer_id: int) -> str:
    async with SessionLocal() as db:
        change = await db.get(ProfileChange, change_id)
        if not change or change.kind != correction_service.CORRECTION or change.status != correction_service.SUBMITTED:
            return "skipped"
        user = await db.get(User, change.user_id)
        field, value, user_id = change.field, change.new_value or "", change.user_id
        sensitive = field in _SENSITIVE_CORRECTION_FIELDS
        expected: dict = {}
        proof: tuple[str, bytes] | None = None
        if sensitive and user:
            if user.cpr_encrypted:
                expected.update({
                    "cpr": decrypt(user.cpr_encrypted), "name": user.full_name,
                    "cpr_issue_date": user.cpr_issue_date.isoformat() if user.cpr_issue_date else None,
                    "cpr_expiry_date": user.cpr_expiry_date.isoformat() if user.cpr_expiry_date else None,
                })
            driver = (await db.execute(select(DriverProfile).where(DriverProfile.user_id == user.id))).scalar_one_or_none()
            if driver:
                expected.update({
                    "license_number": decrypt(driver.license_number_encrypted) if driver.license_number_encrypted else None,
                    "license_issue_date": driver.license_issue_date.isoformat() if driver.license_issue_date else None,
                    "license_expiry_date": driver.license_expiry_date.isoformat() if driver.license_expiry_date else None,
                })
        if sensitive:
            proof_key = value if correction_service.is_file(field) else None
            if field == "license":
                try:
                    proof_key = json.loads(value).get("photo")
                except (TypeError, ValueError):
                    proof_key = None
            if proof_key:
                file_row = await db.get(UploadedFile, proof_key)
                if file_row:
                    proof = (file_row.content_type, await get_bytes(proof_key))

    if not user:
        return "skipped"
    if sensitive:
        matched, reasons, field_matches = await _sensitive_correction_match(field, value, expected, proof)
    else:
        matched, reasons, field_matches = True, [], {"sensitive_match_required": False}

    async with SessionLocal() as db:
        change = await db.get(ProfileChange, change_id)
        user = await db.get(User, user_id)
        if not change or not user or change.status != correction_service.SUBMITTED:
            return "skipped"
        if not matched:
            reason_labels = {
                "confidence_high": "وضوح صورة المستند غير كافٍ",
                "cpr_name_match": "الاسم في البطاقة لا يطابق الاسم المسجل",
                "cpr_number_match": "رقم البطاقة لا يطابق الرقم المسجل",
                "cpr_issue_date_match": "تاريخ إصدار البطاقة لا يطابق البيانات المسجلة",
                "cpr_expiry_date_match": "تاريخ انتهاء البطاقة لا يطابق البيانات المسجلة",
                "license_number_match": "رقم الرخصة لا يطابق الرقم المسجل",
                "license_expiry_date_match": "تاريخ انتهاء الرخصة لا يطابق البيانات المسجلة",
                "license_issue_date_match": "تاريخ إصدار الرخصة لا يطابق البيانات المسجلة",
                "license_photo_number_match": "رقم الرخصة في الصورة لا يطابق الرقم المسجل",
                "license_photo_expiry_match": "تاريخ الرخصة في الصورة لا يطابق البيانات المسجلة",
                "license_photo_confidence_high": "صورة الرخصة غير واضحة بما يكفي للتحقق",
                "CORRECTION_DOCUMENT_MISSING": "ملف المستند المطلوب غير موجود",
                "NO_CPR_DATES_TO_COMPARE": "لم تُرسل تواريخ البطاقة للمقارنة",
                "NO_LICENSE_DATA_TO_COMPARE": "لم تُرسل بيانات الرخصة للمقارنة",
                "INVALID_LICENSE_CORRECTION": "بيانات تصحيح الرخصة غير صالحة",
            }
            reason = "لم يتم قبول التصحيح: " + "؛ ".join(reason_labels.get(code, code) for code in reasons)
            change.status, change.new_value, change.submitted_at, change.reason = (
                correction_service.OPEN, None, None, reason[:500]
            )
            correction_service.touch(change, reviewer_id)
            audit(db, reviewer_id, "AI_CORRECTION_REJECTED", "user", user.id,
                  reason, details={"correction_id": change.id, "field": field, "reason_codes": reasons})
            await notify(db, user.id, "CORRECTION_REJECTED", "تعذر قبول التصحيح", reason)
            await db.commit()
            await _write_worker_audit("AI_CORRECTION_REVIEW", {
                "correction_id": change_id, "user_id": user_id, "field": field,
                "result": "rejected_for_resubmission", "reason_codes": reasons,
                "field_matches": field_matches, "reason": reason,
            }, entity_type="correction", entity_id=str(change_id))
            return "rejected"

        await correction_service.apply(db, user, change)
        change.status = correction_service.ACCEPTED
        correction_service.touch(change, reviewer_id)
        await correction_service.refresh_flag(db, user.id)
        audit(db, reviewer_id, "AI_CORRECTION_ACCEPTED", "user", user.id,
              details={"correction_id": change.id, "field": field, "matched_sensitive_data": sensitive})
        await notify(db, user.id, "CORRECTION_ACCEPTED", "تم قبول التصحيح", "تم قبول البيانات المرسلة.")
        await db.commit()
        await _write_worker_audit("AI_CORRECTION_REVIEW", {
            "correction_id": change_id, "user_id": user_id, "field": field,
            "result": "accepted", "matched_sensitive_data": sensitive,
            "field_matches": field_matches,
        }, entity_type="correction", entity_id=str(change_id))
        return "accepted"


STATE: dict = {"running": False, "last_heartbeat_at": None, "next_review_at": None, "last_cycle_status": None, "last_error": None,
         "sleeping": False, "sleeping_until": None}


async def run_review_cycle() -> dict:
    STATE["running"] = True
    try:
        result = await _run_review_cycle()
    finally:
        STATE["running"] = False
    STATE["last_cycle_status"] = "error" if str(result.get("status", "")).endswith("error") or result.get("status", "").startswith("disabled") else "completed"
    STATE["last_error"] = result.get("error_message") or result.get("error") or (result.get("status") if result.get("status", "").startswith("disabled") else None)
    return result


async def _run_review_cycle() -> dict:
    summary = {
        "cycle_id": uuid.uuid4().hex,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "completed",
        "users_checked": 0,
        "users_approved": [],
        "users_not_approved": [],
        "users_errors": [],
        "payments_checked": 0,
        "payments_confirmed": [],
        "payments_not_confirmed": [],
        "payments_errors": [],
        "corrections_checked": 0,
        "corrections_accepted": [],
        "corrections_rejected": [],
        "corrections_errors": [],
    }
    if not settings.ai_service_phone or not settings.ai_service_password or not (await __import__("app.services.command", fromlist=["gemini"]).gemini())["key"]:
        log.warning("AI review disabled: configure AI_SERVICE_PHONE, AI_SERVICE_PASSWORD, GEMINI_API_KEY")
        summary["status"] = "disabled_missing_configuration"
        await _write_worker_audit("AI_REVIEW_CYCLE", summary)
        return summary
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            token = await _ai_token(client)
        async with SessionLocal() as db:
            reviewer_id = (await db.execute(select(User.id).where(
                User.phone == normalize_phone(settings.ai_service_phone)
            ))).scalar_one_or_none()
            if reviewer_id is None:
                raise RuntimeError("AI review account not found in the database")
            user_ids = list((await db.execute(select(User.id).where(
                User.role.in_([Role.CUSTOMER, Role.DRIVER]), User.account_status == AccountStatus.PENDING_APPROVAL,
                User.phone_verified.is_(True),
            ).order_by(User.created_at).limit(settings.ai_review_batch_size))).scalars())
            payment_ids = list((await db.execute(select(Payment.id).where(
                Payment.payment_method == "BENEFITPAY", Payment.payment_status == "PENDING_VERIFICATION"
            ).order_by(Payment.created_at).limit(settings.ai_review_batch_size))).scalars())
            correction_ids = list((await db.execute(select(ProfileChange.id).where(
                ProfileChange.kind == correction_service.CORRECTION,
                ProfileChange.status == correction_service.SUBMITTED,
            ).order_by(ProfileChange.submitted_at).limit(settings.ai_review_batch_size))).scalars())
    except Exception as exc:
        log.exception("AI review cycle could not start")
        summary.update(status="start_error", error=type(exc).__name__, error_message=str(exc)[:300],
                       ai_service_base_url=settings.ai_service_base_url)
        await _write_worker_audit("AI_REVIEW_CYCLE", summary)
        return summary

    summary["users_checked"] = len(user_ids)
    summary["payments_checked"] = len(payment_ids)
    summary["corrections_checked"] = len(correction_ids)
    for user_id in user_ids:
        try:
            (summary["users_approved"] if await review_user(user_id, token)
             else summary["users_not_approved"]).append(user_id)
        except Exception as exc:
            log.exception("AI identity review failed user_id=%s", user_id)
            summary["users_errors"].append({"id": user_id, "error": type(exc).__name__})
            await _write_worker_audit("AI_IDENTITY_REVIEW", {
                "user_id": user_id, "result": "error", "error": type(exc).__name__,
            }, entity_type="user", entity_id=str(user_id))
    for payment_id in payment_ids:
        try:
            (summary["payments_confirmed"] if await review_payment(payment_id, token)
             else summary["payments_not_confirmed"]).append(payment_id)
        except Exception as exc:
            log.exception("AI receipt review failed payment_id=%s", payment_id)
            summary["payments_errors"].append({"id": payment_id, "error": type(exc).__name__})
            await _write_worker_audit("AI_RECEIPT_REVIEW", {
                "payment_id": payment_id, "result": "error", "error": type(exc).__name__,
            }, entity_type="payment", entity_id=str(payment_id))
    for change_id in correction_ids:
        try:
            outcome = await review_correction(change_id, reviewer_id)
            if outcome == "accepted":
                summary["corrections_accepted"].append(change_id)
            elif outcome == "rejected":
                summary["corrections_rejected"].append(change_id)
        except Exception as exc:
            log.exception("AI correction review failed correction_id=%s", change_id)
            summary["corrections_errors"].append({"id": change_id, "error": type(exc).__name__})
            await _write_worker_audit("AI_CORRECTION_REVIEW", {
                "correction_id": change_id, "result": "error", "error": type(exc).__name__,
            }, entity_type="correction", entity_id=str(change_id))
    if summary["users_errors"] or summary["payments_errors"] or summary["corrections_errors"]:
        summary["status"] = "completed_with_errors"
    await _write_worker_audit("AI_REVIEW_CYCLE", summary)
    return summary


WAKE = asyncio.Event()


def wake() -> None:
    """A new member / BenefitPay receipt arrived: end the sleep (or the wait) and review now."""
    WAKE.set()


def _had_work(result: dict) -> bool:
    return bool(result.get("users_checked") or result.get("payments_checked") or result.get("corrections_checked"))


async def review_loop() -> None:
    """Checks every `interval`; after `idle` seconds with nothing new it sleeps `sleep` seconds (default 1 h idle -> 5 h sleep), then repeats."""
    loop = asyncio.get_running_loop()
    interval = max(60, settings.ai_review_interval_seconds)
    idle_limit, sleep_for = max(60, settings.ai_review_idle_seconds), max(60, settings.ai_review_sleep_seconds)
    log.info("AI review worker started interval_seconds=%s idle_seconds=%s sleep_seconds=%s", interval, idle_limit, sleep_for)
    await asyncio.sleep(5)  # let Uvicorn start accepting the AI account's internal login request
    last_work = loop.time()
    next_review = loop.time()
    while True:
        from . import modes as _modes
        if await _modes.emergency():  # emergency: background jobs are paused
            await asyncio.sleep(10)
            continue
        remaining = max(0, int(next_review - loop.time()))
        if remaining == 0:
            result = await run_review_cycle()
            log.info("AI review attempt completed details=%s", json.dumps(result, ensure_ascii=False, sort_keys=True))
            now = loop.time()
            if _had_work(result):
                last_work = now
            next_review = now + interval
            if now - last_work >= idle_limit:
                wake = datetime.now(UTC) + timedelta(seconds=sleep_for)
                log.info("AI review: nothing new for %s s; sleeping %s s until %s", int(now - last_work), sleep_for, wake.isoformat())
                STATE.update(sleeping=True, sleeping_until=wake.isoformat())
                await _write_worker_audit("AI_REVIEW_SLEEP", {"idle_seconds": int(now - last_work), "sleep_seconds": sleep_for,
                                                              "wake_at": wake.isoformat()})
                end = loop.time() + sleep_for
                while loop.time() < end and not WAKE.is_set():
                    STATE.update(last_heartbeat_at=datetime.now(UTC).isoformat(), next_review_at=wake.isoformat())
                    try:
                        await asyncio.wait_for(WAKE.wait(), timeout=min(60, max(1, end - loop.time())))
                    except TimeoutError:
                        pass
                WAKE.clear()
                STATE.update(sleeping=False, sleeping_until=None)
                log.info("AI review: waking up")
                last_work = loop.time()  # a fresh idle window of one hour before the next sleep
                next_review = loop.time()
            continue

        next_at = (datetime.now(UTC) + timedelta(seconds=remaining)).isoformat()
        log.info("AI review worker alive; next attempt in %s seconds at %s", remaining, next_at)
        STATE.update(last_heartbeat_at=datetime.now(UTC).isoformat(), next_review_at=next_at)  # not written to the audit log (one row a minute flooded it)
        try:
            await asyncio.wait_for(WAKE.wait(), timeout=min(60, remaining))
            WAKE.clear()
            next_review, last_work = loop.time(), loop.time()
        except TimeoutError:
            pass
