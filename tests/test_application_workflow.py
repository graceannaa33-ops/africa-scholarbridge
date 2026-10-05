"""Start Application workflow with Visa Verification as the FINAL step.

Create account -> personal -> education -> funding -> financial ->
preferences -> statement -> documents -> (bank) -> review -> VISA
  YES: upload visa -> verified -> submit -> completed
  NO : USA Student Visa Assistance form -> documents -> declaration ->
       payment -> admin confirms payment -> application completed
"""
import io
import re
import secrets
import uuid

import pytest
from werkzeug.security import generate_password_hash

import app as app_module
from conftest import (APPLICANT, VISA_FORM_ANSWERS, choose_yes, complete_steps_before_visa, complete_visa_form,
                      finish_visa_form,
                      get_application, make_pdf, upload, upload_visa_support_doc, valid_case)
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


def new_student(name=APPLICANT["full_name"]):
    """A student who has only registered + filled the personal step."""
    c = app_module.app.test_client()
    email = f"wf-{uuid.uuid4().hex[:10]}@example.com"
    c.post("/register", data={"email": email, "password": "Passw0rd!", "confirm_password": "Passw0rd!",
                              "full_name": name, "country": "Kenya", "citizenship": "Kenyan", "phone": "0712345678"})
    c.get("/application/start")
    c.post("/application/step/personal", data={
        "full_name": name, "date_of_birth": APPLICANT["date_of_birth"], "country": "Kenya", "citizenship": "Kenyan",
        "phone": "0712345678", "email": email, "gender": "Female"})
    sid = q("SELECT s.id FROM students s JOIN users u ON u.id = s.user_id WHERE u.email = ?", (email,))[0][0]
    return c, {"email": email, "student_id": sid}


def visa_admin_client():
    c = app_module.app.test_client()
    uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'visa_admin')",
                  (f"va-{secrets.token_hex(4)}@example.org",))
    execute("INSERT INTO visa_admins (user_id, full_name) VALUES (?, 'Visa Admin')", (uid,))
    with c.session_transaction() as s:
        s["visa_admin_user_id"] = uid
    return c


def main_admin_client():
    c = app_module.app.test_client()
    uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'admin')",
                  (f"adm-{secrets.token_hex(4)}@example.org", generate_password_hash("x", method="pbkdf2:sha256:1")))
    execute("INSERT INTO admins (user_id, full_name) VALUES (?, 'Main Admin')", (uid,))
    with c.session_transaction() as s:
        s["role"] = "admin"
        s["user_id"] = uid
    return c


def pay_manually(client, request_id):
    """Existing manual M-PESA path (Paystack not configured): SMS + Visa Admin verify."""
    sms, _ = mpesa_sms()
    client.post(f"/student-visa/payment/{request_id}/submit", data={"mpesa_message": sms})
    pay = q("SELECT id FROM visa_payments WHERE request_id = ? ORDER BY id DESC", (request_id,))[0][0]
    visa_admin_client().post(f"/visa-admin/payment-proof/{pay}/review", data={"action": "verify", "confirm_received": "yes"})


def flashes(client):
    with client.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


def request_of(student):
    return q("SELECT * FROM visa_requests WHERE student_id = ? ORDER BY id DESC LIMIT 1",
             (student["student_id"],))[0]


def mpesa_sms(code=None, amount="Ksh 1500"):
    code = code or ("U" + secrets.token_hex(4).upper() + "9")[:10]
    return (f"{code} Confirmed. {amount} sent to JOYCE BAARIU 0181785792 on 30/9/26 at 16.24 PM. "
            f"New M-PESA balance is Ksh1,854.61. Transaction cost, Ksh0.00. Amount you can transact within "
            f"the day is 499,830.00. See all your balances now https://saf.cx/iqIzU"), code


# ---------------------------------------------------------------------
# Visa Verification is the LAST step
# ---------------------------------------------------------------------
def test_progress_indicator_shows_visa_verification_last(client, student):
    html = client.get("/application/step/personal").get_data(as_text=True)
    steps = re.findall(r"(\d+)\. ([A-Za-z ]+?)(?: <span|\s*<span class=\"text-muted\">&rarr;|\s*</li>)",
                       html.split('id="applicationSteps"')[1].split("</ol>")[0])
    labels = [label.strip() for _, label in steps]
    assert labels[0] == "Personal Information" and labels[-1] == "Visa Verification"
    assert labels.index("Review") == len(labels) - 2
    assert "Final step" in html
    assert app_module.APPLICATION_STEPS[-1] == "visa" and app_module.APPLICATION_STEPS[-2] == "review"


def test_visa_not_asked_before_earlier_steps_are_done():
    c, st = new_student()
    r = c.get("/application/step/visa")
    assert r.status_code == 302 and "/application/step/visa" not in r.headers["Location"]
    assert "Visa Verification is the final step" in flashes(c)
    # crafted POSTs can't answer the visa question early either
    c.post("/application/step/visa", data={"visa_choice": "no"})
    c.post("/application/step/visa", data={"visa_choice": "yes"})
    row = get_application(st["student_id"])
    assert row["visa_step_status"] == "NOT_STARTED" and row["visa_status"] in (None, "NOT_STARTED", "")
    assert q("SELECT COUNT(*) FROM visa_requests WHERE student_id = ?", (st["student_id"],))[0][0] == 0
    # ...and nothing can be submitted
    r = c.post("/application/submit")
    assert r.status_code == 302 and get_application(st["student_id"])["status"] == "Draft"


def test_review_leads_to_visa_question_not_submission():
    c, st = new_student()
    r = complete_steps_before_visa(c)
    assert r.headers["Location"].endswith("/application/step/visa")
    assert get_application(st["student_id"])["status"] == "Draft"
    html = c.get("/application/step/visa").get_data(as_text=True)
    assert "Do you currently have a valid visa for your intended study/travel country?" in html
    assert "Yes, I already have a visa" in html and "No, I do not have a visa" in html
    assert "Final Step — Visa Verification" in html


def test_review_requires_payment_information_step_when_needed(client, student, monkeypatch):
    execute("UPDATE funding_applications SET bank_step_status = 'ACTION_REQUIRED', current_step = 9 "
            "WHERE student_id = ?", (student["student_id"],))
    r = client.post("/application/step/review", data={})
    assert r.headers["Location"].endswith("/application/step/bank")


