from app.services.ai_review import compare_receipt_fields


def test_receipt_accepts_trip_date_or_any_later_non_future_date():
    extracted = {
        "transfer_date": "2026-11-05",
        "beneficiary_account": "****1234",
        "amount": "2.500",
        "currency": "BHD",
    }
    accepted, reasons = compare_receipt_fields(
        extracted, "2026-11-05", "+973 3333 1234", "2.500", "BHD", 3, "2026-11-07"
    )
    assert accepted and reasons == []

    extracted["transfer_date"] = "07/11/2026"  # Payment may arrive two days after the trip.
    accepted, reasons = compare_receipt_fields(
        extracted, "2026-11-05", "+973 3333 1234", "2.500", "BHD", 3, "2026-11-07"
    )
    assert accepted and reasons == []

    for bad_date in ("2026-11-04", "2026-11-08"):
        extracted["transfer_date"] = bad_date
        accepted, reasons = compare_receipt_fields(
            extracted, "2026-11-05", "+973 3333 1234", "2.500", "BHD", 3, "2026-11-07"
        )
        assert not accepted and reasons == ["DATE_MISMATCH"]


def test_wrong_amount_and_unclear_fields_fail_closed():
    extracted = {
        "transfer_date": "2026-11-05",
        "beneficiary_account": "****1234",
        "amount": "2.000",
        "currency": "BHD",
    }
    accepted, reasons = compare_receipt_fields(
        extracted, "2026-11-05", "+973 3333 1234", "2.500", "BHD", 3, "2026-11-05"
    )
    assert not accepted and reasons == ["AMOUNT_MISMATCH"]

    accepted, reasons = compare_receipt_fields(
        {"transfer_date": None, "beneficiary_account": "****9999", "amount": None, "currency": None},
        "2026-11-05", "+973 3333 1234", "2.500", "BHD", 3, "2026-11-05"
    )
    assert not accepted
    assert set(reasons) == {"DATE_MISMATCH", "ACCOUNT_MISMATCH", "AMOUNT_MISMATCH", "CURRENCY_MISMATCH"}
