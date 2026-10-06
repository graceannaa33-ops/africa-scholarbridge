"""Paystack payment of the visa assistance fee.

Only the HTTP call to Paystack is replaced (FakePaystack); routes, the
server-side verification, the database, webhook signature checks and the
templates all run for real.
"""
import hashlib
import hmac
import json
import os
import re
import secrets

import pytest

import app as app_module
import paystack_lib
from conftest import APPLICANT, VISA_FORM_ANSWERS, complete_visa_form, finish_visa_form, get_application
from database import get_db

SECRET = "sk_test_" + "x" * 40          # fake test key, never a real one
PUBLIC = "pk_test_" + "y" * 40


def q(sql, args=()):
    db = get_db()
    rows = db.execute(sql, args).fetchall()
    db.close()
    return rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    rid = cur.lastrowid
    db.close()
    return rid


class FakePaystack:
    """Stands in for api.paystack.co. `outcome[reference]` = what verify returns."""

    def __init__(self):
        self.calls, self.outcome, self.fail_next = [], {}, False
        self.charge_reply = None          # dict -> returned as /charge data
        self.charge_error = None          # PaystackError -> raised by /charge
        self.verify_unavailable = False
        self.default_outcome = {"status": "ongoing"}     # verify result for references without an outcome

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.fail_next:
            self.fail_next = False
            raise paystack_lib.PaystackError("Could not reach Paystack (URLError).")
        if path == "/charge":
            if self.charge_error is not None:
                raise self.charge_error
            if self.charge_reply is not None:
                return self.charge_reply
            return {"status": "pay_offline", "display_text": "Please complete authorization on your phone",
                    "reference": body["reference"]}
        if path.startswith("/transaction/verify/") and self.verify_unavailable:
            raise paystack_lib.PaystackError("Could not reach Paystack (URLError).")
        if path == "/transaction/initialize":
            return {"authorization_url": f"https://checkout.paystack.com/{body['reference'][-8:].lower()}",
                    "access_code": "ac", "reference": body["reference"]}
        if path.startswith("/transaction/verify/"):
            ref = path.rsplit("/", 1)[1]
            o = self.outcome.get(ref, self.default_outcome)
            return {"reference": o.get("reference", ref), "amount": o.get("amount", 150000),
                    "currency": o.get("currency", "KES"),
                    "status": o["status"], "channel": "mobile_money", "paid_at": "2026-10-03T08:00:00.000Z",
                    "gateway_response": o.get("gateway_response", "Approved")}
        raise AssertionError(path)

    def bodies(self, path):
        return [b for _, p, b in self.calls if p == path]


@pytest.fixture()
def paystack(monkeypatch):
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", SECRET)
    monkeypatch.setenv("PAYSTACK_PUBLIC_KEY", PUBLIC)
    fake = FakePaystack()
    monkeypatch.setattr(paystack_lib, "_call", fake)
    return fake


def request_of(student):
    return q("SELECT * FROM visa_requests WHERE student_id = ? ORDER BY id DESC LIMIT 1", (student["student_id"],))[0]


def flashes(client):
    with client.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


def ready_to_pay(client, student):
    """Final step 'No visa' -> sections 1-8 + required documents uploaded."""
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    complete_visa_form(client, vr["id"], sign=False)
    return vr["id"]


def start(client, rid, method="mpesa", phone="0712 345 678", **extra):
    return client.post(f"/student-visa/payment/{rid}/paystack/start",
                       data={"method": method, "email": "amina@example.com", "phone": phone, **extra})


def attempts(rid):
    return [dict(r) for r in q("SELECT * FROM visa_payments WHERE request_id = ? AND gateway = 'paystack' ORDER BY id",
                               (rid,))]


def unlocked(rid):
    vr = q("SELECT payment_status, payment_verified FROM visa_requests WHERE id = ?", (rid,))[0]
    return vr["payment_status"] == "paid" and vr["payment_verified"] == 1


def signed_webhook(client, payload, secret=SECRET, signature=None):
    raw = json.dumps(payload).encode()
    sig = signature if signature is not None else hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()
    return client.post("/paystack/webhook", data=raw, content_type="application/json",
                       headers={"x-paystack-signature": sig})


# ---------------------------------------------------------------------
# Position: after the documents, never before
# ---------------------------------------------------------------------
def test_payment_not_shown_or_accepted_before_documents(client, student, paystack):
    client.post("/application/step/visa", data={"visa_choice": "no"})
    rid = request_of(student)["id"]
    page = client.get(f"/student-visa/application/{rid}/step/documents").get_data(as_text=True)
    assert 'id="paystackForm"' not in page and 'id="paymentNotYet"' in page
    r = start(client, rid)
    assert "/student-visa/application/" in r.headers["Location"]
    assert attempts(rid) == [] and paystack.calls == []
    assert client.get(f"/student-visa/payment/{rid}").status_code == 302


