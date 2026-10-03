"""Automatic U.S. visa verification: every failure type, the valid path,
duplicate protection, and bypass attempts."""
import os
from datetime import date, timedelta

import pytest

import visa_verification as vv
from conftest import (FILENAMES, PNG_BYTES, UPLOAD_DIR, choose_yes, complete_visa_form, count_applications,
                      finish_visa_form, get_application,
                      image_bytes, make_mrz, make_pdf, upload, valid_case, visa_requests_for)
from database import get_db


def _assert_failed_to_assistance(client, student, r):
    assert r.status_code == 302, r.status_code
    loc = r.headers["Location"]
    # -> USA Student Visa Assistance: the visa application FORM comes
    #    first (payment only after the form, documents and declaration)
    assert "/student-visa/application/" in loc, loc
    page = client.get(loc, follow_redirects=True).get_data(as_text=True)
    assert "USA Student Visa Assistance" in page
    assert "We could not verify your U.S. visa" in page
    assert "uploaded successfully" not in page.lower()
    assert "verified successfully" not in page.lower()
    row = get_application(student["student_id"])
    assert row["visa_status"] == "NEEDS_ASSISTANCE"
    assert row["visa_step_status"] == "ACTION_REQUIRED"
    assert row["visa_verification_status"] == "FAILED"
    assert row["visa_document_path"] is None
    assert len(visa_requests_for(row["id"])) == 1
    assert count_applications(student["student_id"]) == 1
    nxt = client.post("/application/submit")                      # cannot submit without a passed visa step
    assert nxt.headers["Location"].endswith("/application/step/visa")
    assert get_application(student["student_id"])["status"] == "Draft"
    return row


# ---------------------------------------------------------------------
# Valid submission: the ONLY way to see the success message
# ---------------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["pdf", "jpg", "jpeg", "png", "scanpdf"])
def test_valid_visa_is_verified_and_student_can_continue(client, student, fmt):
    """1-3: valid readable PDF (text), JPG, PNG - plus an image-only scanned PDF."""
    choose_yes(client)
    form, data = valid_case(fmt)
    before = set(os.listdir(UPLOAD_DIR)) if os.path.isdir(UPLOAD_DIR) else set()
    r = upload(client, form, data, FILENAMES[fmt])
    assert r.headers["Location"].endswith("/application/step/visa"), r.headers["Location"]
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "Visa information verified successfully" in page
    assert "Visa Information Verified" in page
    assert "not an authentication of your visa by the U.S. government" in page

    row = get_application(student["student_id"])
    assert row["visa_status"] == "HAS_VISA"
    assert row["visa_step_status"] == "COMPLETE"
    assert row["visa_verification_status"] == "VERIFIED"
    assert row["visa_document_type"] == "F-1"
    assert len(set(os.listdir(UPLOAD_DIR)) - before) == 1       # stored only now
    assert visa_requests_for(row["id"]) == []                    # no assistance request

    r = client.post("/application/step/visa", data={"action": "continue"})
    assert r.headers["Location"].endswith("/application/submit")       # visa was the LAST step
    assert client.get("/application/submit").status_code == 200


def test_valid_visa_with_ten_char_passport_overflow(client, student):
    """ICAO overflow rule for passport numbers longer than 9 characters."""
    issue, expiry = date.today() - timedelta(days=30), date.today() + timedelta(days=700)
    pp = "AB12345678"
    field9, rest = pp[:9], pp[9:]
    l1, _ = make_mrz("OTIENO", "AMINA WANJIRU", "X", "KEN", "030514", "F", expiry.strftime("%y%m%d"))
    from conftest import _cd
    l2 = (field9 + "<" + "KEN" + "030514" + _cd("030514") + "F" + expiry.strftime("%y%m%d")
          + _cd(expiry.strftime("%y%m%d")) + (rest + _cd(pp)).ljust(16, "<"))
    pdf = make_pdf(["Visa Type/Class R F1", f"Issue Date {issue.strftime('%d%b%Y').upper()}", l1, l2])
    choose_yes(client)
    r = upload(client, {"visa_type_category": "F-1", "passport_number": pp,
                        "visa_issue_date": issue.isoformat(), "visa_expiry_date": expiry.isoformat()},
               pdf, "visa.pdf")
    assert r.headers["Location"].endswith("/application/step/visa")
    assert get_application(student["student_id"])["visa_verification_status"] == "VERIFIED"


