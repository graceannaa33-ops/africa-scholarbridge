"""User Accounts -> Account Management: access control, search, details,
permanent deletion (records + files), CSRF, double-click safety, audit log."""
import json
import os
import re
import secrets
import threading
import uuid

import pytest
from werkzeug.security import generate_password_hash

import app as app_module
from conftest import get_application
from database import get_db


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------
def q(sql, args=()):
    db = get_db()
    rows = db.execute(sql, args).fetchall()
    db.close()
    return rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    rid = cur.lastrowid
    db.close()
    return rid


def make_admin():
    email = f"admin-{uuid.uuid4().hex[:6]}@example.org"
    uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'admin')",
                  (email, generate_password_hash("x", method="pbkdf2:sha256:1")))
    execute("INSERT INTO admins (user_id, full_name) VALUES (?, 'Test Admin')", (uid,))
    return uid, email


def as_admin(client):
    uid, email = make_admin()
    with client.session_transaction() as s:
        s.clear()
        s["role"] = "admin"
        s["user_id"] = uid
    return uid, email


def user_id_of(student):
    return q("SELECT user_id FROM students WHERE id = ?", (student["student_id"],))[0][0]


def open_details(client, uid):
    r = client.get(f"/admin/accounts/{uid}")
    assert r.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', r.get_data(as_text=True)).group(1)


def delete(client, uid, email, token, reason="Account violates platform rules/law.", confirm="yes"):
    data = {"csrf_token": token, "reason": reason, "confirm_email": email}
    if confirm:
        data["confirm_permanent"] = confirm
    return client.post(f"/admin/accounts/{uid}/delete", data=data)


def enrich(client, student):
    """Give the student realistic related data + real stored files."""
    client.post("/application/step/visa", data={"visa_choice": "no"})   # visa request + checklist + history
    sid = student["student_id"]
    app_row = get_application(sid)
    req = q("SELECT id FROM visa_requests WHERE student_id = ?", (sid,))[0][0]
    visa_name = secrets.token_hex(16) + ".pdf"
    proof_name = secrets.token_hex(16) + ".png"
    for d, n in ((app_module.VISA_DOCS_DIR, visa_name), (app_module.PAYMENT_PROOF_DIR, proof_name)):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, n), "wb") as fh:
            fh.write(b"personal document")
    execute("UPDATE funding_applications SET visa_document_path = ? WHERE id = ?", (visa_name, app_row["id"]))
    execute("""INSERT INTO visa_payments (request_id, student_id, amount, currency, proof_file, mpesa_transaction_code)
               VALUES (?, ?, 1500, 'KES', ?, ?)""", (req, sid, proof_name, "QK" + secrets.token_hex(4).upper()))
    opp = q("SELECT id FROM funding_opportunities LIMIT 1")
    if opp:
        execute("INSERT INTO saved_opportunities (student_id, opportunity_id) VALUES (?, ?)", (sid, opp[0][0]))
    execute("INSERT INTO capacity_active_users (user_key, last_seen) VALUES (?, strftime('%s','now'))",
            (f"student:{user_id_of(student)}",))
    return {"visa_file": os.path.join(app_module.VISA_DOCS_DIR, visa_name),
            "proof_file": os.path.join(app_module.PAYMENT_PROOF_DIR, proof_name),
            "application_id": app_row["id"], "request_id": req}


def references_to(student_id, user_id, application_id, request_id):
    """Every row anywhere that still points at the deleted account."""
    found = {}
    db = get_db()
    for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({t})")}
        checks = [("student_id", student_id), ("user_id", user_id), ("application_id", application_id),
                  ("annual_application_id", application_id), ("request_id", request_id)]
        if t == "students":
            checks.append(("id", student_id))
        if t == "users":
            checks.append(("id", user_id))
        for col, val in checks:
            if col in cols and t not in ("admin_audit_log",):
                n = db.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} = ?", (val,)).fetchone()[0]
                if n:
                    found[f"{t}.{col}"] = n
    broken = db.execute("PRAGMA foreign_key_check").fetchall()
    db.close()
    return found, broken


# ---------------------------------------------------------------------
# 1-3, 10, 12: access control
# ---------------------------------------------------------------------
def test_admin_can_open_account_management(client):
    as_admin(client)
    r = client.get("/admin/accounts")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Account Management" in html and "User Accounts" in html and "Deletion audit log" in html


def test_student_cannot_access_or_delete(client, student):
    uid = user_id_of(student)
    assert client.get("/admin/accounts").status_code == 302
    assert client.get(f"/admin/accounts/{uid}").status_code == 302
    r = client.post(f"/admin/accounts/{uid}/delete", data={"reason": "self delete attempt", "confirm_email": student["email"],
                                                          "confirm_permanent": "yes"})
    assert r.status_code == 302 and "/admin/login" in r.headers["Location"]
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (uid,))[0][0] == 1


def test_student_cannot_delete_another_student(client, student):
    victim_email = f"victim-{uuid.uuid4().hex[:6]}@example.com"
    execute("INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'student')", (victim_email,))
    victim = q("SELECT id FROM users WHERE email = ?", (victim_email,))[0][0]
    execute("INSERT INTO students (user_id, full_name) VALUES (?, 'Victim')", (victim,))
    r = client.post(f"/admin/accounts/{victim}/delete", data={"reason": "x" * 10, "confirm_email": victim_email,
                                                             "confirm_permanent": "yes"})
    assert r.status_code == 302
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (victim,))[0][0] == 1


def test_visa_admin_cannot_access_account_management(client, student):
    uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'visa_admin')",
                  (f"va-{uuid.uuid4().hex[:6]}@example.org",))
    vaid = execute("INSERT INTO visa_admins (user_id, full_name) VALUES (?, 'Visa Admin')", (uid,))
    with client.session_transaction() as s:
        s.clear()
        s["visa_admin_user_id"] = vaid
    target = user_id_of(student)
    assert client.get("/admin/accounts").status_code == 302
    r = client.post(f"/admin/accounts/{target}/delete", data={"reason": "visa admin attempt", "confirm_email": student["email"],
                                                             "confirm_permanent": "yes"})
    assert r.status_code == 302 and "/admin/login" in r.headers["Location"]
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (target,))[0][0] == 1


def test_unauthenticated_requests_are_rejected(client, student):
    target = user_id_of(student)
    with client.session_transaction() as s:
        s.clear()
    for r in (client.get("/admin/accounts"), client.get(f"/admin/accounts/{target}"),
              client.post(f"/admin/accounts/{target}/delete", data={"reason": "anonymous attempt"})):
        assert r.status_code == 302 and "/admin/login" in r.headers["Location"]
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (target,))[0][0] == 1


