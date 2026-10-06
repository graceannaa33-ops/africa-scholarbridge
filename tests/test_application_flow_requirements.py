"""End-to-end behaviour of the annual funding application flow:

  Education (Step 2)  - six required fields; Additional Academic Information optional.
  Funding need        - Not Needed / Partial / Full per category, incl. Other.
  Financial (Step 4)  - Estimated Financial Need + requested amount required;
                        everything else optional; examples never saved.
  Statement (Step 6)  - optional; the example is never saved.
  Funding documents   - all nine optional: nothing uploaded, blank or No never
                        blocks (blank stays NULL); Yes without an upload blocks.
  Bank / disbursement - required fields enforced at save, confirm, Review and
                        submission; mobile money never replaces the bank
                        account; Edit re-opens a pre-filled form; the full
                        account number never reaches a page.
  Visa                - still the final step; required photo / National ID,
                        optional Yes/No rules and the payment gate unchanged;
                        Purpose of Travel and accommodation details optional;
                        no duplicate requests.

All test data is fictional.
"""
import io
import os
import uuid

import pytest

import app as app_module
import database
from conftest import (APPLICANT, COMPLETE_EDUCATION, PNG_BYTES, TEST_BANK_DETAILS, VISA_FORM_ANSWERS,
                      choose_yes, complete_bank_step, count_applications, funding_documents_payload,
                      get_application, upload, visa_request_for, visa_requests_for)
from database import get_db

FUNDING_DOCUMENTS = [
    "Academic Transcripts", "Certificates", "Admission Letter", "Recommendation Letter",
    "Personal Statement", "CV", "Passport / Identity Document", "Proof of Financial Need",
    "Provider-Specific Document",
]
# Previously required (now optional, like every Step 7 document).
FORMERLY_REQUIRED = ["Academic Transcripts", "Certificates", "Recommendation Letter", "Personal Statement", "CV"]
FINANCIAL_OK = {"requested_amount_ksh": "75,000", "estimated_financial_need": "KSh 150,000 per academic year"}
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
    client.post("/application/step/education", data=COMPLETE_EDUCATION)
    client.post("/application/step/funding_need", data={"funding_type_needed": "Full tuition"})
    client.post("/application/step/financial", data=FINANCIAL_OK)
    client.post("/application/step/preferences", data={"preferences": ["Scholarship"]})
    client.post("/application/step/statement", data={"personal_statement": "A fictional statement."})
    sid = q("SELECT s.id FROM students s JOIN users u ON u.id = s.user_id WHERE u.email = ?", (email,))[0][0]
    application = get_application(sid)
    return {"email": email, "student_id": sid, "application_id": application["id"]}


def doc_field(row, suffix):
    return f"document_{row['id']}_{suffix}"


def required_uploads(application_id, **extra):
    """Documents-step form data uploading every REQUIRED document, plus extra fields."""
    payload = funding_documents_payload(application_id, optional=None)
    payload.update(extra)
    return payload


def post_documents(client, data):
    return client.post("/application/step/documents", data=data, content_type="multipart/form-data")


# =====================================================================
# STEP 7 - FUNDING DOCUMENTS: all nine optional
# =====================================================================
def test_none_of_the_nine_documents_is_required_anywhere(client, drafter):
    rows = docs(drafter["application_id"])
    assert list(rows) == FUNDING_DOCUMENTS
    assert all(r["is_required"] == 0 for r in rows.values())
    assert dict(app_module.DOCUMENT_CHECKLIST) == {n: False for n in FUNDING_DOCUMENTS}
    assert tuple(database.REQUIRED_FUNDING_DOCUMENTS) == ()


def test_documents_step_shows_optional_everywhere_and_no_required_upload(client, drafter):
    page = html(client, "/application/step/documents")
    assert ("All documents are optional. Upload any documents you have. For documents you do not have, select No "
            "or leave the question unanswered. Missing documents will not prevent you from continuing your "
            "application.") in page
    assert page.count('<span class="badge bg-secondary">Optional</span>') == 9
    assert page.count('data-document-requirement="optional"') == 9
    assert ">Required<" not in page and "This document is required." not in page
    assert page.count("Do you have this document?") == 9
    for row in docs(drafter["application_id"]).values():           # nothing answered yet
        tag = page.split(f'name="{doc_field(row, "file")}"', 1)[1].split(">", 1)[0]
        assert "required" not in tag, row["document_type"]


