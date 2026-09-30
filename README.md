# Africa ScholarBridge

**One Application. Multiple Funding Opportunities. One Annual Cycle.**
**Plus: 🇺🇸 U.S. Student Visa Application Assistance**

Africa ScholarBridge helps African students discover, prepare for, and connect with legitimate education funding opportunities through one centralized annual funding application, a transparent matching engine, and a full application tracker. It also offers a separate, optional, paid **U.S. Student Visa Application Assistance** service on the same student account, gated behind a server-side **Pay → Verify → Unlock → Continue Application** flow with an automatic up-to-14-day processing tracker.

This is a complete, working web application built with **Python, Flask, and SQLite** - kept intentionally simple so a beginner can read every file and keep building on it.

> **What's new in this update:** the entire 🇺🇸 U.S. Student Visa Application Assistance module (payment-gated multi-step application, admin payment verification, 14-day processing tracker, per-country pricing, document checklist, interview prep, and official resources) was added on top of the existing, already-working funding platform. See section 12 below for exactly what changed.

---

## 1. Requirements

- Python 3.9 or newer
- pip (comes with Python)

No other services (no separate database server, no Node.js) are required - SQLite is just a file.

---

## 2. Installation (Windows Command Prompt)

> **Build:** M-PESA Visa Workflow v3. After starting, open `http://127.0.0.1:5000/version`.
> It must say `Version: M-PESA Visa Workflow v3`. If it doesn't, you are running an older copy.

The ZIP `africa-scholarbridge-mpesa-v3.zip` has the project files at its top level. Right-click
it → **Extract All…** → keep the suggested folder → **Extract**. You get:

```
C:\Users\<you>\Downloads\africa-scholarbridge-mpesa-v3\
    app.py   <- the ONLY entry point
    database.py  mpesa_parser.py  visa.py  ...
    database\scholarbridge.db   <- the ONLY database (already contains demo data)
    templates\   static\   uploads\
```

```cmd
cd %USERPROFILE%\Downloads\africa-scholarbridge-mpesa-v3
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python create_admin.py
python app.py
```

`python create_admin.py` is needed once, to set the administrator password (hidden prompt, 12+
characters with upper/lower case, a number and a symbol). Then log in at
`http://127.0.0.1:5000/admin/login` with `africascholarbridge@gmail.com`.

When it starts, the window prints a banner with the build name, the project folder and the database
path. Check the folder is `...\africa-scholarbridge-mpesa-v3`. If another copy of the site (an
older version) is still running in a different Command Prompt window, `app.py` now refuses to start
and tells you to close it first.

Do **not** run `python seed_data.py` unless you want to wipe the database and reload demo data. The
included database already has the demo data, and new columns are added automatically at startup.

## 3. Demo Accounts

After running `seed_data.py` you can log in immediately with:

**Administrator account: `africascholarbridge@gmail.com`** (a real database account)
- There is **no default admin password**. Set it once with the setup command (the password is typed
  at a hidden prompt and stored only as a salted hash):
  ```cmd
  python create_admin.py
  ```
  Run it again at any time to reset the password.
- It creates or updates two rows in the `users` table for this email: `role='admin'` (+ an `admins`
  profile row) and `role='visa_admin'` (+ a `visa_admins` profile row).
- Log in at **`/admin/login`**. Because the account holds both roles with the same password, that one
  login opens the Admin Dashboard **and** the Visa Admin portal (Visa Payments, M-PESA verification,
  screenshots, submitted messages). `/visa-admin/login` also still works on its own.
- Students register only as `role='student'`; there is no way to self-register as an admin.

**Student accounts** (password for all: `Student@123`)
- `amina.yusuf@example.com` - full journey: submitted, matched, referred, **funded**
- `kwame.mensah@example.com` - matched, provider referral in progress
- `fatima.diallo@example.com` - submitted, in eligibility review
- `tendai.moyo@example.com` - application in **draft** (not yet submitted)
- `grace.achieng@example.com` - fresh account, no application started

**⚠️ Before deploying anywhere real:** set the admin password with `python create_admin.py` (never put it in a file), change or delete the demo student accounts, and set a real `SECRET_KEY` environment variable (otherwise a random key is generated once and kept in `instance/secret_key`). Never commit real credentials to source control.

**💳 Which account to use to SEE the new M-PESA payment page:**
- `fatima.diallo@example.com` or `grace.achieng@example.com`: **no visa request yet**. Go to
  🇺🇸 Student Visa → **Pay & Start Application**. This shows the new page, with the SMS paste box,
  screenshot upload and Detected Payment Details.
- `tendai.moyo@example.com`: an older **pending** payment, so the page shows *Pending Verification*.
- `amina.yusuf@example.com` / `kwame.mensah@example.com`: **already paid and verified**, so they go
  straight to their visa dashboard and never see the payment form.
- Admin side: log in at `/visa-admin/login` → **Visa Payments**. The Main Admin dashboard
  (`/admin`) now also shows a **💳 Visa Payments** card and nav link that open it.

**🇺🇸 Demo visa assistance journeys** (log in as the student accounts above):
- `amina.yusuf@example.com` - **paid**; her US$185 visa fee coverage is **PROCESSING**, and her submission is already marked **COMPLETE** (auto) - see section 15 to record Africa ScholarBridge's payment of it as Visa Admin
- `kwame.mensah@example.com` - **paid**, application unlocked and **ELIGIBLE** for fee coverage, but not yet started
- `tendai.moyo@example.com` - payment **submitted, awaiting Visa Admin verification** (still locked) - log in as Visa Admin at `/visa-admin/requests` to confirm it
- Any other student - can start a brand-new visa request from `/student-visa`

---

## 4. Project Structure

```
africa-scholarbridge/
│
├── app.py              # All routes/pages - the heart of the application
├── database.py         # Creates the SQLite tables (schema)
├── matching.py         # The funding-matching engine (scoring logic)
├── visa.py              # 🇺🇸 Visa assistance helpers: pricing, reference numbers, the 14-day tracker, and the payment gate
├── seed_data.py        # Fills the database with realistic demo data
├── requirements.txt    # Python packages this project needs
│
├── database/
│   └── scholarbridge.db   # The actual SQLite database file (created automatically)
│
├── templates/           # HTML pages (Jinja2 templates)
│   ├── base.html         # Shared layout: navbar, footer, flash messages
│   ├── index.html, login.html, register.html, dashboard.html, ...
│   ├── student_visa/      # 🇺🇸 Visa landing, payment, application steps, dashboard, documents, interview, resources
│   ├── admin/             # Main Admin-only pages (funding platform - NO visa management, see section 15)
│   └── visa_admin/        # 🇺🇸 Visa Admin-only pages - a completely separate portal (see section 15)
│
└── static/
    ├── css/style.css     # All styling (brand colors, cards, tracker, visa promo/fee boxes, etc.)
    ├── js/app.js          # Small JS helpers (alert auto-dismiss, confirmations)
    └── images/            # (empty - add logos/photos here if you want)
```

### How the pieces fit together

1. **`database.py`** defines every table (students, applications, opportunities, matches, etc.) and how they relate to each other.
2. **`app.py`** is a single Flask app with every route. It's kept as one file (rather than split into many "blueprint" files) so it's easy to search and follow as a beginner. As the project grows you can split it into blueprints per section (auth, student, admin) if you want.
3. **`matching.py`** contains the rule-based matching engine - it compares a student's application against each open funding opportunity and produces a 0-100 score with plain-English reasons.
4. **Templates** all extend `base.html`, which holds the navbar and footer so you never repeat that markup.

---

## 5. How to Create an Admin Account

The easiest way is through the seed script (already done for you). To add another admin manually, run this in a Python shell inside the project folder:

```python
from database import get_db
from werkzeug.security import generate_password_hash

db = get_db()
user_id = db.execute(
    "INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'admin')",
    ("newadmin@example.com", generate_password_hash("ChooseAStrongPassword!")),
).lastrowid
db.execute("INSERT INTO admins (user_id, full_name) VALUES (?, ?)", (user_id, "New Admin"))
db.commit()
```

To create a **Visa Admin** account instead (a completely separate role - see section 15), do the same but with `role = 'visa_admin'` and insert into `visa_admins` instead of `admins`:

