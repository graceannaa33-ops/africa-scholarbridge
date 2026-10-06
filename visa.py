"""
visa.py
-------
Helper functions for the U.S. Student Visa Application Assistance service.

Kept separate from app.py so the payment-gating logic (the most
security-sensitive part of the whole platform) lives in one small,
easy-to-review file.

FEE SPONSORSHIP MODEL
-----------------------------
Two money flows exist and must never be confused:

1. STUDENT -> AFRICA SCHOLARBRIDGE (money IN)
   The student pays Africa ScholarBridge a small service fee
   (STUDENT_SERVICE_FEE_USD / configurable per country in visa_pricing,
   e.g. KSh 1,500 for Kenya). Tracked in visa_requests.service_price and
   the visa_payments table.

2. AFRICA SCHOLARBRIDGE -> U.S. GOVERNMENT (money OUT, on the student's
   behalf)
   After that payment is verified, Africa ScholarBridge separately
   arranges/covers the official U.S. government visa application
   processing fee (US_GOV_VISA_FEE_USD, currently US$185) for the
   eligible student. The student never pays Africa ScholarBridge a
   second amount for this. Tracked in visa_requests.visa_fee_* columns
   and the visa_fee_transactions ledger - entirely separate rows from
   the student's own payment.

Africa ScholarBridge's coverage of the $185 fee does not buy or
guarantee a visa - the U.S. government alone decides visa eligibility,
interview outcomes, and issuance. See VISA_FEE_COVERAGE_STATUSES below
for how a request moves through that coverage process.

A NOTE ON THE BUSINESS MODEL (not financial advice, just an implementation
note): the $11.59 the student pays does not on its own cover the $185
Africa ScholarBridge subsequently pays the U.S. government per eligible
student - whoever operates this platform needs a separate, sufficient
source of funds (a grant, a partner organization, a limited/capped
sponsorship pool, eligibility screening, etc.) to make real payouts at
this ratio sustainable. Nothing here verifies that such funding exists;
that is a business decision for the platform operator, not something the
code can guarantee.
"""

import os
import random
from datetime import datetime, timedelta

# The student's Africa ScholarBridge service fee (approximate USD
# reference; the amount actually charged per country lives in the
# visa_pricing table, e.g. KES 1,500 for Kenya).
STUDENT_SERVICE_FEE_USD = 11.59

# The U.S. government's own MRV (Machine Readable Visa) application fee.
# Under this service model, Africa ScholarBridge - not the student -
# arranges/covers this fee for eligible students. Shown for information
# and accounting purposes. Verify current fees against official U.S.
# government sources (https://travel.state.gov) as they can change.
US_GOV_VISA_FEE_USD = 185

DEFAULT_KENYA_PRICE = {
    "country": "Kenya", "currency": "KES", "currency_symbol": "KSh",
    "service_price": 1500, "exchange_rate_reference": "~US$11.59 equivalent - fixed local price",
}

VISA_FEE_COVERAGE_STATUSES = [
    "PENDING", "ELIGIBLE", "PROCESSING", "PAID_BY_AFRICA_SCHOLARBRIDGE", "CONFIRMED", "NOT_APPLICABLE",
]

VISA_FEE_COVERAGE_LABELS = {
    "PENDING": "Pending",
    "ELIGIBLE": "Eligible - Awaiting Processing",
    "PROCESSING": "Processing",
    "PAID_BY_AFRICA_SCHOLARBRIDGE": "Paid by Africa ScholarBridge",
    "CONFIRMED": "Confirmed",
    "NOT_APPLICABLE": "Not Applicable",
}

# The student-facing processing pipeline for the assistance service
# (distinct from the U.S. government's own visa adjudication process).
# This same list backs the Visa Admin's "Processing" status dropdown
# (/visa-admin/processing) - the labels are the ones the student sees
# on their own dashboard, per the "student should see these updates on
# their own dashboard" requirement.
VISA_PIPELINE_STAGES = [
    ("application_unlocked", "Payment Received"),
    ("preparation", "Information Received / Preparation"),
    ("application_review", "Review"),
    ("fee_coverage_processing", "Visa Fee Coverage"),
    ("interview_preparation", "Interview Preparation"),
    ("document_review", "Processing"),
    ("final_guidance", "Final Guidance"),
    ("completed", "Completed"),
]