def test_no_documents_uploaded_and_all_unanswered_can_continue(client, drafter):
    r = post_documents(client, {})
    assert r.headers["Location"].endswith("/application/step/bank")
    saved = docs(drafter["application_id"])
    assert all(d["availability"] is None and d["status"] == "Missing" for d in saved.values())   # NULL, never "No"


def test_all_documents_answered_no_can_continue(client, drafter):
    rows = docs(drafter["application_id"])
    r = post_documents(client, {doc_field(row, "availability"): "no" for row in rows.values()})
    assert r.headers["Location"].endswith("/application/step/bank")
    assert all(d["availability"] == "No" for d in docs(drafter["application_id"]).values())


@pytest.mark.parametrize("doc_type", FUNDING_DOCUMENTS)
def test_yes_without_upload_blocks_with_a_clear_message(client, drafter, doc_type):
    row = docs(drafter["application_id"])[doc_type]
    r = post_documents(client, {doc_field(row, "availability"): "yes"})
    assert r.headers["Location"].endswith("/application/step/documents")
    assert f"{doc_type} — you selected Yes, so please upload the document." in flashes(client)
    saved = docs(drafter["application_id"])[doc_type]
    assert saved["availability"] == "Yes" and saved["status"] == "Missing"


@pytest.mark.parametrize("doc_type", FUNDING_DOCUMENTS)
def test_yes_with_valid_upload_continues(client, drafter, doc_type):
    row = docs(drafter["application_id"])[doc_type]
    r = post_documents(client, {doc_field(row, "availability"): "yes",
                                doc_field(row, "file"): (io.BytesIO(PDF_BYTES), "my doc.pdf")})
    assert r.headers["Location"].endswith("/application/step/bank")
    saved = docs(drafter["application_id"])[doc_type]
    assert saved["availability"] == "Yes" and saved["status"] == "Uploaded"
    stored = saved["file_path"]
    assert stored.startswith("funding_documents/") and "my doc" not in stored        # randomised name
    assert os.path.isfile(os.path.join(app_module.FUNDING_DOCS_DIR, os.path.basename(stored)))


def test_mixed_answers_continue_and_are_saved(client, drafter):
    rows = docs(drafter["application_id"])
    data = {doc_field(rows["CV"], "availability"): "yes",
            doc_field(rows["CV"], "file"): (io.BytesIO(PDF_BYTES), "cv.pdf"),
            doc_field(rows["Certificates"], "availability"): "no"}          # the other seven left blank
    r = post_documents(client, data)
    assert r.headers["Location"].endswith("/application/step/bank")
    saved = docs(drafter["application_id"])
    assert saved["CV"]["status"] == "Uploaded" and saved["Certificates"]["availability"] == "No"
    assert all(saved[n]["availability"] is None for n in FUNDING_DOCUMENTS if n not in ("CV", "Certificates"))


def test_problems_never_discard_other_answers_from_the_same_submit(client, drafter):
    rows = docs(drafter["application_id"])
    r = post_documents(client, {
        doc_field(rows["CV"], "availability"): "yes",
        doc_field(rows["CV"], "file"): (io.BytesIO(PDF_BYTES), "cv.pdf"),                    # saved
        doc_field(rows["Certificates"], "availability"): "yes",
        doc_field(rows["Certificates"], "file"): (io.BytesIO(b"MZ not a pdf"), "c.pdf"),     # rejected
        doc_field(rows["Proof of Financial Need"], "availability"): "no",                     # saved
    })
    assert r.headers["Location"].endswith("/application/step/documents")
    saved = docs(drafter["application_id"])
    assert saved["CV"]["status"] == "Uploaded"
    assert saved["Certificates"]["status"] == "Missing" and saved["Certificates"]["file_path"] is None
    assert saved["Proof of Financial Need"]["availability"] == "No"


def test_existing_uploads_stay_intact_on_later_submits(client, drafter):
    row = docs(drafter["application_id"])["Academic Transcripts"]
    post_documents(client, {doc_field(row, "availability"): "yes",
                            doc_field(row, "file"): (io.BytesIO(PDF_BYTES), "t.pdf")})
    before = docs(drafter["application_id"])["Academic Transcripts"]
    for again in ({}, {doc_field(row, "availability"): "no"}):          # come back: blank, then No
        r = post_documents(client, again)
        assert r.headers["Location"].endswith("/application/step/bank")
        after = docs(drafter["application_id"])["Academic Transcripts"]
        assert after["file_path"] == before["file_path"] and after["status"] == "Uploaded"
        assert os.path.isfile(os.path.join(app_module.FUNDING_DOCS_DIR, os.path.basename(after["file_path"])))