# ---------------------------------------------------------------------
# Branch 1: applicant HAS a visa
# ---------------------------------------------------------------------
def test_has_visa_branch_to_completed_application(client, student):
    before = dict(get_application(student["student_id"]))
    choose_yes(client)
    html = client.get("/application/step/visa").get_data(as_text=True)
    assert "Upload Your Visa" in html and "Upload Visa Document" in html
    form, data = valid_case("pdf")
    r = upload(client, form, data, "visa.pdf")
    assert r.headers["Location"].endswith("/application/step/visa")
    page = client.get("/application/step/visa").get_data(as_text=True)
    assert "Visa uploaded successfully" in page
    r = client.post("/application/step/visa", data={"action": "continue"})
    assert r.headers["Location"].endswith("/application/submit")
    r = client.post("/application/submit")
    row = get_application(student["student_id"])
    assert row["status"] != "Draft" and row["reference_number"]
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "Application Completed Successfully" in page and row["reference_number"] in page
    assert "Submitted" in page
    # previously entered information kept
    for f in ("full_name", "institution", "course", "personal_statement", "preferences", "estimated_financial_need"):
        assert row[f] == before[f], f
    assert q("SELECT COUNT(*) FROM visa_requests WHERE student_id = ?", (student["student_id"],))[0][0] == 0


# ---------------------------------------------------------------------
# Branch 2: applicant does NOT have a visa
# ---------------------------------------------------------------------
def test_no_visa_branch_form_documents_payment_declaration_completion(client, student):
    """NO visa: form -> documents -> PAYMENT -> additional info -> declaration -> completed."""
    before = dict(get_application(student["student_id"]))
    r = client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    assert f"/student-visa/application/{vr['id']}" in r.headers["Location"]
    assert vr["form_first"] == 1 and vr["annual_application_id"] == before["id"]
    assert vr["service_price"] == app_module._visa_service_fee(get_db())        # the configured fee

    page = " ".join(client.get(r.headers["Location"], follow_redirects=True).get_data(as_text=True).split())
    assert "USA Student Visa Assistance" in page
    assert "We are not the U.S. government, U.S. Embassy, USCIS" in page
    assert "Applicant Personal Information" in page
    assert APPLICANT["full_name"] in page                                       # pre-filled

    # payment is NOT available before the form + required documents
    r = client.get(f"/student-visa/payment/{vr['id']}")
    assert r.status_code == 302 and f"/student-visa/application/{vr['id']}" in r.headers["Location"]
    sms, code = mpesa_sms()
    client.post(f"/student-visa/payment/{vr['id']}/submit", data={"mpesa_message": sms})
    assert q("SELECT COUNT(*) FROM visa_payments WHERE request_id = ?", (vr["id"],))[0][0] == 0
    assert client.post(f"/student-visa/payment/{vr['id']}/parse", data={"mpesa_message": sms}).status_code == 409
    docs_page = client.get(f"/student-visa/application/{vr['id']}/step/documents").get_data(as_text=True)
    assert 'id="paymentNotYet"' in docs_page and 'id="documentsComplete"' not in docs_page

    # sections 1-9 + documents -> the payment appears right after the upload
    complete_visa_form(client, vr["id"], sign=False)
    vr = request_of(student)
    assert vr["passport_number"] == "AK1234567" and vr["visa_category"] == "Student Visa"
    assert vr["destination_country"] == "United States of America" and vr["form_submitted_at"] is None
    docs_page = " ".join(client.get(f"/student-visa/application/{vr['id']}/step/documents").get_data(as_text=True).split())
    assert "All required documents uploaded successfully." in docs_page and "✓ Documents uploaded successfully" in docs_page
    assert "Next step: Visa Assistance Payment" in docs_page
    # additional info + declaration are locked until payment is verified
    for step in ("additional", "declaration"):
        assert client.get(f"/student-visa/application/{vr['id']}/step/{step}").headers["Location"].endswith("/step/documents")
    r = client.post(f"/student-visa/application/{vr['id']}/submit",
                    data={"declaration_name": APPLICANT["full_name"], "declaration_confirmed": "yes"})
    assert r.headers["Location"].endswith("/step/documents") and request_of(student)["form_submitted_at"] is None

    page = client.get(f"/student-visa/payment/{vr['id']}").get_data(as_text=True)
    assert "Visa Assistance Service Fee" in page and "KSh 1,500" in page
    client.post(f"/student-visa/payment/{vr['id']}/submit", data={"mpesa_message": sms, "payment_phone": "0712345678"})
    [pay] = q("SELECT * FROM visa_payments WHERE request_id = ?", (vr["id"],))
    assert pay["payment_status"] == "PAYMENT_PENDING"

    admin = visa_admin_client()
    admin.post(f"/visa-admin/payment-proof/{pay['id']}/review", data={"action": "verify", "confirm_received": "yes"})
    vr = request_of(student)
    assert vr["payment_status"] == "paid" and vr["payment_verified"] == 1
    row = get_application(student["student_id"])
    assert row["status"] == "Draft" and row["visa_payment_status"] == "PAID"     # not complete before the declaration
    assert row["visa_step_status"] == "ACTION_REQUIRED"
    r = client.get(f"/student-visa/payment/{vr['id']}")
    assert r.headers["Location"].endswith(f"/student-visa/application/{vr['id']}/step/additional")

    # 11 + 12 -> application completed
    r = finish_visa_form(client, vr["id"])
    vr = request_of(student)
    row = get_application(student["student_id"])
    assert vr["form_submitted_at"] and vr["declaration_confirmed"] == 1 and vr["declaration_name"] == APPLICANT["full_name"]
    assert vr["application_status"] == "preparation"                           # visa review started
    assert row["status"] != "Draft" and row["reference_number"].startswith("ASB-")
    assert row["visa_payment_status"] == "PAID" and row["visa_step_status"] == "COMPLETE"
    for f in ("full_name", "institution", "course", "personal_statement", "preferences"):
        assert row[f] == before[f], f
    assert r.headers["Location"].endswith(f"/application/confirmation/{row['id']}")
    page = " ".join(client.get(r.headers["Location"]).get_data(as_text=True).split())
    assert "Application Completed Successfully" in page
    assert row["reference_number"] in page and "Submitted" in page
    assert "Your Africa ScholarBridge application and visa assistance submission have been received successfully." in page
    assert "Payment does not guarantee visa approval" in page
    assert vr["request_number"] in page
    # the form is locked once submitted
    assert client.get(f"/student-visa/application/{vr['id']}/step/personal").status_code == 302


