import base64
import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet

from .config import settings

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


@lru_cache
def _fernet() -> Fernet:
    key = settings.fernet_key.encode() if settings.fernet_key else \
        base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def cpr_hash(cpr: str) -> str:
    """Deterministic keyed hash: lets us enforce uniqueness / search without decrypting."""
    return hmac.new(settings.secret_key.encode(), cpr.encode(), hashlib.sha256).hexdigest()


def normalize_phone(raw: str) -> str:
    s = re.sub(r"[\s\-()]", "", raw or "")
    for prefix in ("+973", "00973", "973"):
        if s.startswith(prefix) and len(s) == len(prefix) + 8:
            s = s[len(prefix):]
            break
    if not re.fullmatch(r"\d{8}", s):
        raise ValueError("Phone must be a valid Bahraini number (8 digits)")
    return "+973" + s


def validate_cpr(cpr: str) -> str:
    cpr = (cpr or "").strip()
    if not re.fullmatch(r"\d{9}", cpr):
        raise ValueError("CPR must be exactly 9 digits")
    return cpr


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(user_id: int, token_version: int) -> str:
    payload = {"sub": str(user_id), "tv": token_version, "typ": "access",
               "iat": _now(), "exp": _now() + timedelta(minutes=settings.access_token_minutes)}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    data = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    if data.get("typ") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return data


def create_file_token(file_key: str, minutes: int = 5) -> str:
    return jwt.encode({"key": file_key, "typ": "file", "exp": _now() + timedelta(minutes=minutes)},
                      settings.secret_key, algorithm="HS256")


def decode_file_token(token: str) -> str:
    data = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    if data.get("typ") != "file":
        raise jwt.InvalidTokenError("wrong token type")
    return data["key"]