def test_payment_appears_right_after_successful_upload(client, student, paystack):
    rid = ready_to_pay(client, student)
    page = " ".join(client.get(f"/student-visa/application/{rid}/step/documents").get_data(as_text=True).split())
    assert "All required documents uploaded successfully." in page and "✓ Documents uploaded successfully" in page
    assert "Next step: Visa Assistance Payment" in page and 'id="paystackForm"' in page
    assert "KSh 1,500" in page and "Pay KSh 1,500 with Paystack (M-PESA)" in page and "M-PESA / Paystack" in page
    assert page.index('id="documentChecklist"') < page.index('id="paystackForm"')     # below the uploads
    assert "Test mode" in page
    assert SECRET not in page and PUBLIC not in page                                  # keys never rendered


# ---------------------------------------------------------------------
# Initialising: server decides amount/currency, unique references
# ---------------------------------------------------------------------
def test_mpesa_prompt_initialised_with_server_amount_and_unique_reference(client, student, paystack):
    rid = ready_to_pay(client, student)
    r = start(client, rid, phone="+254 712-345-678", amount="1", currency="NGN")       # client amount ignored
    assert r.status_code == 302 and "#payment" in r.headers["Location"]
    [body] = paystack.bodies("/charge")
    assert body["amount"] == "150000" and body["currency"] == "KES"
    assert body["mobile_money"] == {"phone": "+254712345678", "provider": "mpesa"}
    assert body["email"] == "amina@example.com" and "Check your phone" in flashes(client)
    [a] = attempts(rid)
    assert a["paystack_reference"] == body["reference"] and re.fullmatch(r"ASB-VISA-\d+-[0-9A-F]{16}", body["reference"])
    assert a["gateway_status"] == "pending" and a["expected_amount_subunit"] == 150000 and not unlocked(rid)
    page = client.get(f"/student-visa/application/{rid}/step/documents").get_data(as_text=True)
    assert 'id="paymentPending"' in page and "Check payment status" in page
    start(client, rid)                                                            # second attempt
    refs = [x["paystack_reference"] for x in attempts(rid)]
    assert len(refs) == 2 and len(set(refs)) == 2


def test_checkout_redirects_to_paystack_with_callback(client, student, paystack):
    rid = ready_to_pay(client, student)
    r = start(client, rid, method="checkout", phone="")
    assert r.headers["Location"].startswith("https://checkout.paystack.com/")
    [body] = paystack.bodies("/transaction/initialize")
    assert body["amount"] == "150000" and body["currency"] == "KES" and "mobile_money" in body["channels"]
    assert body["callback_url"].endswith("/paystack/callback")


@pytest.mark.parametrize("phone", ["12345", "0812345678", "+255712345678", ""])
def test_invalid_mpesa_numbers_refused(client, student, paystack, phone):
    rid = ready_to_pay(client, student)
    start(client, rid, phone=phone)
    assert attempts(rid) == [] and paystack.bodies("/charge") == []


def test_phone_normalisation():
    n = paystack_lib.normalize_kenyan_phone
    assert n("0712 345 678") == n("254712345678") == n("+254 712-345-678") == n("712345678") == "+254712345678"
    assert n("0110 123 456") == "+254110123456"
    assert n("0812345678") is None and n("+255712345678") is None and n("abc") is None


def test_initialise_failure_is_friendly_and_retryable(client, student, paystack):
    rid = ready_to_pay(client, student)
    paystack.fail_next = True
    start(client, rid)
    assert "couldn't start the payment" in flashes(client)
    [a] = attempts(rid)
    assert a["gateway_status"] == "failed" and not unlocked(rid)
    start(client, rid)                                                            # retry works
    assert len(attempts(rid)) == 2 and attempts(rid)[1]["gateway_status"] == "pending"


# ---------------------------------------------------------------------
# Verification: only Paystack's verify result counts
# ---------------------------------------------------------------------
def test_successful_payment_verified_server_side_and_unlocks(client, student, paystack):
    rid = ready_to_pay(client, student)
    start(client, rid)
    ref = attempts(rid)[0]["paystack_reference"]
    paystack.outcome[ref] = {"status": "success"}
    r = client.post(f"/student-visa/payment/{rid}/paystack/check")
    assert ("GET", f"/transaction/verify/{ref}", None) in paystack.calls
    assert unlocked(rid) and r.headers["Location"].endswith(f"/student-visa/application/{rid}/step/additional")
    [a] = attempts(rid)
    assert a["gateway_status"] == "successful" and a["payment_status"] == "PAYMENT_VERIFIED"
    assert a["paid_amount_subunit"] == 150000 and a["paid_currency"] == "KES" and a["verification_method"] == "paystack_verify"
    # paid: Payment verified shown; no second charge possible
    page = " ".join(client.get(f"/student-visa/application/{rid}/step/documents").get_data(as_text=True).split())
    text = " ".join(re.sub(r"<[^>]+>", " ", page).split())                     # visible text
    assert "✓ Payment verified" in text and "Amount paid: KSh 1,500" in text and ref in text
    assert 'id="paystackForm"' not in page
    # then additional info + declaration -> funding application completed
    r = finish_visa_form(client, rid)
    row = get_application(student["student_id"])
    assert row["status"] != "Draft" and row["reference_number"] and row["visa_step_status"] == "COMPLETE"
    assert r.headers["Location"].endswith(f"/application/confirmation/{row['id']}")