def test_declaration_rules(client, student):
    """Declaration re-checks required sections and required documents.
    Optional documents selected No are valid and do not block submission."""
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    complete_visa_form(client, vr["id"], sign=False)
    pay_manually(client, vr["id"])
    client.post(f"/student-visa/application/{vr['id']}/step/additional", data=VISA_FORM_ANSWERS["additional"])
    client.post(f"/student-visa/application/{vr['id']}/step/passport", data={"passport_status": ""})
    r = client.post(f"/student-visa/application/{vr['id']}/submit",
                    data={"declaration_name": APPLICANT["full_name"], "declaration_confirmed": "yes"})
    assert r.headers["Location"].endswith("/step/passport")

    client.post(f"/student-visa/application/{vr['id']}/step/passport", data=VISA_FORM_ANSWERS["passport"])
    doc = q("SELECT id FROM visa_documents WHERE request_id = ? AND document_type = 'National ID'",
             (vr["id"],))[0][0]
    client.post(f"/student-visa/application/{vr['id']}/documents/{doc}/remove")
    r = client.post(f"/student-visa/application/{vr['id']}/submit",
                    data={"declaration_name": APPLICANT["full_name"], "declaration_confirmed": "yes"})
    assert r.headers["Location"].endswith("/step/documents")
    assert "National ID" in flashes(client)

    upload_visa_support_doc(client, vr["id"], "National ID", make_pdf(["PHOTO"]), "national-id.pdf")
    r = client.post(f"/student-visa/application/{vr['id']}/submit",
                    data={"declaration_name": APPLICANT["full_name"]})
    assert r.headers["Location"].endswith("/step/declaration")
    r = client.post(f"/student-visa/application/{vr['id']}/submit",
                    data={"declaration_name": "Someone Else", "declaration_confirmed": "yes"})
    assert r.headers["Location"].endswith("/step/declaration")
    assert request_of(student)["form_submitted_at"] is None

def test_conditional_answers_and_choice_validation(client, student):
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    url = f"/student-visa/application/{vr['id']}/step/legal"
    r = client.post(url, data={"overstayed": "Yes", "refused_entry": "No", "visa_refused": "No"})
    assert r.headers["Location"].endswith("/step/legal") and "explain" in flashes(client)
    client.post(url, data={"overstayed": "Yes", "overstayed_explanation": "Stayed 3 days late in 2019",
                           "refused_entry": "No", "refused_entry_explanation": "ignored", "visa_refused": "No"})
    vr = request_of(student)
    assert vr["overstayed_explanation"] == "Stayed 3 days late in 2019" and vr["refused_entry_explanation"] is None
    client.post(f"/student-visa/application/{vr['id']}/step/personal",
                data=dict(VISA_FORM_ANSWERS["personal"], gender="<script>", date_of_birth="31/31/2003"))
    vr = request_of(student)
    assert vr["gender"] is None and vr["date_of_birth"] is None


def test_switching_from_yes_to_no_keeps_application(client, student):
    choose_yes(client)
    r = client.post("/application/step/visa", data={"visa_choice": "no"})
    assert "/student-visa/application/" in r.headers["Location"]
    row = get_application(student["student_id"])
    assert row["visa_status"] == "NEEDS_ASSISTANCE" and row["full_name"] == APPLICANT["full_name"]
    assert q("SELECT COUNT(*) FROM visa_requests WHERE student_id = ?", (student["student_id"],))[0][0] == 1


# ---------------------------------------------------------------------
# Supporting-document upload security
# ---------------------------------------------------------------------
def test_supporting_document_upload_security(client, student):
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    bad = [("Valid Passport", b"MZ\x90\x00 not a pdf", "passport.pdf"),          # wrong signature
           ("Valid Passport", b"%PDF-1.4 fine", "passport.exe"),                  # wrong extension
           ("Valid Passport", b"", "empty.pdf"),                                  # empty
           ("Not A Real Type", make_pdf(["x"]), "x.pdf")]                         # unknown checklist item
    for doc_type, data, name in bad:
        upload_visa_support_doc(client, vr["id"], doc_type, data, name)
    assert q("SELECT COUNT(*) FROM visa_documents WHERE request_id = ? AND stored_file IS NOT NULL",
             (vr["id"],))[0][0] == 0
    too_big = b"%PDF-" + b"0" * (app_module.MAX_SUPPORT_DOC_SIZE_BYTES + 10)
    r = upload_visa_support_doc(client, vr["id"], "Valid Passport", too_big, "big.pdf")
    assert r.status_code in (302, 413)
    assert q("SELECT COUNT(*) FROM visa_documents WHERE request_id = ? AND stored_file IS NOT NULL",
             (vr["id"],))[0][0] == 0

    upload_visa_support_doc(client, vr["id"], "Valid Passport", make_pdf(["PASSPORT"]), "../../etc/My Passport.pdf")
    doc = q("SELECT * FROM visa_documents WHERE request_id = ? AND stored_file IS NOT NULL", (vr["id"],))[0]
    assert re.fullmatch(r"[0-9a-f]{32}\.pdf", doc["stored_file"])                 # random server-side name
    assert doc["original_name"] == "etc_My_Passport.pdf"                          # sanitised display name

    # owner can view, with no-store / nosniff headers
    r = client.get(f"/student-visa/documents/file/{doc['id']}")
    assert r.status_code == 200 and r.data.startswith(b"%PDF")
    assert "no-store" in r.headers["Cache-Control"] and r.headers["X-Content-Type-Options"] == "nosniff"
    # another student: 404; anonymous: login; Main Admin: no access; Visa Admin: yes
    other, _ = new_student("Other Student")
    assert other.get(f"/student-visa/documents/file/{doc['id']}").status_code == 404
    anon = app_module.app.test_client()
    assert "/login" in anon.get(f"/student-visa/documents/file/{doc['id']}").headers["Location"]
    assert "/visa-admin/login" in anon.get(f"/visa-admin/visa-application-documents/{doc['id']}").headers["Location"]
    main = main_admin_client()
    assert main.get(f"/visa-admin/visa-application-documents/{doc['id']}").status_code == 302
    assert visa_admin_client().get(f"/visa-admin/visa-application-documents/{doc['id']}").status_code == 200
    # other students can't upload into this request
    r = other.post(f"/student-visa/application/{vr['id']}/documents/upload",
                   data={"document_type": "National ID", "document": (io.BytesIO(make_pdf(["x"])), "x.pdf")},
                   content_type="multipart/form-data")
    assert r.status_code == 404


def test_documents_locked_after_declaration(client, student):
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    complete_visa_form(client, vr["id"], sign=False)
    pay_manually(client, vr["id"])
    finish_visa_form(client, vr["id"])
    assert request_of(student)["form_submitted_at"]
    n = q("SELECT COUNT(*) FROM visa_documents WHERE request_id = ? AND stored_file IS NOT NULL", (vr["id"],))[0][0]
    upload_visa_support_doc(client, vr["id"], "National ID")
    assert q("SELECT COUNT(*) FROM visa_documents WHERE request_id = ? AND stored_file IS NOT NULL",
             (vr["id"],))[0][0] == n