# The visa assistance application form: 12 sections, in order. The last
# one (declaration) submits the form. The same form is used for every
# visa assistance request; only WHEN payment happens differs (see
# form_is_first below).
VISA_APPLICATION_STEPS = [
    "personal", "contact", "passport", "visa_info", "education", "financial",
    "accommodation", "travel_history", "legal", "documents", "additional", "declaration",
]

VISA_STEP_TITLES = {
    "personal": "Applicant Personal Information",
    "contact": "Contact Information",
    "passport": "Passport Information",
    "visa_info": "Visa Information",
    "education": "Education / Employment Information",
    "financial": "Financial Information",
    "accommodation": "Accommodation Information",
    "travel_history": "Travel History",
    "legal": "Immigration / Legal Questions",
    "documents": "Documents Checklist",
    "additional": "Additional Information",
    "declaration": "Applicant Declaration",
}

# Which visa_requests columns each section saves (plain text fields).
# Multi-select fields are handled separately (MULTI_FIELDS).
VISA_STEP_FIELDS = {
    "personal": ["full_name", "date_of_birth", "gender", "citizenship", "country_of_residence",
                 "national_id_number", "marital_status"],
    "contact": ["email", "phone", "alt_phone", "current_address", "city", "contact_country"],
    "passport": ["passport_status", "passport_number", "passport_type", "passport_issue_date",
                 "passport_expiry_date", "passport_place_of_issue", "passport_issuing_country"],
    "visa_info": ["destination_country", "visa_category", "purpose_of_travel", "intended_arrival_date",
                  "intended_departure_date", "expected_length_of_stay",
                  # optional U.S. study details (kept from the original form)
                  "us_institution", "program", "degree", "intended_start_date", "admission_status",
                  "i20_status", "ds160_status", "sevis_info", "interview_status", "application_type",
                  "previous_us_visa"],
    "education": ["current_status", "education_level", "organization_name", "position_course",
                  "organization_address", "organization_contact"],
    "financial": ["trip_payer", "travel_budget"],
    "accommodation": ["accommodation_type", "accommodation_name", "accommodation_address",
                      "accommodation_contact"],
    "travel_history": ["travelled_before", "countries_visited", "previous_application",
                       "previous_application_date", "previous_visa_approved", "previous_refusal_explanation"],
    "legal": ["overstayed", "overstayed_explanation", "refused_entry", "refused_entry_explanation",
              "visa_refused", "visa_refused_explanation"],
    "documents": [],
    "additional": ["additional_information"],
    "declaration": [],
}
MULTI_FIELDS = {"financial": ["funding_sources"], "additional": ["assistance_required"]}

# Sections that must be filled before the declaration can be submitted.
VISA_REQUIRED_FIELDS = {
    "personal": ["full_name", "date_of_birth", "gender", "citizenship", "country_of_residence"],
    "contact": ["email", "phone"],
    "passport": ["passport_status"],
    # Purpose of Travel is OPTIONAL (examples are offered on the form);
    # the destination country and visa type stay required.
    "visa_info": ["destination_country", "visa_category"],
    "education": ["current_status"],
    "financial": ["trip_payer"],
    "accommodation": ["accommodation_type"],   # "Where will you stay?" (name/address/contact optional)
    "travel_history": ["travelled_before", "previous_application"],
    "legal": ["overstayed", "refused_entry", "visa_refused"],
}

GENDERS = ["Male", "Female", "Other"]
MARITAL_STATUSES = ["Single", "Married", "Divorced", "Widowed"]
PASSPORT_STATUSES = ["I have a valid passport", "I do not currently have a passport"]
PASSPORT_TYPES = ["Ordinary", "Diplomatic", "Official / Service", "Other"]
VISA_TYPES = ["Student Visa", "Tourist Visa", "Work Visa", "Business Visa", "Family/Visitor Visa",
              "Medical Visa", "Transit Visa", "Other"]
DEFAULT_DESTINATION = "United States of America"
DEFAULT_VISA_TYPE = "Student Visa"
CURRENT_STATUSES = ["Student", "Employed", "Self-Employed / Business Owner", "Unemployed", "Other"]
TRIP_PAYERS = ["Myself", "Parent/Guardian", "Sponsor", "Employer", "School/University", "Other"]
FUND_SOURCES = ["Employment Income", "Business Income", "Savings", "Scholarship", "Family Support",
                "Sponsorship", "Other"]
