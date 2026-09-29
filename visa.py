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

VISA_APPLICATION_STEPS = [
    "personal", "education", "visa_info", "financial", "documents", "assistance", "review",
]

VISA_STEP_TITLES = {
    "personal": "Personal Information",
    "education": "Education",
    "visa_info": "Visa Information",
    "financial": "Financial Information",
    "documents": "Documents",
    "assistance": "Assistance Required",
    "review": "Final Review",
}

VISA_DOCUMENT_CHECKLIST = [
    ("Valid Passport", True),
    ("DS-160 Confirmation Page", True),
    ("Visa Appointment Confirmation", False),
    ("Admission Letter", True),
    ("I-20 / School Documentation", True),
    ("SEVIS Fee Receipt (I-901)", False),
    ("Academic Records / Transcripts", True),
    ("Financial Evidence", False),
    ("Other Supporting Documents", False),
]


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