# ---------------------------------------------------------------------
# Admin visibility: Main Admin summary only, Visa Admin everything
# ---------------------------------------------------------------------
def test_main_admin_sees_summary_but_no_sensitive_data(client, student):
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    complete_visa_form(client, vr["id"], sign=False)
    sms, code = mpesa_sms()
    client.post(f"/student-visa/payment/{vr['id']}/submit", data={"mpesa_message": sms})
    app_id = get_application(student["student_id"])["id"]
    main = main_admin_client()
    detail = main.get(f"/admin/applications/{app_id}").get_data(as_text=True)
    summary = detail.split('id="visaSummary"')[1].split("</table>")[0]
    for expected in ("Awaiting Payment", ">No<", "Not submitted", "Pending Verification", "KSh 1,500", code):
        assert expected in summary, expected
    for secret in ("AK1234567", "34567890", "Confirmed. Ksh 1500 sent to", "/visa-admin/visa-application-documents",
                   "/student-visa/documents/file", "/visa-admin/payment-proof", "Ngong Rd", "USD 5,000"):
        assert secret not in detail, secret
    listing = main.get("/admin/applications").get_data(as_text=True)
    assert "Visa Status" in listing and "Awaiting Payment" in listing
    assert "AK1234567" not in listing

    # Visa Admin has the full form and the documents
    va_page = visa_admin_client().get(f"/visa-admin/requests/{vr['id']}").get_data(as_text=True)
    assert "Visa Application Form" in va_page and "AK1234567" in va_page and "34567890" in va_page
    assert "/visa-admin/visa-application-documents/" in va_page


def test_visa_status_labels_follow_the_workflow(client, student):
    import visa as visa_lib
    db = get_db()

    def label():
        app_row = db.execute("SELECT * FROM funding_applications WHERE student_id = ?", (student["student_id"],)).fetchone()
        v = db.execute("SELECT * FROM visa_requests WHERE student_id = ?", (student["student_id"],)).fetchone()
        p = db.execute("SELECT * FROM visa_payments WHERE request_id = ? ORDER BY id DESC",
                       (v["id"],)).fetchone() if v else None
        return visa_lib.visa_display_status(app_row, v, p)
    assert label() == "Not Required Yet"
    client.post("/application/step/visa", data={"visa_choice": "no"})
    assert label() == "Visa Assistance Required"
    vr = request_of(student)
    complete_visa_form(client, vr["id"], sign=False)
    assert label() == "Visa Assistance Required"           # documents in, not paid yet
    sms, _ = mpesa_sms()
    client.post(f"/student-visa/payment/{vr['id']}/submit", data={"mpesa_message": sms})
    assert label() == "Awaiting Payment"
    pay = q("SELECT id FROM visa_payments WHERE request_id = ?", (vr["id"],))[0][0]
    visa_admin_client().post(f"/visa-admin/payment-proof/{pay}/review", data={"action": "verify", "confirm_received": "yes"})
    assert label() == "Paid"                                # paid, declaration still to sign
    finish_visa_form(client, vr["id"])
    assert label() == "Under Review"
    db.close()


# ---------------------------------------------------------------------
# Existing applicants keep working
# ---------------------------------------------------------------------
def test_older_draft_with_visa_done_early_must_still_finish_review():
    """A draft that answered the visa question when it was step 5: the visa
    result is kept, but submission waits until review is done."""
    c, st = new_student()
    execute("""UPDATE funding_applications SET visa_status = 'NEEDS_ASSISTANCE', visa_step_status = 'COMPLETE',
               current_step = 6 WHERE student_id = ?""", (st["student_id"],))
    page = c.get("/application/step/visa")
    assert page.status_code == 200                       # visa result still visible
    r = c.post("/application/step/visa", data={"action": "continue"})
    assert "/application/step/" in r.headers["Location"] and not r.headers["Location"].endswith("/visa")
    assert c.post("/application/submit").status_code == 302
    assert get_application(st["student_id"])["status"] == "Draft"
    complete_steps_before_visa(c)
    r = c.post("/application/step/visa", data={"action": "continue"})
    assert r.headers["Location"].endswith("/application/submit")
    c.post("/application/submit")
    assert get_application(st["student_id"])["status"] != "Draft"


def test_older_pay_first_request_keeps_its_order(client, student):
    """A funding-application visa request created before this change that
    already has a payment submission keeps the pay-first order."""
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    execute("UPDATE visa_requests SET form_first = 0 WHERE id = ?", (vr["id"],))
    sms, _ = mpesa_sms()
    client.post(f"/student-visa/payment/{vr['id']}/submit", data={"mpesa_message": sms})
    assert q("SELECT COUNT(*) FROM visa_payments WHERE request_id = ?", (vr["id"],))[0][0] == 1
    client.post("/application/step/visa", data={"visa_choice": "no"})       # no-op, not re-ordered
    assert request_of(student)["form_first"] == 0


def test_standalone_visa_service_still_pays_first():
    c, st = new_student()
    r = c.post("/student-visa/start")
    vr = request_of(st)
    assert vr["form_first"] == 0 and vr["annual_application_id"] is None
    assert r.headers["Location"].endswith(f"/student-visa/payment/{vr['id']}")
    assert c.get(f"/student-visa/payment/{vr['id']}").status_code == 200
    r = c.get(f"/student-visa/application/{vr['id']}")
    assert r.headers["Location"].endswith(f"/student-visa/payment/{vr['id']}")   # form locked until paid


# ---------------------------------------------------------------------
# Required documents are a configurable policy (VISA_REQUIRED_DOCUMENTS)
# ---------------------------------------------------------------------


def test_required_documents_policy(client, student, monkeypatch, policy, needed):
    """The public visa checklist always requires the photo and identity document.
    Legacy environment settings cannot make those two identity requirements optional."""
    monkeypatch.setenv("VISA_REQUIRED_DOCUMENTS", "none")
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    for step, answers in VISA_FORM_ANSWERS.items():
        client.post(f"/student-visa/application/{vr['id']}/step/{step}", data=answers)
    page = client.get(f"/student-visa/application/{vr['id']}/step/documents").get_data(as_text=True)
    box = " ".join(page.split('id="requiredDocuments"')[1].split("</p>")[0].split())
    assert "Passport-size Photograph" in box and "National ID" in box and "Required:" in box
    assert client.get(f"/student-visa/payment/{vr['id']}").status_code == 302

def test_no_passport_requires_national_id_when_identity_is_required(client, student, monkeypatch):
    """When the applicant has no passport, National ID remains required."""
    monkeypatch.delenv("VISA_REQUIRED_DOCUMENTS", raising=False)
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    for step, answers in VISA_FORM_ANSWERS.items():
        client.post(f"/student-visa/application/{vr['id']}/step/{step}", data=answers)
    client.post(f"/student-visa/application/{vr['id']}/step/passport",
                data={"passport_status": "I do not currently have a passport"})
    page = client.get(f"/student-visa/application/{vr['id']}/step/documents").get_data(as_text=True)
    assert "National ID" in page.split('id="requiredDocuments"')[1].split("</p>")[0]
    assert client.get(f"/student-visa/payment/{vr['id']}").status_code == 302
    upload_visa_support_doc(client, vr["id"], "National ID", make_pdf(["ID"]), "id.pdf")
    assert client.get(f"/student-visa/payment/{vr['id']}").status_code == 302

