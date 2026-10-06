"""Requested funding amount -> Bank Account / Disbursement Information -> Review.

Flow: Personal -> Education -> Funding Information -> Financial Information &
Amount Requested -> (preferences, statement, documents) -> Bank Account /
Disbursement Information -> Review -> Visa Verification (final).
All test data is fictional.
"""
import secrets
import uuid

import pytest

import app as app_module
from conftest import (COMPLETE_EDUCATION, TEST_BANK_DETAILS, complete_bank_step, funding_documents_payload,
                      get_application)
from database import get_db


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


def flashes(client):
    with client.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


def page(client, url):
    return " ".join(client.get(url).get_data(as_text=True).split())


@pytest.fixture()
def applicant(client):
    """A new applicant who has filled Personal, Education and Funding
    Information - nothing about amount or bank yet."""
    email = f"applicant-{uuid.uuid4().hex[:10]}@example.com"
    client.post("/register", data={"email": email, "password": "Passw0rd!", "confirm_password": "Passw0rd!",
                                   "full_name": "Alex Testperson", "country": "Kenya"})
    client.get("/application/start")
    client.post("/application/step/personal", data={"full_name": "Alex Testperson", "date_of_birth": "2000-01-01",
                                                    "country": "Kenya", "phone": "+254 700 000 001", "email": email})
    client.post("/application/step/education", data=COMPLETE_EDUCATION)
    client.post("/application/step/funding_need", data={"funding_type_needed": "Full tuition"})
    sid = q("SELECT s.id FROM students s JOIN users u ON u.id = s.user_id WHERE u.email = ?", (email,))[0][0]
    return {"email": email, "student_id": sid}


def financial(client, amount):
    return client.post("/application/step/financial", data={"requested_amount_ksh": amount,
                                                            "household_situation": "", "source_of_support": "",
                                                            "estimated_financial_need": "KSh 150,000 per academic year",
                                                            "funding_already_received": ""})


def to_bank_step(client):
    for step, data in (("preferences", {"preferences": ["Scholarship"]}),
                       ("statement", {"personal_statement": "Example statement."})):
        client.post(f"/application/step/{step}", data=data)
    app_id = q("SELECT id FROM funding_applications ORDER BY id DESC LIMIT 1")[0][0]
    # Required documents uploaded; optional ones left unanswered (allowed).
    client.post("/application/step/documents", data=funding_documents_payload(app_id, optional=None),
                content_type="multipart/form-data")


def bank_row(student_id):
    rows = q("SELECT * FROM student_bank_details WHERE student_id = ?", (student_id,))
    return dict(rows[0]) if rows else None


# ---------------------------------------------------------------------
# 2 & 7. Requested amount: asked, validated, saved as whole KSh
# ---------------------------------------------------------------------
@pytest.mark.parametrize("raw,saved", [("75000", 75000), ("75,000", 75000), ("KSh 75,000", 75000),
                                       ("Ksh 75 000.00", 75000), ("KES 120000", 120000)])
def test_requested_amount_is_saved(client, applicant, raw, saved):
    r = financial(client, raw)
    assert r.headers["Location"].endswith("/application/step/preferences")
    assert get_application(applicant["student_id"])["requested_amount_ksh"] == saved


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5000", "75000.50", "1e6", "100000001"])
def test_invalid_requested_amount_is_refused_and_does_not_advance(client, applicant, raw):
    before = get_application(applicant["student_id"])["current_step"]
    r = financial(client, raw)
    assert r.headers["Location"].endswith("/application/step/financial")
    assert "amount of funding you are requesting" in flashes(client)
    row = get_application(applicant["student_id"])
    assert row["requested_amount_ksh"] is None and row["current_step"] == before


def test_financial_step_shows_amount_field_with_fictional_example(client, applicant):
    html = page(client, "/application/step/financial")
    assert "Requested Funding Amount (KSh)" in html and 'name="requested_amount_ksh"' in html
    assert 'placeholder="e.g. 75,000"' in html and "final funding decisions are subject to review" in html