# ---------------------------------------------------------------------
# Every kind of wrong / incomplete / unverifiable submission
# ---------------------------------------------------------------------
def _today(n):
    return date.today() + timedelta(days=n)


def _tampered_pdf():
    """Genuine-looking visa whose passport check digit has been altered."""
    from conftest import _cd
    pdf = valid_case()[1]
    good = b"AK1234567" + _cd("AK1234567").encode()
    bad = b"AK1234567" + str((int(_cd("AK1234567")) + 1) % 10).encode()
    assert good in pdf
    return pdf.replace(good, bad)


def _not_a_visa_png():
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1200, 800), (250, 250, 250))
    d = ImageDraw.Draw(im)
    for i, t in enumerate(["BOARDING PASS", "NAIROBI -> NEW YORK", "PASSENGER OTIENO AMINA", "SEAT 23A F1"]):
        d.text((80, 80 + 90 * i), t, fill=(0, 0, 0))
    return image_bytes(im, "PNG")


def _noise_png():
    from PIL import Image
    return image_bytes(Image.frombytes("RGB", (800, 600), os.urandom(800 * 600 * 3)), "PNG")


FAILURES = {
    "non-student visa class (B-2)": lambda: (dict(valid_case()[0], visa_type_category="B-2"), valid_case()[1], "v.pdf"),
    "garbage visa class": lambda: (dict(valid_case()[0], visa_type_category="XYZ"), valid_case()[1], "v.pdf"),
    "passport number not on visa": lambda: (dict(valid_case()[0], passport_number="BK7654321"), valid_case()[1], "v.pdf"),
    "bad passport format": lambda: (dict(valid_case()[0], passport_number="AK-12"), valid_case()[1], "v.pdf"),
    "missing passport": lambda: (dict(valid_case()[0], passport_number=""), valid_case()[1], "v.pdf"),
    "missing expiry": lambda: (dict(valid_case()[0], visa_expiry_date=""), valid_case()[1], "v.pdf"),
    "missing issue date": lambda: (dict(valid_case()[0], visa_issue_date=""), valid_case()[1], "v.pdf"),
    "invalid date text": lambda: (dict(valid_case()[0], visa_expiry_date="2027-02-30"), valid_case()[1], "v.pdf"),
    "expired visa": lambda: (*valid_case(issue=_today(-900), expiry=_today(-5)), "v.pdf"),
    "issue date in future": lambda: (dict(valid_case()[0], visa_issue_date=_today(10).isoformat()), valid_case()[1], "v.pdf"),
    "issue after expiry": lambda: (dict(valid_case()[0], visa_issue_date=_today(-1).isoformat(),
                                        visa_expiry_date=_today(-2).isoformat()), valid_case()[1], "v.pdf"),
    "validity over 10 years": lambda: (dict(valid_case()[0], visa_expiry_date=_today(365 * 11).isoformat()),
                                       valid_case()[1], "v.pdf"),
    "entered expiry differs from visa": lambda: (dict(valid_case()[0], visa_expiry_date=_today(901).isoformat()),
                                                 valid_case()[1], "v.pdf"),
    "entered issue date differs from visa": lambda: (dict(valid_case()[0], visa_issue_date=_today(-199).isoformat()),
                                                     valid_case()[1], "v.pdf"),
    "visa class on document differs": lambda: (valid_case()[0], valid_case(visa_class="J1")[1], "v.pdf"),
    "visa belongs to someone else (name)": lambda: (valid_case()[0], valid_case(surname="KAMAU", given="JOHN")[1], "v.pdf"),
    "visa date of birth differs": lambda: (valid_case()[0], valid_case(dob=date(1999, 1, 1))[1], "v.pdf"),
    "no document": lambda: (valid_case()[0], b"", ""),
    "unsupported file type": lambda: (valid_case()[0], b"hello " * 200, "visa.txt"),
    "renamed file (exe as pdf)": lambda: (valid_case()[0], b"MZ\x90\x00" + b"\x00" * 2000, "visa.pdf"),
    "too small file": lambda: (valid_case()[0], b"%PDF-1.4\n%%EOF", "visa.pdf"),
    "corrupted pdf": lambda: (valid_case()[0], b"%PDF-1.4\n" + os.urandom(3000), "visa.pdf"),
    "corrupt image (PNG header, no picture)": lambda: (valid_case()[0], PNG_BYTES, "visa.png"),
    "corrupt JPG (truncated)": lambda: (valid_case()[0], valid_case("jpg")[1][:3000], "visa.jpg"),
    # ---- the same checks on PHOTOS (real OCR) ----
    "JPG: passport number not on visa": lambda: (dict(valid_case()[0], passport_number="BK7654321"),
                                                 valid_case("jpg")[1], "visa.jpg"),
    "JPG: expired visa": lambda: (*valid_case("jpg", issue=_today(-900), expiry=_today(-5)), "visa.jpg"),
    "PNG: visa belongs to someone else (name)": lambda: (valid_case()[0],
                                                         valid_case("png", surname="KAMAU", given="JOHN")[1], "visa.png"),
    "PNG: visa date of birth differs": lambda: (valid_case()[0], valid_case("png", dob=date(1999, 1, 1))[1], "visa.png"),
    "JPG: wrong visa class entered": lambda: (dict(valid_case()[0], visa_type_category="J-1"),
                                              valid_case("jpg")[1], "visa.jpg"),
    "JPG: blurry photo": lambda: (*valid_case("jpg", blur=4), "visa.jpg"),
    "JPG: slightly blurry photo (name misread)": lambda: (*valid_case("jpg", blur=1.6), "visa.jpg"),
    "PNG: picture that is not a visa": lambda: (valid_case()[0], _not_a_visa_png(), "photo.png"),
    "PNG: random noise": lambda: (valid_case()[0], _noise_png(), "visa.png"),
    "scanned PDF: blurry": lambda: (*valid_case("scanpdf", blur=4), "visa.pdf"),
    "pdf with no visa MRZ": lambda: (valid_case()[0], make_pdf(["My holiday itinerary", "F1 race tickets"] + ["x" * 40] * 5), "v.pdf"),
    "tampered MRZ check digit": lambda: (valid_case()[0], _tampered_pdf(), "v.pdf"),
}