def test_get_cannot_delete(client, student):
    target = user_id_of(student)
    as_admin(client)
    assert client.get(f"/admin/accounts/{target}/delete").status_code == 405
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (target,))[0][0] == 1


@pytest.mark.parametrize("problem", ["no csrf", "wrong csrf", "no reason", "short reason", "wrong email", "not ticked"])
def test_delete_requires_csrf_reason_and_explicit_confirmation(client, student, problem):
    target = user_id_of(student)
    as_admin(client)
    token = open_details(client, target)
    kwargs = {}
    if problem == "no csrf":
        token = ""
    elif problem == "wrong csrf":
        token = "forged-" + token
    elif problem == "no reason":
        kwargs["reason"] = ""
    elif problem == "short reason":
        kwargs["reason"] = "bad"
    elif problem == "wrong email":
        kwargs["confirm_email"] = "someone-else@example.com"
    elif problem == "not ticked":
        kwargs["confirm"] = None
    email = kwargs.pop("confirm_email", student["email"])
    r = delete(client, target, email, token, **kwargs)
    assert r.status_code == 303
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (target,))[0][0] == 1
    assert q("SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = ?", (target,))[0][0] == 0


# ---------------------------------------------------------------------
# 4-5: search, pagination, details, privacy
# ---------------------------------------------------------------------
def test_search_by_name_email_and_phone(client, student):
    as_admin(client)
    by_email = client.get("/admin/accounts", query_string={"q": student["email"]}).get_data(as_text=True)
    assert student["email"] in by_email
    by_name = client.get("/admin/accounts", query_string={"q": "Wanjiru"}).get_data(as_text=True)
    assert student["email"] in by_name
    by_phone = client.get("/admin/accounts", query_string={"q": "0712345678"}).get_data(as_text=True)
    assert student["email"] in by_phone
    none = client.get("/admin/accounts", query_string={"q": "no-such-person-xyz"}).get_data(as_text=True)
    assert "No student accounts found" in none


def test_search_wildcards_are_literal(client, student):
    as_admin(client)
    html = client.get("/admin/accounts", query_string={"q": "%"}).get_data(as_text=True)
    assert "No student accounts found" in html                       # '%' is not "match everything"


def test_pagination(client, student):
    for i in range(30):
        uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'student')",
                      (f"page{i}-{uuid.uuid4().hex[:4]}@example.com",))
        execute("INSERT INTO students (user_id, full_name) VALUES (?, 'Paged Student')", (uid,))
    as_admin(client)
    p1 = client.get("/admin/accounts", query_string={"q": "Paged Student"}).get_data(as_text=True)
    p2 = client.get("/admin/accounts", query_string={"q": "Paged Student", "page": 2}).get_data(as_text=True)
    assert "Page 1 of 2" in p1 and "Page 2 of 2" in p2
    assert p1.count("View details") == 25 and p2.count("View details") == 5
    assert client.get("/admin/accounts", query_string={"page": "abc"}).status_code == 200
    assert client.get("/admin/accounts", query_string={"page": 999}).status_code == 200


def test_list_shows_only_what_is_needed(client, student):
    execute("UPDATE students SET date_of_birth='2003-05-14', citizenship='Kenyan' WHERE id = ?", (student["student_id"],))
    as_admin(client)
    html = client.get("/admin/accounts", query_string={"q": student["email"]}).get_data(as_text=True)
    assert "2003-05-14" not in html and "Kenyan" not in html                   # no DOB / citizenship in the list
    headers = re.findall(r"<th[^>]*>([^<]+)</th>", html.split("Deletion audit log")[0])
    assert headers == ["Name", "Email", "Phone", "Created", "Status", "Applications", "Last activity"]


def test_admin_and_visa_admin_accounts_are_never_listed_or_deletable(client):
    admin_uid, admin_email = as_admin(client)
    va_uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'visa_admin')", (admin_email,))
    execute("INSERT INTO visa_admins (user_id, full_name) VALUES (?, 'Visa Admin')", (va_uid,))
    html = client.get("/admin/accounts", query_string={"q": admin_email}).get_data(as_text=True)
    assert "No student accounts found" in html
    with client.session_transaction() as s:
        s["_csrf_token"] = "valid-token-for-this-test"
    for target in (admin_uid, va_uid):
        assert client.get(f"/admin/accounts/{target}").status_code == 302     # "does not exist" -> list
        r = delete(client, target, admin_email, "valid-token-for-this-test")
        assert r.status_code == 303
        assert q("SELECT COUNT(*) FROM users WHERE id = ?", (target,))[0][0] == 1
    assert q("SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id IN (?, ?)", (admin_uid, va_uid))[0][0] == 0


def test_details_page_shows_account_and_impact(client, student):
    enrich(client, student)
    target = user_id_of(student)
    as_admin(client)
    html = client.get(f"/admin/accounts/{target}").get_data(as_text=True)
    assert student["email"] in html and "Amina Wanjiru Otieno" in html
    assert "PERMANENT DELETION" in html
    flat = " ".join(html.replace("&#39;", "'").split())
    assert ("This action permanently deletes this student's account, applications, uploaded documents, payment records, "
            "visa information, and other associated data. This cannot be undone.") in flat
    for step in ("1. Enter the student's email exactly", "2. Enter a deletion reason",
                 "3. I understand this deletion is permanent.", "4. Click <strong>Permanently Delete Account</strong>"):
        assert step in flat
    assert ">Permanently Delete Account</button>" in flat
    assert "deactivat" not in html.lower() and "archiv" not in html.lower()
    assert "1 visa-assistance request(s)" in flat and "1 M-PESA payment record(s)" in flat
    assert "2 stored file(s)" in flat
    assert 'name="csrf_token"' in html and 'method="POST"' in html