# Suggested answers for the optional Purpose of Travel field. Tapping one only
# fills the text box; the applicant can edit it, type their own, or leave it blank.
PURPOSE_OF_TRAVEL_SUGGESTIONS = ["Educational purposes", "Study", "University/College education",
                                 "Attending an academic program", "Research", "Training", "Other"]
ACCOMMODATION_TYPES = ["Hotel", "University Accommodation", "With Family/Friend", "Rented Accommodation", "Other"]
YES_NO = ["Yes", "No"]
ASSISTANCE_OPTIONS = ["DS-160 Guidance", "Document Preparation", "Application Review", "Appointment Guidance",
                      "Interview Preparation", "General Guidance", "Full Assistance"]

# Supporting documents for the visa assistance application. Uploading is
# optional per item ("if applicable"); the identity rule below decides
# what MUST be present before the declaration can be submitted.
VISA_DOCUMENT_CHECKLIST = [
    ("Valid Passport", False),
    ("Passport-size Photograph", True),
    ("National ID", True),
    ("Bank Statement / Proof of Funds", False),
    ("Flight Itinerary / Travel Reservation", False),
    ("Accommodation Booking", False),
    ("Travel Medical Insurance", False),
    ("Employment/Business/Student Proof", False),
    ("Invitation Letter", False),
    ("Sponsorship Letter", False),
    ("Admission Letter", False),
    ("Other Supporting Documents", False),
]


def form_is_first(visa_request):
    """True for requests raised from the final step of the annual funding
    application: the form, documents and declaration come BEFORE payment."""
    return bool(visa_request) and bool(visa_request["form_first"])


def form_submitted(visa_request):
    return bool(visa_request) and bool(visa_request["form_submitted_at"])


def missing_required_fields(visa_request):
    """[(step, field), ...] still empty before the declaration is allowed."""
    missing = []
    for step, fields in VISA_REQUIRED_FIELDS.items():
        for f in fields:
            if not (visa_request[f] or "").strip():
                missing.append((step, f))
    return missing


# Which supporting documents MUST be uploaded before the declaration.
# This is a configurable policy, not something the specification fixed:
#   VISA_REQUIRED_DOCUMENTS=photo,identity   (default)
#       photo    -> Passport-size Photograph
#       identity -> a Valid Passport copy, or a National ID when the
#                   applicant says they do not currently have a passport
#   VISA_REQUIRED_DOCUMENTS=photo   /   =identity   -> only that one
#   VISA_REQUIRED_DOCUMENTS=none                   -> every document optional
# Unknown words are ignored. Read on every check, so a change takes effect
# on the next request after the environment is updated (Render: restart).
DEFAULT_REQUIRED_DOCUMENTS = "photo,identity"


def required_document_rules():
    raw = os.environ.get("VISA_REQUIRED_DOCUMENTS", DEFAULT_REQUIRED_DOCUMENTS)
    words = {w.strip().lower() for w in raw.replace(";", ",").split(",") if w.strip()}
    if "none" in words:
        return set()
    return words & {"photo", "identity"}


def required_document_types(visa_request):
    """The two visa-assistance documents that are always required."""
    return [document_type for document_type, required in VISA_DOCUMENT_CHECKLIST if required]


def missing_required_documents(visa_request, documents):
    """Required document types not uploaded yet."""
    uploaded = {d["document_type"] for d in documents if d["stored_file"]}
    return [n for n in required_document_types(visa_request) if n not in uploaded]


def missing_selected_documents(visa_request, documents):
    """Documents that block the next step (and the payment gate).

    Required documents must always be uploaded. An optional document blocks
    ONLY when the applicant explicitly selected Yes and has not uploaded it.
    Optional + unanswered (NULL) is not treated as available and never
    blocks; optional + No never blocks.
    """
    missing = []
    for d in documents:
        if d["is_required"]:
            if not d["stored_file"]:
                missing.append(f"{d['document_type']} (required)")
        elif (d["availability"] or "").lower() == "yes" and not d["stored_file"]:
            missing.append(f"{d['document_type']} (you selected Yes)")
    return missing


def documents_complete(visa_request, documents):
    """Sections 1-9 complete and every required/selected document is ready.
    Optional documents are required only after an applicant selects Yes;
    selecting No is a valid completion state."""
    return not missing_required_fields(visa_request) and not missing_selected_documents(visa_request, documents)