def test_previously_required_rows_are_migrated_to_optional(client, drafter):
    rows = docs(drafter["application_id"])
    for name in FORMERLY_REQUIRED:                                   # rows saved under the 5-required policy
        execute("UPDATE documents SET is_required = 1 WHERE id = ?", (rows[name]["id"],))
    execute("UPDATE documents SET status = 'Uploaded', availability = 'Yes', file_path = 'funding_documents/kept.pdf' "
            "WHERE id = ?", (rows["CV"]["id"],))
    execute("UPDATE documents SET availability = 'No' WHERE id = ?", (rows["Certificates"]["id"],))

    database.init_db()   # startup migration

    migrated = docs(drafter["application_id"])
    assert all(r["is_required"] == 0 for r in migrated.values())
    assert migrated["CV"]["file_path"] == "funding_documents/kept.pdf"           # upload kept
    assert migrated["Certificates"]["availability"] == "No"                      # answer kept
    assert migrated["Academic Transcripts"]["availability"] is None              # blank stays NULL
    r = post_documents(client, {})                                               # and nothing blocks
    assert r.headers["Location"].endswith("/application/step/bank")


def test_step_visit_also_migrates_an_old_required_row(client, drafter):
    rows = docs(drafter["application_id"])
    execute("UPDATE documents SET is_required = 1 WHERE id = ?", (rows["CV"]["id"],))
    page = html(client, "/application/step/documents")
    assert ">Required<" not in page
    assert docs(drafter["application_id"])["CV"]["is_required"] == 0


def test_standalone_documents_page_shows_optional_and_status(client, drafter):
    row = docs(drafter["application_id"])["CV"]
    post_documents(client, {doc_field(row, "availability"): "yes",
                            doc_field(row, "file"): (io.BytesIO(PDF_BYTES), "cv.pdf")})
    page = html(client, "/documents")
    assert ">Required<" not in page and "Please choose Yes or No" not in page
    for name in FUNDING_DOCUMENTS:
        block = page.split(f"<strong>{name}</strong>", 1)[1].split("</div>", 1)[0]
        assert "Optional" in block, name
        assert ("Uploaded" if name == "CV" else "Missing") in block, name


def test_missing_document_rules_unit():
    def d(name, availability=None, status="Missing"):
        return {"document_type": name, "is_required": 0, "availability": availability, "status": status}
    assert app_module._missing_funding_documents([d(n) for n in FUNDING_DOCUMENTS]) == []
    assert app_module._missing_funding_documents([d(n, "No") for n in FUNDING_DOCUMENTS]) == []
    assert app_module._missing_funding_documents([d("CV", "Yes", "Uploaded")]) == []
    assert app_module._missing_funding_documents([d("CV", "Yes")]) == [
        "CV — you selected Yes, so please upload the document."]


# =====================================================================
# STEP 2 - EDUCATION
# =====================================================================
EDUCATION_REQUIRED = ["institution", "education_level", "course", "field_of_study", "year_of_study",
                      "graduation_year"]


def test_education_marks_six_required_fields_and_academic_info_optional(client, drafter):
    page = html(client, "/application/step/education")
    for name in EDUCATION_REQUIRED:
        tag = page.split(f'name="{name}"', 1)[1].split(">", 1)[0]
        assert "required" in tag, name
    tag = page.split('name="academic_info"', 1)[1].split(">", 1)[0]
    assert "required" not in tag
    assert "Additional Academic Information <span class=\"text-muted small\">(optional)</span>" in page
    assert ('placeholder="Example: Relevant academic achievements, awards, scholarships, challenges, or any '
            'additional information about your studies."') in page
    for level in ("Undergraduate", "Master&#39;s", "PhD", "Vocational/Technical", "High School"):
        assert f">{level}</option>" in page


def test_additional_academic_information_can_be_blank(client, drafter):
    r = client.post("/application/step/education", data={**COMPLETE_EDUCATION, "academic_info": ""})
    assert r.headers["Location"].endswith("/application/step/funding_need")
    assert get_application(drafter["student_id"])["academic_info"] is None
    assert "Example: Relevant academic achievements" not in (get_application(drafter["student_id"])["academic_info"] or "")


