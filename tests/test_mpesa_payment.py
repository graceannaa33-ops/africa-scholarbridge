"""M-PESA confirmation parsing and the visa-assistance payment flow.

Parsing is an aid, never verification: every submission stays
PAYMENT_PENDING until a Visa Admin clicks VERIFY PAYMENT.
"""
import secrets
import string
from decimal import Decimal

import pytest

import app as app_module
import mpesa_parser as mp
from database import get_db

EXAMPLE = ("UIR1N8OV43 Confirmed. Ksh 1500 sent to JOYCE BAARIU 0181785792 on 30/9/26 at 16.24 PM. "
           "New M-PESA balance is Ksh1,854.61. Transaction cost, Ksh0.00. Amount you can transact within "
           "the day is 499,830.00. See all your balances now https://saf.cx/iqIzU")
FEE, RECIPIENT = 1500, "0181785792"


def D(v):
    return Decimal(v)


def new_code():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        c = "".join(secrets.choice(alphabet) for _ in range(10))
        if any(ch.isdigit() for ch in c) and any(ch.isalpha() for ch in c):
            return c


def sms(code=None, amount="Ksh 1500", name="JOYCE BAARIU", phone="0181785792", date="30/9/26",
        time="16.24 PM", balance="Ksh1,854.61", cost="Ksh0.00", limit="499,830.00"):
    return (f"{code or new_code()} Confirmed. {amount} sent to {name} {phone} on {date} at {time}. "
            f"New M-PESA balance is {balance}. Transaction cost, {cost}. Amount you can transact within "
            f"the day is {limit}. See all your balances now https://saf.cx/iqIzU")


def check(text):
    parsed = mp.parse_mpesa_message(text)
    return parsed, mp.validate_student_message(parsed, FEE, RECIPIENT)


def codes(flags, severity=None):
    return {f["code"] for f in flags if severity is None or f["severity"] == severity}


# ---------------------------------------------------------------------
# 1. The exact example
# ---------------------------------------------------------------------
def test_exact_example_extracts_every_field():
    p, flags = check(EXAMPLE)
    assert p["transaction_code"] == "UIR1N8OV43"
    assert p["status"] == "Confirmed"
    assert p["amount"] == D("1500.00")
    assert p["counterparty_name"] == "JOYCE BAARIU"
    assert p["counterparty_phone"] == "0181785792"
    assert p["date"] == "30/9/26"
    assert p["time"] == "16.24 PM"
    assert p["balance"] == D("1854.61")
    assert p["transaction_cost"] == D("0.00")
    assert p["daily_limit"] == D("499830.00")
    assert p["direction"] == "sent" and p["looks_like_mpesa"] is True
    assert codes(flags, "error") == set() and codes(flags, "warning") == set()
    assert {"amount_ok", "structure_ok", "recipient_ok", "code_found"} <= codes(flags, "ok")


# ---------------------------------------------------------------------
# 2. Normal formatting variations - all must parse as KSh 1,500
# ---------------------------------------------------------------------
VARIANTS = {
    "amount no space": sms(amount="Ksh1500"),
    "amount comma": sms(amount="Ksh1,500"),
    "amount decimals": sms(amount="Ksh 1,500.00"),
    "amount KSh. prefix": sms(amount="KSh. 1,500.00"),
    "amount KES": sms(amount="KES 1500"),
    "double spaces": sms().replace(" ", "  "),
    "newlines and tabs": sms().replace(". ", ".\n").replace(" sent ", "\tsent "),
    "non-breaking spaces": sms().replace(" sent to ", " sent to "),
    "all lowercase": sms().lower(),
    "all uppercase": sms().upper(),
    "date 30/09/2026": sms(date="30/09/2026"),
    "date 1/10/26": sms(date="1/10/26"),
    "date with dashes": sms(date="30-09-2026"),
    "time 4:24 PM": sms(time="4:24 PM"),
    "time 4:24PM": sms(time="4:24PM"),
    "time 24h 16:24": sms(time="16:24"),
    "time 4.24 p.m.": sms(time="4.24 p.m."),
    "time 9:05 am": sms(time="9:05 am"),
    "one-word name": sms(name="JOYCE"),
    "three-word name": sms(name="JOYCE WANJIKU BAARIU"),
    "name extra spaces": sms(name="JOYCE   BAARIU"),
    "name with apostrophe/hyphen": sms(name="MARY-ANN O'NEILL"),
    "no full stops": sms().replace(". ", " "),
    "no comma after cost": sms().replace("cost,", "cost"),
    "no URL": sms().split(" See all")[0],
    "code then colon": sms(code="UIR1N8OV43").replace("UIR1N8OV43 Confirmed", "UIR1N8OV43: Confirmed"),
    "transaction cost 7.00": sms(cost="Ksh7.00"),
    "transaction cost 13": sms(cost="Ksh13"),
    "balance no comma": sms(balance="Ksh1854.61"),
    "balance large": sms(balance="Ksh 1,234,567.89"),
    "limit with Ksh": sms(limit="Ksh499,830.00"),
    "no trailing figures": sms().split(" New M-PESA")[0] + ".",
    "leading/trailing whitespace": "\n  " + sms() + "  \n",
}


