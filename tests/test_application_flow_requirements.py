"""End-to-end behaviour of the annual funding application flow:

  Funding documents   - all nine optional; blank stays NULL, No / Yes saved,
                        Yes without an upload blocks, nothing is lost.
  Financial (Step 4)  - guidance shown, fields persist, amount validated.
  Statement (Step 6)  - guidance shown, the example is never saved.
  Bank / disbursement - required fields enforced at save, confirm, Review and
                        submission; mobile money never replaces the bank
                        account; Edit re-opens a pre-filled form; the full
                        account number never reaches a page.
  Visa                - still the final step; required photo / National ID,
                        optional Yes/No rules, Purpose of Travel and the
                        payment gate unchanged; no duplicate requests.

All test data is fictional.
"""
import io
import os
import uuid

import pytest

import app as app_module
import database
from conftest import (APPLICANT, PNG_BYTES, TEST_BANK_DETAILS, VISA_FORM_ANSWERS, choose_yes,
                      complete_bank_step, count_applications, get_application, upload,
                      visa_request_for, visa_requests_for)
from database import get_db

FUNDING_DOCUMENTS = [
    "Academic Transcripts", "Certificates", "Admission Letter", "Recommendation Letter",
    "Personal Statement", "CV", "Passport / Identity Document", "Proof of Financial Need",
    "Provider-Specific Document",
]
PDF_BYTES = b"%PDF-1.4\n% fictional test document\n"
FULL_ACCOUNT_NUMBER = TEST_BANK_DETAILS["account_number"]


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------
def q(sql, args=()):
    db = get_db()
    rows = db.execute(sql, args).fetchall()
    db.close()
    return rows


def execute(sql, args=()):
    db = get_db()
    db.execute(sql, args)
    db.commit()
    db.close()


def flashes(client):
    with client.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


def html(client, url):
    return " ".join(client.get(url).get_data(as_text=True).split())


def docs(application_id):
    return {r["document_type"]: dict(r) for r in
            q("SELECT * FROM documents WHERE application_id = ? ORDER BY id", (application_id,))}


def bank_row(application_id):
    rows = q("SELECT * FROM student_bank_details WHERE application_id = ?", (application_id,))
    return dict(rows[0]) if rows else None


@pytest.fixture()
def drafter(client):
    """A new applicant who has filled Personal, Education, Funding
    Information, Financial, Preferences and Statement - standing at the
    Documents step with every document still unanswered."""
    email = f"drafter-{uuid.uuid4().hex[:10]}@example.com"
    client.post("/register", data={"email": email, "password": "Passw0rd!", "confirm_password": "Passw0rd!",
                                   "full_name": "Alex Testperson", "country": "Kenya"})
    client.get("/application/start")
    client.post("/application/step/personal", data={"full_name": "Alex Testperson", "date_of_birth": "2000-01-01",
                                                    "country": "Kenya", "phone": "+254 700 000 001", "email": email})
    client.post("/application/step/education", data={"institution": "Example University",
                                                     "education_level": "Undergraduate",
                                                     "course": "Bachelor of Information Technology"})
    client.post("/application/step/funding_need", data={"funding_type_needed": "Full tuition"})
    client.post("/application/step/financial", data={"requested_amount_ksh": "75,000"})
    client.post("/application/step/preferences", data={"preferences": ["Scholarship"]})
    client.post("/application/step/statement", data={"personal_statement": "A fictional statement."})
    sid = q("SELECT s.id FROM students s JOIN users u ON u.id = s.user_id WHERE u.email = ?", (email,))[0][0]
    application = get_application(sid)
    return {"email": email, "student_id": sid, "application_id": application["id"]}


def doc_field(row, suffix):
    return f"document_{row['id']}_{suffix}"


# =====================================================================
# 1-2. FUNDING DOCUMENTS - all nine optional
# =====================================================================
def test_all_nine_funding_documents_exist_and_are_optional(client, drafter):
    rows = docs(drafter["application_id"])
    assert list(rows) == FUNDING_DOCUMENTS
    assert all(r["is_required"] == 0 for r in rows.values())
    assert all(r["availability"] is None for r in rows.values())
    assert all(required is False for _, required in app_module.DOCUMENT_CHECKLIST)


