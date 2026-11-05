from app.services.ai_review import compare_receipt_fields


def test_receipt_does_not_require_date_and_accepts_reference_account_amount_currency():
    extracted = {
        "beneficiary_account": "****1234",
        "amount": "2.500",
        "currency": "BHD",
        "reference": "6244091817-BP1793866797PRY8",
    }
    accepted, reasons = compare_receipt_fields(
        extracted, "+973 3333 1234", "2.500", "BHD", 3
    )
    assert accepted and reasons == []


def test_wrong_amount_and_unclear_fields_fail_closed():
    extracted = {
        "transfer_date": "not a date; ignored by payment review",
        "beneficiary_account": "****1234",
        "amount": "2.000",
        "currency": "BHD",
        "reference": "6244091817-BP1793866797PRY8",
    }
    accepted, reasons = compare_receipt_fields(
        extracted, "+973 3333 1234", "2.500", "BHD", 3
    )
    assert not accepted and reasons == ["AMOUNT_MISMATCH"]

    accepted, reasons = compare_receipt_fields(
        {"beneficiary_account": "****9999", "amount": None, "currency": None, "reference": None},
        "+973 3333 1234", "2.500", "BHD", 3
    )
    assert not accepted
    assert set(reasons) == {"ACCOUNT_MISMATCH", "AMOUNT_MISMATCH", "CURRENCY_MISMATCH", "REFERENCE_MISSING"}
