"""Phoenix ID identity verification (KYC) levels. Rules are owner switches, all OFF by default; a missing level counts as 'none'."""
from datetime import UTC, datetime

from fastapi import HTTPException

from . import command

RANK = {"none": 0, "basic": 1, "verified": 2, "verified_plus": 3}
# rule key -> (required level, description)
RULES = {
    "kyc_rule_driver_online": ("verified_plus", "Drivers must be verified_plus to go online"),
    "kyc_rule_customer_order": ("basic", "Customers must be at least basic to create an order"),
}


def norm(v) -> str:
    v = str(v or "none").lower()
    return v if v in RANK else "none"


def apply_claims(user, claims: dict, info: dict) -> None:
    """Store the KYC claims of the current SSO login on the local user. Missing claims = 'none', never blocks anything by itself."""
    src = {**info, **claims}
    user.kyc_level = norm(src.get("kyc_level"))
    org = src.get("org_kyc_level")
    user.org_kyc_level = norm(org) if org else None
    ts = src.get("kyc_verified_at")
    user.kyc_verified_at = datetime.fromtimestamp(int(ts), UTC) if str(ts or "").isdigit() else None


async def rules() -> dict[str, bool]:
    o = await command.owner_all()
    return {k: o.get(k, "false") == "true" for k in RULES}


async def require(user, rule: str) -> None:
    """Enforce one owner-switchable rule in the API (not only the UI)."""
    if not (await rules()).get(rule):
        return
    need = RULES[rule][0]
    if RANK[norm(user.kyc_level)] < RANK[need]:
        raise HTTPException(403, {"code": "KYC_REQUIRED", "required": need, "current": norm(user.kyc_level), "rule": rule})