@pytest.mark.parametrize("field", EDUCATION_REQUIRED)
def test_each_required_education_field_is_enforced(client, drafter, field):
    r = client.post("/application/step/education", data={**COMPLETE_EDUCATION, field: "  "})
    assert r.headers["Location"].endswith("/application/step/education")
    assert "Please complete the required Education fields" in flashes(client)
    saved = get_application(drafter["student_id"])
    assert saved[field] is None
    # What WAS entered is still saved, so nothing has to be retyped.
    other = next(f for f in EDUCATION_REQUIRED if f != field and f != "education_level")
    assert saved[other] == COMPLETE_EDUCATION[other]


def test_education_level_must_be_one_of_the_listed_levels(client, drafter):
    r = client.post("/application/step/education", data={**COMPLETE_EDUCATION, "education_level": "Kindergarten"})
    assert r.headers["Location"].endswith("/application/step/education")
    assert get_application(drafter["student_id"])["education_level"] is None


# =====================================================================
# STEP 3 - FUNDING NEED
# =====================================================================
NEEDS = {"tuition_need": "Full", "accommodation_need": "Partial", "living_expenses_need": "Not Needed",
         "books_need": "Partial", "transport_need": "Not Needed", "technology_need": "Full"}


def test_funding_need_offers_one_choice_per_category_including_other(client, drafter):
    page = html(client, "/application/step/funding_need")
    for f in [*NEEDS, "other_expenses_need"]:
        select = page.split(f'name="{f}"', 1)[1].split("</select>", 1)[0]
        assert select.count("<option") == 3 and all(f">{lvl}<" in select for lvl in ("Not Needed", "Partial", "Full")), f
    assert ('placeholder="Please specify: e.g. examination fees, research costs, internet/data, '
            'medical/education-related expenses, etc."') in page


def test_funding_selections_save_and_show_again(client, drafter):
    data = {"funding_type_needed": "Partial support", **NEEDS, "other_expenses_need": "Partial",
            "other_expenses": "Examination fees"}
    r = client.post("/application/step/funding_need", data=data)
    assert r.headers["Location"].endswith("/application/step/financial")
    saved = get_application(drafter["student_id"])
    for f, v in {**NEEDS, "other_expenses_need": "Partial", "other_expenses": "Examination fees"}.items():
        assert saved[f] == v, f
    page = client.get("/application/step/funding_need").get_data(as_text=True)
    for f, v in {**NEEDS, "other_expenses_need": "Partial"}.items():
        select = page.split(f'name="{f}"', 1)[1].split("</select>", 1)[0]
        assert f'value="{v}" selected' in select, f
    assert "Examination fees" in page


def test_other_education_expenses_can_be_blank_when_not_needed(client, drafter):
    r = client.post("/application/step/funding_need", data={**NEEDS, "other_expenses_need": "Not Needed",
                                                            "other_expenses": ""})
    assert r.headers["Location"].endswith("/application/step/financial")
    saved = get_application(drafter["student_id"])
    assert saved["other_expenses_need"] == "Not Needed" and saved["other_expenses"] is None


def test_other_education_expenses_must_be_specified_when_needed(client, drafter):
    r = client.post("/application/step/funding_need", data={**NEEDS, "other_expenses_need": "Full",
                                                            "other_expenses": ""})
    assert r.headers["Location"].endswith("/application/step/funding_need")
    assert "please specify the expense" in flashes(client)
    assert get_application(drafter["student_id"])["tuition_need"] == "Full"      # rest still saved


def test_tampered_funding_level_is_not_stored(client, drafter):
    client.post("/application/step/funding_need", data={**NEEDS, "tuition_need": "<script>"})
    assert get_application(drafter["student_id"])["tuition_need"] is None


# =====================================================================
# STEP 4 - FINANCIAL INFORMATION + REQUESTED AMOUNT
# =====================================================================
def test_financial_step_required_optional_and_examples(client, drafter):
    assert app_module.APPLICATION_STEPS.index("financial") == 3
    assert app_module.APPLICATION_STEP_TITLES["financial"] == "Financial Information"
    page = html(client, "/application/step/financial")
    assert "Step 4 — Financial Information<" in page
    for text in (
        'placeholder="Example: KSh 150,000 per academic year"',
        ("placeholder=\"Example: Describe your household's financial situation, including income challenges, "
         "dependants, or circumstances affecting your ability to fund your education.\""),
        'placeholder="Example: HELB KSh 50,000, partial scholarship KSh 30,000, or None"',
        "Requested amount only — final funding decisions are subject to review.",
    ):
        assert text in page, text
    for opt in ("Parents/Guardians", "Self-funded", "Family Members", "Scholarship", "HELB/HEF",
                "Part-time Employment", "Sponsor/Organization", "Other"):
        assert f'name="source_of_support" value="{opt}"' in page, opt
    assert 'name="source_of_support_other"' in page
    for name, required in (("estimated_financial_need", True), ("requested_amount_ksh", True),
                           ("household_situation", False), ("funding_already_received", False),
                           ("source_of_support_other", False)):
        tag = page.split(f'name="{name}"', 1)[1].split(">", 1)[0]
        assert ("required" in tag) == required, name


