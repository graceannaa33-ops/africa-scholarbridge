"""
seed_data.py
-------------
Populates the database with realistic demonstration data so every page of
Africa ScholarBridge looks complete out of the box.

Run this after initializing the database:

    python seed_data.py

Running it again is safe - it wipes and recreates all tables first, so you
always start from a clean, consistent demo dataset. Records created here
are marked `is_demo = 1` in organizations and funding_opportunities, so an
admin can tell them apart from real, live data later.
"""

import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from werkzeug.security import generate_password_hash

from database import get_db, init_db, DB_PATH
from matching import run_matching_for_application
import visa as visa_lib
import banks_lib

TODAY = date.today()


def reset_database():
    """Delete any existing database file and recreate all tables fresh."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()
    # init_db() pre-fills reference tables for production; this script
    # inserts its own full demo versions below, so start them empty.
    conn = get_db()
    for table in ("banks", "countries", "visa_services", "visa_pricing"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()


def d(days_from_today):
    return (TODAY + timedelta(days=days_from_today)).isoformat()


def seed():
    reset_database()
    db = get_db()

    # -------------------------------------------------------------
    # 1. Demo admin account
    #
    # The email and password both come from environment variables so a
    # real deployment never has to keep a password in source control.
    # ADMIN_PASSWORD falls back to a clearly-labeled demo password ONLY
    # so this beginner project still works out of the box - always set
    # a real ADMIN_PASSWORD before deploying anywhere real.
    # -------------------------------------------------------------
    # No built-in/demo admin password any more. Without ADMIN_PASSWORD the
    # account is created with a random password nobody knows (never
    # printed); set the real one afterwards with:  python create_admin.py
    admin_email = os.environ.get("ADMIN_EMAIL", "africascholarbridge@gmail.com")
    admin_password = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(32)
    admin_user_id = db.execute(
        "INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'admin')",
        (admin_email, generate_password_hash(admin_password)),
    ).lastrowid
    db.execute(
        "INSERT INTO admins (user_id, full_name) VALUES (?, ?)",
        (admin_user_id, "Africa ScholarBridge Admin"),
    )

    # -------------------------------------------------------------
    # 1b. Visa Admin - a COMPLETELY SEPARATE account from the Main Admin
    # above, even though it can share the same email address. It has its
    # own users row (role='visa_admin'), its own visa_admins profile row,
    # and its own password from its own environment variable. Nothing
    # here reuses the Main Admin's password hash or session - logging in
    # at /visa-admin/login is a fully independent authentication event.
    # VISA_ADMIN_PASSWORD falls back to a clearly-labeled demo password
    # ONLY so this project still works out of the box - always set a
    # real VISA_ADMIN_PASSWORD before deploying anywhere real.
    # -------------------------------------------------------------
    visa_admin_email = os.environ.get("VISA_ADMIN_EMAIL") or admin_email
    visa_admin_password = os.environ.get("VISA_ADMIN_PASSWORD") or secrets.token_urlsafe(32)
    visa_admin_user_id = db.execute(
        "INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'visa_admin')",
        (visa_admin_email, generate_password_hash(visa_admin_password)),
    ).lastrowid
    db.execute(
        "INSERT INTO visa_admins (user_id, full_name) VALUES (?, ?)",
        (visa_admin_user_id, "Africa ScholarBridge Visa Admin"),
    )

    # -------------------------------------------------------------
    # 2. Annual funding cycle
    # -------------------------------------------------------------
    cycle_id = db.execute(
        """INSERT INTO funding_cycles
           (name, year, open_date, close_date, review_start, matching_start,
            notification_date, status, is_current)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'Open', 1)""",
        ("2026/2027 Funding Cycle", "2026/2027", d(-60), d(180), d(-45), d(-30), d(30)),
    ).lastrowid

    # A closed previous cycle, for history / demo purposes.
    db.execute(
        """INSERT INTO funding_cycles
           (name, year, open_date, close_date, status, is_current)
           VALUES (?, ?, ?, ?, 'Closed', 0)""",
        ("2025/2026 Funding Cycle", "2025/2026", d(-420), d(-60)),
    )

    # -------------------------------------------------------------
    # 3. Organizations (23 total, per the product spec)
    # -------------------------------------------------------------
    organizations = [
        ("Global Partnership for Education", "International Organization", "Global",
         "GPE is the only multilateral partnership devoted entirely to education in developing countries."),
        ("UNICEF", "International Organization", "Global",
         "The United Nations Children's Fund supports education access and quality for children worldwide."),
        ("UNESCO", "International Organization", "Global",
         "UNESCO promotes international cooperation in education, science, and culture."),
        ("Education Cannot Wait (ECW)", "NGO", "Global",
         "ECW is the UN global fund for education in emergencies and protracted crises."),
        ("The World Bank / IDA", "Development Bank", "Global",
         "The International Development Association provides financing for education programs in low-income countries."),
        ("African Development Bank (AfDB)", "Development Bank", "Africa",
         "AfDB finances education and skills-development programs across the African continent."),
        ("Asian Development Bank (ADB)", "Development Bank", "Asia",
         "ADB supports education financing and scholarships across Asia and the Pacific."),
        ("Inter-American Development Bank (IDB)", "Development Bank", "Americas",
         "IDB funds education initiatives across Latin America and the Caribbean."),
        ("Bill & Melinda Gates Foundation", "Foundation", "Global",
         "A philanthropic foundation supporting global education and development initiatives."),
        ("Open Society Foundations", "Foundation", "Global",
         "Supports scholarships and fellowships that advance justice, education, and human rights."),
        ("The LEGO Foundation", "Foundation", "Global",
         "Invests in learning-through-play initiatives and early-childhood education."),
        ("Aga Khan Foundation (AKF)", "Foundation", "Africa & Asia",
         "AKF supports education programs and an International Scholarship Programme for students from developing countries."),
        ("Mastercard Foundation", "Foundation", "Africa",
         "Runs the Mastercard Foundation Scholars Program supporting African students at partner universities."),
        ("Erasmus Mundus", "Education Organization", "Europe",
         "EU-funded joint master's degree scholarships for students worldwide, including African applicants."),
        ("Commonwealth Scholarships", "Government Organization", "Commonwealth",
         "UK Government-funded scholarships for students from Commonwealth countries."),
        ("DAAD", "Government Organization", "Germany",
         "The German Academic Exchange Service funds scholarships for international students in Germany."),
        ("Fulbright Foreign Student Program", "Government Organization", "United States",
         "US Government-funded program for graduate study, research, and teaching in the United States."),
        ("AAUW International Fellowships", "Fellowship Provider", "United States",
         "The American Association of University Women funds fellowships for international women graduate students."),
        ("Joint Japan/World Bank Graduate Scholarship Program", "Scholarship Provider", "Global",
         "Funds graduate studies for students from World Bank member developing countries."),
        ("Aga Khan Foundation International Scholarship Programme", "Scholarship Provider", "Africa & Asia",
         "Provides scholarships on a 50% grant / 50% loan basis for postgraduate study."),
        ("IEFA", "Funding Search Platform", "Global",
         "International Education Financial Aid - a search platform for international scholarships and financial aid."),
        ("International Scholarships", "Funding Search Platform", "Global",
         "A directory and search platform for international scholarship opportunities."),
        ("EducationUSA", "Funding Search Platform", "United States",
         "US Department of State network providing accurate information on US higher-education funding."),
    ]

    org_ids = {}
    for name, org_type, region, description in organizations:
        org_id = db.execute(
            """INSERT INTO organizations (name, org_type, description, region, logo_initial, verification_status, is_demo)
               VALUES (?, ?, ?, ?, ?, 'Verified', 1)""",
            (name, org_type, description, region, name[0]),
        ).lastrowid
        org_ids[name] = org_id

        # Give every organization at least one funding program.
        db.execute(
            "INSERT INTO funding_programs (organization_id, name, description) VALUES (?, ?, ?)",
            (org_id, f"{name} Education Funding Program", f"Primary funding program run by {name}."),
        )

    program_ids = {
        row["organization_id"]: row["id"]
        for row in db.execute("SELECT id, organization_id FROM funding_programs").fetchall()
    }

    # -------------------------------------------------------------
    # 4. Funding opportunities
    # Each tuple: (org_name, title, funding_type, description, amount, coverage,
    #              countries, levels, fields, destination, method, url,
    #              fully_funded, open_offset, close_offset, is_open)
    # -------------------------------------------------------------
    opportunities = [
        ("Mastercard Foundation", "Mastercard Foundation Scholars Program", "Scholarship",
         "Fully funded scholarships for academically talented but economically disadvantaged African students.",
         "Full Tuition + Stipend", "Tuition, accommodation, living expenses",
         "All", "Undergraduate, Master's", "All", "Africa, Canada",
         "Partner Application", "https://mastercardfdn.org/scholars", 1, -30, 90, 1),

        ("DAAD", "DAAD Development-Related Postgraduate Courses", "Scholarship",
         "Scholarships for young professionals from developing countries for postgraduate studies in Germany.",
         "Full Tuition + Stipend", "Tuition, monthly stipend, travel allowance",
         "All", "Master's, PhD", "Engineering, Economics, Development Studies", "Germany",
         "Official External Application", "https://www.daad.de", 1, -20, 60, 1),

        ("Commonwealth Scholarships", "Commonwealth Master's Scholarships", "Scholarship",
         "Scholarships for citizens of Commonwealth countries to pursue master's study in the UK.",
         "Full Tuition + Stipend", "Tuition, living allowance, airfare",
         "Kenya, Uganda, Tanzania, Nigeria, Ghana, All", "Master's", "All", "United Kingdom",
         "Official External Application", "https://cscuk.fcdo.gov.uk", 1, -40, 45, 1),

        ("Fulbright Foreign Student Program", "Fulbright Foreign Student Program", "Scholarship",
         "Graduate study, research, and teaching opportunities in the United States.",
         "Full Tuition + Stipend", "Tuition, living stipend, health insurance",
         "All", "Master's, PhD", "All", "United States",
         "Official External Application", "https://foreign.fulbrightonline.org", 1, -10, 120, 1),

        ("Erasmus Mundus", "Erasmus Mundus Joint Master Degrees", "Scholarship",
         "Fully funded joint master's degrees delivered by consortia of European universities.",
         "Full Tuition + Stipend", "Tuition, travel, monthly allowance",
         "All", "Master's", "All", "Europe",
         "Official External Application", "https://erasmus-plus.ec.europa.eu", 1, -15, 75, 1),

        ("Aga Khan Foundation (AKF)", "AKF International Scholarship Programme", "Sponsorship",
         "50% grant / 50% low-interest loan for postgraduate studies for students from AKF focus countries.",
         "Partial (50/50)", "Tuition support",
         "Kenya, Tanzania, Uganda, Pakistan, All", "Master's, PhD", "All", "Any",
         "Partner Application", "https://the.akdn/en/how-we-work/our-agencies/aga-khan-foundation/scholarship-programme", 0, -25, 30, 1),

        ("Joint Japan/World Bank Graduate Scholarship Program", "JJ/WBGSP Graduate Scholarship", "Scholarship",
         "Scholarships for graduate studies in development-related fields for students from World Bank member countries.",
         "Full Tuition + Stipend", "Tuition, living expenses, travel",
         "All", "Master's", "Development Studies, Economics, Public Policy", "Any",
         "Official External Application", "https://www.worldbank.org/en/programs/scholarships", 1, -35, 20, 1),

        ("AAUW International Fellowships", "AAUW International Fellowship", "Fellowship",
         "Fellowships for international women pursuing full-time graduate or postdoctoral study in the US.",
         "USD 18,000 - 30,000", "Tuition and living stipend",
         "All", "Master's, PhD", "All", "United States",
         "Official External Application", "https://www.aauw.org/resources/programs/fellowships-grants/", 0, -5, 100, 1),

        ("Open Society Foundations", "Open Society Foundation Fellowship", "Fellowship",
         "Supports individuals working on social justice, education access, and human rights initiatives.",
         "Varies", "Project funding and stipend",
         "All", "Master's, PhD, Undergraduate", "Law, Social Sciences, Human Rights", "Any",
         "Official External Application", "https://www.opensocietyfoundations.org/grants", 0, -50, 15, 1),

        ("Bill & Melinda Gates Foundation", "Gates Foundation Education Grant", "Grant",
         "Grants supporting innovative approaches to education access and quality in developing countries.",
         "Varies", "Program-based funding",
         "All", "All", "Education, Public Health", "Any",
         "Funding Match", "https://www.gatesfoundation.org", 0, -60, 150, 1),

        ("The LEGO Foundation", "LEGO Foundation Learning Through Play Grant", "Grant",
         "Funding for programs that advance learning through play in early-childhood education.",
         "Varies", "Program-based funding",
         "All", "All", "Early Childhood Education", "Any",
         "Funding Match", "https://learningthroughplay.com", 0, -10, 90, 1),

        ("African Development Bank (AfDB)", "AfDB Skills for Employability Scholarship", "Scholarship",
         "Scholarships supporting technical and vocational skills training across Africa.",
         "Full Tuition", "Tuition and materials",
         "All", "Vocational/Technical, Undergraduate", "Engineering, Technical Trades", "Africa",
         "Partner Application", "https://www.afdb.org", 0, -15, 45, 1),

        ("Asian Development Bank (ADB)", "ADB-Japan Scholarship Program", "Scholarship",
         "Scholarships for graduate studies in economics, management, science and technology.",
         "Full Tuition + Stipend", "Tuition, living allowance, travel",
         "All", "Master's", "Economics, Management, Science, Technology", "Asia",
         "Official External Application", "https://www.adb.org/site/careers/adb-japan-scholarship-program", 1, -80, -5, 0),

        ("Inter-American Development Bank (IDB)", "IDB Education Fellowship", "Fellowship",
         "Fellowships supporting research and postgraduate study relevant to Latin America and the Caribbean.",
         "Varies", "Stipend and research support",
         "All", "Master's, PhD", "Development Studies, Economics", "Americas",
         "Official External Application", "https://www.iadb.org", 0, -100, -10, 0),

        ("The World Bank / IDA", "World Bank IDA Education Financing Initiative", "Grant",
         "Country-level financing supporting access to quality education in IDA-eligible countries.",
         "Varies", "System-level funding",
         "All", "All", "Education Policy", "Any",
         "Funding Match", "https://ida.worldbank.org", 0, -200, -30, 0),

        ("Global Partnership for Education", "GPE Girls' Education Accelerator", "Grant",
         "Supports initiatives that improve access to and quality of girls' education in partner countries.",
         "Varies", "Program-based funding",
         "All", "Undergraduate, High School", "Education", "Africa, Asia",
         "Funding Match", "https://www.globalpartnership.org", 0, -40, 100, 1),

        ("UNICEF", "UNICEF Education Cannot Wait Emergency Grant", "Grant",
         "Emergency education funding for children affected by crisis and displacement.",
         "Varies", "Emergency education support",
         "All", "All", "All", "Any",
         "Funding Match", "https://www.unicef.org/education", 0, -5, 200, 1),

        ("UNESCO", "UNESCO-Africa Union Continental Education Strategy Grant", "Grant",
         "Grants supporting implementation of continental education strategy initiatives.",
         "Varies", "Program-based funding",
         "All", "All", "Education Policy, Research", "Africa",
         "Funding Match", "https://www.unesco.org", 0, -20, 60, 1),

        ("Education Cannot Wait (ECW)", "ECW First Emergency Response Grant", "Grant",
         "Rapid-response education funding for children in emergencies.",
         "Varies", "Emergency education support",
         "All", "All", "All", "Any",
         "Funding Match", "https://www.educationcannotwait.org", 0, -10, 45, 1),

        ("IEFA", "IEFA Global Scholarship Search Listings", "Scholarship",
         "A curated directory of international scholarships - use this to discover further external funding.",
         "Varies", "Varies by listing",
         "All", "All", "All", "Any",
         "Official External Application", "https://www.iefa.org", 0, -365, 365, 1),

        ("International Scholarships", "International Scholarships Directory Listings", "Scholarship",
         "A directory of scholarships for students studying abroad.",
         "Varies", "Varies by listing",
         "All", "All", "All", "Any",
         "Official External Application", "https://www.internationalscholarships.com", 0, -365, 365, 1),

        ("EducationUSA", "EducationUSA Opportunity Funds Program", "Scholarship",
         "Advising and limited financial assistance to help low-income students apply to US universities.",
         "Partial", "Application and testing fee support",
         "All", "Undergraduate", "All", "United States",
         "Partner Application", "https://educationusa.state.gov", 0, -30, 30, 1),

        ("Aga Khan Foundation International Scholarship Programme", "AKF ISP Postgraduate Award", "Scholarship",
         "Postgraduate award for outstanding students with no other means of financing further studies.",
         "Partial (50/50)", "Tuition support",
         "Kenya, Tanzania, Uganda, All", "Master's", "All", "Any",
         "Partner Application", "https://the.akdn/en/how-we-work/our-agencies/aga-khan-foundation/scholarship-programme", 0, -60, 10, 1),
    ]

    for (org_name, title, funding_type, description, amount, coverage, countries, levels, fields,
         destination, method, url, fully_funded, open_off, close_off, is_open) in opportunities:
        program_id = program_ids[org_ids[org_name]]
        db.execute(
            """INSERT INTO funding_opportunities
               (program_id, title, funding_type, description, amount, coverage, eligible_countries,
                education_levels, fields, study_destination, eligibility_notes, requirements,
                required_documents, open_date, close_date, application_method, application_url,
                fully_funded, verification_status, last_verified_date, is_open, is_demo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Verified', ?, ?, 1)""",
            (program_id, title, funding_type, description, amount, coverage, countries, levels, fields,
             destination, "Please review full eligibility criteria on the official application page.",
             "Academic transcripts, recommendation letter, personal statement (see full requirements on provider site).",
             "Academic Transcripts, Recommendation Letter, Personal Statement, CV",
             d(open_off), d(close_off), method, url, fully_funded, d(-5), is_open),
        )

    # ---------------------------------------------------------------
    # 🏦 Bank / payout requirements - set per-provider, based on how each
    # (demo) provider would realistically disburse funding. Most listings
    # here are directory/informational only (application_method =
    # "Official External Application" or a pure listing service) and
    # never need Africa ScholarBridge to collect bank details at all -
    # only providers whose funding actually gets disbursed THROUGH a
    # match/referral (Partner Application / Funding Match) are flagged.
    # ---------------------------------------------------------------
    bank_requirements = [
        ("Mastercard Foundation Scholars Program", "TRUE", "Bank transfer", "USD", 0, 1),
        ("AKF ISP Postgraduate Award", "PROVIDER_SPECIFIC", "Bank transfer", "USD", 0, 0),
        ("AfDB Skills for Employability Scholarship", "TRUE", "Bank transfer or Mobile Money", "KES", 1, 0),
        ("Gates Foundation Education Grant", "PROVIDER_SPECIFIC", "Bank transfer", "USD", 0, 1),
    ]
    for title, required, method, currency, mm, intl in bank_requirements:
        db.execute(
            """UPDATE funding_opportunities
               SET bank_details_required = ?, payment_method = ?, payment_currency = ?,
                   mobile_money_supported = ?, international_transfer_supported = ?
               WHERE title = ?""",
            (required, method, currency, mm, intl, title),
        )

    db.commit()

    # -------------------------------------------------------------
    # 5. Sample students
    # -------------------------------------------------------------
    students_data = [
        # full_name, email, phone, country, citizenship, level, institution, field, year, dob, gender
        ("Amina Yusuf", "amina.yusuf@example.com", "+254700000001", "Kenya", "Kenyan",
         "Undergraduate", "University of Nairobi", "Computer Science", "Year 3", "2003-04-12", "Female"),
        ("Kwame Mensah", "kwame.mensah@example.com", "+233200000002", "Ghana", "Ghanaian",
         "Master's", "University of Ghana", "Public Health", "Year 1", "1998-09-01", "Male"),
        ("Fatima Diallo", "fatima.diallo@example.com", "+221700000003", "Senegal", "Senegalese",
         "PhD", "Cheikh Anta Diop University", "Economics", "Year 2", "1995-01-20", "Female"),
        ("Tendai Moyo", "tendai.moyo@example.com", "+263770000004", "Zimbabwe", "Zimbabwean",
         "Undergraduate", "University of Zimbabwe", "Engineering", "Year 2", "2002-06-15", "Male"),
        ("Grace Achieng", "grace.achieng@example.com", "+254711000005", "Kenya", "Kenyan",
         "Master's", "Kenyatta University", "Education", "Year 1", "1999-11-05", "Female"),
    ]

    student_ids = {}
    for full_name, email, phone, country, citizenship, level, institution, field, year, dob, gender in students_data:
        user_id = db.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'student')",
            (email, generate_password_hash("Student@123")),
        ).lastrowid
        student_id = db.execute(
            """INSERT INTO students
               (user_id, full_name, phone, country, citizenship, education_level, institution,
                field_of_study, year_of_study, date_of_birth, gender)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, full_name, phone, country, citizenship, level, institution, field, year, dob, gender),
        ).lastrowid
        student_ids[full_name] = student_id

    db.commit()

    # -------------------------------------------------------------
    # 6. Sample applications (varying stages of the pipeline)
    # -------------------------------------------------------------
    DOCUMENT_CHECKLIST = [
        ("Academic Transcripts", True), ("Certificates", True), ("Admission Letter", False),
        ("Recommendation Letter", True), ("Personal Statement", True), ("CV", True),
        ("Passport / Identity Document", False), ("Proof of Financial Need", False),
        ("Provider-Specific Document", False),
    ]

    def create_application(student_name, submitted, preferences, statement, funding_type_needed,
                            estimated_need, financial_need_docs_done=0):
        student = db.execute("SELECT * FROM students WHERE id = ?", (student_ids[student_name],)).fetchone()
        user = db.execute("SELECT email FROM users WHERE id = ?", (student["user_id"],)).fetchone()

        # Build the INSERT from a dict so the column list and the value list
        # can never drift out of sync (much safer than counting "?" marks
        # by hand in a long statement).
        values = {
            "student_id": student_ids[student_name],
            "cycle_id": cycle_id,
            "status": "Draft",
            "current_step": 8,
            "full_name": student["full_name"],
            "date_of_birth": student["date_of_birth"],
            "country": student["country"],
            "citizenship": student["citizenship"],
            "phone": student["phone"],
            "email": user["email"],
            "gender": student["gender"],
            "institution": student["institution"],
            "education_level": student["education_level"],
            "course": student["field_of_study"],
            "field_of_study": student["field_of_study"],
            "year_of_study": student["year_of_study"],
            "academic_info": "Consistently strong academic record.",
            "graduation_year": str(TODAY.year + 1),
            "funding_type_needed": funding_type_needed,
            "tuition_need": "Full",
            "accommodation_need": "Partial",
            "living_expenses_need": "Partial",
            "books_need": "Full",
            "transport_need": "Partial",
            "technology_need": "Partial",
            "other_expenses": "",
            "household_situation": "Household income is limited relative to education costs.",
            "source_of_support": "Family and personal savings",
            "estimated_financial_need": estimated_need,
            "funding_already_received": "None",
            "preferences": preferences,
            "personal_statement": statement,
        }
        columns = ", ".join(values.keys())
        placeholders = ", ".join(["?"] * len(values))
        app_id = db.execute(
            f"INSERT INTO funding_applications ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        ).lastrowid

        for doc_type, required in DOCUMENT_CHECKLIST:
            status = "Verified" if financial_need_docs_done >= 2 else ("Uploaded" if financial_need_docs_done == 1 else "Missing")
            db.execute(
                "INSERT INTO documents (application_id, document_type, is_required, status) VALUES (?, ?, ?, ?)",
                (app_id, doc_type, 1 if required else 0, status if required else "Missing"),
            )

        if submitted:
            ref_number_seed = db.execute("SELECT COUNT(*) c FROM funding_applications WHERE reference_number IS NOT NULL").fetchone()["c"]
            ref = f"ASB-{TODAY.year}-{100000 + ref_number_seed + 1:06d}"
            db.execute(
                "UPDATE funding_applications SET status = 'Submitted', reference_number = ?, submitted_at = ? WHERE id = ?",
                (ref, d(-14), app_id),
            )
            db.execute(
                "INSERT INTO application_history (application_id, status, note, created_at) VALUES (?, 'Submitted', 'Application submitted.', ?)",
                (app_id, d(-14)),
            )
            db.execute(
                "INSERT INTO notifications (student_id, message) VALUES (?, ?)",
                (student_ids[student_name], f"🎓 Your application {ref} has been received."),
            )

        db.commit()
        return app_id

    # Amina - fully submitted, matched, referred, and funded (showcases full pipeline)
    amina_app = create_application(
        "Amina Yusuf", True,
        "Undergraduate, Scholarship, Local Study, International Study",
        "I am passionate about using computer science to solve challenges in my community. Funding this degree "
        "will allow me to focus fully on my studies and eventually build technology solutions for East Africa.",
        "Full tuition and accommodation", "USD 4,000 / year", financial_need_docs_done=2,
    )
    db.execute("UPDATE funding_applications SET status = 'Funding Matching' WHERE id = ?", (amina_app,))
    db.commit()
    run_matching_for_application(db, amina_app)
    top_match = db.execute(
        "SELECT * FROM funding_matches WHERE application_id = ? ORDER BY score DESC LIMIT 1", (amina_app,)
    ).fetchone()
    db.execute("UPDATE funding_applications SET status = 'Matched' WHERE id = ?", (amina_app,))
    db.execute(
        "INSERT INTO notifications (student_id, message) VALUES (?, '🎯 You have received new funding matches.')",
        (student_ids["Amina Yusuf"],),
    )
    if top_match:
        referral_id = db.execute(
            "INSERT INTO provider_referrals (match_id, status, referred_at) VALUES (?, 'Provider Review', ?)",
            (top_match["id"], d(-7)),
        ).lastrowid
        db.execute("UPDATE funding_applications SET status = 'Funded' WHERE id = ?", (amina_app,))
        db.execute(
            """INSERT INTO funding_decisions (referral_id, decision, funding_amount, coverage, decision_date, provider_reference, notes)
               VALUES (?, 'Funded', 'USD 4,000/year', 'Tuition and accommodation', ?, 'REF-DEMO-001', 'Awarded following committee review.')""",
            (referral_id, d(-1)),
        )
        db.execute(
            "INSERT INTO notifications (student_id, message) VALUES (?, '📢 A funding decision has been recorded: Funded')",
            (student_ids["Amina Yusuf"],),
        )
        db.execute(
            "INSERT INTO application_history (application_id, status, note, created_at) VALUES (?, 'Funded', 'Funding decision recorded by provider.', ?)",
            (amina_app, d(-1)),
        )
    db.commit()

    # Kwame - submitted and matched, referral in progress
    kwame_app = create_application(
        "Kwame Mensah", True,
        "Master's, Fellowship, Scholarship, International Study",
        "My goal is to strengthen public health systems in West Africa through evidence-based policy work. "
        "This fellowship would allow me to pursue advanced training I could not otherwise afford.",
        "Full tuition and stipend", "USD 6,000 / year", financial_need_docs_done=1,
    )
    run_matching_for_application(db, kwame_app)
    db.execute("UPDATE funding_applications SET status = 'Matched' WHERE id = ?", (kwame_app,))
    match = db.execute("SELECT * FROM funding_matches WHERE application_id = ? ORDER BY score DESC LIMIT 1", (kwame_app,)).fetchone()
    if match:
        db.execute("INSERT INTO provider_referrals (match_id, status) VALUES (?, 'Application Started')", (match["id"],))
        db.execute("UPDATE funding_applications SET status = 'Provider Referral' WHERE id = ?", (kwame_app,))
    db.commit()

    # Fatima - submitted, still in eligibility review (early stage)
    fatima_app = create_application(
        "Fatima Diallo", True,
        "PhD, Research, Fellowship",
        "My doctoral research focuses on regional trade policy in West Africa. Funding support would let me "
        "complete fieldwork and dedicate myself fully to this research.",
        "Research funding and stipend", "USD 8,000 / year", financial_need_docs_done=0,
    )
    db.execute("UPDATE funding_applications SET status = 'Eligibility Review' WHERE id = ?", (fatima_app,))
    db.execute(
        "INSERT INTO notifications (student_id, message) VALUES (?, '🔎 Your application is currently under eligibility review.')",
        (student_ids["Fatima Diallo"],),
    )
    db.commit()

    # Tendai - draft only (not yet submitted), demonstrates in-progress application
    create_application(
        "Tendai Moyo", False,
        "Undergraduate, Scholarship, Local Study",
        "", "Tuition support", "USD 2,500 / year", financial_need_docs_done=0,
    )

    # Grace - no application started yet (demonstrates a fresh student account)
    # (intentionally left without an application)

    db.commit()

    # -------------------------------------------------------------
    # 🇺🇸 U.S. Student Visa Assistance - demo data
    # -------------------------------------------------------------
    visa_pricing_seed = [
        ("Kenya", "KES", "KSh", 1500, "~US$11.59 equivalent - fixed local price"),
        ("Nigeria", "NGN", "₦", 45000, "~US$30 equivalent, review periodically"),
        ("Ghana", "GHS", "GH₵", 220, "~US$15 equivalent, review periodically"),
        ("Uganda", "UGX", "USh", 55000, "~US$15 equivalent, review periodically"),
        ("Tanzania", "TZS", "TSh", 38000, "~US$15 equivalent, review periodically"),
        ("South Africa", "ZAR", "R", 280, "~US$15 equivalent, review periodically"),
        ("Ethiopia", "ETB", "Br", 1900, "~US$15 equivalent, review periodically"),
        ("Rwanda", "RWF", "FRw", 20000, "~US$15 equivalent, review periodically"),
    ]
    for country, currency, symbol, price, ref in visa_pricing_seed:
        db.execute(
            """INSERT INTO visa_pricing (country, currency, currency_symbol, service_price, exchange_rate_reference)
               VALUES (?, ?, ?, ?, ?)""",
            (country, currency, symbol, price, ref),
        )

    db.execute(
        """INSERT INTO visa_services (name, description, base_price, default_currency, processing_days)
           VALUES (?, ?, ?, ?, ?)""",
        ("U.S. Student Visa Application Assistance",
         "Practical guidance and application assistance for F-1/M-1/J-1 student visa applicants.",
         1500, "KES", 14),
    )
    db.commit()

    def create_visa_request(student_name, payment_status, application_status, extra=None, days_ago_paid=0, days_ago_submitted=None):
        extra = extra or {}
        student = db.execute("SELECT * FROM students WHERE id = ?", (student_ids[student_name],)).fetchone()
        user = db.execute("SELECT email FROM users WHERE id = ?", (student["user_id"],)).fetchone()
        pricing = db.execute("SELECT * FROM visa_pricing WHERE country = ?", (student["country"] or "Kenya",)).fetchone() \
            or db.execute("SELECT * FROM visa_pricing WHERE country = 'Kenya'").fetchone()
        ref = visa_lib.generate_visa_request_number(db, TODAY.year)
        payment_verified = 1 if payment_status == "paid" else 0
        cur = db.execute(
            """INSERT INTO visa_requests
               (request_number, student_id, country, currency, currency_symbol, service_price,
                full_name, email, phone, citizenship, country_of_residence,
                payment_status, payment_verified, application_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ref, student["id"], pricing["country"], pricing["currency"], pricing["currency_symbol"],
             pricing["service_price"], student["full_name"], user["email"], student["phone"],
             student["citizenship"], student["country"], payment_status, payment_verified, application_status),
        )
        request_id = cur.lastrowid
        if extra:
            set_clause = ", ".join(f"{k} = ?" for k in extra)
            db.execute(f"UPDATE visa_requests SET {set_clause} WHERE id = ?", (*extra.values(), request_id))
        for doc_type, required in visa_lib.VISA_DOCUMENT_CHECKLIST:
            db.execute(
                "INSERT INTO visa_documents (request_id, document_type, is_required) VALUES (?, ?, ?)",
                (request_id, doc_type, 1 if required else 0),
            )
        visa_lib.add_visa_history(db, request_id, "payment_required", "Visa assistance request created.")
        if payment_status == "paid":
            visa_lib.add_visa_history(db, request_id, "application_unlocked", "Demo seed: payment verified.")
        if days_ago_submitted is not None:
            start = datetime.utcnow() - timedelta(days=days_ago_submitted)
            completion = start + timedelta(days=14)
            db.execute(
                "UPDATE visa_requests SET processing_start_date = ?, application_submitted_at = ?, "
                "estimated_completion_date = ? WHERE id = ?",
                (start.isoformat(timespec="seconds"), start.isoformat(timespec="seconds"),
                 completion.isoformat(timespec="seconds"), request_id),
            )
            visa_lib.add_visa_history(db, request_id, application_status, "Demo seed: application submitted.")
        db.commit()
        return request_id

    # Amina - paid, mid-way through the 14-day processing timeline, and
    # Africa ScholarBridge is actively processing the US$185 fee coverage.
    amina_visa = create_visa_request(
        "Amina Yusuf", "paid", "fee_coverage_processing",
        extra={
            "visa_category": "F-1", "application_type": "New Application",
            "us_institution": "Boston University", "program": "MSc Data Science", "degree": "Master's",
            "admission_status": "Admitted", "i20_status": "I-20 received",
            "ds160_status": "Submitted", "funding_sources": "Scholarship, Personal Funds",
            "assistance_required": "DS-160 Guidance, Interview Preparation, Document Preparation",
            "current_step": 7, "visa_fee_coverage_status": "PROCESSING",
            "submission_status": "complete", "automatically_approved": 1,
        },
        days_ago_submitted=6,
    )
    visa_lib.add_visa_note(db, amina_visa, "We've reviewed your DS-160 draft - looks good. Now processing your US$185 visa fee coverage.", visible_to_student=True)
    db.execute(
        "INSERT INTO visa_fee_transactions (request_id, amount, currency, status, notes) VALUES (?, 185, 'USD', 'PROCESSING', 'Demo seed: coverage being arranged.')",
        (amina_visa,),
    )

    # Kwame - just paid, application unlocked and eligible for fee
    # coverage, hasn't started the steps yet.
    create_visa_request(
        "Kwame Mensah", "paid", "application_unlocked",
        extra={"us_institution": "", "current_step": 1, "visa_fee_coverage_status": "ELIGIBLE"},
    )

    # Tendai - payment submitted, awaiting verification (demonstrates the locked state)
    create_visa_request("Tendai Moyo", "pending_verification", "payment_required")
    db.execute(
        """INSERT INTO visa_payments (request_id, student_id, amount, currency, payment_method, transaction_reference, status)
           SELECT id, student_id, service_price, currency, 'M-Pesa / Bank Transfer (Manual)', 'DEMO-REF-88213', 'pending'
           FROM visa_requests WHERE student_id = ?""",
        (student_ids["Tendai Moyo"],),
    )

    # =================================================================
    # 🏦 AFRICAN BANK DIRECTORY & FUNDING PAYOUT INFORMATION
    # =================================================================

    # All 54 internationally recognized African countries.
    for name, iso, currency in banks_lib.AFRICAN_COUNTRIES:
        db.execute(
            "INSERT INTO countries (name, iso_code, currency, status) VALUES (?, ?, ?, 'Active')",
            (name, iso, currency),
        )
    db.commit()

    # ---------------------------------------------------------------
    # KENYA - drawn directly from the Central Bank of Kenya's own
    # published "Directory of Licensed Commercial Banks and Authorised
    # Non-Operating Holding Companies". Only bank NAMES come from that
    # official directory - CBK's directory does not publish bank codes
    # or SWIFT/BIC codes, and this app never fabricates those (per the
    # explicit no-fabrication requirement - see README section 21).
    # bank_code/swift_bic are left blank here; a Main Admin can fill
    # them in later via /admin/banks once confirmed from each bank's own
    # official materials. Re-verify this list periodically - bank
    # mergers, receiverships, and rebrands happen (e.g. Access Bank
    # Kenya's 2024 acquisition of Sidian Bank), so "Licensed" here means
    # "licensed as of last_verified", not "licensed forever".
    # ---------------------------------------------------------------
    kenya_verified_date = date.today().isoformat()
    kenya_banks = banks_lib.KENYA_CBK_LICENSED_BANKS
    for bank_name in kenya_banks:
        db.execute(
            """INSERT INTO banks (bank_name, country, bank_code, swift_bic, status, last_verified)
               VALUES (?, 'Kenya', NULL, NULL, 'Licensed', ?)""",
            (bank_name, kenya_verified_date),
        )

    # No other country's bank directory is pre-populated: this app does
    # not fabricate bank names, codes, or licensing status for a
    # regulator it hasn't actually checked. A student in any other
    # country simply sees "no banks listed yet" and uses "My bank is not
    # listed" (manual entry -> MANUAL_REVIEW) until a Main Admin adds and
    # verifies that country's banks via /admin/banks against its own
    # official regulator.

    db.commit()
    db.close()

    print("Database seeded successfully!")
    print(f" - Admin account: {admin_email} (Main Admin + Visa Admin roles)")
    print("   ⚠️ Set its password now with:  python create_admin.py")
    print(" - Demo student login: amina.yusuf@example.com / Student@123 (or any other seeded student email)")
    print(" - Demo visa journeys: Amina Yusuf (paid, fee coverage processing), Kwame Mensah (paid, eligible, not started),")
    print("   Tendai Moyo (payment pending admin verification)")


if __name__ == "__main__":
    seed()