@pytest.mark.parametrize("case", sorted(FAILURES))
def test_every_invalid_submission_fails_automatically(client, student, case):
    form, data, filename = FAILURES[case]()
    choose_yes(client)
    r = upload(client, form, data, filename)
    _assert_failed_to_assistance(client, student, r)


def test_oversized_upload_fails_to_assistance(client, student):
    choose_yes(client)
    form, _ = valid_case()
    r = upload(client, form, b"%PDF-1.4\n" + b"0" * (9 * 1024 * 1024), "huge.pdf")
    _assert_failed_to_assistance(client, student, r)


# ---------------------------------------------------------------------
# No duplicates, data preserved, M-PESA flow reached
# ---------------------------------------------------------------------
def test_failures_never_duplicate_requests_or_applications(client, student):
    choose_yes(client)
    form, _ = valid_case()
    r = upload(client, dict(form, visa_type_category="B-1"), valid_case()[1], "v.pdf")
    row = _assert_failed_to_assistance(client, student, r)
    request_id = visa_requests_for(row["id"])[0]["id"]

    # Repeat everything a student might click/refresh.
    upload(client, form, valid_case()[1], "v.pdf")                 # re-upload is ignored now
    client.post("/application/step/visa", data={"visa_choice": "no"})
    client.post("/application/step/visa", data={"visa_choice": "yes"})
    client.get("/application/start")
    rows = visa_requests_for(row["id"])
    assert [r_["id"] for r_ in rows] == [request_id]
    assert count_applications(student["student_id"]) == 1
    assert get_application(student["student_id"])["visa_step_status"] != "COMPLETE"
    # payment is not offered before the visa form is completed
    r = client.get(f"/student-visa/payment/{request_id}")
    assert r.status_code == 302 and f"/student-visa/application/{request_id}" in r.headers["Location"]