"""Start Application workflow with Visa Verification as the FINAL step.

Create account -> personal -> education -> funding -> financial ->
preferences -> statement -> documents -> (bank) -> review -> VISA
  YES: upload visa -> verified -> submit -> completed
  NO : USA Student Visa Assistance form -> documents -> declaration ->
       payment -> admin confirms payment -> application completed
"""
import io
import re
import secrets
import uuid

import pytest
from werkzeug.security import generate_password_hash

import app as app_module
from conftest import (APPLICANT, VISA_FORM_ANSWERS, choose_yes, complete_steps_before_visa, complete_visa_form,
                      finish_visa_form,
                      get_application, make_pdf, upload, upload_visa_support_doc, valid_case)
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


def new_student(name=APPLICANT["full_name"]):
    """A student who has only registered + filled the personal step."""
    c = app_module.app.test_client()
    email = f"wf-{uuid.uuid4().hex[:10]}@example.com"
    c.post("/register", data={"email": email, "password": "Passw0rd!", "confirm_password": "Passw0rd!",
                              "full_name": name, "country": "Kenya", "citizenship": "Kenyan", "phone": "0712345678"})
    c.get("/application/start")
    c.post("/application/step/personal", data={
        "full_name": name, "date_of_birth": APPLICANT["date_of_birth"], "country": "Kenya", "citizenship": "Kenyan",
        "phone": "0712345678", "email": email, "gender": "Female"})
    sid = q("SELECT s.id FROM students s JOIN users u ON u.id = s.user_id WHERE u.email = ?", (email,))[0][0]
    return c, {"email": email, "student_id": sid}


def visa_admin_client():
    c = app_module.app.test_client()
    uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'visa_admin')",
                  (f"va-{secrets.token_hex(4)}@example.org",))
    execute("INSERT INTO visa_admins (user_id, full_name) VALUES (?, 'Visa Admin')", (uid,))
    with c.session_transaction() as s:
        s["visa_admin_user_id"] = uid
    return c


def main_admin_client():
    c = app_module.app.test_client()
    uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'admin')",
                  (f"adm-{secrets.token_hex(4)}@example.org", generate_password_hash("x", method="pbkdf2:sha256:1")))
    execute("INSERT INTO admins (user_id, full_name) VALUES (?, 'Main Admin')", (uid,))
    with c.session_transaction() as s:
        s["role"] = "admin"
        s["user_id"] = uid
    return c


def pay_manually(client, request_id):
    """Existing manual M-PESA path (Paystack not configured): SMS + Visa Admin verify."""
    sms, _ = mpesa_sms()
    client.post(f"/student-visa/payment/{request_id}/submit", data={"mpesa_message": sms})
    pay = q("SELECT id FROM visa_payments WHERE request_id = ? ORDER BY id DESC", (request_id,))[0][0]
    visa_admin_client().post(f"/visa-admin/payment-proof/{pay}/review", data={"action": "verify", "confirm_received": "yes"})


def flashes(client):
    with client.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


def request_of(student):
    return q("SELECT * FROM visa_requests WHERE student_id = ? ORDER BY id DESC LIMIT 1",
             (student["student_id"],))[0]


def mpesa_sms(code=None, amount="Ksh 1500"):
    code = code or ("U" + secrets.token_hex(4).upper() + "9")[:10]
    return (f"{code} Confirmed. {amount} sent to JOYCE BAARIU 0181785792 on 30/9/26 at 16.24 PM. "
            f"New M-PESA balance is Ksh1,854.61. Transaction cost, Ksh0.00. Amount you can transact within "
            f"the day is 499,830.00. See all your balances now https://saf.cx/iqIzU"), code


# ---------------------------------------------------------------------
# Visa Verification is the LAST step
# ---------------------------------------------------------------------
def test_progress_indicator_shows_visa_verification_last(client, student):
    html = client.get("/application/step/personal").get_data(as_text=True)
    steps = re.findall(r"(\d+)\. ([A-Za-z ]+?)(?: <span|\s*<span class=\"text-muted\">&rarr;|\s*</li>)",
                       html.split('id="applicationSteps"')[1].split("</ol>")[0])
    labels = [label.strip() for _, label in steps]
    assert labels[0] == "Personal Information" and labels[-1] == "Visa Verification"
    assert labels.index("Review") == len(labels) - 2
    assert "Final step" in html
    assert app_module.APPLICATION_STEPS[-1] == "visa" and app_module.APPLICATION_STEPS[-2] == "review"


def test_visa_not_asked_before_earlier_steps_are_done():
    c, st = new_student()
    r = c.get("/application/step/visa")
    assert r.status_code == 302 and "/application/step/visa" not in r.headers["Location"]
    assert "Visa Verification is the final step" in flashes(c)
    # crafted POSTs can't answer the visa question early either
    c.post("/application/step/visa", data={"visa_choice": "no"})
    c.post("/application/step/visa", data={"visa_choice": "yes"})
    row = get_application(st["student_id"])
    assert row["visa_step_status"] == "NOT_STARTED" and row["visa_status"] in (None, "NOT_STARTED", "")
    assert q("SELECT COUNT(*) FROM visa_requests WHERE student_id = ?", (st["student_id"],))[0][0] == 0
    # ...and nothing can be submitted
    r = c.post("/application/submit")
    assert r.status_code == 302 and get_application(st["student_id"])["status"] == "Draft"


def test_review_leads_to_visa_question_not_submission():
    c, st = new_student()
    r = complete_steps_before_visa(c)
    assert r.headers["Location"].endswith("/application/step/visa")
    assert get_application(st["student_id"])["status"] == "Draft"
    html = c.get("/application/step/visa").get_data(as_text=True)
    assert "Do you currently have a valid visa for your intended study/travel country?" in html
    assert "Yes, I already have a visa" in html and "No, I do not have a visa" in html
    assert "Final Step — Visa Verification" in html


def test_review_requires_payment_information_step_when_needed(client, student, monkeypatch):
    execute("UPDATE funding_applications SET bank_step_status = 'ACTION_REQUIRED', current_step = 9 "
            "WHERE student_id = ?", (student["student_id"],))
    r = client.post("/application/step/review", data={})
    assert r.headers["Location"].endswith("/application/step/bank")


# ---------------------------------------------------------------------
# Branch 1: applicant HAS a visa
# ---------------------------------------------------------------------
def test_has_visa_branch_to_completed_application(client, student):
    before = dict(get_application(student["student_id"]))
    choose_yes(client)
    html = client.get("/application/step/visa").get_data(as_text=True)
    assert "Upload Your Visa" in html and "Upload Visa Document" in html
    form, data = valid_case("pdf")
    r = upload(client, form, data, "visa.pdf")
    assert r.headers["Location"].endswith("/application/step/visa")
    page = client.get("/application/step/visa").get_data(as_text=True)
    assert "Visa uploaded successfully" in page
    r = client.post("/application/step/visa", data={"action": "continue"})
    assert r.headers["Location"].endswith("/application/submit")
    r = client.post("/application/submit")
    row = get_application(student["student_id"])
    assert row["status"] != "Draft" and row["reference_number"]
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "Application Completed Successfully" in page and row["reference_number"] in page
    assert "Submitted" in page
    # previously entered information kept
    for f in ("full_name", "institution", "course", "personal_statement", "preferences", "estimated_financial_need"):
        assert row[f] == before[f], f
    assert q("SELECT COUNT(*) FROM visa_requests WHERE student_id = ?", (student["student_id"],))[0][0] == 0