@pytest.mark.parametrize("status", ["failed", "abandoned", "ongoing", "pending", "processing", "queued", "reversed"])
def test_unsuccessful_statuses_never_unlock(client, student, paystack, status):
    rid = ready_to_pay(client, student)
    start(client, rid)
    ref = attempts(rid)[0]["paystack_reference"]
    paystack.outcome[ref] = {"status": status, "gateway_response": "Declined"}
    client.post(f"/student-visa/payment/{rid}/paystack/check")
    client.get(f"/paystack/callback?reference={ref}&trxref={ref}&status=success")     # forged query string
    assert not unlocked(rid)
    a = attempts(rid)[0]
    expected = {"failed": "failed", "reversed": "failed", "abandoned": "abandoned"}.get(status, "pending")
    assert a["gateway_status"] == expected and a["payment_status"] != "PAYMENT_VERIFIED"
    # documents are kept and the student can try again
    stored = q("SELECT stored_file FROM visa_documents WHERE request_id = ? AND stored_file IS NOT NULL", (rid,))
    assert len(stored) == 2 and all(os.path.exists(os.path.join(app_module.VISA_APP_DOCS_DIR, s[0])) for s in stored)
    assert get_application(student["student_id"])["status"] == "Draft"
    for step in ("additional", "declaration"):
        assert client.get(f"/student-visa/application/{rid}/step/{step}").headers["Location"].endswith("/step/documents")
    if expected != "pending":
        page = client.get(f"/student-visa/application/{rid}/step/documents").get_data(as_text=True)
        assert 'id="paymentNotCompleted"' in page and 'id="paystackForm"' in page


@pytest.mark.parametrize("outcome", [{"status": "success", "amount": 100},
                                     {"status": "success", "amount": 149999},
                                     {"status": "success", "amount": 1500000},
                                     {"status": "success", "currency": "NGN"}])
def test_wrong_amount_or_currency_never_accepted(client, student, paystack, outcome):
    rid = ready_to_pay(client, student)
    start(client, rid)
    ref = attempts(rid)[0]["paystack_reference"]
    paystack.outcome[ref] = outcome
    client.post(f"/student-visa/payment/{rid}/paystack/check")
    a = attempts(rid)[0]
    assert not unlocked(rid) and a["gateway_status"] == "failed" and "mismatch" in a["rejection_reason"]


def test_forged_callback_for_unknown_or_other_students_reference(client, student, paystack):
    rid = ready_to_pay(client, student)
    start(client, rid)
    ref = attempts(rid)[0]["paystack_reference"]
    paystack.outcome[ref] = {"status": "ongoing"}
    client.get("/paystack/callback?reference=ASB-VISA-1-DEADBEEFDEADBEEF&status=success")
    client.get(f"/paystack/callback?reference={ref}&status=success&amount=150000")
    assert not unlocked(rid)
    # another logged-in student can't even trigger a check of this reference
    other = app_module.app.test_client()
    other.post("/register", data={"email": f"o-{secrets.token_hex(4)}@example.com", "password": "Passw0rd!",
                                  "confirm_password": "Passw0rd!", "full_name": "Other", "country": "Kenya",
                                  "citizenship": "Kenyan", "phone": "0711111111"})
    n = len(paystack.calls)
    paystack.outcome[ref] = {"status": "success"}
    r = other.get(f"/paystack/callback?reference={ref}")
    assert "could not find that payment" in flashes(other) and len(paystack.calls) == n and not unlocked(rid)
    assert r.status_code == 302


def test_already_paid_cannot_be_charged_again(client, student, paystack):
    rid = ready_to_pay(client, student)
    start(client, rid)
    ref = attempts(rid)[0]["paystack_reference"]
    paystack.outcome[ref] = {"status": "success"}
    client.post(f"/student-visa/payment/{rid}/paystack/check")
    n_calls = len(paystack.bodies("/charge"))
    r = start(client, rid)
    assert "Payment already verified." in flashes(client) and r.status_code == 302
    assert len(attempts(rid)) == 1 and len(paystack.bodies("/charge")) == n_calls


def test_pending_attempt_that_was_paid_is_found_before_a_new_charge(client, student, paystack):
    rid = ready_to_pay(client, student)
    start(client, rid)
    ref = attempts(rid)[0]["paystack_reference"]
    paystack.outcome[ref] = {"status": "success"}            # student paid on the phone, never clicked "check"
    start(client, rid)                                        # clicks Pay again
    assert unlocked(rid) and len(attempts(rid)) == 1 and len(paystack.bodies("/charge")) == 1
    assert "Payment already verified." in flashes(client)