# ---------------------------------------------------------------------
# No bypassing
# ---------------------------------------------------------------------
def _assert_blocked(client, student):
    """Visa Verification is the last step: nothing - 'continue', the
    final submit - gets past it until the visa requirement has passed."""
    assert client.post("/application/step/visa", data={"action": "continue"}).headers[
        "Location"].endswith("/application/step/visa")
    assert client.post("/application/submit").headers["Location"].endswith("/application/step/visa")
    row = get_application(student["student_id"])
    assert row["status"] == "Draft"
    assert row["visa_step_status"] != "COMPLETE"


def test_cannot_skip_visa_step_without_answering(client, student):
    _assert_blocked(client, student)


def test_cannot_continue_after_choosing_yes_without_verified_visa(client, student):
    choose_yes(client)
    _assert_blocked(client, student)


def test_cannot_continue_on_assistance_path_until_payment_verified(client, student):
    client.post("/application/step/visa", data={"visa_choice": "no"})
    _assert_blocked(client, student)


def test_forged_fields_cannot_mark_visa_complete(client, student):
    choose_yes(client)
    form, _ = valid_case()
    forged = dict(form, visa_step_status="COMPLETE", visa_verification_status="VERIFIED", action="continue")
    r = upload(client, forged, PNG_BYTES, "v.png")
    _assert_failed_to_assistance(client, student, r)


def test_legacy_unverified_upload_is_no_longer_accepted(client, student):
    """Applications marked COMPLETE by the old upload-only code were never
    verified: they are blocked and must verify now."""
    choose_yes(client)
    db = get_db()
    db.execute("""UPDATE funding_applications SET visa_step_status='COMPLETE', visa_document_status='UPLOADED'
                  WHERE student_id=?""", (student["student_id"],))
    db.commit()
    db.close()
    assert client.post("/application/submit").headers["Location"].endswith("/application/step/visa")
    row = get_application(student["student_id"])
    assert row["visa_step_status"] == "ACTION_REQUIRED"
    page = client.get("/application/step/visa").get_data(as_text=True)
    assert "Visa Information Verified" not in page and "VERIFY VISA" in page


# ---------------------------------------------------------------------
# Unit checks of the verifier itself
# ---------------------------------------------------------------------
def test_icao_check_digit_reference_values():
    # ICAO 9303 specimen values
    assert vv.mrz_check_digit("L898902C3") == "6"
    assert vv.mrz_check_digit("740812") == "2"
    assert vv.mrz_check_digit("120415") == "9"


def test_visa_class_normalisation():
    assert vv.normalize_visa_class("f1") == "F-1"
    assert vv.normalize_visa_class(" J-1 ") == "J-1"
    assert vv.normalize_visa_class("m 1") == "M-1"
    assert vv.normalize_visa_class("F-1X") == ""


def test_ocr_digit_slips_still_need_valid_check_digits():
    expiry = date.today() + timedelta(days=500)
    l1, l2 = make_mrz("OTIENO", "AMINA", "AK1234567", "KEN", "030514", "F", expiry.strftime("%y%m%d"))
    slipped = l2[:13] + l2[13:19].replace("0", "O") + l2[19:]
    mrz = vv.find_us_visa_mrz(l1 + "\n" + slipped)
    assert mrz and mrz["dob_raw"] == "030514"
    forged = l2[:13] + "040514" + l2[19:]   # changed DOB, old check digit
    mrz = vv.find_us_visa_mrz(l1 + "\n" + forged)
    assert vv.mrz_check_digit(mrz["dob_raw"]) != mrz["dob_cd"]


def test_verifier_rejects_when_applicant_details_missing():
    form, pdf = valid_case()
    ok, _, errors = vv.verify_visa_submission(form, pdf, "v.pdf", {"full_name": "", "date_of_birth": None})
    assert not ok and any("date of birth" in e for e in errors)