# ---------------------------------------------------------------------
# Branch 2: applicant does NOT have a visa
# ---------------------------------------------------------------------
def test_no_visa_branch_form_documents_payment_declaration_completion(client, student):
    """NO visa: form -> documents -> PAYMENT -> additional info -> declaration -> completed."""
    before = dict(get_application(student["student_id"]))
    r = client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    assert f"/student-visa/application/{vr['id']}" in r.headers["Location"]
    assert vr["form_first"] == 1 and vr["annual_application_id"] == before["id"]
    assert vr["service_price"] == app_module._visa_service_fee(get_db())        # the configured fee

    page = " ".join(client.get(r.headers["Location"], follow_redirects=True).get_data(as_text=True).split())
    assert "USA Student Visa Assistance" in page
    assert "We are not the U.S. government, U.S. Embassy, USCIS" in page
    assert "Applicant Personal Information" in page
    assert APPLICANT["full_name"] in page                                       # pre-filled

    # payment is NOT available before the form + required documents
    r = client.get(f"/student-visa/payment/{vr['id']}")
    assert r.status_code == 302 and f"/student-visa/application/{vr['id']}" in r.headers["Location"]
    sms, code = mpesa_sms()
    client.post(f"/student-visa/payment/{vr['id']}/submit", data={"mpesa_message": sms})
    assert q("SELECT COUNT(*) FROM visa_payments WHERE request_id = ?", (vr["id"],))[0][0] == 0
    assert client.post(f"/student-visa/payment/{vr['id']}/parse", data={"mpesa_message": sms}).status_code == 409
    docs_page = client.get(f"/student-visa/application/{vr['id']}/step/documents").get_data(as_text=True)
    assert 'id="paymentNotYet"' in docs_page and 'id="documentsComplete"' not in docs_page

    # sections 1-9 + documents -> the payment appears right after the upload
    complete_visa_form(client, vr["id"], sign=False)
    vr = request_of(student)
    assert vr["passport_number"] == "AK1234567" and vr["visa_category"] == "Student Visa"
    assert vr["destination_country"] == "United States of America" and vr["form_submitted_at"] is None
    docs_page = " ".join(client.get(f"/student-visa/application/{vr['id']}/step/documents").get_data(as_text=True).split())
    assert "All required documents uploaded successfully." in docs_page and "✓ Documents uploaded successfully" in docs_page
    assert "Next step: Visa Assistance Payment" in docs_page
    # additional info + declaration are locked until payment is verified
    for step in ("additional", "declaration"):
        assert client.get(f"/student-visa/application/{vr['id']}/step/{step}").headers["Location"].endswith("/step/documents")
    r = client.post(f"/student-visa/application/{vr['id']}/submit",
                    data={"declaration_name": APPLICANT["full_name"], "declaration_confirmed": "yes"})
    assert r.headers["Location"].endswith("/step/documents") and request_of(student)["form_submitted_at"] is None

    page = client.get(f"/student-visa/payment/{vr['id']}").get_data(as_text=True)
    assert "Visa Assistance Service Fee" in page and "KSh 1,500" in page
    client.post(f"/student-visa/payment/{vr['id']}/submit", data={"mpesa_message": sms, "payment_phone": "0712345678"})
    [pay] = q("SELECT * FROM visa_payments WHERE request_id = ?", (vr["id"],))
    assert pay["payment_status"] == "PAYMENT_PENDING"

    admin = visa_admin_client()
    admin.post(f"/visa-admin/payment-proof/{pay['id']}/review", data={"action": "verify", "confirm_received": "yes"})
    vr = request_of(student)
    assert vr["payment_status"] == "paid" and vr["payment_verified"] == 1
    row = get_application(student["student_id"])
    assert row["status"] == "Draft" and row["visa_payment_status"] == "PAID"     # not complete before the declaration
    assert row["visa_step_status"] == "ACTION_REQUIRED"
    r = client.get(f"/student-visa/payment/{vr['id']}")
    assert r.headers["Location"].endswith(f"/student-visa/application/{vr['id']}/step/additional")

    # 11 + 12 -> application completed
    r = finish_visa_form(client, vr["id"])
    vr = request_of(student)
    row = get_application(student["student_id"])
    assert vr["form_submitted_at"] and vr["declaration_confirmed"] == 1 and vr["declaration_name"] == APPLICANT["full_name"]
    assert vr["application_status"] == "preparation"                           # visa review started
    assert row["status"] != "Draft" and row["reference_number"].startswith("ASB-")
    assert row["visa_payment_status"] == "PAID" and row["visa_step_status"] == "COMPLETE"
    for f in ("full_name", "institution", "course", "personal_statement", "preferences"):
        assert row[f] == before[f], f
    assert r.headers["Location"].endswith(f"/application/confirmation/{row['id']}")
    page = " ".join(client.get(r.headers["Location"]).get_data(as_text=True).split())
    assert "Application Completed Successfully" in page
    assert row["reference_number"] in page and "Submitted" in page
    assert "Your Africa ScholarBridge application and visa assistance submission have been received successfully." in page
    assert "Payment does not guarantee visa approval" in page
    assert vr["request_number"] in page
    # the form is locked once submitted
    assert client.get(f"/student-visa/application/{vr['id']}/step/personal").status_code == 302


def test_declaration_rules(client, student):
    """Declaration re-checks required sections and required documents.
    Optional documents selected No are valid and do not block submission."""
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    complete_visa_form(client, vr["id"], sign=False)
    pay_manually(client, vr["id"])
    client.post(f"/student-visa/application/{vr['id']}/step/additional", data=VISA_FORM_ANSWERS["additional"])
    client.post(f"/student-visa/application/{vr['id']}/step/passport", data={"passport_status": ""})
    r = client.post(f"/student-visa/application/{vr['id']}/submit",
                    data={"declaration_name": APPLICANT["full_name"], "declaration_confirmed": "yes"})
    assert r.headers["Location"].endswith("/step/passport")

    client.post(f"/student-visa/application/{vr['id']}/step/passport", data=VISA_FORM_ANSWERS["passport"])
    doc = q("SELECT id FROM visa_documents WHERE request_id = ? AND document_type = 'Passport-size Photograph'",
             (vr["id"],))[0][0]
    client.post(f"/student-visa/application/{vr['id']}/documents/{doc}/remove")
    r = client.post(f"/student-visa/application/{vr['id']}/submit",
                    data={"declaration_name": APPLICANT["full_name"], "declaration_confirmed": "yes"})
    assert r.headers["Location"].endswith("/step/documents")
    assert "Passport-size Photograph" in flashes(client)

    upload_visa_support_doc(client, vr["id"], "Passport-size Photograph", make_pdf(["PHOTO"]), "photo.pdf")
    r = client.post(f"/student-visa/application/{vr['id']}/submit",
                    data={"declaration_name": APPLICANT["full_name"]})
    assert r.headers["Location"].endswith("/step/declaration")
    r = client.post(f"/student-visa/application/{vr['id']}/submit",
                    data={"declaration_name": "Someone Else", "declaration_confirmed": "yes"})
    assert r.headers["Location"].endswith("/step/declaration")
    assert request_of(student)["form_submitted_at"] is None

