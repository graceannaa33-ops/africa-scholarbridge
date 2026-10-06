"""Visa form, section 9 (Documents Checklist): real-browser upload behaviour.

Regression for "Passport-size Photograph / National ID refuse to upload and stay
Missing". Two causes, both fixed:

1. The checklist's file inputs and Yes/No choices were rendered OUTSIDE the
   <form> that the Save button submits, so a browser sent no files at all.
   These tests read the rendered page like a browser does and submit ONLY the
   fields that actually belong to that form.
2. The app-wide 8 MB request limit applied to the whole multi-file request, so
   two ordinary photos (e.g. 5 MB each) were refused before any file was read.
   The 8 MB limit now applies per document on these routes.

All test data is fictional.
"""
import io
from html.parser import HTMLParser

import pytest

from conftest import PNG_BYTES, make_pdf, visa_request_for
from database import get_db

FORM_ID = "visaDocumentsForm"
EIGHT_MB = 8 * 1024 * 1024
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 2048
REQUIRED = ("Passport-size Photograph", "National ID")


class _FormFields(HTMLParser):
    """Names of the inputs a browser would submit with <form id=FORM_ID>:
    those nested inside it, plus those pointing at it with form="FORM_ID"."""

    def __init__(self):
        super().__init__()
        self.stack, self.fields = [], {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.stack.append(a.get("id"))
        elif tag in ("input", "select", "textarea") and a.get("name"):
            owner = a.get("form") or (self.stack[-1] if self.stack else None)
            if owner == FORM_ID:
                self.fields.setdefault(a["name"], []).append(a)

    def handle_endtag(self, tag):
        if tag == "form" and self.stack:
            self.stack.pop()


def submitted_fields(client, request_id):
    page = client.get(f"/student-visa/application/{request_id}/step/documents").get_data(as_text=True)
    parser = _FormFields()
    parser.feed(page)
    return parser.fields


def visa_docs(request_id):
    db = get_db()
    rows = db.execute("SELECT * FROM visa_documents WHERE request_id = ? ORDER BY id", (request_id,)).fetchall()
    db.close()
    return {r["document_type"]: dict(r) for r in rows}


def execute(sql, args=()):
    db = get_db()
    db.execute(sql, args)
    db.commit()
    db.close()


def flashes(client):
    with client.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


@pytest.fixture()
def docs_request(client, student):
    """A form-first visa request at section 9 with BOTH required documents missing
    and every optional document still unanswered."""
    req = visa_request_for(client, student)
    execute("UPDATE visa_documents SET stored_file = NULL, original_name = NULL, file_size = NULL, "
            "status = 'Missing', availability = NULL WHERE request_id = ?", (req,))
    return req


def browser_post(client, req, files=None, answers=None):
    """Submit section 9 the way a browser would: only fields linked to the form."""
    fields = submitted_fields(client, req)
    data = {}
    for (name, value) in (answers or {}).items():
        assert name in fields, f"{name} would not be submitted by a browser"
        data[name] = value
    for name, (blob, filename) in (files or {}).items():
        assert name in fields, f"{name} would not be submitted by a browser"
        data[name] = (io.BytesIO(blob), filename)
    return client.post(f"/student-visa/application/{req}/step/documents", data=data,
                       content_type="multipart/form-data")


def file_field(req, doc_type):
    return f"document_{visa_docs(req)[doc_type]['id']}_file"


# ---------------------------------------------------------------------
# Cause 1: every checklist input belongs to the form the Save button submits
# ---------------------------------------------------------------------
def test_every_checklist_input_is_submitted_with_the_save_form(client, docs_request):
    fields = submitted_fields(client, docs_request)
    for doc_type, row in visa_docs(docs_request).items():
        assert f"document_{row['id']}_file" in fields, doc_type
        if not row["is_required"]:
            values = {a["value"] for a in fields[f"document_{row['id']}_availability"]}
            assert values == {"yes", "no"}, doc_type


@pytest.mark.parametrize("doc_type", REQUIRED)
def test_required_document_uploads_through_the_real_form(client, docs_request, doc_type):
    r = browser_post(client, docs_request, files={file_field(docs_request, doc_type): (PNG_BYTES, "doc.png")})
    assert r.status_code == 302
    saved = visa_docs(docs_request)[doc_type]
    assert saved["status"] == "Uploaded" and saved["stored_file"], doc_type
    assert saved["stored_file"] != "doc.png" and saved["original_name"] == "doc.png"     # randomised on disk
    page = client.get(f"/student-visa/application/{docs_request}/step/documents").get_data(as_text=True)
    block = page.split(f"<strong>{doc_type}</strong>", 1)[1].split("</li>", 1)[0]
    assert ">Uploaded<" in block and ">Missing<" not in block


def test_both_required_documents_then_payment_opens(client, docs_request):
    optional = {f"document_{d['id']}_availability": "no"
                for d in visa_docs(docs_request).values() if not d["is_required"]}
    browser_post(client, docs_request,
                 files={file_field(docs_request, "Passport-size Photograph"): (JPEG_BYTES, "photo.jpg"),
                        file_field(docs_request, "National ID"): (make_pdf(["FICTIONAL ID"]), "id.pdf")},
                 answers=optional)
    saved = visa_docs(docs_request)
    assert all(saved[d]["status"] == "Uploaded" for d in REQUIRED)
    assert client.get(f"/student-visa/payment/{docs_request}").status_code == 200


# ---------------------------------------------------------------------
# Formats: PDF / JPG / JPEG / PNG accepted, anything else refused
# ---------------------------------------------------------------------
@pytest.mark.parametrize("blob,filename", [
    (JPEG_BYTES, "photo.jpg"), (JPEG_BYTES, "photo.JPEG"), (PNG_BYTES, "photo.png"),
    (make_pdf(["FICTIONAL PHOTO PAGE"]), "photo.pdf"),
], ids=["jpg", "JPEG", "png", "pdf"])
def test_valid_formats_are_accepted(client, docs_request, blob, filename):
    browser_post(client, docs_request, files={file_field(docs_request, "Passport-size Photograph"): (blob, filename)})
    assert visa_docs(docs_request)["Passport-size Photograph"]["status"] == "Uploaded", filename


@pytest.mark.parametrize("blob,filename", [
    (b"GIF89a" + b"\x00" * 100, "photo.gif"),
    (b"MZ\x90\x00 not a document", "photo.exe"),
    (b"PK\x03\x04 fake docx", "id.docx"),
    (b"MZ\x90\x00 disguised", "photo.png"),          # right extension, wrong content
    (b"", "empty.pdf"),
], ids=["gif", "exe", "docx", "fake-png", "empty-pdf"])
def test_unsupported_or_fake_files_are_refused_and_stay_missing(client, docs_request, blob, filename):
    browser_post(client, docs_request, files={file_field(docs_request, "National ID"): (blob, filename)})
    saved = visa_docs(docs_request)["National ID"]
    assert saved["status"] == "Missing" and not saved["stored_file"], filename
    assert "National ID" in flashes(client)


# ---------------------------------------------------------------------
# Size: 8 MB per document (Cause 2)
# ---------------------------------------------------------------------
def test_file_over_8_mb_is_refused_with_a_clear_message(client, docs_request):
    big = PNG_BYTES[:8] + b"\x00" * (EIGHT_MB + 1 - 8)
    r = browser_post(client, docs_request, files={file_field(docs_request, "Passport-size Photograph"): (big, "big.png")})
    assert r.status_code == 302                                   # handled, not a dropped connection
    assert visa_docs(docs_request)["Passport-size Photograph"]["status"] == "Missing"
    assert "invalid or oversized" in flashes(client)


def test_two_large_photos_in_one_submission_are_both_accepted(client, docs_request):
    five_mb = 5 * 1024 * 1024
    browser_post(client, docs_request, files={
        file_field(docs_request, "Passport-size Photograph"): (JPEG_BYTES[:4] + b"\x00" * five_mb, "photo.jpg"),
        file_field(docs_request, "National ID"): (b"%PDF-1.4\n" + b"0" * five_mb, "id.pdf"),
    })
    saved = visa_docs(docs_request)
    assert saved["Passport-size Photograph"]["status"] == "Uploaded"
    assert saved["National ID"]["status"] == "Uploaded"


def test_a_file_of_exactly_8_mb_is_accepted(client, docs_request):
    exact = b"%PDF-1.4\n" + b"0" * (EIGHT_MB - 9)
    browser_post(client, docs_request, files={file_field(docs_request, "National ID"): (exact, "id.pdf")})
    assert visa_docs(docs_request)["National ID"]["status"] == "Uploaded"


def test_other_routes_keep_the_8_mb_request_limit(client, student):
    big = b"\x00" * (EIGHT_MB + 1024)
    r = client.post("/application/step/personal", data={"blob": (io.BytesIO(big), "x.bin")},
                    content_type="multipart/form-data")
    assert r.status_code in (302, 413)
    assert "too large" in flashes(client)


# ---------------------------------------------------------------------
# Required documents can never stay Missing when proceeding
# ---------------------------------------------------------------------
def test_required_documents_missing_block_payment_and_are_reported(client, docs_request):
    optional = {f"document_{d['id']}_availability": "no"
                for d in visa_docs(docs_request).values() if not d["is_required"]}
    browser_post(client, docs_request, answers=optional)
    message = flashes(client)
    assert 'Please upload "Passport-size Photograph"' in message and 'Please upload "National ID"' in message
    assert all(visa_docs(docs_request)[d]["status"] == "Missing" for d in REQUIRED)
    r = client.get(f"/student-visa/payment/{docs_request}")
    assert r.status_code == 302 and f"/student-visa/application/{docs_request}" in r.headers["Location"]


def test_only_one_required_document_still_blocks_payment(client, docs_request):
    browser_post(client, docs_request, files={file_field(docs_request, "Passport-size Photograph"): (PNG_BYTES, "p.png")})
    assert visa_docs(docs_request)["National ID"]["status"] == "Missing"
    assert client.get(f"/student-visa/payment/{docs_request}").status_code == 302


# ---------------------------------------------------------------------
# Optional documents keep their behaviour through the real form
# ---------------------------------------------------------------------
def test_optional_yes_no_and_upload_through_the_real_form(client, docs_request):
    docs = visa_docs(docs_request)
    inv, ins = docs["Invitation Letter"], docs["Travel Medical Insurance"]
    browser_post(client, docs_request, answers={f"document_{inv['id']}_availability": "yes",
                                                f"document_{ins['id']}_availability": "no"})
    saved = visa_docs(docs_request)
    assert saved["Invitation Letter"]["availability"] == "Yes" and saved["Invitation Letter"]["status"] == "Missing"
    assert saved["Travel Medical Insurance"]["availability"] == "No"
    assert "Invitation Letter" in flashes(client)                          # Yes needs the upload
    browser_post(client, docs_request, answers={f"document_{inv['id']}_availability": "yes"},
                 files={f"document_{inv['id']}_file": (PNG_BYTES, "invite.png")})
    assert visa_docs(docs_request)["Invitation Letter"]["status"] == "Uploaded"


# ---------------------------------------------------------------------
# Issue 2: U.S. Student Visa information section before the visa question
# ---------------------------------------------------------------------
def test_us_student_visa_section_appears_immediately_before_the_visa_question(client, student):
    page = client.get("/application/step/visa").get_data(as_text=True)
    flat = " ".join(page.split())
    start = flat.index('id="usStudentVisaInfo"')
    question = flat.index('id="visaQuestion"')
    assert start < question
    between = flat[flat.index("</section>", start) + len("</section>"):question]
    assert between.strip() == "<p class=\"mb-4 fw-semibold\""        # nothing else in between
    section = flat[start:flat.index("</section>", start)]
    for text in (
        "🇺🇸 Why a U.S. Student Visa Is Important for Your Funding Journey",
        "For many African students, studying in the United States is not only about gaining a quality education",
        "💰 How Funding and the Student Visa Connect", "The process can often work like this:",
        "Find an eligible U.S. program", "Apply for admission", "Secure available funding",
        "Complete your student-visa process", "Travel and begin your studies",
        "🎓 Why This Matters", "⚠️ Important",
        "A U.S. student visa does not itself provide funding",
        "we do not guarantee funding, admission, or visa approval.",
    ):
        assert text in section, text
    # The existing question and its Yes/No choices are unchanged.
    assert "Do you currently have a valid visa for your intended study/travel country?" in flat
    assert 'name="visa_choice" value="yes"' in flat and 'name="visa_choice" value="no"' in flat


def test_us_student_visa_section_is_not_in_the_documents_checklist(client, docs_request):
    page = client.get(f"/student-visa/application/{docs_request}/step/documents").get_data(as_text=True)
    assert "usStudentVisaInfo" not in page and "Why a U.S. Student Visa Is Important" not in page