def test_documents_page_shows_optional_for_every_document_and_the_explanation(client, drafter):
    page = html(client, "/application/step/documents")
    assert ("All documents in this section are optional. You may upload any documents you have, or select No "
            "if a document is not available. You can continue without uploading any document.") in page
    assert page.count('<span class="badge bg-secondary">Optional</span>') == 9
    assert ">Required<" not in page
    assert page.count("Do you have this document?") == 9
    assert page.count("Yes, I have it") == 9 and page.count("No, I don't have it") == 9
    # Nothing is pre-selected and no upload is forced while unanswered.
    assert " checked" not in page.split('id="fundingDocumentChecklist"')[1].split("</form>")[0]
    assert "funding-doc-file\" accept=\".pdf,.jpg,.jpeg,.png\" required" not in page


def test_unanswered_documents_continue_and_stay_null(client, drafter):
    r = client.post("/application/step/documents", data={})
    assert r.status_code == 302 and r.headers["Location"].endswith("/application/step/bank")
    rows = docs(drafter["application_id"])
    assert all(row["availability"] is None for row in rows.values())     # never turned into "No"
    assert all(row["status"] == "Missing" for row in rows.values())
    # ...and still NULL after coming back / refreshing the page.
    client.get("/application/step/documents")
    client.get("/application/step/documents")
    assert all(row["availability"] is None for row in docs(drafter["application_id"]).values())


def test_no_is_saved_as_no_and_shown_again(client, drafter):
    rows = docs(drafter["application_id"])
    cv = rows["CV"]
    r = client.post("/application/step/documents", data={doc_field(cv, "availability"): "no"})
    assert r.headers["Location"].endswith("/application/step/bank")
    saved = docs(drafter["application_id"])
    assert saved["CV"]["availability"] == "No"
    assert all(saved[t]["availability"] is None for t in FUNDING_DOCUMENTS if t != "CV")
    page = client.get("/application/step/documents").get_data(as_text=True)
    assert f'id="doc_{cv["id"]}_no" data-upload-target="upload_{cv["id"]}" checked' in page


@pytest.mark.parametrize("doc_type", FUNDING_DOCUMENTS)
def test_yes_without_upload_blocks_for_every_document(client, drafter, doc_type):
    row = docs(drafter["application_id"])[doc_type]
    r = client.post("/application/step/documents", data={doc_field(row, "availability"): "yes"})
    assert r.headers["Location"].endswith("/application/step/documents")
    assert f"{doc_type} — you selected Yes, so please upload the document." in flashes(client)
    assert docs(drafter["application_id"])[doc_type]["availability"] == "Yes"
    assert get_application(drafter["student_id"])["current_step"] <= app_module.APPLICATION_STEPS.index("documents") + 1


def test_yes_without_upload_keeps_the_other_answers_from_the_same_submit(client, drafter):
    rows = docs(drafter["application_id"])
    data = {doc_field(rows["CV"], "availability"): "yes",                       # blocks
            doc_field(rows["Certificates"], "availability"): "no",
            doc_field(rows["Admission Letter"], "availability"): "yes",
            doc_field(rows["Admission Letter"], "file"): (io.BytesIO(PDF_BYTES), "admission.pdf")}
    r = client.post("/application/step/documents", data=data, content_type="multipart/form-data")
    assert r.headers["Location"].endswith("/application/step/documents")
    saved = docs(drafter["application_id"])
    assert saved["Certificates"]["availability"] == "No"
    assert saved["Admission Letter"]["status"] == "Uploaded"
    assert saved["CV"]["availability"] == "Yes" and saved["CV"]["status"] == "Missing"
    # The page then explains exactly what is still needed.
    assert "CV — you selected Yes, so please upload the document." in html(client, "/application/step/documents")


def test_yes_with_upload_continues_and_stores_the_file_safely(client, drafter):
    row = docs(drafter["application_id"])["Academic Transcripts"]
    r = client.post("/application/step/documents",
                    data={doc_field(row, "availability"): "yes",
                          doc_field(row, "file"): (io.BytesIO(PDF_BYTES), "../../my transcripts.pdf")},
                    content_type="multipart/form-data")
    assert r.headers["Location"].endswith("/application/step/bank")
    saved = docs(drafter["application_id"])["Academic Transcripts"]
    assert saved["availability"] == "Yes" and saved["status"] == "Uploaded"
    stored = saved["file_path"]
    assert stored.startswith("funding_documents/") and "transcripts" not in stored   # randomised name
    path = os.path.join(app_module.FUNDING_DOCS_DIR, os.path.basename(stored))
    assert os.path.isfile(path)
    assert "static" not in os.path.relpath(path, os.path.dirname(app_module.__file__)).split(os.sep)


