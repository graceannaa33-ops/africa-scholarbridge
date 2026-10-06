"""Strict permanent deletion of a student by the Main Admin - the parts not
already covered by tests/test_account_moderation.py:

* the student disappears from every Main Admin and Visa Admin page and search;
* Step 7 funding-document files are removed from disk (the folder that was
  previously missed), other students' files are not;
* shared/reference data is untouched;
* missing files and unsafe stored names are handled safely;
* no orphaned rows anywhere; nothing student-specific in the audit log,
  including rows written before this rule (cleared at startup).

All test data is fictional.
"""
import json
import os
import re

import pytest
from werkzeug.security import generate_password_hash

import app as app_module
import database
from test_account_moderation import (_admin_delete, _second_student, as_admin, audit_max, audits_since, delete,
                                     execute, full_footprint, open_details, q)

MAIN_ADMIN_PAGES = ["/admin/dashboard", "/admin/students", "/admin/applications", "/admin/accounts",
                    "/admin/disbursements", "/admin/matches", "/admin/providers", "/admin/decisions"]
VISA_ADMIN_PAGES = ["/visa-admin/dashboard", "/visa-admin/requests", "/visa-admin/payments",
                    "/visa-admin/submissions", "/visa-admin/documents", "/visa-admin/fee-coverage",
                    "/visa-admin/processing", "/visa-admin/reports", "/visa-admin/notifications"]
REFERENCE_TABLES = ["banks", "organizations", "funding_programs", "funding_opportunities", "funding_cycles",
                    "visa_pricing", "visa_services", "countries", "app_settings", "visa_admins"]


def visa_admin_client():
    c = app_module.app.test_client()
    uid = execute("INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'visa_admin')",
                  (f"visa-{os.urandom(4).hex()}@example.org", generate_password_hash("x", method="pbkdf2:sha256:1")))
    execute("INSERT INTO visa_admins (user_id, full_name) VALUES (?, 'Test Visa Admin')", (uid,))
    with c.session_transaction() as s:
        s["visa_admin_user_id"] = uid
    return c


def main_admin_client():
    c = app_module.app.test_client()
    as_admin(c)
    return c


def identifiers(f, student):
    return {"name": f["name"], "email": student["email"], "phone": f["phone"],
            "request_number": f["request_number"], "reference": f["reference"]}


def page_hits(client, paths, values, query_values=()):
    """{(path, label)} for every identifying value found on any of the pages
    (plus each page searched for each value)."""
    hits = set()
    for path in paths:
        bodies = [client.get(path).get_data(as_text=True)]
        for v in query_values:
            body = client.get(path, query_string={"q": v}).get_data(as_text=True)
            # Search pages echo the typed query back (the input's value="..." and
            # e.g. 'matching “...”'); only RESULTS count.
            body = re.sub(r'<input[^>]*name="q"[^>]*>', "", body).replace(f"“{v}”", "")
            bodies.append(body)
        for body in bodies:
            for label, v in values.items():
                if v and str(v) in body:
                    hits.add((path, label))
    return hits


@pytest.fixture()
def footprint(client, student):
    f = full_footprint(client, student)
    f["request_number"] = q("SELECT request_number FROM visa_requests WHERE id = ?", (f["request_id"],))[0][0]
    execute("UPDATE funding_applications SET reference_number = ? WHERE id = ?",
            (f"ASB-TEST-{f['application_id']:06d}", f["application_id"]))
    f["reference"] = f"ASB-TEST-{f['application_id']:06d}"
    return f


# ---------------------------------------------------------------------
# Disappears from Main Admin, Visa Admin and search results
# ---------------------------------------------------------------------
def test_student_disappears_from_every_main_admin_page_and_search(client, student, footprint):
    f, ids = footprint, identifiers(footprint, student)
    admin = main_admin_client()
    before = page_hits(admin, MAIN_ADMIN_PAGES, ids, query_values=(f["name"], student["email"]))
    assert ("/admin/students", "name") in before and ("/admin/accounts", "email") in before   # really listed before
    assert admin.get(f"/admin/applications/{f['application_id']}").status_code == 200

    _admin_delete(client, f, student["email"])

    after = page_hits(admin, MAIN_ADMIN_PAGES, ids,
                      query_values=(f["name"], student["email"], f["phone"], f["reference"]))
    assert after == set(), sorted(after)
    assert admin.get(f"/admin/applications/{f['application_id']}").status_code == 404
    for v in (f["name"], student["email"], f["phone"]):
        page = admin.get("/admin/accounts", query_string={"q": v}).get_data(as_text=True)
        assert f"0 student accounts matching “{v}”" in page, v
    r = admin.get(f"/admin/accounts/{f['uid']}")
    assert r.status_code == 302                                          # "does not exist" -> list