def test_estimated_financial_need_is_still_required(client, drafter):
    execute("UPDATE funding_applications SET estimated_financial_need = NULL WHERE id = ?", (drafter["application_id"],))
    r = client.post("/application/step/financial", data={**FINANCIAL_OK, "estimated_financial_need": "   ",
                                                         "household_situation": "Kept anyway"})
    assert r.headers["Location"].endswith("/application/step/financial")
    assert "Estimated Financial Need is required" in flashes(client)
    saved = get_application(drafter["student_id"])
    assert saved["estimated_financial_need"] is None
    assert saved["household_situation"] == "Kept anyway"          # nothing typed is lost


def test_optional_financial_fields_can_all_be_blank(client, drafter):
    r = client.post("/application/step/financial", data={**FINANCIAL_OK, "household_situation": "",
                                                         "source_of_support_other": "", "funding_already_received": ""})
    assert r.headers["Location"].endswith("/application/step/preferences")
    saved = get_application(drafter["student_id"])
    for f in ("household_situation", "source_of_support", "funding_already_received"):
        assert saved[f] is None, f                                 # blank, never an example


def test_funding_already_received_can_be_blank(client, drafter):
    r = client.post("/application/step/financial", data={**FINANCIAL_OK, "funding_already_received": ""})
    assert r.headers["Location"].endswith("/application/step/preferences")
    assert get_application(drafter["student_id"])["funding_already_received"] is None


def test_financial_fields_save_and_persist(client, drafter):
    data = {"requested_amount_ksh": "KES 80000", "estimated_financial_need": "KSh 150,000 per academic year",
            "household_situation": "Fictional: two dependants, one income.",
            "source_of_support": ["Parents/Guardians", "HELB/HEF", "Other"],
            "source_of_support_other": "Church bursary", "funding_already_received": "HELB KSh 50,000"}
    r = client.post("/application/step/financial", data=data)
    assert r.headers["Location"].endswith("/application/step/preferences")
    saved = get_application(drafter["student_id"])
    assert saved["requested_amount_ksh"] == 80000
    assert saved["source_of_support"] == "Parents/Guardians, HELB/HEF, Other: Church bursary"
    assert saved["funding_already_received"] == "HELB KSh 50,000"
    client.get("/application/step/personal")
    page = client.get("/application/step/financial").get_data(as_text=True)
    assert 'value="80,000"' in page and "KSh 150,000 per academic year" in page
    for opt in ("Parents/Guardians", "HELB/HEF", "Other"):
        assert f'value="{opt}" id="support_' in page and \
            page.split(f'value="{opt}" id="support_', 1)[1].split(">", 1)[0].rstrip().endswith("checked"), opt
    assert 'value="Church bursary"' in page


def test_older_free_text_source_of_support_is_kept_when_editing(client, drafter):
    execute("UPDATE funding_applications SET source_of_support = 'Family and personal savings' WHERE id = ?",
            (drafter["application_id"],))
    page = client.get("/application/step/financial").get_data(as_text=True)
    assert 'value="Family and personal savings"' in page          # shown in the Other box, not lost


@pytest.mark.parametrize("raw,saved", [("75,000", 75000), ("75000", 75000), ("KSh 75,000", 75000),
                                       ("KES 75000", 75000)])
def test_requested_amount_accepts_valid_formats(client, drafter, raw, saved):
    r = client.post("/application/step/financial", data={**FINANCIAL_OK, "requested_amount_ksh": raw})
    assert r.headers["Location"].endswith("/application/step/preferences")
    assert get_application(drafter["student_id"])["requested_amount_ksh"] == saved


