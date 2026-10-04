"""
app.py
------
The main Flask application for Africa ScholarBridge.

This single file contains every route (page) in the website. It is kept
in one file on purpose - for a beginner-sized project this is easier to
read and navigate than splitting into many small blueprint files. As the
project grows, routes could later be split into blueprints per section.

Run it with:  python app.py
"""

import os
import re
import random
import sqlite3
import secrets
import hmac
from datetime import datetime, date
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, g, send_from_directory, abort
)
from werkzeug.security import generate_password_hash, check_password_hash

from database import get_db, init_db, DB_PATH
from matching import run_matching_for_application
import visa as visa_lib
import banks_lib
import email_lib
import mpesa_parser
import paystack_lib
from werkzeug.utils import secure_filename
import visa_verification as visa_verify
import capacity_monitor
import account_moderation
import json

app = Flask(__name__)

# ---------------------------------------------------------------------
# BUILD / VERSION INDICATOR (non-sensitive). Shown in the site footer, on
# the admin dashboards, at /version, and printed when the server starts,
# so it's obvious which copy of the project is actually running.
# ---------------------------------------------------------------------
APP_NAME = "Africa ScholarBridge"
APP_VERSION = "M-PESA Visa Workflow v4 (Render)"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_secret_key():
    """The SECRET_KEY signs login sessions, so it must be secret and must be
    the SAME for every gunicorn worker and across restarts.

    1. SECRET_KEY environment variable (recommended on Render).
    2. Otherwise a random key generated ONCE and stored next to the database
       (on Render: the persistent disk, e.g. /var/data/.flask_secret_key).
       O_EXCL makes creation atomic, so two workers starting together still
       end up sharing one key. The key is never printed or shown anywhere.
    """
    env_key = os.environ.get("SECRET_KEY", "").strip()
    if env_key and env_key != "change-me-to-a-long-random-string":
        return env_key
    key_file = os.path.join(os.path.dirname(DB_PATH), ".flask_secret_key")
    os.makedirs(os.path.dirname(key_file), exist_ok=True)
    try:
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    for _ in range(20):  # another worker may be mid-write
        with open(key_file, "r", encoding="ascii") as fh:
            key = fh.read().strip()
        if len(key) >= 32:
            return key
        import time
        time.sleep(0.05)
    raise RuntimeError(f"Could not read the session key file {key_file}")


app.config["SECRET_KEY"] = _load_secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# ---------------------------------------------------------------------
# 🇺🇸 Visa document uploads (Path A: "Yes, I already have my visa").
#
# These files contain sensitive personal information, so they are stored
# OUTSIDE the `static/` folder (Flask never serves this directory
# directly - there is no public URL for any file in it), under
# randomized filenames that reveal nothing about their contents, and are
# only ever readable through the authenticated view routes below
# (student sees only their own; Visa Admin can see any). The original
# filename the student picked is kept purely for display and is never
# used to build a filesystem path.
# ---------------------------------------------------------------------
# All uploaded files live under UPLOAD_ROOT. Local default: <project>/uploads.
# Production (Render): UPLOAD_ROOT=/var/data/uploads (the persistent disk).
UPLOAD_ROOT = os.environ.get("UPLOAD_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
VISA_DOCS_DIR = os.path.join(UPLOAD_ROOT, "visa_documents")
os.makedirs(VISA_DOCS_DIR, exist_ok=True)
ALLOWED_VISA_DOC_EXTENSIONS = visa_verify.ALLOWED_EXTENSIONS
MAX_VISA_DOC_SIZE_BYTES = visa_verify.MAX_FILE_BYTES  # 8 MB - a reasonable limit for a scanned document/photo
app.config["MAX_CONTENT_LENGTH"] = MAX_VISA_DOC_SIZE_BYTES

# The visa upload route alone accepts a larger REQUEST (files above 500 KB are
# spooled to a temp file, not held in memory) so that an over-8 MB visa file
# is read and then rejected by the normal verification failure path (-> visa
# assistance) instead of the connection being cut mid-upload. Every other
# route - including M-PESA screenshot uploads - keeps MAX_CONTENT_LENGTH.
VISA_UPLOAD_REQUEST_LIMIT = 32 * 1024 * 1024


class _AppRequest(app.request_class):
    @property
    def max_content_length(self):
        if self.path == "/application/visa-document/upload":
            return VISA_UPLOAD_REQUEST_LIMIT
        return super().max_content_length


app.request_class = _AppRequest

# Render terminates HTTPS in front of gunicorn: trust ONLY its
# X-Forwarded-Proto so external links (Paystack callback_url) use https.
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0)

# Capacity monitoring: lightweight request counters + a background sampler
# that e-mails the main admin as the service approaches its limits. It
# never raises into requests (see capacity_monitor.py).
capacity_monitor.init_app(app)

# File-type, size, signature and content checks live in visa_verification.py.


def _visa_doc_extension(filename):
    if "." not in filename:
        return None
    return filename.rsplit(".", 1)[1].lower()


def save_verified_visa_document(file_bytes, ext):
    """Stores a visa document that has ALREADY passed automatic
    verification, under a random, unguessable filename. Nothing is ever
    written to disk for a submission that failed verification."""
    stored_filename = f"{secrets.token_hex(16)}.{ext}"
    with open(os.path.join(VISA_DOCS_DIR, stored_filename), "wb") as fh:
        fh.write(file_bytes)
    return stored_filename


# Shown whenever a "Yes, I already have my visa" submission fails automatic
# verification. The student is then moved straight onto the
# "I do not have a visa" assistance path.
VISA_VERIFICATION_FAILED_MESSAGE = (
    "We could not verify your U.S. visa, so it has not been accepted. You have been moved to "
    "Africa ScholarBridge U.S. visa assistance - your funding application details are kept."
)
VISA_STEP_REQUIRED_MESSAGE = "Please complete the U.S. visa step before continuing your application."
# Kept so sessions created before this change don't error; no longer set.
VISA_FAILED_SESSION_KEY = "visa_verification_failed_app_id"


def _visa_requirement_passed(application):
    """The ONE rule every later application step and the final submission
    use. The visa step counts as passed only when:
      - the student chose "I have a visa" AND that visa passed automatic
        verification (visa_verification_status = 'VERIFIED'), or
      - the student chose visa assistance AND that payment was verified
        (the existing M-PESA flow sets visa_step_status = 'COMPLETE').
    A successful FILE UPLOAD on its own never satisfies it."""
    if application["visa_step_status"] != "COMPLETE":
        return False
    if application["visa_status"] == "NEEDS_ASSISTANCE":
        return True
    if application["visa_status"] == "HAS_VISA":
        return application["visa_verification_status"] == "VERIFIED"
    return False


def _visa_upload_incomplete(application):
    """True when the student chose "Yes, I already have my visa" but no
    visa has passed automatic verification yet."""
    return application["visa_status"] == "HAS_VISA" and not _visa_requirement_passed(application)


def _revoke_unverified_visa_step(db, application):
    """Draft applications whose visa step was marked done under the old,
    upload-only logic (HAS_VISA + COMPLETE but never verified), or the
    legacy NOT_REQUIRED state, were never actually verified. Put them
    back to the point where verification has to happen. Returns the
    (possibly refreshed) application row."""
    if application["status"] != "Draft":
        return application
    if (application["visa_status"] == "HAS_VISA" and application["visa_step_status"] == "COMPLETE"
            and application["visa_verification_status"] != "VERIFIED"):
        db.execute(
            """UPDATE funding_applications
               SET visa_step_status = 'ACTION_REQUIRED', visa_document_status = 'NOT_UPLOADED',
                   last_updated = CURRENT_TIMESTAMP WHERE id = ?""",
            (application["id"],),
        )
    elif application["visa_step_status"] == "NOT_REQUIRED":
        db.execute(
            "UPDATE funding_applications SET visa_step_status = 'NOT_STARTED', last_updated = CURRENT_TIMESTAMP WHERE id = ?",
            (application["id"],),
        )
    else:
        return application
    db.commit()
    return db.execute("SELECT * FROM funding_applications WHERE id = ?", (application["id"],)).fetchone()


def _send_to_visa_assistance(db, student, application, cycle):
    """Moves this SAME application onto the "I do not have a U.S. visa"
    path and into the existing visa assistance / M-PESA flow. Used both
    when the student picks that option and automatically when a visa
    fails verification. Reuses the application's existing visa_requests
    row (get_or_create_integrated_visa_request), so nothing is
    duplicated; all other application data is kept."""
    db.execute(
        """UPDATE funding_applications
           SET visa_required = 1, visa_status = 'NEEDS_ASSISTANCE', visa_assistance_required = 1,
               visa_step_status = 'ACTION_REQUIRED',
               visa_document_status = 'NOT_UPLOADED', visa_document_path = NULL,
               visa_document_original_name = NULL, visa_document_uploaded_at = NULL,
               visa_document_type = NULL, visa_document_issue_date = NULL,
               visa_document_expiry_date = NULL, visa_document_passport_number = NULL,
               visa_document_notes = NULL, last_updated = CURRENT_TIMESTAMP WHERE id = ?""",
        (application["id"],),
    )
    db.commit()
    session.pop(VISA_FAILED_SESSION_KEY, None)
    visa_request_id = get_or_create_integrated_visa_request(db, student, application, cycle)
    if not visa_request_id:
        db.commit()
        flash("Visa assistance pricing is not yet configured for your country. Please contact support.", "danger")
        return redirect(url_for("application_step", step_name="visa"))
    # Requests from the funding application follow the order
    # visa form -> documents -> declaration -> payment. An older request of
    # this application that has no payment yet is moved onto that order
    # too; one already paid / with a payment submitted keeps its order.
    db.execute(
        """UPDATE visa_requests SET form_first = 1, updated_at = CURRENT_TIMESTAMP
           WHERE id = ? AND form_first = 0 AND payment_verified = 0
             AND NOT EXISTS (SELECT 1 FROM visa_payments WHERE request_id = visa_requests.id)""",
        (visa_request_id,))
    db.commit()
    vr = db.execute("SELECT * FROM visa_requests WHERE id = ?", (visa_request_id,)).fetchone()
    if visa_lib.form_is_first(vr) and not visa_lib.form_submitted(vr):
        return redirect(url_for("student_visa_application", request_id=visa_request_id))
    return redirect(url_for("student_visa_payment", request_id=visa_request_id))


def _fail_visa_verification(db, student, application, cycle, reasons):
    """Automatic rejection: record why (visible to admins), tell the
    student, and send them straight into visa assistance. Never marks
    anything complete and never shows a success message."""
    notes = "; ".join(reasons) or "Visa could not be verified."
    db.execute(
        """UPDATE funding_applications
           SET visa_verification_status = 'FAILED', visa_verification_notes = ?, visa_verified_at = NULL,
               last_updated = CURRENT_TIMESTAMP WHERE id = ?""",
        (notes[:2000], application["id"]),
    )
    add_history(db, application["id"], application["status"],
                f"Visa verification failed automatically: {notes}"[:2000])
    add_notification(db, student["id"],
                     "❌ Your U.S. visa could not be verified. You have been moved to visa assistance.")
    db.commit()
    app.logger.info("Visa verification failed for application %s: %s", application["id"], notes)
    flash(VISA_VERIFICATION_FAILED_MESSAGE, "danger")
    for reason in reasons[:6]:
        flash(reason, "danger")
    return _send_to_visa_assistance(db, student, application, cycle)

PAYMENT_PROOF_DIR = os.path.join(UPLOAD_ROOT, "payment_proofs")
os.makedirs(PAYMENT_PROOF_DIR, exist_ok=True)
ALLOWED_PAYMENT_PROOF_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_PAYMENT_PROOF_SIZE_BYTES = 5 * 1024 * 1024
ALLOWED_PAYMENT_PROOF_MIMETYPES = {"image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/webp"}

_PAYMENT_PROOF_SIGNATURES = {
    "jpg": [b"\xff\xd8\xff"], "jpeg": [b"\xff\xd8\xff"],
    "png": [b"\x89PNG\r\n\x1a\n"], "webp": [b"RIFF"],
}

def _payment_proof_extension(filename):
    return filename.rsplit(".", 1)[1].lower() if filename and "." in filename else None

def _payment_proof_looks_valid(file_storage, ext):
    if ext not in ALLOWED_PAYMENT_PROOF_EXTENSIONS:
        return False
    header = file_storage.stream.read(12)
    file_storage.stream.seek(0)
    if ext == "webp":
        return header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    return any(header.startswith(sig) for sig in _PAYMENT_PROOF_SIGNATURES.get(ext, []))

def save_payment_proof(file_storage, original_filename):
    if not original_filename:
        return None, "Please choose your M-PESA confirmation screenshot."
    ext = _payment_proof_extension(original_filename)
    if ext not in ALLOWED_PAYMENT_PROOF_EXTENSIONS:
        return None, "Unsupported file type. Please upload JPG, JPEG, PNG, or WEBP."
    # Browser-declared MIME type must also be an image type (a cheap extra
    # check; the magic-byte check below is the one that actually matters).
    if (file_storage.mimetype or "").lower() not in ALLOWED_PAYMENT_PROOF_MIMETYPES:
        return None, "Unsupported file type. Please upload a JPG, PNG, or WEBP image."
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size == 0:
        return None, "The screenshot file is empty."
    if size > MAX_PAYMENT_PROOF_SIZE_BYTES:
        return None, f"The screenshot is too large. Please upload an image under {MAX_PAYMENT_PROOF_SIZE_BYTES // (1024 * 1024)} MB."
    if not _payment_proof_looks_valid(file_storage, ext):
        return None, "This file does not look like a valid JPG, PNG, or WEBP image."
    # Random name + fixed extension: the student's filename is never used
    # on disk (no path traversal, no .php/.exe/.html ever stored).
    stored_filename = f"{secrets.token_hex(16)}.{ext}"
    file_storage.save(os.path.join(PAYMENT_PROOF_DIR, stored_filename))
    return stored_filename, None

def _visa_setting(db, key, default=None):
    row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default

def _set_visa_setting(db, key, value):
    db.execute("INSERT INTO app_settings(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

def _visa_payment_recipient(db):
    # Visa Admin setting (editable at /visa-admin/settings) wins; the
    # MPESA_PHONE_NUMBER environment variable is the first-run default.
    return _visa_setting(db, "mpesa_receiving_number",
                         os.environ.get("MPESA_PHONE_NUMBER") or os.environ.get("ASB_MPESA_RECEIVING_NUMBER") or "0181785792")

def _visa_service_fee(db):
    raw = _visa_setting(db, "visa_application_fee",
                        os.environ.get("VISA_APPLICATION_FEE") or os.environ.get("ASB_VISA_APPLICATION_FEE") or "1500")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 1500.0
# Demo mode lets the visa payment page offer an instant "simulate payment
# confirmation" button, so the full pay -> verify -> unlock journey can be
# tested without a second admin login or a real payment gateway. Turn this
# off in production (DEMO_MODE=0) once a real payment gateway is wired in -
# real gateways confirm via server-to-server webhook instead.
DEMO_MODE = False  # Legacy demo payment is permanently disabled for manual verification.

# Document checklist used for every application (kept simple / hard-coded
# for a beginner project - could later move into its own database table).
DOCUMENT_CHECKLIST = [
    ("Academic Transcripts", True),
    ("Certificates", True),
    ("Admission Letter", False),
    ("Recommendation Letter", True),
    ("Personal Statement", True),
    ("CV", True),
    ("Passport / Identity Document", False),
    ("Proof of Financial Need", False),
    ("Provider-Specific Document", False),
]

APPLICATION_STATUSES = [
    "Draft", "Submitted", "Received", "Eligibility Review", "Information Required",
    "Funding Matching", "Documents Review", "Matched", "Provider Referral",
    "Provider Application", "Provider Review", "Decision Pending", "Funded",
    "Unsuccessful", "No Suitable Match", "Withdrawn", "Cycle Closed",
]

TRACKER_STEPS = [
    "Application Submitted", "Eligibility Review", "Funding Matching",
    "Documents Verified", "Provider Referral", "Provider Review",
    "Decision", "Funding / Next Steps",
]


# ---------------------------------------------------------------------
# Database connection lifecycle - one connection per request
# ---------------------------------------------------------------------
@app.before_request
def open_db():
    g.db = get_db()


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None) if hasattr(g, "pop") else None
    if db is not None:
        db.close()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def current_user():
    if "user_id" not in session:
        return None
    return g.db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()


def current_student():
    if session.get("role") != "student":
        return None
    return g.db.execute("SELECT * FROM students WHERE user_id = ?", (session["user_id"],)).fetchone()


def current_admin():
    """The MAIN Africa ScholarBridge admin (funding platform). Reads only
    session['role']/session['user_id'] - completely unaware of, and
    unaffected by, any Visa Admin session (see current_visa_admin below)."""
    if session.get("role") != "admin":
        return None
    return g.db.execute("SELECT * FROM admins WHERE user_id = ?", (session["user_id"],)).fetchone()


def current_visa_admin():
    """The U.S. Student Visa Admin - a fully separate authentication
    context from student/main-admin. It is deliberately keyed off its
    OWN session key (session['visa_admin_user_id']), never off
    session['role']/session['user_id']. This is what makes the three
    login states independent: logging into /visa-admin/login never sets
    session['role'], so it can never satisfy login_required() or
    admin_required(); logging into /login or /admin/login never sets
    session['visa_admin_user_id'], so it can never satisfy
    visa_admin_required() either.
    """
    if "visa_admin_user_id" not in session:
        return None
    return g.db.execute(
        "SELECT * FROM visa_admins WHERE user_id = ?", (session["visa_admin_user_id"],)
    ).fetchone()


def get_current_cycle():
    return g.db.execute("SELECT * FROM funding_cycles WHERE is_current = 1 ORDER BY id DESC LIMIT 1").fetchone()


@app.context_processor
def inject_globals():
    """Make these available in every template without passing them manually."""
    return {
        "logged_in_student": current_student() if g.get("db") is not None else None,
        "logged_in_admin": current_admin() if g.get("db") is not None else None,
        "logged_in_visa_admin": current_visa_admin() if g.get("db") is not None else None,
        "current_cycle": get_current_cycle() if g.get("db") is not None else None,
        "now": datetime.utcnow(),
        "app_version": APP_VERSION,
    }


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "student":
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        if current_student() is None:
            # The account no longer exists (e.g. deleted by an admin): end
            # this student session cleanly instead of failing on every page.
            session.pop("user_id", None)
            session.pop("role", None)
            flash("Your session has ended. Please log in again.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Protects /admin/* (the MAIN Africa ScholarBridge admin only).
    A Visa Admin session alone (session['visa_admin_user_id']) never
    satisfies this - MAIN_ADMIN and VISA_ADMIN are separate permissions."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Admin login required.", "warning")
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


