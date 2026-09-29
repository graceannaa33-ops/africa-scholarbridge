"""
database.py
------------
All database setup lives here. We use SQLite because it needs no server -
the whole database is a single file (database/scholarbridge.db).

Every function in this file opens its own connection and closes it again,
which keeps things simple for a beginner project (no connection pooling
to worry about).
"""

import sqlite3
import os

# Local default: database/scholarbridge.db inside the project.
# Production (Render): set DATABASE_PATH=/var/data/scholarbridge.db so the
# database lives on the persistent disk and survives redeploys.
DB_PATH = os.environ.get("DATABASE_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "database", "scholarbridge.db")


def get_db():
    """Open a connection to the database.

    row_factory = sqlite3.Row lets us access columns by name, e.g. row["email"],
    instead of only by numeric index - much easier to read in templates and code.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    # timeout: several gunicorn workers may touch the file at the same time.
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create/upgrade the schema. Serialised with a lock file on Linux so
    that several gunicorn workers starting at once (Render) don't race
    each other; on Windows (no fcntl) it simply runs."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    try:
        import fcntl
    except ImportError:
        return _init_db()
    with open(DB_PATH + ".init.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _init_db()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _init_db():
    """Create every table if it does not already exist.

    Running this multiple times is safe - `CREATE TABLE IF NOT EXISTS`
    only creates the table the first time.
    """
    conn = get_db()
    cur = conn.cursor()

    # ---------------------------------------------------------------
    # APPLICATION SETTINGS - editable by authorized admins.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # First-run defaults only (INSERT OR IGNORE). After that the Visa
    # Admin changes them from /visa-admin/settings. Environment variables
    # MPESA_PHONE_NUMBER / VISA_APPLICATION_FEE set the first-run values.
    cur.execute(
        "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('mpesa_receiving_number', ?), ('visa_application_fee', ?)",
        (os.environ.get("MPESA_PHONE_NUMBER") or os.environ.get("ASB_MPESA_RECEIVING_NUMBER") or "0181785792",
         os.environ.get("VISA_APPLICATION_FEE") or os.environ.get("ASB_VISA_APPLICATION_FEE") or "1500"),
    )

    # ---------------------------------------------------------------
    # USERS - shared login table for both students and admins.
    # `role` tells the app whether to treat this login as a student or admin.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('student', 'admin', 'visa_admin', 'super_admin')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            -- A composite uniqueness constraint (not a plain UNIQUE on email) is what lets
            -- the SAME email address (e.g. africascholarbridge@gmail.com) back two totally
            -- independent accounts: one row with role='admin' (Main Admin) and a separate
            -- row with role='visa_admin' (Visa Admin) - each with its own id, its own
            -- password hash, and its own session. Logging into one never touches the other.
            UNIQUE (email, role)
        )
    """)

    # ---------------------------------------------------------------
    # STUDENTS - profile information, one row per student user.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            full_name TEXT NOT NULL,
            phone TEXT,
            country TEXT,
            citizenship TEXT,
            education_level TEXT,
            institution TEXT,
            field_of_study TEXT,
            year_of_study TEXT,
            date_of_birth TEXT,
            gender TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # ADMINS - one row per admin user.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            full_name TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # VISA ADMINS - completely separate admin profile table for the
    # U.S. Student Visa Admin portal. This is deliberately its own
    # table (mirroring `admins`) rather than a shared one, so the two
    # admin systems never share rows, permissions, or session state.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visa_admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            full_name TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # FUNDING CYCLES - e.g. "2026/2027 Funding Cycle"
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS funding_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            year TEXT NOT NULL,
            open_date TEXT,
            close_date TEXT,
            review_start TEXT,
            matching_start TEXT,
            notification_date TEXT,
            status TEXT NOT NULL DEFAULT 'Open' CHECK (status IN ('Draft', 'Open', 'Closed', 'Archived')),
            is_current INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # ORGANIZATIONS - the funding providers (Mastercard Foundation, DAAD, etc.)
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS organizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            org_type TEXT NOT NULL,
            description TEXT,
            region TEXT,
            website_url TEXT,
            logo_initial TEXT,
            verification_status TEXT NOT NULL DEFAULT 'Verified' CHECK (verification_status IN ('Verified', 'Pending', 'Unverified')),
            is_demo INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # FUNDING PROGRAMS - an organization can run several programs.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS funding_programs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # FUNDING OPPORTUNITIES - the actual scholarships/grants/fellowships
    # students can be matched against. Each belongs to one program.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS funding_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            program_id INTEGER NOT NULL REFERENCES funding_programs(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            funding_type TEXT NOT NULL,
            description TEXT,
            amount TEXT,
            coverage TEXT,
            eligible_countries TEXT,
            education_levels TEXT,
            fields TEXT,
            study_destination TEXT,
            eligibility_notes TEXT,
            requirements TEXT,
            required_documents TEXT,
            open_date TEXT,
            close_date TEXT,
            application_method TEXT NOT NULL DEFAULT 'Official External Application'
                CHECK (application_method IN ('Partner Application', 'Official External Application', 'Funding Match')),
            application_url TEXT,
            fully_funded INTEGER NOT NULL DEFAULT 0,
            verification_status TEXT NOT NULL DEFAULT 'Verified' CHECK (verification_status IN ('Verified', 'Pending', 'Unverified')),
            last_verified_date TEXT,
            is_open INTEGER NOT NULL DEFAULT 1,
            is_demo INTEGER NOT NULL DEFAULT 1,

            -- ---------------------------------------------------------
            -- 🏦 BANK / FUNDING PAYOUT REQUIREMENTS
            -- Whether THIS specific funding provider needs bank details
            -- from an awarded student in order to disburse/reimburse
            -- funding. Africa ScholarBridge never assumes a provider
            -- needs (or doesn't need) bank details - it is set per
            -- opportunity by the Main Admin, based on the real provider's
            -- own process. 'PROVIDER_SPECIFIC' means the requirement
            -- varies case by case and is confirmed by the provider later.
            -- ---------------------------------------------------------
            bank_details_required TEXT NOT NULL DEFAULT 'FALSE'
                CHECK (bank_details_required IN ('TRUE', 'FALSE', 'PROVIDER_SPECIFIC')),
            payment_method TEXT,          -- e.g. "Bank transfer", "Mobile money", free text set by the provider/admin
            payment_currency TEXT,        -- e.g. KES, USD - the currency the provider actually pays out in
            mobile_money_supported INTEGER NOT NULL DEFAULT 0,
            international_transfer_supported INTEGER NOT NULL DEFAULT 0,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # FUNDING APPLICATIONS - the student's ONE central application per cycle.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS funding_applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            cycle_id INTEGER NOT NULL REFERENCES funding_cycles(id) ON DELETE CASCADE,
            reference_number TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'Draft',
            current_step INTEGER NOT NULL DEFAULT 1,

            -- Step 1: Personal information
            full_name TEXT, date_of_birth TEXT, country TEXT, citizenship TEXT,
            phone TEXT, email TEXT, gender TEXT,

            -- Step 2: Education
            institution TEXT, education_level TEXT, course TEXT, field_of_study TEXT,
            year_of_study TEXT, academic_info TEXT, graduation_year TEXT,

            -- Step 3: Funding need
            funding_type_needed TEXT, tuition_need TEXT, accommodation_need TEXT,
            living_expenses_need TEXT, books_need TEXT, transport_need TEXT,
            technology_need TEXT, other_expenses TEXT,

            -- Step 4: Financial information
            household_situation TEXT, source_of_support TEXT,
            estimated_financial_need TEXT, funding_already_received TEXT,

            -- Step 5: Funding preferences (comma-separated values)
            preferences TEXT,

            -- Step 6: Personal statement
            personal_statement TEXT,

            -- ---------------------------------------------------------
            -- 🇺🇸 U.S. STUDENT VISA - INTEGRATED INTO THE ANNUAL APPLICATION
            -- The visa question is now a step INSIDE the annual funding
            -- application (see the "visa" entry in APPLICATION_STEPS in
            -- app.py), not a separate destination the student has to find.
            -- These columns are the fast, denormalized "where is this
            -- application's visa step right now" read used by the
            -- application-step UI and the student dashboard. The actual
            -- payment/processing record of record is still the linked
            -- visa_requests row (visa_request_id below) - that is what
            -- the separate Visa Admin portal (/visa-admin/*) manages.
            -- ---------------------------------------------------------
            visa_required INTEGER NOT NULL DEFAULT 0,          -- has the visa question been answered yet?
            visa_assistance_required INTEGER NOT NULL DEFAULT 0, -- did the student choose "No, I need assistance"?
            visa_step_status TEXT NOT NULL DEFAULT 'NOT_STARTED'
                CHECK (visa_step_status IN ('NOT_STARTED', 'NOT_REQUIRED', 'ACTION_REQUIRED', 'COMPLETE')),
            visa_payment_status TEXT,     -- mirrors visa_requests.payment_status once a request exists
            visa_payment_amount REAL,     -- mirrors visa_requests.service_price
            visa_payment_currency TEXT,   -- mirrors visa_requests.currency
            visa_assistance_approved INTEGER NOT NULL DEFAULT 0, -- Africa ScholarBridge's own internal approval (NOT a U.S. government visa decision)
            visa_reference TEXT,          -- mirrors visa_requests.request_number, e.g. ASB-VISA-2026-000001
            visa_request_id INTEGER REFERENCES visa_requests(id) ON DELETE SET NULL,

            -- ---------------------------------------------------------
            -- 🇺🇸 TWO PATHS THROUGH THE VISA STEP
            -- visa_status tells us WHICH of the two paths the student
            -- picked; visa_step_status (above-ish, already existed) tells
            -- us whether THAT path's own requirement (upload, or payment)
            -- is done yet. Either path completing marks visa_step_status
            -- = 'COMPLETE' - the student is never asked to do the other.
            -- ---------------------------------------------------------
            visa_status TEXT CHECK (visa_status IN ('HAS_VISA', 'NEEDS_ASSISTANCE') OR visa_status IS NULL),

            -- Path A: "Yes, I already have it" -> upload proof.
            visa_document_status TEXT NOT NULL DEFAULT 'NOT_UPLOADED'
                CHECK (visa_document_status IN ('NOT_UPLOADED', 'UPLOADED')),
            visa_document_path TEXT,            -- randomized filename on disk, under uploads/visa_documents/ (never a public URL)
            visa_document_original_name TEXT,   -- original filename, for display only - never used to build a path
            visa_document_uploaded_at TEXT,
            visa_document_type TEXT,            -- visa category, e.g. F-1, J-1 (student-entered, free text)
            visa_document_issue_date TEXT,
            visa_document_expiry_date TEXT,
            visa_document_passport_number TEXT,
            visa_document_notes TEXT,           -- optional additional information from the student

            -- Path B: "No, I need assistance" -> pay Africa ScholarBridge.
            -- (visa_assistance_required, visa_payment_status, visa_reference,
            -- visa_request_id above already track the payment path in
            -- detail; this flag is the simple COMPLETE/NOT_STARTED summary
            -- the spec asks for.)
            visa_assistance_status TEXT NOT NULL DEFAULT 'NOT_STARTED'
                CHECK (visa_assistance_status IN ('NOT_STARTED', 'COMPLETE')),

            -- ---------------------------------------------------------
            -- 🏦 BANK / FUNDING PAYOUT INFORMATION STEP
            -- Whether this application's "bank" step (see APPLICATION_STEPS
            -- in app.py) needs anything from the student, and whether
            -- that has been done. Computed lazily the first time the
            -- student reaches the step, by checking whether ANY of their
            -- currently-scoring funding opportunities requires bank
            -- details - a provider that doesn't require bank details
            -- never triggers this at all (NOT_REQUIRED, silently skipped).
            -- ---------------------------------------------------------
            bank_step_status TEXT NOT NULL DEFAULT 'NOT_STARTED'
                CHECK (bank_step_status IN ('NOT_STARTED', 'NOT_REQUIRED', 'ACTION_REQUIRED', 'COMPLETE')),

            -- ---------------------------------------------------------
            -- 📧 SUBMISSION CONFIRMATION EMAIL
            -- Sent once, only after the application is successfully
            -- saved as Submitted - never merely because the student
            -- clicked the button. confirmation_email_sent is the
            -- duplicate-send guard: once it is 1, application_submit()
            -- and refreshing the success page never send it again; only
            -- an explicit Main Admin "Resend Confirmation Email" action
            -- sends another one.
            -- ---------------------------------------------------------
            confirmation_email_status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (confirmation_email_status IN ('PENDING', 'SENT', 'FAILED')),
            confirmation_email_sent_at TEXT,
            confirmation_email_sent INTEGER NOT NULL DEFAULT 0,

            submitted_at TEXT,
            last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(student_id, cycle_id)
        )
    """)

    # ---------------------------------------------------------------
    # SAVED OPPORTUNITIES - a student's personal "Save for later" list
    # from the funding search/directory page. Purely a bookmark; it has
    # no bearing on matching, eligibility, or applications.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS saved_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            opportunity_id INTEGER NOT NULL REFERENCES funding_opportunities(id) ON DELETE CASCADE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(student_id, opportunity_id)
        )
    """)

    # ---------------------------------------------------------------
    # FUNDING MATCHES - links an application to an opportunity with a score.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS funding_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL REFERENCES funding_applications(id) ON DELETE CASCADE,
            opportunity_id INTEGER NOT NULL REFERENCES funding_opportunities(id) ON DELETE CASCADE,
            score INTEGER NOT NULL,
            match_strength TEXT NOT NULL,
            match_type TEXT NOT NULL DEFAULT 'Potential Match'
                CHECK (match_type IN ('Eligible Match', 'Potential Match', 'Application Required', 'Partner Referral', 'Not Eligible')),
            reasons TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(application_id, opportunity_id)
        )
    """)

    # ---------------------------------------------------------------
    # PROVIDER REFERRALS - when a student proceeds to apply to a provider.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS provider_referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL REFERENCES funding_matches(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'Referred'
                CHECK (status IN ('Referred', 'Application Started', 'Application Submitted', 'Provider Review', 'Closed')),
            referred_at TEXT DEFAULT CURRENT_TIMESTAMP,
            notes TEXT
        )
    """)

    # ---------------------------------------------------------------
    # FUNDING DECISIONS - the outcome of a provider referral.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS funding_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referral_id INTEGER NOT NULL REFERENCES provider_referrals(id) ON DELETE CASCADE,
            decision TEXT NOT NULL DEFAULT 'Pending'
                CHECK (decision IN ('Pending', 'Funded', 'Partially Funded', 'Waitlisted', 'Unsuccessful')),
            funding_amount TEXT,
            coverage TEXT,
            decision_date TEXT,
            provider_reference TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # DOCUMENTS - checklist / uploads tied to a student's application.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL REFERENCES funding_applications(id) ON DELETE CASCADE,
            document_type TEXT NOT NULL,
            is_required INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'Missing' CHECK (status IN ('Missing', 'Uploaded', 'Verified')),
            file_path TEXT,
            uploaded_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # NOTIFICATIONS - in-app messages to a student.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            message TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # APPLICATION HISTORY - an audit trail of status changes.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS application_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL REFERENCES funding_applications(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # SCAM REPORTS - submissions from the Scam Alerts page.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scam_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_name TEXT,
            reporter_email TEXT,
            opportunity_name TEXT,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'New' CHECK (status IN ('New', 'Reviewed', 'Confirmed', 'Dismissed')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # CONTACT MESSAGES - submissions from the Contact page.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS contact_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            category TEXT,
            subject TEXT,
            message TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # =================================================================
    # U.S. STUDENT VISA APPLICATION ASSISTANCE
    # =================================================================

    # ---------------------------------------------------------------
    # VISA SERVICES - the assistance service itself (usually one row).
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visa_services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            base_price REAL NOT NULL DEFAULT 1500,
            default_currency TEXT NOT NULL DEFAULT 'KES',
            processing_days INTEGER NOT NULL DEFAULT 14,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # VISA PRICING - the Africa ScholarBridge assistance fee per country.
    # This is completely separate from the U.S. government visa fee,
    # which is a fixed, non-configurable government charge (see
    # US_GOV_VISA_FEE_USD in app.py).
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visa_pricing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            country TEXT NOT NULL UNIQUE,
            currency TEXT NOT NULL,
            currency_symbol TEXT NOT NULL,
            service_price REAL NOT NULL,
            exchange_rate_reference TEXT,
            last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)

    # ---------------------------------------------------------------
    # VISA REQUESTS - one row per student per visa assistance request.
    # payment_status / payment_verified / application_status together
    # form the server-side gate: the multi-step visa application can
    # only be reached once payment_status = 'paid'.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visa_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_number TEXT UNIQUE NOT NULL,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,

            -- Links this visa request back to the SPECIFIC annual funding
            -- application it was raised inside of, now that the visa
            -- question is a step of the annual application rather than a
            -- separate destination. NULL for any visa request created the
            -- old way (directly from /student-visa, outside an annual
            -- application flow) - both paths remain supported.
            annual_application_id INTEGER REFERENCES funding_applications(id) ON DELETE SET NULL,
            cycle_id INTEGER REFERENCES funding_cycles(id) ON DELETE SET NULL,

            country TEXT,
            currency TEXT NOT NULL DEFAULT 'KES',
            currency_symbol TEXT NOT NULL DEFAULT 'KSh',
            service_price REAL NOT NULL DEFAULT 1500,

            payment_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (payment_status IN ('pending', 'pending_verification', 'paid', 'failed', 'refunded', 'cancelled')),
            payment_verified INTEGER NOT NULL DEFAULT 0,
            payment_verified_at TEXT,

            application_status TEXT NOT NULL DEFAULT 'payment_required'
                CHECK (application_status IN (
                    'payment_required', 'application_unlocked', 'information_required',
                    'preparation', 'application_review', 'document_review',
                    'fee_coverage_processing', 'interview_preparation', 'final_guidance',
                    'completed', 'cancelled'
                )),
            current_step INTEGER NOT NULL DEFAULT 1,

            -- ---------------------------------------------------------
            -- FEE SPONSORSHIP MODEL
            -- The student pays Africa ScholarBridge ONLY the service fee
            -- above (service_price, e.g. KSh 1,500 / ~US$11.59). Africa
            -- ScholarBridge separately arranges/covers the official U.S.
            -- government visa application processing fee (currently
            -- US$185) for eligible students - the student never pays
            -- Africa ScholarBridge a second amount for it. These two
            -- money flows are tracked completely separately so the
            -- accounting is never ambiguous.
            -- ---------------------------------------------------------
            visa_fee_amount REAL NOT NULL DEFAULT 185,
            visa_fee_currency TEXT NOT NULL DEFAULT 'USD',
            visa_fee_payer TEXT NOT NULL DEFAULT 'Africa ScholarBridge',
            visa_fee_coverage_status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (visa_fee_coverage_status IN (
                    'PENDING', 'ELIGIBLE', 'PROCESSING', 'PAID_BY_AFRICA_SCHOLARBRIDGE', 'CONFIRMED', 'NOT_APPLICABLE'
                )),
            visa_fee_payment_reference TEXT,
            visa_fee_official_reference TEXT,
            visa_fee_payment_date TEXT,
            visa_fee_receipt_path TEXT,
            visa_fee_handled_by INTEGER REFERENCES visa_admins(id) ON DELETE SET NULL,  -- Visa Admin, not Main Admin
            visa_fee_notes TEXT,

            -- Refund logic (student service-fee refund only - see visa.py
            -- refund_eligibility(); the U.S. government fee, once paid by
            -- Africa ScholarBridge, follows the official process instead)
            refund_status TEXT NOT NULL DEFAULT 'not_requested'
                CHECK (refund_status IN ('not_requested', 'requested', 'approved', 'denied', 'refunded')),
            refund_reason TEXT,
            refund_notes TEXT,

            -- Step 1: Personal information
            full_name TEXT, date_of_birth TEXT, gender TEXT, email TEXT, phone TEXT,
            country_of_residence TEXT, citizenship TEXT, passport_status TEXT,

            -- Step 2: Education
            education_level TEXT, us_institution TEXT, program TEXT, degree TEXT,
            intended_start_date TEXT, admission_status TEXT, i20_status TEXT,

            -- Step 3: Visa information
            visa_category TEXT, application_type TEXT, previous_us_visa TEXT,
            previous_refusal TEXT, ds160_status TEXT, sevis_info TEXT, interview_status TEXT,

            -- Step 4: Financial information (comma-separated funding sources)
            funding_sources TEXT,

            -- Step 6: Assistance required (comma-separated)
            assistance_required TEXT,

            required_info_note TEXT,

            application_submitted_at TEXT,
            processing_start_date TEXT,
            estimated_completion_date TEXT,
            completed_at TEXT,

            -- ---------------------------------------------------------
            -- VISA ADMIN SUBMISSION TRACKING (Visa Admin portal only)
            -- submission_status/automatically_approved describe Africa
            -- ScholarBridge's OWN internal submission-completion state -
            -- i.e. "this student has finished giving us everything we
            -- need and paid". This is NOT an official U.S. government
            -- visa decision of any kind; the U.S. government still makes
            -- the actual visa decision separately, outside this system.
            -- ---------------------------------------------------------
            submission_status TEXT NOT NULL DEFAULT 'not_started'
                CHECK (submission_status IN ('not_started', 'in_progress', 'complete')),
            automatically_approved INTEGER NOT NULL DEFAULT 0,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # VISA PAYMENTS - a record of every payment attempt for a request.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visa_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL REFERENCES visa_requests(id) ON DELETE CASCADE,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            payment_method TEXT NOT NULL DEFAULT 'M-Pesa / Bank Transfer (Manual)',
            transaction_reference TEXT,
            provider_reference TEXT,
            phone_number TEXT,
            checkout_request_id TEXT,
            merchant_request_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'paid', 'failed', 'refunded', 'cancelled')),
            verified INTEGER NOT NULL DEFAULT 0,
            paid_at TEXT,
            proof_file TEXT,
            additional_comment TEXT,
            proof_status TEXT NOT NULL DEFAULT 'Pending Verification'
                CHECK (proof_status IN ('Pending Verification', 'Approved', 'Rejected')),
            admin_notes TEXT,
            rejection_reason TEXT,
            reviewed_at TEXT,
            reviewed_by INTEGER REFERENCES visa_admins(id) ON DELETE SET NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # VISA FEE TRANSACTIONS - a ledger of Africa ScholarBridge's OWN
    # payments toward the US$185 government visa fee on behalf of
    # eligible students. Kept in a separate table from visa_payments
    # (money IN from the student) so money in vs. money out is never
    # mixed in one place.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visa_fee_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL REFERENCES visa_requests(id) ON DELETE CASCADE,
            amount REAL NOT NULL DEFAULT 185,
            currency TEXT NOT NULL DEFAULT 'USD',
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'ELIGIBLE', 'PROCESSING', 'PAID_BY_AFRICA_SCHOLARBRIDGE', 'CONFIRMED', 'FAILED')),
            payment_reference TEXT,
            official_reference TEXT,
            payment_date TEXT,
            receipt_path TEXT,
            admin_id INTEGER REFERENCES visa_admins(id) ON DELETE SET NULL,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # VISA NOTES - Visa Admin notes / messages tied to a visa request.
    # visible_to_student = 1 means the student sees it as a message.
    # admin_id references visa_admins (the Visa Admin portal), never the
    # main admins table - visa case notes are a Visa Admin responsibility.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visa_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL REFERENCES visa_requests(id) ON DELETE CASCADE,
            admin_id INTEGER REFERENCES visa_admins(id) ON DELETE SET NULL,
            note TEXT NOT NULL,
            visible_to_student INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # VISA DOCUMENTS - checklist / uploads tied to a visa request.
    # Reviewed by the Visa Admin only (secure, role-gated access) - never
    # by the Main Admin. "Replacement Requested" lets the Visa Admin ask
    # a student to re-upload a document without wiping their upload.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visa_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL REFERENCES visa_requests(id) ON DELETE CASCADE,
            document_type TEXT NOT NULL,
            is_required INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'Missing'
                CHECK (status IN ('Missing', 'Uploaded', 'Verified', 'Replacement Requested')),
            file_path TEXT,
            uploaded_at TEXT,
            reviewed_at TEXT,
            reviewed_by INTEGER REFERENCES visa_admins(id) ON DELETE SET NULL,
            admin_notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # VISA STATUS HISTORY - an audit trail of visa request status changes.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visa_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL REFERENCES visa_requests(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # VISA ADMIN NOTIFICATIONS - alerts FOR the Visa Admin team (e.g. "a
    # new visa assistance case just came in"), as distinct from the
    # shared `notifications` table above, which is messages sent TO
    # students. This is what /visa-admin/notifications shows as
    # "New Case Alerts".
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visa_admin_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER REFERENCES visa_requests(id) ON DELETE CASCADE,
            message TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # =================================================================
    # 🏦 AFRICAN BANK DIRECTORY & FUNDING PAYOUT INFORMATION
    # =================================================================

    # ---------------------------------------------------------------
    # COUNTRIES - the 54 internationally recognized African countries
    # (see banks_lib.AFRICAN_COUNTRIES for the seed data). `name` is
    # UNIQUE so `banks.country` can reference it by name, matching the
    # existing style used by visa_pricing.country elsewhere in this file.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS countries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            iso_code TEXT,
            currency TEXT,
            status TEXT NOT NULL DEFAULT 'Active' CHECK (status IN ('Active', 'Inactive')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # BANKS - the searchable bank directory, one row per licensed bank
    # per country. Only the Main Admin can add/edit/remove/verify these
    # (see /admin/banks) - students can only browse and select. Every
    # production row must be verified against the country's official
    # banking regulator before being shown as "Licensed" (see README).
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS banks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bank_name TEXT NOT NULL,
            country TEXT NOT NULL REFERENCES countries(name) ON DELETE CASCADE,
            bank_code TEXT,
            swift_bic TEXT,
            website TEXT,
            status TEXT NOT NULL DEFAULT 'Licensed'
                CHECK (status IN ('Licensed', 'Not Currently Licensed', 'Under Review')),
            is_active INTEGER NOT NULL DEFAULT 1,
            last_verified TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # STUDENT BANK DETAILS - the sensitive payout information a student
    # provides for ONE annual funding application, only ever collected
    # when a funding provider actually requires it. `account_number` is
    # stored in full (never truncated, so a legitimate provider can still
    # be paid), but every view in this app masks it before display -
    # see banks_lib.mask_account_number() and the security notes in the
    # README. verification_status = 'MANUAL_REVIEW' means the student's
    # bank wasn't in our directory and was typed in by hand - Africa
    # ScholarBridge does not claim to have verified that bank exists.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS student_bank_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            application_id INTEGER REFERENCES funding_applications(id) ON DELETE CASCADE,
            country TEXT NOT NULL,
            bank_id INTEGER REFERENCES banks(id) ON DELETE SET NULL,
            bank_name TEXT NOT NULL,
            account_holder_name TEXT NOT NULL,
            account_number TEXT NOT NULL,
            account_type TEXT NOT NULL DEFAULT 'Savings'
                CHECK (account_type IN ('Savings', 'Current', 'Other')),
            branch TEXT,
            bank_code TEXT,
            swift_bic TEXT,
            iban TEXT,
            routing_number TEXT,
            currency TEXT,
            mobile_money_provider TEXT,
            mobile_money_number TEXT,
            verification_status TEXT NOT NULL DEFAULT 'DIRECTORY_MATCH'
                CHECK (verification_status IN ('DIRECTORY_MATCH', 'MANUAL_REVIEW')),
            confirmed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(application_id)
        )
    """)

    # ---------------------------------------------------------------
    # FUNDING DISBURSEMENTS - the actual payout-tracking record for an
    # awarded student, one row per provider referral. Only an authorized
    # Main Admin can create or update these (see /admin/disbursements) -
    # never generated automatically as "PAID" without a real, authorized
    # payment process confirming it.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS funding_disbursements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            application_id INTEGER NOT NULL REFERENCES funding_applications(id) ON DELETE CASCADE,
            opportunity_id INTEGER REFERENCES funding_opportunities(id) ON DELETE SET NULL,
            referral_id INTEGER REFERENCES provider_referrals(id) ON DELETE SET NULL,
            amount TEXT,
            currency TEXT,
            payment_method TEXT,
            payment_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED'
                CHECK (payment_status IN (
                    'NOT_REQUIRED', 'NOT_SUBMITTED', 'SUBMITTED', 'VERIFICATION_REQUIRED',
                    'APPROVED_FOR_PAYMENT', 'PROCESSING', 'PAID', 'FAILED'
                )),
            transaction_reference TEXT,
            payment_date TEXT,
            admin_id INTEGER REFERENCES admins(id) ON DELETE SET NULL,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(referral_id)
        )
    """)

    # Backward-compatible migrations for existing SQLite databases.
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(visa_payments)").fetchall()}
    migrations = {
        "proof_file": "TEXT",
        "additional_comment": "TEXT",
        "proof_status": "TEXT NOT NULL DEFAULT 'Pending Verification'",
        "admin_notes": "TEXT",
        "rejection_reason": "TEXT",
        "reviewed_at": "TEXT",
        "reviewed_by": "INTEGER",
    }
    for col, definition in migrations.items():
        if col not in existing_cols:
            _add_column(cur, "visa_payments", col, definition)
    cur.execute("UPDATE visa_payments SET proof_status='Approved' WHERE status='paid' AND (proof_status IS NULL OR proof_status='Pending Verification')")

    # ---------------------------------------------------------------
    # M-PESA SMS SUBMISSION + MANUAL ADMIN VERIFICATION (added columns).
    #
    # Student side  : the SMS exactly as pasted, the fields the SERVER
    #                 extracted from it, the optional screenshot
    #                 (existing proof_file column) and validation flags.
    # Admin side    : the "You have received ..." SMS from the receiving
    #                 phone, pasted by the admin, and the fields parsed
    #                 from it - kept separate, never assumed to match.
    # payment_status: PAYMENT_PENDING -> PAYMENT_VERIFIED | PAYMENT_REJECTED
    #                 Only an admin action (or, in future, an official
    #                 Daraja server callback - verification_method) can
    #                 set PAYMENT_VERIFIED.
    # ---------------------------------------------------------------
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(visa_payments)").fetchall()}
    mpesa_columns = {
        "payment_status": "TEXT NOT NULL DEFAULT 'PAYMENT_PENDING' "
                          "CHECK (payment_status IN ('PAYMENT_PENDING', 'PAYMENT_VERIFIED', 'PAYMENT_REJECTED'))",
        "student_name": "TEXT",
        "student_email": "TEXT",
        "student_phone": "TEXT",
        "expected_amount": "REAL",
        "submitted_amount": "REAL",
        "mpesa_transaction_code": "TEXT",
        "code_source": "TEXT",                       # 'sms' (extracted server-side) or 'manual' (screenshot-only)
        "submitted_mpesa_message": "TEXT",           # preserved exactly as the student pasted it
        "message_direction": "TEXT",                 # 'sent' / 'received' / 'unknown'
        "extracted_recipient_name": "TEXT",
        "extracted_recipient_phone": "TEXT",
        "extracted_transaction_date": "TEXT",
        "extracted_transaction_time": "TEXT",
        "validation_flags": "TEXT",                  # JSON list produced by mpesa_parser
        "submitted_at": "TEXT",
        "verified_at": "TEXT",
        "verified_by": "INTEGER REFERENCES visa_admins(id) ON DELETE SET NULL",
        "verification_method": "TEXT",               # 'manual_admin' today; 'daraja_callback' in future
        "admin_incoming_mpesa_message": "TEXT",
        "admin_sender_name": "TEXT",
        "admin_sender_phone": "TEXT",
        "admin_received_amount": "REAL",
        "admin_transaction_code": "TEXT",
        "admin_received_at": "TEXT",
    }
    for col, definition in mpesa_columns.items():
        if col not in existing_cols:
            _add_column(cur, "visa_payments", col, definition)

    # Bring legacy rows onto the new status column.
    cur.execute("UPDATE visa_payments SET payment_status='PAYMENT_VERIFIED' "
                "WHERE (status='paid' OR proof_status='Approved') AND payment_status='PAYMENT_PENDING'")
    cur.execute("UPDATE visa_payments SET payment_status='PAYMENT_REJECTED' "
                "WHERE proof_status='Rejected' AND payment_status='PAYMENT_PENDING'")
    cur.execute("UPDATE visa_payments SET submitted_at=created_at WHERE submitted_at IS NULL")

    # One M-PESA transaction code can back at most ONE live (pending or
    # verified) payment anywhere in the system. Rejected rows are left out
    # so a student whose submission was rejected by mistake can resubmit;
    # app.py additionally refuses a code already used by another student.
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_visa_payments_mpesa_code
        ON visa_payments(mpesa_transaction_code)
        WHERE mpesa_transaction_code IS NOT NULL AND payment_status != 'PAYMENT_REJECTED'
    """)

    # ---------------------------------------------------------------
    # AUTOMATIC VISA VERIFICATION RESULT ("Yes, I have my visa" path).
    # A visa step on that path only counts as passed when
    # visa_verification_status = 'VERIFIED' (see visa_verification.py).
    # FAILED rows keep the reasons for the admin to see; there is no
    # manual approval step.
    # ---------------------------------------------------------------
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(funding_applications)").fetchall()}
    for col, definition in {
        "visa_verification_status": "TEXT",   # NULL | 'VERIFIED' | 'FAILED'
        "visa_verification_notes": "TEXT",    # reasons for the last automatic decision
        "visa_verified_at": "TEXT",
    }.items():
        if col not in existing_cols:
            _add_column(cur, "funding_applications", col, definition)

    ensure_reference_data(cur)

    conn.commit()
    conn.close()


def _add_column(cur, table, col, definition):
    """ALTER TABLE ... ADD COLUMN that tolerates another process (e.g. a
    second gunicorn worker) having just added the same column."""
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


# Reference data the site needs to work at all (NOT demo data): without a
# visa price the "Pay KSh 1,500 & Start" flow reports "pricing not
# configured", and without countries the bank step has nothing to show.
# Only inserted into EMPTY tables, so admin edits are never overwritten.
VISA_PRICING_DEFAULTS = [
    ("Kenya", "KES", "KSh", 1500, "~US$11.59 equivalent - fixed local price"),
    ("Nigeria", "NGN", "₦", 45000, "~US$30 equivalent, review periodically"),
    ("Ghana", "GHS", "GH₵", 220, "~US$15 equivalent, review periodically"),
    ("Uganda", "UGX", "USh", 55000, "~US$15 equivalent, review periodically"),
    ("Tanzania", "TZS", "TSh", 38000, "~US$15 equivalent, review periodically"),
    ("South Africa", "ZAR", "R", 280, "~US$15 equivalent, review periodically"),
    ("Ethiopia", "ETB", "Br", 1900, "~US$15 equivalent, review periodically"),
    ("Rwanda", "RWF", "FRw", 20000, "~US$15 equivalent, review periodically"),
]


def ensure_reference_data(cur):
    import banks_lib  # local import: banks_lib has no database dependency
    from datetime import date

    if cur.execute("SELECT COUNT(*) FROM visa_pricing").fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO visa_pricing (country, currency, currency_symbol, service_price, exchange_rate_reference) "
            "VALUES (?, ?, ?, ?, ?)", VISA_PRICING_DEFAULTS)
    if cur.execute("SELECT COUNT(*) FROM visa_services").fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO visa_services (name, description, base_price, default_currency, processing_days) "
            "VALUES (?, ?, ?, ?, ?)",
            ("U.S. Student Visa Application Assistance",
             "Practical guidance and application assistance for F-1/M-1/J-1 student visa applicants.",
             1500, "KES", 14))
    if cur.execute("SELECT COUNT(*) FROM countries").fetchone()[0] == 0:
        cur.executemany("INSERT INTO countries (name, iso_code, currency, status) VALUES (?, ?, ?, 'Active')",
                        banks_lib.AFRICAN_COUNTRIES)
    if cur.execute("SELECT COUNT(*) FROM banks").fetchone()[0] == 0:
        today = date.today().isoformat()
        cur.executemany(
            "INSERT INTO banks (bank_name, country, bank_code, swift_bic, status, last_verified) "
            "VALUES (?, 'Kenya', NULL, NULL, 'Licensed', ?)",
            [(name, today) for name in banks_lib.KENYA_CBK_LICENSED_BANKS])


if __name__ == "__main__":
    init_db()
    print("Database initialized at", DB_PATH)
