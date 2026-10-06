"""Shared fixtures for the visa-step tests.

The app creates its database and upload folders at import time, so the
environment is pointed at a throwaway directory BEFORE `app` is imported.
Nothing here touches the real database/ or uploads/ folders.
"""
import io
import os
import sys
import tempfile
import uuid
from datetime import date, timedelta

import pytest

_TMP = tempfile.mkdtemp(prefix="asb-tests-")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["UPLOAD_ROOT"] = os.path.join(_TMP, "uploads")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from database import get_db  # noqa: E402

UPLOAD_DIR = os.path.join(_TMP, "uploads", "visa_documents")


def _ensure_cycle():
    db = get_db()
    if not db.execute("SELECT 1 FROM funding_cycles WHERE is_current = 1").fetchone():
        d = lambda n: (date.today() + timedelta(days=n)).isoformat()  # noqa: E731
        db.execute(
            """INSERT INTO funding_cycles (name, year, open_date, close_date, status, is_current)
               VALUES ('Test Cycle', '2026/2027', ?, ?, 'Open', 1)""",
            (d(-30), d(180)),
        )
        db.commit()
    db.close()


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    _ensure_cycle()
    with app_module.app.test_client() as c:
        yield c


APPLICANT = {"full_name": "Amina Wanjiru Otieno", "date_of_birth": "2003-05-14"}


@pytest.fixture()
def student(client):
    """A freshly registered Kenyan student whose application has reached
    the visa step with the personal details filled in."""
    email = f"student-{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/register", data={
        "email": email, "password": "Passw0rd!", "confirm_password": "Passw0rd!",
        "full_name": APPLICANT["full_name"], "country": "Kenya", "citizenship": "Kenyan",
        "phone": "0712345678",
    })
    assert r.status_code == 302
    client.get("/application/start")
    client.post("/application/step/personal", data={
        "full_name": APPLICANT["full_name"], "date_of_birth": APPLICANT["date_of_birth"],
        "country": "Kenya", "citizenship": "Kenyan", "phone": "0712345678",
        "email": email, "gender": "Female",
    })
    complete_steps_before_visa(client)
    db = get_db()
    user = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    stu = db.execute("SELECT id FROM students WHERE user_id = ?", (user["id"],)).fetchone()
    db.close()
    return {"email": email, "student_id": stu["id"]}


TEST_BANK_DETAILS = {
    "action": "save_details", "country": "Kenya", "manual_bank_name": "Example Bank",
    "account_holder_name": "Alex Testperson", "account_number": "TEST-ACCOUNT-001", "account_type": "Savings",
    "branch": "Example Branch", "bank_code": "TEST-BANK-001", "swift_bic": "", "iban": "",
}


def complete_bank_step(client, **overrides):
    """Bank Account / Disbursement Information (fictional details), then
    the confirmation screen."""
    client.post("/application/step/bank", data={**TEST_BANK_DETAILS, **overrides})
    return client.post("/application/step/bank", data={"action": "confirm", "confirm_accurate": "yes"})


def complete_steps_before_visa(client):
    """Every Start Application step after "personal", up to and including
    Review - so the student stands at the FINAL step, Visa Verification."""
    client.post("/application/step/education", data={
        "institution": "University of Nairobi", "education_level": "Undergraduate", "course": "BSc Computer Science",
        "field_of_study": "Computer Science", "year_of_study": "2", "graduation_year": "2027", "academic_info": ""})
    client.post("/application/step/funding_need", data={
        "funding_type_needed": "Full tuition", "tuition_need": "Full", "accommodation_need": "Partial",
        "living_expenses_need": "Partial", "books_need": "Partial", "transport_need": "Not Needed",
        "technology_need": "Not Needed", "other_expenses": ""})
    client.post("/application/step/financial", data={
        "requested_amount_ksh": "75,000",
        "household_situation": "Single parent household", "source_of_support": "Family",
        "estimated_financial_need": "USD 3,000 / year", "funding_already_received": ""})
    client.post("/application/step/preferences", data={"preferences": ["International Study", "Scholarship"]})
    client.post("/application/step/statement", data={"personal_statement": "I want to study computer science."})
    # Required funding documents are uploaded (fictional PDF bytes) through
    # the real Documents step; optional documents are explicitly marked No.
    db = get_db()
    app_row = db.execute("SELECT id FROM funding_applications ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    client.post("/application/step/documents", data=funding_documents_payload(app_row["id"]),
                content_type="multipart/form-data")
    complete_bank_step(client)
    r = client.post("/application/step/review", data={})
    assert r.status_code == 302 and r.headers["Location"].endswith("/application/step/visa"), r.headers.get("Location")
    return r