def test_fake_file_is_rejected_but_other_answers_are_kept(client, drafter):
    rows = docs(drafter["application_id"])
    r = client.post("/application/step/documents",
                    data={doc_field(rows["CV"], "availability"): "yes",
                          doc_field(rows["CV"], "file"): (io.BytesIO(b"MZ not a pdf"), "cv.pdf"),
                          doc_field(rows["Certificates"], "availability"): "no"},
                    content_type="multipart/form-data")
    assert r.headers["Location"].endswith("/application/step/documents")
    saved = docs(drafter["application_id"])
    assert saved["CV"]["status"] == "Missing" and saved["CV"]["file_path"] is None
    assert saved["Certificates"]["availability"] == "No"


def test_existing_uploads_stay_intact_on_later_submits(client, drafter):
    row = docs(drafter["application_id"])["Recommendation Letter"]
    client.post("/application/step/documents",
                data={doc_field(row, "availability"): "yes",
                      doc_field(row, "file"): (io.BytesIO(PDF_BYTES), "letter.pdf")},
                content_type="multipart/form-data")
    before = docs(drafter["application_id"])["Recommendation Letter"]
    # Coming back and continuing with "Yes" but no new file keeps the upload.
    r = client.post("/application/step/documents", data={doc_field(row, "availability"): "yes"})
    assert r.headers["Location"].endswith("/application/step/bank")
    after = docs(drafter["application_id"])["Recommendation Letter"]
    assert after["file_path"] == before["file_path"] and after["status"] == "Uploaded"
    assert os.path.isfile(os.path.join(app_module.FUNDING_DOCS_DIR, os.path.basename(after["file_path"])))
    # Answering "No" later never deletes the stored file either.
    client.post("/application/step/documents", data={doc_field(row, "availability"): "no"})
    after_no = docs(drafter["application_id"])["Recommendation Letter"]
    assert after_no["file_path"] == before["file_path"]
    assert os.path.isfile(os.path.join(app_module.FUNDING_DOCS_DIR, os.path.basename(after_no["file_path"])))


def test_old_required_document_records_are_migrated_to_optional(client, drafter):
    rows = docs(drafter["application_id"])
    legacy = ["Academic Transcripts", "Certificates", "Recommendation Letter", "Personal Statement", "CV"]
    for name in legacy:
        execute("UPDATE documents SET is_required = 1 WHERE id = ?", (rows[name]["id"],))
    execute("UPDATE documents SET status = 'Uploaded', availability = 'Yes', file_path = 'funding_documents/legacy.pdf' "
            "WHERE id = ?", (rows["CV"]["id"],))

    database.init_db()   # the startup migration (runs on every deploy)

    migrated = docs(drafter["application_id"])
    assert all(migrated[n]["is_required"] == 0 for n in FUNDING_DOCUMENTS)
    assert migrated["Academic Transcripts"]["availability"] is None          # unanswered stays NULL
    assert migrated["CV"]["file_path"] == "funding_documents/legacy.pdf"    # upload kept
    assert migrated["CV"]["availability"] == "Yes" and migrated["CV"]["status"] == "Uploaded"
    r = client.post("/application/step/documents", data={doc_field(rows["CV"], "availability"): "yes"})
    assert r.headers["Location"].endswith("/application/step/bank")


def test_step_visit_also_migrates_an_old_required_row(client, drafter):
    rows = docs(drafter["application_id"])
    execute("UPDATE documents SET is_required = 1 WHERE id = ?", (rows["Certificates"]["id"],))
    page = html(client, "/application/step/documents")
    assert ">Required<" not in page
    assert docs(drafter["application_id"])["Certificates"]["is_required"] == 0


def test_standalone_documents_page_has_no_required_state(client, drafter):
    page = html(client, "/documents")
    assert ">Required<" not in page
    assert "Please choose Yes or No" not in page
    assert page.count("Not answered (optional)") == 9


def test_no_backend_rule_requires_a_funding_document():
    blank = [{"document_type": n, "availability": None, "status": "Missing"} for n in FUNDING_DOCUMENTS]
    no = [{"document_type": n, "availability": "No", "status": "Missing"} for n in FUNDING_DOCUMENTS]
    assert app_module._missing_funding_documents(blank) == []
    assert app_module._missing_funding_documents(no) == []
    yes = [{"document_type": "CV", "availability": "Yes", "status": "Missing"}]
    assert app_module._missing_funding_documents(yes) == ["CV — you selected Yes, so please upload the document."]


