"""The exact bug reported from the live site: deliberately WRONG visa
information plus a document that is not a visa was answered with
"Visa uploaded successfully". It must fail automatically, show no
success, block every way forward, and land in visa assistance / M-PESA.
"""
from datetime import date, timedelta

from conftest import (APPLICANT, PNG_BYTES, choose_yes, count_applications, get_application,
                      upload, visa_requests_for)


def test_wrong_visa_information_fails_and_goes_to_visa_assistance(client, student):
    choose_yes(client)
    wrong = {
        "visa_type_category": "XYZ",           # not a U.S. student visa class
        "passport_number": "ABC123",           # does not belong to this applicant
        "visa_issue_date": "",
        "visa_expiry_date": (date.today() + timedelta(days=400)).isoformat(),
        "additional_info": "",
    }
    r = upload(client, wrong, PNG_BYTES, "holiday-photo.png")  # any picture, not a visa

    # -> automatically sent into the existing visa assistance / M-PESA flow
    assert r.status_code == 302
    assert "/student-visa/" in r.headers["Location"] and "payment" in r.headers["Location"]
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "We could not verify your U.S. visa" in page          # failure message shown
    assert "uploaded successfully" not in page.lower()           # no success message
    assert "verified successfully" not in page.lower()

    app_row = get_application(student["student_id"])
    assert app_row["visa_step_status"] != "COMPLETE"             # step not complete
    assert app_row["visa_status"] == "NEEDS_ASSISTANCE"          # "I do not have a visa" path
    assert app_row["visa_verification_status"] == "FAILED"
    assert app_row["visa_document_path"] is None                 # nothing stored
    assert app_row["full_name"] == APPLICANT["full_name"]        # application data kept
    assert app_row["date_of_birth"] == APPLICANT["date_of_birth"]
    assert count_applications(student["student_id"]) == 1       # no duplicate application
    assert len(visa_requests_for(app_row["id"])) == 1            # one assistance request

    # no continuing
    r = client.get("/application/step/preferences")
    assert r.status_code == 302 and r.headers["Location"].endswith("/application/step/visa")
    r = client.post("/application/submit")
    assert r.headers["Location"].endswith("/application/step/visa")
    assert get_application(student["student_id"])["status"] == "Draft"