@pytest.mark.parametrize("label", list(VARIANTS))
def test_formatting_variations_parse_correctly(label):
    p, flags = check(VARIANTS[label])
    assert p["amount"] == D("1500.00"), label
    assert p["status"] == "Confirmed" and p["transaction_code"]
    assert p["counterparty_phone"] == "0181785792"
    assert p["date"] and p["time"]
    assert p["looks_like_mpesa"] is True, label
    assert codes(flags, "error") == set(), (label, flags)


def test_variation_details():
    p, _ = check(sms(name="JOYCE   WANJIKU  BAARIU", time="4:24pm", date="30/09/2026", cost="Ksh7.00",
                     balance="Ksh 1,234,567.89"))
    assert p["counterparty_name"] == "JOYCE WANJIKU BAARIU"
    assert p["time"] == "4:24 PM" and p["date"] == "30/09/2026"
    assert p["transaction_cost"] == D("7.00") and p["balance"] == D("1234567.89")
    assert check(sms(time="4.24 p.m."))[0]["time"] == "4.24 PM"
    assert check(sms(code="UIR1N8OV43").lower())[0]["transaction_code"] == "UIR1N8OV43"


# ---------------------------------------------------------------------
# 3. Balance / cost / daily limit are never the payment amount
# ---------------------------------------------------------------------
@pytest.mark.parametrize("balance,cost,limit", [
    ("Ksh1,854.61", "Ksh0.00", "499,830.00"),
    ("Ksh1,500.00", "Ksh1,500.00", "1,500.00"),          # trailing figures equal the fee
    ("Ksh0.00", "Ksh1500", "1500"),
    ("Ksh99,999.00", "Ksh33.00", "10.00"),
])
def test_trailing_figures_never_taken_as_amount(balance, cost, limit):
    p, _ = check(sms(amount="Ksh 1,500", balance=balance, cost=cost, limit=limit))
    assert p["amount"] == D("1500.00")
    p, flags = check(sms(amount="Ksh 200", balance=balance, cost=cost, limit=limit))
    assert p["amount"] == D("200.00") and "wrong_amount" in codes(flags, "error")


def test_message_with_only_trailing_figures_has_no_amount():
    p, flags = check("UIR1N8OV43 Confirmed. New M-PESA balance is Ksh1,500.00. Transaction cost, Ksh1,500.00. "
                     "Amount you can transact within the day is 1,500.00.")
    assert p["amount"] is None and p["balance"] == D("1500.00")
    assert "no_amount" in codes(flags, "error") and p["looks_like_mpesa"] is False


# ---------------------------------------------------------------------
# 4. Invalid / fake messages are refused
# ---------------------------------------------------------------------
FAKES = {
    "empty": "",
    "just the amount": "Ksh 1500",
    "words only": "I have paid Ksh 1500, it is Confirmed",
    "Confirmed and amount, no code": "Confirmed. Ksh 1500 sent to JOYCE BAARIU 0181785792 on 30/9/26 at 16.24 PM.",
    "code + Confirmed + amount only": "ABCD123456 Confirmed Ksh 1500",
    "code + amount + date, no sent-to": "UIR1N8OV43 Confirmed. Ksh 1500 on 30/9/26 at 16.24 PM.",
    "all-letter 'code'": "ABCDEFGHIJ Confirmed. Ksh 1500 sent to JOYCE BAARIU 0181785792 on 30/9/26 at 4:24 PM.",
    "all-digit 'code'": "1234567890 Confirmed. Ksh 1500 sent to JOYCE BAARIU 0181785792 on 30/9/26 at 4:24 PM.",
    "failed transaction": "UIR1N8OV43 Failed. Insufficient funds in your M-PESA account to send Ksh1,500.00.",
    "only balance": "UIR1N8OV43 Confirmed. Your M-PESA balance was Ksh1,500.00 on 30/9/26 at 4:24 PM.",
    "promo text": "Confirmed! Get Ksh 1500 bonus when you send money. Visit https://saf.cx/abc",
    "received message": "UIR1N8OSFE Confirmed.You have received Ksh1,500.00 from EDWARD DAVID 0758***959 "
                        "on 27/9/26 at 10:10 PM New M-PESA balance is Ksh3,354.61.",
}