def visa_admin_required(f):
    """Protects /visa-admin/* (the SEPARATE Visa Admin portal only).
    A main-admin session (session['role'] == 'admin') never satisfies
    this - logging into the Main Admin area does not grant Visa Admin
    access, and vice versa."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "visa_admin_user_id" not in session:
            flash("Visa Admin login required.", "warning")
            # Remember the page (e.g. /visa-admin/payments) so the admin
            # lands there after logging in. Only /visa-admin/ paths are
            # accepted, so this can't be abused as an open redirect.
            return redirect(url_for("visa_admin_login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def generate_reference_number(db, year):
    """Format: ASB-[YEAR]-[6 DIGIT NUMBER], guaranteed unique."""
    while True:
        number = random.randint(0, 999999)
        ref = f"ASB-{year}-{number:06d}"
        exists = db.execute(
            "SELECT id FROM funding_applications WHERE reference_number = ?", (ref,)
        ).fetchone()
        if not exists:
            return ref


def add_notification(db, student_id, message):
    db.execute(
        "INSERT INTO notifications (student_id, message) VALUES (?, ?)",
        (student_id, message),
    )


def add_history(db, application_id, status, note=""):
    db.execute(
        "INSERT INTO application_history (application_id, status, note) VALUES (?, ?, ?)",
        (application_id, status, note),
    )


def send_application_confirmation_email(db, application_id, force=False):
    """The single place in this app that sends the "application
    submitted" confirmation email. Called exactly once by
    application_submit(), right after the application has actually been
    saved as Submitted (never merely because the student clicked
    Submit) - and again, explicitly, only by the Main Admin's "Resend
    Confirmation Email" action (force=True).

    Goes to the email address REGISTERED ON THE STUDENT'S ACCOUNT
    (users.email - the login email), not whatever the student may have
    typed into the application's own "Email" field, per the spec.

    Never raises, and never touches the application's own status/data -
    an email failure here always leaves the application itself exactly
    as "successfully submitted" as it already was.
    """
    application = db.execute("SELECT * FROM funding_applications WHERE id = ?", (application_id,)).fetchone()
    if not application:
        return False
    if application["confirmation_email_sent"] and not force:
        return False  # already sent once - never auto-resend (duplicate-prevention)

    student = db.execute("SELECT * FROM students WHERE id = ?", (application["student_id"],)).fetchone()
    cycle = db.execute("SELECT * FROM funding_cycles WHERE id = ?", (application["cycle_id"],)).fetchone()
    account = db.execute("SELECT email FROM users WHERE id = ?", (student["user_id"],)).fetchone() if student else None
    to_address = account["email"] if account else None

    submitted_display = application["submitted_at"] or ""
    try:
        submitted_display = datetime.strptime(application["submitted_at"][:19], "%Y-%m-%d %H:%M:%S").strftime("%d %B %Y")
    except (ValueError, TypeError, AttributeError):
        pass

    subject, body = email_lib.application_confirmation_email(
        student["full_name"] if student else "Student",
        application["reference_number"] or "Pending",
        cycle["name"] if cycle else "",
        submitted_display,
    )
    sent_ok, _error_code = email_lib.send_email(to_address, subject, body)

    db.execute(
        """UPDATE funding_applications
           SET confirmation_email_status = ?, confirmation_email_sent_at = CURRENT_TIMESTAMP,
               confirmation_email_sent = ?
           WHERE id = ?""",
        ("SENT" if sent_ok else "FAILED", 1 if sent_ok else application["confirmation_email_sent"], application_id),
    )
    db.commit()
    return sent_ok


def _ensure_funding_document_checklist(db, application_id):
    """Ensure older applications have every current checklist item without
    duplicating existing document rows."""
    have = {r["document_type"] for r in db.execute(
        "SELECT document_type FROM documents WHERE application_id = ?", (application_id,))}
    for doc_type, required in DOCUMENT_CHECKLIST:
        if doc_type not in have:
            db.execute(
                "INSERT INTO documents (application_id, document_type, is_required, availability) VALUES (?, ?, ?, ?)",
                (application_id, doc_type, 1 if required else 0, None),
            )


def _missing_funding_documents(documents):
    missing = []
    for d in documents:
        if d["is_required"] and d["status"] == "Missing":
            missing.append(d["document_type"])
        elif not d["is_required"] and (d["availability"] or "").lower() == "yes" and d["status"] == "Missing":
            missing.append(f'{d["document_type"]} (you selected Yes)')
        elif not d["is_required"] and not d["availability"]:
            missing.append(f'{d["document_type"]} (choose Yes or No)')
    return missing


def get_or_create_draft_application(db, student, cycle):
    """Every student has at most ONE application per cycle. This fetches
    the existing one, or creates a fresh Draft, pre-filling it from the
    student's most recent previous-cycle application when available.
    """
    existing = db.execute(
        "SELECT * FROM funding_applications WHERE student_id = ? AND cycle_id = ?",
        (student["id"], cycle["id"]),
    ).fetchone()
    if existing:
        return existing

    # Try to pre-fill from the most recent earlier application.
    previous = db.execute(
        """SELECT * FROM funding_applications WHERE student_id = ? AND cycle_id != ?
           ORDER BY id DESC LIMIT 1""",
        (student["id"], cycle["id"]),
    ).fetchone()

    fields = ["full_name", "date_of_birth", "country", "citizenship", "phone", "email", "gender",
              "institution", "education_level", "course", "field_of_study", "year_of_study",
              "academic_info", "graduation_year", "funding_type_needed", "tuition_need",
              "accommodation_need", "living_expenses_need", "books_need", "transport_need",
              "technology_need", "other_expenses", "household_situation", "source_of_support",
              "estimated_financial_need", "funding_already_received", "preferences",
              "personal_statement"]

    values = {f: (previous[f] if previous else None) for f in fields}
    if not previous:
        values["full_name"] = student["full_name"]
        values["country"] = student["country"]
        values["citizenship"] = student["citizenship"]
        values["phone"] = student["phone"]
        values["education_level"] = student["education_level"]
        values["institution"] = student["institution"]
        values["field_of_study"] = student["field_of_study"]
        values["year_of_study"] = student["year_of_study"]
        values["date_of_birth"] = student["date_of_birth"]
        values["gender"] = student["gender"]
        user = db.execute("SELECT email FROM users WHERE id = ?", (student["user_id"],)).fetchone()
        values["email"] = user["email"] if user else None

    columns = ", ".join(values.keys())
    placeholders = ", ".join(["?"] * len(values))
    cur = db.execute(
        f"INSERT INTO funding_applications (student_id, cycle_id, status, {columns}) "
        f"VALUES (?, ?, 'Draft', {placeholders})",
        (student["id"], cycle["id"], *values.values()),
    )
    db.commit()
    app_id = cur.lastrowid

    # Create the document checklist for this application.
    for doc_type, required in DOCUMENT_CHECKLIST:
        db.execute(
            "INSERT INTO documents (application_id, document_type, is_required, availability) VALUES (?, ?, ?, ?)",
            (app_id, doc_type, 1 if required else 0, None),
        )
    db.commit()

    return db.execute("SELECT * FROM funding_applications WHERE id = ?", (app_id,)).fetchone()


def get_active_visa_request(db, student):
    """A student's most recent, non-cancelled visa request (if any).
    Kept simple: one "active" visa journey at a time, matching how the
    dashboard and /student-visa/* pages present a single request.
    """
    return db.execute(
        """SELECT * FROM visa_requests WHERE student_id = ? AND application_status != 'cancelled'
           ORDER BY id DESC LIMIT 1""",
        (student["id"],),
    ).fetchone()


def get_visa_request_or_404(db, request_id, student):
    return db.execute(
        "SELECT * FROM visa_requests WHERE id = ? AND student_id = ?", (request_id, student["id"])
    ).fetchone()


def _visa_post_unlock_redirect(visa_request):
    """Where the student goes once their visa assistance payment is
    unlocked/verified.

    - A visa request raised from INSIDE an annual funding application
      (the integrated flow - see the "visa" step in APPLICATION_STEPS)
      sends the student back to that step, which now shows the
      "Visa Assistance Requirement Completed" message and a
      "Continue Funding Application" button - never a separate module.
    - A visa request started the old way, directly from /student-visa,
      still goes to the full multi-step visa application as before.
    """
    if visa_lib.form_is_first(visa_request) and not visa_lib.form_submitted(visa_request):
        # Paid right after the documents: finish Additional Information + Declaration.
        return redirect(url_for("student_visa_step", request_id=visa_request["id"], step_name="additional"))
    if visa_request["annual_application_id"]:
        app_row = g.db.execute("SELECT id, status FROM funding_applications WHERE id = ?",
                               (visa_request["annual_application_id"],)).fetchone()
        if app_row and app_row["status"] != "Draft":
            return redirect(url_for("application_confirmation", application_id=app_row["id"]))
        return redirect(url_for("application_step", step_name="visa"))
    return redirect(url_for("student_visa_application", request_id=visa_request["id"]))


def get_or_create_integrated_visa_request(db, student, application, cycle):
    """Reuse or create the visa_requests row tied to THIS SPECIFIC annual
    funding application (application["id"]). Reusing an existing row is
    what stops a student from ever being charged twice for one
    application's visa step, even if they revisit the "visa" step after
    already choosing "No, I need assistance" once.
    """
    existing = db.execute(
        "SELECT * FROM visa_requests WHERE annual_application_id = ?", (application["id"],)
    ).fetchone()
    if existing:
        return existing["id"]

    pricing = (visa_lib.get_pricing_for_country(db, student["country"])
               or db.execute("SELECT * FROM visa_pricing WHERE country = 'Kenya'").fetchone())
    if not pricing:
        return None

    ref = visa_lib.generate_visa_request_number(db, datetime.utcnow().year)
    user = db.execute("SELECT email FROM users WHERE id = ?", (student["user_id"],)).fetchone()
    # The configured visa assistance fee (Visa Admin -> Settings), exactly
    # like the standalone service - one fee, one payment system.
    pricing = dict(pricing)
    pricing.update(service_price=_visa_service_fee(db), currency="KES", currency_symbol="KSh")
    # Pre-fill the visa form from the funding application the student has
    # just completed, so nothing has to be typed twice.
    cur = db.execute(
        """INSERT INTO visa_requests
           (request_number, student_id, annual_application_id, cycle_id, country, currency, currency_symbol,
            service_price, full_name, email, phone, citizenship, country_of_residence, date_of_birth, gender,
            education_level, current_status, organization_name, position_course,
            destination_country, visa_category, form_first)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Student', ?, ?, ?, ?, 1)""",
        (ref, student["id"], application["id"], cycle["id"], pricing["country"], pricing["currency"],
         pricing["currency_symbol"], pricing["service_price"],
         application["full_name"] or student["full_name"],
         application["email"] or (user["email"] if user else None),
         application["phone"] or student["phone"], application["citizenship"] or student["citizenship"],
         application["country"] or student["country"], application["date_of_birth"],
         application["gender"] if application["gender"] in visa_lib.GENDERS else None,
         application["education_level"], application["institution"], application["course"],
         visa_lib.DEFAULT_DESTINATION, visa_lib.DEFAULT_VISA_TYPE),
    )
    request_id = cur.lastrowid
    visa_lib.add_visa_history(db, request_id, "payment_required",
                               "Visa assistance requested as part of the annual funding application.")
    for doc_type, required in visa_lib.VISA_DOCUMENT_CHECKLIST:
        db.execute(
            "INSERT INTO visa_documents (request_id, document_type, is_required) VALUES (?, ?, ?)",
            (request_id, doc_type, 1 if required else 0),
        )
    db.execute(
        """UPDATE funding_applications
           SET visa_request_id = ?, visa_reference = ?, visa_payment_status = 'PENDING',
               visa_payment_amount = ?, visa_payment_currency = ?, last_updated = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (request_id, ref, pricing["service_price"], pricing["currency"], application["id"]),
    )
    add_notification(db, student["id"],
                      f"🇺🇸 Visa assistance request {ref} created as part of your annual application. "
                      f"Complete the visa application form to continue.")
    return request_id


# ---------------------------------------------------------------------
# PAYMENT VERIFICATION - the ONLY place that ever marks a visa payment
# verified and unlocks the visa application.
#
# Today it is called from exactly one route: the Visa Admin clicking
# VERIFY PAYMENT (verification_method='manual_admin'). Nothing a student
# submits - SMS text, screenshot, transaction code - can reach it.
#
# FUTURE OFFICIAL M-PESA INTEGRATION: when the business moves to an
# official Paybill/Till with Safaricom Daraja, a server-to-server
# callback route would look up the visa_payments row by its Daraja
# CheckoutRequestID / receipt number, confirm ResultCode == 0 and the
# amount, then call this same function with
# verification_method='daraja_callback' and admin_id=None. No other
# code needs to change: the gate, the student pages and the admin
# screens all read the result of this function.
# ---------------------------------------------------------------------
def _verify_visa_payment(db, visa_request, method, provider_reference=None,
                          admin_id=None, payment_id=None, verification_method="manual_admin"):
    request_id = visa_request["id"]
    now = datetime.utcnow().isoformat(timespec="seconds")

    if payment_id:
        db.execute(
            """UPDATE visa_payments
               SET payment_status='PAYMENT_VERIFIED', proof_status='Approved', status='paid', verified=1,
                   paid_at=?, verified_at=?, verified_by=?, reviewed_at=?, reviewed_by=?,
                   verification_method=?, provider_reference=?, payment_method=?,
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND request_id=?""",
            (now, now, admin_id, now, admin_id, verification_method, provider_reference, method,
             payment_id, request_id),
        )

    new_app_status = ("application_unlocked" if visa_request["application_status"] == "payment_required"
                      else visa_request["application_status"])
    db.execute(
        """UPDATE visa_requests
           SET payment_status='paid', payment_verified=1, payment_verified_at=?,
               application_status=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (now, new_app_status, request_id),
    )
    if visa_request["visa_fee_coverage_status"] == "PENDING":
        visa_lib.mark_fee_eligible(db, request_id)
    visa_lib.add_visa_history(db, request_id, "payment_verified",
                              f"Payment verified ({method}). Reference: {provider_reference or 'n/a'}.")

    # Keep the annual funding application's denormalised mirror in sync
    # (integrated flow - see README sections 16/17).
    # A form-first request paid BEFORE its declaration (the current order:
    # documents -> payment -> additional info -> declaration) is not complete
    # yet - the funding application's visa step completes at the declaration.
    awaiting_declaration = visa_lib.form_is_first(visa_request) and not visa_lib.form_submitted(visa_request)
    if visa_request["annual_application_id"]:
        if awaiting_declaration:
            db.execute(
                """UPDATE funding_applications
                   SET visa_payment_status='PAID', visa_assistance_approved=1, visa_status='NEEDS_ASSISTANCE',
                       visa_reference=?, visa_request_id=?, last_updated=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (visa_request["request_number"], request_id, visa_request["annual_application_id"]),
            )
        else:
            db.execute(
                """UPDATE funding_applications
                   SET visa_payment_status='PAID', visa_step_status='COMPLETE', visa_assistance_approved=1,
                       visa_status='NEEDS_ASSISTANCE', visa_assistance_status='COMPLETE',
                       visa_reference=?, visa_request_id=?, last_updated=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (visa_request["request_number"], request_id, visa_request["annual_application_id"]),
            )

    student = db.execute("SELECT full_name FROM students WHERE id=?", (visa_request["student_id"],)).fetchone()
    db.execute(
        "INSERT INTO visa_admin_notifications (request_id, message) VALUES (?, ?)",
        (request_id, f"🔔 New Visa Assistance Case\nStudent: {student['full_name'] if student else '—'}\n"
                     f"Reference: {visa_request['request_number']}\nPayment verified ({method})."),
    )

    # Form-first (from the final step of the funding application): the
    # form, documents and declaration are already in, so payment is the
    # last step - visa processing starts now and the annual funding
    # application is submitted (reference number, confirmation email,
    # matching) exactly as the student's own final submit would do.
    if visa_lib.form_is_first(visa_request) and visa_lib.form_submitted(visa_request):
        # Older order: declaration was signed before payment.
        add_notification(db, visa_request["student_id"],
                         "✅ Payment verified successfully. Your visa assistance application is now under review.")
        _complete_form_first_visa(db, visa_request)
        return
    if awaiting_declaration:
        add_notification(db, visa_request["student_id"],
                         "✅ Payment verified successfully. Please complete Additional Information and sign the "
                         "declaration to finish your application.")
        return
    add_notification(db, visa_request["student_id"],
                     "✅ Payment verified successfully. You can now continue with your visa application.")


def _reject_visa_payment(db, payment, reason, admin_id=None, admin_notes=None):
    """Marks one visa_payments row PAYMENT_REJECTED and keeps the visa
    request locked. The row is kept (never deleted) for the audit trail."""
    db.execute(
        """UPDATE visa_payments
           SET payment_status='PAYMENT_REJECTED', proof_status='Rejected', status='failed', verified=0,
               rejection_reason=?, admin_notes=COALESCE(?, admin_notes), reviewed_at=CURRENT_TIMESTAMP,
               reviewed_by=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (reason, admin_notes or None, admin_id, payment["id"]),
    )
    db.execute(
        "UPDATE visa_requests SET payment_status='pending', payment_verified=0, "
        "application_status='payment_required', updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (payment["request_id"],),
    )
    visa_lib.add_visa_history(db, payment["request_id"], "payment_rejected", reason)
    add_notification(db, payment["student_id"],
                     f"❌ Your payment could not be verified. Reason: {reason}")


# =======================================================================
# PUBLIC PAGES
# =======================================================================
@app.route("/")
def index():
    db = g.db
    featured = db.execute(
        """SELECT o.*, org.name AS org_name FROM funding_opportunities o
           JOIN funding_programs p ON o.program_id = p.id
           JOIN organizations org ON p.organization_id = org.id
           WHERE o.is_open = 1 ORDER BY RANDOM() LIMIT 6"""
    ).fetchall()
    organizations = db.execute("SELECT * FROM organizations ORDER BY RANDOM() LIMIT 8").fetchall()

    stats = {
        "students": db.execute("SELECT COUNT(*) c FROM students").fetchone()["c"],
        "opportunities": db.execute("SELECT COUNT(*) c FROM funding_opportunities").fetchone()["c"],
        "organizations": db.execute("SELECT COUNT(*) c FROM organizations").fetchone()["c"],
        "applications": db.execute("SELECT COUNT(*) c FROM funding_applications").fetchone()["c"],
    }
    return render_template("index.html", featured=featured, organizations=organizations, stats=stats)


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        g.db.execute(
            """INSERT INTO contact_messages (name, email, category, subject, message)
               VALUES (?, ?, ?, ?, ?)""",
            (request.form["name"], request.form["email"], request.form.get("category"),
             request.form.get("subject"), request.form["message"]),
        )
        g.db.commit()
        flash("Thank you - your message has been sent. We'll respond soon.", "success")
        return redirect(url_for("contact"))
    return render_template("contact.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/guides")
def guides():
    return render_template("guides.html")


@app.route("/calendar")
def calendar_page():
    db = g.db
    today = date.today().isoformat()
    opportunities = db.execute(
        """SELECT o.*, org.name AS org_name FROM funding_opportunities o
           JOIN funding_programs p ON o.program_id = p.id
           JOIN organizations org ON p.organization_id = org.id
           ORDER BY o.close_date ASC"""
    ).fetchall()
    cycles = db.execute("SELECT * FROM funding_cycles ORDER BY year ASC").fetchall()
    return render_template("calendar.html", opportunities=opportunities, cycles=cycles, today=today)


@app.route("/scam-alerts", methods=["GET", "POST"])
def scam_alerts():
    if request.method == "POST":
        g.db.execute(
            """INSERT INTO scam_reports (reporter_name, reporter_email, opportunity_name, description)
               VALUES (?, ?, ?, ?)""",
            (request.form.get("reporter_name"), request.form.get("reporter_email"),
             request.form.get("opportunity_name"), request.form["description"]),
        )
        g.db.commit()
        flash("Thank you for the report - our team will review it.", "success")
        return redirect(url_for("scam_alerts"))
    return render_template("scam_alerts.html")


@app.route("/providers")
def providers():
    db = g.db
    query = "SELECT * FROM organizations WHERE 1=1"
    params = []
    org_type = request.args.get("org_type")
    search = request.args.get("q")
    if org_type:
        query += " AND org_type = ?"
        params.append(org_type)
    if search:
        query += " AND (name LIKE ? OR description LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    query += " ORDER BY name ASC"
    organizations = db.execute(query, params).fetchall()
    org_types = [r["org_type"] for r in db.execute("SELECT DISTINCT org_type FROM organizations ORDER BY org_type").fetchall()]

    # Count active opportunities per organization
    counts = {}
    for row in db.execute(
        """SELECT org.id AS org_id, COUNT(o.id) AS c FROM organizations org
           LEFT JOIN funding_programs p ON p.organization_id = org.id
           LEFT JOIN funding_opportunities o ON o.program_id = p.id AND o.is_open = 1
           GROUP BY org.id"""
    ).fetchall():
        counts[row["org_id"]] = row["c"]

    return render_template("providers.html", organizations=organizations, org_types=org_types,
                            counts=counts, selected_type=org_type, search=search or "")


@app.route("/providers/<int:org_id>")
def provider_detail(org_id):
    db = g.db
    org = db.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
    if not org:
        return render_template("errors/404.html"), 404
    opportunities = db.execute(
        """SELECT o.* FROM funding_opportunities o
           JOIN funding_programs p ON o.program_id = p.id
           WHERE p.organization_id = ? ORDER BY o.close_date ASC""",
        (org_id,),
    ).fetchall()
    programs = db.execute("SELECT * FROM funding_programs WHERE organization_id = ?", (org_id,)).fetchall()
    return render_template("provider_detail.html", org=org, opportunities=opportunities, programs=programs)


@app.route("/opportunities")
@app.route("/funding", endpoint="funding")
def opportunities():
    """The funding directory - reachable at both /opportunities (its
    original address, kept so nothing that already links to it breaks)
    and /funding (the address the home page's global search bar and the
    rest of this feature use). Same view, same template, same data -
    just two doors into it.

    The `q` search box checks across every field a student would
    plausibly type: opportunity name, provider, country, education
    level, field of study, funding type, description, eligibility notes,
    and study destination - all case-insensitive, partial-match (SQLite's
    LIKE is case-insensitive for plain ASCII), so "computer" finds
    "Computer Science", "Computer Engineering", etc.
    """
    db = g.db
    query = """SELECT o.*, org.name AS org_name, org.id AS org_id FROM funding_opportunities o
               JOIN funding_programs p ON o.program_id = p.id
               JOIN organizations org ON p.organization_id = org.id WHERE 1=1"""
    params = []

    q = request.args.get("q", "").strip()
    country = request.args.get("country")
    level = request.args.get("level")
    funding_type = request.args.get("funding_type")
    field = request.args.get("field")
    destination = request.args.get("destination")
    funding_status = request.args.get("status")  # open/closed
    fully_funded = request.args.get("fully_funded")

    if q:
        query += """ AND (
            o.title LIKE ? OR org.name LIKE ? OR o.eligible_countries LIKE ? OR
            o.education_levels LIKE ? OR o.fields LIKE ? OR o.funding_type LIKE ? OR
            o.description LIKE ? OR o.eligibility_notes LIKE ? OR o.study_destination LIKE ?
        )"""
        like = f"%{q}%"
        params += [like] * 9
    if country:
        query += " AND (o.eligible_countries LIKE ? OR o.eligible_countries LIKE '%All%')"
        params.append(f"%{country}%")
    if level:
        query += " AND o.education_levels LIKE ?"
        params.append(f"%{level}%")
    if funding_type:
        query += " AND o.funding_type = ?"
        params.append(funding_type)
    if field:
        query += " AND (o.fields LIKE ? OR o.fields LIKE '%All%')"
        params.append(f"%{field}%")
    if destination:
        query += " AND o.study_destination LIKE ?"
        params.append(f"%{destination}%")
    if funding_status == "open":
        query += " AND o.is_open = 1"
    elif funding_status == "closed":
        query += " AND o.is_open = 0"
    if fully_funded == "1":
        query += " AND o.fully_funded = 1"

    query += " ORDER BY o.is_open DESC, o.close_date ASC"
    results = db.execute(query, params).fetchall()

    funding_types = [r["funding_type"] for r in db.execute("SELECT DISTINCT funding_type FROM funding_opportunities").fetchall()]

    saved_ids = set()
    student = current_student()
    if student:
        saved_ids = {r["opportunity_id"] for r in db.execute(
            "SELECT opportunity_id FROM saved_opportunities WHERE student_id = ?", (student["id"],)
        ).fetchall()}

    return render_template("opportunities.html", opportunities=results, funding_types=funding_types,
                            filters=request.args, search_query=q, saved_ids=saved_ids)


@app.route("/opportunities/<int:opp_id>")
def opportunity_detail(opp_id):
    db = g.db
    opp = db.execute(
        """SELECT o.*, org.name AS org_name, org.id AS org_id, org.verification_status AS org_verification
           FROM funding_opportunities o
           JOIN funding_programs p ON o.program_id = p.id
           JOIN organizations org ON p.organization_id = org.id
           WHERE o.id = ?""",
        (opp_id,),
    ).fetchone()
    if not opp:
        return render_template("errors/404.html"), 404
    days_left = None
    if opp["close_date"]:
        try:
            close = datetime.strptime(opp["close_date"], "%Y-%m-%d").date()
            days_left = (close - date.today()).days
        except ValueError:
            days_left = None
    return render_template("opportunity_detail.html", opp=opp, days_left=days_left)


@app.route("/opportunities/<int:opp_id>/save", methods=["POST"])
@login_required
def opportunity_toggle_save(opp_id):
    """Saves/un-saves an opportunity for the logged-in student. A student
    session is required (login_required covers admin/visa_admin sessions
    too, but current_student() below returns None for those and we bail
    out rather than let a non-student session save anything)."""
    db = g.db
    student = current_student()
    if not student:
        return redirect(url_for("login"))
    existing = db.execute(
        "SELECT id FROM saved_opportunities WHERE student_id = ? AND opportunity_id = ?",
        (student["id"], opp_id),
    ).fetchone()
    if existing:
        db.execute("DELETE FROM saved_opportunities WHERE id = ?", (existing["id"],))
        db.commit()
        flash("Removed from your saved opportunities.", "info")
    else:
        opp = db.execute("SELECT id FROM funding_opportunities WHERE id = ?", (opp_id,)).fetchone()
        if not opp:
            return render_template("errors/404.html"), 404
        db.execute(
            "INSERT INTO saved_opportunities (student_id, opportunity_id) VALUES (?, ?)",
            (student["id"], opp_id),
        )
        db.commit()
        flash("Saved for later.", "success")
    return redirect(request.referrer or url_for("opportunities"))


# =======================================================================
# AUTHENTICATION
# =======================================================================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        db = g.db
        email = request.form["email"].strip().lower()
        existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            flash("An account with that email already exists. Please log in instead.", "danger")
            return redirect(url_for("register"))

        if request.form["password"] != request.form["confirm_password"]:
            flash("Passwords do not match.", "danger")
            return redirect(url_for("register"))

        password_hash = generate_password_hash(request.form["password"])
        cur = db.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'student')",
            (email, password_hash),
        )
        user_id = cur.lastrowid
        db.execute(
            """INSERT INTO students
               (user_id, full_name, phone, country, citizenship, education_level,
                institution, field_of_study, year_of_study)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, request.form["full_name"], request.form.get("phone"),
             request.form.get("country"), request.form.get("citizenship"),
             request.form.get("education_level"), request.form.get("institution"),
             request.form.get("field_of_study"), request.form.get("year_of_study")),
        )
        db.commit()

        session["user_id"] = user_id
        session["role"] = "student"
        flash("Welcome to Africa ScholarBridge! Your account has been created.", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        db = g.db
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = db.execute("SELECT * FROM users WHERE email = ? AND role = 'student'", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["role"] = "student"
            flash("Logged in successfully.", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "danger")
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    # Targeted pop, not session.clear(): this only ends the STUDENT session.
    # A Visa Admin or Main Admin session that happens to share this browser's
    # cookie (e.g. a demo/testing session) is left untouched - the three
    # login states are independent.
    session.pop("user_id", None)
    session.pop("role", None)
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        db = g.db
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = db.execute("SELECT * FROM users WHERE email = ? AND role = 'admin'", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["role"] = "admin"
            # Full administrator: if the SAME email also holds the separate
            # visa_admin role and this SAME password verifies against that
            # row's own hash, open the Visa Admin session too, so one login
            # reaches Visa Payments / M-PESA verification. Accounts that only
            # have the admin role get exactly what they had before.
            visa_user = db.execute(
                "SELECT * FROM users WHERE email = ? AND role = 'visa_admin'", (email,)
            ).fetchone()
            if visa_user and check_password_hash(visa_user["password_hash"], password):
                session["visa_admin_user_id"] = visa_user["id"]
                session["visa_admin_via_admin_login"] = True
            flash("Welcome back, admin.", "success")
            return redirect(url_for("admin_dashboard"))
        flash("Invalid admin credentials.", "danger")
        return redirect(url_for("admin_login"))
    return render_template("admin_login.html")


# =======================================================================
# STUDENT AREA
# =======================================================================
@app.route("/dashboard")
@login_required
def dashboard():
    db = g.db
    student = current_student()
    cycle = get_current_cycle()
    application = None
    matches_count = 0
    referrals_count = 0
    docs_progress = (0, 0)
    notifications = []
    history = []

    if cycle:
        application = db.execute(
            "SELECT * FROM funding_applications WHERE student_id = ? AND cycle_id = ?",
            (student["id"], cycle["id"]),
        ).fetchone()

    if application:
        matches_count = db.execute(
            "SELECT COUNT(*) c FROM funding_matches WHERE application_id = ?", (application["id"],)
        ).fetchone()["c"]
        referrals_count = db.execute(
            """SELECT COUNT(*) c FROM provider_referrals r
               JOIN funding_matches m ON r.match_id = m.id WHERE m.application_id = ?""",
            (application["id"],),
        ).fetchone()["c"]
        total_docs = db.execute(
            "SELECT COUNT(*) c FROM documents WHERE application_id = ?", (application["id"],)
        ).fetchone()["c"]
        done_docs = db.execute(
            "SELECT COUNT(*) c FROM documents WHERE application_id = ? AND status != 'Missing'",
            (application["id"],),
        ).fetchone()["c"]
        docs_progress = (done_docs, total_docs)
        history = db.execute(
            "SELECT * FROM application_history WHERE application_id = ? ORDER BY id DESC LIMIT 5",
            (application["id"],),
        ).fetchall()

    notifications = db.execute(
        "SELECT * FROM notifications WHERE student_id = ? ORDER BY id DESC LIMIT 5", (student["id"],)
    ).fetchall()

    upcoming = db.execute(
        """SELECT o.*, org.name AS org_name FROM funding_opportunities o
           JOIN funding_programs p ON o.program_id = p.id
           JOIN organizations org ON p.organization_id = org.id
           WHERE o.is_open = 1 AND o.close_date IS NOT NULL
           ORDER BY o.close_date ASC LIMIT 5"""
    ).fetchall()

    has_us_match = False
    if application:
        has_us_match = db.execute(
            """SELECT COUNT(*) c FROM funding_matches m
               JOIN funding_opportunities o ON m.opportunity_id = o.id
               WHERE m.application_id = ? AND o.study_destination LIKE '%United States%'""",
            (application["id"],),
        ).fetchone()["c"] > 0

    visa_request = get_active_visa_request(db, student)
    visa_tracker = visa_lib.processing_tracker(visa_request) if visa_request else None

    # 🏦 Funding Payment Information + Funding Payment Status (dashboard).
    bank_details = None
    masked_account = None
    disbursement = None
    if application:
        bank_details = db.execute(
            "SELECT * FROM student_bank_details WHERE application_id = ? AND confirmed = 1",
            (application["id"],),
        ).fetchone()
        if bank_details:
            masked_account = banks_lib.mask_account_number(bank_details["account_number"])
        disbursement = db.execute(
            """SELECT fd.*, o.title AS opportunity_title FROM funding_disbursements fd
               LEFT JOIN funding_opportunities o ON fd.opportunity_id = o.id
               WHERE fd.application_id = ? ORDER BY fd.id DESC LIMIT 1""",
            (application["id"],),
        ).fetchone()

    return render_template(
        "dashboard.html", application=application, cycle=cycle, matches_count=matches_count,
        referrals_count=referrals_count, docs_progress=docs_progress, notifications=notifications,
        upcoming=upcoming, tracker_steps=TRACKER_STEPS, history=history,
        has_us_match=has_us_match, visa_request=visa_request, visa_tracker=visa_tracker,
        us_gov_fee=visa_lib.US_GOV_VISA_FEE_USD, format_price=visa_lib.format_price,
        bank_details=bank_details, masked_account=masked_account, disbursement=disbursement,
    )


APPLICATION_STEPS = [
    # 🇺🇸 Visa Verification is the VERY LAST step of the annual funding
    # application: the student is only asked about their visa after every
    # other step, including Review, is done. See the "visa" branch inside
    # application_step() below and _review_done().
    #
    # 🏦 The "bank" step (right before review) is the Bank Account /
    # Disbursement Information step - see the "bank" branch inside
    # application_step(). Every draft applicant completes it after entering
    # the requested funding amount on the "financial" step. (NOT_REQUIRED
    # remains only on applications submitted before this was required.)
    "personal", "education", "funding_need", "financial", "preferences",
    "statement", "documents", "bank", "review", "visa",
]
APPLICATION_STEP_TITLES = {
    "personal": "Personal Information", "education": "Education", "funding_need": "Funding Information",
    "financial": "Financial Information & Amount Requested", "preferences": "Preferences",
    "statement": "Personal Statement", "documents": "Documents", "bank": "Bank Account / Disbursement Information",
    "review": "Review", "visa": "Visa Verification",
}

# Requested funding amount (Financial step): whole Kenyan shillings.
REQUESTED_AMOUNT_MAX_KSH = 100_000_000
REQUESTED_AMOUNT_MESSAGE = ("Please enter the amount of funding you are requesting in Kenyan shillings, "
                            "e.g. 75,000.")


def parse_requested_amount_ksh(raw):
    """'75000', '75,000', 'KSh 75,000', 'Ksh 75 000.00' -> 75000.
    Returns None for anything that isn't a whole, positive amount in range."""
    text = re.sub(r"(?i)^\s*(?:kes|k\s*sh?s?)\.?\s*", "", str(raw or "")).strip()
    text = re.sub(r"[,\s]", "", text)
    text = re.sub(r"\.0+$", "", text)
    if not re.fullmatch(r"\d{1,9}", text):
        return None
    amount = int(text)
    return amount if 1 <= amount <= REQUESTED_AMOUNT_MAX_KSH else None
VISA_STEP_NOT_READY_MESSAGE = ("Please complete and review all of your application steps first. "
                               "Visa Verification is the final step.")


def _review_done(application):
    """True once the student has submitted the Review step (current_step
    then points at the final Visa Verification step or beyond)."""
    return (application["current_step"] or 0) >= len(APPLICATION_STEPS)


def _first_unfinished_step(application):
    idx = max(0, min((application["current_step"] or 1) - 1, APPLICATION_STEPS.index("review")))
    return APPLICATION_STEPS[idx]


@app.route("/application/start")
@login_required
def application_start():
    db = g.db
    student = current_student()
    cycle = get_current_cycle()
    if not cycle:
        flash("There is no open funding cycle right now. Please check back soon.", "warning")
        return redirect(url_for("dashboard"))

    application = get_or_create_draft_application(db, student, cycle)

    if application["status"] != "Draft":
        flash("You already have a submitted application for this cycle.", "info")
        return redirect(url_for("dashboard"))

    return redirect(url_for("application_step", step_name=APPLICATION_STEPS[0]))


@app.route("/application/step/<step_name>", methods=["GET", "POST"])
@login_required
def application_step(step_name):
    db = g.db
    student = current_student()
    cycle = get_current_cycle()
    if not cycle:
        flash("There is no open funding cycle right now.", "warning")
        return redirect(url_for("dashboard"))

    application = db.execute(
        "SELECT * FROM funding_applications WHERE student_id = ? AND cycle_id = ?",
        (student["id"], cycle["id"]),
    ).fetchone()
    if not application:
        return redirect(url_for("application_start"))
    if application["status"] != "Draft":
        flash("This application has already been submitted and can no longer be edited.", "info")
        return redirect(url_for("dashboard"))

    if step_name not in APPLICATION_STEPS:
        return render_template("errors/404.html"), 404

    step_index = APPLICATION_STEPS.index(step_name)

    # Visa steps completed under the old upload-only logic were never
    # verified - they must be verified now.
    application = _revoke_unverified_visa_step(db, application)

    # Visa Verification is the final step: it is not shown (URL typing,
    # crafted POSTs) until the student has completed and reviewed every
    # earlier step. A visa path already under way (older applications that
    # answered the question when it was earlier in the flow) stays
    # reachable so the student can always see and finish it.
    if (step_name == "visa" and not _review_done(application)
            and application["visa_step_status"] == "NOT_STARTED"):
        flash(VISA_STEP_NOT_READY_MESSAGE, "warning")
        return redirect(url_for("application_step", step_name=_first_unfinished_step(application)))

    # ---------------------------------------------------------------
    # 🇺🇸 VISA STEP - handled separately from the generic "save fields,
    # advance to next step" flow below, because its two answers behave
    # completely differently: "Yes" advances like a normal step, while
    # "No" branches OUT to the payment page and does NOT advance the
    # step counter (the student comes right back to this same step once
    # payment is verified - see _visa_post_unlock_redirect above).
    # ---------------------------------------------------------------
    if step_name == "visa":
        # Once COMPLETE (either path), the only legal action is moving on -
        # the student is never asked to redo or repeat the other path.
        if request.method == "POST" and request.form.get("action") == "continue":
            if not _visa_requirement_passed(application):
                flash(VISA_STEP_REQUIRED_MESSAGE, "warning")
                return redirect(url_for("application_step", step_name="visa"))
            if not _review_done(application):
                flash(VISA_STEP_NOT_READY_MESSAGE, "warning")
                return redirect(url_for("application_step", step_name=_first_unfinished_step(application)))
            # Visa is the last step: what follows is the final submission.
            return redirect(url_for("application_submit"))

        if request.method == "POST":
            choice = request.form.get("visa_choice")

            # Answering the question again once a path is already under way
            # (or done) is a no-op - just send them to whatever state
            # they're actually in, never reset progress or double-charge.
            # ONE exception: a student on the "Yes" path whose visa has NOT
            # been accepted yet may switch to "I do not have a visa". That
            # reuses this same application (and the same visa_requests row
            # via get_or_create_integrated_visa_request), so nothing is
            # duplicated and their earlier answers are kept.
            switching_to_no_visa = choice == "no" and _visa_upload_incomplete(application)
            if application["visa_step_status"] in ("ACTION_REQUIRED", "COMPLETE") and not switching_to_no_visa:
                return redirect(url_for("application_step", step_name="visa"))

            if choice == "yes":
                # PATH A: "Yes, I already have my visa" -> upload proof.
                # This does NOT complete the step yet and does NOT send the
                # student to payment - the upload itself is what completes it.
                db.execute(
                    """UPDATE funding_applications
                       SET visa_required = 1, visa_status = 'HAS_VISA', visa_assistance_required = 0,
                           visa_step_status = 'ACTION_REQUIRED', last_updated = CURRENT_TIMESTAMP WHERE id = ?""",
                    (application["id"],),
                )
                db.commit()
                return redirect(url_for("application_step", step_name="visa"))

            elif choice == "no":
                # PATH B: "No, I need visa assistance" -> pay Africa
                # ScholarBridge's service fee (existing M-PESA flow).
                return _send_to_visa_assistance(db, student, application, cycle)

            flash("Please choose an option to continue.", "warning")
            return redirect(url_for("application_step", step_name="visa"))

        # GET: the template branches on (visa_status, visa_step_status):
        #   NOT_STARTED                          -> the Yes/No question
        #   HAS_VISA + ACTION_REQUIRED            -> the upload form
        #   NEEDS_ASSISTANCE + ACTION_REQUIRED    -> the payment prompt
        #   COMPLETE (either visa_status)         -> the matching success message
        linked_visa_request = None
        if application["visa_request_id"]:
            linked_visa_request = db.execute(
                "SELECT * FROM visa_requests WHERE id = ?", (application["visa_request_id"],)
            ).fetchone()
        return render_template(
            "application_visa_step.html", application=application, step_index=step_index,
            steps=APPLICATION_STEPS, step_titles=APPLICATION_STEP_TITLES, linked_visa_request=linked_visa_request,
            us_gov_fee=visa_lib.US_GOV_VISA_FEE_USD, student_fee=visa_lib.STUDENT_SERVICE_FEE_USD,
            format_price=visa_lib.format_price,
            allowed_visa_doc_extensions=sorted(ALLOWED_VISA_DOC_EXTENSIONS),
            max_visa_doc_mb=MAX_VISA_DOC_SIZE_BYTES // (1024 * 1024),
            visa_verification_failed=(session.get(VISA_FAILED_SESSION_KEY) == application["id"]
                                      and _visa_upload_incomplete(application)),
            visa_verification_failed_message=VISA_VERIFICATION_FAILED_MESSAGE,
        )

    # ---------------------------------------------------------------
    # 🏦 BANK ACCOUNT / DISBURSEMENT INFORMATION STEP - handled separately
    # from the generic "save fields, advance" flow below, because it is
    # asked of every draft applicant only after they have entered the
    # amount they request (Financial step), and because
    # it has its own two-stage form -> confirm sub-flow (see section 13
    # of the spec: "Confirm Payment Information" with masked details and
    # a confirmation checkbox before the step can complete).
    # ---------------------------------------------------------------
    if step_name == "bank":
        # Bank Account / Disbursement Information is asked of every applicant,
        # right after they state the amount they are requesting (Financial
        # step) and before Review. A draft that an older version marked
        # NOT_REQUIRED (when the step depended on opportunity matching) is
        # asked too; submitted applications keep their recorded status.
        if application["status"] == "Draft" and application["bank_step_status"] in ("NOT_STARTED", "NOT_REQUIRED"):
            db.execute(
                "UPDATE funding_applications SET bank_step_status = 'ACTION_REQUIRED', last_updated = CURRENT_TIMESTAMP "
                "WHERE id = ?", (application["id"],),
            )
            db.commit()
            application = db.execute("SELECT * FROM funding_applications WHERE id = ?", (application["id"],)).fetchone()

        if application["status"] == "Draft" and not application["requested_amount_ksh"]:
            flash("Please enter the amount of funding you are requesting first.", "warning")
            return redirect(url_for("application_step", step_name="financial"))

        if application["bank_step_status"] == "NOT_REQUIRED":
            # Already determined not needed on an earlier visit - just move on.
            if request.method == "POST":
                return redirect(url_for("application_step", step_name=APPLICATION_STEPS[step_index + 1]))
            return redirect(url_for("application_step", step_name=APPLICATION_STEPS[step_index + 1]))

        existing_details = db.execute(
            "SELECT * FROM student_bank_details WHERE application_id = ?", (application["id"],)
        ).fetchone()

        if request.method == "POST":
            action = request.form.get("action")

            if application["bank_step_status"] == "COMPLETE" and action == "continue":
                return redirect(url_for("application_step", step_name=APPLICATION_STEPS[step_index + 1]))

            if action == "save_details":
                country = request.form.get("country", "").strip()
                bank_id = request.form.get("bank_id") or None
                manual_bank_name = request.form.get("manual_bank_name", "").strip()
                account_holder_name = request.form.get("account_holder_name", "").strip()
                account_number = request.form.get("account_number", "").strip()
                account_type = request.form.get("account_type", "Savings")
                branch = request.form.get("branch", "").strip()
                bank_code = request.form.get("bank_code", "").strip()
                swift_bic = request.form.get("swift_bic", "").strip()
                iban = request.form.get("iban", "").strip()
                routing_number = request.form.get("routing_number", "").strip()
                mobile_money_provider = request.form.get("mobile_money_provider", "").strip()
                mobile_money_number = request.form.get("mobile_money_number", "").strip()

                if not country or country not in banks_lib.country_names():
                    flash("Please select a valid country.", "danger")
                    return redirect(url_for("application_step", step_name="bank"))
                if account_type not in banks_lib.ACCOUNT_TYPES:
                    account_type = "Other"

                bank_row = None
                if bank_id:
                    bank_row = db.execute(
                        "SELECT * FROM banks WHERE id = ? AND country = ? AND is_active = 1", (bank_id, country)
                    ).fetchone()

                # Required for disbursement: bank name (from the directory or
                # typed), account holder, account number (an edit may leave it
                # blank to keep the stored one), branch and bank code (the
                # directory's code counts). SWIFT/BIC, IBAN, routing number and
                # mobile money stay optional. No format rules beyond presence,
                # so legitimate international formats are never rejected.
                missing = [label for ok, label in (
                    (bank_row or manual_bank_name, "Bank Name"),
                    (account_holder_name, "Account Holder Name"),
                    (account_number or (existing_details and existing_details["account_number"]), "Account Number"),
                    (branch, "Branch"),
                    (bank_code or (bank_row and bank_row["bank_code"]), "Bank Code"),
                ) if not ok]
                if missing:
                    flash("Please fill in: " + ", ".join(missing) + ".", "danger")
                    return redirect(url_for("application_step", step_name="bank"))

                bank_name_final = bank_row["bank_name"] if bank_row else (manual_bank_name or None)
                verification_status = "DIRECTORY_MATCH" if bank_row else "MANUAL_REVIEW"

                if existing_details:
                    # Leaving account_number blank on an edit keeps the
                    # existing stored value - the student is never forced
                    # to re-type a number just to fix an unrelated field.
                    kept_account_number = account_number or existing_details["account_number"]
                    db.execute(
                        """UPDATE student_bank_details
                           SET country = ?, bank_id = ?, bank_name = ?, account_holder_name = ?,
                               account_number = ?, account_type = ?, branch = ?, bank_code = ?,
                               swift_bic = ?, iban = ?, routing_number = ?, currency = ?,
                               mobile_money_provider = ?, mobile_money_number = ?,
                               verification_status = ?, confirmed = 0, updated_at = CURRENT_TIMESTAMP
                           WHERE application_id = ?""",
                        (country, bank_row["id"] if bank_row else None, bank_name_final or "Not specified",
                         account_holder_name or existing_details["account_holder_name"], kept_account_number,
                         account_type, branch or None, bank_code or (bank_row["bank_code"] if bank_row else None),
                         swift_bic or (bank_row["swift_bic"] if bank_row else None), iban or None,
                         routing_number or None, banks_lib.currency_for_country(country),
                         mobile_money_provider or None, mobile_money_number or None,
                         verification_status, application["id"]),
                    )
                else:
                    db.execute(
                        """INSERT INTO student_bank_details
                           (student_id, application_id, country, bank_id, bank_name, account_holder_name,
                            account_number, account_type, branch, bank_code, swift_bic, iban, routing_number,
                            currency, mobile_money_provider, mobile_money_number, verification_status, confirmed)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                        (student["id"], application["id"], country, bank_row["id"] if bank_row else None,
                         bank_name_final or "Not specified", account_holder_name, account_number, account_type,
                         branch or None, bank_code or (bank_row["bank_code"] if bank_row else None),
                         swift_bic or (bank_row["swift_bic"] if bank_row else None), iban or None,
                         routing_number or None, banks_lib.currency_for_country(country),
                         mobile_money_provider or None, mobile_money_number or None, verification_status),
                    )
                db.commit()
                # Falls through to GET below, which now shows the
                # confirmation screen since a details row now exists.
                return redirect(url_for("application_step", step_name="bank"))

            if action == "confirm":
                if not existing_details:
                    return redirect(url_for("application_step", step_name="bank"))
                if not request.form.get("confirm_accurate"):
                    flash("Please confirm that your bank information is accurate before continuing.", "warning")
                    return redirect(url_for("application_step", step_name="bank"))
                db.execute(
                    "UPDATE student_bank_details SET confirmed = 1, updated_at = CURRENT_TIMESTAMP WHERE application_id = ?",
                    (application["id"],),
                )
                db.execute(
                    "UPDATE funding_applications SET bank_step_status = 'COMPLETE', last_updated = CURRENT_TIMESTAMP WHERE id = ?",
                    (application["id"],),
                )
                db.commit()
                flash("✅ Funding payment information saved.", "success")
                return redirect(url_for("application_step", step_name="bank"))

            if action == "edit":
                # Un-confirm so the entry form shows again instead of the
                # read-only confirmation screen - the row itself is kept.
                db.execute(
                    "UPDATE student_bank_details SET confirmed = 0, updated_at = CURRENT_TIMESTAMP WHERE application_id = ?",
                    (application["id"],),
                )
                if application["bank_step_status"] == "COMPLETE":
                    db.execute(
                        "UPDATE funding_applications SET bank_step_status = 'ACTION_REQUIRED' WHERE id = ?",
                        (application["id"],),
                    )
                db.commit()
                return redirect(url_for("application_step", step_name="bank"))

            flash("Please complete the form to continue.", "warning")
            return redirect(url_for("application_step", step_name="bank"))

        # GET
        application = db.execute("SELECT * FROM funding_applications WHERE id = ?", (application["id"],)).fetchone()
        existing_details = db.execute(
            "SELECT * FROM student_bank_details WHERE application_id = ?", (application["id"],)
        ).fetchone()
        masked_account = banks_lib.mask_account_number(existing_details["account_number"]) if existing_details else None
        return render_template(
            "application_bank_step.html", application=application, step_index=step_index, steps=APPLICATION_STEPS, step_titles=APPLICATION_STEP_TITLES,
            existing_details=existing_details, masked_account=masked_account,
            countries=banks_lib.country_names(), account_types=banks_lib.ACCOUNT_TYPES,
        )

    if request.method == "POST":
        form = request.form
        updates = {}

        if step_name == "personal":
            for f in ["full_name", "date_of_birth", "country", "citizenship", "phone", "email", "gender"]:
                updates[f] = form.get(f)
        elif step_name == "education":
            for f in ["institution", "education_level", "course", "field_of_study", "year_of_study",
                      "academic_info", "graduation_year"]:
                updates[f] = form.get(f)
        elif step_name == "funding_need":
            for f in ["funding_type_needed", "tuition_need", "accommodation_need", "living_expenses_need",
                      "books_need", "transport_need", "technology_need", "other_expenses"]:
                updates[f] = form.get(f)
        elif step_name == "financial":
            requested = parse_requested_amount_ksh(form.get("requested_amount_ksh"))
            if requested is None:
                flash(REQUESTED_AMOUNT_MESSAGE, "danger")
                return redirect(url_for("application_step", step_name="financial"))
            updates["requested_amount_ksh"] = requested
            for f in ["household_situation", "source_of_support", "estimated_financial_need",
                      "funding_already_received"]:
                updates[f] = form.get(f)
        elif step_name == "preferences":
            updates["preferences"] = ", ".join(form.getlist("preferences"))
        elif step_name == "statement":
            updates["personal_statement"] = form.get("personal_statement")
        elif step_name == "documents":
            _ensure_funding_document_checklist(db, application["id"])
            document_rows = db.execute(
                "SELECT * FROM documents WHERE application_id = ? ORDER BY id",
                (application["id"],),
            ).fetchall()
            for row in document_rows:
                if row["is_required"]:
                    continue
                answer = (request.form.get(f"document_{row['id']}_availability") or "").strip().lower()
                if answer not in ("yes", "no"):
                    flash(f'Please choose Yes or No for "{row["document_type"]}".', "warning")
                    return redirect(url_for("application_step", step_name="documents"))
                db.execute("UPDATE documents SET availability = ? WHERE id = ?",
                           (answer.title(), row["id"]))
            db.commit()
            document_rows = db.execute(
                "SELECT * FROM documents WHERE application_id = ? ORDER BY id",
                (application["id"],),
            ).fetchall()
            missing_docs = _missing_funding_documents(document_rows)
            if missing_docs:
                flash("Please upload: " + ", ".join(missing_docs) + ".", "warning")
                return redirect(url_for("application_step", step_name="documents"))
        elif step_name == "review":
            # Review leads to the FINAL step (Visa Verification), so make
            # sure the earlier steps that must be filled actually are.
            missing = [label for f, label in (("full_name", "full name"), ("email", "email"),
                                               ("country", "country"))
                       if not (application[f] or "").strip()]
            if missing:
                flash("Please complete your " + ", ".join(missing) + " before continuing.", "warning")
                return redirect(url_for("application_step", step_name="personal"))
            if application["bank_step_status"] != "COMPLETE":
                flash("Please complete the Bank Account / Disbursement Information step before continuing.", "warning")
                return redirect(url_for("application_step", step_name="bank"))
            if not application["requested_amount_ksh"]:
                flash(REQUESTED_AMOUNT_MESSAGE, "warning")
                return redirect(url_for("application_step", step_name="financial"))

        if updates:
            set_clause = ", ".join([f"{k} = ?" for k in updates])
            db.execute(
                f"UPDATE funding_applications SET {set_clause}, last_updated = CURRENT_TIMESTAMP WHERE id = ?",
                (*updates.values(), application["id"]),
            )

        next_step = min(step_index + 1, len(APPLICATION_STEPS) - 1)
        db.execute("UPDATE funding_applications SET current_step = ? WHERE id = ?",
                   (next_step + 1, application["id"]))
        db.commit()

        return redirect(url_for("application_step", step_name=APPLICATION_STEPS[step_index + 1]))

    # Refresh application after any earlier commits
    application = db.execute("SELECT * FROM funding_applications WHERE id = ?", (application["id"],)).fetchone()
    _ensure_funding_document_checklist(db, application["id"])
    db.commit()
    documents = db.execute("SELECT * FROM documents WHERE application_id = ?", (application["id"],)).fetchall()
    review_bank, review_masked_account = None, None
    if step_name == "review":
        # Shown masked only - the full account number never reaches the page.
        review_bank = db.execute("SELECT * FROM student_bank_details WHERE application_id = ? AND confirmed = 1",
                                 (application["id"],)).fetchone()
        if review_bank:
            review_masked_account = banks_lib.mask_account_number(review_bank["account_number"])

    return render_template(
        "application.html", application=application, step_name=step_name, step_index=step_index,
        steps=APPLICATION_STEPS, step_titles=APPLICATION_STEP_TITLES, documents=documents,
        review_bank=review_bank, review_masked_account=review_masked_account,
    )


@app.route("/application/visa-document/upload", methods=["POST"])
@login_required
def application_visa_document_upload():
    """Handles the 'Verify & Continue' form on the visa step's Path A
    (student says they already have a U.S. visa).

    RECEIVING A FILE IS NOT VERIFYING A VISA. The step is marked COMPLETE,
    and the success message shown, ONLY after visa_verification has
    automatically checked the entered details AND the document itself
    and every check passed. Any failure - wrong, incomplete, expired,
    inconsistent, unsupported or unreadable - immediately moves the
    student to the "I do not have a U.S. visa" assistance path (same
    application, same visa request, nothing duplicated). There is no
    manual approval step.
    """
    db = g.db
    student = current_student()
    cycle = get_current_cycle()
    application = db.execute(
        "SELECT * FROM funding_applications WHERE student_id = ? AND cycle_id = ?",
        (student["id"], cycle["id"]) if cycle else (student["id"], -1),
    ).fetchone()
    if not application:
        return redirect(url_for("application_start"))
    if application["status"] != "Draft":
        return redirect(url_for("dashboard"))
    application = _revoke_unverified_visa_step(db, application)
    if application["visa_status"] != "HAS_VISA" or application["visa_step_status"] != "ACTION_REQUIRED":
        # Wrong state to be hitting this route (already verified, on the
        # assistance path, or never chose "Yes") - show the real state.
        return redirect(url_for("application_step", step_name="visa"))

    uploaded_file = request.files.get("visa_document")
    filename = uploaded_file.filename if uploaded_file and uploaded_file.filename else ""
    # Bounded by MAX_CONTENT_LENGTH; one extra byte lets the size check
    # see an over-limit file.
    file_bytes = uploaded_file.stream.read(MAX_VISA_DOC_SIZE_BYTES + 1) if filename else b""

    verified, details, errors = visa_verify.verify_visa_submission(
        request.form, file_bytes, filename,
        {"full_name": application["full_name"] or student["full_name"],
         "date_of_birth": application["date_of_birth"]},
    )
    if not verified:
        return _fail_visa_verification(db, student, application, cycle,
                                       errors or ["Visa could not be verified."])

    # ---- Every automatic check passed: only now is anything recorded as done.
    stored_filename = save_verified_visa_document(file_bytes, _visa_doc_extension(filename))
    db.execute(
        """UPDATE funding_applications
           SET visa_document_status = 'UPLOADED', visa_document_path = ?, visa_document_original_name = ?,
               visa_document_uploaded_at = CURRENT_TIMESTAMP, visa_document_type = ?,
               visa_document_issue_date = ?, visa_document_expiry_date = ?, visa_document_passport_number = ?,
               visa_document_notes = ?, visa_verification_status = 'VERIFIED',
               visa_verification_notes = 'All automatic visa information checks passed (not a U.S. government authentication).', visa_verified_at = CURRENT_TIMESTAMP,
               visa_step_status = 'COMPLETE', last_updated = CURRENT_TIMESTAMP
           WHERE id = ? AND visa_status = 'HAS_VISA' AND visa_step_status = 'ACTION_REQUIRED'""",
        (stored_filename, filename[:255], details["visa_type"],
         details["issue_date"], details["expiry_date"],
         details["passport_number"], details["notes"] or None,
         application["id"]),
    )
    add_history(db, application["id"], application["status"],
                f"U.S. visa information passed all automatic checks (read from: {details.get('read_from')}).")
    add_notification(db, student["id"], "✅ Your U.S. visa information was verified successfully.")
    db.commit()
    session.pop(VISA_FAILED_SESSION_KEY, None)
    flash("✅ Visa uploaded successfully. Visa information verified successfully.", "success")
    return redirect(url_for("application_step", step_name="visa"))


@app.route("/application/visa-document/view")
@login_required
def application_visa_document_view():
    """Lets the STUDENT securely view/download their own uploaded visa
    document. There is no public URL for this file - it only exists
    inside uploads/visa_documents/, and this route is the only way to
    reach it, gated by login + an ownership check.
    """
    db = g.db
    student = current_student()
    cycle = get_current_cycle()
    application = db.execute(
        "SELECT * FROM funding_applications WHERE student_id = ? AND cycle_id = ?",
        (student["id"], cycle["id"]) if cycle else (student["id"], -1),
    ).fetchone()
    if not application or not application["visa_document_path"]:
        abort(404)
    return send_from_directory(VISA_DOCS_DIR, application["visa_document_path"], as_attachment=False)


# ---------------------------------------------------------------------
# 🏦 BANK DIRECTORY LOOKUP - a small JSON API the "bank" step's page uses
# to dynamically populate the bank dropdown once a country is chosen, and
# to power the "Search your bank" box (filter by name/code/SWIFT). Read-
# only, login-required (no bank directory browsing for anonymous
# visitors, and no student can modify it - only /admin/banks can).
# ---------------------------------------------------------------------
@app.route("/api/banks")
@login_required
def api_banks():
    db = g.db
    country = request.args.get("country", "").strip()
    q = request.args.get("q", "").strip()

    query = "SELECT id, bank_name, country, bank_code, swift_bic, status FROM banks WHERE is_active = 1"
    params = []
    if country:
        query += " AND country = ?"
        params.append(country)
    if q:
        query += " AND (bank_name LIKE ? OR bank_code LIKE ? OR swift_bic LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    query += " ORDER BY bank_name"

    rows = db.execute(query, params).fetchall()
    return {"banks": [dict(r) for r in rows]}


@app.route("/application/bank-details/remove", methods=["POST"])
@login_required
def application_bank_details_remove():
    """Lets the student remove the payment/bank information they
    submitted (e.g. from the dashboard's "REMOVE PAYMENT INFORMATION"
    button). Never available to any other student, and never touches
    anyone else's data - scoped strictly to the caller's own application.
    """
    db = g.db
    student = current_student()
    cycle = get_current_cycle()
    application = db.execute(
        "SELECT * FROM funding_applications WHERE student_id = ? AND cycle_id = ?",
        (student["id"], cycle["id"]) if cycle else (student["id"], -1),
    ).fetchone()
    if not application:
        return redirect(url_for("dashboard"))

    db.execute("DELETE FROM student_bank_details WHERE application_id = ?", (application["id"],))
    if application["bank_step_status"] == "COMPLETE":
        db.execute(
            "UPDATE funding_applications SET bank_step_status = 'ACTION_REQUIRED', last_updated = CURRENT_TIMESTAMP WHERE id = ?",
            (application["id"],),
        )
    db.commit()
    flash("Your funding payment information was removed. You can add it again at any time.", "info")
    return redirect(url_for("dashboard"))


def _submit_funding_application(db, application_id, student_id):
    """Submits a Draft annual funding application: reference number,
    confirmation email, matching. Used by the student's own final submit
    and, on the visa assistance path, automatically once the visa
    assistance payment has been verified. Returns (reference, email_ok)
    or (None, None) when the application is not a Draft any more."""
    application = db.execute("SELECT * FROM funding_applications WHERE id = ?", (application_id,)).fetchone()
    if not application or application["status"] != "Draft":
        return None, None
    cycle = db.execute("SELECT * FROM funding_cycles WHERE id = ?", (application["cycle_id"],)).fetchone()
    ref = generate_reference_number(db, cycle["year"].split("/")[0])
    cur = db.execute(
        """UPDATE funding_applications
           SET status = 'Submitted', reference_number = ?, submitted_at = CURRENT_TIMESTAMP,
               last_updated = CURRENT_TIMESTAMP
           WHERE id = ? AND status = 'Draft'""",
        (ref, application_id),
    )
    if cur.rowcount != 1:
        return None, None
    add_history(db, application_id, "Submitted", "Application submitted by student.")
    add_notification(db, student_id, f"🎓 Your application {ref} has been received.")
    db.commit()

    # 📧 The application is now genuinely saved as Submitted with a
    # real reference number - only NOW do we send the confirmation
    # email. An email failure never reverses the submission; it only
    # ever changes confirmation_email_status.
    email_sent_ok = send_application_confirmation_email(db, application_id)

    # Move straight to eligibility review + matching.
    db.execute("UPDATE funding_applications SET status = 'Funding Matching' WHERE id = ?", (application_id,))
    add_history(db, application_id, "Funding Matching", "Automatically queued for matching.")
    add_notification(db, student_id, "🔎 Your application is currently being matched with funding opportunities.")
    db.commit()

    run_matching_for_application(db, application_id)

    match_count = db.execute(
        "SELECT COUNT(*) c FROM funding_matches WHERE application_id = ?", (application_id,)
    ).fetchone()["c"]
    new_status = "Matched" if match_count > 0 else "No Suitable Match"
    db.execute("UPDATE funding_applications SET status = ? WHERE id = ?", (new_status, application_id))
    add_history(db, application_id, new_status)
    if match_count > 0:
        add_notification(db, student_id, f"🎯 You have received {match_count} new funding match(es).")
    db.commit()
    return ref, email_sent_ok


@app.route("/application/submit", methods=["GET", "POST"])
@login_required
def application_submit():
    db = g.db
    student = current_student()
    cycle = get_current_cycle()
    application = db.execute(
        "SELECT * FROM funding_applications WHERE student_id = ? AND cycle_id = ?",
        (student["id"], cycle["id"]),
    ).fetchone()
    if not application or application["status"] != "Draft":
        return redirect(url_for("dashboard"))

    # Every step - Visa Verification last - must be done first.
    application = _revoke_unverified_visa_step(db, application)
    if not _review_done(application):
        flash(VISA_STEP_NOT_READY_MESSAGE, "warning")
        return redirect(url_for("application_step", step_name=_first_unfinished_step(application)))
    if not _visa_requirement_passed(application):
        flash(VISA_STEP_REQUIRED_MESSAGE, "warning")
        return redirect(url_for("application_step", step_name="visa"))

    if request.method == "POST":
        ref, email_sent_ok = _submit_funding_application(db, application["id"], student["id"])
        if ref is None:
            return redirect(url_for("dashboard"))
        if email_sent_ok:
            flash(f"Your application was submitted successfully! Reference: {ref}", "success")
        else:
            # The application itself is fine - only the email failed.
            # We never tell the student their APPLICATION failed here.
            flash(
                "Your application was successfully submitted. We could not deliver the confirmation "
                "email at this time. Please check your email address or contact support.",
                "warning",
            )
        return redirect(url_for("application_confirmation", application_id=application["id"]))

    return render_template("application_review.html", application=application)


@app.route("/student/applications")
@login_required
def student_applications():
    """A student's full annual-application history across every funding
    cycle they've applied in (this app has one central application per
    student per cycle - see the UNIQUE(student_id, cycle_id) constraint),
    each showing its reference, cycle, status, submission date, and
    confirmation email status."""
    db = g.db
    student = current_student()
    applications = db.execute(
        """SELECT a.*, c.name AS cycle_name FROM funding_applications a
           JOIN funding_cycles c ON a.cycle_id = c.id
           WHERE a.student_id = ? ORDER BY a.id DESC""",
        (student["id"],),
    ).fetchall()
    return render_template("student_applications.html", applications=applications)


@app.route("/application/confirmation/<int:application_id>")
@login_required
def application_confirmation(application_id):
    db = g.db
    student = current_student()
    application = db.execute(
        "SELECT * FROM funding_applications WHERE id = ? AND student_id = ?",
        (application_id, student["id"]),
    ).fetchone()
    if not application:
        return render_template("errors/404.html"), 404
    cycle = db.execute("SELECT * FROM funding_cycles WHERE id = ?", (application["cycle_id"],)).fetchone()
    visa_request = None
    if application["visa_request_id"]:
        visa_request = db.execute("SELECT * FROM visa_requests WHERE id = ? AND student_id = ?",
                                  (application["visa_request_id"], student["id"])).fetchone()
    return render_template("application_confirmation.html", application=application, cycle=cycle,
                           visa_request=visa_request, format_price=visa_lib.format_price)


@app.route("/matches")
@login_required
def matches():
    db = g.db
    student = current_student()
    cycle = get_current_cycle()
    application = None
    match_rows = []
    if cycle:
        application = db.execute(
            "SELECT * FROM funding_applications WHERE student_id = ? AND cycle_id = ?",
            (student["id"], cycle["id"]),
        ).fetchone()
    if application:
        match_rows = db.execute(
            """SELECT m.*, o.title, o.funding_type, o.amount, o.close_date, o.application_method,
                      o.application_url, o.study_destination, org.name AS org_name
               FROM funding_matches m
               JOIN funding_opportunities o ON m.opportunity_id = o.id
               JOIN funding_programs p ON o.program_id = p.id
               JOIN organizations org ON p.organization_id = org.id
               WHERE m.application_id = ? ORDER BY m.score DESC""",
            (application["id"],),
        ).fetchall()
    return render_template("funding_matches.html", application=application, matches=match_rows)


@app.route("/matches/<int:match_id>/refer", methods=["POST"])
@login_required
def refer_match(match_id):
    db = g.db
    student = current_student()
    match = db.execute(
        """SELECT m.* FROM funding_matches m
           JOIN funding_applications a ON m.application_id = a.id
           WHERE m.id = ? AND a.student_id = ?""",
        (match_id, student["id"]),
    ).fetchone()
    if not match:
        return render_template("errors/404.html"), 404

    existing = db.execute("SELECT id FROM provider_referrals WHERE match_id = ?", (match_id,)).fetchone()
    if not existing:
        db.execute("INSERT INTO provider_referrals (match_id, status) VALUES (?, 'Referred')", (match_id,))
        db.execute("UPDATE funding_applications SET status = 'Provider Referral' WHERE id = ?", (match["application_id"],))
        add_history(db, match["application_id"], "Provider Referral", "Student proceeded with a provider referral.")
        add_notification(db, student["id"], "📢 Your provider referral has been recorded.")
        db.commit()
        flash("Provider referral recorded. Track its progress from your Application Tracker.", "success")
    return redirect(url_for("matches"))


@app.route("/tracker")
@login_required
def tracker():
    db = g.db
    student = current_student()
    cycle = get_current_cycle()
    application = None
    referrals = []
    if cycle:
        application = db.execute(
            "SELECT * FROM funding_applications WHERE student_id = ? AND cycle_id = ?",
            (student["id"], cycle["id"]),
        ).fetchone()
    if application:
        referrals = db.execute(
            """SELECT r.*, o.title, d.decision, d.funding_amount FROM provider_referrals r
               JOIN funding_matches m ON r.match_id = m.id
               JOIN funding_opportunities o ON m.opportunity_id = o.id
               LEFT JOIN funding_decisions d ON d.referral_id = r.id
               WHERE m.application_id = ?""",
            (application["id"],),
        ).fetchall()

    # Work out how far along the visual tracker the student is.
    status_to_step = {
        "Draft": 0, "Submitted": 1, "Received": 1, "Eligibility Review": 2,
        "Information Required": 2, "Funding Matching": 3, "Documents Review": 4,
        "Matched": 4, "Provider Referral": 5, "Provider Application": 5,
        "Provider Review": 6, "Decision Pending": 7, "Funded": 8,
        "Unsuccessful": 8, "No Suitable Match": 8, "Withdrawn": 8, "Cycle Closed": 8,
    }
    current_step = status_to_step.get(application["status"], 0) if application else 0

    return render_template("tracker.html", application=application, referrals=referrals,
                            steps=TRACKER_STEPS, current_step=current_step)


@app.route("/documents", methods=["GET", "POST"])
@login_required
def documents():
    db = g.db
    student = current_student()
    cycle = get_current_cycle()
    application = None
    if cycle:
        application = db.execute(
            "SELECT * FROM funding_applications WHERE student_id = ? AND cycle_id = ?",
            (student["id"], cycle["id"]),
        ).fetchone()
    if not application:
        flash("Start your application first to manage documents.", "info")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        doc_id = request.form.get("document_id")
        row = db.execute(
            "SELECT * FROM documents WHERE id = ? AND application_id = ?",
            (doc_id, application["id"]),
        ).fetchone()
        if not row:
            abort(404)
        db.execute(
            "UPDATE documents SET status = 'Uploaded', availability = 'Yes', uploaded_at = CURRENT_TIMESTAMP, file_path = ? "
            "WHERE id = ? AND application_id = ?",
            (f"uploads/demo-{doc_id}.pdf", doc_id, application["id"]),
        )
        db.commit()
        flash("Document marked as uploaded.", "success")
        return redirect(url_for("documents"))

    docs = db.execute("SELECT * FROM documents WHERE application_id = ?", (application["id"],)).fetchall()
    return render_template("documents.html", application=application, documents=docs,
                           missing_documents=_missing_funding_documents(docs))


@app.route("/notifications/mark-read/<int:notification_id>", methods=["POST"])
@login_required
def mark_notification_read(notification_id):
    db = g.db
    student = current_student()
    db.execute(
        "UPDATE notifications SET is_read = 1 WHERE id = ? AND student_id = ?",
        (notification_id, student["id"]),
    )
    db.commit()
    return redirect(request.referrer or url_for("dashboard"))


# =======================================================================
# 🇺🇸 U.S. STUDENT VISA APPLICATION ASSISTANCE
#
# Flow:  PAY -> VERIFY -> UNLOCK -> CONTINUE APPLICATION
#
# The gate is enforced in one place: visa_lib.is_unlocked(visa_request),
# checked server-side on every route that reaches the application steps.
# Reaching a URL, a "success" page, or any client-side state never
# unlocks anything by itself.
# =======================================================================
@app.route("/student-visa")
def student_visa_landing():
    db = g.db
    student = current_student()
    country = student["country"] if student else "Kenya"
    pricing = visa_lib.get_pricing_for_country(db, country)
    active_request = get_active_visa_request(db, student) if student else None
    return render_template(
        "student_visa/landing.html", pricing=pricing,
        us_gov_fee=visa_lib.US_GOV_VISA_FEE_USD, active_request=active_request,
        student_fee=visa_lib.STUDENT_SERVICE_FEE_USD, format_usd=visa_lib.format_usd,
    )


@app.route("/student-visa/start", methods=["POST"])
@login_required
def student_visa_start():
    db = g.db
    student = current_student()

    existing = get_active_visa_request(db, student)
    if existing and existing["payment_status"] in ("pending", "pending_verification", "paid"):
        flash("You already have a visa assistance request in progress.", "info")
        if visa_lib.is_unlocked(existing):
            return redirect(url_for("student_visa_dashboard"))
        return redirect(url_for("student_visa_payment", request_id=existing["id"]))

    pricing = visa_lib.get_pricing_for_country(db, student["country"])
    if not pricing:
        flash("Visa assistance pricing is not yet configured for your country. Please contact support.", "danger")
        return redirect(url_for("student_visa_landing"))

    ref = visa_lib.generate_visa_request_number(db, datetime.utcnow().year)
    pricing = dict(pricing)
    pricing["service_price"] = _visa_service_fee(db)
    pricing["currency"] = "KES"
    pricing["currency_symbol"] = "KSh"
    cur = db.execute(
        """INSERT INTO visa_requests
           (request_number, student_id, country, currency, currency_symbol, service_price,
            full_name, email, phone, citizenship, country_of_residence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ref, student["id"], pricing["country"], pricing["currency"], pricing["currency_symbol"],
         pricing["service_price"], student["full_name"],
         db.execute("SELECT email FROM users WHERE id = ?", (student["user_id"],)).fetchone()["email"],
         student["phone"], student["citizenship"], student["country"]),
    )
    request_id = cur.lastrowid
    visa_lib.add_visa_history(db, request_id, "payment_required", "Visa assistance request created.")

    for doc_type, required in visa_lib.VISA_DOCUMENT_CHECKLIST:
        db.execute(
            "INSERT INTO visa_documents (request_id, document_type, is_required) VALUES (?, ?, ?)",
            (request_id, doc_type, 1 if required else 0),
        )
    add_notification(db, student["id"], f"🇺🇸 Visa assistance request {ref} created. Complete payment to continue.")
    db.commit()

    return redirect(url_for("student_visa_payment", request_id=request_id))


# ---------------------------------------------------------------------
# 🇺🇸 VISA PAYMENT - direct M-PESA to a phone number, then the student
# submits the confirmation SMS (and/or a screenshot). The flow is:
#
#   student pays -> submits SMS/screenshot -> server parses & validates
#   -> PAYMENT_PENDING -> Visa Admin compares & clicks VERIFY PAYMENT
#   -> PAYMENT_VERIFIED -> visa application unlocked
#
# Nothing in these student routes can mark a payment verified. Only
# _verify_visa_payment() does that, and only an admin route calls it.
# ---------------------------------------------------------------------
PAYMENT_STATUS_LABELS = {
    "PAYMENT_PENDING": "Pending Verification",
    "PAYMENT_VERIFIED": "Verified",
    "PAYMENT_REJECTED": "Rejected",
}
MPESA_CODE_PATTERN = re.compile(r"^[A-Z0-9]{10}$")


def _visa_payment_ready(db, visa_request):
    """Payment is offered only once the required documents are uploaded
    (form-first requests) - see visa_lib.payment_ready."""
    _ensure_visa_checklist(db, visa_request["id"])
    return visa_lib.payment_ready(visa_request, _checklist_documents(db, visa_request["id"]))


def _latest_visa_payment(db, request_id):
    return db.execute(
        "SELECT * FROM visa_payments WHERE request_id=? ORDER BY id DESC LIMIT 1", (request_id,)
    ).fetchone()


def _payment_flags(payment):
    if not payment or not payment["validation_flags"]:
        return []
    try:
        return json.loads(payment["validation_flags"])
    except (TypeError, ValueError):
        return []


def _parsed_for_display(parsed):
    """JSON/template-friendly version of mpesa_parser output."""
    return {
        "direction": parsed["direction"],
        "transaction_code": parsed["transaction_code"],
        "amount": f"{parsed['amount']:,.2f}" if parsed["amount"] is not None else None,
        "counterparty_name": parsed["counterparty_name"],
        "counterparty_phone": parsed["counterparty_phone"],
        "date": parsed["date"],
        "time": parsed["time"],
    }


@app.route("/student-visa/payment/<int:request_id>")
@login_required
def student_visa_payment(request_id):
    db = g.db
    student = current_student()
    visa_request = get_visa_request_or_404(db, request_id, student)
    if not visa_request:
        return render_template("errors/404.html"), 404
    if not _visa_payment_ready(db, visa_request):
        flash("Please complete the visa application form and upload your required documents before payment.",
              "warning")
        return redirect(url_for("student_visa_application", request_id=request_id))
    if visa_lib.is_unlocked(visa_request) and visa_request["annual_application_id"]:
        return _visa_post_unlock_redirect(visa_request)
    payment = _latest_visa_payment(db, request_id)
    return render_template(
        "student_visa/payment.html", visa_request=visa_request, payment=payment,
        payment_flags=_payment_flags(payment), unlocked=visa_lib.is_unlocked(visa_request),
        status_labels=PAYMENT_STATUS_LABELS,
        mpesa_receiving_number=_visa_payment_recipient(db),
        visa_fee=_visa_service_fee(db), us_gov_fee=visa_lib.US_GOV_VISA_FEE_USD,
        format_price=visa_lib.format_price,
        max_proof_mb=MAX_PAYMENT_PROOF_SIZE_BYTES // (1024 * 1024),
        max_message_length=mpesa_parser.MAX_MESSAGE_LENGTH,
        **_paystack_context(db, visa_request, student),
    )


@app.route("/student-visa/payment/<int:request_id>/parse", methods=["POST"])
@login_required
def student_visa_payment_parse(request_id):
    """Live preview for the payment page: runs the SAME server-side parser
    the submit route uses and returns what it detected. Stores nothing,
    verifies nothing."""
    db = g.db
    student = current_student()
    visa_request = get_visa_request_or_404(db, request_id, student)
    if not visa_request:
        abort(404)
    if not _visa_payment_ready(db, visa_request):
        abort(409)
    text = (request.form.get("mpesa_message") or "")[: mpesa_parser.MAX_MESSAGE_LENGTH * 2]
    parsed = mpesa_parser.parse_mpesa_message(text)
    flags = mpesa_parser.validate_student_message(
        parsed, _visa_service_fee(db), _visa_payment_recipient(db)) if text.strip() else []
    return {"detected": _parsed_for_display(parsed), "flags": flags}


@app.route("/student-visa/payment/<int:request_id>/submit", methods=["POST"])
@login_required
def student_visa_payment_submit(request_id):
    """Record the student's M-PESA SMS and/or screenshot as PAYMENT_PENDING.
    This NEVER verifies the payment or unlocks the application."""
    db = g.db
    student = current_student()
    visa_request = get_visa_request_or_404(db, request_id, student)
    if not visa_request:
        return render_template("errors/404.html"), 404
    if not _visa_payment_ready(db, visa_request):
        flash("Please complete the visa application form and upload your required documents before payment.",
              "warning")
        return redirect(url_for("student_visa_application", request_id=request_id))
    back = redirect(url_for("student_visa_payment", request_id=request_id))
    if paystack_lib.is_configured():
        # Fallback only: with Paystack configured, payments go through Paystack.
        flash("Please pay with Paystack (M-PESA) below.", "info")
        return redirect(_paystack_page_url(visa_request))

    latest = _latest_visa_payment(db, request_id)
    if visa_lib.is_unlocked(visa_request) or (latest and latest["payment_status"] == "PAYMENT_VERIFIED"):
        flash("Your payment has already been verified.", "info")
        return back

    raw_message = request.form.get("mpesa_message") or ""
    sms_text = raw_message.strip()
    proof_file = request.files.get("payment_proof")
    has_file = bool(proof_file and proof_file.filename)
    manual_code = (request.form.get("transaction_reference") or "").strip().upper()
    payer_phone = (request.form.get("payment_phone") or "").strip()[:20] or None
    additional_comment = (request.form.get("additional_comment") or "").strip()[:500] or None

    if not sms_text and not has_file:
        flash("Please paste your M-PESA confirmation message or upload a screenshot of it.", "warning")
        return back
    if len(sms_text) > mpesa_parser.MAX_MESSAGE_LENGTH:
        flash("That message is too long to be an M-PESA confirmation. Paste only the confirmation SMS.", "warning")
        return back
    if payer_phone and len("".join(c for c in payer_phone if c.isdigit())) < 9:
        flash("Please enter a valid phone number for the number you paid from (or leave it blank).", "warning")
        return back

    expected_amount = _visa_service_fee(db)
    recipient_phone = _visa_payment_recipient(db)

    if sms_text:
        # Extracted SERVER-SIDE from the original message. Any code typed
        # into the manual box is ignored - students cannot override what
        # the SMS says.
        parsed = mpesa_parser.parse_mpesa_message(sms_text)
        flags = mpesa_parser.validate_student_message(parsed, expected_amount, recipient_phone)
        if mpesa_parser.has_errors(flags):
            for f in flags:
                if f["severity"] == "error":
                    flash(f["message"], "danger")
            return back
        code = parsed["transaction_code"]
        code_source = "sms"
        submitted_amount = float(parsed["amount"])
    else:
        # Screenshot only: the code has to be typed so duplicates can be
        # caught, but it is clearly labelled as student-typed, not extracted.
        if not MPESA_CODE_PATTERN.match(manual_code):
            flash("When you upload only a screenshot, please also type the 10-character M-PESA "
                  "transaction code shown on it (e.g. UIR1N8OV43).", "warning")
            return back
        parsed = mpesa_parser.parse_mpesa_message("")
        code = manual_code
        code_source = "manual"
        submitted_amount = None
        flags = [{"code": "screenshot_only", "severity": "warning",
                  "message": "No SMS text was submitted. The transaction code was TYPED by the student, "
                             "not extracted - check it against the screenshot and the receiving phone."}]

    # Duplicate transaction codes (live index + legacy column).
    pending_own_id = latest["id"] if latest and latest["payment_status"] == "PAYMENT_PENDING" else None
    clashes = db.execute(
        """SELECT id, student_id, payment_status FROM visa_payments
           WHERE (mpesa_transaction_code = ? OR UPPER(transaction_reference) = ?) AND id != ?""",
        (code, code, pending_own_id or -1),
    ).fetchall()
    for c in clashes:
        if c["student_id"] != student["id"] or c["payment_status"] != "PAYMENT_REJECTED":
            flash(f"The M-PESA transaction code {code} has already been submitted. Each payment can only "
                  f"be used once. Contact support if you believe this is a mistake.", "danger")
            return back
    if clashes:
        flags.append({"code": "previously_rejected", "severity": "warning",
                      "message": f"This student previously submitted code {code} and it was rejected."})

    stored_filename = None
    if has_file:
        stored_filename, error = save_payment_proof(proof_file, proof_file.filename)
        if error:
            flash(error, "danger")
            return back

    account = db.execute("SELECT email FROM users WHERE id=?", (student["user_id"],)).fetchone()
    now = datetime.utcnow().isoformat(timespec="seconds")
    values = {
        "amount": expected_amount, "currency": "KES", "payment_method": "M-Pesa (Direct to Phone - Manual)",
        "transaction_reference": code, "mpesa_transaction_code": code, "code_source": code_source,
        "phone_number": payer_phone, "additional_comment": additional_comment,
        "student_name": student["full_name"], "student_email": account["email"] if account else None,
        "student_phone": student["phone"],
        "expected_amount": expected_amount, "submitted_amount": submitted_amount,
        "submitted_mpesa_message": raw_message if sms_text else None,  # preserved exactly as pasted
        "message_direction": parsed["direction"] if sms_text else None,
        "extracted_recipient_name": parsed["counterparty_name"],
        "extracted_recipient_phone": parsed["counterparty_phone"],
        "extracted_transaction_date": parsed["date"], "extracted_transaction_time": parsed["time"],
        "validation_flags": json.dumps(flags), "submitted_at": now,
        "payment_status": "PAYMENT_PENDING", "proof_status": "Pending Verification",
        "status": "pending", "verified": 0,
    }
    try:
        if pending_own_id:
            # Resubmission while still pending replaces the pending
            # submission (keeping the old screenshot if no new one).
            if stored_filename:
                values["proof_file"] = stored_filename
            set_clause = ", ".join(f"{k}=?" for k in values)
            db.execute(f"UPDATE visa_payments SET {set_clause}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (*values.values(), pending_own_id))
            visa_lib.add_visa_history(db, request_id, "pending_verification",
                                      f"Student resubmitted M-PESA details (previous code: "
                                      f"{latest['mpesa_transaction_code'] or latest['transaction_reference'] or '—'}).")
        else:
            values.update({"request_id": request_id, "student_id": student["id"], "proof_file": stored_filename})
            cols = ", ".join(values)
            db.execute(f"INSERT INTO visa_payments ({cols}) VALUES ({', '.join('?' for _ in values)})",
                       tuple(values.values()))
    except sqlite3.IntegrityError:
        db.rollback()
        if stored_filename:
            try:
                os.remove(os.path.join(PAYMENT_PROOF_DIR, stored_filename))
            except OSError:
                pass
        flash(f"The M-PESA transaction code {code} has already been submitted.", "danger")
        return back

    db.execute(
        "UPDATE visa_requests SET payment_status='pending_verification', payment_verified=0, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?", (request_id,)
    )
    visa_lib.add_visa_history(db, request_id, "pending_verification",
                              f"M-PESA details submitted (code {code}). Awaiting manual admin verification.")
    db.execute(
        "INSERT INTO visa_admin_notifications (request_id, message) VALUES (?, ?)",
        (request_id, f"💳 Payment awaiting verification\nStudent: {student['full_name']}\n"
                     f"Code: {code} ({'from SMS' if code_source == 'sms' else 'typed, screenshot only'})"),
    )
    db.commit()

    if account and email_lib.is_configured():
        email_lib.send_email(
            account["email"], "Africa ScholarBridge — Payment Details Received",
            f"""Hello {student["full_name"]},

Your M-PESA payment details for visa application assistance have been received.

Reference: {visa_request["request_number"]}
Transaction code: {code}
Status: Pending Verification

Your payment details have been submitted and are awaiting verification.
Africa ScholarBridge staff will check the payment on the receiving M-PESA
account. Your visa application unlocks once the payment is verified.

Africa ScholarBridge
"""
        )

    for f in flags:
        if f["severity"] == "warning" and f["code"] != "screenshot_only":
            flash(f"Note for review: {f['message']}", "warning")
    flash("Your payment details have been submitted and are awaiting verification.", "success")
    return back



# ---------------------------------------------------------------------
# 💳 PAYSTACK - online payment of the SAME visa assistance fee (M-PESA
# prompt via the Charge API, or Paystack's hosted checkout). Used whenever
# PAYSTACK_SECRET_KEY is set; otherwise the manual M-PESA SMS flow above is
# shown instead. A Paystack row in visa_payments unlocks the service ONLY
# after the server verified the transaction with Paystack
# (_apply_paystack_result): status success + exact amount + KES + same
# reference. Callback query strings, webhooks and the browser are never
# trusted on their own.
# ---------------------------------------------------------------------
def _paystack_page_url(visa_request):
    """Where the payment section lives: the Documents page (form-first,
    right after the uploads) or the payment page (standalone/older)."""
    if visa_lib.form_is_first(visa_request) and not visa_lib.form_submitted(visa_request):
        return url_for("student_visa_step", request_id=visa_request["id"], step_name="documents") + "#payment"
    return url_for("student_visa_payment", request_id=visa_request["id"])


def _paystack_attempts(db, request_id):
    return db.execute("SELECT * FROM visa_payments WHERE request_id = ? AND gateway = 'paystack' ORDER BY id DESC",
                      (request_id,)).fetchall()


def _paystack_context(db, visa_request, student):
    attempts = _paystack_attempts(db, visa_request["id"])
    verified = next((a for a in attempts if a["gateway_status"] == "successful"), None)
    account = db.execute("SELECT email FROM users WHERE id = ?", (student["user_id"],)).fetchone()
    fee = _visa_service_fee(db)
    return {
        "paystack_enabled": paystack_lib.is_configured(), "paystack_test_mode": paystack_lib.is_test_mode(),
        "paystack_latest": attempts[0] if attempts else None, "paystack_verified": verified,
        "paystack_fee": fee, "paystack_email": account["email"] if account else "",
        "paystack_phone": student["phone"] or "", "paystack_labels": paystack_lib.GATEWAY_STATUS_LABELS,
    }


def _apply_paystack_result(db, payment, data):
    """Applies a VERIFIED Paystack transaction (from paystack_lib.verify)
    to one attempt. Idempotent. Returns the attempt's gateway_status."""
    if (data.get("reference") or "") != payment["paystack_reference"]:
        return payment["gateway_status"]
    status = paystack_lib.gateway_status(data.get("status"))
    message = str(data.get("gateway_response") or data.get("message") or "")[:300] or None
    amount = data.get("amount")
    currency = str(data.get("currency") or "").upper()
    now = datetime.utcnow().isoformat(timespec="seconds")
    if status == "successful" and (currency != paystack_lib.CURRENCY
                                   or not isinstance(amount, (int, float))
                                   or int(amount) != int(payment["expected_amount_subunit"] or -1)):
        # Paid, but not exactly KSh 1,500 in KES: never unlocks; flagged for the Visa Admin.
        db.execute(
            """UPDATE visa_payments SET gateway_status='failed', payment_status='PAYMENT_REJECTED', status='failed',
                      proof_status='Rejected', paid_amount_subunit=?, paid_currency=?, gateway_message=?,
                      rejection_reason=?, gateway_verified_at=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND gateway_status != 'successful'""",
            (amount if isinstance(amount, (int, float)) else None, currency or None, message,
             f"Paystack amount/currency mismatch: received {amount} {currency}, expected "
             f"{payment['expected_amount_subunit']} {paystack_lib.CURRENCY}. Not accepted - check and refund if needed.",
             now, payment["id"]))
        db.execute("INSERT INTO visa_admin_notifications (request_id, message) VALUES (?, ?)",
                   (payment["request_id"], f"⚠️ Paystack payment {payment['paystack_reference']} had the wrong "
                                           f"amount/currency and was NOT accepted."))
        db.commit()
        return "failed"
    if status != "successful":
        if payment["gateway_status"] == "successful":
            return "successful"                     # never downgrade a verified payment
        rejected = status in ("failed", "abandoned", "cancelled")
        db.execute(
            """UPDATE visa_payments SET gateway_status=?, gateway_message=?, gateway_verified_at=?,
                      payment_status=?, status=?, rejection_reason=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND gateway_status != 'successful'""",
            (status, message, now, "PAYMENT_REJECTED" if rejected else "PAYMENT_PENDING",
             "failed" if rejected else "pending",
             f"Paystack: {paystack_lib.GATEWAY_STATUS_LABELS.get(status, status)}" if rejected else None,
             payment["id"]))
        db.commit()
        return status

    # Successful: claim the row atomically - a second callback/webhook/check
    # for the same reference finds it already 'successful' and stops here.
    claimed = db.execute(
        """UPDATE visa_payments SET gateway_status='successful', paid_amount_subunit=?, paid_currency=?,
                  gateway_channel=?, gateway_paid_at=?, gateway_verified_at=?, gateway_message=?,
                  submitted_amount=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND gateway_status != 'successful'""",
        (int(amount), currency, str(data.get("channel") or "")[:40] or None, str(data.get("paid_at") or "")[:40] or None,
         now, message, int(amount) / 100.0, payment["id"])).rowcount
    if claimed != 1:
        db.commit()
        return "successful"
    visa_request = db.execute("SELECT * FROM visa_requests WHERE id = ?", (payment["request_id"],)).fetchone()
    if visa_lib.is_unlocked(visa_request):
        # Already paid by another attempt: record it, never unlock twice.
        db.execute(
            """UPDATE visa_payments SET payment_status='PAYMENT_VERIFIED', status='paid', verified=1, verified_at=?,
                      verification_method='paystack_verify', admin_notes=COALESCE(admin_notes || ' ', '') || ?
               WHERE id=?""",
            (now, "DUPLICATE: this visa request was already paid - a refund may be due.", payment["id"]))
        db.execute("INSERT INTO visa_admin_notifications (request_id, message) VALUES (?, ?)",
                   (payment["request_id"], f"⚠️ Duplicate Paystack payment {payment['paystack_reference']} on an "
                                           f"already-paid visa request - check whether a refund is due."))
    else:
        _verify_visa_payment(db, visa_request, method="Paystack (M-PESA)" if data.get("channel") == "mobile_money"
                             else "Paystack", provider_reference=payment["paystack_reference"],
                             payment_id=payment["id"], verification_method="paystack_verify")
    db.commit()
    return "successful"


def _paystack_verify_attempt(db, payment):
    """Asks Paystack for the real status of one attempt and applies it.
    Returns the gateway_status, or None if Paystack couldn't be reached."""
    try:
        data = paystack_lib.verify(payment["paystack_reference"])
    except paystack_lib.PaystackError as exc:
        app.logger.warning("Paystack verify failed for %s: %s", payment["paystack_reference"], exc)
        return None
    return _apply_paystack_result(db, payment, data)


def _paystack_recheck_pending(db, request_id):
    """Before any new attempt: if an earlier attempt was actually paid,
    find out now (prevents charging the student twice)."""
    for p in _paystack_attempts(db, request_id):
        if p["gateway_status"] == "pending":
            _paystack_verify_attempt(db, p)


@app.route("/student-visa/payment/<int:request_id>/paystack/start", methods=["POST"])
@login_required
def student_visa_paystack_start(request_id):
    db = g.db
    student = current_student()
    visa_request = get_visa_request_or_404(db, request_id, student)
    if not visa_request:
        return render_template("errors/404.html"), 404
    back = redirect(_paystack_page_url(visa_request))
    if not paystack_lib.is_configured():
        flash("Online payment is not available right now. Please use the M-PESA payment instructions.", "warning")
        return redirect(url_for("student_visa_payment", request_id=request_id))
    if not _visa_payment_ready(db, visa_request):
        flash("Please complete the visa application form and upload your required documents before payment.",
              "warning")
        return redirect(url_for("student_visa_application", request_id=request_id))
    _paystack_recheck_pending(db, request_id)
    visa_request = db.execute("SELECT * FROM visa_requests WHERE id = ?", (request_id,)).fetchone()
    if visa_lib.is_unlocked(visa_request):
        flash("Payment already verified.", "info")
        return back

    method = request.form.get("method")
    email = (request.form.get("email") or "").strip()[:200]
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash("Please enter a valid email address for your payment receipt.", "warning")
        return back
    phone = None
    if method == "mpesa":
        phone = paystack_lib.normalize_kenyan_phone(request.form.get("phone"))
        if not phone:
            flash("Please enter a valid Kenyan M-PESA number, e.g. 0712 345 678.", "warning")
            return back
    elif method != "checkout":
        flash("Please choose a payment option.", "warning")
        return back

    fee = _visa_service_fee(db)                     # decided here, never by the browser
    amount_subunit = paystack_lib.to_subunit(fee)
    reference = paystack_lib.new_reference(request_id)
    account = db.execute("SELECT email FROM users WHERE id=?", (student["user_id"],)).fetchone()
    db.execute(
        """INSERT INTO visa_payments (request_id, student_id, amount, currency, payment_method, gateway,
               paystack_reference, transaction_reference, gateway_status, gateway_method, expected_amount,
               expected_amount_subunit, phone_number, payer_email, student_name, student_email, student_phone,
               payment_status, status, proof_status, verified, submitted_at)
           VALUES (?, ?, ?, 'KES', 'Paystack', 'paystack', ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?,
                   'PAYMENT_PENDING', 'pending', 'Pending Verification', 0, ?)""",
        (request_id, student["id"], fee, reference, reference, "mpesa_prompt" if phone else "checkout", fee,
         amount_subunit, phone, email, student["full_name"],
         account["email"] if account else None, student["phone"], datetime.utcnow().isoformat(timespec="seconds")))
    db.execute("UPDATE visa_requests SET payment_status='pending_verification', updated_at=CURRENT_TIMESTAMP "
               "WHERE id=? AND payment_verified=0", (request_id,))
    db.commit()
    payment = db.execute("SELECT * FROM visa_payments WHERE paystack_reference = ?", (reference,)).fetchone()
    metadata = {"visa_request_id": request_id, "request_number": visa_request["request_number"],
                "purpose": "visa_assistance_fee"}
    try:
        if phone:
            result = paystack_lib.charge_mpesa(email, amount_subunit, reference, phone, metadata)
            payment = _paystack_store_returned_reference(db, payment, result["reference"])
            db.execute("UPDATE visa_payments SET gateway_message=? WHERE id=?",
                       (result["display_text"] or None, payment["id"]))
            db.commit()
            app.logger.info("Paystack M-PESA charge started: request=%s ref=%s phone=%s data.status=%s",
                            request_id, payment["paystack_reference"], paystack_lib.mask_phone(phone),
                            result["status"])
            if paystack_lib.gateway_status(result["status"]) in ("successful", "failed"):
                # Never trust the charge response itself - ask Paystack (verify).
                _paystack_verify_attempt(db, payment)
                return _paystack_attempt_outcome(db, payment["id"], back)
            # "Charge attempted" + pay_offline (or any in-progress status): the
            # prompt was SENT. The attempt stays pending until Paystack's
            # webhook or a verify confirms the final result.
            flash("📱 Payment request sent. {} Check your phone and complete the M-PESA authorization for "
                  "KSh {:,.0f}. Your payment will be confirmed automatically once Paystack reports it as "
                  "successful - or click “Check payment status”.".format(
                      (result["display_text"] or "Please complete the authorization on your phone.").rstrip(".") + ".",
                      fee), "info")
            return back
        checkout_url = paystack_lib.initialize_checkout(
            email, amount_subunit, reference, url_for("paystack_callback", _external=True), metadata)
        return redirect(checkout_url)
    except paystack_lib.PaystackError as exc:
        app.logger.warning("Paystack start failed: request=%s ref=%s method=%s phone=%s %s", request_id,
                           reference, "mpesa" if phone else "checkout",
                           paystack_lib.mask_phone(phone) if phone else "-", exc.diagnostics())
        if exc.charge_attempted:
            # Paystack DID attempt a charge on this reference (e.g. HTTP 400
            # "Charge attempted" with data.status "failed"): its real state
            # comes from verify, never from the HTTP code alone.
            if _paystack_verify_attempt(db, payment) is None:
                # Verify unavailable. An HTTP 4xx means Paystack did not
                # fulfil the request: a genuine failure, whatever its body
                # says. (A later real success still arrives via the webhook,
                # which re-verifies.) Otherwise the charge's own status can
                # only keep it pending or fail it - never mark it paid.
                state = paystack_lib.gateway_status(exc.data.get("status"))
                if ((exc.http_status or 0) < 400 and state in ("pending", "successful")
                        and exc.data.get("status")):
                    db.execute("UPDATE visa_payments SET gateway_message=? WHERE id=? AND gateway_status='pending'",
                               (exc.reason or None, payment["id"]))
                    db.commit()
                else:
                    _paystack_mark_start_failed(db, payment["id"], exc.reason)
            _paystack_fill_failure_reason(db, payment["id"], exc.reason)
            return _paystack_attempt_outcome(db, payment["id"], back)
        _paystack_mark_start_failed(db, payment["id"], exc.reason)
        shown = exc.reason if exc.http_status in (400, 422) and exc.reason else ""
        flash("We couldn't start the payment with Paystack right now{}. Nothing was charged - please try again "
              "in a moment.".format(f" (Paystack: {shown})" if shown else ""), "danger")
        return back


def _paystack_store_returned_reference(db, payment, returned):
    """Paystack echoes the reference we send. If it ever returns a different
    (valid) one, that is the transaction to verify - keep it on this attempt."""
    if returned and returned != payment["paystack_reference"]:
        taken = db.execute("SELECT 1 FROM visa_payments WHERE paystack_reference=? AND id != ?",
                           (returned, payment["id"])).fetchone()
        if taken or not re.fullmatch(r"[A-Za-z0-9.=\-]{1,100}", returned):
            # Never attach one Paystack transaction to two attempts.
            app.logger.error("Paystack returned reference %r for our %s that cannot be stored (%s); keeping ours.",
                             returned[:100], payment["paystack_reference"], "already used" if taken else "invalid")
        else:
            app.logger.warning("Paystack returned reference %s for our %s; storing Paystack's.",
                               returned, payment["paystack_reference"])
            db.execute("UPDATE visa_payments SET paystack_reference=? WHERE id=? AND gateway_status='pending'",
                       (returned, payment["id"]))
            db.commit()
        return db.execute("SELECT * FROM visa_payments WHERE id=?", (payment["id"],)).fetchone()
    return payment


def _paystack_mark_start_failed(db, payment_id, reason):
    db.execute("""UPDATE visa_payments SET gateway_status='failed', payment_status='PAYMENT_REJECTED',
                  status='failed', gateway_message=COALESCE(?, gateway_message),
                  rejection_reason=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND gateway_status='pending'""",
               (reason or None, f"Paystack: {reason}" if reason else "Payment could not be started with Paystack.",
                payment_id))
    db.commit()


def _paystack_fill_failure_reason(db, payment_id, reason):
    """Keeps Paystack's charge-time reason when verify gave none."""
    if reason:
        db.execute("""UPDATE visa_payments SET gateway_message=? WHERE id=? AND gateway_status IN
                      ('failed','abandoned','cancelled') AND (gateway_message IS NULL OR gateway_message='')""",
                   (reason, payment_id))
        db.commit()


def _paystack_attempt_outcome(db, payment_id, back):
    """Tells the student the VERIFIED state of one attempt."""
    payment = db.execute("SELECT * FROM visa_payments WHERE id=?", (payment_id,)).fetchone()
    visa_request = db.execute("SELECT * FROM visa_requests WHERE id=?", (payment["request_id"],)).fetchone()
    if visa_lib.is_unlocked(visa_request):
        flash("✅ Payment verified.", "success")
        return _visa_post_unlock_redirect(visa_request)
    if payment["gateway_status"] == "pending":
        flash("📱 Payment request sent - waiting for M-PESA authorization. {} Your payment will be confirmed "
              "automatically once Paystack reports it as successful.".format(
                  (payment["gateway_message"] or "Check your phone.").rstrip(".") + "."), "info")
    else:
        flash("Paystack could not complete this M-PESA payment{}. Nothing was marked as paid and you have not "
              "been charged for this failed attempt - please check the number and try again.".format(
                  f": {payment['gateway_message']}" if payment["gateway_message"] else ""), "warning")
    return back


@app.route("/student-visa/payment/<int:request_id>/paystack/check", methods=["POST"])
@login_required
def student_visa_paystack_check(request_id):
    """'Check payment status' - asks Paystack directly (server-side)."""
    db = g.db
    student = current_student()
    visa_request = get_visa_request_or_404(db, request_id, student)
    if not visa_request:
        return render_template("errors/404.html"), 404
    if paystack_lib.is_configured():
        _paystack_recheck_pending(db, request_id)
    visa_request = db.execute("SELECT * FROM visa_requests WHERE id = ?", (request_id,)).fetchone()
    latest = (_paystack_attempts(db, request_id) or [None])[0]
    if visa_lib.is_unlocked(visa_request):
        flash("✅ Payment verified.", "success")
        return _visa_post_unlock_redirect(visa_request)
    if latest is not None and latest["gateway_status"] == "pending":
        flash("⏳ Your payment is still being processed. If you have entered your M-PESA PIN, wait a moment "
              "and check again.", "info")
    elif latest is not None:
        flash("Your payment was not completed{}. You have not been charged for an unsuccessful payment - you can "
              "try again.".format(f" (Paystack: {latest['gateway_message']})" if latest["gateway_message"] else ""),
              "warning")
    return redirect(_paystack_page_url(visa_request))


@app.route("/paystack/callback")
@login_required
def paystack_callback():
    """Paystack sends the browser back here. The query string only tells us
    WHICH attempt to check - the result always comes from Paystack."""
    db = g.db
    student = current_student()
    reference = (request.args.get("reference") or request.args.get("trxref") or "").strip()[:100]
    payment = db.execute(
        "SELECT * FROM visa_payments WHERE paystack_reference = ? AND student_id = ? AND gateway = 'paystack'",
        (reference, student["id"])).fetchone() if reference else None
    if not payment:
        flash("We could not find that payment.", "warning")
        return redirect(url_for("student_visa_dashboard"))
    outcome = _paystack_verify_attempt(db, payment) if paystack_lib.is_configured() else None
    visa_request = db.execute("SELECT * FROM visa_requests WHERE id = ?", (payment["request_id"],)).fetchone()
    if visa_lib.is_unlocked(visa_request):
        flash("✅ Payment verified. Thank you!", "success")
        return _visa_post_unlock_redirect(visa_request)
    if outcome is None or outcome == "pending":
        flash("⏳ Your payment is still being processed. Use “Check payment status” in a moment.", "info")
    else:
        flash("Your payment was not completed, so nothing was unlocked. You can try again.", "warning")
    return redirect(_paystack_page_url(visa_request))


@app.route("/paystack/webhook", methods=["POST"])
def paystack_webhook():
    """Paystack server-to-server events. Signature-checked (HMAC-SHA512 of
    the raw body with the secret key); a charge.success event is then
    RE-VERIFIED with Paystack before anything changes. Idempotent."""
    if not paystack_lib.is_configured():
        return "", 404
    raw = request.get_data(cache=False, as_text=False)
    if not paystack_lib.valid_signature(raw, request.headers.get("x-paystack-signature")):
        return "", 401
    try:
        event = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        return "", 400
    # charge.success and a failed charge are both RE-VERIFIED with Paystack;
    # only a verified success with the exact amount/currency/reference unlocks.
    if event.get("event") in ("charge.success", "charge.failed"):
        reference = str(((event.get("data") or {}).get("reference")) or "")[:100]
        db = g.db
        payment = db.execute("SELECT * FROM visa_payments WHERE paystack_reference = ? AND gateway = 'paystack'",
                             (reference,)).fetchone() if reference else None
        if payment is not None and payment["gateway_status"] != "successful":
            if _paystack_verify_attempt(db, payment) is None:
                return "", 503                      # Paystack retries later
    return "", 200


# ---------------------------------------------------------------------
# 🇺🇸 VISA ASSISTANCE APPLICATION FORM (12 sections, visa_lib).
#
# Two orders, ONE form:
#   * form_first requests (raised from the final step of the annual funding
#     application): form -> documents -> declaration -> payment.
#   * every other request (standalone /student-visa): the original
#     pay-first order - the form unlocks after verified payment.
# ---------------------------------------------------------------------
VISA_APP_DOCS_DIR = os.path.join(UPLOAD_ROOT, "visa_application_documents")
os.makedirs(VISA_APP_DOCS_DIR, exist_ok=True)
ALLOWED_SUPPORT_DOC_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}
MAX_SUPPORT_DOC_SIZE_BYTES = MAX_VISA_DOC_SIZE_BYTES          # 8 MB per file
_SUPPORT_DOC_SIGNATURES = {"pdf": (b"%PDF-",), "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",),
                           "png": (b"\x89PNG\r\n\x1a\n",)}
_SUPPORT_DOC_MIMETYPES = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
_DATE_FIELDS = {"date_of_birth", "passport_issue_date", "passport_expiry_date", "intended_arrival_date",
                "intended_departure_date", "previous_application_date", "intended_start_date"}
_CHOICE_FIELDS = {
    "gender": visa_lib.GENDERS, "marital_status": visa_lib.MARITAL_STATUSES,
    "passport_status": visa_lib.PASSPORT_STATUSES, "passport_type": visa_lib.PASSPORT_TYPES,
    "visa_category": visa_lib.VISA_TYPES, "current_status": visa_lib.CURRENT_STATUSES,
    "trip_payer": visa_lib.TRIP_PAYERS, "accommodation_type": visa_lib.ACCOMMODATION_TYPES,
    "travelled_before": visa_lib.YES_NO, "previous_application": visa_lib.YES_NO,
    "previous_visa_approved": visa_lib.YES_NO, "overstayed": visa_lib.YES_NO,
    "refused_entry": visa_lib.YES_NO, "visa_refused": visa_lib.YES_NO,
}
_MULTI_CHOICES = {"funding_sources": visa_lib.FUND_SOURCES, "assistance_required": visa_lib.ASSISTANCE_OPTIONS}
# "Yes" answers that need their follow-up field filled in.
_FOLLOW_UPS = [
    ("travelled_before", "countries_visited", "Please list the countries you have visited."),
    ("overstayed", "overstayed_explanation", "Please explain the overstay / immigration violation."),
    ("refused_entry", "refused_entry_explanation", "Please explain the refused entry."),
    ("visa_refused", "visa_refused_explanation", "Please explain the visa refusal."),
]
# Follow-up fields cleared when the answer is "No".
_CLEAR_ON_NO = {
    "travelled_before": ["countries_visited"],
    "previous_application": ["previous_application_date", "previous_visa_approved", "previous_refusal_explanation"],
    "overstayed": ["overstayed_explanation"], "refused_entry": ["refused_entry_explanation"],
    "visa_refused": ["visa_refused_explanation"],
}


def _clean_form_value(field, raw):
    value = " ".join((raw or "").split()) if field not in (
        "additional_information", "current_address", "purpose_of_travel", "organization_address",
        "accommodation_address", "previous_refusal_explanation", "overstayed_explanation",
        "refused_entry_explanation", "visa_refused_explanation", "countries_visited") else (raw or "").strip()
    value = value[:2000]
    if not value:
        return None
    if field in _DATE_FIELDS:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None
    if field in _CHOICE_FIELDS and value not in _CHOICE_FIELDS[field]:
        return None
    return value


def _complete_form_first_visa(db, visa_request):
    """Form, documents, payment and declaration are all done: start the visa
    review and submit the annual funding application (reference number,
    confirmation email, matching) as the student's own final submit would."""
    request_id = visa_request["id"]
    visa_lib.start_processing_timeline(db, request_id, processing_days=14)
    if not visa_request["annual_application_id"]:
        return
    db.execute(
        """UPDATE funding_applications
           SET visa_step_status='COMPLETE', visa_assistance_status='COMPLETE', visa_payment_status='PAID',
               visa_assistance_approved=1, visa_status='NEEDS_ASSISTANCE', last_updated=CURRENT_TIMESTAMP
           WHERE id=?""", (visa_request["annual_application_id"],))
    app_row = db.execute("SELECT * FROM funding_applications WHERE id = ?",
                         (visa_request["annual_application_id"],)).fetchone()
    if app_row and app_row["status"] == "Draft" and _review_done(app_row):
        _submit_funding_application(db, app_row["id"], visa_request["student_id"])


def _visa_form_editable(visa_request):
    """Can the student fill in / change the visa application form now?"""
    if visa_lib.form_is_first(visa_request):
        if not visa_lib.form_submitted(visa_request):
            return True              # before payment: sections 1-10; after: 11-12 too
        return visa_lib.is_unlocked(visa_request) and visa_request["application_status"] == "information_required"
    return (visa_lib.can_continue_application(visa_request)
            and visa_request["application_status"] in ("application_unlocked", "information_required"))


def _visa_form_redirect(visa_request):
    """Where to send a student who can't edit the form right now."""
    if visa_lib.form_is_first(visa_request):
        if visa_lib.form_submitted(visa_request) and not visa_lib.is_unlocked(visa_request):
            return redirect(url_for("student_visa_payment", request_id=visa_request["id"]))
        if visa_lib.is_unlocked(visa_request):
            return _visa_post_unlock_redirect(visa_request)
    if not visa_lib.can_continue_application(visa_request):
        flash("Your visa application unlocks once your payment has been verified by Africa ScholarBridge.", "warning")
        return redirect(url_for("student_visa_payment", request_id=visa_request["id"]))
    flash("This visa application is no longer editable. Check your dashboard for its status.", "info")
    return redirect(url_for("student_visa_dashboard"))


def _ensure_visa_checklist(db, request_id):
    """Adds any checklist item this request doesn't have yet (older
    requests keep their original rows too)."""
    have = {r["document_type"] for r in db.execute(
        "SELECT document_type FROM visa_documents WHERE request_id = ?", (request_id,))}
    for doc_type, required in visa_lib.VISA_DOCUMENT_CHECKLIST:
        if doc_type not in have:
            db.execute("INSERT INTO visa_documents (request_id, document_type, is_required) VALUES (?, ?, ?)",
                       (request_id, doc_type, 1 if required else 0))


def _checklist_documents(db, request_id):
    order = {t: i for i, (t, _) in enumerate(visa_lib.VISA_DOCUMENT_CHECKLIST)}
    rows = db.execute("SELECT * FROM visa_documents WHERE request_id = ?", (request_id,)).fetchall()
    return sorted((r for r in rows if r["document_type"] in order), key=lambda r: order[r["document_type"]])


@app.route("/student-visa/application/<int:request_id>")
@login_required
def student_visa_application(request_id):
    db = g.db
    student = current_student()
    visa_request = get_visa_request_or_404(db, request_id, student)
    if not visa_request:
        return render_template("errors/404.html"), 404
    # SERVER-SIDE GATE (pay-first requests stay locked until an admin has
    # VERIFIED the payment; form-first requests are editable until the
    # declaration is submitted).
    if not _visa_form_editable(visa_request):
        return _visa_form_redirect(visa_request)
    step_index = max(0, min((visa_request["current_step"] or 1) - 1, len(visa_lib.VISA_APPLICATION_STEPS) - 1))
    return redirect(url_for("student_visa_step", request_id=request_id,
                             step_name=visa_lib.VISA_APPLICATION_STEPS[step_index]))


@app.route("/student-visa/application/<int:request_id>/step/<step_name>", methods=["GET", "POST"])
@login_required
def student_visa_step(request_id, step_name):
    db = g.db
    student = current_student()
    visa_request = get_visa_request_or_404(db, request_id, student)
    if not visa_request:
        return render_template("errors/404.html"), 404
    if not _visa_form_editable(visa_request):
        return _visa_form_redirect(visa_request)
    if step_name not in visa_lib.VISA_APPLICATION_STEPS:
        return render_template("errors/404.html"), 404
    step_index = visa_lib.VISA_APPLICATION_STEPS.index(step_name)
    docs_index = visa_lib.VISA_APPLICATION_STEPS.index("documents")
    paid = visa_lib.is_unlocked(visa_request)

    # Form-first order: ... -> 10. Documents -> PAYMENT -> 11. Additional
    # Information -> 12. Declaration. Nothing after the documents is
    # reachable until the payment has been verified.
    if visa_lib.form_is_first(visa_request) and not paid and step_index > docs_index:
        flash("Please complete the Visa Assistance Payment on the Documents page first.", "warning")
        return redirect(url_for("student_visa_step", request_id=request_id, step_name="documents"))
    if (visa_lib.form_is_first(visa_request) and not paid and step_name == "documents"
            and request.method == "POST"):
        flash("Upload your required documents and complete the Visa Assistance Payment to continue.", "warning")
        return redirect(url_for("student_visa_step", request_id=request_id, step_name="documents"))

    if request.method == "POST" and step_name != "declaration":
        form = request.form
        updates = {f: _clean_form_value(f, form.get(f)) for f in visa_lib.VISA_STEP_FIELDS[step_name]}
        for f in visa_lib.MULTI_FIELDS.get(step_name, []):
            picked = [v for v in form.getlist(f) if v in _MULTI_CHOICES[f]]
            updates[f] = ", ".join(picked) or None
        for answer, follow_ups in _CLEAR_ON_NO.items():
            if answer in updates and updates[answer] != "Yes":
                for f in follow_ups:
                    updates[f] = None
        if step_name == "passport" and updates.get("passport_status") == "I do not currently have a passport":
            for f in ("passport_number", "passport_type", "passport_issue_date", "passport_expiry_date",
                      "passport_place_of_issue", "passport_issuing_country"):
                updates[f] = None
        problems = [msg for answer, follow_up, msg in _FOLLOW_UPS
                    if answer in updates and updates[answer] == "Yes" and not updates.get(follow_up)]
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            db.execute(f"UPDATE visa_requests SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                       (*updates.values(), request_id))
        if problems:
            db.commit()
            for msg in problems:
                flash(msg, "warning")
            return redirect(url_for("student_visa_step", request_id=request_id, step_name=step_name))
        next_step = min(step_index + 1, len(visa_lib.VISA_APPLICATION_STEPS) - 1)
        db.execute("UPDATE visa_requests SET current_step = MAX(COALESCE(current_step, 1), ?) WHERE id = ?",
                   (next_step + 1, request_id))
        db.commit()
        return redirect(url_for("student_visa_step", request_id=request_id,
                                 step_name=visa_lib.VISA_APPLICATION_STEPS[next_step]))

    if step_name == "documents":
        _ensure_visa_checklist(db, request_id)
        db.commit()
    visa_request = db.execute("SELECT * FROM visa_requests WHERE id = ?", (request_id,)).fetchone()
    documents = _checklist_documents(db, request_id)
    return render_template(
        "student_visa/application.html", visa_request=visa_request, step_name=step_name,
        step_index=step_index, steps=visa_lib.VISA_APPLICATION_STEPS, step_titles=visa_lib.VISA_STEP_TITLES,
        documents=documents, v=visa_lib, form_first=visa_lib.form_is_first(visa_request),
        missing_fields=visa_lib.missing_required_fields(visa_request),
        missing_documents=visa_lib.missing_required_documents(visa_request, documents),
        required_documents=visa_lib.required_document_types(visa_request),
        allowed_doc_extensions=sorted(ALLOWED_SUPPORT_DOC_EXTENSIONS),
        max_doc_mb=MAX_SUPPORT_DOC_SIZE_BYTES // (1024 * 1024),
        today=datetime.utcnow().date().isoformat(), format_price=visa_lib.format_price,
        documents_complete=visa_lib.documents_complete(visa_request, documents),
        **(_paystack_context(db, visa_request, student) if step_name == "documents" else {}),
    )


@app.route("/student-visa/application/<int:request_id>/documents/upload", methods=["POST"])
@login_required
def student_visa_document_upload(request_id):
    """Secure upload of one supporting document. Type is checked by
    extension AND file signature, size is limited, the stored name is
    random (the student's filename is never used on disk) and the file is
    only reachable through the authenticated view routes below."""
    db = g.db
    student = current_student()
    visa_request = get_visa_request_or_404(db, request_id, student)
    if not visa_request:
        return render_template("errors/404.html"), 404
    if not _visa_form_editable(visa_request):
        return _visa_form_redirect(visa_request)
    back = redirect(url_for("student_visa_step", request_id=request_id, step_name="documents"))

    doc_type = request.form.get("document_type") or ""
    if doc_type not in {t for t, _ in visa_lib.VISA_DOCUMENT_CHECKLIST}:
        flash("Please choose which document you are uploading.", "warning")
        return back
    uploaded = request.files.get("document")
    if not uploaded or not uploaded.filename:
        flash("Please choose a file to upload.", "warning")
        return back
    original = secure_filename(uploaded.filename)[:200] or "document"
    ext = _visa_doc_extension(uploaded.filename or "")
    if ext not in ALLOWED_SUPPORT_DOC_EXTENSIONS:
        flash("Only PDF, JPG, JPEG and PNG files can be uploaded.", "danger")
        return back
    data = uploaded.stream.read(MAX_SUPPORT_DOC_SIZE_BYTES + 1)
    if len(data) > MAX_SUPPORT_DOC_SIZE_BYTES:
        flash(f"That file is too large. The maximum size is {MAX_SUPPORT_DOC_SIZE_BYTES // (1024 * 1024)} MB.", "danger")
        return back
    if not data:
        flash("That file is empty.", "danger")
        return back
    if not data.startswith(_SUPPORT_DOC_SIGNATURES[ext]):
        flash("That file does not look like a real PDF, JPG or PNG file. Please upload the original document.",
              "danger")
        return back

    _ensure_visa_checklist(db, request_id)
    row = db.execute("SELECT * FROM visa_documents WHERE request_id = ? AND document_type = ? ORDER BY id LIMIT 1",
                     (request_id, doc_type)).fetchone()
    stored = f"{secrets.token_hex(16)}.{ext}"
    with open(os.path.join(VISA_APP_DOCS_DIR, stored), "wb") as fh:
        fh.write(data)
    try:
        db.execute(
            """UPDATE visa_documents SET stored_file = ?, original_name = ?, file_size = ?, status = 'Uploaded',
                      uploaded_at = CURRENT_TIMESTAMP, file_path = NULL WHERE id = ?""",
            (stored, original, len(data), row["id"]))
        db.commit()
    except Exception:
        db.rollback()
        _remove_support_doc_file(stored)
        raise
    if row["stored_file"]:
        _remove_support_doc_file(row["stored_file"])        # replaced
    flash(f"{doc_type} uploaded.", "success")
    return back


def _remove_support_doc_file(name):
    path = os.path.realpath(os.path.join(VISA_APP_DOCS_DIR, name or ""))
    if name and os.path.dirname(path) == os.path.realpath(VISA_APP_DOCS_DIR):
        try:
            os.remove(path)
        except OSError:
            pass


@app.route("/student-visa/application/<int:request_id>/documents/<int:document_id>/remove", methods=["POST"])
@login_required
def student_visa_document_remove(request_id, document_id):
    db = g.db
    student = current_student()
    visa_request = get_visa_request_or_404(db, request_id, student)
    if not visa_request:
        return render_template("errors/404.html"), 404
    if not _visa_form_editable(visa_request):
        return _visa_form_redirect(visa_request)
    row = db.execute("SELECT * FROM visa_documents WHERE id = ? AND request_id = ?",
                     (document_id, request_id)).fetchone()
    if row and row["stored_file"]:
        db.execute("""UPDATE visa_documents SET stored_file = NULL, original_name = NULL, file_size = NULL,
                      status = 'Missing', uploaded_at = NULL WHERE id = ?""", (document_id,))
        db.commit()
        _remove_support_doc_file(row["stored_file"])
        flash(f"{row['document_type']} removed.", "info")
    return redirect(url_for("student_visa_step", request_id=request_id, step_name="documents"))


def _send_support_document(row):
    if not row or not row["stored_file"]:
        abort(404)
    ext = _visa_doc_extension(row["stored_file"])
    response = send_from_directory(VISA_APP_DOCS_DIR, row["stored_file"], as_attachment=False,
                                   mimetype=_SUPPORT_DOC_MIMETYPES.get(ext, "application/octet-stream"))
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    return response


@app.route("/student-visa/documents/file/<int:document_id>")
@login_required
def student_visa_document_file(document_id):
    """The student's OWN supporting document only (ownership checked)."""
    student = current_student()
    row = g.db.execute(
        """SELECT d.* FROM visa_documents d JOIN visa_requests v ON v.id = d.request_id
           WHERE d.id = ? AND v.student_id = ?""", (document_id, student["id"])).fetchone()
    return _send_support_document(row)


@app.route("/student-visa/application/<int:request_id>/submit", methods=["GET", "POST"])
@login_required
def student_visa_submit(request_id):
    """Applicant Declaration -> submits the visa application form.
    form-first: then payment. Pay-first (already paid): processing starts."""
    db = g.db
    student = current_student()
    visa_request = get_visa_request_or_404(db, request_id, student)
    if not visa_request:
        return render_template("errors/404.html"), 404
    if not _visa_form_editable(visa_request):
        return _visa_form_redirect(visa_request)
    if request.method == "GET":
        return redirect(url_for("student_visa_step", request_id=request_id, step_name="declaration"))
    if visa_lib.form_is_first(visa_request) and not visa_lib.is_unlocked(visa_request):
        flash("Please complete the Visa Assistance Payment on the Documents page first.", "warning")
        return redirect(url_for("student_visa_step", request_id=request_id, step_name="documents"))

    declaration_step = redirect(url_for("student_visa_step", request_id=request_id, step_name="declaration"))
    missing = visa_lib.missing_required_fields(visa_request)
    if missing:
        first_step = missing[0][0]
        flash(f"Please complete the \"{visa_lib.VISA_STEP_TITLES[first_step]}\" section before submitting.", "warning")
        return redirect(url_for("student_visa_step", request_id=request_id, step_name=first_step))
    for answer, follow_up, msg in _FOLLOW_UPS:
        if visa_request[answer] == "Yes" and not visa_request[follow_up]:
            flash(msg, "warning")
            return redirect(url_for("student_visa_step", request_id=request_id,
                                     step_name="travel_history" if answer == "travelled_before" else "legal"))
    _ensure_visa_checklist(db, request_id)
    missing_docs = visa_lib.missing_required_documents(visa_request, _checklist_documents(db, request_id))
    if missing_docs:
        db.commit()
        flash("Please upload: " + ", ".join(missing_docs) + ".", "warning")
        return redirect(url_for("student_visa_step", request_id=request_id, step_name="documents"))

    typed_name = " ".join((request.form.get("declaration_name") or "").split())
    if request.form.get("declaration_confirmed") != "yes":
        flash("Please tick the declaration to confirm the information is true and accurate.", "warning")
        return declaration_step
    if not typed_name or typed_name.lower() != " ".join((visa_request["full_name"] or "").split()).lower():
        flash("Please type your full name exactly as entered in section 1 to sign the declaration.", "warning")
        return declaration_step

    today = datetime.utcnow().date().isoformat()
    now = datetime.utcnow().isoformat(timespec="seconds")
    db.execute(
        """UPDATE visa_requests SET declaration_name = ?, declaration_confirmed = 1, declaration_date = ?,
                  form_submitted_at = COALESCE(form_submitted_at, ?), updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""", (typed_name[:200], today, now, request_id))

    if visa_lib.form_is_first(visa_request) and visa_request["application_status"] != "information_required":
        # Documents, payment (verified) and declaration are all done.
        visa_lib.add_visa_history(db, request_id, "form_submitted",
                                  "Visa application form, documents, payment and declaration completed.")
        add_notification(db, student["id"],
                         f"🇺🇸 Your visa assistance application {visa_request['request_number']} has been submitted "
                         f"and is now under review.")
        _complete_form_first_visa(db, db.execute("SELECT * FROM visa_requests WHERE id = ?",
                                                 (request_id,)).fetchone())
        db.commit()
        flash("✅ Your visa assistance application has been submitted.", "success")
        app_row = (db.execute("SELECT id, status FROM funding_applications WHERE id = ?",
                              (visa_request["annual_application_id"],)).fetchone()
                   if visa_request["annual_application_id"] else None)
        if app_row and app_row["status"] != "Draft":
            return redirect(url_for("application_confirmation", application_id=app_row["id"]))
        if app_row:
            return redirect(url_for("application_step", step_name="visa"))
        return redirect(url_for("student_visa_dashboard"))

    # Payment already verified (pay-first order, or more information
    # requested by the Visa Admin): processing starts / resumes now.
    visa_lib.start_processing_timeline(db, request_id, processing_days=14)
    visa_lib.add_visa_history(db, request_id, "preparation", "Application submitted by student.")
    add_notification(db, student["id"],
                     f"🇺🇸 Your visa assistance application {visa_request['request_number']} has been submitted. "
                     f"Processing begins now (up to 2 weeks).")
    db.commit()
    flash("Your visa assistance application was submitted! Track its progress on your Visa Dashboard.", "success")
    return redirect(url_for("student_visa_dashboard"))


@app.route("/student-visa/dashboard")
@login_required
def student_visa_dashboard():
    db = g.db
    student = current_student()
    visa_request = get_active_visa_request(db, student)
    tracker = visa_lib.processing_tracker(visa_request) if visa_request else None
    notes = []
    documents = []
    if visa_request:
        notes = db.execute(
            "SELECT * FROM visa_notes WHERE request_id = ? AND visible_to_student = 1 ORDER BY id DESC",
            (visa_request["id"],),
        ).fetchall()
        documents = db.execute("SELECT * FROM visa_documents WHERE request_id = ?", (visa_request["id"],)).fetchall()
    return render_template(
        "student_visa/dashboard.html", visa_request=visa_request, tracker=tracker,
        notes=notes, documents=documents, us_gov_fee=visa_lib.US_GOV_VISA_FEE_USD,
    )


@app.route("/student-visa/documents", methods=["GET", "POST"])
@login_required
def student_visa_documents():
    db = g.db
    student = current_student()
    visa_request = get_active_visa_request(db, student)
    if not visa_request:
        flash("Start a visa assistance request first to manage documents.", "info")
        return redirect(url_for("student_visa_landing"))

    if request.method == "POST":
        if not visa_lib.is_unlocked(visa_request):
            flash("Please complete payment before uploading visa documents.", "warning")
            return redirect(url_for("student_visa_payment", request_id=visa_request["id"]))
        doc_id = request.form.get("document_id")
        db.execute(
            "UPDATE visa_documents SET status = 'Uploaded', uploaded_at = CURRENT_TIMESTAMP, file_path = ? "
            "WHERE id = ? AND request_id = ?",
            (f"uploads/demo-visa-{doc_id}.pdf", doc_id, visa_request["id"]),
        )
        db.commit()
        flash("Document marked as uploaded.", "success")
        return redirect(url_for("student_visa_documents"))

    documents = db.execute("SELECT * FROM visa_documents WHERE request_id = ?", (visa_request["id"],)).fetchall()
    return render_template("student_visa/documents.html", visa_request=visa_request, documents=documents)


@app.route("/student-visa/interview")
def student_visa_interview():
    return render_template("student_visa/interview.html")


@app.route("/student-visa/resources")
def student_visa_resources():
    return render_template("student_visa/resources.html", us_gov_fee=visa_lib.US_GOV_VISA_FEE_USD)


# =======================================================================
# ADMIN AREA
# =======================================================================
@app.route("/admin")
@admin_required
def admin_root():
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    db = g.db
    cycle = get_current_cycle()
    stats = {
        "students": db.execute("SELECT COUNT(*) c FROM students").fetchone()["c"],
        "applications": db.execute("SELECT COUNT(*) c FROM funding_applications").fetchone()["c"],
        "under_review": db.execute(
            "SELECT COUNT(*) c FROM funding_applications WHERE status IN ('Submitted','Received','Eligibility Review','Information Required','Documents Review')"
        ).fetchone()["c"],
        "matches": db.execute("SELECT COUNT(*) c FROM funding_matches").fetchone()["c"],
        "referrals": db.execute("SELECT COUNT(*) c FROM provider_referrals").fetchone()["c"],
        "funded": db.execute("SELECT COUNT(*) c FROM funding_decisions WHERE decision = 'Funded'").fetchone()["c"],
        "pending_decisions": db.execute("SELECT COUNT(*) c FROM funding_decisions WHERE decision = 'Pending'").fetchone()["c"],
        "opportunities": db.execute("SELECT COUNT(*) c FROM funding_opportunities WHERE is_open = 1").fetchone()["c"],
        # NOTE: no visa_* stats here on purpose. The Main Admin dashboard focuses
        # exclusively on the funding platform - visa numbers live only on the
        # separate Visa Admin dashboard at /visa-admin/dashboard.
    }
    upcoming = db.execute(
        """SELECT o.*, org.name AS org_name FROM funding_opportunities o
           JOIN funding_programs p ON o.program_id = p.id
           JOIN organizations org ON p.organization_id = org.id
           WHERE o.is_open = 1 ORDER BY o.close_date ASC LIMIT 5"""
    ).fetchall()
    recent_applications = db.execute(
        """SELECT a.*, s.full_name FROM funding_applications a
           JOIN students s ON a.student_id = s.id
           ORDER BY a.id DESC LIMIT 8"""
    ).fetchall()
    visa_payments_pending = db.execute(
        "SELECT COUNT(*) c FROM visa_payments WHERE payment_status = 'PAYMENT_PENDING'"
    ).fetchone()["c"]
    capacity = _capacity_snapshot_or_none(db)
    return render_template("admin/dashboard.html", stats=stats, cycle=cycle,
                           visa_payments_pending=visa_payments_pending,
                            upcoming=upcoming, recent_applications=recent_applications,
                           capacity=capacity)


def _capacity_snapshot_or_none(db):
    """The System Capacity section must never break the admin dashboard."""
    try:
        return capacity_monitor.dashboard_snapshot(db)
    except Exception:  # noqa: BLE001
        app.logger.exception("Capacity snapshot failed (dashboard still shown).")
        return None


@app.route("/admin/capacity")
@admin_required
def admin_capacity():
    return render_template("admin/capacity.html", capacity=_capacity_snapshot_or_none(g.db))


@app.route("/admin/capacity/test-alert", methods=["POST"])
@admin_required
def admin_capacity_test_alert():
    ok, err = capacity_monitor.send_test_alert(g.db)
    if ok:
        flash("Test capacity alert e-mail sent. Check the admin inbox.", "success")
    elif err == "not_configured":
        flash("Test alert NOT sent: outgoing e-mail is not configured (MAIL_* settings on Render).", "danger")
    else:
        flash("Test alert NOT sent: the e-mail server rejected or could not be reached. See Render logs.", "danger")
    return redirect(url_for("admin_capacity"))


def _main_admin_visa_summary(db, application):
    """NON-SENSITIVE visa/payment summary for the Main Admin (statuses,
    amount, payment reference and date only). Passport/ID numbers, the
    visa form, documents and M-PESA proofs/messages stay in the Visa
    Admin portal - nothing here links to them."""
    vr = None
    if application["visa_request_id"]:
        vr = db.execute("SELECT * FROM visa_requests WHERE id = ?", (application["visa_request_id"],)).fetchone()
    latest = _latest_visa_payment(db, vr["id"]) if vr else None
    if application["visa_status"] == "HAS_VISA":
        had_visa = "Yes"
    elif application["visa_status"] == "NEEDS_ASSISTANCE":
        had_visa = "No"
    else:
        had_visa = "Not answered yet"
    payment_status, amount, reference, paid_date = "Not required", None, None, None
    shown = None
    if vr is not None:
        if visa_lib.is_unlocked(vr):
            payment_status = "Paid"
        elif latest is not None:
            payment_status = PAYMENT_STATUS_LABELS.get(latest["payment_status"], latest["payment_status"])
        else:
            payment_status = "Not paid yet"
        amount = f"{vr['currency_symbol'] or 'KSh'} {(vr['service_price'] or 0):,.0f}"
        verified = db.execute(
            "SELECT * FROM visa_payments WHERE request_id = ? AND payment_status = 'PAYMENT_VERIFIED' "
            "ORDER BY id DESC LIMIT 1", (vr["id"],)).fetchone()
        shown = verified or latest
        if shown is not None:
            reference = shown["mpesa_transaction_code"] or shown["transaction_reference"]
            when = shown["verified_at"] or shown["submitted_at"] or shown["created_at"]
            try:
                paid_date = datetime.fromisoformat(str(when)[:19]).strftime("%d/%m/%Y")
            except ValueError:
                paid_date = str(when)[:10]
    visa_status = visa_lib.visa_display_status(application, vr, latest)
    shown_row = shown
    transaction_status = None
    if shown_row is not None:
        transaction_status = (paystack_lib.GATEWAY_STATUS_LABELS.get(shown_row["gateway_status"], shown_row["gateway_status"])
                              if shown_row["gateway"] == "paystack"
                              else PAYMENT_STATUS_LABELS.get(shown_row["payment_status"], shown_row["payment_status"]))
    return {
        "payment_currency": "KES" if vr is not None else None,
        "payment_method": (("Paystack" if shown_row["gateway"] == "paystack" else "M-PESA (manual)")
                           if shown_row is not None else None),
        "transaction_status": transaction_status,
        "visa_status": visa_status, "had_visa": had_visa,
        "visa_application": ("Submitted" if vr is not None and visa_lib.form_submitted(vr)
                             else "Not submitted" if vr is not None else "—"),
        "visa_review": visa_status if visa_status in ("Under Review", "Completed") else "—",
        "payment_status": payment_status, "payment_amount": amount,
        "payment_reference": reference, "payment_date": paid_date,
        "visa_reference": vr["request_number"] if vr is not None else None,
    }


@app.route("/admin/applications")
@admin_required
def admin_applications():
    db = g.db
    status = request.args.get("status")
    q = request.args.get("q")
    query = """SELECT a.*, s.full_name, s.country FROM funding_applications a
               JOIN students s ON a.student_id = s.id WHERE 1=1"""
    params = []
    if status:
        query += " AND a.status = ?"
        params.append(status)
    if q:
        query += " AND (s.full_name LIKE ? OR a.reference_number LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    query += " ORDER BY a.id DESC"
    apps = db.execute(query, params).fetchall()
    visa_summaries = {a["id"]: _main_admin_visa_summary(db, a) for a in apps}
    return render_template("admin/applications.html", applications=apps, statuses=APPLICATION_STATUSES,
                            selected_status=status, search=q or "", visa_summaries=visa_summaries)


@app.route("/admin/applications/<int:application_id>", methods=["GET", "POST"])
@admin_required
def admin_application_detail(application_id):
    db = g.db
    application = db.execute(
        """SELECT a.*, s.full_name AS student_name, s.id AS student_id_ref
           FROM funding_applications a JOIN students s ON a.student_id = s.id WHERE a.id = ?""",
        (application_id,),
    ).fetchone()
    if not application:
        return render_template("errors/404.html"), 404

    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_status":
            new_status = request.form["status"]
            note = request.form.get("note", "")
            db.execute("UPDATE funding_applications SET status = ?, last_updated = CURRENT_TIMESTAMP WHERE id = ?",
                       (new_status, application_id))
            add_history(db, application_id, new_status, note)
            add_notification(db, application["student_id"], f"📢 Your application status changed to: {new_status}")
            db.commit()
            flash("Application status updated.", "success")
        elif action == "run_matching":
            run_matching_for_application(db, application_id)
            flash("Matching engine re-run for this application.", "success")
        return redirect(url_for("admin_application_detail", application_id=application_id))

    matches_rows = db.execute(
        """SELECT m.*, o.title FROM funding_matches m JOIN funding_opportunities o ON m.opportunity_id = o.id
           WHERE m.application_id = ? ORDER BY m.score DESC""",
        (application_id,),
    ).fetchall()
    documents = db.execute("SELECT * FROM documents WHERE application_id = ?", (application_id,)).fetchall()
    history = db.execute(
        "SELECT * FROM application_history WHERE application_id = ? ORDER BY id DESC", (application_id,)
    ).fetchall()
    opportunities = db.execute("SELECT id, title FROM funding_opportunities ORDER BY title").fetchall()

    return render_template("admin/application_detail.html", application=application, matches=matches_rows,
                            documents=documents, history=history, statuses=APPLICATION_STATUSES,
                            opportunities=opportunities,
                            visa_summary=_main_admin_visa_summary(db, application))


@app.route("/admin/applications/<int:application_id>/resend-email", methods=["POST"])
@admin_required
def admin_resend_confirmation_email(application_id):
    """Main Admin-only 'Resend Confirmation Email' action (spec section 14).
    Never happens automatically just because an admin opened the
    application - only this explicit button click calls
    send_application_confirmation_email(..., force=True), which
    re-sends even though confirmation_email_sent may already be 1.
    Kept out of the Visa Admin Dashboard entirely, since this concerns
    the annual-application submission email, not a visa email."""
    db = g.db
    application = db.execute("SELECT id FROM funding_applications WHERE id = ?", (application_id,)).fetchone()
    if not application:
        return render_template("errors/404.html"), 404
    sent_ok = send_application_confirmation_email(db, application_id, force=True)
    if sent_ok:
        flash("Confirmation email resent successfully.", "success")
    else:
        flash("Could not resend the confirmation email (mail service unavailable or address invalid).", "warning")
    return redirect(url_for("admin_application_detail", application_id=application_id))


@app.route("/admin/applications/<int:application_id>/documents/<int:document_id>/verify", methods=["POST"])
@admin_required
def admin_verify_document(application_id, document_id):
    db = g.db
    db.execute("UPDATE documents SET status = 'Verified' WHERE id = ? AND application_id = ?",
               (document_id, application_id))
    db.commit()
    flash("Document marked as verified.", "success")
    return redirect(url_for("admin_application_detail", application_id=application_id))


@app.route("/admin/applications/<int:application_id>/matches/add", methods=["POST"])
@admin_required
def admin_add_match(application_id):
    db = g.db
    opportunity_id = request.form["opportunity_id"]
    opp = db.execute("SELECT * FROM funding_opportunities WHERE id = ?", (opportunity_id,)).fetchone()
    existing = db.execute(
        "SELECT id FROM funding_matches WHERE application_id = ? AND opportunity_id = ?",
        (application_id, opportunity_id),
    ).fetchone()
    if not existing and opp:
        db.execute(
            """INSERT INTO funding_matches (application_id, opportunity_id, score, match_strength, match_type, reasons)
               VALUES (?, ?, 100, 'Strong Match', 'Eligible Match', 'Manually added by admin')""",
            (application_id, opportunity_id),
        )
        db.commit()
        flash("Match added manually.", "success")
    return redirect(url_for("admin_application_detail", application_id=application_id))


@app.route("/admin/applications/<int:application_id>/matches/<int:match_id>/remove", methods=["POST"])
@admin_required
def admin_remove_match(application_id, match_id):
    db = g.db
    db.execute("DELETE FROM funding_matches WHERE id = ? AND application_id = ?", (match_id, application_id))
    db.commit()
    flash("Match removed.", "success")
    return redirect(url_for("admin_application_detail", application_id=application_id))


@app.route("/admin/students")
@admin_required
def admin_students():
    db = g.db
    q = request.args.get("q")
    query = """SELECT s.*, u.email FROM students s JOIN users u ON s.user_id = u.id WHERE 1=1"""
    params = []
    if q:
        query += " AND (s.full_name LIKE ? OR u.email LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    query += " ORDER BY s.id DESC"
    students = db.execute(query, params).fetchall()
    return render_template("admin/students.html", students=students, search=q or "")


# ---------------------------------------------------------------------
# 👥 USER ACCOUNTS -> ACCOUNT MANAGEMENT (Main Admin only)
# List / search / inspect student accounts and permanently delete one,
# with a written reason, typed confirmation, CSRF protection and an audit
# log. Logic lives in account_moderation.py. Main Admin and Visa Admin
# accounts are never listed and cannot be deleted here.
# ---------------------------------------------------------------------
def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def _csrf_ok():
    sent = request.form.get("csrf_token") or ""
    expected = session.get("_csrf_token") or ""
    return bool(expected) and hmac.compare_digest(sent, expected)


app.jinja_env.globals["csrf_token"] = csrf_token


def _current_admin_identity():
    """(admin user id, admin e-mail) of the signed-in Main Admin, or None."""
    if session.get("role") != "admin":
        return None
    row = g.db.execute("SELECT id, email FROM users WHERE id = ? AND role = 'admin'",
                       (session.get("user_id"),)).fetchone()
    return (row["id"], row["email"]) if row else None


@app.route("/admin/accounts")
@admin_required
def admin_accounts():
    q = request.args.get("q", "")
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    rows, total, page, pages = account_moderation.list_students(g.db, q, page)
    return render_template("admin/accounts.html", accounts=rows, total=total, page=page, pages=pages,
                           search=q.strip(), audit=account_moderation.recent_audit(g.db))


@app.route("/admin/accounts/<int:user_id>")
@admin_required
def admin_account_detail(user_id):
    account = account_moderation.get_student_account(g.db, user_id)
    if account is None:
        flash("That student account does not exist (it may already have been deleted).", "info")
        return redirect(url_for("admin_accounts"))
    return render_template("admin/account_detail.html", account=account,
                           reason_min=account_moderation.REASON_MIN, reason_max=account_moderation.REASON_MAX,
                           open_delete=request.args.get("delete") == "1")


@app.route("/admin/accounts/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_account_delete(user_id):
    if not _csrf_ok():
        flash("The request could not be verified (it may have expired). Please try again.", "danger")
        return redirect(url_for("admin_account_detail", user_id=user_id)), 303
    admin = _current_admin_identity()
    if admin is None:
        abort(403)
    account = account_moderation.get_student_account(g.db, user_id)
    if account is None:
        flash("That account was already deleted.", "info")
        return redirect(url_for("admin_accounts")), 303
    reason, errors = account_moderation.validate_request(account, request.form)
    if errors:
        for e in errors:
            flash(e, "danger")
        return redirect(url_for("admin_account_detail", user_id=user_id, delete=1)), 303
    upload_dirs = {"visa_documents": VISA_DOCS_DIR, "payment_proofs": PAYMENT_PROOF_DIR,
                   "visa_application_documents": VISA_APP_DOCS_DIR}
    try:
        outcome, info = account_moderation.delete_student_account(
            g.db, user_id, admin[0], admin[1], reason, upload_dirs)
    except Exception:  # noqa: BLE001 - rolled back inside; nothing was deleted
        app.logger.exception("Account deletion failed for user %s (rolled back).", user_id)
        flash("The account could not be deleted because of a server error. Nothing was changed.", "danger")
        return redirect(url_for("admin_account_detail", user_id=user_id)), 303
    if outcome == "not_found":
        flash("That account was already deleted.", "info")
    elif outcome == "refused":
        flash(info, "danger")
    else:
        files = info["files"]
        flash("The student account and all of its data were permanently deleted "
              f"({files['removed']} stored file(s) removed).", "success")
        if files["failed"]:
            flash(f"Database deletion succeeded, but {files['failed']} stored file(s) could not be removed from "
                  "storage. This has been recorded in the audit log - please ask whoever manages the server "
                  "to remove them.", "warning")
    return redirect(url_for("admin_accounts")), 303


@app.route("/admin/cycles", methods=["GET", "POST"])
@admin_required
def admin_cycles():
    db = g.db
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            db.execute(
                """INSERT INTO funding_cycles (name, year, open_date, close_date, review_start,
                   matching_start, notification_date, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'Draft')""",
                (request.form["name"], request.form["year"], request.form.get("open_date"),
                 request.form.get("close_date"), request.form.get("review_start"),
                 request.form.get("matching_start"), request.form.get("notification_date")),
            )
            db.commit()
            flash("New funding cycle created.", "success")
        elif action == "set_current":
            cycle_id = request.form["cycle_id"]
            db.execute("UPDATE funding_cycles SET is_current = 0")
            db.execute("UPDATE funding_cycles SET is_current = 1, status = 'Open' WHERE id = ?", (cycle_id,))
            db.commit()
            flash("Current funding cycle updated.", "success")
        elif action == "close":
            cycle_id = request.form["cycle_id"]
            db.execute("UPDATE funding_cycles SET status = 'Closed' WHERE id = ?", (cycle_id,))
            db.commit()
            flash("Funding cycle closed.", "success")
        return redirect(url_for("admin_cycles"))

    cycles = db.execute("SELECT * FROM funding_cycles ORDER BY year DESC").fetchall()
    cycle_stats = {}
    for c in cycles:
        cycle_stats[c["id"]] = db.execute(
            "SELECT COUNT(*) c FROM funding_applications WHERE cycle_id = ?", (c["id"],)
        ).fetchone()["c"]
    return render_template("admin/cycles.html", cycles=cycles, cycle_stats=cycle_stats)


@app.route("/admin/organizations", methods=["GET", "POST"])
@admin_required
def admin_organizations():
    db = g.db
    if request.method == "POST":
        db.execute(
            """INSERT INTO organizations (name, org_type, description, region, website_url,
               logo_initial, verification_status, is_demo)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            (request.form["name"], request.form["org_type"], request.form.get("description"),
             request.form.get("region"), request.form.get("website_url"),
             request.form["name"][0].upper(), request.form.get("verification_status", "Pending")),
        )
        db.commit()
        flash("Organization added.", "success")
        return redirect(url_for("admin_organizations"))

    organizations = db.execute("SELECT * FROM organizations ORDER BY name").fetchall()
    return render_template("admin/organizations.html", organizations=organizations)


@app.route("/admin/organizations/<int:org_id>/programs", methods=["POST"])
@admin_required
def admin_add_program(org_id):
    db = g.db
    db.execute(
        "INSERT INTO funding_programs (organization_id, name, description) VALUES (?, ?, ?)",
        (org_id, request.form["name"], request.form.get("description")),
    )
    db.commit()
    flash("Program added.", "success")
    return redirect(url_for("admin_organizations"))


@app.route("/admin/opportunities", methods=["GET", "POST"])
@admin_required
def admin_opportunities():
    db = g.db
    if request.method == "POST":
        db.execute(
            """INSERT INTO funding_opportunities
               (program_id, title, funding_type, description, amount, coverage, eligible_countries,
                education_levels, fields, study_destination, eligibility_notes, requirements,
                required_documents, open_date, close_date, application_method, application_url,
                fully_funded, verification_status, last_verified_date, is_open, is_demo,
                bank_details_required, payment_method, payment_currency,
                mobile_money_supported, international_transfer_supported)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?)""",
            (
                request.form["program_id"], request.form["title"], request.form["funding_type"],
                request.form.get("description"), request.form.get("amount"), request.form.get("coverage"),
                request.form.get("eligible_countries"), request.form.get("education_levels"),
                request.form.get("fields"), request.form.get("study_destination"),
                request.form.get("eligibility_notes"), request.form.get("requirements"),
                request.form.get("required_documents"), request.form.get("open_date"),
                request.form.get("close_date"), request.form.get("application_method"),
                request.form.get("application_url"), 1 if request.form.get("fully_funded") else 0,
                request.form.get("verification_status", "Pending"), date.today().isoformat(),
                request.form.get("bank_details_required", "FALSE"), request.form.get("payment_method"),
                request.form.get("payment_currency"), 1 if request.form.get("mobile_money_supported") else 0,
                1 if request.form.get("international_transfer_supported") else 0,
            ),
        )
        db.commit()
        flash("Funding opportunity added.", "success")
        return redirect(url_for("admin_opportunities"))

    opportunities = db.execute(
        """SELECT o.*, org.name AS org_name FROM funding_opportunities o
           JOIN funding_programs p ON o.program_id = p.id
           JOIN organizations org ON p.organization_id = org.id ORDER BY o.id DESC"""
    ).fetchall()
    programs = db.execute(
        """SELECT p.*, org.name AS org_name FROM funding_programs p
           JOIN organizations org ON p.organization_id = org.id ORDER BY org.name"""
    ).fetchall()
    return render_template("admin/opportunities.html", opportunities=opportunities, programs=programs)


@app.route("/admin/opportunities/<int:opp_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_opportunity(opp_id):
    db = g.db
    opp = db.execute("SELECT * FROM funding_opportunities WHERE id = ?", (opp_id,)).fetchone()
    db.execute("UPDATE funding_opportunities SET is_open = ? WHERE id = ?", (0 if opp["is_open"] else 1, opp_id))
    db.commit()
    return redirect(url_for("admin_opportunities"))


@app.route("/admin/opportunities/<int:opp_id>/verify", methods=["POST"])
@admin_required
def admin_verify_opportunity(opp_id):
    db = g.db
    db.execute(
        "UPDATE funding_opportunities SET verification_status = 'Verified', last_verified_date = ? WHERE id = ?",
        (date.today().isoformat(), opp_id),
    )
    db.commit()
    flash("Opportunity marked as verified.", "success")
    return redirect(url_for("admin_opportunities"))


@app.route("/admin/providers")
@admin_required
def admin_providers():
    db = g.db
    referrals = db.execute(
        """SELECT r.*, o.title, s.full_name FROM provider_referrals r
           JOIN funding_matches m ON r.match_id = m.id
           JOIN funding_opportunities o ON m.opportunity_id = o.id
           JOIN funding_applications a ON m.application_id = a.id
           JOIN students s ON a.student_id = s.id
           ORDER BY r.id DESC"""
    ).fetchall()
    return render_template("admin/providers.html", referrals=referrals)


@app.route("/admin/providers/<int:referral_id>/status", methods=["POST"])
@admin_required
def admin_update_referral_status(referral_id):
    db = g.db
    db.execute("UPDATE provider_referrals SET status = ? WHERE id = ?", (request.form["status"], referral_id))
    db.commit()
    flash("Referral status updated.", "success")
    return redirect(url_for("admin_providers"))


@app.route("/admin/matches")
@admin_required
def admin_matches():
    db = g.db
    matches_rows = db.execute(
        """SELECT m.*, o.title, s.full_name, a.reference_number FROM funding_matches m
           JOIN funding_opportunities o ON m.opportunity_id = o.id
           JOIN funding_applications a ON m.application_id = a.id
           JOIN students s ON a.student_id = s.id
           ORDER BY m.id DESC"""
    ).fetchall()
    return render_template("admin/matches.html", matches=matches_rows)


@app.route("/admin/decisions", methods=["GET", "POST"])
@admin_required
def admin_decisions():
    db = g.db
    if request.method == "POST":
        referral_id = request.form["referral_id"]
        existing = db.execute("SELECT id FROM funding_decisions WHERE referral_id = ?", (referral_id,)).fetchone()
        decision = request.form["decision"]

        if existing:
            db.execute(
                """UPDATE funding_decisions SET decision = ?, funding_amount = ?, coverage = ?,
                   decision_date = ?, provider_reference = ?, notes = ? WHERE referral_id = ?""",
                (decision, request.form.get("funding_amount"), request.form.get("coverage"),
                 date.today().isoformat(), request.form.get("provider_reference"),
                 request.form.get("notes"), referral_id),
            )
        else:
            db.execute(
                """INSERT INTO funding_decisions
                   (referral_id, decision, funding_amount, coverage, decision_date, provider_reference, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (referral_id, decision, request.form.get("funding_amount"), request.form.get("coverage"),
                 date.today().isoformat(), request.form.get("provider_reference"), request.form.get("notes")),
            )

        # Reflect the decision back onto the student's central application.
        referral = db.execute(
            """SELECT m.application_id, m.opportunity_id, s.id AS student_id FROM provider_referrals r
               JOIN funding_matches m ON r.match_id = m.id
               JOIN funding_applications a ON m.application_id = a.id
               JOIN students s ON a.student_id = s.id
               WHERE r.id = ?""",
            (referral_id,),
        ).fetchone()
        if referral:
            status_map = {"Funded": "Funded", "Partially Funded": "Funded",
                          "Waitlisted": "Decision Pending", "Unsuccessful": "Unsuccessful", "Pending": "Decision Pending"}
            new_status = status_map.get(decision, "Decision Pending")
            db.execute("UPDATE funding_applications SET status = ? WHERE id = ?",
                       (new_status, referral["application_id"]))
            add_history(db, referral["application_id"], new_status, f"Provider decision recorded: {decision}")
            add_notification(db, referral["student_id"], f"📢 A funding decision has been recorded: {decision}")

            # 🏦 A funded decision may need bank/payout tracking. We never
            # fabricate a PAID status here - only NOT_REQUIRED, NOT_SUBMITTED,
            # or SUBMITTED, depending on what the provider actually needs
            # and whether the student has already given us their details.
            # Any further progress (VERIFICATION_REQUIRED -> ... -> PAID) is
            # a deliberate, later action by an authorized admin only - see
            # /admin/disbursements.
            if decision in ("Funded", "Partially Funded"):
                opp = db.execute("SELECT * FROM funding_opportunities WHERE id = ?", (referral["opportunity_id"],)).fetchone()
                existing_disbursement = db.execute(
                    "SELECT id FROM funding_disbursements WHERE referral_id = ?", (referral_id,)
                ).fetchone()
                if not existing_disbursement:
                    bank_details = db.execute(
                        "SELECT id FROM student_bank_details WHERE application_id = ? AND confirmed = 1",
                        (referral["application_id"],),
                    ).fetchone()
                    if opp and opp["bank_details_required"] == "FALSE":
                        initial_status = "NOT_REQUIRED"
                    elif bank_details:
                        initial_status = "SUBMITTED"
                    else:
                        initial_status = "NOT_SUBMITTED"
                    db.execute(
                        """INSERT INTO funding_disbursements
                           (student_id, application_id, opportunity_id, referral_id, amount, currency,
                            payment_method, payment_status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (referral["student_id"], referral["application_id"], referral["opportunity_id"],
                         referral_id, request.form.get("funding_amount"), opp["payment_currency"] if opp else None,
                         opp["payment_method"] if opp else None, initial_status),
                    )

        db.commit()
        flash("Funding decision recorded.", "success")
        return redirect(url_for("admin_decisions"))

    referrals = db.execute(
        """SELECT r.id AS referral_id, r.status AS referral_status, o.title, s.full_name,
                  d.decision, d.funding_amount, d.coverage, d.notes
           FROM provider_referrals r
           JOIN funding_matches m ON r.match_id = m.id
           JOIN funding_opportunities o ON m.opportunity_id = o.id
           JOIN funding_applications a ON m.application_id = a.id
           JOIN students s ON a.student_id = s.id
           LEFT JOIN funding_decisions d ON d.referral_id = r.id
           ORDER BY r.id DESC"""
    ).fetchall()
    return render_template("admin/decisions.html", referrals=referrals)


# ---------------------------------------------------------------------
# 🏦 /admin/banks - the Main Admin's bank directory manager. Ordinary
# students can only browse/select from this directory (via /api/banks) -
# only an authenticated Main Admin can add, edit, remove, verify, or
# (de)activate a bank. The Visa Admin has no access to this page at all
# (it isn't under /visa-admin/*, and @admin_required rejects a Visa Admin
# session the same way it rejects a student session).
# ---------------------------------------------------------------------
@app.route("/admin/banks", methods=["GET", "POST"])
@admin_required
def admin_banks():
    db = g.db
    if request.method == "POST":
        action = request.form.get("action", "add")

        if action == "add":
            country = request.form.get("country", "").strip()
            bank_name = request.form.get("bank_name", "").strip()
            if not country or country not in banks_lib.country_names() or not bank_name:
                flash("Please provide a valid country and bank name.", "danger")
                return redirect(url_for("admin_banks"))
            db.execute(
                """INSERT INTO banks (bank_name, country, bank_code, swift_bic, website, status, last_verified)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (bank_name, country, request.form.get("bank_code", "").strip() or None,
                 request.form.get("swift_bic", "").strip() or None, request.form.get("website", "").strip() or None,
                 request.form.get("status", "Licensed"), date.today().isoformat()),
            )
            db.commit()
            flash(f"{bank_name} added to the {country} bank directory.", "success")

        elif action == "edit":
            bank_id = request.form.get("bank_id")
            db.execute(
                """UPDATE banks SET bank_name = ?, bank_code = ?, swift_bic = ?, website = ?, status = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (request.form.get("bank_name", "").strip(), request.form.get("bank_code", "").strip() or None,
                 request.form.get("swift_bic", "").strip() or None, request.form.get("website", "").strip() or None,
                 request.form.get("status", "Licensed"), bank_id),
            )
            db.commit()
            flash("Bank details updated.", "success")

        elif action == "verify":
            bank_id = request.form.get("bank_id")
            db.execute(
                "UPDATE banks SET status = 'Licensed', last_verified = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (date.today().isoformat(), bank_id),
            )
            db.commit()
            flash("Bank marked as verified today.", "success")

        elif action == "toggle_active":
            bank_id = request.form.get("bank_id")
            bank = db.execute("SELECT is_active FROM banks WHERE id = ?", (bank_id,)).fetchone()
            if bank:
                db.execute("UPDATE banks SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                           (0 if bank["is_active"] else 1, bank_id))
                db.commit()

        elif action == "remove":
            bank_id = request.form.get("bank_id")
            db.execute("DELETE FROM banks WHERE id = ?", (bank_id,))
            db.commit()
            flash("Bank removed from the directory.", "success")

        return redirect(url_for("admin_banks"))

    country_filter = request.args.get("country", "").strip()
    q = request.args.get("q", "").strip()
    query = "SELECT * FROM banks WHERE 1=1"
    params = []
    if country_filter:
        query += " AND country = ?"
        params.append(country_filter)
    if q:
        query += " AND (bank_name LIKE ? OR bank_code LIKE ? OR swift_bic LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    query += " ORDER BY country, bank_name"
    banks = db.execute(query, params).fetchall()

    return render_template(
        "admin/banks.html", banks=banks, countries=banks_lib.country_names(),
        country_filter=country_filter, q=q, status_choices=banks_lib.BANK_STATUS_CHOICES,
    )


# ---------------------------------------------------------------------
# 💰 /admin/disbursements - where an authorized Main Admin tracks and
# updates the actual payout status for an awarded student. A payment is
# only ever marked PAID here, by a human admin action - never generated
# automatically anywhere else in the system.
# ---------------------------------------------------------------------
@app.route("/admin/disbursements", methods=["GET", "POST"])
@admin_required
def admin_disbursements():
    db = g.db
    admin = current_admin()

    if request.method == "POST":
        disbursement_id = request.form.get("disbursement_id")
        db.execute(
            """UPDATE funding_disbursements
               SET payment_status = ?, transaction_reference = ?, payment_date = ?, notes = ?,
                   admin_id = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (request.form.get("payment_status"), request.form.get("transaction_reference", "").strip() or None,
             date.today().isoformat() if request.form.get("payment_status") == "PAID" else request.form.get("payment_date") or None,
             request.form.get("notes", "").strip() or None, admin["id"] if admin else None, disbursement_id),
        )
        db.commit()
        flash("Disbursement status updated.", "success")
        return redirect(url_for("admin_disbursements"))

    disbursements = db.execute(
        """SELECT fd.*, s.full_name AS student_name, o.title AS opportunity_title,
                  a.reference_number, bd.confirmed AS bank_details_confirmed
           FROM funding_disbursements fd
           JOIN students s ON fd.student_id = s.id
           JOIN funding_applications a ON fd.application_id = a.id
           LEFT JOIN funding_opportunities o ON fd.opportunity_id = o.id
           LEFT JOIN student_bank_details bd ON bd.application_id = fd.application_id
           ORDER BY fd.id DESC"""
    ).fetchall()
    return render_template("admin/disbursements.html", disbursements=disbursements)


@app.route("/admin/logout")
def admin_logout():
    # Targeted pop, not session.clear(): only ends the MAIN ADMIN session.
    # A Visa Admin session sharing this browser is left untouched.
    session.pop("user_id", None)
    session.pop("role", None)
    # Also end the Visa Admin session if it was opened by this same login.
    if session.pop("visa_admin_via_admin_login", None):
        session.pop("visa_admin_user_id", None)
    flash("Admin logged out.", "success")
    return redirect(url_for("admin_login"))


# =======================================================================
# VISA ADMIN PORTAL — completely separate from the Main Admin portal
# above. Different routes (/visa-admin/*), a different login and session
# key (session['visa_admin_user_id'], never session['role']/['user_id']),
# a different nav (templates/visa_admin/_nav.html) and different
# permissions (@visa_admin_required, never @admin_required). This portal
# manages ONLY the U.S. Student Visa Assistance service - it has no
# controls for funding opportunities, matching, cycles, decisions,
# scholarship approval, or provider management (those stay exclusively
# in the Main Admin portal above).
# =======================================================================
@app.route("/visa-admin")
def visa_admin_root():
    return redirect(url_for("visa_admin_dashboard" if "visa_admin_user_id" in session else "visa_admin_login"))


@app.route("/visa-admin/login", methods=["GET", "POST"])
def visa_admin_login():
    if request.method == "POST":
        db = g.db
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = db.execute("SELECT * FROM users WHERE email = ? AND role = 'visa_admin'", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            # Sets ONLY this dedicated key - never session['role'] or
            # session['user_id'], so this can never be mistaken for a
            # Main Admin (or student) login by admin_required()/login_required().
            session["visa_admin_user_id"] = user["id"]
            flash("Welcome back, Visa Admin.", "success")
            next_path = request.args.get("next", "")
            if next_path.startswith("/visa-admin/") and "//" not in next_path:
                return redirect(next_path)
            return redirect(url_for("visa_admin_dashboard"))
        flash("Invalid Visa Admin credentials.", "danger")
        return redirect(url_for("visa_admin_login"))
    return render_template("visa_admin/login.html")


@app.route("/visa-admin/logout")
def visa_admin_logout():
    # Targeted pop: only ends the VISA ADMIN session. A Main Admin or
    # student session sharing this browser is left completely untouched.
    session.pop("visa_admin_user_id", None)
    session.pop("visa_admin_via_admin_login", None)
    flash("Visa Admin logged out.", "success")
    return redirect(url_for("visa_admin_login"))


@app.route("/visa-admin/dashboard")
@visa_admin_required
def visa_admin_dashboard():
    db = g.db
    stats = {
        "requests": db.execute("SELECT COUNT(*) c FROM visa_requests").fetchone()["c"],
        "paid": db.execute("SELECT COUNT(*) c FROM visa_requests WHERE payment_status = 'paid'").fetchone()["c"],
        "unlocked": db.execute(
            "SELECT COUNT(*) c FROM visa_requests WHERE payment_status = 'paid' AND payment_verified = 1"
        ).fetchone()["c"],
        "submissions": db.execute(
            "SELECT COUNT(*) c FROM visa_requests WHERE application_submitted_at IS NOT NULL"
        ).fetchone()["c"],
        "submissions_complete": db.execute(
            "SELECT COUNT(*) c FROM visa_requests WHERE submission_status = 'complete'"
        ).fetchone()["c"],
        "processing": db.execute(
            "SELECT COUNT(*) c FROM visa_requests WHERE application_status IN "
            "('preparation', 'application_review', 'document_review', 'fee_coverage_processing', "
            "'interview_preparation', 'final_guidance')"
        ).fetchone()["c"],
        "completed": db.execute("SELECT COUNT(*) c FROM visa_requests WHERE application_status = 'completed'").fetchone()["c"],
        "fee_coverage_total": db.execute(
            "SELECT COALESCE(SUM(amount), 0) t FROM visa_fee_transactions WHERE status = 'PAID_BY_AFRICA_SCHOLARBRIDGE'"
        ).fetchone()["t"],
    }
    recent_requests = db.execute(
        """SELECT v.*, s.full_name AS student_name FROM visa_requests v
           JOIN students s ON v.student_id = s.id ORDER BY v.id DESC LIMIT 8"""
    ).fetchall()
    information_required = db.execute(
        "SELECT COUNT(*) c FROM visa_requests WHERE application_status = 'information_required'"
    ).fetchone()["c"]
    pending_payments = db.execute(
        "SELECT COUNT(*) c FROM visa_payments WHERE payment_status = 'PAYMENT_PENDING'"
    ).fetchone()["c"]
    return render_template("visa_admin/dashboard.html", stats=stats, recent_requests=recent_requests,
                            information_required=information_required, pending_payments=pending_payments, us_gov_fee=visa_lib.US_GOV_VISA_FEE_USD,
                            student_fee=visa_lib.STUDENT_SERVICE_FEE_USD, format_usd=visa_lib.format_usd)


@app.route("/visa-admin/requests")
@visa_admin_required
def visa_admin_requests():
    db = g.db
    status_filter = request.args.get("status", "")
    country_filter = request.args.get("country", "")

    query = """SELECT v.*, s.full_name AS student_name, s.id AS student_row_id,
                      fa.reference_number AS annual_application_reference
               FROM visa_requests v
               JOIN students s ON v.student_id = s.id
               LEFT JOIN funding_applications fa ON fa.student_id = s.id
               WHERE 1=1"""
    params = []
    if status_filter:
        query += " AND v.application_status = ?"
        params.append(status_filter)
    if country_filter:
        query += " AND v.country = ?"
        params.append(country_filter)
    query += " GROUP BY v.id ORDER BY v.id DESC"
    requests_rows = db.execute(query, params).fetchall()

    by_country = db.execute(
        "SELECT country, COUNT(*) c FROM visa_requests GROUP BY country ORDER BY c DESC"
    ).fetchall()

    return render_template(
        "visa_admin/requests.html", requests=requests_rows, by_country=by_country,
        status_filter=status_filter, country_filter=country_filter,
    )


@app.route("/visa-admin/visa-application-documents/<int:document_id>")
@visa_admin_required
def visa_admin_support_document(document_id):
    """A visa assistance supporting document. Visa Admin only - the Main
    Admin has no route to these files."""
    row = g.db.execute("SELECT * FROM visa_documents WHERE id = ?", (document_id,)).fetchone()
    return _send_support_document(row)


@app.route("/visa-admin/requests/<int:request_id>", methods=["GET", "POST"])
@visa_admin_required
def visa_admin_request_detail(request_id):
    db = g.db
    visa_admin = current_visa_admin()
    visa_request = db.execute(
        """SELECT v.*, s.full_name AS student_name FROM visa_requests v
           JOIN students s ON v.student_id = s.id WHERE v.id = ?""",
        (request_id,),
    ).fetchone()
    if not visa_request:
        return render_template("errors/404.html"), 404

    if request.method == "POST":
        action = request.form.get("action")

        if action == "confirm_payment":
            # Legacy shortcut (no form uses it any more). Payments are
            # verified per submission from Visa Payments, so there is one
            # audited path with the student's SMS and the admin's checks.
            flash("Verify payments from Visa Payments → Review, where the submitted M-PESA details are shown.", "info")

        elif action == "update_status":
            new_status = request.form.get("application_status")
            db.execute(
                "UPDATE visa_requests SET application_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_status, request_id),
            )
            visa_lib.add_visa_history(db, request_id, new_status, "Status updated by Visa Admin.")
            if new_status == "completed":
                db.execute("UPDATE visa_requests SET completed_at = CURRENT_TIMESTAMP WHERE id = ?", (request_id,))
                add_notification(db, visa_request["student_id"],
                                  "✅ Africa ScholarBridge Assistance Completed for your visa request.")
            add_notification(db, visa_request["student_id"], f"🇺🇸 Your visa request status is now: {new_status.replace('_', ' ').title()}")
            flash("Status updated.", "success")

        elif action == "request_information":
            note_text = request.form.get("note", "").strip()
            db.execute(
                "UPDATE visa_requests SET application_status = 'information_required', required_info_note = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (note_text, request_id),
            )
            visa_lib.add_visa_history(db, request_id, "information_required", note_text)
            visa_lib.add_visa_note(db, request_id, note_text, admin_id=visa_admin["id"] if visa_admin else None)
            add_notification(db, visa_request["student_id"], "⏸️ Processing Paused — Information Required for your visa request.")
            flash("Information request sent to the student.", "success")

        elif action == "add_note":
            note_text = request.form.get("note", "").strip()
            if note_text:
                visible = request.form.get("visible_to_student") == "on"
                visa_lib.add_visa_note(db, request_id, note_text, admin_id=visa_admin["id"] if visa_admin else None,
                                        visible_to_student=visible)
                flash("Note added.", "success")

        elif action == "update_fee_coverage_status":
            new_coverage_status = request.form.get("visa_fee_coverage_status")
            note_text = request.form.get("note", "").strip()
            visa_lib.update_fee_coverage_status(db, request_id, new_coverage_status,
                                                 admin_id=visa_admin["id"] if visa_admin else None, note=note_text)
            add_notification(db, visa_request["student_id"],
                              f"🇺🇸 Your U.S. visa fee coverage status is now: "
                              f"{visa_lib.VISA_FEE_COVERAGE_LABELS.get(new_coverage_status, new_coverage_status)}")
            flash("Visa fee coverage status updated.", "success")

        elif action == "record_fee_coverage_payment":
            visa_lib.record_fee_coverage_payment(
                db, request_id, admin_id=visa_admin["id"] if visa_admin else None,
                payment_reference=request.form.get("payment_reference", "").strip(),
                official_reference=request.form.get("official_reference", "").strip(),
                payment_date=request.form.get("payment_date") or date.today().isoformat(),
                notes=request.form.get("notes", "").strip(),
                receipt_path=(f"receipts/{request_id}-{request.form.get('receipt_filename','').strip()}"
                              if request.form.get("receipt_filename", "").strip() else None),
            )
            add_notification(db, visa_request["student_id"],
                              f"✅ Africa ScholarBridge has paid your US${visa_lib.US_GOV_VISA_FEE_USD} "
                              f"U.S. visa application fee.")
            flash(f"Recorded Africa ScholarBridge's US${visa_lib.US_GOV_VISA_FEE_USD} payment toward this student's visa fee.", "success")

        elif action == "update_refund_status":
            new_refund_status = request.form.get("refund_status")
            refund_reason = request.form.get("refund_reason", "").strip()
            refund_notes = request.form.get("refund_notes", "").strip()
            db.execute(
                """UPDATE visa_requests SET refund_status = ?, refund_reason = ?, refund_notes = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (new_refund_status, refund_reason, refund_notes, request_id),
            )
            if new_refund_status == "refunded":
                db.execute("UPDATE visa_requests SET payment_status = 'refunded' WHERE id = ?", (request_id,))
            visa_lib.add_visa_history(db, request_id, f"refund_{new_refund_status}", refund_reason)
            flash("Refund status updated.", "success")

        db.commit()
        return redirect(url_for("visa_admin_request_detail", request_id=request_id))

    payments = db.execute("SELECT * FROM visa_payments WHERE request_id = ? ORDER BY id DESC", (request_id,)).fetchall()
    fee_transactions = db.execute(
        "SELECT * FROM visa_fee_transactions WHERE request_id = ? ORDER BY id DESC", (request_id,)
    ).fetchall()
    notes = db.execute("SELECT * FROM visa_notes WHERE request_id = ? ORDER BY id DESC", (request_id,)).fetchall()
    documents = db.execute("SELECT * FROM visa_documents WHERE request_id = ?", (request_id,)).fetchall()
    history = db.execute("SELECT * FROM visa_status_history WHERE request_id = ? ORDER BY id DESC", (request_id,)).fetchall()
    tracker = visa_lib.processing_tracker(visa_request)
    refund_eligible, refund_reason_hint = visa_lib.refund_eligibility(visa_request)

    return render_template(
        "visa_admin/request_detail.html", visa_request=visa_request, payments=payments, notes=notes,
        v=visa_lib, visa_status_label=visa_lib.visa_display_status(
            db.execute("SELECT * FROM funding_applications WHERE id = ?",
                       (visa_request["annual_application_id"],)).fetchone()
            if visa_request["annual_application_id"] else None,
            visa_request, payments[0] if payments else None),
        documents=documents, history=history, tracker=tracker, fee_transactions=fee_transactions,
        refund_eligible=refund_eligible, refund_reason_hint=refund_reason_hint,
        fee_coverage_statuses=visa_lib.VISA_FEE_COVERAGE_STATUSES,
        fee_coverage_labels=visa_lib.VISA_FEE_COVERAGE_LABELS,
        processing_stage_options=visa_lib.VISA_PIPELINE_STAGES,
        us_gov_fee=visa_lib.US_GOV_VISA_FEE_USD,
    )


@app.route("/visa-admin/payment-proof/<int:payment_id>/file")
@visa_admin_required
def visa_admin_payment_proof_file(payment_id):
    """Payment screenshots are NOT under static/ - this Visa-Admin-only
    route is the only way to view one."""
    db = g.db
    payment = db.execute("SELECT proof_file FROM visa_payments WHERE id=?", (payment_id,)).fetchone()
    if not payment or not payment["proof_file"]:
        return render_template("errors/404.html"), 404
    # proof_file is always our own random hex name; reject anything else.
    if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|jpeg|png|webp)", payment["proof_file"]):
        abort(404)
    if not os.path.isfile(os.path.join(PAYMENT_PROOF_DIR, payment["proof_file"])):
        return render_template("errors/404.html"), 404
    response = send_from_directory(PAYMENT_PROOF_DIR, payment["proof_file"], as_attachment=False)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'"
    return response


@app.route("/visa-admin/payments")
@visa_admin_required
def visa_admin_payments():
    db = g.db
    status_filter = request.args.get("status", "")
    query = """SELECT p.*, v.request_number, v.annual_application_id,
                      COALESCE(p.student_name, s.full_name) AS student_name_display,
                      COALESCE(p.student_email, u.email) AS student_email_display,
                      COALESCE(p.student_phone, s.phone) AS student_phone_display
               FROM visa_payments p
               JOIN visa_requests v ON p.request_id = v.id
               JOIN students s ON p.student_id = s.id
               LEFT JOIN users u ON s.user_id = u.id
               WHERE 1=1"""
    params = []
    if status_filter in PAYMENT_STATUS_LABELS:
        query += " AND p.payment_status = ?"
        params.append(status_filter)
    query += " ORDER BY CASE p.payment_status WHEN 'PAYMENT_PENDING' THEN 0 ELSE 1 END, p.id DESC"
    rows = db.execute(query, params).fetchall()
    payments = []
    for r in rows:
        d = dict(r)
        d["flags"] = _payment_flags(r)
        d["flag_count"] = sum(1 for f in d["flags"] if f["severity"] in ("warning", "error"))
        payments.append(d)
    counts = {row["payment_status"]: row["c"] for row in db.execute(
        "SELECT payment_status, COUNT(*) c FROM visa_payments GROUP BY payment_status").fetchall()}
    totals = {"pending": counts.get("PAYMENT_PENDING", 0), "approved": counts.get("PAYMENT_VERIFIED", 0),
              "rejected": counts.get("PAYMENT_REJECTED", 0)}
    return render_template("visa_admin/payments.html", payments=payments, totals=totals,
                           status_filter=status_filter, status_labels=PAYMENT_STATUS_LABELS)


@app.route("/visa-admin/payments/<int:payment_id>", methods=["GET", "POST"])
@visa_admin_required
def visa_admin_payment_detail(payment_id):
    """One payment submission: the student's SMS/screenshot, the admin's
    optional incoming SMS, a side-by-side comparison, and the VERIFY /
    REJECT buttons. The comparison is an aid only - it never changes the
    payment status by itself."""
    db = g.db
    visa_admin = current_visa_admin()
    payment = db.execute(
        """SELECT p.*, v.request_number, v.annual_application_id, v.payment_status AS request_payment_status,
                  COALESCE(p.student_name, s.full_name) AS student_name_display,
                  COALESCE(p.student_email, u.email) AS student_email_display,
                  COALESCE(p.student_phone, s.phone) AS student_phone_display,
                  fa.reference_number AS annual_reference, va.full_name AS verified_by_name,
                  ra.full_name AS reviewed_by_name
           FROM visa_payments p
           JOIN visa_requests v ON p.request_id = v.id
           JOIN students s ON p.student_id = s.id
           LEFT JOIN users u ON s.user_id = u.id
           LEFT JOIN funding_applications fa ON fa.id = v.annual_application_id
           LEFT JOIN visa_admins va ON va.id = p.verified_by
           LEFT JOIN visa_admins ra ON ra.id = p.reviewed_by
           WHERE p.id = ?""", (payment_id,)
    ).fetchone()
    if not payment:
        return render_template("errors/404.html"), 404

    if request.method == "POST":
        action = request.form.get("action")
        if action == "save_incoming":
            incoming_text = (request.form.get("admin_incoming_mpesa_message") or "").strip()
            if len(incoming_text) > mpesa_parser.MAX_MESSAGE_LENGTH:
                flash("That message is too long to be an M-PESA confirmation.", "warning")
            elif not incoming_text:
                db.execute(
                    """UPDATE visa_payments SET admin_incoming_mpesa_message=NULL, admin_sender_name=NULL,
                       admin_sender_phone=NULL, admin_received_amount=NULL, admin_transaction_code=NULL,
                       admin_received_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""", (payment_id,))
                db.commit()
                flash("Incoming M-PESA message cleared.", "info")
            else:
                parsed = mpesa_parser.parse_mpesa_message(incoming_text)
                received_at = " ".join(x for x in (parsed["date"], parsed["time"]) if x) or None
                db.execute(
                    """UPDATE visa_payments SET admin_incoming_mpesa_message=?, admin_sender_name=?,
                       admin_sender_phone=?, admin_received_amount=?, admin_transaction_code=?,
                       admin_received_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (incoming_text, parsed["counterparty_name"], parsed["counterparty_phone"],
                     float(parsed["amount"]) if parsed["amount"] is not None else None,
                     parsed["transaction_code"], received_at, payment_id),
                )
                db.commit()
                if parsed["direction"] != "received":
                    flash("Saved, but this doesn't look like a 'You have received ...' message from the "
                          "receiving phone. Check you pasted the right SMS.", "warning")
                else:
                    flash("Incoming M-PESA message saved and parsed. Compare the details below, then "
                          "click VERIFY PAYMENT only if you are satisfied.", "success")
        elif action == "save_notes":
            db.execute("UPDATE visa_payments SET admin_notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       ((request.form.get("admin_notes") or "").strip()[:2000] or None, payment_id))
            db.commit()
            flash("Admin notes saved.", "success")
        return redirect(url_for("visa_admin_payment_detail", payment_id=payment_id))

    # Re-read the ORIGINAL message with the current parser for display (the
    # stored copy is exactly what the student pasted). Aids only - this never
    # changes the payment status.
    student_parsed = (mpesa_parser.parse_mpesa_message(payment["submitted_mpesa_message"])
                      if payment["submitted_mpesa_message"] else None)
    reparsed = student_parsed or {}
    student_side = {
        "transaction_code": payment["mpesa_transaction_code"] or payment["transaction_reference"],
        "amount": payment["submitted_amount"],
        "date": payment["extracted_transaction_date"] or reparsed.get("date"),
        "time": payment["extracted_transaction_time"] or reparsed.get("time"),
        "name": payment["extracted_recipient_name"] or reparsed.get("counterparty_name"),
        "phone": payment["extracted_recipient_phone"] or reparsed.get("counterparty_phone"),
        "payer_phone": payment["phone_number"],
    }
    incoming_side = {
        "transaction_code": payment["admin_transaction_code"], "amount": payment["admin_received_amount"],
        "name": payment["admin_sender_name"], "phone": payment["admin_sender_phone"],
        "date": None, "time": None,
    }
    if payment["admin_incoming_mpesa_message"]:
        parsed_in = mpesa_parser.parse_mpesa_message(payment["admin_incoming_mpesa_message"])
        incoming_side.update({"date": parsed_in["date"], "time": parsed_in["time"],
                              "direction": parsed_in["direction"]})
    comparison = mpesa_parser.compare_student_and_incoming(
        student_side, incoming_side, payment["expected_amount"] or _visa_service_fee(db)
    ) if payment["admin_incoming_mpesa_message"] else []

    other_submissions = db.execute(
        "SELECT id, mpesa_transaction_code, transaction_reference, payment_status, submitted_at, created_at "
        "FROM visa_payments WHERE request_id=? AND id != ? ORDER BY id DESC",
        (payment["request_id"], payment_id),
    ).fetchall()
    return render_template(
        "visa_admin/payment_detail.html", payment=payment, flags=_payment_flags(payment),
        student_side=student_side, incoming_side=incoming_side, comparison=comparison,
        student_parsed=student_parsed, manual_review_message=mpesa_parser.MANUAL_REVIEW_MESSAGE,
        paystack_labels=paystack_lib.GATEWAY_STATUS_LABELS,
        other_submissions=other_submissions, status_labels=PAYMENT_STATUS_LABELS,
        mpesa_receiving_number=_visa_payment_recipient(db), visa_fee=_visa_service_fee(db),
    )


@app.route("/visa-admin/payments/<int:payment_id>/paystack-recheck", methods=["POST"])
@visa_admin_required
def visa_admin_paystack_recheck(payment_id):
    """Asks Paystack for the current status of one attempt (Visa Admin)."""
    db = g.db
    payment = db.execute("SELECT * FROM visa_payments WHERE id=? AND gateway='paystack'", (payment_id,)).fetchone()
    if not payment:
        return render_template("errors/404.html"), 404
    if not paystack_lib.is_configured():
        flash("Paystack is not configured on this server.", "warning")
    else:
        outcome = _paystack_verify_attempt(db, payment)
        if outcome is None:
            flash("Could not reach Paystack. Try again in a moment.", "danger")
        else:
            flash(f"Paystack says: {paystack_lib.GATEWAY_STATUS_LABELS.get(outcome, outcome)}.", "info")
    return redirect(url_for("visa_admin_payment_detail", payment_id=payment_id))


@app.route("/visa-admin/payment-proof/<int:payment_id>/review", methods=["POST"])
@visa_admin_required
def visa_admin_payment_proof_review(payment_id):
    """VERIFY PAYMENT / REJECT PAYMENT. Visa Admin only (decorator above);
    POST only. Verification is always an explicit human decision here."""
    db = g.db
    visa_admin = current_visa_admin()
    admin_id = visa_admin["id"] if visa_admin else None
    payment = db.execute("SELECT * FROM visa_payments WHERE id=?", (payment_id,)).fetchone()
    if not payment:
        return render_template("errors/404.html"), 404
    back = redirect(url_for("visa_admin_payment_detail", payment_id=payment_id))

    if payment["payment_status"] != "PAYMENT_PENDING":
        flash(f"This payment is already {PAYMENT_STATUS_LABELS.get(payment['payment_status'], payment['payment_status'])} "
              f"and can't be changed from here.", "warning")
        return back

    action = request.form.get("action")
    if payment["gateway"] == "paystack" and action in ("verify", "approve"):
        # Online payments are confirmed ONLY by Paystack (server-side verify).
        flash("Paystack payments can't be verified by hand - use “Re-check with Paystack”.", "warning")
        return back
    admin_notes = (request.form.get("admin_notes") or "").strip()[:2000] or None
    if action in ("verify", "approve"):
        if request.form.get("confirm_received") != "yes":
            flash("Tick the confirmation box to confirm you have checked that this payment was actually "
                  "received on the M-PESA account before verifying.", "warning")
            return back
        visa_request = db.execute("SELECT * FROM visa_requests WHERE id=?", (payment["request_id"],)).fetchone()
        if admin_notes:
            db.execute("UPDATE visa_payments SET admin_notes=? WHERE id=?", (admin_notes, payment_id))
        _verify_visa_payment(
            db, visa_request, method="M-Pesa direct (manually verified by Visa Admin)",
            provider_reference=payment["mpesa_transaction_code"] or payment["transaction_reference"],
            admin_id=admin_id, payment_id=payment_id, verification_method="manual_admin",
        )
        db.commit()
        if email_lib.is_configured():
            account = db.execute("SELECT email FROM users WHERE id=(SELECT user_id FROM students WHERE id=?)",
                                 (payment["student_id"],)).fetchone()
            if account:
                email_lib.send_email(account["email"], "Africa ScholarBridge — Payment Verified",
                                     "Payment verified successfully. You can now continue with your visa application.")
        flash("Payment VERIFIED. The student's visa application is now unlocked.", "success")
    elif action == "reject":
        reason = (request.form.get("rejection_reason") or "").strip()[:500]
        if not reason:
            flash("Enter a rejection reason before rejecting the payment.", "warning")
            return back
        _reject_visa_payment(db, payment, reason, admin_id=admin_id, admin_notes=admin_notes)
        db.commit()
        if email_lib.is_configured():
            account = db.execute("SELECT email FROM users WHERE id=(SELECT user_id FROM students WHERE id=?)",
                                 (payment["student_id"],)).fetchone()
            if account:
                email_lib.send_email(account["email"], "Africa ScholarBridge — Payment Not Verified",
                                     f"Your payment could not be verified. Please check your payment details "
                                     f"or contact support.\n\nReason: {reason}")
        flash("Payment REJECTED. The visa application remains locked.", "warning")
    else:
        flash("Unknown action.", "warning")
    return back


# ---------------------------------------------------------------------
# Visa Admin pages restored: the nav bar (_nav.html) and the templates
# for these pages were present, but the routes were missing from the
# uploaded app.py, which made EVERY Visa Admin page fail with a 500.
# ---------------------------------------------------------------------
@app.route("/visa-admin/submissions")
@visa_admin_required
def visa_admin_submissions():
    submissions = g.db.execute(
        """SELECT v.*, s.full_name AS student_name FROM visa_requests v
           JOIN students s ON v.student_id = s.id
           WHERE v.application_submitted_at IS NOT NULL ORDER BY v.application_submitted_at DESC"""
    ).fetchall()
    return render_template("visa_admin/submissions.html", submissions=submissions)


@app.route("/visa-admin/documents", methods=["GET", "POST"])
@visa_admin_required
def visa_admin_documents():
    db = g.db
    visa_admin = current_visa_admin()
    if request.method == "POST":
        doc_id = request.form.get("document_id", type=int)
        action = request.form.get("action")
        notes = (request.form.get("admin_notes") or "").strip()[:1000] or None
        doc = db.execute("SELECT * FROM visa_documents WHERE id=?", (doc_id,)).fetchone()
        if doc:
            if action == "mark_reviewed" and doc["status"] != "Missing":
                db.execute("UPDATE visa_documents SET status='Verified', admin_notes=?, reviewed_at=CURRENT_TIMESTAMP, "
                           "reviewed_by=? WHERE id=?", (notes, visa_admin["id"] if visa_admin else None, doc_id))
                flash("Document marked reviewed.", "success")
            elif action == "request_replacement" and doc["status"] != "Missing":
                db.execute("UPDATE visa_documents SET status='Replacement Requested', admin_notes=?, "
                           "reviewed_at=CURRENT_TIMESTAMP, reviewed_by=? WHERE id=?",
                           (notes, visa_admin["id"] if visa_admin else None, doc_id))
                req = db.execute("SELECT student_id FROM visa_requests WHERE id=?", (doc["request_id"],)).fetchone()
                if req:
                    add_notification(db, req["student_id"],
                                     f"📄 Please upload a replacement for your visa document: {doc['document_type']}.")
                flash("Replacement requested.", "success")
            elif action == "add_notes":
                db.execute("UPDATE visa_documents SET admin_notes=? WHERE id=?", (notes, doc_id))
                flash("Note saved.", "success")
            db.commit()
        return redirect(url_for("visa_admin_documents"))

    documents = db.execute(
        """SELECT d.*, v.request_number, s.full_name AS student_name FROM visa_documents d
           JOIN visa_requests v ON d.request_id = v.id JOIN students s ON v.student_id = s.id
           ORDER BY v.id DESC, d.id"""
    ).fetchall()
    has_visa_uploads = db.execute(
        """SELECT fa.id AS application_id, fa.reference_number, fa.visa_document_type, fa.visa_document_status,
                  fa.visa_document_uploaded_at, fa.visa_verification_status, s.full_name AS student_name
           FROM funding_applications fa JOIN students s ON fa.student_id = s.id
           WHERE fa.visa_status = 'HAS_VISA' ORDER BY fa.id DESC"""
    ).fetchall()
    return render_template("visa_admin/documents.html", documents=documents, has_visa_uploads=has_visa_uploads)


@app.route("/visa-admin/visa-document/<int:application_id>")
@visa_admin_required
def visa_admin_visa_document_view(application_id):
    row = g.db.execute("SELECT visa_document_path FROM funding_applications WHERE id=?", (application_id,)).fetchone()
    if not row or not row["visa_document_path"]:
        abort(404)
    return send_from_directory(VISA_DOCS_DIR, row["visa_document_path"], as_attachment=False)


@app.route("/visa-admin/fee-coverage")
@visa_admin_required
def visa_admin_fee_coverage():
    db = g.db
    rows = db.execute(
        """SELECT v.*, s.full_name AS student_name FROM visa_requests v JOIN students s ON v.student_id = s.id
           WHERE v.payment_status = 'paid' ORDER BY v.id DESC"""
    ).fetchall()
    by_status = db.execute(
        "SELECT visa_fee_coverage_status, COUNT(*) c FROM visa_requests WHERE payment_status='paid' "
        "GROUP BY visa_fee_coverage_status"
    ).fetchall()
    total_covered = db.execute(
        "SELECT COALESCE(SUM(amount), 0) t FROM visa_fee_transactions WHERE status = 'PAID_BY_AFRICA_SCHOLARBRIDGE'"
    ).fetchone()["t"]
    return render_template("visa_admin/fee_coverage.html", rows=rows, by_status=by_status,
                           total_covered=total_covered, fee_coverage_labels=visa_lib.VISA_FEE_COVERAGE_LABELS,
                           us_gov_fee=visa_lib.US_GOV_VISA_FEE_USD)


@app.route("/visa-admin/processing", methods=["GET", "POST"])
@visa_admin_required
def visa_admin_processing():
    db = g.db
    allowed = {value for value, _ in visa_lib.VISA_PIPELINE_STAGES} | {"information_required"}
    if request.method == "POST":
        request_id = request.form.get("request_id", type=int)
        new_status = request.form.get("application_status")
        req = db.execute("SELECT * FROM visa_requests WHERE id=?", (request_id,)).fetchone()
        # Processing stages only apply to requests whose payment is verified.
        if req and new_status in allowed and visa_lib.is_unlocked(req):
            db.execute("UPDATE visa_requests SET application_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (new_status, request_id))
            if new_status == "completed":
                db.execute("UPDATE visa_requests SET completed_at=CURRENT_TIMESTAMP WHERE id=?", (request_id,))
            visa_lib.add_visa_history(db, request_id, new_status, "Stage updated by Visa Admin.")
            add_notification(db, req["student_id"],
                             f"🇺🇸 Your visa request status is now: {dict(visa_lib.VISA_PIPELINE_STAGES).get(new_status, new_status.replace('_', ' ').title())}")
            db.commit()
            flash("Stage updated.", "success")
        else:
            flash("That update isn't allowed for this request.", "warning")
        return redirect(url_for("visa_admin_processing"))
    rows = db.execute(
        """SELECT v.*, s.full_name AS student_name FROM visa_requests v JOIN students s ON v.student_id = s.id
           WHERE v.payment_status = 'paid' AND v.payment_verified = 1 ORDER BY v.id DESC"""
    ).fetchall()
    trackers = {r["id"]: visa_lib.processing_tracker(r) for r in rows}
    return render_template("visa_admin/processing.html", rows=rows, trackers=trackers,
                           stage_options=visa_lib.VISA_PIPELINE_STAGES)


@app.route("/visa-admin/interview")
@visa_admin_required
def visa_admin_interview():
    return render_template("visa_admin/interview.html")


@app.route("/visa-admin/notifications")
@visa_admin_required
def visa_admin_notifications():
    db = g.db
    case_alerts = db.execute(
        """SELECT a.*, v.request_number, s.full_name AS student_name FROM visa_admin_notifications a
           LEFT JOIN visa_requests v ON a.request_id = v.id LEFT JOIN students s ON v.student_id = s.id
           ORDER BY a.id DESC LIMIT 100"""
    ).fetchall()
    notifications = db.execute(
        """SELECT n.*, s.full_name AS student_name FROM notifications n JOIN students s ON n.student_id = s.id
           WHERE n.message LIKE '%visa%' OR n.message LIKE '%Visa%' OR n.message LIKE '%payment%'
           ORDER BY n.id DESC LIMIT 100"""
    ).fetchall()
    return render_template("visa_admin/notifications.html", case_alerts=case_alerts, notifications=notifications)


@app.route("/visa-admin/reports")
@visa_admin_required
def visa_admin_reports():
    db = g.db
    paid_requests = db.execute("SELECT COUNT(*) c FROM visa_requests WHERE payment_status='paid'").fetchone()["c"]
    fees_paid_count = db.execute(
        "SELECT COUNT(*) c FROM visa_requests WHERE visa_fee_coverage_status IN ('PAID_BY_AFRICA_SCHOLARBRIDGE','CONFIRMED')"
    ).fetchone()["c"]
    covered_requests = db.execute(
        "SELECT COUNT(*) c FROM visa_requests WHERE visa_fee_coverage_status NOT IN ('PENDING','NOT_APPLICABLE')"
    ).fetchone()["c"]
    total_expenditure = db.execute(
        "SELECT COALESCE(SUM(amount), 0) t FROM visa_fee_transactions WHERE status = 'PAID_BY_AFRICA_SCHOLARBRIDGE'"
    ).fetchone()["t"]
    asb_revenue = round(paid_requests * visa_lib.STUDENT_SERVICE_FEE_USD, 2)
    return render_template(
        "visa_admin/reports.html", paid_requests=paid_requests, asb_revenue=asb_revenue,
        covered_requests=covered_requests, fees_paid_count=fees_paid_count,
        total_expenditure=total_expenditure, net_margin=round(asb_revenue - total_expenditure, 2),
        by_coverage_status=db.execute(
            "SELECT visa_fee_coverage_status, COUNT(*) c FROM visa_requests GROUP BY visa_fee_coverage_status").fetchall(),
        requests_rows=db.execute(
            """SELECT v.*, s.full_name AS student_name FROM visa_requests v JOIN students s ON v.student_id = s.id
               ORDER BY v.id DESC""").fetchall(),
        student_fee=visa_lib.STUDENT_SERVICE_FEE_USD, us_gov_fee=visa_lib.US_GOV_VISA_FEE_USD,
        format_usd=visa_lib.format_usd,
    )



@app.route("/visa-admin/settings", methods=["GET", "POST"])
@visa_admin_required
def visa_admin_settings():
    """Visa Admin settings for manual M-PESA payment and per-country pricing."""
    db = g.db
    if request.method == "POST":
        action = request.form.get("action", "pricing")
        if action == "manual_payment":
            number = request.form.get("mpesa_receiving_number", "").strip()
            fee = request.form.get("visa_application_fee", "").strip()
            digits = "".join(c for c in number if c.isdigit())
            try:
                fee_value = float(fee)
            except ValueError:
                fee_value = 0
            if len(digits) < 9:
                flash("Enter a valid Kenyan M-PESA receiving phone number.", "warning")
            elif fee_value <= 0:
                flash("Visa application fee must be greater than zero.", "warning")
            else:
                _set_visa_setting(db, "mpesa_receiving_number", number)
                _set_visa_setting(db, "visa_application_fee", fee_value)
                db.commit()
                flash("Manual M-PESA payment settings saved.", "success")
            return redirect(url_for("visa_admin_settings"))

        country = request.form.get("country", "").strip()
        currency = request.form.get("currency", "").strip().upper()
        currency_symbol = request.form.get("currency_symbol", "").strip()
        service_price = request.form.get("service_price", "0")
        exchange_rate_reference = request.form.get("exchange_rate_reference", "").strip()
        existing = db.execute("SELECT id FROM visa_pricing WHERE country = ?", (country,)).fetchone()
        if existing:
            db.execute(
                """UPDATE visa_pricing SET currency=?, currency_symbol=?, service_price=?,
                   exchange_rate_reference=?, last_updated=CURRENT_TIMESTAMP WHERE id=?""",
                (currency, currency_symbol, service_price, exchange_rate_reference, existing["id"]),
            )
        else:
            db.execute(
                """INSERT INTO visa_pricing (country, currency, currency_symbol, service_price, exchange_rate_reference)
                   VALUES (?, ?, ?, ?, ?)""",
                (country, currency, currency_symbol, service_price, exchange_rate_reference),
            )
        db.commit()
        flash(f"Pricing saved for {country}.", "success")
        return redirect(url_for("visa_admin_settings"))

    pricing_rows = db.execute("SELECT * FROM visa_pricing ORDER BY country").fetchall()
    return render_template(
        "visa_admin/settings.html",
        pricing_rows=pricing_rows,
        us_gov_fee=visa_lib.US_GOV_VISA_FEE_USD,
        mpesa_receiving_number=_visa_payment_recipient(db),
        visa_application_fee=_visa_service_fee(db),
    )


@app.route("/visa-admin/settings/pricing/<int:pricing_id>/toggle", methods=["POST"])
@visa_admin_required
def visa_admin_settings_pricing_toggle(pricing_id):
    db = g.db
    row = db.execute("SELECT active FROM visa_pricing WHERE id = ?", (pricing_id,)).fetchone()
    if row:
        db.execute("UPDATE visa_pricing SET active = ? WHERE id = ?", (0 if row["active"] else 1, pricing_id))
        db.commit()
    return redirect(url_for("visa_admin_settings"))


# =======================================================================
# /version - confirms which build is running. Never shows secrets,
# passwords, keys or environment values. The folder/database paths are
# only shown to requests from this same computer (localhost), to help
# spot "I'm running an old copy of the project".
# =======================================================================
@app.route("/version")
def version():
    lines = [APP_NAME, f"Version: {APP_VERSION}", "Status: Modified build"]
    if request.remote_addr in ("127.0.0.1", "::1"):
        cols = {r[1] for r in g.db.execute("PRAGMA table_info(visa_payments)").fetchall()}
        lines += [
            "",
            "Local diagnostics (only shown on this computer):",
            f"Project folder: {PROJECT_DIR}",
            f"Database file: {DB_PATH}",
            f"Templates folder: {app.template_folder and os.path.join(app.root_path, app.template_folder)}",
            f"M-PESA payment columns in database: {'YES' if 'submitted_mpesa_message' in cols else 'NO - old database'}",
            f"Visa Payments route registered: {'YES' if 'visa_admin_payments' in app.view_functions else 'NO'}",
        ]
    return "\n".join(lines) + "\n", 200, {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"}


# =======================================================================
# ERROR HANDLERS
# =======================================================================
# The error pages are standalone templates (they don't extend base.html and
# need no database), and each handler still has a plain-HTML fallback in
# case even that template can't be rendered - so an error page can never
# itself crash with TemplateNotFound.
_FALLBACK_ERROR_HTML = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                        "<title>{code} - Africa ScholarBridge</title></head>"
                        "<body style='font-family:system-ui,sans-serif;text-align:center;padding:4rem 1rem'>"
                        "<h1>{code}</h1><p>{message}</p><p><a href='/'>Go to the homepage</a></p></body></html>")


def _render_error(code, message):
    try:
        return render_template(f"errors/{code}.html"), code
    except Exception:
        app.logger.exception("Error page errors/%s.html failed to render", code)
        return _FALLBACK_ERROR_HTML.format(code=code, message=message), code


@app.errorhandler(404)
def not_found(e):
    return _render_error(404, "Page not found.")


@app.errorhandler(403)
def forbidden(e):
    return _render_error(403, "You don't have permission to view this page.")


@app.errorhandler(500)
def server_error(e):
    return _render_error(500, "Something went wrong on our side. Please try again shortly.")


@app.errorhandler(413)
def file_too_large(e):
    flash(f"That file is too large. Please upload a file under {MAX_VISA_DOC_SIZE_BYTES // (1024*1024)} MB.", "danger")
    if request.path == url_for("application_visa_document_upload"):
        # An oversized visa document is a failed visa verification too:
        # same automatic outcome as any other failure.
        student = current_student() if session.get("role") == "student" and g.get("db") is not None else None
        cycle = get_current_cycle() if student else None
        if student and cycle:
            app_row = g.db.execute(
                "SELECT * FROM funding_applications WHERE student_id = ? AND cycle_id = ?",
                (student["id"], cycle["id"]),
            ).fetchone()
            if app_row and app_row["status"] == "Draft" and _visa_upload_incomplete(app_row):
                return _fail_visa_verification(g.db, student, app_row, cycle,
                                               ["The visa document is larger than the size limit."])
        return redirect(url_for("application_step", step_name="visa"))
    return redirect(request.referrer or url_for("dashboard"))


# Create any missing tables/columns on startup. init_db() is idempotent
# (CREATE TABLE IF NOT EXISTS + guarded ALTER TABLE), so this is also what
# upgrades an existing scholarbridge.db with the M-PESA payment columns.
init_db()

def _port_already_in_use(port):
    """True if something is ALREADY answering on 127.0.0.1:<port>.

    Important on Windows: Flask's development server sets SO_REUSEADDR,
    which on Windows lets a second `python app.py` start on port 5000
    WITHOUT any "address already in use" error while an older copy is
    still running in another Command Prompt window. The browser can then
    keep getting pages from the OLD copy. We refuse to start instead.
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    # WERKZEUG_RUN_MAIN is set only in the auto-reloader's child process;
    # do the checks and print the banner once, in the parent.
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        if _port_already_in_use(port):
            print("\n" + "!" * 72)
            print(f"  ERROR: another web server is ALREADY running on port {port}.")
            print("  It is probably an OLD copy of Africa ScholarBridge in another")
            print("  Command Prompt window. Close that window (or press CTRL+C in it),")
            print("  then run  python app.py  again.")
            print(f"  (Or start this copy on another port:  set PORT=5001  then  python app.py)")
            print("!" * 72 + "\n")
            raise SystemExit(1)
        print("\n" + "=" * 72)
        print(f"  {APP_NAME} - {APP_VERSION}")
        print(f"  Project folder : {PROJECT_DIR}")
        print(f"  Database       : {DB_PATH}")
        print(f"  Open           : http://127.0.0.1:{port}   (check: http://127.0.0.1:{port}/version)")
        print("=" * 72 + "\n")
    app.run(debug=debug, port=port)