def test_after_failure_existing_mpesa_verification_lets_student_continue(client, student):
    """Failed visa -> assistance -> the EXISTING payment verification
    (_verify_visa_payment, unchanged) completes the step as before."""
    import app as app_module
    choose_yes(client)
    r = upload(client, valid_case()[0], PNG_BYTES, "visa.png")
    row = _assert_failed_to_assistance(client, student, r)
    request_id = visa_requests_for(row["id"])[0]["id"]
    complete_visa_form(client, request_id, sign=False)          # form + documents, then payment
    with app_module.app.test_request_context():
        db = get_db()
        vr = db.execute("SELECT * FROM visa_requests WHERE id = ?", (request_id,)).fetchone()
        app_module._verify_visa_payment(db, vr, method="M-Pesa (test)", provider_reference="TEST123ABC")
        db.commit()
        db.close()
    row = get_application(student["student_id"])
    assert row["visa_payment_status"] == "PAID" and row["visa_status"] == "NEEDS_ASSISTANCE"
    r = finish_visa_form(client, request_id)                    # additional info + declaration
    row = get_application(student["student_id"])
    assert row["visa_step_status"] == "COMPLETE" and row["visa_status"] == "NEEDS_ASSISTANCE"
    assert row["status"] != "Draft" and row["reference_number"]
    assert count_applications(student["student_id"]) == 1 and len(visa_requests_for(row["id"])) == 1
    assert r.headers["Location"].endswith(f"/application/confirmation/{row['id']}")


# ---------------------------------------------------------------------
# Fail closed when the image reader itself can't run
# ---------------------------------------------------------------------
@pytest.mark.parametrize("problem", ["reader missing", "reader times out", "reader over memory limit",
                                     "server busy", "reader crashes"])
def test_valid_photo_still_fails_if_it_cannot_be_read(client, student, monkeypatch, problem):
    """A GENUINE, readable visa photo must still FAIL (never pass) when the
    reader can't do its job - fail closed."""
    if problem == "reader missing":
        monkeypatch.setattr(vv, "_OCR_WORKER", "/nonexistent/visa_ocr_worker.py")
    elif problem == "reader times out":
        monkeypatch.setattr(vv, "OCR_TIMEOUT_SECONDS", 0.2)
    elif problem == "reader over memory limit":
        monkeypatch.setattr(vv, "OCR_MAX_RSS_MB", 20)
    elif problem == "server busy":
        monkeypatch.setattr(vv._OcrLock, "__enter__", lambda self: False)
    else:
        monkeypatch.setattr(vv.sys, "executable", "/bin/false")
    choose_yes(client)
    form, data = valid_case("jpg")
    r = upload(client, form, data, "visa.jpg")
    _assert_failed_to_assistance(client, student, r)


def test_ocr_lookalike_passport_is_only_accepted_for_the_same_number():
    assert vv._same_despite_ocr("AK12345G7", "AK1234567") is True   # G read for 6
    assert vv._same_despite_ocr("AKI234567", "AK1234567") is True   # I read for 1
    assert vv._same_despite_ocr("AK1234568", "AK1234567") is False


@pytest.mark.parametrize("surname,given,fmt", [("KAMAU", "JOHN", "png"), ("LI", "WEI", "jpg")])
def test_short_names_on_photos_are_read(surname, given, fmt):
    """Short names followed by long <<< fillers must still be read from photos."""
    form, data = valid_case(fmt, surname=surname, given=given)
    ok, cleaned, errors = vv.verify_visa_submission(
        form, data, FILENAMES[fmt], {"full_name": f"{given} {surname}", "date_of_birth": "2003-05-14"})
    assert ok, errors
    assert cleaned["read_from"] == "ocr"


# ---------------------------------------------------------------------
# Real-world document variations (each must still pass EVERY check)
# ---------------------------------------------------------------------
def _applicant():
    from conftest import APPLICANT
    return {"full_name": APPLICANT["full_name"], "date_of_birth": APPLICANT["date_of_birth"]}