def test_conditional_answers_and_choice_validation(client, student):
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    url = f"/student-visa/application/{vr['id']}/step/legal"
    r = client.post(url, data={"overstayed": "Yes", "refused_entry": "No", "visa_refused": "No"})
    assert r.headers["Location"].endswith("/step/legal") and "explain" in flashes(client)
    client.post(url, data={"overstayed": "Yes", "overstayed_explanation": "Stayed 3 days late in 2019",
                           "refused_entry": "No", "refused_entry_explanation": "ignored", "visa_refused": "No"})
    vr = request_of(student)
    assert vr["overstayed_explanation"] == "Stayed 3 days late in 2019" and vr["refused_entry_explanation"] is None
    client.post(f"/student-visa/application/{vr['id']}/step/personal",
                data=dict(VISA_FORM_ANSWERS["personal"], gender="<script>", date_of_birth="31/31/2003"))
    vr = request_of(student)
    assert vr["gender"] is None and vr["date_of_birth"] is None


def test_switching_from_yes_to_no_keeps_application(client, student):
    choose_yes(client)
    r = client.post("/application/step/visa", data={"visa_choice": "no"})
    assert "/student-visa/application/" in r.headers["Location"]
    row = get_application(student["student_id"])
    assert row["visa_status"] == "NEEDS_ASSISTANCE" and row["full_name"] == APPLICANT["full_name"]
    assert q("SELECT COUNT(*) FROM visa_requests WHERE student_id = ?", (student["student_id"],))[0][0] == 1


# ---------------------------------------------------------------------
# Supporting-document upload security
# ---------------------------------------------------------------------
def test_supporting_document_upload_security(client, student):
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    bad = [("Valid Passport", b"MZ\x90\x00 not a pdf", "passport.pdf"),          # wrong signature
           ("Valid Passport", b"%PDF-1.4 fine", "passport.exe"),                  # wrong extension
           ("Valid Passport", b"", "empty.pdf"),                                  # empty
           ("Not A Real Type", make_pdf(["x"]), "x.pdf")]                         # unknown checklist item
    for doc_type, data, name in bad:
        upload_visa_support_doc(client, vr["id"], doc_type, data, name)
    assert q("SELECT COUNT(*) FROM visa_documents WHERE request_id = ? AND stored_file IS NOT NULL",
             (vr["id"],))[0][0] == 0
    too_big = b"%PDF-" + b"0" * (app_module.MAX_SUPPORT_DOC_SIZE_BYTES + 10)
    r = upload_visa_support_doc(client, vr["id"], "Valid Passport", too_big, "big.pdf")
    assert r.status_code in (302, 413)
    assert q("SELECT COUNT(*) FROM visa_documents WHERE request_id = ? AND stored_file IS NOT NULL",
             (vr["id"],))[0][0] == 0

    upload_visa_support_doc(client, vr["id"], "Valid Passport", make_pdf(["PASSPORT"]), "../../etc/My Passport.pdf")
    doc = q("SELECT * FROM visa_documents WHERE request_id = ? AND stored_file IS NOT NULL", (vr["id"],))[0]
    assert re.fullmatch(r"[0-9a-f]{32}\.pdf", doc["stored_file"])                 # random server-side name
    assert doc["original_name"] == "etc_My_Passport.pdf"                          # sanitised display name

    # owner can view, with no-store / nosniff headers
    r = client.get(f"/student-visa/documents/file/{doc['id']}")
    assert r.status_code == 200 and r.data.startswith(b"%PDF")
    assert "no-store" in r.headers["Cache-Control"] and r.headers["X-Content-Type-Options"] == "nosniff"
    # another student: 404; anonymous: login; Main Admin: no access; Visa Admin: yes
    other, _ = new_student("Other Student")
    assert other.get(f"/student-visa/documents/file/{doc['id']}").status_code == 404
    anon = app_module.app.test_client()
    assert "/login" in anon.get(f"/student-visa/documents/file/{doc['id']}").headers["Location"]
    assert "/visa-admin/login" in anon.get(f"/visa-admin/visa-application-documents/{doc['id']}").headers["Location"]
    main = main_admin_client()
    assert main.get(f"/visa-admin/visa-application-documents/{doc['id']}").status_code == 302
    assert visa_admin_client().get(f"/visa-admin/visa-application-documents/{doc['id']}").status_code == 200
    # other students can't upload into this request
    r = other.post(f"/student-visa/application/{vr['id']}/documents/upload",
                   data={"document_type": "National ID", "document": (io.BytesIO(make_pdf(["x"])), "x.pdf")},
                   content_type="multipart/form-data")
    assert r.status_code == 404


def test_documents_locked_after_declaration(client, student):
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    complete_visa_form(client, vr["id"], sign=False)
    pay_manually(client, vr["id"])
    finish_visa_form(client, vr["id"])
    assert request_of(student)["form_submitted_at"]
    n = q("SELECT COUNT(*) FROM visa_documents WHERE request_id = ? AND stored_file IS NOT NULL", (vr["id"],))[0][0]
    upload_visa_support_doc(client, vr["id"], "National ID")
    assert q("SELECT COUNT(*) FROM visa_documents WHERE request_id = ? AND stored_file IS NOT NULL",
             (vr["id"],))[0][0] == n


# ---------------------------------------------------------------------
# Admin visibility: Main Admin summary only, Visa Admin everything
# ---------------------------------------------------------------------
def test_main_admin_sees_summary_but_no_sensitive_data(client, student):
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    complete_visa_form(client, vr["id"], sign=False)
    sms, code = mpesa_sms()
    client.post(f"/student-visa/payment/{vr['id']}/submit", data={"mpesa_message": sms})
    app_id = get_application(student["student_id"])["id"]
    main = main_admin_client()
    detail = main.get(f"/admin/applications/{app_id}").get_data(as_text=True)
    summary = detail.split('id="visaSummary"')[1].split("</table>")[0]
    for expected in ("Awaiting Payment", ">No<", "Not submitted", "Pending Verification", "KSh 1,500", code):
        assert expected in summary, expected
    for secret in ("AK1234567", "34567890", "Confirmed. Ksh 1500 sent to", "/visa-admin/visa-application-documents",
                   "/student-visa/documents/file", "/visa-admin/payment-proof", "Ngong Rd", "USD 5,000"):
        assert secret not in detail, secret
    listing = main.get("/admin/applications").get_data(as_text=True)
    assert "Visa Status" in listing and "Awaiting Payment" in listing
    assert "AK1234567" not in listing

    # Visa Admin has the full form and the documents
    va_page = visa_admin_client().get(f"/visa-admin/requests/{vr['id']}").get_data(as_text=True)
    assert "Visa Application Form" in va_page and "AK1234567" in va_page and "34567890" in va_page
    assert "/visa-admin/visa-application-documents/" in va_page