# ---------------------------------------------------------------------
# Webhook: signature, re-verification, idempotency
# ---------------------------------------------------------------------
def test_webhook_signed_success_is_reverified_and_idempotent(client, student, paystack):
    rid = ready_to_pay(client, student)
    start(client, rid)
    ref = attempts(rid)[0]["paystack_reference"]
    event = {"event": "charge.success", "data": {"reference": ref, "amount": 150000, "currency": "KES", "status": "success"}}
    paystack.outcome[ref] = {"status": "ongoing"}             # Paystack itself doesn't confirm (yet)
    assert signed_webhook(client, event).status_code == 200
    assert not unlocked(rid)                                   # webhook body alone is NOT proof
    paystack.outcome[ref] = {"status": "success"}
    for _ in range(3):                                         # Paystack retries / duplicates
        assert signed_webhook(client, event).status_code == 200
    assert unlocked(rid)
    assert len(attempts(rid)) == 1
    assert q("SELECT COUNT(*) FROM visa_status_history WHERE request_id = ? AND status = 'payment_verified'",
             (rid,))[0][0] == 1
    assert q("SELECT COUNT(*) FROM visa_admin_notifications WHERE request_id = ? AND message LIKE '%New Visa Assistance Case%'",
             (rid,))[0][0] == 1
    client.post(f"/student-visa/payment/{rid}/paystack/check")  # and the student's own check changes nothing
    assert q("SELECT COUNT(*) FROM visa_status_history WHERE request_id = ? AND status = 'payment_verified'",
             (rid,))[0][0] == 1


@pytest.mark.parametrize("bad", ["wrong-secret", "garbage", "missing"])
def test_webhook_with_bad_signature_is_rejected(client, student, paystack, bad):
    rid = ready_to_pay(client, student)
    start(client, rid)
    ref = attempts(rid)[0]["paystack_reference"]
    paystack.outcome[ref] = {"status": "success"}
    event = {"event": "charge.success", "data": {"reference": ref}}
    if bad == "wrong-secret":
        r = signed_webhook(client, event, secret="sk_test_someone_else")
    elif bad == "garbage":
        r = signed_webhook(client, event, signature="abc123")
    else:
        r = client.post("/paystack/webhook", data=json.dumps(event), content_type="application/json")
    assert r.status_code == 401 and not unlocked(rid)
    assert paystack.bodies(f"/transaction/verify/{ref}") == []


def test_signature_check_unit():
    os.environ["PAYSTACK_SECRET_KEY"] = SECRET
    try:
        raw = b'{"event":"charge.success"}'
        good = hmac.new(SECRET.encode(), raw, hashlib.sha512).hexdigest()
        assert paystack_lib.valid_signature(raw, good) and paystack_lib.valid_signature(raw, good.upper())
        assert not paystack_lib.valid_signature(raw + b" ", good) and not paystack_lib.valid_signature(raw, "")
    finally:
        del os.environ["PAYSTACK_SECRET_KEY"]


# ---------------------------------------------------------------------
# Not configured: everything keeps working with the manual M-PESA flow
# ---------------------------------------------------------------------
def test_missing_keys_fall_back_to_manual_mpesa(client, student, monkeypatch):
    monkeypatch.delenv("PAYSTACK_SECRET_KEY", raising=False)
    monkeypatch.delenv("PAYSTACK_PUBLIC_KEY", raising=False)
    rid = ready_to_pay(client, student)
    page = client.get(f"/student-visa/application/{rid}/step/documents").get_data(as_text=True)
    assert 'id="documentsComplete"' in page and 'id="paystackForm"' not in page
    assert "Continue to M-PESA payment" in page
    pay_page = client.get(f"/student-visa/payment/{rid}").get_data(as_text=True)
    assert 'id="mpesaForm"' in pay_page and 'id="paystackForm"' not in pay_page
    start(client, rid)
    assert attempts(rid) == [] and "not available" in flashes(client)
    assert client.post("/paystack/webhook", data=b"{}", content_type="application/json").status_code == 404


def test_manual_sms_refused_while_paystack_is_configured(client, student, paystack):
    rid = ready_to_pay(client, student)
    pay_page = client.get(f"/student-visa/payment/{rid}").get_data(as_text=True)
    assert 'id="paystackForm"' in pay_page and 'id="mpesaForm"' not in pay_page
    client.post(f"/student-visa/payment/{rid}/submit", data={"mpesa_message": "QK12345678 Confirmed. Ksh 1500 sent to X"})
    assert q("SELECT COUNT(*) FROM visa_payments WHERE request_id = ?", (rid,))[0][0] == 0


# ---------------------------------------------------------------------
# Standalone (pay-first) service uses the same Paystack payment
# ---------------------------------------------------------------------
def test_standalone_service_pays_first_with_paystack(client, student, paystack):
    r = client.post("/student-visa/start")
    rid = request_of(student)["id"]
    assert request_of(student)["form_first"] == 0
    page = client.get(f"/student-visa/payment/{rid}").get_data(as_text=True)
    assert 'id="paystackForm"' in page
    start(client, rid)
    ref = attempts(rid)[0]["paystack_reference"]
    paystack.outcome[ref] = {"status": "success"}
    client.get(f"/paystack/callback?reference={ref}")
    assert unlocked(rid)
    assert client.get(f"/student-visa/application/{rid}").status_code == 302      # form now open
    assert client.get(f"/student-visa/application/{rid}/step/personal").status_code == 200
    assert r.status_code == 302


# ---------------------------------------------------------------------
# Admin: Visa Admin sees Paystack details; can't hand-verify; Main Admin summary
# ---------------------------------------------------------------------
def _visa_admin():
    c = app_module.app.test_client()
    uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'visa_admin')",
                  (f"va-{secrets.token_hex(4)}@example.org",))
    execute("INSERT INTO visa_admins (user_id, full_name) VALUES (?, 'VA')", (uid,))
    with c.session_transaction() as s:
        s["visa_admin_user_id"] = uid
    return c