# ---------------------------------------------------------------------
# 6-8, 13: the deletion itself
# ---------------------------------------------------------------------
def test_admin_deletes_account_records_files_and_audit(client, student):
    files = enrich(client, student)
    sid, uid = student["student_id"], user_id_of(student)
    admin_uid, admin_email = as_admin(client)
    token = open_details(client, uid)
    r = delete(client, uid, student["email"].upper(), token)          # e-mail match is case-insensitive
    assert r.status_code == 303 and r.headers["Location"].endswith("/admin/accounts")
    page = client.get("/admin/accounts").get_data(as_text=True)
    assert "all of its data were permanently deleted" in page

    found, broken = references_to(sid, uid, files["application_id"], files["request_id"])
    assert found == {} and broken == []
    assert not os.path.exists(files["visa_file"]) and not os.path.exists(files["proof_file"])

    audit = q("SELECT * FROM admin_audit_log WHERE target_user_id = ?", (uid,))
    assert len(audit) == 1
    a = dict(audit[0])
    assert a["admin_user_id"] == admin_uid and a["admin_email"] == admin_email
    assert a["action"] == "delete_student_account" and a["target_student_id"] == sid
    assert a["reason"] == "Account violates platform rules/law." and a["created_at"]
    assert student["email"] not in json.dumps(a) and a["target_label"].endswith("@example.com")
    details = json.loads(a["details"])
    assert details["files"]["removed"] == 2 and details["removed"]["visa_requests"] == 1
    assert "Account violates platform rules/law." in page                  # shown in the audit panel


def test_other_students_are_untouched(client, student):
    other_email = f"other-{uuid.uuid4().hex[:6]}@example.com"
    other_uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'student')", (other_email,))
    other_sid = execute("INSERT INTO students (user_id, full_name) VALUES (?, 'Other')", (other_uid,))
    other_file = os.path.join(app_module.VISA_DOCS_DIR, secrets.token_hex(16) + ".pdf")
    os.makedirs(app_module.VISA_DOCS_DIR, exist_ok=True)
    open(other_file, "wb").write(b"other")
    enrich(client, student)
    uid = user_id_of(student)
    as_admin(client)
    delete(client, uid, student["email"], open_details(client, uid))
    assert q("SELECT COUNT(*) FROM students WHERE id = ?", (other_sid,))[0][0] == 1
    assert os.path.exists(other_file)


def test_deleted_students_open_session_is_signed_out_cleanly(client, student):
    uid = user_id_of(student)
    with client.session_transaction() as s:
        student_session = dict(s)
    admin_client = app_module.app.test_client()
    as_admin(admin_client)
    delete(admin_client, uid, student["email"], open_details(admin_client, uid))
    with client.session_transaction() as s:
        s.update(student_session)
    r = client.get("/dashboard")
    assert r.status_code == 302 and "/login" in r.headers["Location"]      # not a 500
    assert client.get("/application/step/personal").status_code == 302


def test_database_error_rolls_back_everything(client, student, monkeypatch):
    files = enrich(client, student)
    uid = user_id_of(student)
    as_admin(client)
    token = open_details(client, uid)
    import account_moderation

    def boom(*a, **k):
        raise RuntimeError("simulated failure mid-deletion")
    monkeypatch.setattr(account_moderation, "mask_email", boom)             # fails after the DELETE, before commit
    r = delete(client, uid, student["email"], token)
    assert r.status_code == 303
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (uid,))[0][0] == 1  # rolled back
    assert q("SELECT COUNT(*) FROM visa_requests WHERE student_id = ?", (student["student_id"],))[0][0] == 1
    assert os.path.exists(files["visa_file"]) and os.path.exists(files["proof_file"])
    assert q("SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = ?", (uid,))[0][0] == 0


# ---------------------------------------------------------------------
# 9: double-click / concurrent deletion
# ---------------------------------------------------------------------
def test_double_click_delete_is_harmless(client, student):
    uid = user_id_of(student)
    as_admin(client)
    token = open_details(client, uid)
    first = delete(client, uid, student["email"], token)
    second = delete(client, uid, student["email"], token)
    assert first.status_code == 303 and second.status_code == 303
    assert "already deleted" in client.get("/admin/accounts").get_data(as_text=True)
    assert q("SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = ?", (uid,))[0][0] == 1


def test_simultaneous_deletes_run_once(client, student):
    enrich(client, student)
    uid = user_id_of(student)
    as_admin(client)
    token = open_details(client, uid)
    with client.session_transaction() as s:
        session_data = dict(s)
    clients = [app_module.app.test_client() for _ in range(4)]
    for c in clients:
        with c.session_transaction() as s:
            s.update(session_data)
    results = []

    def go(c):
        results.append(delete(c, uid, student["email"], token).status_code)
    threads = [threading.Thread(target=go, args=(c,)) for c in clients]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [303] * 4                                             # no 500s
    assert q("SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = ?", (uid,))[0][0] == 1
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (uid,))[0][0] == 0