def payment_ready(visa_request, documents):
    """May this request be paid for now?
      * standalone (pay-first) requests: always;
      * older form-first requests whose declaration is already signed: yes;
      * form-first requests: only after documents_complete()."""
    if not form_is_first(visa_request) or form_submitted(visa_request):
        return True
    return documents_complete(visa_request, documents)


VISA_DISPLAY_STATUSES = ["Not Required Yet", "Visa Already Provided", "Visa Assistance Required",
                         "Visa Form Submitted", "Awaiting Payment", "Paid", "Under Review", "Completed"]
_REVIEW_STAGES = {"preparation", "application_review", "fee_coverage_processing", "interview_preparation",
                  "document_review", "final_guidance", "information_required"}


def visa_display_status(application, visa_request=None, latest_payment=None):
    """One plain label for admins, from the application + visa request."""
    if application is None:
        return "Not Required Yet"
    if application["visa_status"] == "HAS_VISA":
        return "Visa Already Provided" if application["visa_step_status"] == "COMPLETE" else "Not Required Yet"
    if application["visa_status"] != "NEEDS_ASSISTANCE" or visa_request is None:
        return "Not Required Yet"
    if visa_request["application_status"] == "completed":
        return "Completed"
    if visa_request["application_status"] in _REVIEW_STAGES:
        return "Under Review"
    if is_unlocked(visa_request):
        return "Paid"
    if latest_payment is not None and latest_payment["payment_status"] == "PAYMENT_PENDING":
        return "Awaiting Payment"          # payment started / proof submitted, not yet confirmed
    if form_is_first(visa_request) and not form_submitted(visa_request):
        return "Visa Assistance Required"
    return "Visa Form Submitted" if form_is_first(visa_request) else "Visa Assistance Required"


def get_pricing_for_country(db, country):
    """Return the visa_pricing row for a country, falling back to Kenya's
    default price if the country has no configured pricing yet.
    """
    row = None
    if country:
        row = db.execute(
            "SELECT * FROM visa_pricing WHERE country = ? AND active = 1", (country,)
        ).fetchone()
    if row:
        return row
    return db.execute(
        "SELECT * FROM visa_pricing WHERE country = 'Kenya' AND active = 1"
    ).fetchone()


def generate_visa_request_number(db, year):
    """Format: ASB-VISA-[YEAR]-[6 DIGIT NUMBER], guaranteed unique."""
    while True:
        number = random.randint(0, 999999)
        ref = f"ASB-VISA-{year}-{number:06d}"
        exists = db.execute(
            "SELECT id FROM visa_requests WHERE request_number = ?", (ref,)
        ).fetchone()
        if not exists:
            return ref


def add_visa_history(db, request_id, status, note=""):
    db.execute(
        "INSERT INTO visa_status_history (request_id, status, note) VALUES (?, ?, ?)",
        (request_id, status, note),
    )


def add_visa_note(db, request_id, note, admin_id=None, visible_to_student=True):
    db.execute(
        "INSERT INTO visa_notes (request_id, admin_id, note, visible_to_student) VALUES (?, ?, ?, ?)",
        (request_id, admin_id, note, 1 if visible_to_student else 0),
    )


def is_unlocked(visa_request):
    """True only after an authorized admin has approved the payment proof."""
    return bool(visa_request) and visa_request["payment_status"] == "paid" and bool(visa_request["payment_verified"])

def can_continue_application(visa_request):
    """Server-side gate for every visa application route.

    Previously a student could start filling the form as soon as they
    SUBMITTED payment proof. That is no longer allowed: submitted proof
    (an SMS or screenshot) is not proof of payment, so the application
    stays locked until an admin has verified the payment - i.e. exactly
    the same rule as is_unlocked()."""
    return is_unlocked(visa_request)

