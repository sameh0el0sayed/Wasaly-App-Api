from fastapi import HTTPException


def is_live_test(s: dict) -> bool:
    return s.get("live_test") == "true"


def block_tap(s: dict) -> None:
    if is_live_test(s):
        raise HTTPException(403, {"code": "LIVE_TEST_TAP_DISABLED", "message": "Online payment is off during live test"})


def benefit_on(s: dict) -> bool:
    return s.get("benefit_enabled") == "true" and not is_live_test(s)  # Benefit through Tap is an online payment too


def tap_on(s: dict) -> bool:
    return s.get("tap_enabled") == "true" and not is_live_test(s)