# ---------------------------------------------------------------------
# True permanent deletion: nothing recoverable is left behind
# ---------------------------------------------------------------------
def full_footprint(client, student):
    """Everything a real student can accumulate, in every table that can hold
    their data, each personal value a UNIQUE marker so it can be searched for
    anywhere in the database afterwards."""
    files = enrich(client, student)
    sid, uid = student["student_id"], user_id_of(student)
    app_id, req = files["application_id"], files["request_id"]
    tag = secrets.token_hex(4)
    m = {  # label -> unique personal value
        "email": student["email"],
        "phone": "07" + str(secrets.randbelow(10 ** 8)).zfill(8),
        "name": "Zawadi" + tag + " Mwende",
        "passport": "AK" + tag.upper() + "77",
        "visa_number": "SEVIS-N" + tag + "0042",
        "answer": "MY-PERSONAL-STATEMENT-" + tag,
        "household": "HOUSEHOLD-ANSWER-" + tag,
        "mpesa_message": "MPESA-SMS-" + tag + " Confirmed Ksh1,500 sent",
        "admin_mpesa_message": "ADMIN-MPESA-" + tag,
        "account_number": "ACCT" + tag + "5501",
        "iban": "KE" + tag + "IBAN",
        "original_filename": "my-visa-" + tag + ".pdf",
        "receipt_path": "receipts/" + tag + "-receipt.pdf",
        "document_path": "uploads/doc-" + tag + ".pdf",
        "notification": "NOTIFY-" + tag,
        "match_reason": "MATCH-REASON-" + tag,
        "referral_note": "REFERRAL-NOTE-" + tag,
        "decision_note": "DECISION-NOTE-" + tag,
        "disbursement_ref": "DISB-" + tag,
        "visa_note": "VISA-NOTE-" + tag,
        "visa_admin_alert": "VISA-ALERT-" + tag,
        "history_note": "HISTORY-NOTE-" + tag,
        "contact_message": "CONTACT-MSG-" + tag,
        "scam_description": "SCAM-DESC-" + tag,
        "fee_reference": "FEE-REF-" + tag,
    }
    m["password_hash"] = q("SELECT password_hash FROM users WHERE id = ?", (uid,))[0][0]
    m["mpesa_code"] = q("SELECT mpesa_transaction_code FROM visa_payments WHERE student_id = ?", (sid,))[0][0]
    m["visa_file"] = os.path.basename(files["visa_file"])
    m["proof_file"] = os.path.basename(files["proof_file"])

    execute("UPDATE students SET phone = ?, full_name = ? WHERE id = ?", (m["phone"], m["name"], sid))
    execute("""UPDATE funding_applications SET full_name = ?, phone = ?, personal_statement = ?, household_situation = ?,
                      visa_document_passport_number = ?, visa_document_original_name = ? WHERE id = ?""",
            (m["name"], m["phone"], m["answer"], m["household"], m["passport"], m["original_filename"], app_id))
    execute("UPDATE visa_requests SET full_name = ?, email = ?, phone = ?, sevis_info = ?, visa_fee_receipt_path = ? "
            "WHERE id = ?", (m["name"], m["email"], m["phone"], m["visa_number"], m["receipt_path"], req))
    execute("""UPDATE visa_payments SET phone_number = ?, student_name = ?, student_email = ?, student_phone = ?,
                      submitted_mpesa_message = ?, admin_incoming_mpesa_message = ? WHERE student_id = ?""",
            (m["phone"], m["name"], m["email"], m["phone"], m["mpesa_message"], m["admin_mpesa_message"], sid))
    execute("""INSERT INTO student_bank_details (student_id, application_id, country, bank_name, account_holder_name,
                      account_number, iban, mobile_money_number) VALUES (?, ?, 'Kenya', 'Test Bank', ?, ?, ?, ?)""",
            (sid, app_id, m["name"], m["account_number"], m["iban"], m["phone"]))
    execute("INSERT INTO documents (application_id, document_type, status, file_path) VALUES (?, 'Transcript', 'Uploaded', ?)",
            (app_id, m["document_path"]))
    execute("UPDATE visa_documents SET file_path = ? WHERE request_id = ?", (m["document_path"], req))
    execute("INSERT INTO notifications (student_id, message) VALUES (?, ?)", (sid, m["notification"]))
    execute("INSERT INTO application_history (application_id, status, note) VALUES (?, 'Draft', ?)",
            (app_id, m["history_note"]))
    org = execute("INSERT INTO organizations (name, org_type) VALUES ('Test Org', 'Foundation')")
    prog = execute("INSERT INTO funding_programs (organization_id, name) VALUES (?, 'Test Program')", (org,))
    opp = execute("INSERT INTO funding_opportunities (program_id, title, funding_type) VALUES (?, 'Test Grant', 'Grant')",
                  (prog,))
    execute("INSERT INTO saved_opportunities (student_id, opportunity_id) VALUES (?, ?)", (sid, opp))
    match = execute("""INSERT INTO funding_matches (application_id, opportunity_id, score, match_strength, reasons)
                       VALUES (?, ?, 80, 'Strong', ?)""", (app_id, opp, m["match_reason"]))
    referral = execute("INSERT INTO provider_referrals (match_id, notes) VALUES (?, ?)", (match, m["referral_note"]))
    decision = execute("INSERT INTO funding_decisions (referral_id, decision, notes) VALUES (?, 'Funded', ?)",
                       (referral, m["decision_note"]))
    execute("""INSERT INTO funding_disbursements (student_id, application_id, opportunity_id, referral_id, amount, currency,
                      payment_status, transaction_reference) VALUES (?, ?, ?, ?, '1000', 'USD', 'PAID', ?)""",
            (sid, app_id, opp, referral, m["disbursement_ref"]))
    execute("INSERT INTO visa_notes (request_id, note) VALUES (?, ?)", (req, m["visa_note"]))
    execute("INSERT INTO visa_admin_notifications (request_id, message) VALUES (?, ?)", (req, m["visa_admin_alert"]))
    execute("INSERT INTO visa_fee_transactions (request_id, payment_reference, receipt_path) VALUES (?, ?, ?)",
            (req, m["fee_reference"], m["receipt_path"]))
    execute("INSERT INTO visa_status_history (request_id, status, note) VALUES (?, 'Draft', ?)", (req, m["history_note"]))
    execute("INSERT INTO contact_messages (name, email, subject, message) VALUES (?, ?, 'Help', ?)",
            (m["name"], m["email"], m["contact_message"] + " call " + m["phone"]))
    execute("INSERT INTO scam_reports (reporter_name, reporter_email, description) VALUES (?, ?, ?)",
            (m["name"], m["email"].upper(), m["scam_description"]))
    files.update(sid=sid, uid=uid, phone=m["phone"], name=m["name"], markers=m,
                 match_id=match, referral_id=referral, decision_id=decision)
    return files


PER_STUDENT_TABLES = {
    "users": "id = :uid", "students": "id = :sid", "funding_applications": "student_id = :sid",
    "application_history": "application_id = :app", "documents": "application_id = :app",
    "funding_matches": "id = :match", "provider_referrals": "id = :referral", "funding_decisions": "id = :decision",
    "student_bank_details": "student_id = :sid", "funding_disbursements": "student_id = :sid",
    "notifications": "student_id = :sid", "saved_opportunities": "student_id = :sid",
    "visa_requests": "student_id = :sid", "visa_documents": "request_id = :req",
    "visa_status_history": "request_id = :req", "visa_notes": "request_id = :req",
    "visa_admin_notifications": "request_id = :req", "visa_fee_transactions": "request_id = :req",
    "visa_payments": "student_id = :sid",
    "contact_messages": "lower(email) = lower(:email)", "scam_reports": "lower(reporter_email) = lower(:email)",
    "capacity_active_users": "user_key = 'student:' || :uid",
}


def footprint_counts(f, email):
    args = {"uid": f["uid"], "sid": f["sid"], "req": f["request_id"], "app": f["application_id"], "email": email,
            "match": f["match_id"], "referral": f["referral_id"], "decision": f["decision_id"]}
    return {t: q(f"SELECT COUNT(*) FROM {t} WHERE {w}", args)[0][0] for t, w in PER_STUDENT_TABLES.items()}