@pytest.mark.parametrize("raw", ["", "0", "-5000", "75000.50", "12.5", "abc", "seventy thousand", "KSh"])
def test_requested_amount_rejects_invalid_values(client, drafter, raw):
    execute("UPDATE funding_applications SET requested_amount_ksh = 60000 WHERE id = ?", (drafter["application_id"],))
    r = client.post("/application/step/financial", data={**FINANCIAL_OK, "requested_amount_ksh": raw})
    assert r.headers["Location"].endswith("/application/step/financial")
    assert get_application(drafter["student_id"])["requested_amount_ksh"] == 60000    # never overwritten


# =====================================================================
# STEP 6 - PERSONAL STATEMENT (optional)
# =====================================================================
EXAMPLE_STATEMENT_START = "I am currently pursuing a Bachelor's degree in Information Technology"


def test_statement_step_is_optional_and_shows_the_example(client, drafter):
    assert app_module.APPLICATION_STEPS.index("statement") == 5
    page = html(client, "/application/step/statement")
    assert "Step 6 — Personal Statement" in page and "Optional" in page
    assert ("Tell us about your educational goals, financial need, career goals, and why funding matters "
            "to you and your community.") in page
    assert EXAMPLE_STATEMENT_START in page
    assert "create opportunities for other young people in my community." in page
    assert 'placeholder="e.g. My educational goal is to...' in page
    tag = page.split('name="personal_statement"', 1)[1].split(">", 1)[0]
    assert "required" not in tag
    assert page.index(EXAMPLE_STATEMENT_START) < page.index('name="personal_statement"')


def test_personal_statement_can_be_blank_and_example_is_never_saved(client, drafter):
    r = client.post("/application/step/statement", data={"personal_statement": ""})
    assert r.headers["Location"].endswith("/application/step/documents")
    assert get_application(drafter["student_id"])["personal_statement"] is None
    textarea = client.get("/application/step/statement").get_data(as_text=True).split('name="personal_statement"')[1]
    assert "I am currently pursuing" not in textarea.split("</textarea>")[0]


def test_personal_statement_saves_and_persists(client, drafter):
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
    app_id = q("SELECT id FROM funding_applications ORDER BY id DESC LIMIT 1")[0][0]
    return post_documents(client, required_uploads(app_id))


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


def test_purpose_of_travel_is_optional_with_examples(client, student):
    req = visa_request_for(client, student)
    page = html(client, f"/student-visa/application/{req}/step/visa_info")
    assert 'placeholder="Example: Educational purposes"' in page
    assert "Purpose of Travel (optional)" in page
    tag = page.split('name="purpose_of_travel"', 1)[1].split(">", 1)[0]
    assert "required" not in tag
    for example in ("Educational purposes", "Study", "University/College education", "Attending an academic program",
                    "Research", "Training", "Other"):
        assert f'data-value="{example}"' in page, example
    # Country and Visa Type stay required.
    for name in ("destination_country", "visa_category"):
        assert "required" in page.split(f'name="{name}"', 1)[1].split(">", 1)[0], name
    assert "purpose_of_travel" not in app_module.visa_lib.VISA_REQUIRED_FIELDS["visa_info"]


def test_purpose_of_travel_can_be_blank_and_payment_stays_available(client, student):
    req = visa_request_for(client, student)
    r = client.post(f"/student-visa/application/{req}/step/visa_info",
                    data={**VISA_FORM_ANSWERS["visa_info"], "purpose_of_travel": ""})
    assert r.headers["Location"].endswith(f"/student-visa/application/{req}/step/education")   # advanced
    assert not q("SELECT purpose_of_travel FROM visa_requests WHERE id = ?", (req,))[0][0]
    assert client.get(f"/student-visa/payment/{req}").status_code == 200


@pytest.mark.parametrize("field", ["destination_country", "visa_category"])
def test_country_and_visa_type_are_still_required(client, student, field):
    req = visa_request_for(client, student)
    client.post(f"/student-visa/application/{req}/step/visa_info", data={**VISA_FORM_ANSWERS["visa_info"], field: ""})
    r = client.get(f"/student-visa/payment/{req}")
    assert r.status_code == 302 and f"/student-visa/application/{req}" in r.headers["Location"]