# =====================================================================
# 3-4. STEP 4 - FINANCIAL INFORMATION + REQUESTED AMOUNT
# =====================================================================
def test_financial_step_title_examples_and_guidance(client, drafter):
    assert app_module.APPLICATION_STEPS.index("financial") == 3
    assert app_module.APPLICATION_STEP_TITLES["financial"] == "Financial Information"
    page = html(client, "/application/step/financial")
    assert "Step 4 — Financial Information<" in page
    for text in (
        "e.g. My household earns about KSh 25,000 per month, and my education costs are about KSh 100,000 per year.",
        "Example: Explain your household income, major expenses, and how education costs affect your family.",
        "e.g. Parent/guardian, part-time work, sibling, employer, or personal savings",
        "Example: Parent/guardian, sibling, employer, part-time work, or personal savings.",
        "e.g. KSh 75,000 per academic year",
        "Example: KSh 75,000 for tuition, accommodation, books, and other education expenses.",
        "e.g. I received KSh 20,000 from a school bursary.",
        "Example: Name of funding received and amount. If none, write",
        "Requested amount only — final funding decisions are subject to review.",
        'name="requested_amount_ksh"',
    ):
        assert text in page, text


def test_financial_fields_save_and_persist(client, drafter):
    values = {"requested_amount_ksh": "KES 80000",
              "household_situation": "Fictional: household earns KSh 30,000 per month.",
              "source_of_support": "Fictional sibling support",
              "estimated_financial_need": "KSh 90,000 per academic year",
              "funding_already_received": "None"}
    r = client.post("/application/step/financial", data=values)
    assert r.headers["Location"].endswith("/application/step/preferences")
    app_row = get_application(drafter["student_id"])
    assert app_row["requested_amount_ksh"] == 80000
    for f in ("household_situation", "source_of_support", "estimated_financial_need", "funding_already_received"):
        assert app_row[f] == values[f]
    # Leaving, refreshing and coming back shows the saved values again.
    client.get("/application/step/personal")
    page = client.get("/application/step/financial").get_data(as_text=True)
    assert 'value="80,000"' in page
    for f in ("household_situation", "source_of_support", "estimated_financial_need", "funding_already_received"):
        assert values[f] in page


def test_blank_financial_fields_do_not_save_the_examples(client, drafter):
    client.post("/application/step/financial", data={"requested_amount_ksh": "75000", "household_situation": "",
                                                     "source_of_support": "", "estimated_financial_need": "",
                                                     "funding_already_received": ""})
    app_row = get_application(drafter["student_id"])
    for f in ("household_situation", "source_of_support", "estimated_financial_need", "funding_already_received"):
        assert not app_row[f]


@pytest.mark.parametrize("raw,saved", [("75,000", 75000), ("75000", 75000), ("KSh 75,000", 75000),
                                       ("KES 75000", 75000)])
def test_requested_amount_accepts_valid_formats(client, drafter, raw, saved):
    r = client.post("/application/step/financial", data={"requested_amount_ksh": raw})
    assert r.headers["Location"].endswith("/application/step/preferences")
    assert get_application(drafter["student_id"])["requested_amount_ksh"] == saved


@pytest.mark.parametrize("raw", ["", "0", "-5000", "75000.50", "12.5", "abc", "seventy thousand", "KSh"])
def test_requested_amount_rejects_invalid_values(client, drafter, raw):
    execute("UPDATE funding_applications SET requested_amount_ksh = 60000 WHERE id = ?", (drafter["application_id"],))
    r = client.post("/application/step/financial", data={"requested_amount_ksh": raw,
                                                         "household_situation": "should not be saved"})
    assert r.headers["Location"].endswith("/application/step/financial")
    app_row = get_application(drafter["student_id"])
    assert app_row["requested_amount_ksh"] == 60000                 # unchanged
    assert app_row["household_situation"] != "should not be saved"


# =====================================================================
# 5. STEP 6 - PERSONAL STATEMENT
# =====================================================================
EXAMPLE_STATEMENT_START = "My goal is to complete my degree in Information Technology"


def test_statement_step_shows_example_placeholder_and_tip(client, drafter):
    assert app_module.APPLICATION_STEPS.index("statement") == 5
    page = html(client, "/application/step/statement")
    assert "Step 6 — Personal Statement<" in page
    assert ("Tell us about your educational goals, financial need, career goals, and why funding matters "
            "to you and your community.") in page
    assert EXAMPLE_STATEMENT_START in page
    assert ('placeholder="e.g. My educational goal is to... My financial need is... My career goal is... '
            'Funding would help me..."') in page
    assert ("Tip: Explain your education goal, financial need, career goal, and how your education could "
            "benefit your community.") in page
    # The example sits before the text box and is never inside it.
    assert page.index(EXAMPLE_STATEMENT_START) < page.index('name="personal_statement"')