def all_table_counts():
    return {t: q(f"SELECT COUNT(*) FROM {t}")[0][0]
            for (t,) in q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def test_permanent_deletion_leaves_nothing_behind(client, student):
    f = full_footprint(client, student)
    before = footprint_counts(f, student["email"])
    assert all(before[t] >= 1 for t in PER_STUDENT_TABLES), before     # every table really had data

    admin_uid, _ = as_admin(client)
    r = delete(client, f["uid"], student["email"], open_details(client, f["uid"]))
    assert r.status_code == 303
    assert "permanently deleted" in client.get("/admin/accounts").get_data(as_text=True)

    # 1. every table: zero rows for this student, and no dangling references anywhere
    assert footprint_counts(f, student["email"]) == {t: 0 for t in PER_STUDENT_TABLES}
    found, broken = references_to(f["sid"], f["uid"], f["application_id"], f["request_id"])
    assert found == {} and broken == []
    assert q("SELECT COUNT(*) FROM users WHERE lower(email) = lower(?)", (student["email"],))[0][0] == 0

    # 2. stored files are gone
    assert not os.path.exists(f["visa_file"]) and not os.path.exists(f["proof_file"])

    # 3. the student cannot log in, and cannot re-use the old session
    login = app_module.app.test_client()
    r = login.post("/login", data={"email": student["email"], "password": "Passw0rd!"}, follow_redirects=True)
    assert "Invalid email or password" in r.get_data(as_text=True)
    with login.session_transaction() as s:
        assert "user_id" not in s

    # 4. not recoverable from the database file itself (secure_delete zeroes freed pages)
    raw = open(os.environ["DATABASE_PATH"], "rb").read()
    for secret in (student["email"], f["phone"], f["name"]):
        assert secret.encode() not in raw, secret
    assert not os.path.exists(os.environ["DATABASE_PATH"] + "-journal")

    # 5. the audit record survives with only the minimum
    a = dict(q("SELECT * FROM admin_audit_log WHERE target_user_id = ?", (f["uid"],))[0])
    blob = json.dumps(a)
    for secret in (student["email"], f["phone"], f["name"], os.path.basename(f["visa_file"]),
                   os.path.basename(f["proof_file"]), "Amina"):
        assert secret not in blob
    assert a["admin_user_id"] == admin_uid and a["target_label"].startswith(student["email"][0])
    d = json.loads(a["details"])
    assert d["removed"]["contact_messages"] == 1 and d["removed"]["scam_reports"] == 1
    assert d["files"] == {"to_delete": 2, "removed": 2, "already_missing": 0, "failed": 0}


def test_other_students_data_is_fully_untouched(client, student):
    other_client = app_module.app.test_client()
    other_email = f"keep-{uuid.uuid4().hex[:8]}@example.com"
    other_client.post("/register", data={"email": other_email, "password": "Passw0rd!", "confirm_password": "Passw0rd!",
                                         "full_name": "Keep Me", "country": "Kenya", "citizenship": "Kenyan",
                                         "phone": "0799999999"})
    other_client.get("/application/start")
    other_client.post("/application/step/personal", data={
        "full_name": "Keep Me", "date_of_birth": "2002-01-01", "country": "Kenya", "citizenship": "Kenyan",
        "phone": "0799999999", "email": other_email, "gender": "Female"})
    other_uid = q("SELECT id FROM users WHERE email = ?", (other_email,))[0][0]
    other = {"email": other_email, "student_id": q("SELECT id FROM students WHERE user_id = ?", (other_uid,))[0][0]}
    of = full_footprint(other_client, other)
    other_before = footprint_counts(of, other_email)

    f = full_footprint(client, student)
    as_admin(client)
    delete(client, f["uid"], student["email"], open_details(client, f["uid"]))
    assert footprint_counts(f, student["email"])["users"] == 0
    assert footprint_counts(of, other_email) == other_before
    assert os.path.exists(of["visa_file"]) and os.path.exists(of["proof_file"])
    r = app_module.app.test_client().post("/login", data={"email": other_email, "password": "Passw0rd!"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")


def test_failed_transaction_rolls_back_every_table(client, student, monkeypatch):
    f = full_footprint(client, student)
    as_admin(client)
    token = open_details(client, f["uid"])
    before_all = all_table_counts()
    before = footprint_counts(f, student["email"])
    import account_moderation

    def boom(*a, **k):
        raise RuntimeError("simulated failure after the DELETEs")
    monkeypatch.setattr(account_moderation, "mask_email", boom)
    r = delete(client, f["uid"], student["email"], token)
    assert r.status_code == 303
    assert "Nothing was changed" in client.get(f"/admin/accounts/{f['uid']}").get_data(as_text=True)
    assert all_table_counts() == before_all
    assert footprint_counts(f, student["email"]) == before
    assert os.path.exists(f["visa_file"]) and os.path.exists(f["proof_file"])


def test_leftover_verification_aborts_the_whole_deletion(client, student, monkeypatch):
    """If anything linked to the student survived the cascade, nothing is committed."""
    f = full_footprint(client, student)
    as_admin(client)
    token = open_details(client, f["uid"])
    before_all = all_table_counts()
    import account_moderation
    monkeypatch.setattr(account_moderation, "_leftovers", lambda *a: {"visa_payments.student_id": 1})
    delete(client, f["uid"], student["email"], token)
    assert all_table_counts() == before_all
    assert os.path.exists(f["visa_file"])


def test_file_cleanup_failure_is_reported_and_audited(client, student, monkeypatch):
    f = full_footprint(client, student)
    as_admin(client)
    token = open_details(client, f["uid"])
    import account_moderation
    real_remove = os.remove

    def flaky_remove(path):
        if path.endswith(os.path.basename(f["proof_file"])):
            raise PermissionError("read-only")
        real_remove(path)
    monkeypatch.setattr(account_moderation.os, "remove", flaky_remove)
    r = delete(client, f["uid"], student["email"], token)
    monkeypatch.undo()
    assert r.status_code == 303
    page = client.get("/admin/accounts").get_data(as_text=True)
    assert "permanently deleted" in page
    assert "Database deletion succeeded, but 1 stored file(s) could not be removed" in page
    assert footprint_counts(f, student["email"]) == {t: 0 for t in PER_STUDENT_TABLES}   # DB deletion stands
    assert not os.path.exists(f["visa_file"]) and os.path.exists(f["proof_file"])
    d = json.loads(q("SELECT details FROM admin_audit_log WHERE target_user_id = ?", (f["uid"],))[0][0])
    assert d["files"]["failed"] == 1 and d["files"]["removed"] == 1
    os.remove(f["proof_file"])


def test_csrf_token_from_another_session_is_rejected(client, student):
    uid = user_id_of(student)
    other_admin = app_module.app.test_client()
    as_admin(other_admin)
    foreign_token = open_details(other_admin, uid)
    as_admin(client)
    open_details(client, uid)                                           # client has its own, different token
    r = delete(client, uid, student["email"], foreign_token)
    assert r.status_code == 303
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (uid,))[0][0] == 1
    r = client.post(f"/admin/accounts/{uid}/delete", data={"reason": "no token at all", "confirm_email": student["email"],
                                                            "confirm_permanent": "yes"})
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (uid,))[0][0] == 1


def test_double_click_deletes_once_with_full_cleanup(client, student):
    f = full_footprint(client, student)
    as_admin(client)
    token = open_details(client, f["uid"])
    first = delete(client, f["uid"], student["email"], token)
    second = delete(client, f["uid"], student["email"], token)
    assert first.status_code == second.status_code == 303
    assert q("SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = ?", (f["uid"],))[0][0] == 1
    assert footprint_counts(f, student["email"]) == {t: 0 for t in PER_STUDENT_TABLES}


# ---------------------------------------------------------------------
# Final security / privacy audit
# ---------------------------------------------------------------------
def _every_text_hit(values):
    """{(table.column, label)} for every value found in ANY column of ANY table."""
    hits = set()
    db = get_db()
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        for col in [r[1] for r in db.execute(f"PRAGMA table_info({t})")]:
            for label, v in values.items():
                n = db.execute(f'SELECT COUNT(*) FROM "{t}" WHERE instr(lower(CAST("{col}" AS TEXT)), lower(?)) > 0',
                               (str(v),)).fetchone()[0]
                if n:
                    hits.add((f"{t}.{col}", label))
    db.close()
    return hits, len(tables)


def _admin_delete(client, f, email, reason="Account violates platform rules/law."):
    as_admin(client)
    return delete(client, f["uid"], email, open_details(client, f["uid"]), reason=reason)


def test_audit_no_personal_value_survives_in_any_table_or_column(client, student):
    f = full_footprint(client, student)
    markers = f["markers"]
    before, n_tables = _every_text_hit(markers)
    assert {label for _, label in before} == set(markers), "every marker must exist before deletion"
    assert n_tables >= 38

    _admin_delete(client, f, student["email"])
    after, _ = _every_text_hit(markers)
    assert after == set(), sorted(after)

    raw = open(os.environ["DATABASE_PATH"], "rb").read()
    leaked = [label for label, v in markers.items() if str(v).encode() in raw]
    assert leaked == []
    assert not os.path.exists(os.environ["DATABASE_PATH"] + "-journal")
    assert not os.path.exists(os.environ["DATABASE_PATH"] + "-wal")


def test_audit_record_is_minimal(client, student):
    f = full_footprint(client, student)
    _admin_delete(client, f, student["email"])
    row = dict(q("SELECT * FROM admin_audit_log WHERE target_user_id = ?", (f["uid"],))[0])
    assert set(row) == {"id", "created_at", "admin_user_id", "admin_email", "action", "target_user_id",
                        "target_student_id", "target_label", "reason", "details"}
    blob = json.dumps(row).lower()
    for label, v in f["markers"].items():
        assert str(v).lower() not in blob, label
    assert row["target_label"] == student["email"][0] + "***@example.com"
    d = json.loads(row["details"])
    assert set(d) == {"removed", "files"}
    assert all(isinstance(v, int) for v in d["removed"].values())         # counts only
    assert all(isinstance(v, int) for v in d["files"].values())


@pytest.mark.parametrize("label", ["email", "phone", "name", "passport", "mpesa_code", "other_email", "long_number"])
def test_reason_must_not_carry_personal_details(client, student, label):
    f = full_footprint(client, student)
    extra = {"other_email": "someone.else@example.org", "long_number": "ID 3456 7890 12"}
    value = extra.get(label) or f["markers"][label]
    before = all_table_counts()
    r = _admin_delete(client, f, student["email"], reason=f"Fraudulent account, see {value}")
    assert r.status_code == 303 and "delete=1" in r.headers["Location"]
    assert "must not contain the student" in client.get(r.headers["Location"]).get_data(as_text=True)
    after = all_table_counts()
    assert {t: after[t] - before[t] for t in after if after[t] != before[t]} == {"users": 1, "admins": 1}  # just the test admin
    assert os.path.exists(f["visa_file"])


def test_storage_has_no_files_or_references_left(client, student):
    f = full_footprint(client, student)
    second = _second_student()
    other = full_footprint(second["_client"], second)
    _admin_delete(client, f, student["email"])
    names = {os.path.basename(f["visa_file"]), os.path.basename(f["proof_file"])}
    on_disk = {n for _, _, fs in os.walk(app_module.UPLOAD_ROOT) for n in fs}
    assert names & on_disk == set()
    path_cols = []
    db = get_db()
    for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        for col in [r[1] for r in db.execute(f"PRAGMA table_info({t})")]:
            if any(k in col for k in ("path", "file", "receipt", "original_name")):
                path_cols.append((t, col))
                for n in names | {f["markers"]["document_path"], f["markers"]["receipt_path"]}:
                    assert db.execute(f'SELECT COUNT(*) FROM "{t}" WHERE instr("{col}", ?) > 0', (n,)).fetchone()[0] == 0
    db.close()
    assert len(path_cols) >= 7
    assert os.path.exists(other["visa_file"]) and os.path.exists(other["proof_file"])   # other student's kept


def _second_student():
    c = app_module.app.test_client()
    email = f"second-{uuid.uuid4().hex[:8]}@example.com"
    c.post("/register", data={"email": email, "password": "Passw0rd!", "confirm_password": "Passw0rd!",
                              "full_name": "Second Student", "country": "Kenya", "citizenship": "Kenyan",
                              "phone": "0788888888"})
    c.get("/application/start")
    c.post("/application/step/personal", data={
        "full_name": "Second Student", "date_of_birth": "2001-02-03", "country": "Kenya", "citizenship": "Kenyan",
        "phone": "0788888888", "email": email, "gender": "Male"})
    uid = q("SELECT id FROM users WHERE email = ?", (email,))[0][0]
    return {"email": email, "student_id": q("SELECT id FROM students WHERE user_id = ?", (uid,))[0][0], "_client": c}


# a database error at different points INSIDE the transaction: nothing may be half-deleted
@pytest.mark.parametrize("trigger", [
    "BEFORE DELETE ON contact_messages",            # first statement
    "BEFORE DELETE ON scam_reports",                # after contact messages were deleted
    "BEFORE DELETE ON visa_payments",               # in the middle of the cascade
    "BEFORE DELETE ON funding_decisions",           # deepest level of the cascade
    "BEFORE DELETE ON students",                    # after all children were deleted
    "BEFORE INSERT ON admin_audit_log",             # very last statement before commit
])
def test_database_failure_at_any_point_changes_nothing(client, student, trigger):
    f = full_footprint(client, student)
    as_admin(client)
    token = open_details(client, f["uid"])
    execute(f"CREATE TRIGGER fail_here {trigger} BEGIN SELECT RAISE(ABORT, 'simulated disk error'); END")
    try:
        before = all_table_counts()
        r = delete(client, f["uid"], student["email"], token)
    finally:
        execute("DROP TRIGGER fail_here")
    assert r.status_code == 303
    assert "Nothing was changed" in client.get(r.headers["Location"]).get_data(as_text=True)
    assert all_table_counts() == before
    assert all(v >= 1 for v in footprint_counts(f, student["email"]).values())
    assert os.path.exists(f["visa_file"]) and os.path.exists(f["proof_file"])
    assert q("PRAGMA foreign_key_check") == []


def test_expired_admin_session_cannot_delete(client, student):
    uid = user_id_of(student)
    as_admin(client)
    token = open_details(client, uid)
    with client.session_transaction() as s:                   # session expired / cookie gone
        s.clear()
    r = delete(client, uid, student["email"], token)
    assert r.status_code == 302 and "/admin/login" in r.headers["Location"]
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (uid,))[0][0] == 1


