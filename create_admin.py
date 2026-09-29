"""
create_admin.py
---------------
One-time (and re-runnable) setup command that creates or updates the REAL
Africa ScholarBridge administrator account in the database.

    python create_admin.py

What it does, using the site's existing authentication system (the `users`
table + Werkzeug password hashing, exactly what /admin/login and
/visa-admin/login check against):

  * users row, role='admin'      + admins profile row
        -> Main Admin: dashboard, student applications, students, cycles,
           organizations, opportunities, matches, providers, decisions,
           banks, disbursements
  * users row, role='visa_admin' + visa_admins profile row
        -> Visa Admin: visa requests, Visa Payments, M-PESA verification,
           verify / reject payments, payment screenshots, submitted M-PESA
           messages, documents, fee coverage, processing, settings

If the email already has these rows they are UPDATED (password reset, role
kept); otherwise they are CREATED. Nothing else in the database is touched.

The password is typed at a hidden prompt (nothing is shown on screen), is
never printed, logged or written anywhere except as a salted hash in the
database. Run it again at any time to reset the password.

Options:
    python create_admin.py --email someone@example.com
        (default: ADMIN_EMAIL environment variable, else
         africascholarbridge@gmail.com)
"""

import argparse
import getpass
import os
import re
import sys

from werkzeug.security import generate_password_hash

from database import get_db, init_db, DB_PATH

DEFAULT_EMAIL = "africascholarbridge@gmail.com"
MIN_LENGTH = 12
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def password_problems(password, email):
    problems = []
    if len(password) < MIN_LENGTH:
        problems.append(f"at least {MIN_LENGTH} characters")
    if not re.search(r"[a-z]", password) or not re.search(r"[A-Z]", password):
        problems.append("both upper-case and lower-case letters")
    if not re.search(r"\d", password):
        problems.append("at least one number")
    if not re.search(r"[^A-Za-z0-9]", password):
        problems.append("at least one symbol (e.g. ! @ # $ %)")
    lowered = password.lower()
    if email.split("@")[0].lower() in lowered or lowered in {
        "admin@12345", "visaadmin@12345", "password1234!", "africascholarbridge1!"
    }:
        problems.append("not the demo password and not based on the email address")
    return problems


def ask_password(email):
    print(f"\nSet the administrator password for {email}")
    print(f"Requirements: {MIN_LENGTH}+ characters, upper + lower case, a number and a symbol.")
    print("(Typing is hidden - nothing will appear on screen. That's normal.)\n")
    for _ in range(3):
        first = getpass.getpass("New admin password: ")
        problems = password_problems(first, email)
        if problems:
            print("  Password needs: " + "; ".join(problems) + ". Try again.\n")
            continue
        second = getpass.getpass("Type it again to confirm: ")
        if first != second:
            print("  The two passwords did not match. Try again.\n")
            continue
        return first
    print("\nToo many attempts. Nothing was changed.")
    sys.exit(1)


def upsert_role(db, email, role, profile_table, full_name, password_hash):
    """Create or update one users row (email+role is unique) and its
    profile row. Returns 'created' or 'updated'."""
    row = db.execute("SELECT id FROM users WHERE email = ? AND role = ?", (email, role)).fetchone()
    if row:
        user_id = row["id"]
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        outcome = "updated"
    else:
        user_id = db.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)", (email, password_hash, role)
        ).lastrowid
        outcome = "created"
    if not db.execute(f"SELECT id FROM {profile_table} WHERE user_id = ?", (user_id,)).fetchone():
        db.execute(f"INSERT INTO {profile_table} (user_id, full_name) VALUES (?, ?)", (user_id, full_name))
    return outcome


def main():
    parser = argparse.ArgumentParser(description="Create or update the Africa ScholarBridge admin account.")
    parser.add_argument("--email", default=os.environ.get("ADMIN_EMAIL") or DEFAULT_EMAIL)
    args = parser.parse_args()
    email = args.email.strip().lower()
    if not EMAIL_RE.match(email):
        print(f"'{email}' is not a valid email address.")
        sys.exit(1)

    init_db()  # make sure all tables/columns exist (safe to run repeatedly)
    db = get_db()
    student = db.execute("SELECT id FROM users WHERE email = ? AND role = 'student'", (email,)).fetchone()
    if student:
        print(f"\nNote: {email} also has a separate STUDENT account. It stays a normal student account;")
        print("the admin account below is a different row with its own password.\n")

    password = ask_password(email)
    password_hash = generate_password_hash(password)  # salted hash, same method as the rest of the site
    del password

    try:
        main_outcome = upsert_role(db, email, "admin", "admins", "Africa ScholarBridge Admin", password_hash)
        visa_outcome = upsert_role(db, email, "visa_admin", "visa_admins", "Africa ScholarBridge Visa Admin",
                                   password_hash)
        db.commit()
    except Exception as exc:  # pragma: no cover - reported to the operator
        db.rollback()
        print(f"\nFailed - nothing was changed: {exc}")
        sys.exit(1)
    finally:
        db.close()

    print("\n" + "=" * 64)
    print("  Administrator account ready")
    print(f"  Email            : {email}")
    print(f"  Main Admin role  : {main_outcome} (users.role = 'admin')")
    print(f"  Visa Admin role  : {visa_outcome} (users.role = 'visa_admin')")
    print(f"  Database         : {DB_PATH}")
    print("  Log in at        : /admin/login on your site (locally: http://127.0.0.1:5000/admin/login)")
    print("=" * 64)


if __name__ == "__main__":
    main()