def test_statement_saves_persists_and_example_is_never_saved(client, drafter):
    client.post("/application/step/statement", data={"personal_statement": ""})
    assert not get_application(drafter["student_id"])["personal_statement"]
    textarea = client.get("/application/step/statement").get_data(as_text=True).split('name="personal_statement"')[1]
    assert EXAMPLE_STATEMENT_START not in textarea.split("</textarea>")[0]

    mine = "Fictional: I study nursing and want to serve rural clinics in my county."
    r = client.post("/application/step/statement", data={"personal_statement": mine})
    assert r.headers["Location"].endswith("/application/step/documents")
    assert get_application(drafter["student_id"])["personal_statement"] == mine
    client.get("/application/step/education")
    assert mine in client.get("/application/step/statement").get_data(as_text=True)


# =====================================================================
# 8. BANK / DISBURSEMENT
# =====================================================================
def to_bank(client):
    return client.post("/application/step/documents", data={})


@pytest.mark.parametrize("field", ["manual_bank_name", "account_holder_name", "account_number", "branch", "bank_code"])
def test_mobile_money_cannot_replace_a_required_bank_field(client, drafter, field):
    to_bank(client)
    data = {**TEST_BANK_DETAILS, field: "", "mobile_money_provider": "M-Pesa",
            "mobile_money_number": "+254 700 000 001"}
    client.post("/application/step/bank", data=data)
    assert bank_row(drafter["application_id"]) is None
    assert "Please fill in" in flashes(client)
    assert get_application(drafter["student_id"])["bank_step_status"] != "COMPLETE"


def test_mobile_money_only_cannot_complete_the_bank_step(client, drafter):
    to_bank(client)
    client.post("/application/step/bank", data={"action": "save_details", "country": "Kenya",
                                                 "mobile_money_provider": "M-Pesa",
                                                 "mobile_money_number": "+254 700 000 001"})
    client.post("/application/step/bank", data={"action": "confirm", "confirm_accurate": "yes"})
    assert bank_row(drafter["application_id"]) is None
    assert get_application(drafter["student_id"])["bank_step_status"] == "ACTION_REQUIRED"
    r = client.post("/application/step/review", data={})
    assert r.headers["Location"].endswith("/application/step/bank")


def test_bank_details_with_optional_mobile_money_persist(client, drafter):
    to_bank(client)
    complete_bank_step(client, swift_bic="TESTSWIFTXXX", iban="TEST-IBAN-001", routing_number="000000000",
                       mobile_money_provider="M-Pesa", mobile_money_number="+254 700 000 009")
    row = bank_row(drafter["application_id"])
    assert row["confirmed"] == 1
    assert (row["bank_name"], row["account_holder_name"], row["account_number"], row["branch"], row["bank_code"]) == (
        "Example Bank", "Alex Testperson", FULL_ACCOUNT_NUMBER, "Example Branch", "TEST-BANK-001")
    assert (row["swift_bic"], row["iban"], row["routing_number"]) == ("TESTSWIFTXXX", "TEST-IBAN-001", "000000000")
    assert row["mobile_money_provider"] == "M-Pesa"
    assert get_application(drafter["student_id"])["bank_step_status"] == "COMPLETE"
    page = html(client, "/application/step/bank")
    assert FULL_ACCOUNT_NUMBER not in page
    assert "+254 700 000 009" not in page


def test_edit_reopens_a_prefilled_form_without_the_account_number(client, drafter):
    to_bank(client)
    complete_bank_step(client)
    r = client.post("/application/step/bank", data={"action": "edit"})
    assert r.headers["Location"].endswith("/application/step/bank?edit=1")
    assert get_application(drafter["student_id"])["bank_step_status"] == "ACTION_REQUIRED"
    page = html(client, "/application/step/bank?edit=1")
    assert 'id="bankDetailsForm"' in page                              # the form, not the confirm screen
    assert 'value="Alex Testperson"' in page and 'value="Example Branch"' in page
    assert 'value="TEST-BANK-001"' in page and 'value="Example Bank"' in page
    assert FULL_ACCOUNT_NUMBER not in page                             # never echoed back
    assert "Leave blank to keep" in page

    # Change only the branch: blank account number keeps the stored one.
    client.post("/application/step/bank", data={**TEST_BANK_DETAILS, "account_number": "", "branch": "New Branch"})
    client.post("/application/step/bank", data={"action": "confirm", "confirm_accurate": "yes"})
    row = bank_row(drafter["application_id"])
    assert row["branch"] == "New Branch" and row["account_number"] == FULL_ACCOUNT_NUMBER and row["confirmed"] == 1
    assert get_application(drafter["student_id"])["bank_step_status"] == "COMPLETE"