def test_removed_admin_with_old_session_cannot_delete(client, student):
    uid = user_id_of(student)
    admin_uid, _ = as_admin(client)
    token = open_details(client, uid)
    execute("DELETE FROM users WHERE id = ?", (admin_uid,))    # admin account removed while signed in
    r = delete(client, uid, student["email"], token)
    assert r.status_code == 403
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (uid,))[0][0] == 1


def test_direct_url_access_by_every_non_admin(client, student):
    target = user_id_of(student)
    urls = ["/admin/accounts", f"/admin/accounts/{target}", f"/admin/accounts/{target}?delete=1"]
    va_uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'visa_admin')",
                     (f"va-{uuid.uuid4().hex[:6]}@example.org",))
    sessions = [{}, {"role": "student", "user_id": target}, {"visa_admin_user_id": va_uid}]
    for sess in sessions:
        c = app_module.app.test_client()
        with c.session_transaction() as s:
            s.update(sess)
            s["_csrf_token"] = "t"
        for u in urls:
            r = c.get(u)
            assert r.status_code == 302 and "/admin/login" in r.headers["Location"], (sess, u)
            assert student["email"] not in r.get_data(as_text=True)
        r = c.post(f"/admin/accounts/{target}/delete", data={"csrf_token": "t", "reason": "not an admin at all",
                                                              "confirm_email": student["email"], "confirm_permanent": "yes"})
        assert r.status_code == 302 and "/admin/login" in r.headers["Location"]
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (target,))[0][0] == 1


