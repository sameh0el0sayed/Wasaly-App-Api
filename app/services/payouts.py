"""Driver payouts: moving the driver's share from the COMPANY account to the driver after a payment is confirmed.

Providers (settings.payout_provider):
  simulated - no money moves; the payout is marked TRANSFERRED with simulated=True (development / demos only).
  none      - the payout stays PENDING; an admin transfers manually and calls .../mark-transferred.
  http      - POSTs the transfer instruction to payout_http_url (adapter for your bank / payout API; adapt the body to it).
"""
import logging
from decimal import Decimal
from typing import NamedTuple

import httpx

from ..config import settings

log = logging.getLogger("payouts")


class PayoutResult(NamedTuple):
    status: str  # TRANSFERRED | PENDING | FAILED
    reference: str | None = None
    error: str | None = None
    simulated: bool = False


async def transfer(method: str | None, account: str | None, holder: str | None, amount: Decimal, currency: str,
                   reference: str) -> PayoutResult:
    if not account or not method:
        return PayoutResult("FAILED", error="NO_PAYOUT_ACCOUNT")
    provider = settings.payout_provider
    if provider == "simulated":
        log.warning("SIMULATED payout %s %s to %s (%s) ref=%s - no money moved", amount, currency, holder, method, reference)
        return PayoutResult("TRANSFERRED", reference=f"SIM-{reference}", simulated=True)
    if provider == "none":
        return PayoutResult("PENDING", error="MANUAL_TRANSFER_REQUIRED")
    if provider == "http":
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(settings.payout_http_url,
                                 headers={"Authorization": f"Bearer {settings.payout_http_token}"},
                                 json={"reference": reference, "method": method, "account": account, "holder": holder,
                                       "amount": str(amount), "currency": currency})
            if r.status_code // 100 == 2:
                return PayoutResult("TRANSFERRED", reference=str(r.json().get("reference") or reference))
            return PayoutResult("FAILED", error=f"PROVIDER_HTTP_{r.status_code}")
        except Exception as e:  # noqa
            return PayoutResult("FAILED", error=f"PROVIDER_ERROR:{type(e).__name__}")
    return PayoutResult("FAILED", error="UNKNOWN_PAYOUT_PROVIDER")