```python
user_id = db.execute(
    "INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'visa_admin')",
    ("newvisaadmin@example.com", generate_password_hash("ChooseAStrongPassword!")),
).lastrowid
db.execute("INSERT INTO visa_admins (user_id, full_name) VALUES (?, ?)", (user_id, "New Visa Admin"))
db.commit()
```

---

## 6. How to Add Funding Opportunities

Two ways:

1. **Through the app** - log in as admin, go to **Admin Dashboard → Opportunities**, and use the "Add Opportunity" form. You'll need to pick a Program first (add one under **Organizations** if needed).
2. **Directly in the database** - insert a row into `funding_opportunities` following the columns in `database.py`. Useful for bulk-loading real opportunities later.

Every opportunity is tied to a `funding_program`, which is tied to an `organization` - this three-level structure (`Organization → Program → Opportunity`) keeps things scalable as you add more funding sources.

---

## 7. How to Create a New Annual Cycle

Log in as admin and go to **Admin Dashboard → Cycles**. Fill in the "Create New Cycle" form (e.g. name `2027/2028 Funding Cycle`, year `2027/2028`, and the relevant dates), then click **Make Current & Open** to switch students over to it.

When a new cycle becomes current, students who log in and start a new application will have their previous cycle's answers pre-filled automatically (see `get_or_create_draft_application` in `app.py`), so they only need to review and update, not start from scratch.

---

## 8. How to Change the Database

The database is a single file: `database/scholarbridge.db`. To make schema changes:

1. Edit the `CREATE TABLE` statements in `database.py`.
2. Because SQLite won't automatically add new columns to an existing file, either:
   - Delete `database/scholarbridge.db` and re-run `python database.py` (and `python seed_data.py` if you want fresh demo data), **or**
   - Write a small migration script that runs `ALTER TABLE ... ADD COLUMN ...` for production data you want to keep.

---

## 9. Deploying Later

This project runs on Flask's built-in development server, which is fine for learning and demos but **not for production**. When you're ready to deploy:

1. Set a real secret key as an environment variable instead of the placeholder in `app.py`:
   ```bash
   export SECRET_KEY="a-long-random-string"
   ```
