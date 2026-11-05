from app.services.ai_review import compare_receipt_fields


def test_receipt_requires_trip_date_account_amount_and_currency_match():
    extracted = {
        "transfer_date": "2026-11-05",
        "beneficiary_account": "****1234",
        "amount": "2.500",
        "currency": "BHD",
    }
    accepted, reasons = compare_receipt_fields(
        extracted, "2026-11-05", "+973 3333 1234", "2.500", "BHD", 3
    )
    assert accepted and reasons == []

    extracted["amount"] = "2.000"
    accepted, reasons = compare_receipt_fields(
        extracted, "2026-11-05", "+973 3333 1234", "2.500", "BHD", 3
    )
    assert not accepted and reasons == ["AMOUNT_MISMATCH"]


def test_unclear_or_mismatched_receipt_fields_fail_closed():
    accepted, reasons = compare_receipt_fields(
        {"transfer_date": None, "beneficiary_account": "****9999", "amount": None, "currency": None},
        "2026-11-05", "+973 3333 1234", "2.500", "BHD", 3
    )
    assert not accepted
    assert set(reasons) == {"DATE_MISMATCH", "ACCOUNT_MISMATCH", "AMOUNT_MISMATCH", "CURRENCY_MISMATCH"}