def test_student_disappears_from_every_visa_admin_page(client, student, footprint):
    f, ids = footprint, identifiers(footprint, student)
    visa = visa_admin_client()
    before = page_hits(visa, VISA_ADMIN_PAGES, ids)
    assert any(label in ("name", "request_number") for _, label in before)   # really listed before
    assert visa.get(f"/visa-admin/requests/{f['request_id']}").status_code == 200

    _admin_delete(client, f, student["email"])

    after = page_hits(visa, VISA_ADMIN_PAGES, ids, query_values=(f["name"], f["request_number"]))
    assert after == set(), sorted(after)
    r = visa.get(f"/visa-admin/requests/{f['request_id']}")
    assert r.status_code in (302, 404) and f["name"] not in r.get_data(as_text=True)


# ---------------------------------------------------------------------
# Files: Step 7 funding documents (and every other upload) removed from disk
# ---------------------------------------------------------------------
def test_every_uploaded_file_of_the_student_is_removed_from_disk(client, student, footprint):
    f = footprint
    paths = [f["funding_file"], f["visa_file"], f["proof_file"], f["support_file"]]
    assert all(os.path.exists(p) for p in paths)
    base = audit_max()
    _admin_delete(client, f, student["email"])
    for p in paths:
        assert not os.path.exists(p), p
    details = json.loads(audits_since(base)[0]["details"])
    assert details["files"] == {"to_delete": 4, "removed": 4, "already_missing": 0, "failed": 0}
    on_disk = {n for _, _, fs in os.walk(app_module.UPLOAD_ROOT) for n in fs}
    assert {os.path.basename(p) for p in paths} & on_disk == set()


def test_other_students_files_and_rows_are_untouched(client, student, footprint):
    second = _second_student()
    other = full_footprint(second["_client"], second)
    other_paths = [other["funding_file"], other["visa_file"], other["proof_file"], other["support_file"]]
    other_counts = {t: q(f"SELECT COUNT(*) FROM {t} WHERE student_id = ?", (other["sid"],))[0][0]
                    for t in ("funding_applications", "visa_requests", "visa_payments", "student_bank_details",
                              "notifications")}
    other_docs = q("SELECT COUNT(*) FROM documents WHERE application_id = ?", (other["application_id"],))[0][0]

    _admin_delete(client, footprint, student["email"])

    assert all(os.path.exists(p) for p in other_paths)
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (other["uid"],))[0][0] == 1
    assert {t: q(f"SELECT COUNT(*) FROM {t} WHERE student_id = ?", (other["sid"],))[0][0]
            for t in other_counts} == other_counts
    assert q("SELECT COUNT(*) FROM documents WHERE application_id = ?", (other["application_id"],))[0][0] == other_docs


def test_shared_and_reference_data_are_untouched(client, student, footprint):
    before = {t: q(f"SELECT COUNT(*) FROM {t}")[0][0] for t in REFERENCE_TABLES}
    admins_before = {r[0] for r in q("SELECT user_id FROM admins")}
    _admin_delete(client, footprint, student["email"])      # (this helper signs in a NEW admin to delete)
    after = {t: q(f"SELECT COUNT(*) FROM {t}")[0][0] for t in REFERENCE_TABLES}
    assert after == before
    assert admins_before <= {r[0] for r in q("SELECT user_id FROM admins")}   # every existing admin kept


def test_already_missing_files_do_not_break_deletion(client, student, footprint):
    f = footprint
    os.remove(f["funding_file"])
    os.remove(f["proof_file"])
    base = audit_max()
    r = _admin_delete(client, f, student["email"])
    assert r.status_code == 303
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (f["uid"],))[0][0] == 0
    d = json.loads(audits_since(base)[0]["details"])["files"]
    assert d["already_missing"] == 2 and d["removed"] == 2 and d["failed"] == 0
    assert not os.path.exists(f["visa_file"]) and not os.path.exists(f["support_file"])


def test_unsafe_stored_file_name_is_never_followed(client, student, footprint, tmp_path):
    """A tampered file_path must never delete anything outside the upload folder."""
    f = footprint
    outside = os.path.join(os.path.dirname(app_module.FUNDING_DOCS_DIR), "outside-keep.txt")
    with open(outside, "w") as fh:
        fh.write("must survive")
    try:
        execute("INSERT INTO documents (application_id, document_type, status, file_path) "
                "VALUES (?, 'Other', 'Uploaded', 'funding_documents/../outside-keep.txt')", (f["application_id"],))
        base = audit_max()
        _admin_delete(client, f, student["email"])
        assert os.path.exists(outside)                                   # untouched
        assert json.loads(audits_since(base)[0]["details"])["files"]["failed"] == 1   # reported, not followed
        assert q("SELECT COUNT(*) FROM users WHERE id = ?", (f["uid"],))[0][0] == 0
    finally:
        os.remove(outside)