def test_repeat_deletion_later_from_another_admin_changes_nothing(client, student):
    f = full_footprint(client, student)
    _admin_delete(client, f, student["email"])
    later = app_module.app.test_client()
    as_admin(later)
    with later.session_transaction() as s:
        s["_csrf_token"] = "later-token"
    before = all_table_counts()
    r = delete(later, f["uid"], student["email"], "later-token")
    assert r.status_code == 303
    assert "already deleted" in later.get("/admin/accounts").get_data(as_text=True)
    assert all_table_counts() == before
    assert later.get(f"/admin/accounts/{f['uid']}").status_code == 302


def test_stale_cookie_of_deleted_student_does_not_recreate_activity_row(client, student):
    import capacity_monitor as cm
    f = full_footprint(client, student)
    keep = _second_student()
    keep_uid = user_id_of(keep)
    with client.session_transaction() as s:
        stale = dict(s)                                        # the deleted student's browser cookie
    admin = app_module.app.test_client()
    _admin_delete(admin, f, student["email"])
    cm._take_interval()                                        # start from an empty interval
    for key in (f"student:{f['uid']}",                          # stale cookie seen by a worker
                f"student:{keep_uid}"):                          # a live student
        cm.request_finished(cm.request_started("/dashboard", key), 200)
    db = get_db()
    try:
        cm.flush_sample(db, cm.load_config())
    finally:
        db.close()
        cm._take_interval()                                    # leave no counters behind for other tests
    assert q("SELECT COUNT(*) FROM capacity_active_users WHERE user_key = ?", (f"student:{f['uid']}",))[0][0] == 0
    assert q("SELECT COUNT(*) FROM capacity_active_users WHERE user_key = ?", (f"student:{keep_uid}",))[0][0] == 1
    with client.session_transaction() as s:
        s.update(stale)
    assert client.get("/dashboard").status_code == 302         # signed out, not a 500


# ---------------------------------------------------------------------
# Re-registration after permanent deletion
# ---------------------------------------------------------------------
def _register(c, email, phone, password="N3wPassw0rd!", name="Fresh Start Student"):
    return c.post("/register", data={"email": email, "password": password, "confirm_password": password,
                                     "full_name": name, "country": "Kenya", "citizenship": "Kenyan", "phone": phone})


