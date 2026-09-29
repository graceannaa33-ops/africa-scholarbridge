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
    db = get_db()
    user = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    stu = db.execute("SELECT id FROM students WHERE user_id = ?", (user["id"],)).fetchone()
    db.close()
    return {"email": email, "student_id": stu["id"]}


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


def make_pdf(lines):
    """A small but real one-page PDF whose text layer holds `lines`."""
    def esc(t):
        return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 10 Tf 40 780 Td 14 TL\n" + "".join(f"({esc(l)}) Tj T*\n" for l in lines) + "ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{off:010d} 00000 n \n" for off in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


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


def visa_image(blur=0, **overrides):
    """A rendered visa-page picture (PIL Image) - what a student photographs."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    _, printed, (l1, l2) = _visa_fields(overrides)

    def font(size):
        for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"):
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        return ImageFont.load_default(size=size)

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
    elif fmt == "jpg":
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


FILENAMES = {"pdf": "visa.pdf", "jpg": "visa.jpg", "png": "visa.png", "scanpdf": "visa-scan.pdf"}


PNG_BYTES = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (800).to_bytes(4, "big")
             + (600).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"\x00" * 4 + b"\x00" * 4096)


def upload(client, form, file_bytes, filename):
    data = dict(form)
    data["visa_document"] = (io.BytesIO(file_bytes), filename)
    return client.post("/application/visa-document/upload", data=data,
                       content_type="multipart/form-data")


def choose_yes(client):
    return client.post("/application/step/visa", data={"visa_choice": "yes"})