# ---------------------------------------------------------------------
# No orphans, no student-specific audit data
# ---------------------------------------------------------------------
def test_no_orphaned_rows_anywhere_after_deletion(client, student, footprint):
    _admin_delete(client, footprint, student["email"])
    db = database.get_db()
    try:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        # Every student-owned table: no row points at an id that no longer exists.
        checks = {
            "students": "SELECT COUNT(*) FROM students s WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = s.user_id)",
            "funding_applications": "SELECT COUNT(*) FROM funding_applications a WHERE NOT EXISTS "
                                    "(SELECT 1 FROM students s WHERE s.id = a.student_id)",
            "documents": "SELECT COUNT(*) FROM documents d WHERE NOT EXISTS "
                         "(SELECT 1 FROM funding_applications a WHERE a.id = d.application_id)",
            "visa_requests": "SELECT COUNT(*) FROM visa_requests v WHERE NOT EXISTS "
                             "(SELECT 1 FROM students s WHERE s.id = v.student_id)",
            "visa_documents": "SELECT COUNT(*) FROM visa_documents d WHERE NOT EXISTS "
                              "(SELECT 1 FROM visa_requests v WHERE v.id = d.request_id)",
            "visa_payments": "SELECT COUNT(*) FROM visa_payments p WHERE p.student_id IS NOT NULL AND NOT EXISTS "
                             "(SELECT 1 FROM students s WHERE s.id = p.student_id)",
            "student_bank_details": "SELECT COUNT(*) FROM student_bank_details b WHERE NOT EXISTS "
                                    "(SELECT 1 FROM students s WHERE s.id = b.student_id)",
        }
        for table, sql in checks.items():
            assert db.execute(sql).fetchone()[0] == 0, table
    finally:
        db.close()


def test_audit_row_holds_nothing_about_the_student(client, student, footprint):
    f = footprint
    base = audit_max()
    _admin_delete(client, f, student["email"])
    rows = audits_since(base)
    assert len(rows) == 1
    a = rows[0]
    assert a["target_user_id"] is None and a["target_student_id"] is None and a["target_label"] is None
    blob = json.dumps(a).lower()
    for value in (student["email"], student["email"].split("@")[0], f["name"], f["phone"], f["reference"],
                  f["request_number"], os.path.basename(f["funding_file"])):
        assert str(value).lower() not in blob, value
    assert a["action"] == "delete_student_account" and a["reason"] and a["admin_email"]
    page = main_admin_client().get("/admin/accounts").get_data(as_text=True)
    assert "no identifying details kept" in page


def test_older_audit_rows_with_student_identifiers_are_cleared_on_startup(client):
    rid = execute("""INSERT INTO admin_audit_log (admin_user_id, admin_email, action, target_user_id,
                         target_student_id, target_label, reason, details)
                     VALUES (1, 'admin@example.org', 'delete_student_account', 9991, 8881, 'z***@example.com',
                             'Old deletion', '{"removed": {}, "files": {}}')""")
    other = execute("""INSERT INTO admin_audit_log (admin_user_id, admin_email, action, target_user_id, reason)
                       VALUES (1, 'admin@example.org', 'some_other_action', 77, 'unrelated')""")
    database.init_db()
    row = dict(q("SELECT * FROM admin_audit_log WHERE id = ?", (rid,))[0])
    assert row["target_user_id"] is None and row["target_student_id"] is None and row["target_label"] is None
    assert row["reason"] == "Old deletion" and row["admin_email"] == "admin@example.org"   # nothing else changed
    assert q("SELECT target_user_id FROM admin_audit_log WHERE id = ?", (other,))[0][0] == 77   # other actions untouched
    database.init_db()                                                   # idempotent
    assert q("SELECT target_label FROM admin_audit_log WHERE id = ?", (rid,))[0][0] is None


# ---------------------------------------------------------------------
# Confirmation and repeat
# ---------------------------------------------------------------------
def test_confirmation_lists_everything_that_will_be_deleted(client, student, footprint):
    admin = main_admin_client()
    page = " ".join(admin.get(f"/admin/accounts/{footprint['uid']}").get_data(as_text=True)
                    .replace("&#39;", "'").split())
    assert ("permanently deletes this student's account, application information, documents, visa information, "
            "payment/application information, and uploaded files") in page
    assert "4 uploaded file(s) (funding documents, visa documents, passport/ID and photo uploads" in page


def test_deletion_without_confirmation_changes_nothing(client, student, footprint):
    f = footprint
    as_admin(client)
    token = open_details(client, f["uid"])
    base = audit_max()
    delete(client, f["uid"], student["email"], token, confirm=None)
    assert q("SELECT COUNT(*) FROM users WHERE id = ?", (f["uid"],))[0][0] == 1
    assert os.path.exists(f["funding_file"])
    assert audits_since(base) == []


def test_repeated_deletion_is_safe(client, student, footprint):
    f = footprint
    as_admin(client)
    token = open_details(client, f["uid"])
    base = audit_max()
    first = delete(client, f["uid"], student["email"], token)
    second = delete(client, f["uid"], student["email"], token)
    assert first.status_code == second.status_code == 303
    assert "already deleted" in client.get("/admin/accounts").get_data(as_text=True)
    assert len(audits_since(base)) == 1