def test_visa_status_labels_follow_the_workflow(client, student):
    import visa as visa_lib
    db = get_db()

    def label():
        app_row = db.execute("SELECT * FROM funding_applications WHERE student_id = ?", (student["student_id"],)).fetchone()
        v = db.execute("SELECT * FROM visa_requests WHERE student_id = ?", (student["student_id"],)).fetchone()
        p = db.execute("SELECT * FROM visa_payments WHERE request_id = ? ORDER BY id DESC",
                       (v["id"],)).fetchone() if v else None
        return visa_lib.visa_display_status(app_row, v, p)
    assert label() == "Not Required Yet"
    client.post("/application/step/visa", data={"visa_choice": "no"})
    assert label() == "Visa Assistance Required"
    vr = request_of(student)
    complete_visa_form(client, vr["id"], sign=False)
    assert label() == "Visa Assistance Required"           # documents in, not paid yet
    sms, _ = mpesa_sms()
    client.post(f"/student-visa/payment/{vr['id']}/submit", data={"mpesa_message": sms})
    assert label() == "Awaiting Payment"
    pay = q("SELECT id FROM visa_payments WHERE request_id = ?", (vr["id"],))[0][0]
    visa_admin_client().post(f"/visa-admin/payment-proof/{pay}/review", data={"action": "verify", "confirm_received": "yes"})
    assert label() == "Paid"                                # paid, declaration still to sign
    finish_visa_form(client, vr["id"])
    assert label() == "Under Review"
    db.close()


# ---------------------------------------------------------------------
# Existing applicants keep working
# ---------------------------------------------------------------------
def test_older_draft_with_visa_done_early_must_still_finish_review():
    """A draft that answered the visa question when it was step 5: the visa
    result is kept, but submission waits until review is done."""
    c, st = new_student()
    execute("""UPDATE funding_applications SET visa_status = 'NEEDS_ASSISTANCE', visa_step_status = 'COMPLETE',
               current_step = 6 WHERE student_id = ?""", (st["student_id"],))
    page = c.get("/application/step/visa")
    assert page.status_code == 200                       # visa result still visible
    r = c.post("/application/step/visa", data={"action": "continue"})
    assert "/application/step/" in r.headers["Location"] and not r.headers["Location"].endswith("/visa")
    assert c.post("/application/submit").status_code == 302
    assert get_application(st["student_id"])["status"] == "Draft"
    complete_steps_before_visa(c)
    r = c.post("/application/step/visa", data={"action": "continue"})
    assert r.headers["Location"].endswith("/application/submit")
    c.post("/application/submit")
    assert get_application(st["student_id"])["status"] != "Draft"


def test_older_pay_first_request_keeps_its_order(client, student):
    """A funding-application visa request created before this change that
    already has a payment submission keeps the pay-first order."""
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    execute("UPDATE visa_requests SET form_first = 0 WHERE id = ?", (vr["id"],))
    sms, _ = mpesa_sms()
    client.post(f"/student-visa/payment/{vr['id']}/submit", data={"mpesa_message": sms})
    assert q("SELECT COUNT(*) FROM visa_payments WHERE request_id = ?", (vr["id"],))[0][0] == 1
    client.post("/application/step/visa", data={"visa_choice": "no"})       # no-op, not re-ordered
    assert request_of(student)["form_first"] == 0


def test_standalone_visa_service_still_pays_first():
    c, st = new_student()
    r = c.post("/student-visa/start")
    vr = request_of(st)
    assert vr["form_first"] == 0 and vr["annual_application_id"] is None
    assert r.headers["Location"].endswith(f"/student-visa/payment/{vr['id']}")
    assert c.get(f"/student-visa/payment/{vr['id']}").status_code == 200
    r = c.get(f"/student-visa/application/{vr['id']}")
    assert r.headers["Location"].endswith(f"/student-visa/payment/{vr['id']}")   # form locked until paid


# ---------------------------------------------------------------------
# Required documents are a configurable policy (VISA_REQUIRED_DOCUMENTS)
# ---------------------------------------------------------------------


@pytest.mark.parametrize("policy,needed", [
    (None, ["Passport-size Photograph", "Valid Passport"]),          # default
    ("photo,identity", ["Passport-size Photograph", "Valid Passport"]),
    ("photo", ["Passport-size Photograph"]),
    ("identity", ["Valid Passport"]),
    ("none", []),
    (" Photo ; bogus ", ["Passport-size Photograph"]),
])
def test_required_documents_policy(client, student, monkeypatch):
    """Legacy policy settings cannot make the public required documents optional."""
    monkeypatch.setenv("VISA_REQUIRED_DOCUMENTS", "none")
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    for step, answers in VISA_FORM_ANSWERS.items():
        client.post(f"/student-visa/application/{vr['id']}/step/{step}", data=answers)
    page = client.get(f"/student-visa/application/{vr['id']}/step/documents").get_data(as_text=True)
    box = " ".join(page.split('id="requiredDocuments"')[1].split("</p>")[0].split())
    assert "Passport-size Photograph" in box and "National ID" in box and "Required:" in box
    assert client.get(f"/student-visa/payment/{vr['id']}").status_code == 302

def test_no_passport_requires_national_id_when_identity_is_required(client, student, monkeypatch):
    monkeypatch.setenv("VISA_REQUIRED_DOCUMENTS", "identity")
    client.post("/application/step/visa", data={"visa_choice": "no"})
    vr = request_of(student)
    for step, answers in VISA_FORM_ANSWERS.items():
        client.post(f"/student-visa/application/{vr['id']}/step/{step}", data=answers)
    client.post(f"/student-visa/application/{vr['id']}/step/passport",
                data={"passport_status": "I do not currently have a passport"})
    page = client.get(f"/student-visa/application/{vr['id']}/step/documents").get_data(as_text=True)
    assert "National ID" in page.split('id="requiredDocuments"')[1].split("</p>")[0]
    assert client.get(f"/student-visa/payment/{vr['id']}").status_code == 302          # not before the ID
    upload_visa_support_doc(client, vr["id"], "National ID", make_pdf(["ID"]), "id.pdf")
    assert client.get(f"/student-visa/payment/{vr['id']}").status_code == 200          # right after it