# ---------------------------------------------------------------------
# 3. Bank section appears after the amount, before Review - for everyone
# ---------------------------------------------------------------------
def test_bank_step_waits_for_the_requested_amount(client, applicant):
    r = client.get("/application/step/bank")
    assert r.headers["Location"].endswith("/application/step/financial")
    assert "amount of funding you are requesting first" in flashes(client)


def test_bank_section_appears_after_amount_even_with_no_matching_opportunities(client, applicant):
    execute("UPDATE funding_opportunities SET is_open = 0")         # nothing to match: previously auto-skipped
    try:
        financial(client, "75,000")
        to_bank_step(client)
        r = client.get("/application/step/bank")
        assert r.status_code == 200
        html = " ".join(r.get_data(as_text=True).split())
        assert "Bank Account / Disbursement Information" in html
        assert ("Provide the account details that could be used for funding disbursement if your application is "
                "approved. Providing bank details does not guarantee funding approval.") in html
        assert "Requested Funding Amount: <strong>KSh 75,000</strong>" in html
        for ph in ("e.g. Example Bank", "e.g. Alex Testperson", "e.g. TEST-ACCOUNT-001", "e.g. Example Branch",
                   "e.g. TEST-BANK-001", "e.g. TESTSWIFTXXX", "e.g. TEST-IBAN-001"):
            assert f'placeholder="{ph}"' in html, ph
        assert get_application(applicant["student_id"])["bank_step_status"] == "ACTION_REQUIRED"
        assert client.post("/application/step/review", data={}).headers["Location"].endswith("/application/step/bank")
    finally:
        execute("UPDATE funding_opportunities SET is_open = 1")


def test_bank_step_title_in_progress_list(client, applicant):
    assert app_module.APPLICATION_STEP_TITLES["bank"] == "Bank Account / Disbursement Information"
    steps = app_module.APPLICATION_STEPS
    assert steps.index("financial") < steps.index("bank") < steps.index("review") < steps.index("visa") == len(steps) - 1


# ---------------------------------------------------------------------
# 5. Validation: required fields, optional SWIFT/IBAN, international formats
# ---------------------------------------------------------------------
@pytest.mark.parametrize("field,label", [("manual_bank_name", "Bank Name"), ("account_holder_name", "Account Holder Name"),
                                         ("account_number", "Account Number"), ("branch", "Branch"),
                                         ("bank_code", "Bank Code")])
def test_required_bank_fields_must_not_be_empty(client, applicant, field, label):
    financial(client, "75000")
    to_bank_step(client)
    client.post("/application/step/bank", data={**TEST_BANK_DETAILS, field: "   "})
    assert label in flashes(client)
    assert bank_row(applicant["student_id"]) is None


def test_swift_and_iban_optional_and_international_formats_accepted(client, applicant):
    financial(client, "75000")
    to_bank_step(client)
    complete_bank_step(client, account_number="TEST 0001-02/03", bank_code="TEST/BANK.001",
                       swift_bic="", iban="")
    row = bank_row(applicant["student_id"])
    assert row and row["account_number"] == "TEST 0001-02/03" and row["confirmed"] == 1
    assert get_application(applicant["student_id"])["bank_step_status"] == "COMPLETE"