def start_processing_timeline(db, request_id, processing_days=14):
    """Called only once, when the student submits their completed visa
    application (never when payment succeeds - the 2-week Africa
    ScholarBridge assistance clock starts once we actually have the
    application information to work with).
    """
    now = datetime.utcnow()
    completion = now + timedelta(days=processing_days)
    db.execute(
        """UPDATE visa_requests
           SET application_status = 'preparation',
               application_submitted_at = ?, processing_start_date = ?,
               estimated_completion_date = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
         completion.isoformat(timespec="seconds"), request_id),
    )
    mark_submission_complete(db, request_id)


def mark_submission_complete(db, request_id):
    """Called once the student has submitted a fully-completed visa
    assistance application AND their service-fee payment is verified
    (this function is only ever reached through the payment-gated
    /student-visa/application/<id>/submit route, so both conditions
    already hold by the time we get here).

    submission_status = 'complete' / automatically_approved = 1 describe
    Africa ScholarBridge's OWN internal submission-completion state only.
    This is NOT an official U.S. government visa approval or decision of
    any kind - the U.S. government makes that decision separately,
    entirely outside this platform.
    """
    db.execute(
        "UPDATE visa_requests SET submission_status = 'complete', automatically_approved = 1, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (request_id,),
    )
    add_visa_history(db, request_id, "submission_complete",
                      "Africa ScholarBridge submission automatically marked complete "
                      "(internal completion only - not a U.S. government visa decision).")


def processing_tracker(visa_request, processing_days=14):
    """Return a dict describing where a visa request is in the 14-day
    Africa ScholarBridge processing timeline (not a U.S. government
    timeline - see the disclaimer shown throughout the visa pages).
    """
    if not visa_request["processing_start_date"]:
        return None

    start = datetime.fromisoformat(visa_request["processing_start_date"])
    now = datetime.utcnow()
    elapsed_days = (now - start).days
    current_day = max(1, min(processing_days, elapsed_days + 1))
    days_remaining = max(0, processing_days - elapsed_days)
    completion = datetime.fromisoformat(visa_request["estimated_completion_date"])

    if visa_request["application_status"] == "completed":
        week_label = "Completed"
    elif current_day <= 7:
        week_label = "Week 1 — Application Preparation"
    else:
        week_label = "Week 2 — Final Review & Assistance"

    stage_label = dict(VISA_PIPELINE_STAGES).get(visa_request["application_status"], week_label)

    return {
        "current_day": current_day,
        "total_days": processing_days,
        "days_remaining": days_remaining,
        "estimated_completion": completion,
        "week_label": week_label,
        "stage_label": stage_label,
        "percent": round((current_day / processing_days) * 100),
    }


def format_price(visa_request_or_pricing):
    row = visa_request_or_pricing
    symbol = row["currency_symbol"]
    price = row["service_price"]
    if float(price).is_integer():
        price = int(price)
    return f"{symbol} {price:,}"


def mark_fee_eligible(db, request_id):
    """Called the moment the student's OWN payment is verified. This does
    NOT pay the U.S. government fee - it only marks the student eligible
    for Africa ScholarBridge to do so; an admin still has to actually
    arrange/record that payment (see record_fee_coverage_payment).
    """
    db.execute(
        "UPDATE visa_requests SET visa_fee_coverage_status = 'ELIGIBLE', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (request_id,),
    )
    add_visa_history(db, request_id, "fee_coverage_eligible",
                      "Student payment verified - eligible for Africa ScholarBridge to cover the U.S. visa application fee.")


def update_fee_coverage_status(db, request_id, status, admin_id=None, note=None):
    if status not in VISA_FEE_COVERAGE_STATUSES:
        raise ValueError(f"Unknown visa_fee_coverage_status: {status}")
    db.execute(
        "UPDATE visa_requests SET visa_fee_coverage_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, request_id),
    )
    db.execute(
        """INSERT INTO visa_fee_transactions (request_id, amount, currency, status, admin_id, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (request_id, US_GOV_VISA_FEE_USD, "USD", status, admin_id, note),
    )
    add_visa_history(db, request_id, f"fee_coverage_{status.lower()}", note or "")


def record_fee_coverage_payment(db, request_id, admin_id, payment_reference, official_reference,
                                 payment_date, notes, receipt_path=None):
    """Admin records that Africa ScholarBridge has actually paid the
    US$185 government fee on the student's behalf. This is a bookkeeping
    record of Africa ScholarBridge's OWN outgoing payment - never a
    second charge to the student.
    """
    db.execute(
        """UPDATE visa_requests
           SET visa_fee_coverage_status = 'PAID_BY_AFRICA_SCHOLARBRIDGE',
               visa_fee_payment_reference = ?, visa_fee_official_reference = ?,
               visa_fee_payment_date = ?, visa_fee_receipt_path = ?, visa_fee_notes = ?,
               visa_fee_handled_by = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (payment_reference, official_reference, payment_date, receipt_path, notes, admin_id, request_id),
    )
    db.execute(
        """INSERT INTO visa_fee_transactions
           (request_id, amount, currency, status, payment_reference, official_reference,
            payment_date, receipt_path, admin_id, notes)
           VALUES (?, ?, 'USD', 'PAID_BY_AFRICA_SCHOLARBRIDGE', ?, ?, ?, ?, ?, ?)""",
        (request_id, US_GOV_VISA_FEE_USD, payment_reference, official_reference, payment_date,
         receipt_path, admin_id, notes),
    )
    add_visa_history(db, request_id, "fee_coverage_paid",
                      f"Africa ScholarBridge paid the US${US_GOV_VISA_FEE_USD} visa application fee "
                      f"(ref: {official_reference or payment_reference or 'n/a'}).")


def refund_eligibility(visa_request):
    """A plain, explainable (not automatically executed) read on whether
    the student's OWN service-fee payment looks refundable, based on the
    factors the platform operator asked for. This never touches the
    US$185 government fee - once Africa ScholarBridge has paid that on
    the student's behalf, it follows the official U.S. government refund
    process instead, not this platform's.

    Returns (eligible: bool, reason: str). The admin still makes and
    records the final refund decision (visa_requests.refund_status) -
    this is guidance, not an automatic action.
    """
    if visa_request["payment_status"] != "paid":
        return True, "No verified student payment has been taken yet."
    if visa_request["visa_fee_coverage_status"] in ("PAID_BY_AFRICA_SCHOLARBRIDGE", "CONFIRMED"):
        return False, "Africa ScholarBridge has already paid the U.S. government visa fee for this student."
    if visa_request["application_status"] in ("preparation", "application_review", "fee_coverage_processing",
                                               "interview_preparation", "completed"):
        return False, "Assistance work has already started on this application."
    return True, "Payment verified, but no assistance work or fee coverage has started yet."


def format_usd(amount):
    if float(amount).is_integer():
        return f"US${int(amount)}"
    return f"US${amount:,.2f}"


# Labels for showing a submitted form (Visa Admin only - this includes
# passport/ID and financial details).
VISA_FIELD_LABELS = {
    "full_name": "Full Name", "date_of_birth": "Date of Birth", "gender": "Gender", "citizenship": "Nationality",
    "country_of_residence": "Country of Residence", "national_id_number": "National ID Number",
    "marital_status": "Marital Status", "email": "Email Address", "phone": "Phone Number",
    "alt_phone": "Alternative Phone Number", "current_address": "Current Address", "city": "City/Town",
    "contact_country": "Country", "passport_status": "Passport", "passport_number": "Passport Number",
    "passport_type": "Passport Type", "passport_issue_date": "Date of Issue", "passport_expiry_date": "Date of Expiry",
    "passport_place_of_issue": "Place of Issue", "passport_issuing_country": "Issuing Country",
    "destination_country": "Country to Visit", "visa_category": "Visa Type", "purpose_of_travel": "Purpose of Travel",
    "intended_arrival_date": "Intended Arrival", "intended_departure_date": "Intended Departure",
    "expected_length_of_stay": "Expected Length of Stay", "us_institution": "U.S. Institution", "program": "Program",
    "degree": "Degree", "intended_start_date": "Intended Start Date", "admission_status": "Admission Status",
    "i20_status": "I-20 Status", "ds160_status": "DS-160 Status", "sevis_info": "SEVIS Information",
    "interview_status": "Interview Status", "application_type": "Application Type",
    "previous_us_visa": "Previous U.S. Visa", "current_status": "Current Status", "education_level": "Education Level",
    "organization_name": "School/University/Employer/Business", "position_course": "Position/Course",
    "organization_address": "Address", "organization_contact": "Phone/Email", "trip_payer": "Who will pay",
    "travel_budget": "Estimated Travel Budget", "funding_sources": "Source of Funds",
    "accommodation_type": "Where will you stay", "accommodation_name": "Hotel/Host/Accommodation",
    "accommodation_address": "Address", "accommodation_contact": "Phone/Email",
    "travelled_before": "Travelled outside country before", "countries_visited": "Countries Visited",
    "previous_application": "Previously applied to destination", "previous_application_date": "Date of Previous Application",
    "previous_visa_approved": "Was the visa approved", "previous_refusal_explanation": "Refusal explanation",
    "overstayed": "Overstayed / violated immigration rules", "overstayed_explanation": "Explanation",
    "refused_entry": "Refused entry to another country", "refused_entry_explanation": "Explanation",
    "visa_refused": "Visa application refused before", "visa_refused_explanation": "Explanation",
    "additional_information": "Additional Information", "assistance_required": "Assistance Requested",
}