def test_edit_url_cannot_show_the_form_for_confirmed_details(client, drafter):
    to_bank(client)
    complete_bank_step(client)
    page = html(client, "/application/step/bank?edit=1")
    assert 'id="bankDetailsForm"' not in page
    assert "Bank Account / Disbursement Information Saved" in page


def test_older_incomplete_bank_row_cannot_pass_review_or_be_confirmed(client, drafter):
    to_bank(client)
    complete_bank_step(client)
    execute("UPDATE student_bank_details SET branch = NULL, bank_code = NULL WHERE application_id = ?",
            (drafter["application_id"],))
    r = client.post("/application/step/review", data={})
    assert r.headers["Location"].endswith("/application/step/bank?edit=1")
    assert "Branch, Bank Code" in flashes(client)
    app_row = get_application(drafter["student_id"])
    assert app_row["bank_step_status"] == "ACTION_REQUIRED"
    assert bank_row(drafter["application_id"])["account_number"] == FULL_ACCOUNT_NUMBER   # row kept
    # Confirming without completing it is refused too.
    r = client.post("/application/step/bank", data={"action": "confirm", "confirm_accurate": "yes"})
    assert r.headers["Location"].endswith("/application/step/bank?edit=1")
    assert get_application(drafter["student_id"])["bank_step_status"] == "ACTION_REQUIRED"


def test_review_shows_bank_masked_and_the_requested_summary(client, drafter):
    client.post("/application/step/financial", data={"requested_amount_ksh": "75,000",
                                                     "estimated_financial_need": "KSh 75,000 per academic year"})
    to_bank(client)
    complete_bank_step(client)
    page = html(client, "/application/step/review")
    for text in ("Alex Testperson", "Example University", "Full tuition", "KSh 75,000",
                 "Requested amount only — final funding decisions are subject to review.",
                 "KSh 75,000 per academic year", "Scholarship", "A fictional statement.",
                 "✓ Complete", "Example Bank · Example Branch",
                 "Final step — next, after this review"):
        assert text in page, text
    assert "Alex Testperson · •••• •••• T001" in page
    assert FULL_ACCOUNT_NUMBER not in page


def test_review_shows_action_required_until_bank_is_done(client, drafter):
    to_bank(client)
    client.get("/application/step/bank")
    page = html(client, "/application/step/review")
    assert "⚠ Action Required" in page
    assert FULL_ACCOUNT_NUMBER not in page


def test_final_submission_is_blocked_if_bank_was_reopened_after_review(client, student):
    application = get_application(student["student_id"])
    # Stand at the end of the flow: visa assistance completed and verified.
    execute("UPDATE funding_applications SET visa_status = 'NEEDS_ASSISTANCE', visa_step_status = 'COMPLETE' "
            "WHERE id = ?", (application["id"],))
    assert client.get("/application/submit").status_code == 200

    client.post("/application/step/bank", data={"action": "edit"})
    r = client.post("/application/submit")
    assert r.headers["Location"].endswith("/application/step/bank")
    assert get_application(student["student_id"])["status"] == "Draft"

    client.post("/application/step/bank", data={"action": "confirm", "confirm_accurate": "yes"})
    r = client.post("/application/submit")
    assert "/application/confirmation/" in r.headers["Location"]
    assert get_application(student["student_id"])["status"] != "Draft"


def test_main_admin_application_pages_never_show_the_account_number(client, student):
    application = get_application(student["student_id"])
    admin = app_module.app.test_client()
    db = get_db()
    uid = db.execute("INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'admin')",
                     (f"admin-{uuid.uuid4().hex[:8]}@example.org",)).lastrowid
    db.execute("INSERT INTO admins (user_id, full_name) VALUES (?, 'Example Admin')", (uid,))
    db.commit()
    db.close()
    with admin.session_transaction() as s:
        s["role"], s["user_id"] = "admin", uid
    for url in (f"/admin/applications/{application['id']}", "/admin/applications", "/admin/disbursements"):
        r = admin.get(url)
        assert r.status_code == 200, url
        assert FULL_ACCOUNT_NUMBER not in r.get_data(as_text=True), url


# =====================================================================
# 6-7. VISA - final step, document rules, Purpose of Travel, payment gate
# =====================================================================
def test_visa_verification_is_still_the_final_step(client, drafter):
    steps = app_module.APPLICATION_STEPS
    assert steps[-1] == "visa"
    assert steps.index("documents") < steps.index("bank") < steps.index("review") < steps.index("visa")
    r = client.get("/application/step/visa")
    assert r.status_code == 302 and not r.headers["Location"].endswith("/application/step/visa")


