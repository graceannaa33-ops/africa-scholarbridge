"""Automatic U.S. visa verification: every failure type, the valid path,
duplicate protection, and bypass attempts."""
import os
from datetime import date, timedelta

import pytest

import visa_verification as vv
from conftest import (PNG_BYTES, UPLOAD_DIR, choose_yes, count_applications, get_application,
                      make_mrz, make_pdf, upload, valid_case, visa_requests_for)
from database import get_db


def _assert_failed_to_assistance(client, student, r):
    assert r.status_code == 302, r.status_code
    loc = r.headers["Location"]
    assert "/student-visa/" in loc and "payment" in loc, loc
    page = client.get(loc).get_data(as_text=True)
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
    nxt = client.get("/application/step/preferences")
    assert nxt.headers["Location"].endswith("/application/step/visa")
    return row


# ---------------------------------------------------------------------
# Valid submission: the ONLY way to see the success message
# ---------------------------------------------------------------------
def test_valid_visa_is_verified_and_student_can_continue(client, student):
    choose_yes(client)
    form, pdf = valid_case()
    before = set(os.listdir(UPLOAD_DIR)) if os.path.isdir(UPLOAD_DIR) else set()
    r = upload(client, form, pdf, "my-visa.pdf")
    assert r.headers["Location"].endswith("/application/step/visa")
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "Visa verified successfully" in page
    assert "Visa Verified" in page

    row = get_application(student["student_id"])
    assert row["visa_status"] == "HAS_VISA"
    assert row["visa_step_status"] == "COMPLETE"
    assert row["visa_verification_status"] == "VERIFIED"
    assert row["visa_document_type"] == "F-1"
    assert len(set(os.listdir(UPLOAD_DIR)) - before) == 1       # stored only now
    assert visa_requests_for(row["id"]) == []                    # no assistance request

    r = client.post("/application/step/visa", data={"action": "continue"})
    assert r.headers["Location"].endswith("/application/step/preferences")
    assert client.get("/application/step/preferences").status_code == 200


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
    "photo (unreadable without OCR)": lambda: (valid_case()[0], PNG_BYTES, "visa.png"),
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
    assert client.get(f"/student-visa/payment/{request_id}").status_code == 200


# ---------------------------------------------------------------------
# No bypassing
# ---------------------------------------------------------------------
LATER_STEPS = ["preferences", "statement", "documents", "bank", "review"]


def _assert_blocked(client, student):
    for step in LATER_STEPS:
        assert client.get(f"/application/step/{step}").headers["Location"].endswith("/application/step/visa"), step
        assert client.post(f"/application/step/{step}", data={"personal_statement": "x"}).headers[
            "Location"].endswith("/application/step/visa"), step
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
    assert client.get("/application/step/review").headers["Location"].endswith("/application/step/visa")
    row = get_application(student["student_id"])
    assert row["visa_step_status"] == "ACTION_REQUIRED"
    page = client.get("/application/step/visa").get_data(as_text=True)
    assert "Visa Verified" not in page and "VERIFY VISA" in page


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
    with app_module.app.test_request_context():
        db = get_db()
        vr = db.execute("SELECT * FROM visa_requests WHERE id = ?", (request_id,)).fetchone()
        app_module._verify_visa_payment(db, vr, method="M-Pesa (test)", provider_reference="TEST123ABC")
        db.commit()
        db.close()
    row = get_application(student["student_id"])
    assert row["visa_step_status"] == "COMPLETE" and row["visa_status"] == "NEEDS_ASSISTANCE"
    assert client.post("/application/step/visa", data={"action": "continue"}).headers[
        "Location"].endswith("/application/step/preferences")
    assert client.get("/application/step/preferences").status_code == 200