def test_reregistration_after_deletion_is_a_completely_new_account(client, student):
    # a/b. a student with realistic records and real uploaded files
    f = full_footprint(client, student)
    email, phone = student["email"], f["phone"]
    old = {"uid": f["uid"], "sid": f["sid"], "app": f["application_id"], "req": f["request_id"],
           "hash": f["markers"]["password_hash"],
           "reference": q("SELECT reference_number FROM funding_applications WHERE id = ?", (f["application_id"],))[0][0],
           "request_number": q("SELECT request_number FROM visa_requests WHERE id = ?", (f["request_id"],))[0][0]}
    with client.session_transaction() as s:
        old_cookie = dict(s)

    # c. permanent deletion through Main Admin
    admin = app_module.app.test_client()
    assert _admin_delete(admin, f, email).status_code == 303
    audit_before = [dict(r) for r in q("SELECT * FROM admin_audit_log WHERE target_user_id = ?", (old["uid"],))]
    assert len(audit_before) == 1

    # d. account and personal data gone
    assert footprint_counts(f, email) == {t: 0 for t in PER_STUDENT_TABLES}
    assert not os.path.exists(f["visa_file"]) and not os.path.exists(f["proof_file"])

    # e/f. register again with the exact same email and phone
    new_client = app_module.app.test_client()
    r = _register(new_client, email, phone)
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")
    assert "Your account has been created" in new_client.get("/dashboard").get_data(as_text=True)

    # g. completely new identity, nothing old attached
    new_uid, new_hash = q("SELECT id, password_hash FROM users WHERE email = ?", (email,))[0]
    new_sid, new_name, new_phone = q("SELECT id, full_name, phone FROM students WHERE user_id = ?", (new_uid,))[0]
    assert new_uid > old["uid"] and new_sid > old["sid"]
    assert new_hash != old["hash"]
    assert new_name == "Fresh Start Student" and new_phone == phone
    for table, col in (("funding_applications", "student_id"), ("visa_requests", "student_id"),
                       ("visa_payments", "student_id"), ("funding_disbursements", "student_id"),
                       ("notifications", "student_id"), ("saved_opportunities", "student_id"),
                       ("student_bank_details", "student_id")):
        assert q(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (new_sid,))[0][0] == 0, table
    # the ONLY places any old value appears now are the new account's own email and phone
    old_values = {k: v for k, v in f["markers"].items()}
    hits, _ = _every_text_hit(old_values)
    assert hits == {("users.email", "email"), ("students.phone", "phone")}, sorted(hits)
    assert footprint_counts(f, email)["students"] == 0                  # old ids still empty
    for t, cnt in footprint_counts(f, email).items():
        if t not in ("users", "contact_messages", "scam_reports"):        # those match by the (re-used) e-mail
            assert cnt == 0, t
    assert not os.path.exists(f["visa_file"]) and not os.path.exists(f["proof_file"])   # nothing restored

    # h. normal login with the NEW password; the old password and the old cookie do not work
    fresh = app_module.app.test_client()
    bad = fresh.post("/login", data={"email": email, "password": "Passw0rd!"}, follow_redirects=True)
    assert "Invalid email or password" in bad.get_data(as_text=True)
    r = fresh.post("/login", data={"email": email, "password": "N3wPassw0rd!"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")
    with fresh.session_transaction() as s:
        assert s["user_id"] == new_uid
    dash = fresh.get("/dashboard").get_data(as_text=True)
    for value in (old["reference"], old["request_number"], f["name"]):
        if value:
            assert value not in dash

    # ...and start a fresh annual application
    fresh.get("/application/start")
    apps = q("SELECT * FROM funding_applications WHERE student_id = ?", (new_sid,))
    assert len(apps) == 1
    new_app = dict(apps[0])
    assert new_app["id"] > old["app"]
    assert new_app["reference_number"] is None or new_app["reference_number"] != old["reference"]
    assert new_app["visa_document_path"] is None and new_app["visa_document_passport_number"] is None
    assert new_app["visa_request_id"] is None and new_app["personal_statement"] in (None, "")
    assert fresh.get("/application/step/personal").status_code == 200

    old_session = app_module.app.test_client()
    with old_session.session_transaction() as s:
        s.update(old_cookie)
    r = old_session.get("/dashboard")
    assert r.status_code == 302 and "/login" in r.headers["Location"]  # old cookie never reaches the new account

    # the audit record is still there, unchanged, and did not block anything
    assert [dict(r) for r in q("SELECT * FROM admin_audit_log WHERE target_user_id = ?", (old["uid"],))] == audit_before


def test_reregistered_account_can_itself_be_deleted_again(client, student):
    f = full_footprint(client, student)
    _admin_delete(app_module.app.test_client(), f, student["email"])
    c = app_module.app.test_client()
    _register(c, student["email"], f["phone"])
    new_uid = q("SELECT id FROM users WHERE email = ?", (student["email"],))[0][0]
    admin = app_module.app.test_client()
    as_admin(admin)
    r = delete(admin, new_uid, student["email"], open_details(admin, new_uid))
    assert r.status_code == 303
    assert q("SELECT COUNT(*) FROM users WHERE lower(email) = lower(?)", (student["email"],))[0][0] == 0
    assert q("SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id IN (?, ?)", (f["uid"], new_uid))[0][0] == 2


@pytest.mark.parametrize("variant", ["exact", "upper", "padded"])
def test_active_student_cannot_register_duplicate_email(client, student, variant):
    email = {"exact": student["email"], "upper": student["email"].upper(),
             "padded": "  " + student["email"] + " "}[variant]
    before = all_table_counts()
    c = app_module.app.test_client()
    r = _register(c, email, "0700000001")
    assert r.status_code == 302 and r.headers["Location"].endswith("/register")
    assert "already exists" in c.get("/register").get_data(as_text=True)
    assert all_table_counts() == before
    with c.session_transaction() as s:
        assert "user_id" not in s


def test_two_active_students_can_share_a_phone_number(client, student):
    shared = q("SELECT phone FROM students WHERE id = ?", (student["student_id"],))[0][0]
    c = app_module.app.test_client()
    email2 = f"sibling-{uuid.uuid4().hex[:8]}@example.com"
    r = _register(c, email2, shared, name="Sibling Student")
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")
    rows = q("SELECT u.email FROM students s JOIN users u ON u.id = s.user_id WHERE s.phone = ?", (shared,))
    emails = {row[0] for row in rows}
    assert {student["email"], email2} <= emails
    for email, pw in ((student["email"], "Passw0rd!"), (email2, "N3wPassw0rd!")):   # both keep working
        lc = app_module.app.test_client()
        r = lc.post("/login", data={"email": email, "password": pw})
        assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard"), email
        assert lc.get("/dashboard").status_code == 200


def test_deleted_students_phone_reused_while_another_active_student_shares_it(client, student):
    f = full_footprint(client, student)
    sharer = app_module.app.test_client()
    sharer_email = f"sharer-{uuid.uuid4().hex[:8]}@example.com"
    _register(sharer, sharer_email, f["phone"], name="Phone Sharer")      # active student, same phone
    _admin_delete(app_module.app.test_client(), f, student["email"])
    c = app_module.app.test_client()
    r = _register(c, student["email"], f["phone"])
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")
    assert q("SELECT COUNT(*) FROM students WHERE phone = ?", (f["phone"],))[0][0] == 2
    assert q("SELECT COUNT(*) FROM users WHERE email = ?", (sharer_email,))[0][0] == 1       # sharer untouched