@pytest.mark.parametrize("label", list(FAKES))
def test_invalid_or_fake_messages_are_refused(label):
    p, flags = check(FAKES[label])
    assert mp.has_errors(flags), (label, flags)
    assert not (p["looks_like_mpesa"] and p["direction"] == "sent")


def test_incomplete_but_structured_message_goes_to_manual_review():
    """Has the payment sentence but no time -> accepted for a human to review,
    never marked as a valid automatic parse."""
    text = "UIR1N8OV43 Confirmed. Ksh 1500 sent to JOYCE BAARIU 0181785792 on 30/9/26."
    p, flags = check(text)
    assert p["amount"] == D("1500.00") and p["looks_like_mpesa"] is False
    assert not mp.has_errors(flags)
    manual = [f for f in flags if f["code"] == "manual_review"]
    assert manual and manual[0]["message"] == "Unable to automatically verify this M-PESA message. Please review manually."
    assert "structure_ok" not in codes(flags)


# ---------------------------------------------------------------------
# 5. Wrong amounts: below / above KSh 1,500
# ---------------------------------------------------------------------
@pytest.mark.parametrize("amount", ["Ksh 1,499.00", "Ksh 1499", "Ksh 150", "Ksh 1", "Ksh 1,500.50",
                                    "Ksh 1,501", "Ksh 15,000", "Ksh 150,000.00", "Ksh 0.00"])
def test_amounts_other_than_1500_are_refused(amount):
    p, flags = check(sms(amount=amount))
    assert p["amount"] != D("1500.00")
    assert "wrong_amount" in codes(flags, "error") or "no_amount" in codes(flags, "error")


def test_exactly_1500_in_every_notation_is_accepted():
    for amount in ("Ksh 1500", "Ksh1500", "Ksh 1,500", "Ksh1,500.00", "Ksh 1500.0", "KES 1,500.00"):
        _, flags = check(sms(amount=amount))
        assert "amount_ok" in codes(flags, "ok"), amount


# ---------------------------------------------------------------------
# 6. End-to-end payment flow (student -> admin), duplicate codes
# ---------------------------------------------------------------------
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


def visa_request_for(client, student):
    client.post("/application/step/visa", data={"visa_choice": "no"})
    return q("SELECT id FROM visa_requests WHERE student_id = ?", (student["student_id"],))[0][0]


def submit(client, req, text, payer="0712345678"):
    return client.post(f"/student-visa/payment/{req}/submit",
                       data={"mpesa_message": text, "payment_phone": payer})


def payments_of(req):
    return [dict(r) for r in q("SELECT * FROM visa_payments WHERE request_id = ? ORDER BY id", (req,))]


def visa_admin_client():
    c = app_module.app.test_client()
    uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'visa_admin')",
                  (f"va-{secrets.token_hex(4)}@example.org",))
    execute("INSERT INTO visa_admins (user_id, full_name) VALUES (?, 'Visa Admin')", (uid,))
    with c.session_transaction() as s:
        s["visa_admin_user_id"] = uid
    return c


def flashes(client):
    with client.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


def test_example_sms_submission_is_pending_until_admin_verifies(client, student):
    req = visa_request_for(client, student)
    code = new_code()
    text = EXAMPLE.replace("UIR1N8OV43", code)
    r = submit(client, req, text)
    assert r.status_code == 302
    [p] = payments_of(req)
    assert p["payment_status"] == "PAYMENT_PENDING" and p["verified"] == 0
    assert p["mpesa_transaction_code"] == code and p["code_source"] == "sms"
    assert p["submitted_amount"] == 1500.0
    assert p["extracted_recipient_name"] == "JOYCE BAARIU" and p["extracted_recipient_phone"] == "0181785792"
    assert p["extracted_transaction_date"] == "30/9/26" and p["extracted_transaction_time"] == "16.24 PM"
    assert p["submitted_mpesa_message"] == text and p["phone_number"] == "0712345678"
    vr = q("SELECT payment_status, payment_verified FROM visa_requests WHERE id = ?", (req,))[0]
    assert vr["payment_status"] == "pending_verification" and vr["payment_verified"] == 0

    # Admin review page shows every extracted field.
    admin = visa_admin_client()
    html = admin.get(f"/visa-admin/payments/{p['id']}").get_data(as_text=True)
    table = html.split('id="extractedDetails"')[1].split("</table>")[0]
    for expected in (code, "Confirmed", "KSh 1,500.00", "0712345678", "JOYCE BAARIU", "0181785792",
                     "30/9/26", "16.24 PM", "KSh 0.00", "KSh 1,854.61", "KSh 499,830.00", "Pending"):
        assert expected in table, expected
    assert "Standard M-PESA confirmation structure detected" in html
    assert "Unable to automatically verify" not in html

    # Only the admin's explicit VERIFY unlocks the application.
    r = admin.post(f"/visa-admin/payment-proof/{p['id']}/review", data={"action": "verify"})
    assert payments_of(req)[0]["payment_status"] == "PAYMENT_PENDING"          # no confirmation tick
    admin.post(f"/visa-admin/payment-proof/{p['id']}/review", data={"action": "verify", "confirm_received": "yes"})
    assert payments_of(req)[0]["payment_status"] == "PAYMENT_VERIFIED"
    vr = q("SELECT payment_status, payment_verified FROM visa_requests WHERE id = ?", (req,))[0]
    assert vr["payment_status"] == "paid" and vr["payment_verified"] == 1