FICTIONAL_PDF = b"%PDF-1.4\n% fictional test document\n"
COMPLETE_EDUCATION = {"institution": "Example University", "education_level": "Undergraduate",
                      "course": "Bachelor of Information Technology", "field_of_study": "Information Technology",
                      "year_of_study": "2nd Year", "graduation_year": "2028", "academic_info": ""}


def funding_documents_payload(application_id, optional="no"):
    """Form data for the Documents step: a fictional PDF for every REQUIRED
    funding document, and `optional` ('no', 'yes' or None = leave blank)
    for every optional one."""
    db = get_db()
    rows = db.execute("SELECT id, is_required FROM documents WHERE application_id = ?", (application_id,)).fetchall()
    db.close()
    payload = {}
    for row in rows:
        if row["is_required"]:
            payload[f"document_{row['id']}_file"] = (io.BytesIO(FICTIONAL_PDF), f"required-{row['id']}.pdf")
        elif optional:
            payload[f"document_{row['id']}_availability"] = optional
    return payload


def get_application(student_id):
    db = get_db()
    row = db.execute("SELECT * FROM funding_applications WHERE student_id = ?", (student_id,)).fetchone()
    db.close()
    return row


def visa_requests_for(application_id):
    db = get_db()
    rows = db.execute("SELECT * FROM visa_requests WHERE annual_application_id = ?", (application_id,)).fetchall()
    db.close()
    return rows



def visa_request_for(client, student):
    """Create the form-first visa assistance request used by document-flow tests.
    The annual application is already at the final Visa Verification step."""
    application = get_application(student["student_id"])
    if not application:
        raise AssertionError("Expected a funding application for the test student.")

    requests = visa_requests_for(application["id"])
    if requests:
        return requests[-1]["id"]

    response = client.post("/application/step/visa", data={"visa_choice": "no"})
    assert response.status_code == 302, response.status_code

    application = get_application(student["student_id"])
    request_id = application["visa_request_id"]
    assert request_id, "Expected visa assistance request to be linked to the annual application."

    # Put the request in the same document-ready state used by the existing
    # visa tests: required photograph + National ID uploaded, optional items No.
    complete_visa_form(client, request_id, sign=False)
    return request_id

def count_applications(student_id):
    db = get_db()
    n = db.execute("SELECT COUNT(*) FROM funding_applications WHERE student_id = ?", (student_id,)).fetchone()[0]
    db.close()
    return n


# ---------------------------------------------------------------------
# Test documents
# ---------------------------------------------------------------------
def _cd(s):
    """ICAO 9303 check digit - an independent implementation for the tests."""
    total = 0
    for i, ch in enumerate(s):
        v = int(ch) if ch.isdigit() else (ord(ch) - 55 if ch.isalpha() else 0)
        total += v * (7, 3, 1)[i % 3]
    return str(total % 10)


def make_mrz(surname, given, passport, nationality, dob_yymmdd, sex, expiry_yymmdd):
    name = (surname.replace(" ", "<") + "<<" + given.replace(" ", "<"))
    line1 = ("VNUSA" + name).ljust(44, "<")[:44]
    pp = passport.ljust(9, "<")
    line2 = (pp + _cd(pp) + nationality + dob_yymmdd + _cd(dob_yymmdd) + sex
             + expiry_yymmdd + _cd(expiry_yymmdd)).ljust(44, "<")
    return line1, line2


def make_pdf(lines, jpeg=None, size=None):
    """A small but real one-page PDF whose text layer holds `lines`.
    With `jpeg` (bytes) and `size` (w, h) the page also shows that image -
    like a phone-scanner PDF: page image + its own (possibly imperfect)
    OCR text layer."""
    def esc(t):
        return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = ""
    if jpeg:
        content += "q 612 0 0 400 0 300 cm /Im1 Do Q\n"
    content += "BT /F1 10 Tf 40 780 Td 14 TL\n" + "".join(f"({esc(l)}) Tj T*\n" for l in lines) + "ET"
    xobj = " /XObject << /Im1 6 0 R >>" if jpeg else ""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
         f"/Resources << /Font << /F1 5 0 R >>{xobj} >> >>").encode(),
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream".encode("latin-1"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    ]
    if jpeg:
        w, h = size
        objs.append(f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceRGB "
                    f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n".encode()
                    + jpeg + b"\nendstream")
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{off:010d} 00000 n \n" for off in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def scanner_pdf(text_lines, blur=0, **overrides):
    """Phone-scanner style PDF: the visa page image + a separate text layer."""
    im = visa_image(blur, **overrides)
    return make_pdf(text_lines, jpeg=image_bytes(im, "JPEG"), size=im.size)