def test_admin_views_and_no_manual_verification_of_paystack(client, student, paystack):
    rid = ready_to_pay(client, student)
    start(client, rid)
    a = attempts(rid)[0]
    admin = _visa_admin()
    html = admin.get(f"/visa-admin/payments/{a['id']}").get_data(as_text=True)
    for want in ('id="paystackDetails"', a["paystack_reference"], "KSh 1,500", "KES", "Pending - waiting for payment",
                 "Re-check with Paystack"):
        assert want in html, want
    assert 'name="confirm_received"' not in html               # no manual VERIFY form for Paystack rows
    assert SECRET not in html
    admin.post(f"/visa-admin/payment-proof/{a['id']}/review", data={"action": "verify", "confirm_received": "yes"})
    assert not unlocked(rid)                                   # hand-verification refused
    paystack.outcome[a["paystack_reference"]] = {"status": "success"}
    admin.post(f"/visa-admin/payments/{a['id']}/paystack-recheck")
    assert unlocked(rid)
    # Main Admin: non-sensitive summary with Paystack reference + transaction status
    mc = app_module.app.test_client()
    uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'admin')",
                  (f"ma-{secrets.token_hex(4)}@example.org",))
    execute("INSERT INTO admins (user_id, full_name) VALUES (?, 'MA')", (uid,))
    with mc.session_transaction() as s:
        s["role"], s["user_id"] = "admin", uid
    app_id = get_application(student["student_id"])["id"]
    summary = mc.get(f"/admin/applications/{app_id}").get_data(as_text=True).split('id="visaSummary"')[1].split("</table>")[0]
    for want in (a["paystack_reference"], "KES", "Paystack", "Successful (verified with Paystack)", "Paid"):
        assert want in summary, want
    assert SECRET not in summary


def test_account_deletion_still_removes_paystack_records(client, student, paystack):
    rid = ready_to_pay(client, student)
    start(client, rid)
    assert attempts(rid)
    execute("DELETE FROM users WHERE id = (SELECT user_id FROM students WHERE id = ?)", (student["student_id"],))
    assert attempts(rid) == []


def test_documents_and_required_fields_unchanged_by_payment_failure(client, student, paystack):
    rid = ready_to_pay(client, student)
    before = dict(request_of(student))
    for _ in range(2):
        start(client, rid)
        paystack.outcome[attempts(rid)[-1]["paystack_reference"]] = {"status": "failed"}
        client.post(f"/student-visa/payment/{rid}/paystack/check")
    after = dict(request_of(student))
    for f in list(VISA_FORM_ANSWERS["personal"]) + ["passport_number", "destination_country"]:
        assert after[f] == before[f], f
    assert all(a["gateway_status"] == "failed" for a in attempts(rid)) and not unlocked(rid)
    assert after["full_name"] == APPLICANT["full_name"]