def _visa_docs(request_id):
    return {r["document_type"]: dict(r) for r in
            q("SELECT * FROM visa_documents WHERE request_id = ? ORDER BY id", (request_id,))}


def test_visa_required_documents_are_unchanged(client, student):
    req = visa_request_for(client, student)
    vdocs = _visa_docs(req)
    assert {n for n, d in vdocs.items() if d["is_required"]} == {"Passport-size Photograph", "National ID"}
    for name in ("Valid Passport", "Bank Statement / Proof of Funds", "Flight Itinerary / Travel Reservation",
                 "Accommodation Booking", "Travel Medical Insurance", "Employment/Business/Student Proof",
                 "Invitation Letter", "Admission Letter", "Other Supporting Documents"):
        assert vdocs[name]["is_required"] == 0, name


@pytest.mark.parametrize("required_doc", ["Passport-size Photograph", "National ID"])
def test_payment_stays_locked_without_a_required_visa_document(client, student, required_doc):
    req = visa_request_for(client, student)
    assert client.get(f"/student-visa/payment/{req}").status_code == 200    # ready when complete
    execute("UPDATE visa_documents SET stored_file = NULL, status = 'Missing' WHERE request_id = ? AND document_type = ?",
            (req, required_doc))
    r = client.get(f"/student-visa/payment/{req}")
    assert r.status_code == 302 and f"/student-visa/application/{req}" in r.headers["Location"]


def test_purpose_of_travel_is_still_required_before_payment(client, student):
    req = visa_request_for(client, student)
    page = html(client, f"/student-visa/application/{req}/step/visa_info")
    assert 'placeholder="e.g. Educational purpose"' in page
    client.post(f"/student-visa/application/{req}/step/visa_info",
                data={**VISA_FORM_ANSWERS["visa_info"], "purpose_of_travel": ""})
    r = client.get(f"/student-visa/payment/{req}")
    assert r.status_code == 302 and f"/student-visa/application/{req}" in r.headers["Location"]
    client.post(f"/student-visa/application/{req}/step/visa_info", data=VISA_FORM_ANSWERS["visa_info"])
    assert client.get(f"/student-visa/payment/{req}").status_code == 200


def test_optional_visa_yes_without_upload_blocks_payment_but_keeps_other_answers(client, student):
    req = visa_request_for(client, student)
    vdocs = _visa_docs(req)
    optional = [d for d in vdocs.values() if not d["is_required"]]
    execute("UPDATE visa_documents SET availability = NULL WHERE request_id = ? AND is_required = 0", (req,))
    target = vdocs["Invitation Letter"]
    data = {f"document_{d['id']}_availability": "no" for d in optional}
    data[f"document_{target['id']}_availability"] = "yes"
    client.post(f"/student-visa/application/{req}/step/documents", data=data)
    after = _visa_docs(req)
    assert after["Invitation Letter"]["availability"] == "Yes"
    assert all(after[d["document_type"]]["availability"] == "No"
               for d in optional if d["id"] != target["id"])            # nothing else lost
    assert client.get(f"/student-visa/payment/{req}").status_code == 302

    data[f"document_{target['id']}_file"] = (io.BytesIO(PNG_BYTES), "invitation.png")
    client.post(f"/student-visa/application/{req}/step/documents", data=data, content_type="multipart/form-data")
    assert _visa_docs(req)["Invitation Letter"]["status"] == "Uploaded"
    assert client.get(f"/student-visa/payment/{req}").status_code == 200


def test_blank_optional_visa_answer_is_not_treated_as_available(client, student):
    req = visa_request_for(client, student)
    target = _visa_docs(req)["Accommodation Booking"]
    execute("UPDATE visa_documents SET availability = NULL WHERE id = ?", (target["id"],))
    client.post(f"/student-visa/application/{req}/step/documents", data={})
    assert _visa_docs(req)["Accommodation Booking"]["availability"] is None
    assert client.get(f"/student-visa/payment/{req}").status_code == 302


def test_visa_assistance_fee_is_still_1500(client, student):
    req = visa_request_for(client, student)
    db = get_db()
    assert app_module._visa_service_fee(db) == 1500.0
    db.close()
    assert q("SELECT service_price FROM visa_requests WHERE id = ?", (req,))[0][0] == 1500.0