def fmt_foil(d):
    return d.strftime("%d%b%Y").upper()


def _visa_fields(overrides):
    p = {
        "surname": "OTIENO", "given": "AMINA WANJIRU", "passport": "AK1234567",
        "dob": date(2003, 5, 14), "visa_class": "F1",
        "issue": date.today() - timedelta(days=200), "expiry": date.today() + timedelta(days=900),
    }
    p.update(overrides)
    l1, l2 = make_mrz(p["surname"], p["given"], p["passport"], "KEN",
                      p["dob"].strftime("%y%m%d"), "F", p["expiry"].strftime("%y%m%d"))
    printed = [
        "UNITED STATES OF AMERICA   VISA",
        f"Surname {p['surname']}", f"Given Name {p['given']}",
        f"Visa Type/Class R {p['visa_class']}", f"Passport Number {p['passport']}",
        f"Issue Date {fmt_foil(p['issue'])}", f"Expiration Date {fmt_foil(p['expiry'])}",
    ]
    return p, printed, (l1, l2)


FIXTURE_FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "fonts", "DejaVuSansMono.ttf")


def visa_image(blur=0, **overrides):
    """A rendered visa-page picture (PIL Image) - what a student photographs."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    _, printed, (l1, l2) = _visa_fields(overrides)

    def font(size):
        # Always the bundled font, so the sample visa looks the same (and is
        # read the same by OCR) on Windows, macOS and Linux.
        return ImageFont.truetype(FIXTURE_FONT, size)

    im = Image.new("RGB", (1400, 900), (236, 242, 236))
    d = ImageDraw.Draw(im)
    y = 40
    for line in printed:
        d.text((60, y), line, fill=(20, 20, 60), font=font(30))
        y += 60
    d.text((40, 720), l1, fill=(0, 0, 0), font=font(34))
    d.text((40, 790), l2, fill=(0, 0, 0), font=font(34))
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    return im


def image_bytes(im, fmt):
    b = io.BytesIO()
    if fmt == "JPEG":
        im.save(b, "JPEG", quality=80)
    else:
        im.save(b, fmt)
    return b.getvalue()


def valid_case(fmt="pdf", blur=0, **overrides):
    """A consistent visa: form fields + matching document.
    fmt: 'pdf' (searchable text PDF), 'jpg', 'png', or 'scanpdf' (image-only PDF)."""
    p, printed, (l1, l2) = _visa_fields(overrides)
    if fmt == "pdf":
        data = make_pdf(printed + [l1, l2])
    elif fmt in ("jpg", "jpeg"):
        data = image_bytes(visa_image(blur, **overrides), "JPEG")
    elif fmt == "png":
        data = image_bytes(visa_image(blur, **overrides), "PNG")
    elif fmt == "scanpdf":
        data = image_bytes(visa_image(blur, **overrides), "PDF")
    else:
        raise ValueError(fmt)
    form = {
        "visa_type_category": "F-1", "passport_number": "AK1234567",
        "visa_issue_date": p["issue"].isoformat(), "visa_expiry_date": p["expiry"].isoformat(),
        "additional_info": "",
    }
    return form, data


FILENAMES = {"pdf": "visa.pdf", "jpg": "visa.jpg", "jpeg": "visa.JPEG", "png": "visa.png",
             "scanpdf": "visa-scan.pdf"}


PNG_BYTES = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (800).to_bytes(4, "big")
             + (600).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"\x00" * 4 + b"\x00" * 4096)


def upload(client, form, file_bytes, filename):
    data = dict(form)
    data["visa_document"] = (io.BytesIO(file_bytes), filename)
    return client.post("/application/visa-document/upload", data=data,
                       content_type="multipart/form-data")


def choose_yes(client):
    return client.post("/application/step/visa", data={"visa_choice": "yes"})


VISA_FORM_ANSWERS = {
    "personal": {"full_name": APPLICANT["full_name"], "date_of_birth": APPLICANT["date_of_birth"], "gender": "Female",
                 "citizenship": "Kenyan", "country_of_residence": "Kenya", "national_id_number": "34567890",
                 "marital_status": "Single"},
    "contact": {"email": "amina@example.com", "phone": "0712345678", "alt_phone": "", "current_address": "Ngong Rd",
                "city": "Nairobi", "contact_country": "Kenya"},
    "passport": {"passport_status": "I have a valid passport", "passport_number": "AK1234567",
                 "passport_type": "Ordinary", "passport_issue_date": "2024-01-10", "passport_expiry_date": "2034-01-09",
                 "passport_place_of_issue": "Nairobi", "passport_issuing_country": "Kenya"},
    "visa_info": {"destination_country": "United States of America", "visa_category": "Student Visa",
                  "purpose_of_travel": "Undergraduate study", "intended_arrival_date": "2027-08-15",
                  "intended_departure_date": "2031-06-01", "expected_length_of_stay": "4 years"},
    "education": {"current_status": "Student", "education_level": "Undergraduate",
                  "organization_name": "University of Nairobi", "position_course": "BSc Computer Science",
                  "organization_address": "Nairobi", "organization_contact": "info@uon.ac.ke"},
    "financial": {"trip_payer": "Sponsor", "travel_budget": "USD 5,000", "funding_sources": ["Scholarship", "Family Support"]},
    "accommodation": {"accommodation_type": "University Accommodation", "accommodation_name": "Campus housing",
                      "accommodation_address": "Campus", "accommodation_contact": "housing@example.edu"},
    "travel_history": {"travelled_before": "No", "previous_application": "No"},
    "legal": {"overstayed": "No", "refused_entry": "No", "visa_refused": "No"},
    "documents": {},
    "additional": {"additional_information": "First time applying.", "assistance_required": ["DS-160 Guidance"]},
}


def upload_visa_support_doc(client, request_id, doc_type, data=None, filename="doc.png"):
    payload = {"document_type": doc_type,
               "document": (io.BytesIO(PNG_BYTES if data is None else data), filename)}
    return client.post(f"/student-visa/application/{request_id}/documents/upload", data=payload,
                       content_type="multipart/form-data")


def complete_visa_form(client, request_id, sign=True):
    """Sections 1-9 + the required documents of a form-first visa
    assistance request: the point where payment opens. (Additional
    Information and the Declaration come AFTER payment - see
    finish_visa_form; `sign` is kept for older callers and only signs if
    the request has already been paid.)"""
    for step, answers in VISA_FORM_ANSWERS.items():
        r = client.post(f"/student-visa/application/{request_id}/step/{step}", data=answers)
        assert r.status_code == 302, (step, r.status_code)
    upload_visa_support_doc(client, request_id, "Passport-size Photograph")
    upload_visa_support_doc(client, request_id, "National ID", make_pdf(["NATIONAL ID"]), "national-id.pdf")
    db = get_db()
    optional_rows = db.execute("SELECT id FROM visa_documents WHERE request_id = ? AND is_required = 0", (request_id,)).fetchall()
    db.close()
    answers = {f"document_{row['id']}_availability": "no" for row in optional_rows}
    client.post(f"/student-visa/application/{request_id}/step/documents", data=answers)
    if sign:
        return client.post(f"/student-visa/application/{request_id}/submit",
                           data={"declaration_name": APPLICANT["full_name"], "declaration_confirmed": "yes"})


def finish_visa_form(client, request_id):
    """After a verified payment: 11. Additional Information + 12. Declaration."""
    client.post(f"/student-visa/application/{request_id}/step/additional", data=VISA_FORM_ANSWERS["additional"])
    return client.post(f"/student-visa/application/{request_id}/submit",
                       data={"declaration_name": APPLICANT["full_name"], "declaration_confirmed": "yes"})



def pytest_report_header(config):
    """Say up front whether the visa OCR reader can run here. Without it
    (e.g. Python 3.13+, where rapidocr-onnxruntime 1.4.4 cannot be
    installed) visa verification FAILS CLOSED, so the tests that need to
    read photos/scans fail. Use Python 3.11, as on Render (.python-version)."""
    import importlib.util
    ok = importlib.util.find_spec("rapidocr_onnxruntime") is not None
    return (f"visa OCR reader (rapidocr_onnxruntime): {'available' if ok else 'NOT AVAILABLE'} | "
            f"Python {sys.version.split()[0]} (project uses 3.11 - see .python-version)")