# ---------------------------------------------------------------------
# 4, 6 & 8. Saved, associated with the amount, shown (masked) on Review
# ---------------------------------------------------------------------
def test_full_flow_saves_bank_details_and_review_shows_requested_amount(client, applicant, caplog):
    financial(client, "75,000")
    to_bank_step(client)
    with caplog.at_level("DEBUG"):
        client.post("/application/step/bank", data={**TEST_BANK_DETAILS, "swift_bic": "TESTSWIFTXXX",
                                                    "iban": "TEST-IBAN-001"})
        confirm = page(client, "/application/step/bank")
        assert "Confirm Bank Account / Disbursement Information" in confirm and "KSh 75,000" in confirm
        assert "TEST-ACCOUNT-001" not in confirm                          # masked on the confirmation screen
        r = client.post("/application/step/bank", data={"action": "confirm", "confirm_accurate": "yes"})
    row = bank_row(applicant["student_id"])
    app_row = get_application(applicant["student_id"])
    assert row["application_id"] == app_row["id"] and app_row["requested_amount_ksh"] == 75000
    assert (row["bank_name"], row["account_holder_name"], row["account_number"], row["branch"], row["bank_code"],
            row["swift_bic"], row["iban"], row["confirmed"]) == (
        "Example Bank", "Alex Testperson", "TEST-ACCOUNT-001", "Example Branch", "TEST-BANK-001",
        "TESTSWIFTXXX", "TEST-IBAN-001", 1)
    assert app_row["bank_step_status"] == "COMPLETE"
    assert "TEST-ACCOUNT-001" not in caplog.text                         # never logged
    assert "TEST-ACCOUNT-001" not in (r.headers.get("Location") or "")  # never in a URL

    review = page(client, "/application/step/review")
    assert "Requested Funding Amount" in review and "KSh 75,000" in review
    assert "Requested amount only — final funding decisions are subject to review." in review
    assert "approved" not in review.split("Requested Funding Amount")[1].split("</tr>")[0].lower().replace(
        "not approved", "")
    assert "Example Bank" in review and "Example Branch" in review and "Alex Testperson" in review
    assert "TEST-ACCOUNT-001" not in review                               # masked on Review
    assert "TEST-ACCOUNT-001" not in page(client, "/dashboard")
    r = client.post("/application/step/review", data={})
    assert r.headers["Location"].endswith("/application/step/visa")      # Visa Verification stays last


def test_review_requires_the_requested_amount(client, applicant):
    financial(client, "75000")
    to_bank_step(client)
    complete_bank_step(client)
    execute("UPDATE funding_applications SET requested_amount_ksh = NULL WHERE student_id = ?",
            (applicant["student_id"],))
    r = client.post("/application/step/review", data={})
    assert r.headers["Location"].endswith("/application/step/financial")


# ---------------------------------------------------------------------
# Existing applications keep working
# ---------------------------------------------------------------------
def test_older_draft_marked_not_required_is_now_asked_for_bank_details(client, applicant):
    financial(client, "75000")
    to_bank_step(client)
    execute("UPDATE funding_applications SET bank_step_status = 'NOT_REQUIRED' WHERE student_id = ?",
            (applicant["student_id"],))
    assert client.get("/application/step/bank").status_code == 200
    assert get_application(applicant["student_id"])["bank_step_status"] == "ACTION_REQUIRED"


def test_submitted_application_keeps_its_recorded_bank_status(client, applicant):
    execute("UPDATE funding_applications SET status = 'Submitted', bank_step_status = 'NOT_REQUIRED', "
            "requested_amount_ksh = NULL WHERE student_id = ?", (applicant["student_id"],))
    r = client.get("/application/step/bank")
    assert r.status_code == 302 and not r.headers["Location"].endswith("/application/step/financial")
    assert get_application(applicant["student_id"])["bank_step_status"] == "NOT_REQUIRED"


def test_main_admin_sees_requested_amount_but_not_the_account_number(client, applicant):
    financial(client, "75,000")
    to_bank_step(client)
    complete_bank_step(client)
    admin = app_module.app.test_client()
    uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'admin')",
                  (f"admin-{secrets.token_hex(4)}@example.org",))
    execute("INSERT INTO admins (user_id, full_name) VALUES (?, 'Example Admin')", (uid,))
    with admin.session_transaction() as s:
        s["role"], s["user_id"] = "admin", uid
    html = page(admin, f"/admin/applications/{get_application(applicant['student_id'])['id']}")
    assert "Requested Funding Amount" in html and "KSh 75,000" in html and "requested, not approved" in html
    assert "TEST-ACCOUNT-001" not in html


def test_amount_parser_unit():
    p = app_module.parse_requested_amount_ksh
    assert p("75,000") == p("KSh 75,000") == p("ksh. 75 000") == p("KES 75000") == p("75000.00") == 75000
    assert p("100,000,000") == 100_000_000
    for bad in (None, "", "0", "-1", "7.5", "seventy", "100000001", "75,000 USD"):
        assert p(bad) is None, bad