def test_accommodation_details_are_optional_with_examples(client, student):
    req = visa_request_for(client, student)
    page = html(client, f"/student-visa/application/{req}/step/accommodation")
    for name, example in (("accommodation_name", "Example: University of Embu Hostels / ABC Hotel / John Doe"),
                          ("accommodation_address", "Example: Embu, Kenya"),
                          ("accommodation_contact", "Example: +254 7XX XXX XXX / host@example.com")):
        tag = page.split(f'name="{name}"', 1)[1].split(">", 1)[0]
        assert "required" not in tag and f'placeholder="{example}"' in tag, name
    r = client.post(f"/student-visa/application/{req}/step/accommodation",
                    data={"accommodation_type": "Hotel", "accommodation_name": "", "accommodation_address": "",
                          "accommodation_contact": ""})
    assert r.headers["Location"].endswith(f"/student-visa/application/{req}/step/travel_history")
    row = q("SELECT accommodation_name, accommodation_address, accommodation_contact FROM visa_requests WHERE id = ?",
            (req,))[0]
    assert not any(row)
    assert client.get(f"/student-visa/payment/{req}").status_code == 200       # nothing blocks payment


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


def test_blank_optional_visa_answer_is_not_treated_as_available_and_does_not_block(client, student):
    req = visa_request_for(client, student)
    target = _visa_docs(req)["Accommodation Booking"]
    execute("UPDATE visa_documents SET availability = NULL WHERE id = ?", (target["id"],))
    r = client.post(f"/student-visa/application/{req}/step/documents", data={})
    assert r.headers["Location"].endswith(f"/student-visa/application/{req}/step/documents")
    saved = _visa_docs(req)["Accommodation Booking"]
    assert saved["availability"] is None and not saved["stored_file"]     # not treated as available
    assert client.get(f"/student-visa/payment/{req}").status_code == 200  # ...and never blocks


def test_all_optional_visa_documents_unanswered_still_allow_payment(client, student):
    req = visa_request_for(client, student)
    execute("UPDATE visa_documents SET availability = NULL WHERE request_id = ? AND is_required = 0", (req,))
    client.post(f"/student-visa/application/{req}/step/documents", data={})
    assert client.get(f"/student-visa/payment/{req}").status_code == 200
    page = html(client, f"/student-visa/application/{req}/step/documents")
    assert "please choose Yes or No" not in page.lower()


def test_required_visa_documents_still_block_even_when_optional_ones_are_blank(client, student):
    req = visa_request_for(client, student)
    execute("UPDATE visa_documents SET availability = NULL WHERE request_id = ? AND is_required = 0", (req,))
    execute("UPDATE visa_documents SET stored_file = NULL, status = 'Missing' WHERE request_id = ? "
            "AND document_type = 'National ID'", (req,))
    assert client.get(f"/student-visa/payment/{req}").status_code == 302


def test_where_will_you_stay_is_required(client, student):
    req = visa_request_for(client, student)
    page = html(client, f"/student-visa/application/{req}/step/accommodation")
    select = page.split('name="accommodation_type"', 1)[1].split("</select>", 1)[0]
    assert select.split(">", 1)[0].rstrip().endswith("required")
    for opt in ("Hotel", "University Accommodation", "With Family/Friend", "Rented Accommodation", "Other"):
        assert f">{opt}</option>" in select, opt
    assert "Where will you stay? <span class=\"text-danger\">*</span>" in page
    assert app_module.visa_lib.VISA_REQUIRED_FIELDS["accommodation"] == ["accommodation_type"]
    for bad in ("", "Tent on the moon"):                           # blank or tampered value
        client.post(f"/student-visa/application/{req}/step/accommodation", data={"accommodation_type": bad})
        assert q("SELECT accommodation_type FROM visa_requests WHERE id = ?", (req,))[0][0] is None
        r = client.get(f"/student-visa/payment/{req}")
        assert r.status_code == 302 and f"/student-visa/application/{req}" in r.headers["Location"], bad
    client.post(f"/student-visa/application/{req}/step/accommodation",
                data={"accommodation_type": "University Accommodation"})   # name/address/contact left blank
    assert client.get(f"/student-visa/payment/{req}").status_code == 200


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
    letter = docs(drafter["application_id"])["Admission Letter"]
    client.post("/application/step/documents", data={doc_field(letter, "availability"): "yes"})   # Yes, no file
    page = html(client, "/documents")
    assert 'method="POST"' not in page and 'name="document_id"' not in page
    assert ">Upload<" not in page
    assert "Admission Letter — you selected Yes, so please upload the document." in page
    assert "Required: please upload" not in page                    # no document is required
    assert 'href="/application/step/documents"' in page
    assert docs(drafter["application_id"])["Admission Letter"]["status"] == "Missing"


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