def test_failed_visa_then_assistance_never_duplicates_records(client, student):
    choose_yes(client)
    upload(client, {"visa_type_category": "XYZ", "passport_number": "ABC123", "visa_issue_date": "",
                    "visa_expiry_date": "2030-01-01", "additional_info": ""}, PNG_BYTES, "not-a-visa.png")
    app_row = get_application(student["student_id"])
    assert app_row["visa_status"] == "NEEDS_ASSISTANCE"
    for _ in range(3):
        client.post("/application/step/visa", data={"visa_choice": "no"})
    assert count_applications(student["student_id"]) == 1
    assert len(visa_requests_for(app_row["id"])) == 1
    assert get_application(student["student_id"])["full_name"] == APPLICANT["full_name"]


# =====================================================================
# Demo "Upload" buttons removed - no document can be marked uploaded
# without a real, validated file.
# =====================================================================
def test_documents_page_cannot_mark_a_funding_document_uploaded(client, drafter):
    before = docs(drafter["application_id"])
    for row in before.values():
        r = client.post("/documents", data={"document_id": row["id"]})
        assert r.status_code == 405
    assert docs(drafter["application_id"]) == before            # nothing changed, nothing marked Yes
    assert all(r["status"] == "Missing" and r["file_path"] is None and r["availability"] is None
               for r in before.values())


def test_documents_page_has_no_demo_upload_button_and_links_to_real_upload(client, drafter):
    cv = docs(drafter["application_id"])["CV"]
    client.post("/application/step/documents", data={doc_field(cv, "availability"): "yes"})   # Yes, no file
    page = html(client, "/documents")
    assert 'method="POST"' not in page and 'name="document_id"' not in page
    assert ">Upload<" not in page
    assert "CV — you selected Yes, so please upload the document." in page
    assert 'href="/application/step/documents"' in page
    assert docs(drafter["application_id"])["CV"]["status"] == "Missing"


def test_visa_documents_page_cannot_mark_a_visa_document_uploaded(client, student):
    req = visa_request_for(client, student)
    execute("UPDATE visa_requests SET payment_status = 'paid' WHERE id = ?", (req,))   # old button only showed when paid
    before = _visa_docs(req)
    missing = [d for d in before.values() if not d["stored_file"]]
    assert missing
    for d in missing:
        assert client.post("/student-visa/documents", data={"document_id": d["id"]}).status_code == 405
    assert _visa_docs(req) == before
    page = html(client, "/student-visa/documents")
    assert 'name="document_id"' not in page and 'method="POST"' not in page
    assert f'href="/student-visa/application/{req}"' in page


def test_previously_faked_rows_are_reset_but_real_uploads_are_kept(client, drafter):
    rows = docs(drafter["application_id"])
    real = rows["Admission Letter"]
    client.post("/application/step/documents",
                data={doc_field(real, "availability"): "yes",
                      doc_field(real, "file"): (io.BytesIO(PDF_BYTES), "admission.pdf")},
                content_type="multipart/form-data")
    real_before = docs(drafter["application_id"])["Admission Letter"]
    fake = rows["CV"]
    execute("UPDATE documents SET status = 'Uploaded', availability = 'Yes', uploaded_at = CURRENT_TIMESTAMP, "
            "file_path = ? WHERE id = ?", (f"uploads/demo-{fake['id']}.pdf", fake["id"]))

    database.init_db()   # startup migration

    after = docs(drafter["application_id"])
    assert after["CV"]["status"] == "Missing" and after["CV"]["file_path"] is None
    assert after["CV"]["availability"] == "Yes"                      # the student's answer is kept
    assert after["Admission Letter"] == real_before                  # real upload untouched
    assert os.path.isfile(os.path.join(app_module.FUNDING_DOCS_DIR, os.path.basename(real_before["file_path"])))
    # The old fake no longer satisfies "Yes": a real file is now required.
    r = client.post("/application/step/documents", data={doc_field(fake, "availability"): "yes"})
    assert r.headers["Location"].endswith("/application/step/documents")


def test_previously_faked_visa_rows_are_reset_but_real_uploads_are_kept(client, student):
    req = visa_request_for(client, student)
    vdocs = _visa_docs(req)
    photo = vdocs["Passport-size Photograph"]                        # real upload from the fixture
    fake = vdocs["Travel Medical Insurance"]
    execute("UPDATE visa_documents SET status = 'Uploaded', file_path = ? WHERE id = ?",
            (f"uploads/demo-visa-{fake['id']}.pdf", fake["id"]))

    database.init_db()

    after = _visa_docs(req)
    assert after["Travel Medical Insurance"]["status"] == "Missing"
    assert after["Travel Medical Insurance"]["file_path"] is None
    assert after["Passport-size Photograph"] == photo
    assert client.get(f"/student-visa/documents/file/{photo['id']}").status_code == 200