# ---------------------------------------------------------------------
# The real HTTP client (paystack_lib._call) against a local stand-in server
# ---------------------------------------------------------------------
def test_http_client_sends_bearer_key_and_parses_responses(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            seen["auth"] = self.headers.get("Authorization")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/charge" and seen["body"]["reference"].startswith("BAD"):
                # A charge Paystack attempted but could not start: HTTP 400 whose
                # real reason is inside `data`, not the top-level message.
                self._reply(400, {"status": False, "message": "Charge attempted",
                                  "data": {"status": "failed", "message": "Invalid phone number",
                                           "reference": seen["body"]["reference"]}})
            elif self.path == "/charge":
                self._reply(200, {"status": True, "message": "Charge attempted",
                                  "data": {"status": "pay_offline", "display_text": "Enter PIN",
                                           "reference": seen["body"]["reference"]}})
            else:
                self._reply(400, {"status": False, "message": "Invalid amount"})

        def do_GET(self):
            self._reply(200, {"status": True, "data": {"status": "success", "reference": self.path.rsplit("/", 1)[1],
                                                       "amount": 150000, "currency": "KES"}})

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(paystack_lib, "API_BASE", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", SECRET)
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(k, raising=False)
    try:
        result = paystack_lib.charge_mpesa("a@b.co", 150000, "ASB-VISA-1-ABC", "+254712345678", {"x": 1})
        assert result == {"status": "pay_offline", "display_text": "Enter PIN", "reference": "ASB-VISA-1-ABC"}
        assert seen["auth"] == f"Bearer {SECRET}"
        assert seen["body"]["reference"] == "ASB-VISA-1-ABC" and seen["body"]["email"] == "a@b.co"
        with pytest.raises(paystack_lib.PaystackError) as e:
            paystack_lib.charge_mpesa("a@b.co", 150000, "BAD-1", "+254712345678")
        err = e.value
        assert err.http_status == 400 and err.api_status is False and err.api_message == "Charge attempted"
        assert err.charge_attempted and err.reason == "Invalid phone number"
        diag = err.diagnostics()
        assert "http=400" in diag and "data.status='failed'" in diag and "Invalid phone number" in diag
        assert SECRET not in diag and SECRET not in str(err) and "712345678" not in diag
        assert seen["body"]["mobile_money"] == {"phone": "+254712345678", "provider": "mpesa"}
        assert seen["body"]["amount"] == "150000" and seen["body"]["currency"] == "KES"
        assert paystack_lib.verify("ASB-VISA-1-ABC")["status"] == "success"
        with pytest.raises(paystack_lib.PaystackError) as e:
            paystack_lib.initialize_checkout("a@b.co", 150000, "R1", "https://x/cb")
        assert "Invalid amount" in str(e.value) and SECRET not in str(e.value)
        with pytest.raises(paystack_lib.PaystackError):
            paystack_lib.verify("../../etc/passwd")                       # path injection refused
    finally:
        server.shutdown()


def test_two_simultaneous_confirmations_unlock_once(client, student, paystack):
    """Webhook and the student's own check both read the attempt while it
    was still 'pending' and both apply Paystack's success: only one may
    unlock (the atomic claim on the row)."""
    rid = ready_to_pay(client, student)
    start(client, rid)
    stale = q("SELECT * FROM visa_payments WHERE request_id = ? AND gateway = 'paystack'", (rid,))[0]
    data = {"reference": stale["paystack_reference"], "status": "success", "amount": 150000, "currency": "KES",
            "channel": "mobile_money", "paid_at": "2026-10-03T08:00:00Z"}
    with app_module.app.test_request_context():
        db1, db2 = get_db(), get_db()
        assert app_module._apply_paystack_result(db1, stale, data) == "successful"
        assert app_module._apply_paystack_result(db2, stale, data) == "successful"   # same stale row
        db1.close()
        db2.close()
    assert unlocked(rid)
    assert q("SELECT COUNT(*) FROM visa_status_history WHERE request_id = ? AND status = 'payment_verified'",
             (rid,))[0][0] == 1
    assert q("SELECT COUNT(*) FROM notifications WHERE student_id = ? AND message LIKE '%Payment verified%'",
             (student["student_id"],))[0][0] == 1
    # ...and the same payment is not mis-reported as a duplicate needing a refund
    assert q("SELECT COUNT(*) FROM visa_admin_notifications WHERE request_id = ? AND message LIKE '%Duplicate%'",
             (rid,))[0][0] == 0
    assert q("SELECT admin_notes FROM visa_payments WHERE id = ?", (stale["id"],))[0][0] is None


# ---------------------------------------------------------------------
# "Charge attempted" handling (Paystack Kenya M-PESA, /charge)
# ---------------------------------------------------------------------
DOCS_PAY_OFFLINE = {"reference": "test-paystack-reference", "status": "pay_offline",
                    "display_text": "Please complete authorization process on your mobile phone"}


def _verified_rows(rid):
    return q("SELECT COUNT(*) FROM visa_payments WHERE request_id = ? AND payment_status = 'PAYMENT_VERIFIED'",
             (rid,))[0][0]


def test_A_charge_attempted_pay_offline_is_pending_not_a_failure(client, student, paystack):
    rid = ready_to_pay(client, student)
    paystack.charge_reply = dict(DOCS_PAY_OFFLINE)
    r = start(client, rid, phone="0710000000")
    msg = flashes(client)
    assert "couldn't start" not in msg and "Nothing was charged" not in msg
    assert "Payment request sent" in msg and "Please complete authorization process on your mobile phone" in msg
    [a] = attempts(rid)
    assert a["paystack_reference"] == "test-paystack-reference"            # Paystack's reference stored
    assert a["gateway_status"] == "pending" and a["payment_status"] == "PAYMENT_PENDING"
    assert a["gateway_message"] == DOCS_PAY_OFFLINE["display_text"]
    assert not unlocked(rid) and _verified_rows(rid) == 0                  # NOT verified by the charge reply
    assert paystack.bodies("/transaction/verify/test-paystack-reference") == []
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert 'id="paymentPending"' in page and "waiting for M-PESA authorization" in page
    # The request Paystack received
    [body] = paystack.bodies("/charge")
    assert body["amount"] == "150000" and body["currency"] == "KES" and body["email"] == "amina@example.com"
    assert body["mobile_money"] == {"phone": "+254710000000", "provider": "mpesa"}


def test_B_C_success_webhook_verifies_once_even_if_sent_twice(client, student, paystack):
    rid = ready_to_pay(client, student)
    ref = f"test-paystack-reference-{secrets.token_hex(4)}"
    paystack.charge_reply = dict(DOCS_PAY_OFFLINE, reference=ref)
    start(client, rid)
    assert attempts(rid)[0]["paystack_reference"] == ref
    paystack.outcome[ref] = {"status": "success"}
    event = {"event": "charge.success", "data": {"reference": ref, "status": "success", "amount": 150000}}
    assert signed_webhook(client, event).status_code == 200
    assert len(paystack.bodies(f"/transaction/verify/{ref}")) == 1           # re-verified with Paystack
    assert unlocked(rid) and _verified_rows(rid) == 1
    a = attempts(rid)[0]
    assert a["paid_amount_subunit"] == 150000 and a["paid_currency"] == "KES" and a["verified"] == 1
    notes = lambda: q("SELECT COUNT(*) FROM visa_admin_notifications WHERE request_id = ? AND message LIKE ?",  # noqa: E731
                      (rid, "%New Visa Assistance Case%"))[0][0]
    before = notes()
    assert signed_webhook(client, event).status_code == 200                 # duplicate delivery
    assert unlocked(rid) and _verified_rows(rid) == 1 and notes() == before
    assert len(paystack.bodies(f"/transaction/verify/{ref}")) == 1           # already credited: no re-processing
    assert len(attempts(rid)) == 1


def test_D_paystack_refusal_is_friendly_logged_and_never_verified(client, student, paystack, caplog):
    rid = ready_to_pay(client, student)
    paystack.charge_error = paystack_lib._error_from_payload(
        "Paystack HTTP 400", 400, {"status": False, "message": "Some real Paystack error"})
    with caplog.at_level("WARNING"):
        start(client, rid, phone="0712345678")
    msg = flashes(client)
    assert "couldn't start the payment" in msg and "Some real Paystack error" in msg
    [a] = attempts(rid)
    assert a["gateway_status"] == "failed" and not unlocked(rid) and _verified_rows(rid) == 0
    log = caplog.text
    assert "http=400" in log and "Some real Paystack error" in log and a["paystack_reference"] in log
    assert "0712345678" not in log and "+254712345678" not in log and SECRET not in log   # phone masked, no key


def _charge_attempted_400(data_status="failed", message="Test mobile money only works with the test number"):
    return paystack_lib._error_from_payload("Paystack HTTP 400", 400, {
        "status": False, "message": "Charge attempted", "data": {"status": data_status, "message": message}})


def test_D2_http_400_charge_attempted_is_checked_with_verify_not_assumed(client, student, paystack, caplog):
    """The production symptom: HTTP 400 'Charge attempted'. The real reason is
    in data; Paystack is then asked (verify) for the reference's real state.
    Here verify says Paystack is still processing -> stays pending."""
    rid = ready_to_pay(client, student)
    paystack.charge_error = _charge_attempted_400()
    with caplog.at_level("WARNING"):
        start(client, rid)
    [a] = attempts(rid)
    assert len(paystack.bodies(f"/transaction/verify/{a['paystack_reference']}")) == 1
    assert a["gateway_status"] == "pending" and not unlocked(rid) and _verified_rows(rid) == 0
    assert "Charge attempted" in caplog.text and "Test mobile money only works with the test number" in caplog.text
    assert "Nothing was charged" not in flashes(client)


def test_D2b_http_400_charge_attempted_confirmed_failed_shows_paystack_reason(client, student, paystack):
    rid = ready_to_pay(client, student)
    paystack.charge_error = _charge_attempted_400()
    paystack.default_outcome = {"status": "failed", "gateway_response": ""}
    start(client, rid)
    [a] = attempts(rid)
    assert a["gateway_status"] == "failed" and not unlocked(rid) and _verified_rows(rid) == 0
    assert a["gateway_message"] == "Test mobile money only works with the test number"
    msg = flashes(client)
    assert "Test mobile money only works with the test number" in msg and "couldn't start" not in msg
    page = client.get(f"/student-visa/application/{rid}/step/documents").get_data(as_text=True)
    assert 'id="paymentNotCompleted"' in page and "Test mobile money only works with the test number" in page
    start(client, rid)                                                      # retry still possible
    assert len(attempts(rid)) == 2


def test_D3_charge_attempted_when_verify_is_unreachable_never_success(client, student, paystack):
    """Verify unreachable: an HTTP 400 is a genuine failure whatever data.status
    says; a non-HTTP-error refusal may stay pending; nothing is ever paid."""
    rid = ready_to_pay(client, student)
    paystack.verify_unavailable = True
    for data_status in ("failed", "pay_offline", "success"):
        paystack.charge_error = _charge_attempted_400(data_status, f"m-{data_status}")
        start(client, rid)
        a = attempts(rid)[-1]
        assert a["gateway_status"] == "failed" and a["gateway_message"] == f"m-{data_status}", data_status
        assert not unlocked(rid)
    for data_status, expected in (("pay_offline", "pending"), ("success", "pending"), ("failed", "failed")):
        paystack.charge_error = paystack_lib._error_from_payload("Paystack refused the request", 200, {
            "status": False, "message": "Charge attempted", "data": {"status": data_status}})
        start(client, rid)
        assert attempts(rid)[-1]["gateway_status"] == expected, data_status
        assert not unlocked(rid)


def test_E_kenyan_phone_normalisation_and_friendly_rejection(client, student, paystack):
    n = paystack_lib.normalize_kenyan_phone
    for raw in ("0712345678", "254712345678", "+254712345678"):
        assert n(raw) == "+254712345678"
    assert n("0112345678") == "+254112345678"
    rid = ready_to_pay(client, student)
    start(client, rid, phone="07123")
    assert "valid Kenyan M-PESA number" in flashes(client) and attempts(rid) == []
    assert paystack_lib.mask_phone("+254712345678") == "+2547******78"


@pytest.mark.parametrize("outcome", [{"status": "success", "amount": 15000},
                                     {"status": "success", "currency": "NGN"},
                                     {"status": "success", "reference": "ASB-VISA-999-OTHERPAYMENT00"}],
                         ids=["F-amount", "G-currency", "H-reference"])
def test_F_G_H_successful_looking_mismatches_never_verify(client, student, paystack, outcome):
    rid = ready_to_pay(client, student)
    start(client, rid)
    ref = attempts(rid)[0]["paystack_reference"]
    paystack.outcome[ref] = outcome
    client.post(f"/student-visa/payment/{rid}/paystack/check")
    assert not unlocked(rid) and _verified_rows(rid) == 0
    assert attempts(rid)[0]["gateway_status"] != "successful"


@pytest.mark.parametrize("still", ["pay_offline", "pending", "ongoing"])
def test_I_requery_while_waiting_for_authorization_stays_pending(client, student, paystack, still):
    rid = ready_to_pay(client, student)
    start(client, rid)
    ref = attempts(rid)[0]["paystack_reference"]
    paystack.outcome[ref] = {"status": still}
    client.post(f"/student-visa/payment/{rid}/paystack/check")
    assert attempts(rid)[0]["gateway_status"] == "pending" and not unlocked(rid)
    assert paystack.bodies(f"/transaction/verify/{ref}")                    # asked Paystack with OUR reference
    assert "still being processed" in flashes(client)


def test_failed_webhook_event_is_reverified_and_never_unlocks(client, student, paystack):
    rid = ready_to_pay(client, student)
    start(client, rid)
    ref = attempts(rid)[0]["paystack_reference"]
    paystack.outcome[ref] = {"status": "failed", "gateway_response": "Insufficient funds"}
    assert signed_webhook(client, {"event": "charge.failed", "data": {"reference": ref}}).status_code == 200
    a = attempts(rid)[0]
    assert a["gateway_status"] == "failed" and a["gateway_message"] == "Insufficient funds" and not unlocked(rid)


def test_returned_reference_already_used_elsewhere_is_never_attached(client, student, paystack):
    rid = ready_to_pay(client, student)
    start(client, rid)
    first = attempts(rid)[0]["paystack_reference"]
    paystack.charge_reply = dict(DOCS_PAY_OFFLINE, reference=first)          # Paystack echoes an existing reference
    r = start(client, rid)
    assert r.status_code == 302
    second = attempts(rid)[1]
    assert second["paystack_reference"] != first and second["gateway_status"] == "pending" and not unlocked(rid)


def test_regression_http_400_charge_attempted_end_to_end_preserves_diagnostics(client, student, monkeypatch, caplog):
    """Reproduces the Render symptom through the REAL HTTP client and the
    real start route: /charge answers HTTP 400 {status:false, "Charge
    attempted", data:{...}}. The data must be kept, logged safely, shown to
    the student, and the attempt must fail (never pending/paid)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            seen["path"], seen["auth"] = self.path, self.headers.get("Authorization")
            seen["content_type"] = self.headers.get("Content-Type")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self._reply(400, {"status": False, "message": "Charge attempted",
                              "data": {"status": "failed",
                                       "message": "Charge could not be completed for +254712345678",
                                       "gateway_response": "Declined",
                                       "reference": seen["body"]["reference"]}})

        def do_GET(self):
            self._reply(404, {"status": False, "message": "Transaction reference not found"})

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(paystack_lib, "API_BASE", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", SECRET)
    monkeypatch.setenv("PAYSTACK_PUBLIC_KEY", PUBLIC)
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(k, raising=False)
    try:
        rid = ready_to_pay(client, student)
        with caplog.at_level("WARNING"):
            start(client, rid, phone="0712345678")
    finally:
        server.shutdown()
    [a] = attempts(rid)
    ref = a["paystack_reference"]
    # Exact request sent
    assert seen["path"] == "/charge" and seen["auth"] == f"Bearer {SECRET}"
    assert seen["content_type"] == "application/json"
    vr = request_of(student)
    assert seen["body"] == {"email": "amina@example.com", "amount": "150000", "currency": "KES", "reference": ref,
                            "mobile_money": {"phone": "+254712345678", "provider": "mpesa"},
                            "metadata": {"visa_request_id": rid, "request_number": vr["request_number"],
                                         "purpose": "visa_assistance_fee"}}
    # Diagnostics preserved and logged (safe fields only)
    diag = [r.getMessage() for r in caplog.records if "Paystack HTTP error diagnostics" in r.getMessage()]
    charge_line = next(line for line in diag if "Charge attempted" in line)
    for want in ("http=400", "status=False", "message='Charge attempted'", "data_status='failed'",
                 "gateway_response='Declined'", f"reference='{ref}'", "data_message='Charge could not be completed"):
        assert want in charge_line, want
    assert any("message='Transaction reference not found'" in line and "http=404" in line for line in diag)
    log = caplog.text
    assert SECRET not in log and "Bearer" not in log
    assert "712345678" not in log and "+2547******78" in log                 # phone masked even inside messages
    # Genuine failure: never pending, never paid; the reason is kept and shown
    assert a["gateway_status"] == "failed" and a["payment_status"] == "PAYMENT_REJECTED" and not unlocked(rid)
    assert a["gateway_message"] == "Charge could not be completed for +254712345678"
    msg = flashes(client)
    assert "Charge could not be completed" in msg and "Nothing was charged" not in msg
