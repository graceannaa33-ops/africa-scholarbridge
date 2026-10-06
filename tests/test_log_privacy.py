"""No full student phone number (or e-mail address) is ever written to the
application logs on the payment and e-mail paths. Payment behaviour itself
is unchanged - these tests only look at what is logged.

All test data is fictional.
"""
import logging
import smtplib

import pytest

import email_lib
import paystack_lib
from tests.test_paystack_payment import (SECRET, _charge_attempted_400, attempts, paystack,  # noqa: F401
                                         ready_to_pay, start, unlocked)

PHONE_FORMS = ("0712345678", "0712 345 678", "0712-345-678", "+254712345678", "254712345678",
               "+254 712 345 678", "712345678")
FULL_DIGITS = ("0712345678", "712345678", "254712345678")


def _no_full_phone(text):
    flat = text.replace(" ", "").replace("-", "")
    for form in FULL_DIGITS:
        assert form not in flat, f"full phone number {form!r} found in log: {text!r}"


# --- masking helpers -------------------------------------------------------
@pytest.mark.parametrize("raw,masked", [("0712345678", "07******78"), ("0712 345 678", "07******78"),
                                        ("+254712345678", "+254*******78"), ("254712345678", "25********78"),
                                        ("", "***"), (None, "***"), ("1234", "***")])
def test_mask_phone_keeps_only_minimum(raw, masked):
    assert paystack_lib.mask_phone(raw) == masked


@pytest.mark.parametrize("phone", PHONE_FORMS)
def test_mask_in_text_hides_every_phone_format(phone):
    out = paystack_lib.mask_in_text(f"Invalid number {phone} supplied")
    _no_full_phone(out)
    assert "Invalid number" in out and "supplied" in out and "78" in out


def test_mask_in_text_keeps_useful_diagnostics():
    text = "ref=ASB-VISA-12-ABCDEF123456 paid_at=2026-10-03 amount=150000 http=400 code=12345678"
    assert paystack_lib.mask_in_text(text) == text


def test_mask_emails_in_text():
    out = email_lib.mask_emails_in_text("{'amina.yusuf@example.com': (550, b'<amina.yusuf@example.com>')}")
    assert "amina.yusuf@example.com" not in out and out.count("a***@example.com") == 2


# --- payment log lines -----------------------------------------------------
@pytest.mark.parametrize("phone", ["0712345678", "0712 345 678", "+254712345678", "254712345678"])
def test_mpesa_charge_start_log_masks_phone(client, student, paystack, caplog, phone):
    rid = ready_to_pay(client, student)
    with caplog.at_level(logging.INFO):
        start(client, rid, phone=phone)
    [a] = attempts(rid)
    started = [r.getMessage() for r in caplog.records if "M-PESA charge started" in r.getMessage()]
    assert len(started) == 1 and a["paystack_reference"] in started[0]
    assert "+254*******78" in started[0]                              # still useful for debugging
    _no_full_phone(caplog.text)


def test_start_failure_log_masks_phone_even_inside_paystack_message(client, student, paystack, caplog):
    rid = ready_to_pay(client, student)
    paystack.charge_error = paystack_lib._error_from_payload("Paystack HTTP 400", 400, {
        "status": False, "message": "Invalid phone 0712345678",
        "data": {"status": "failed", "message": "Number +254712345678 is not registered"}})
    with caplog.at_level(logging.INFO):
        start(client, rid, phone="0712345678")
    assert "Paystack start failed" in caplog.text and "http=400" in caplog.text
    assert "07******78" in caplog.text and "+254*******78" in caplog.text
    _no_full_phone(caplog.text)
    assert SECRET not in caplog.text
    assert not unlocked(rid)                                           # payment outcome logic unchanged


def test_charge_attempted_failure_log_masks_phone(client, student, paystack, caplog):
    rid = ready_to_pay(client, student)
    paystack.charge_error = _charge_attempted_400(message="M-PESA 0712 345 678 cancelled the request")
    paystack.default_outcome = {"status": "failed", "gateway_response": ""}
    with caplog.at_level(logging.INFO):
        start(client, rid, phone="0712 345 678")
    assert "Charge attempted" in caplog.text
    _no_full_phone(caplog.text)


def test_verify_failure_log_masks_phone(client, student, paystack, caplog, monkeypatch):
    rid = ready_to_pay(client, student)
    start(client, rid, phone="0712345678")
    [a] = attempts(rid)
    real_call = paystack_lib._call

    def failing_verify(method, path, body=None):
        if path.startswith("/transaction/verify/"):
            raise paystack_lib._error_from_payload("Paystack HTTP 400", 400, {
                "status": False, "message": "Customer 0712345678 / +254712345678 not found"})
        return real_call(method, path, body)
    monkeypatch.setattr(paystack_lib, "_call", failing_verify)
    with caplog.at_level(logging.INFO):
        client.post(f"/student-visa/payment/{rid}/paystack/check")
    lines = [r.getMessage() for r in caplog.records if "Paystack verify failed" in r.getMessage()]
    assert lines and a["paystack_reference"] in lines[0] and "http=400" in lines[0]
    assert "07******78" in lines[0]
    _no_full_phone(caplog.text)
    assert attempts(rid)[0]["gateway_status"] == "pending" and not unlocked(rid)


def test_payment_records_still_store_what_they_stored_before(client, student, paystack):
    """Masking is for logs only - the database payment record is unchanged."""
    rid = ready_to_pay(client, student)
    start(client, rid, phone="0712 345 678")
    [a] = attempts(rid)
    [body] = paystack.bodies("/charge")
    assert body["mobile_money"]["phone"] == "+254712345678"           # Paystack still gets the real number
    assert a["phone_number"] == "+254712345678" and a["gateway_status"] == "pending"


# --- e-mail failure log ----------------------------------------------------
def test_smtp_failure_log_masks_recipient_address(monkeypatch, caplog):
    for k, v in {"MAIL_SERVER": "smtp.example.test", "MAIL_PORT": "587", "MAIL_USERNAME": "u",
                 "MAIL_PASSWORD": "p", "MAIL_DEFAULT_SENDER": "noreply@example.test"}.items():
        monkeypatch.setenv(k, v)

    class RefusingSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): pass
        def login(self, *a): pass
        def send_message(self, msg):
            raise smtplib.SMTPRecipientsRefused({"amina.yusuf@example.com": (550, b"<amina.yusuf@example.com> unknown")})
    monkeypatch.setattr(email_lib.smtplib, "SMTP", RefusingSMTP)
    with caplog.at_level(logging.INFO):
        ok, err = email_lib.send_email("amina.yusuf@example.com", "Subject", "Body")
    assert (ok, err) == (False, "send_failed")
    assert "SMTPRecipientsRefused" in caplog.text and "a***@example.com" in caplog.text
    assert "amina.yusuf@example.com" not in caplog.text