def _variants():
    import io as _io
    from PIL import Image
    from conftest import _visa_fields, scanner_pdf, visa_image
    im = visa_image()
    _, printed, (l1, l2) = _visa_fields({})
    bad_l2 = l2[:9] + str((int(l2[9]) + 1) % 10) + l2[10:]

    def exif_sideways():
        ex = Image.Exif()
        ex[0x0112] = 6
        b = _io.BytesIO()
        im.rotate(90, expand=True).save(b, "JPEG", quality=80, exif=ex.tobytes())
        return b.getvalue()
    return {
        "JPEG extension (.JPEG)": (valid_case("jpeg")[1], "VISA.JPEG"),
        "scanner PDF with correct text layer": (scanner_pdf(printed + [l1, l2]), "scan.pdf"),
        "scanner PDF whose text layer misread a digit": (scanner_pdf(printed + [l1, bad_l2]), "scan.pdf"),
        "upside-down photo": (image_bytes(im.rotate(180), "JPEG"), "v.jpg"),
        "sideways photo, no orientation data": (image_bytes(im.rotate(90, expand=True), "JPEG"), "v.jpg"),
        "sideways photo with EXIF orientation": (exif_sideways(), "v.jpg"),
        "grayscale PNG": (image_bytes(im.convert("L"), "PNG"), "v.png"),
        "PNG with transparency": (image_bytes(im.convert("RGBA"), "PNG"), "v.png"),
        "CMYK JPEG": (image_bytes(im.convert("CMYK"), "JPEG"), "v.jpg"),
        "large phone photo (4032x2592)": (image_bytes(im.resize((4032, 2592)), "JPEG"), "v.jpg"),
    }


@pytest.mark.parametrize("name", sorted(_variants()))
def test_real_world_variants_pass_all_checks(name):
    data, filename = _variants()[name]
    ok, _, errors = vv.verify_visa_submission(valid_case()[0], data, filename, _applicant())
    assert ok, errors


def test_scanner_pdf_with_bad_text_and_blurry_image_fails():
    from conftest import _visa_fields, scanner_pdf
    _, printed, (l1, l2) = _visa_fields({})
    bad_l2 = l2[:9] + str((int(l2[9]) + 1) % 10) + l2[10:]
    ok, _, _ = vv.verify_visa_submission(valid_case()[0], scanner_pdf(printed + [l1, bad_l2], blur=4),
                                         "scan.pdf", _applicant())
    assert not ok


def test_image_with_too_many_pixels_is_refused_not_decoded():
    from PIL import Image
    big = image_bytes(Image.new("RGB", (7000, 7000), (255, 255, 255)), "PNG")   # 49 MP, small file
    ok, _, errors = vv.verify_visa_submission(valid_case()[0], big, "v.png", _applicant())
    assert not ok and any("too many pixels" in e for e in errors)


@pytest.mark.parametrize("size_mb", [9, 30])
def test_oversized_visa_file_goes_to_assistance_not_a_cut_connection(client, student, size_mb):
    choose_yes(client)
    r = upload(client, valid_case()[0], b"%PDF-1.4\n" + b"0" * (size_mb * 1024 * 1024), "big.pdf")
    _assert_failed_to_assistance(client, student, r)


def test_other_routes_keep_the_8mb_request_limit(client, student):
    """Only the visa upload route accepts a larger request body; everything
    else (e.g. M-PESA payment screenshots) is unchanged."""
    import app as app_module
    with app_module.app.test_request_context("/student-visa/payment/1/submit", method="POST"):
        from flask import request
        assert request.max_content_length == 8 * 1024 * 1024
    with app_module.app.test_request_context("/application/visa-document/upload", method="POST"):
        from flask import request
        assert request.max_content_length == 32 * 1024 * 1024


@pytest.mark.parametrize("printed,visa_class,expected", [
    ("VISA TYPE/CLASS R F1", "F-1", True),
    ("VISATYPE/CLASSRF1", "F-1", True),          # OCR dropped the spaces (proportional fonts, JPEG)
    ("TYPE/CLASS: RF-1", "F-1", True),
    ("VISATYPE/CLASSRFI", "F-1", True),          # 1 read as I
    ("VISATYPE/CLASSRJ1", "F-1", False),         # a different class never matches
    ("VISATYPE/CLASSRB1", "F-1", False),
    ("VISATYPE/CLASSRF12", "F-1", False),        # longer token
    ("VISATYPE/CLASSRRF1", "F-1", False),        # more than one annotation letter
    ("PASSPORTF1", "F-1", False),                # glued to an unrelated word
    ("VISATYPE/CLASSRJ1", "J-1", True),
])
def test_visa_class_found_in_labelled_field_without_spaces(printed, visa_class, expected):
    assert vv.visa_class_printed(printed, visa_class) is expected