# =====================================================================
# REVIEW - required vs optional, blanks are not errors, server-side checks
# =====================================================================
def _ready_for_review(client, drafter):
    to_bank(client)
    complete_bank_step(client)


def test_review_lists_every_document_with_requirement_and_status(client, drafter):
    _ready_for_review(client, drafter)
    page = html(client, "/application/step/review")
    table = page.split('id="reviewDocuments"', 1)[1].split("</table>", 1)[0]
    for name in FUNDING_DOCUMENTS:
        row = table.split(f"<td>{name}</td>", 1)[1].split("</tr>", 1)[0]
        assert ">Optional<" in row and ">Required<" not in row, name
        assert "Missing" in row and "⚠" not in row, name             # missing is never an error
    assert "All documents are optional — a missing document does not stop your application." in page


def test_review_shows_blank_optional_fields_as_optional_not_errors(client, drafter):
    client.post("/application/step/statement", data={"personal_statement": ""})
    client.post("/application/step/financial", data={**FINANCIAL_OK, "funding_already_received": "",
                                                     "household_situation": ""})
    _ready_for_review(client, drafter)
    page = html(client, "/application/step/review")
    summary = page.split('id="reviewSummary"', 1)[1].split("</table>", 1)[0]
    assert "⚠" not in summary
    assert summary.count("Not provided (optional)") >= 4      # academic info, household, funding received, statement
    r = client.post("/application/step/review", data={})
    assert r.headers["Location"].endswith("/application/step/visa")


def test_review_shows_funding_need_selections(client, drafter):
    client.post("/application/step/funding_need", data={**NEEDS, "other_expenses_need": "Partial",
                                                        "other_expenses": "Internet/data"})
    _ready_for_review(client, drafter)
    page = html(client, "/application/step/review")
    assert 'id="reviewNeed_tuition_need">Full<' in page
    assert 'id="reviewNeed_other_expenses_need">Partial — Internet/data<' in page


@pytest.mark.parametrize("setup_sql,step", [
    ("UPDATE funding_applications SET graduation_year = NULL WHERE id = ?", "education"),
    ("UPDATE funding_applications SET estimated_financial_need = '' WHERE id = ?", "financial"),
    # An explicit "Yes" without an upload is the only document state that blocks.
    ("UPDATE documents SET availability = 'Yes', status = 'Missing', file_path = NULL WHERE application_id = ? "
     "AND document_type = 'CV'", "documents"),
])
def test_review_enforces_required_fields_and_documents_server_side(client, drafter, setup_sql, step):
    _ready_for_review(client, drafter)
    execute(setup_sql, (drafter["application_id"],))
    r = client.post("/application/step/review", data={})
    assert r.headers["Location"].endswith(f"/application/step/{step}")


def test_review_passes_with_every_document_missing(client, drafter):
    _ready_for_review(client, drafter)
    assert all(d["status"] == "Missing" for d in docs(drafter["application_id"]).values())
    r = client.post("/application/step/review", data={})
    assert r.headers["Location"].endswith("/application/step/visa")


def test_final_submission_never_blocked_by_missing_documents_only_by_yes_without_upload(client, student):
    application = get_application(student["student_id"])
    execute("UPDATE funding_applications SET visa_status = 'NEEDS_ASSISTANCE', visa_step_status = 'COMPLETE' "
            "WHERE id = ?", (application["id"],))
    execute("UPDATE documents SET status = 'Missing', file_path = NULL WHERE application_id = ?", (application["id"],))
    execute("UPDATE documents SET availability = 'Yes' WHERE application_id = ? "
            "AND document_type = 'Recommendation Letter'", (application["id"],))
    r = client.post("/application/submit")
    assert r.headers["Location"].endswith("/application/step/documents")
    assert get_application(student["student_id"])["status"] == "Draft"
    # Answering No (or leaving it blank) for that document - with NO documents uploaded at all - submits.
    execute("UPDATE documents SET availability = 'No' WHERE application_id = ? "
            "AND document_type = 'Recommendation Letter'", (application["id"],))
    r = client.post("/application/submit")
    assert "/application/confirmation/" in r.headers["Location"]


def test_admin_application_detail_labels_every_document_optional(client, student):
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
    page = html(admin, f"/admin/applications/{application['id']}")
    for name in FUNDING_DOCUMENTS:
        item = page.split(f"<span>{name}", 1)[1].split("</span></span>", 1)[0]
        assert "Optional" in item and "Required" not in item, name