2. Run behind a production WSGI server, e.g. [Gunicorn](https://gunicorn.org/):
   ```bash
   pip install gunicorn
   gunicorn app:app
   ```
3. Put a reverse proxy (e.g. Nginx) in front of it for HTTPS.
4. Consider moving from SQLite to PostgreSQL if you expect many simultaneous writers - the schema in `database.py` translates fairly directly.
5. Change or remove the demo admin account and demo data before going live.

---

## 10. Testing the Main User Journey Yourself

1. Open the homepage and click **Start My Annual Application** (or register a new account).
2. Complete the 8-step application form and submit it - note your generated `ASB-YYYY-NNNNNN` reference number.
3. Visit **Funding Matches** to see your automatically generated matches with scores and reasons.
4. Click **Proceed / Refer Me** on a match to create a provider referral.
5. Visit **Application Tracker** to see your progress visually.
6. Log out, then log in as the admin (`/admin/login`) to see the application, review it, update its status, and record a funding decision under **Admin → Decisions**.
7. Log back in as the student to see the updated status and notification on your dashboard.

---

## 11. Testing the U.S. Student Visa Assistance Journey Yourself

1. Log in as any student and go to **🇺🇸 Student Visa** (or `/student-visa`), then click **Get Visa Assistance**.
2. You'll land on the payment page. Try visiting `/student-visa/application/<id>` directly at this point - notice you're bounced straight back to payment. This is a **server-side** check (`visa.is_unlocked()` in `visa.py`), not something JavaScript or the URL can get around.
3. Enter any transaction reference and click **Submit Payment for Verification** - the request moves to "pending verification" but is *still locked* (visit the application URL again to confirm).
4. Either:
   - Click **Simulate Payment Confirmation (Demo)** on the payment page (only shown when `DEMO_MODE=1`, the default), or
   - Log in as admin (`/admin/login` → **🇺🇸 Visa Requests** → open the request → **Confirm Payment & Unlock Application**).
5. You'll see "✅ Payment Successful!" and the 7-step visa application unlocks. Complete all 7 steps and submit.
6. Visit **My Visa Request** (`/student-visa/dashboard`) to see the automatic 14-day processing tracker ("Day X of 14", days remaining, estimated completion date, Week 1/Week 2 labels).
7. As admin, open the request under **🇺🇸 Visa Requests** to update its status (e.g. to "Document Review" or "Completed"), request additional information from the student, or leave a note/message - the student sees these on their dashboard.
8. Still as admin on that same request page, scroll to **🇺🇸 U.S. Visa Fee Coverage**: the status should already show **ELIGIBLE** (it's set automatically the moment payment was verified). Fill in the "Record the US$185 Payment" form and submit - the status moves to **Paid by Africa ScholarBridge**, a ledger entry is created, and the student sees "✓ COVERED" on their dashboard. Move it to **Confirmed** once you're satisfied.
9. Visit **Admin → Visa Financials** to see Africa ScholarBridge's revenue (money in) and visa-fee expenditure (money out) tracked as two separate totals, plus the net margin.

---

## 12. 🇺🇸 U.S. Student Visa Assistance Module - What Was Added

The existing Africa ScholarBridge funding platform (registration, annual applications, matching engine, tracker, admin tools) was already complete and working. This update adds a fully separate, fully integrated **U.S. Student Visa Application Assistance** service on the same student account, admin system, and database, without touching the funding side's existing behavior.

### The core rule: PAY → VERIFY → UNLOCK → CONTINUE APPLICATION

The multi-step visa application can **never** be reached before payment is verified. This is enforced by a single function, `visa.is_unlocked(visa_request)`, checked at the top of every visa-application route (`student_visa_application`, `student_visa_step`, `student_visa_submit`). It requires **both** `payment_status == 'paid'` **and** `payment_verified == 1` in the database - never a URL parameter, a hidden field, a cookie, or "the student reached a success page." See section 11a above to see this gate in action.

### New files
- `visa.py` - pricing lookup, reference-number generation (`ASB-VISA-YYYY-NNNNNN`), the payment gate (`is_unlocked`), and the 14-day processing tracker calculation.
- `templates/student_visa/` - `landing.html`, `payment.html`, `application.html` (7 steps), `review.html`, `dashboard.html`, `documents.html`, `interview.html`, `resources.html`.
- `templates/admin/visa_requests.html`, `templates/admin/visa_detail.html`, `templates/admin/visa_pricing.html` *(superseded - see section 15: these moved to `templates/visa_admin/*.html` under the separate Visa Admin portal).*

### Database changes (`database.py`)
Six new tables: `visa_services`, `visa_pricing` (per-country service fee, seeded for Kenya, Nigeria, Ghana, Uganda, Tanzania, South Africa, Ethiopia, Rwanda), `visa_requests` (the central record - payment + application status, all 7 steps' answers), `visa_payments` (every payment attempt), `visa_notes` (admin notes/messages to the student), `visa_documents` (checklist), and `visa_status_history` (audit trail).

### New routes (`app.py`)
Student-facing: `/student-visa`, `/student-visa/start`, `/student-visa/payment/<id>`, `/student-visa/payment/<id>/submit`, `/student-visa/payment/<id>/demo payment`, `/student-visa/application/<id>`, `/student-visa/application/<id>/step/<step_name>`, `/student-visa/application/<id>/submit`, `/student-visa/dashboard`, `/student-visa/documents`, `/student-visa/interview`, `/student-visa/resources`.
Admin-facing (superseded - see section 15): these routes now live under the separate Visa Admin portal as `/visa-admin/requests` (list + stats), `/visa-admin/requests/<id>` (confirm payment, update status, request information, add notes), `/visa-admin/settings` (manage per-country fees), `/visa-admin/settings/pricing/<id>/toggle`.

### Fee separation
The Africa ScholarBridge service fee (admin-configurable per country, e.g. **KSh 1,500** for Kenya) and the **U.S. government's own visa application fee** (`US_GOV_VISA_FEE_USD = 185` in `visa.py`, shown for information only) are kept completely separate everywhere - the payment page, the dashboard, the FAQ, and the Terms of Use. Africa ScholarBridge never collects, holds, or refunds the U.S. government fee.

### Payment method (demo-appropriate, still server-verified)
Rather than wiring in a real payment gateway (which needs live credentials this project doesn't have), the student submits a manual payment reference (e.g. from M-Pesa or a bank transfer made outside the platform), and the **Visa Admin confirms it** from `/visa-admin/requests/<id>` (see section 15) - exactly the "confirm payment for supported manual payment methods" capability the platform calls for. A `DEMO_MODE` environment variable (**on by default**, set `DEMO_MODE=0` to turn off) additionally exposes a "Simulate Payment Confirmation" button so the full journey can be tested solo; both paths call the same single `_verify_visa_payment()` function in `app.py`, so there is exactly one place that ever marks a payment "paid." In a real deployment, wire a real gateway's server-to-server webhook into that same function and turn `DEMO_MODE` off.

### Environment variables
- `SECRET_KEY` - as before.
- `DEMO_MODE` - `1` (default) shows the demo payment-confirmation button described above; set to `0` before connecting a real payment gateway.

### Funding ↔ visa connection
When a student's funding matches include an opportunity with `study_destination = "United States"` (e.g. the seeded Fulbright, AAUW, and EducationUSA opportunities), a "🇺🇸 U.S. Student Visa Assistance" promo card appears on both **Funding Matches** and the main **Dashboard**, linking straight to the visa service - both features share the same student account, login, dashboard, notifications, and document/admin systems as called for.

### What was intentionally kept simple
Documents are "uploaded" the same way the existing funding-application documents are (a simulated upload, no real file storage) - consistent with how the rest of the beginner-friendly project already works. Messaging is implemented as admin notes visible to the student on their visa dashboard rather than a full two-way chat system.

---

## 13. 🇺🇸 U.S. Student Visa FEE SPONSORSHIP Model - What Changed

A later update reworked the visa service's money flow. Previously the platform displayed the US$185 U.S. government fee as something the student pays *directly to the government, separately*. Under the current model:

**STUDENT PAYS US$11.59 / KSh 1,500 → PAYMENT VERIFIED → VISA APPLICATION UNLOCKED → AFRICA SCHOLARBRIDGE COVERS THE US$185 VISA APPLICATION FEE → APPLICATION PROCESS CONTINUES**

The student makes exactly **one** payment to Africa ScholarBridge (the service fee). They never pay Africa ScholarBridge a second amount for the US$185 government fee - after their own payment is verified, Africa ScholarBridge separately arranges and covers that fee for eligible students. This does not buy or guarantee a visa; the U.S. government alone decides eligibility, interview outcomes, and issuance.

> **A note on the business model, not financial advice:** collecting US$11.59 per student while separately paying out US$185 per eligible student is a real net cost per case (see the Financial Dashboard's "Net Service Margin," which will show negative until an operator provides a genuine, sufficient funding source - a grant, a partner organization, a capped sponsorship pool, tighter eligibility, etc.). Nothing in the code verifies that such funding exists; that is a business decision for whoever operates this platform, and the honest, accurate accounting built here (see below) is meant to make that decision visible, not to paper over it.

### `visa_fee_coverage_status` (new)
Every visa request now tracks the US$185 coverage separately from the student's own payment, through these values: `PENDING → ELIGIBLE → PROCESSING → PAID_BY_AFRICA_SCHOLARBRIDGE → CONFIRMED` (plus `NOT_APPLICABLE`). A request becomes `ELIGIBLE` automatically the moment the student's own payment is verified (`visa.mark_fee_eligible()`, called from `_verify_visa_payment()` in `app.py`) - nothing more happens automatically after that; an admin must explicitly record Africa ScholarBridge's own US$185 payment (`visa.record_fee_coverage_payment()`) before the status can reach `PAID_BY_AFRICA_SCHOLARBRIDGE`.

### New database tables/columns
- `visa_requests` gained `visa_fee_amount`, `visa_fee_currency`, `visa_fee_payer`, `visa_fee_coverage_status`, `visa_fee_payment_reference`, `visa_fee_official_reference`, `visa_fee_payment_date`, `visa_fee_receipt_path`, `visa_fee_handled_by`, `visa_fee_notes`, and refund fields (`refund_status`, `refund_reason`, `refund_notes`). `application_status` gained a `fee_coverage_processing` stage.
- New table `visa_fee_transactions` - a ledger of Africa ScholarBridge's own outgoing payments toward the US$185 fee, kept entirely separate from `visa_payments` (money IN from students), so the two flows are never mixed in one place.
- `visa_payments` gained `phone_number`, `checkout_request_id`, `merchant_request_id` for M-Pesa STK Push tracking.

### Admin controls (`/visa-admin/requests/<id>` - see section 15 for the portal split)
Admins can now update the coverage status, record the actual US$185 payment (payment reference, official U.S. government receipt/reference, date, notes, a receipt filename placeholder, and which admin handled it), and manage refund status for the student's *own* service fee (`not_requested → requested → approved → denied → refunded`) - `visa.refund_eligibility()` gives a plain-English, non-binding read on whether a refund looks appropriate given payment status, whether assistance has started, and whether the government fee has already been paid, but the admin makes and records the actual decision.

### Admin Visa Financial Summary (`/visa-admin/reports` - see section 15 for the portal split)
Visible only to admins. Shows, kept deliberately separate: **Students Who Paid** and **Africa ScholarBridge Revenue** (money IN, paid-requests × US$11.59) vs. **Visa Fees Actually Paid** and **Total Visa-Fee Expenditure** (money OUT, from the `visa_fee_transactions` ledger) vs. **Net Service Margin** (revenue − expenditure) - with a visible warning banner whenever that margin goes negative.

### M-Pesa STK Push (`mpesa.py`, new)
A real Safaricom Daraja API client (OAuth token, STK Push initiation, callback parsing) - not a mock, but unconfigured out of the box, since this project ships with no real merchant credentials. Set these environment variables to enable it:
```
ASB_MPESA_RECEIVING_NUMBER
ASB_MPESA_RECEIVING_NUMBER
ASB_MPESA_RECEIVING_NUMBER
ASB_MPESA_RECEIVING_NUMBER
Not used     # a public HTTPS URL pointing at manual payment proof
Not used              # "sandbox" (default) or "production"
```
None of these are ever sent to the browser. The payment page checks `mpesa.is_configured()` and only shows the "Pay with M-Pesa" phone-number form when all five are set; otherwise it shows a note and falls back to the manual-reference / demo payment methods already in place. The route `manual payment proof` is Safaricom's own server-to-server webhook target - it is the **only** thing that can mark an STK Push payment "paid," matched purely by `CheckoutRequestID`, never by anything the student's browser reports.

### Admin account
The admin email is `africascholarbridge@gmail.com` (override with `ADMIN_EMAIL`). There is no default password: `seed_data.py` gives the account a random unknown password, and `python create_admin.py` sets the real one.

---

## 14. Code Style Notes

- Code favors clarity over cleverness - comments explain *why*, not just *what*.
- SQL uses parameterized queries (`?` placeholders) everywhere to prevent SQL injection.
- Passwords are hashed with Werkzeug's `generate_password_hash` / `check_password_hash` - never stored in plain text.
- Sessions store only a `user_id` and `role`; all other data is looked up fresh from the database on each request.

---

## 15. Two Completely Separate Admin Portals: Main Admin vs. Visa Admin

The Main Africa ScholarBridge Admin (funding platform) and the U.S. Student Visa Admin are now **two
independent portals** - different routes, different logins, different sessions, different navigation,
and different permissions. Logging into one never grants access to the other.

### Routes

| | Main Admin | Visa Admin |
|---|---|---|
| Login | `/admin/login` | `/visa-admin/login` |
| Dashboard | `/admin/dashboard` | `/visa-admin/dashboard` |
| Logout | `/admin/logout` | `/visa-admin/logout` |
| Manages | Students, annual applications, funding cycles/opportunities/organizations/providers, matching, decisions, documents, notifications, guides, calendar, content, general settings | Visa requests, payments, submissions, documents, fee coverage, processing, interview-prep reference, notifications, financial reports, visa pricing settings |

The Visa Admin's full route list: `/visa-admin/dashboard`, `/visa-admin/requests`,
`/visa-admin/requests/<id>`, `/visa-admin/payments`, `/visa-admin/submissions`,
`/visa-admin/fee-coverage`, `/visa-admin/processing`, `/visa-admin/documents`,
`/visa-admin/notifications`, `/visa-admin/interview`, `/visa-admin/reports`, `/visa-admin/settings`.

The Main Admin dashboard has **no visa stats or controls** at all - just one small, purely
informational nav link, "🇺🇸 Visa Services", that redirects to `/visa-admin/login` (see
`templates/admin/_nav.html`). It never exposes visa data or actions itself.

### Accounts and roles

`users.role` now allows four values: `student`, `admin` (Main Admin), `visa_admin` (Visa Admin), and
`super_admin` (reserved for future use - not wired into any route yet). A new `visa_admins` profile
table mirrors `admins`, and `users.email` is no longer globally unique by itself - it is unique per
`(email, role)` pair, so the same email address (e.g. `africascholarbridge@gmail.com`) can back a
Main Admin account and a completely separate Visa Admin account at the same time, each with its own
row, its own password hash, and its own primary key.

Admin accounts are created/updated with `python create_admin.py` (see section 3), which sets both
roles for `ADMIN_EMAIL` (default `africascholarbridge@gmail.com`) from a hidden password prompt.
There are no default admin passwords anywhere in the project.

### Sessions

Three independent authentication contexts live in the same Flask session cookie, but never read or
write each other's keys:

- **Student**: `session['role'] = 'student'` + `session['user_id']`
- **Main Admin**: `session['role'] = 'admin'` + `session['user_id']`
- **Visa Admin**: `session['visa_admin_user_id']` only - a dedicated key that never touches
  `session['role']` or `session['user_id']` at all

`current_admin()` / `@admin_required` only ever look at `session['role']`, so a Visa Admin login can
never satisfy them. `current_visa_admin()` / `@visa_admin_required` only ever look at
`session['visa_admin_user_id']`, so a Main Admin (or student) login can never satisfy them either.
`/logout` and `/admin/logout` use targeted `session.pop(...)` calls (not `session.clear()`), so
logging out of one role never disturbs another role's session key if more than one happens to be set
in the same browser (e.g. while testing). This is verified end-to-end by
`test_separation.py` (see section 11-style testing).

### Visa submission auto-completion

When a student submits their visa assistance application through the payment-gated
`/student-visa/application/<id>/submit` route, `visa.mark_submission_complete()` sets
`visa_requests.submission_status = 'complete'` and `automatically_approved = 1`. This is **only**
an internal Africa ScholarBridge bookkeeping state meaning "this student's information and payment
are both in and ready for our team to work" - it is never an official U.S. government visa decision,
and the Visa Admin UI says so everywhere this field appears.

### Database design note

`visa_notes.admin_id`, `visa_fee_transactions.admin_id`, `visa_requests.visa_fee_handled_by`, and
`visa_documents.reviewed_by` all reference `visa_admins(id)`, not `admins(id)` - every record of who
took a visa-related admin action points at the Visa Admin table, consistent with visa case-handling
being exclusively a Visa Admin responsibility.

---

## 16. 🇺🇸 The Visa Question Is Now a Step INSIDE the Annual Funding Application

The U.S. Student Visa Assistance question is no longer something a student has to go find on a
separate page - it's a dedicated step of the annual funding application itself:

```
Personal Info → Education → Funding Need → Financial Info → 🇺🇸 Visa Requirement → Preferences → Statement → Documents → Review → Submit
```

### The flow

> **Note:** the two-path behavior described here for "Yes, I already have it" was superseded by
> section 17 below - a "Yes" answer now requires uploading proof of the visa rather than skipping the
> step outright. Everything else in this section (the payment path, server-side verification, resume
> logic, no-double-charge guarantee) is unchanged.

1. After the first few application steps, the student reaches the **"visa"** step
   (`/application/step/visa`) and is asked: *"Do you already have the required U.S. student visa/
   application arrangements for your intended study?"*
2. **"Yes, I already have it"** → (superseded, see section 17) previously `visa_step_status =
   'NOT_REQUIRED'`, skipping straight to the next step with no verification at all.
3. **"No, I need U.S. Student Visa Assistance"** → they are sent immediately to the existing visa
   payment page (`/student-visa/payment/<id>`) and **cannot continue the funding application past
   this step** until that payment is verified. `visa_step_status = 'ACTION_REQUIRED'` in the
   meantime, and revisiting the step always shows the outstanding payment rather than letting them
   slip past it.
4. Once payment is **server-side verified** (the same `_verify_visa_payment()` used everywhere else -
   never a frontend success message), the student is sent straight back to `/application/step/visa`,
   which now shows **"✅ Visa Assistance Requirement Completed"** and a **"Continue Funding
   Application"** button. `visa_step_status = 'COMPLETE'` and `visa_assistance_approved = 1` - this is
   Africa ScholarBridge's own internal approval of its service, and the page says explicitly that it
   is **not** a U.S. government visa approval, issuance, or guarantee.
5. The student then finishes the remaining funding-application steps and submits normally. They are
   **never charged twice** for the same annual application - `get_or_create_integrated_visa_request()`
   reuses the one `visa_requests` row already linked to that application (`visa_requests
   .annual_application_id`), and revisiting an already-COMPLETE visa step just continues onward.

### What's new in the data model

- `funding_applications` gained: `visa_required`, `visa_assistance_required`, `visa_step_status`
  (`NOT_STARTED` / `NOT_REQUIRED` / `ACTION_REQUIRED` / `COMPLETE`), `visa_payment_status`,
  `visa_payment_amount`, `visa_payment_currency`, `visa_assistance_approved`, `visa_reference`,
  `visa_request_id`. These are a fast, denormalized mirror of the linked `visa_requests` row, kept in
  sync by `_verify_visa_payment()` - the actual payment/processing record of truth is still
  `visa_requests`, managed by the separate Visa Admin portal (section 15).
- `visa_requests` gained `annual_application_id` and `cycle_id`, linking a visa request back to the
  specific annual application that raised it (NULL for a visa request started the old way, directly
  from `/student-visa` - both paths still work side by side).
- A new `visa_admin_notifications` table holds automatic **"🔔 New Visa Assistance Case"** alerts for
  the Visa Admin team (shown on `/visa-admin/notifications` under "New Case Alerts"), fired the moment
  a payment is verified - separate from the `notifications` table, which holds messages sent *to*
  students.

### Dashboard

The student dashboard now shows an **Annual Funding Application Progress** checklist (personal,
education, funding info, 🇺🇸 Visa Assistance, personal statement) whenever there's a Draft
application, with the Visa Assistance row showing `✓ COMPLETE`, `✓ Not Required`, or `⚠ Action
Required` (with a one-click button straight to the payment page) exactly as it appears on the
application-step page itself.

---

## 17. 🇺🇸 The Visa Step Now Has Two Real Paths: Upload Proof, or Pay for Assistance

Building on section 16, the visa step inside the annual funding application no longer lets a student
skip past it just by saying "yes." Instead there are two genuine, fully-verified paths, and completing
**either one** (never both) marks the step complete:

```
                🇺🇸 Do you already have your U.S. student visa?
                        ┌────────────┴────────────┐
                        ↓                          ↓
              YES — I HAVE MY VISA        NO — I NEED VISA ASSISTANCE
                        ↓                          ↓
        📄 Upload Your U.S. Student Visa    Pay US$11.59 / KSh 1,500
                        ↓                          ↓
              Upload verified & saved       Payment server-verified
                        ↓                          ↓
                        └────────────┬─────────────┘
                                     ↓
                        visa_step_status = COMPLETE
                                     ↓
                        CONTINUE APPLICATION (resumes at the
                        next incomplete section - never restarts,
                        never re-charges, never re-asks)
```

### Path A — "YES — I HAVE MY VISA" → upload proof

- Choosing **Yes** sets `visa_status = 'HAS_VISA'` and `visa_step_status = 'ACTION_REQUIRED'`, then
  shows **"📄 Upload Your U.S. Student Visa"** with the message: *"Please upload a clear copy of your
  U.S. student visa so Africa ScholarBridge can record it as part of your annual funding application."*
- The **student is never sent to the payment page and is never asked for the US$11.59 / KSh 1,500 fee**
  on this path - that fee only exists for students who don't already have a visa.
- The upload form collects: the visa document itself (PDF/JPG/JPEG/PNG, 8 MB max), visa type/category,
  issue date (optional), expiry date (optional), passport number, and optional additional information.
  A JavaScript preview shows the chosen image inline (or the filename for a PDF) before the student
  clicks **"UPLOAD & CONTINUE"**.
- On a valid upload, `application_visa_document_upload()` calls the shared `save_visa_document()`
  helper, then sets `visa_document_status = 'UPLOADED'`, `visa_document_path`,
  `visa_document_original_name`, `visa_document_uploaded_at`, and `visa_step_status = 'COMPLETE'` -
  all in one atomic update. **No `visa_requests` row is ever created and no payment is ever charged**
  for this path.
- The page then shows **"✅ Visa Document Uploaded"** with a **"CONTINUE APPLICATION"** button. Revisiting
  later shows the same completed state - re-uploading is blocked once the step is `COMPLETE`.

### Path B — "NO — I NEED VISA ASSISTANCE" → pay the service fee

- Unchanged from section 16: choosing **No** sets `visa_status = 'NEEDS_ASSISTANCE'`, opens the
  existing Africa ScholarBridge visa-assistance payment page (US$11.59 / KSh 1,500), and the student
  cannot continue past this step until the payment is **server-side verified** by the same
  `_verify_visa_payment()` used everywhere else in the app.
- On verified payment, `_verify_visa_payment()` now also sets `visa_status = 'NEEDS_ASSISTANCE'` and
  `visa_assistance_status = 'COMPLETE'` alongside the existing `visa_payment_status = 'PAID'`,
  `visa_step_status = 'COMPLETE'`, `visa_assistance_approved = 1`, and `visa_reference`.
- The student is returned to `/application/step/visa`, which shows **"✅ Visa Assistance Completed"**
  with the same not-a-U.S.-government-decision disclaimer from section 16, and a **"CONTINUE
  APPLICATION"** button that resumes the funding application from the next incomplete section - never
  restarting, never re-charging.

### Secure visa document storage

Visa documents contain sensitive personal information, so real file storage (the first in this
codebase - the old generic `documents()` page only simulated uploads) was built to a strict spec:

- **Private storage location**: files are saved to `uploads/visa_documents/` at the project root,
  *outside* `static/`, so Flask's static file route can never serve them and there is no public URL.
- **Randomized filenames**: every stored file is named `secrets.token_hex(16) + ".<ext>"` - the
  original filename is kept only as `visa_document_original_name` for display, and is never used to
  build a filesystem path.
- **File-type validation, twice over**: the extension must be one of `pdf`/`jpg`/`jpeg`/`png`, *and*
  the first few bytes of the file must match that type's real file signature (`%PDF`, `\xFF\xD8\xFF`,
  `\x89PNG...`) - a `.pdf` that's actually a text file is rejected even though its extension looks fine.
- **File-size limit**: capped globally at 8 MB via `app.config["MAX_CONTENT_LENGTH"]`, with a dedicated
  `@app.errorhandler(413)` that flashes a friendly message instead of a raw server error.
- **Authentication + authorization on every view**: the student's own copy is served only through
  `/application/visa-document/view` (`@login_required`, and the route re-checks the document belongs to
  *that* student's own application before calling `send_from_directory`) - a different student gets a
  404, not someone else's file. The Visa Admin's equivalent, `/visa-admin/visa-document/<application_id>`,
  is gated by `@visa_admin_required` and is completely separate from the Main Admin, who has no access
  to visa documents at all.
- **No public document URLs, ever** - both view routes stream the file through Flask after their own
  auth check; nothing under `uploads/` is ever reachable directly.

### Visa Admin visibility

The Visa Admin's **Documents** page (`/visa-admin/documents`) now has two sections:

1. **Students Who Already Have a Visa** - every `funding_applications` row with `visa_status =
   'HAS_VISA'`, showing the student, application reference, visa type, upload date, and a **View
   Document** link into the secure Visa Admin document route.
2. **Visa Assistance Documents (paid cases)** - unchanged, the existing standalone `visa_documents`
   checklist tied to a paid `visa_requests` case.

Main Admin continues to have zero visibility into either section - visa documents, uploaded or paid
for, are Visa Admin territory only, exactly as required by the separation in section 15.

### Student dashboard

A new **🇺🇸 VISA STATUS** card appears on the dashboard once either path is complete:

- Uploaded: **"✓ Visa Document Uploaded" / Status: Complete**, with **[VIEW DOCUMENT]** and
  **[CONTINUE APPLICATION]** buttons.
- Paid: **"✓ Visa Assistance Paid" / Payment: KSh 1,500 / Status: Complete**, with a **[CONTINUE
  APPLICATION]** button.

The **Annual Funding Application Progress** checklist's visa row (relabeled **"🇺🇸 Visa Requirement"**)
shows `✓ COMPLETE` the moment *either* path finishes, and the outstanding-action alert box now
distinguishes an unfinished upload ("📄 Please upload your U.S. student visa document") from an
unfinished payment, so the student always knows exactly what's left to do.

### New/changed routes

| Route | Method | Who | Purpose |
|---|---|---|---|
| `/application/step/visa` | GET/POST | Student | Two-path visa question, upload form, payment prompt, or completion message, depending on state |
| `/application/visa-document/upload` | POST | Student | Validates and saves the uploaded visa file (Path A) |
| `/application/visa-document/view` | GET | Student (own document only) | Securely streams the student's own uploaded visa document |
| `/visa-admin/documents` | GET/POST | Visa Admin | Now also lists HAS_VISA upload cases alongside paid-case documents |
| `/visa-admin/visa-document/<application_id>` | GET | Visa Admin | Securely streams a student's uploaded visa document for review |

---

## 18. 🏦 African Bank Directory & Funding Payout Information

The annual funding application can now collect a student's bank/payout details - but **only when a real
funding provider actually needs them**, and never with a claim that Africa ScholarBridge itself sends or
guarantees any money.

### The "bank" step

A new **"bank"** step sits right before Final Review in `APPLICATION_STEPS`. The first time a student
reaches it, `matching.application_needs_bank_details()` previews their application against every open
opportunity (the same scoring function `run_matching_for_application` uses, score >= 50) and checks
whether any of them has `bank_details_required` set to `TRUE` or `PROVIDER_SPECIFIC`:

- **Nothing requires it** → `bank_step_status = 'NOT_REQUIRED'` and the student is sent straight to
  Review - they are never shown a form for information no provider needs.
- **Something does** → `bank_step_status = 'ACTION_REQUIRED'` and the **🏦 Funding Payment Information**
  form appears: *"If your selected funding provider requires bank information for payment or
  reimbursement, provide your bank details below. Requirements vary by funding provider and country."*

The form collects Country (all 54 African countries), a dynamically-loaded Bank dropdown (via
`/api/banks?country=...`, with a live search box and "My bank is not listed" manual fallback), Account
Holder Name, Account Number, Account Type (Savings/Current/Other), optional Branch, Bank Code/SWIFT/BIC,
IBAN/Routing Number for international transfers, and an optional Mobile Money account, entirely separate
from a bank account. Saving moves the student to a **"Confirm Payment Information"** screen (masked
account number, a required "I confirm..." checkbox, **SAVE & CONTINUE**) before `bank_step_status`
becomes `COMPLETE` - exactly mirroring the confirm-then-continue pattern used by the visa document
upload. Revisiting a completed step shows the saved (masked) summary with **Edit** and **Continue**
actions; editing never re-asks anything not being changed, and leaving Account Number blank on an edit
keeps the previously stored value instead of forcing a re-type.

### An unlisted bank is never claimed as verified

If a student's bank isn't in the directory, "My bank is not listed" reveals a free-text field. Whatever
they type is saved with `verification_status = 'MANUAL_REVIEW'` - Africa ScholarBridge never implies that
bank has been checked or is legitimate just because a student typed a name.

### Never a payment promise

Every screen that mentions payment (the bank step, the dashboard, the confirmation screen) uses the same
wording: *"If your funding application is approved by the relevant funding provider, payment will be
handled according to that provider's official disbursement process."* Nothing in this feature claims
Africa ScholarBridge guarantees or directly sends money, and no `PAID` status is ever set automatically
anywhere in the code - only a Main Admin, from `/admin/disbursements`, can mark one `PAID`.

### Data model

- **`countries`** - all 54 internationally recognized African countries (name, ISO code, currency),
  seeded from `banks_lib.AFRICAN_COUNTRIES`.
- **`banks`** - the searchable bank directory (`bank_name`, `country`, `bank_code`, `swift_bic`,
  `website`, `status`, `is_active`, `last_verified`). Kenya is seeded with 38 institutions drawn directly
  from the Central Bank of Kenya's own published "Directory of Licensed Commercial Banks" - **only the
  bank names come from that official source**; CBK's directory does not publish bank codes or SWIFT/BIC
  codes, so this app leaves those blank rather than inventing plausible-looking ones (a Main Admin can
  fill them in later, once confirmed from each bank's own materials, via `/admin/banks`). No other
  country is pre-seeded with banks at all - rather than guess, a student in any other country simply
  sees "no banks listed yet" and uses manual entry until a Main Admin adds and verifies that country's
  real banks against its own regulator. Re-verify Kenya's list periodically too - bank mergers and
  receiverships happen (e.g. Access Bank Kenya's 2024 acquisition of Sidian Bank).
- **`student_bank_details`** - one row per annual application (`UNIQUE(application_id)`): country, the
  selected `bank_id` (or a manually-typed `bank_name`), account holder/number/type, branch, bank
  code/SWIFT, IBAN/routing number, currency, mobile money fields, `verification_status`
  (`DIRECTORY_MATCH` / `MANUAL_REVIEW`), and `confirmed`. `account_number` is stored in full (a
  legitimate provider still needs the real number to pay someone) but **every view in this app masks it**
  via `banks_lib.mask_account_number()` before rendering - the confirmation screen, the dashboard, and
  the admin disbursements page never print the full number, and it is never put in a URL or a flash
  message.
- **`funding_disbursements`** - one row per provider referral once a Funded/Partially Funded decision is
  recorded (auto-created inside `/admin/decisions`), tracking `payment_status` through `NOT_REQUIRED` →
  `NOT_SUBMITTED`/`SUBMITTED` → `VERIFICATION_REQUIRED` → `APPROVED_FOR_PAYMENT` → `PROCESSING` → `PAID`
  (or `FAILED`). The initial status is computed honestly (`NOT_REQUIRED` if the opportunity's
  `bank_details_required = 'FALSE'`, `SUBMITTED` if the student had already confirmed bank details,
  otherwise `NOT_SUBMITTED`) - it is never created as `PAID`.
- **`funding_opportunities`** gained `bank_details_required` (`TRUE` / `FALSE` / `PROVIDER_SPECIFIC`,
  set per provider by the Main Admin in `/admin/opportunities`), `payment_method`, `payment_currency`,
  `mobile_money_supported`, `international_transfer_supported`.
- **`funding_applications`** gained `bank_step_status` (`NOT_STARTED` / `NOT_REQUIRED` /
  `ACTION_REQUIRED` / `COMPLETE`), computed lazily the first time the student reaches the step.

### Security

Bank information is sensitive financial data, handled the same way visa documents are (section 17):
account numbers are masked everywhere they're displayed (`•••• •••• 7890`), never appear in a URL (every
save/remove is a POST, never a query string), and are never printed in a flash message or log line. Only
the owning student (via the normal `@login_required` + ownership-scoped queries) and an authenticated
Main Admin (via `/admin/banks` and `/admin/disbursements`, both `@admin_required`) can see this data -
the Visa Admin, being a completely separate system (section 15), has no route into it at all. Removing
payment information is a real, immediate delete (`DELETE FROM student_bank_details`), not a soft flag.

### Student dashboard

Two new cards appear once relevant:

- **🏦 My Funding Payment Information** - shows the masked account once submitted, with **EDIT PAYMENT
  INFORMATION** and **REMOVE PAYMENT INFORMATION** buttons, or an explanatory note if none is needed/given yet.
- **💰 Funding Payment Status** - once a decision creates a disbursement record: Funding Application
  status, Funding Provider, whether Payment Information has been submitted, and the current Payment
  Status badge.

### Main Admin

- **`/admin/banks`** - add, edit, verify (stamps today's date), activate/deactivate, remove, search, and
  filter-by-country the bank directory. Never reachable from a student session or a Visa Admin session.
- **`/admin/disbursements`** - lists every disbursement case and lets an admin update its payment status,
  transaction reference, and internal notes. A payment is only ever marked `PAID` here, by a human.
- **`/admin/opportunities`** - the "Add Opportunity" form now also sets `bank_details_required`,
  `payment_method`, `payment_currency`, and mobile-money/international-transfer support per provider, and
  the opportunities table shows a 🏦 Bank Details column at a glance.

### New/changed routes

| Route | Method | Who | Purpose |
|---|---|---|---|
| `/application/step/bank` | GET/POST | Student | The two-stage bank/payment information form + confirmation screen (or auto-skip) |
| `/application/bank-details/remove` | POST | Student (own application only) | Deletes saved payment information |
| `/api/banks` | GET | Any logged-in user | JSON bank lookup by country and/or search text, for the dynamic dropdown |
| `/admin/banks` | GET/POST | Main Admin | Bank directory CRUD, verification, activation |
| `/admin/disbursements` | GET/POST | Main Admin | Payout status tracking and updates |

## 19. 🔍 Global Funding Search & 📧 Automatic Submission Confirmation Email

Two independent additions - a global search bar over the funding directory, and an automatic
"your application was submitted" email - neither of which removes or changes any existing funding,
application, visa, payment, student-account, Main Admin, or Visa Admin functionality.

### Global funding search

The home page hero now has a search box (`🔍 Search scholarships, sponsors, grants, bursaries...`) that
submits to **`/funding`** - a second URL for the exact same funding directory that already lived at
`/opportunities` (same view function, same template, same filters; every existing `/opportunities` link
still works unchanged). Typing a term there, or on the `/funding` page's own search box, matches it
case-insensitively and by partial text (SQLite's `LIKE` is case-insensitive for plain ASCII) against
**nine fields at once**: opportunity title, provider/organization name, eligible countries, education
levels, fields of study, funding type, description, eligibility notes, and study destination - so
"computer" finds "Computer Science", "Computer Engineering", "Information Technology", etc. wherever any
of those fields mentions it.

The search term combines with every existing filter (country, study destination, education level, field,
funding type, fully-funded, open/closed) rather than replacing them - a student can search "engineering"
*and* filter to Kenya *and* Undergraduate at the same time. When nothing matches, the page shows the exact
required empty state - *"🔍 No matching opportunities found. Try another keyword or adjust your
filters."* with **Clear Search** and **Browse All Opportunities** buttons - and, per the spec, never
invents or pads the results with opportunities that don't actually match.

Each result card shows opportunity name, provider, country, study level, field, funding type, funding
coverage, deadline, an eligibility summary, verification status, and last-verified date, with **View
Details**, **Official Application** (when a real application URL exists), and, for a logged-in student, a
**☆ Save / ★ Saved** toggle backed by a new `saved_opportunities` table
(`UNIQUE(student_id, opportunity_id)`) - saving requires login and never affects matching or eligibility,
it's purely a bookmark.

### Automatic submission confirmation email

`application_submit()` now sends exactly one confirmation email, and only once the application is
genuinely saved: the email fires *after* the database commit that sets `status = 'Submitted'` and writes
a real `reference_number` - never merely because the student clicked Submit, and never from the GET
confirmation page, so refreshing the success page can never trigger a second send.

- **Recipient:** the email address *registered on the student's account* (`users.email`, the login
  email) - not whatever the student may have separately typed into the application's own "Email" field.
- **Subject (exact):** `Africa ScholarBridge — Application Submitted Successfully`
- **Body:** confirms only that the application was received, with the Application Reference, Application
  Cycle, and Submission Date, and closes with *"Please keep your application reference for your
  records."* It never says anything is funded, approved, or guaranteed - a real funding decision is
  always a separate, later notification, exactly like the existing "no payment promise" rule in section
  18.
- **Status tracking:** `funding_applications` gained `confirmation_email_status`
  (`PENDING` / `SENT` / `FAILED`), `confirmation_email_sent_at`, and `confirmation_email_sent` (0/1,
  the duplicate-send guard). `confirmation_email_sent` only ever becomes 1 after an actual successful
  send - the record set on every attempt is `confirmation_email_status`.
- **If the email fails, the application is never touched.** It stays fully `Submitted` (and still
  proceeds through the normal Funding Matching pipeline to `Matched`/`No Suitable Match`); only
  `confirmation_email_status` becomes `FAILED`. The student sees exactly: *"Your application was
  successfully submitted. We could not deliver the confirmation email at this time. Please check your
  email address or contact support."* - never anything implying the application itself failed.
- **Duplicate prevention:** `send_application_confirmation_email(db, application_id, force=False)` is a
  no-op once `confirmation_email_sent` is already 1, unless called with `force=True` - which only the Main
  Admin's explicit **Resend Confirmation Email** button ever does. Opening an application in the admin
  panel never auto-resends anything.
- **Success page (`/application/confirmation/<id>`):** shows `✅ Application Submitted Successfully`,
  the Application Reference, Submission Date, Application Cycle, and either *"📧 A confirmation email has
  been sent to your registered email address"* or the honest failure notice above - plus a **Go to My
  Dashboard** button.
- **`/student/applications`** (new page, linked from the nav as **My Applications**) lists every annual
  application a student has ever submitted, across cycles, with its reference, cycle, status, submission
  date, and confirmation-email badge (Sent/Pending/Failed), and a link into its details.

### Email configuration - environment variables only, never hard-coded

`email_lib.py` sends plain-text mail over standard `smtplib`/`ssl` - no third-party SDK or Flask
extension required, so `requirements.txt` is unchanged - using only environment variables, exactly
mirroring the existing `mpesa.py` pattern (section 13):

```
MAIL_SERVER            e.g. smtp.gmail.com
MAIL_PORT              e.g. 587
MAIL_USERNAME          the SMTP account username
MAIL_PASSWORD          the SMTP account password / app password
MAIL_DEFAULT_SENDER    the "From" address, e.g. "Africa ScholarBridge <no-reply@africascholarbridge.org>"
MAIL_USE_TLS           optional, "1" (default) or "0"
```

No real credentials ship with this project. `email_lib.is_configured()` returns `False` until all five
required variables are set, and an unconfigured mail service fails exactly like a real SMTP outage would
(`confirmation_email_status = 'FAILED'`) rather than the app pretending success - the same honest-failure
discipline used everywhere else in this codebase. Any real SMTP provider (Gmail SMTP, SendGrid,
Mailgun, Amazon SES SMTP, a self-hosted mail server) works by setting these variables in the deployment
environment - never in source code, never committed to a repository, and never sent to the browser.
Email addresses are validated (`email_lib.is_valid_email()`) before any send is attempted, and every SMTP
error is logged server-side only, in enough detail to debug but never containing credentials or being
shown to the student.

### Main Admin: Email Status & Resend

The Main Admin's existing **Applications** list (`/admin/applications`) gained a **Confirmation Email**
column (Sent/Pending/Failed), and each application's detail page (`/admin/applications/<id>`) gained an
**📧 Email Status** card showing Submission: Successful, Confirmation Email: Sent/Pending/Failed, and a
**Resend Confirmation Email** button - a new `@admin_required` route,
`/admin/applications/<id>/resend-email` (POST), that calls the same
`send_application_confirmation_email(..., force=True)` used internally, so a resend is always the same
code path, just deliberately forced. This lives only in the Main Admin Dashboard - the separate Visa
Admin Dashboard (section 15) has no route into it and no such button, since it doesn't concern a visa
email.

### Security & privacy

Email passwords, SMTP credentials, API keys, and admin credentials are never present in frontend
JavaScript, HTML, templates, or any database record a student can read - they exist only as server-side
environment variables. Confirmation emails include only what the student needs to identify their own
submission (name, reference, cycle, submission date) - no unnecessary personal or financial data, and
never anything from the bank/payment-information feature (section 18).

### New/changed routes

| Route | Method | Who | Purpose |
|---|---|---|---|
| `/funding` | GET | Anyone | Same funding directory as `/opportunities`, with the broadened multi-field search |
| `/opportunities/<id>/save` | POST | Student | Toggles a saved/bookmarked opportunity |
| `/student/applications` | GET | Student | Full history of the student's own annual applications, with confirmation-email status |
| `/admin/applications/<id>/resend-email` | POST | Main Admin | Force-resends the submission confirmation email |

Happy building!


## 20. 💳 Visa Payment: Direct M-PESA + SMS Submission + Manual Admin Verification

> Replaces the earlier "Manual M-PESA payment verification" notes. This project has **no**
> Safaricom Daraja / STK Push integration, and nothing in it confirms a payment with Safaricom.

### The workflow

```
Student pays KSh 1,500 to 0181785792 (normal M-PESA Send Money)
  → student pastes the confirmation SMS and/or uploads a screenshot
  → server parses the SMS and validates it (mpesa_parser.py)
  → PAYMENT_PENDING   (visa application stays LOCKED)
  → Visa Admin pastes the "You have received…" SMS from the receiving phone (optional),
    compares both, and clicks VERIFY PAYMENT or REJECT PAYMENT
  → PAYMENT_VERIFIED  → visa application unlocked
    PAYMENT_REJECTED  → stays locked, student sees the reason and can resubmit
```

**Parsing is NOT proof of payment.** A pasted SMS or screenshot is text/an image supplied by the
student and can be edited. The parser only (a) shows the student what was detected, (b) rejects
obviously wrong submissions and (c) gives the admin a side-by-side comparison. It never sets
`PAYMENT_VERIFIED`. Only `_verify_visa_payment()` in `app.py` does that, and the only caller is
the Visa Admin's **VERIFY PAYMENT** button (which also requires ticking "I have checked the
receiving M-PESA account…").

### Student side (`/student-visa/payment/<id>`)
- Shows **Visa Application Assistance Fee: KSh 1,500** and **M-PESA Number: 0181785792**
  (both from Visa Admin → Settings).
- **Option A:** paste the SMS. As they paste, "Detected Payment Details" fills in. This comes from
  the server-side parser via `POST /student-visa/payment/<id>/parse`, so there is no duplicate
  parsing logic in JavaScript.
- **Option B:** upload a screenshot (JPG/PNG/WEBP, 5 MB). With a screenshot only, the student
  must type the 10-character code (needed to catch duplicates). It is stored as
  `code_source='manual'` and flagged to the admin as *typed, not extracted*.
- When an SMS is pasted, every field is extracted **server-side** from the original message. A
  typed code is ignored, and the original message is stored exactly as pasted.
- **Refused outright:** no transaction code, amount ≠ fee, text that isn't an M-PESA
  confirmation, a "You have received…" message (the wrong side), or a transaction code already
  used by anyone else.
- **Accepted but flagged for the admin:** recipient phone ≠ configured number, recipient
  phone not shown (paybill/till wording), no date/time, screenshot-only, or a code this student
  had rejected before.
- While pending, the student can correct a submission by pasting the original SMS again. This
  replaces the pending submission. After a rejection, a new submission creates a new row, and
  the rejected one is kept for the audit trail.

### Server-side lock
`visa.can_continue_application()` now equals `visa.is_unlocked()`: `payment_status == 'paid'`
**and** `payment_verified = 1`. It is checked in every visa application route
(`/student-visa/application/<id>`, `/step/<name>` GET and POST, `/submit`) and on document
uploads. Typing a URL directly just redirects back to the payment page. Previously, submitting
proof alone let the student start the form. That gap is now closed.

### Admin side (Visa Admin portal)
- **Visa Payments** (`/visa-admin/payments`): all submissions with student name, email and phone,
  application ID, amount, transaction code, extracted recipient, date/time, screenshot,
  submission time, status and flags. You can filter by status. The Visa Admin dashboard shows
  how many are waiting.
- **Payment review** (`/visa-admin/payments/<id>`): the complete original SMS, extracted fields,
  automatic checks, the screenshot, an optional **Incoming M-PESA Confirmation** box (parsed into
  code, sender, phone, amount, date and time), a **Payment Matching Assistance** comparison
  (amount / code / date-time / sender / recipient indicators), admin notes, and the
  **VERIFY PAYMENT** / **REJECT PAYMENT** buttons (rejection needs a reason).
- Only a Visa Admin session can reach these routes. Neither a Main Admin nor a student session
  can.

### The parser (`mpesa_parser.py`)
Pure Python standard library. It looks for the 10-character receipt code next to "Confirmed",
then the amount (`Ksh 1500.00` / `Ksh1,500.00` / `KES 1,500`), then either
`sent to|paid to NAME [PHONE] [for account X] on DATE` or
`received Ksh… from NAME PHONE on DATE`. The balance line is cut off first, so
"New M-PESA balance is Ksh1,854.61" is never mistaken for the amount. There are fallbacks for
unfamiliar wording. Masked phones (`0758***959`) are kept as printed and are only used as a
weak hint.

### Database (`visa_payments`, added by guarded `ALTER TABLE` in `database.py`)
`payment_status` (`PAYMENT_PENDING` / `PAYMENT_VERIFIED` / `PAYMENT_REJECTED`), `student_name`,
`student_email`, `student_phone`, `expected_amount`, `submitted_amount`,
`mpesa_transaction_code`, `code_source`, `submitted_mpesa_message`, `message_direction`,
`extracted_recipient_name`, `extracted_recipient_phone`, `extracted_transaction_date`,
`extracted_transaction_time`, `validation_flags`, `submitted_at`, `verified_at`, `verified_by`,
`verification_method`, `admin_incoming_mpesa_message`, `admin_sender_name`, `admin_sender_phone`,
`admin_received_amount`, `admin_transaction_code`, `admin_received_at`. Existing columns are
reused: `request_id` (→ application ID), `student_id`, `proof_file` (screenshot),
`rejection_reason` and `admin_notes`. A partial unique index `ux_visa_payments_mpesa_code`
guarantees one live payment per transaction code. The migration runs automatically every time
`app.py` starts, so an existing `scholarbridge.db` is upgraded in place.

### Screenshot security
Extension allow-list, browser MIME type check, magic-byte check (a renamed `.exe` is refused),
5 MB limit, and a random 32-hex filename (the student's filename is never used on disk, so there
is no path traversal). Files are stored in `uploads/payment_proofs/`, outside `static/`. They are
served only through `/visa-admin/payment-proof/<id>/file` (Visa Admin only) with `nosniff`,
`no-store` and a restrictive CSP.

### Future: official M-PESA (Paybill/Till + Daraja)
To remove the manual step later:
1. Register a Paybill/Till and a Daraja app; keep keys in environment variables.
2. Add a server-to-server callback route (STK Push result or C2B confirmation). Validate that it
   really comes from Safaricom (IP allow-list and/or query the Transaction Status API), and check
   `ResultCode == 0`, the amount and the receipt number.
3. Look up the `visa_payments` row (e.g. by `checkout_request_id`, a column that already exists)
   and call `_verify_visa_payment(..., verification_method='daraja_callback', admin_id=None)`.

Nothing else changes: the lock, the student pages and the admin screens all read the result of
that one function. Until step 2 exists, the admin click remains the only way a payment is verified.

### Configuration
Environment variables (see `.env.example`): `MPESA_PHONE_NUMBER=0181785792`,
`VISA_APPLICATION_FEE=1500`. These seed a **new** database. After that, the values live in
Visa Admin → Settings, which is what the site uses. The old names `ASB_MPESA_RECEIVING_NUMBER` /
`ASB_VISA_APPLICATION_FEE` still work.

### Also fixed in this update
The uploaded `app.py` was missing nine Visa Admin routes that the nav bar links to (Visa
Payments, Submissions, Documents, Fee Coverage, Processing, Interview Prep, Notifications,
Reports, visa-document view), and the `_verify_visa_payment()` function. As a result, **every**
Visa Admin page returned HTTP 500. All of these are restored, using the existing templates.

---

## 21. 🚀 Deploying on Render (and the TemplateNotFound fix)

### What went wrong
Render reported `TemplateNotFound: index.html`, `errors/404.html` and `errors/500.html` because the
GitHub repository contained `app.py` but **not the `templates/` and `static/` folders**. Flask looks for
pages in `templates/` next to `app.py`. When that folder is missing, every page fails, including the
error pages. (Uploading through GitHub's web page with "choose your files" only uploads individual
files, not folders. That is the most common way these two folders go missing.)

### What was restored / changed (build "M-PESA Visa Workflow v4 (Render)")
- **Restored:** all 66 original templates (`templates/`, `templates/admin/`, `templates/student_visa/`,
  `templates/visa_admin/`, `templates/errors/`) and `static/css/style.css`, `static/js/app.js`, taken from
  the last complete build of this project, not rewritten, so every form field, variable and link matches
  `app.py`.
- **Error pages** (`templates/errors/404.html`, `500.html`, `403.html` + `_standalone.html`) are now
  **standalone**. They don't extend `base.html`, use no database and no external files, and `app.py`
  also has a plain-HTML fallback, so an error page can never itself crash.
- **No CDN:** Bootstrap 5.3.3 and Bootstrap Icons 1.11.3 are served from `static/vendor/` (MIT licences
  included).
- **Render settings:** `DATABASE_PATH` (default `database/scholarbridge.db`) and `UPLOAD_ROOT`
  (default `uploads/`) environment variables; `wsgi.py` for `gunicorn wsgi:app`; `gunicorn` in
  `requirements.txt`; `render.yaml` blueprint.
- **Multi-worker safe:** the database setup is serialised with a lock file, the session key is shared by
  all gunicorn workers (`SECRET_KEY` env var, or a key created once next to the database), and SQLite
  waits instead of failing when busy.
- **Empty production database works:** on first start, the visa price table (Kenya KSh 1,500), visa
  service, the 54 African countries and the CBK list of Kenyan bank names are added if the tables
  are empty. **No demo students or admin passwords are created.**
- **`check_templates.py`:** run it before every deploy (see below).

### Render setup (one time)
1. Push the whole project to GitHub (check that `templates/` and `static/` appear in the repo).
2. Render → **New → Web Service** → pick the repo (or **New → Blueprint** to use `render.yaml`).
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn wsgi:app`
3. **Disks → Add disk**: mount path `/var/data`, 1 GB. (Persistent disks need a paid instance type.
   Without one, the database and uploads are wiped on every deploy.)
4. **Environment variables:**
   `DATABASE_PATH=/var/data/scholarbridge.db`, `UPLOAD_ROOT=/var/data/uploads`,
   `SECRET_KEY=<long random value>` (or "Generate"), `ADMIN_EMAIL=africascholarbridge@gmail.com`,
   `MPESA_PHONE_NUMBER=0181785792`, `VISA_APPLICATION_FEE=1500`, `FLASK_DEBUG=0`,
   `PYTHON_VERSION=3.11.9`.
   - **Python version:** the repo's `.python-version` file pins Python **3.11** (Render uses the latest 3.11
     patch). The visa OCR package `rapidocr-onnxruntime==1.4.4` only supports Python below 3.13, so Render's
     default Python 3.14 cannot install it. A `PYTHON_VERSION` variable overrides the file - if you set one,
     keep it on 3.11.x (or 3.12.x), never 3.13 or newer. (`runtime.txt` is not read by Render.)
5. After the first successful deploy, open the service's **Shell** tab and run
   `python create_admin.py` to set the admin password (hidden prompt, never stored in a file).
6. Log in at `https://<your-app>.onrender.com/admin/login` → **Cycles** → create and open the current
   funding cycle, and add organizations/opportunities. Students can then apply.

### Pre-deploy check
```cmd
python check_templates.py
```
This lists every `render_template()` template with OK/MISSING, and checks `{% extends %}`/`{% include %}`
targets, Jinja syntax, every `url_for()` endpoint, every static file, every form's HTTP method, and
Python syntax. It uses a temporary database, never your real one. It must end with
`ALL CHECKS PASSED`.