def test_manual_review_message_shown_to_admin_with_original(client, student):
    req = visa_request_for(client, student)
    text = f"{new_code()} Confirmed. Ksh 1500 sent to JOYCE BAARIU 0181785792 on 30/9/26."   # no time
    submit(client, req, text)
    [p] = payments_of(req)
    assert p["payment_status"] == "PAYMENT_PENDING"
    html = visa_admin_client().get(f"/visa-admin/payments/{p['id']}").get_data(as_text=True)
    assert "Unable to automatically verify this M-PESA message. Please review manually." in html
    assert text in html                                                  # original message shown


@pytest.mark.parametrize("label", ["words only", "code + Confirmed + amount only", "received message",
                                   "Confirmed and amount, no code"])
def test_fake_messages_are_not_stored(client, student, label):
    req = visa_request_for(client, student)
    submit(client, req, FAKES[label])
    assert payments_of(req) == []
    assert q("SELECT payment_status FROM visa_requests WHERE id = ?", (req,))[0][0] != "paid"


@pytest.mark.parametrize("amount", ["Ksh 1,499.00", "Ksh 1,501.00", "Ksh 15,000.00", "Ksh 150"])
def test_wrong_amount_submission_is_refused(client, student, amount):
    req = visa_request_for(client, student)
    submit(client, req, sms(amount=amount))
    assert payments_of(req) == []
    assert "fee is exactly" in flashes(client)


def test_duplicate_transaction_code_is_refused(client, student):
    req = visa_request_for(client, student)
    code = new_code()
    submit(client, req, sms(code=code))
    assert len(payments_of(req)) == 1

    other = app_module.app.test_client()
    email = f"dup-{secrets.token_hex(4)}@example.com"
    other.post("/register", data={"email": email, "password": "Passw0rd!", "confirm_password": "Passw0rd!",
                                  "full_name": "Other Student", "country": "Kenya", "citizenship": "Kenyan",
                                  "phone": "0711111111"})
    other.get("/application/start")
    other.post("/application/step/personal", data={
        "full_name": "Other Student", "date_of_birth": "2002-02-02", "country": "Kenya", "citizenship": "Kenyan",
        "phone": "0711111111", "email": email, "gender": "Male"})
    osid = q("SELECT s.id FROM students s JOIN users u ON u.id = s.user_id WHERE u.email = ?", (email,))[0][0]
    other_req = visa_request_for(other, {"student_id": osid})
    for variant in (sms(code=code), sms(code=code).lower(), sms(code=code, time="4:24 PM", amount="Ksh1,500.00")):
        submit(other, other_req, variant)
        assert payments_of(other_req) == []
        assert "already been submitted" in flashes(other)
    assert len(q("SELECT id FROM visa_payments WHERE mpesa_transaction_code = ?", (code,))) == 1


def test_resubmission_while_pending_replaces_own_submission(client, student):
    req = visa_request_for(client, student)
    submit(client, req, sms())
    code2 = new_code()
    submit(client, req, sms(code=code2))
    ps = payments_of(req)
    assert len(ps) == 1 and ps[0]["mpesa_transaction_code"] == code2 and ps[0]["payment_status"] == "PAYMENT_PENDING"


def test_live_preview_uses_same_parser(client, student):
    req = visa_request_for(client, student)
    r = client.post(f"/student-visa/payment/{req}/parse", data={"mpesa_message": EXAMPLE})
    d = r.get_json()
    assert d["detected"]["transaction_code"] == "UIR1N8OV43" and d["detected"]["amount"] == "1,500.00"
    assert d["detected"]["time"] == "16.24 PM" and d["detected"]["date"] == "30/9/26"
    assert not [f for f in d["flags"] if f["severity"] == "error"]
    assert payments_of(req) == []                                           # preview stores nothing
