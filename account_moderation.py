"""
account_moderation.py
---------------------
Main Admin "User Accounts -> Account Management": list, search, inspect and
PERMANENTLY delete student accounts, with a minimal audit log.

Permanent deletion (delete_student_account) removes, in ONE transaction:
  * the login (users row) and the student profile (students row);
  * everything linked to them by the schema's ON DELETE CASCADE foreign
    keys - applications and their answers, history, documents records,
    matches, provider referrals, funding decisions, bank details,
    disbursements, notifications, saved opportunities, visa-assistance
    requests with their documents/history/notes/fee transactions/admin
    notifications, and M-PESA payment records;
  * personal records NOT linked by a foreign key but belonging to the same
    e-mail address: contact-form messages and scam reports;
  * the student's "recently active" monitoring entry.
It then VERIFIES nothing linked to the student remains (otherwise it rolls
back), writes one DE-IDENTIFIED audit row (which admin, when, why, and how
many records/files - never the student's e-mail, name or account ids), and
commits. SQLite's secure_delete is on for
every connection (database.get_db), so SQLite overwrites freed space with
zeros instead of leaving old content in the file's free space; after the
deletion the file is also compacted (VACUUM, best effort) so copies left by
earlier updates are removed too.

Scope: this is APPLICATION-LEVEL permanent deletion. It removes the
student's data from the application's active database and
application-managed storage. It does not control copies that may exist in
infrastructure backups, snapshots, previously sent emails, or external
systems, and it does not guarantee forensic destruction on the physical
storage device (see docs/ACCOUNT_DELETION.md).

Stored files (Step 7 funding documents, verified visa documents, M-PESA
screenshots, visa assistance supporting documents) are deleted only
AFTER the commit. If that fails, the database deletion stands, the failure
is recorded in the audit row, and the admin is told.

Other rules: only role 'student' accounts with a students row can be
listed or deleted (never Main Admin / Visa Admin); concurrent clicks are
serialised by BEGIN IMMEDIATE; a repeat click reports "already deleted".
"""

import json
import logging
import re
import os
from datetime import datetime, timezone

PER_PAGE = 25
REASON_MIN, REASON_MAX = 5, 500


def mask_email(email):
    """'amina.yusuf@example.com' -> 'a***@example.com' (for the audit log)."""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _like(term):
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_students(db, q="", page=1, per_page=PER_PAGE):
    """Returns (rows, total, page, pages). Only the fields needed for account
    administration are selected."""
    where = ["u.role = 'student'"]
    params = []
    q = (q or "").strip()[:100]
    if q:
        where.append("(s.full_name LIKE ? ESCAPE '\\' OR u.email LIKE ? ESCAPE '\\' OR s.phone LIKE ? ESCAPE '\\')")
        params += [_like(q)] * 3
    where_sql = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM users u JOIN students s ON s.user_id = u.id WHERE {where_sql}",
                       params).fetchone()[0]
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    rows = db.execute(
        f"""SELECT u.id AS user_id, s.id AS student_id, s.full_name, u.email, s.phone, u.created_at,
                   (SELECT COUNT(*) FROM funding_applications a WHERE a.student_id = s.id) AS applications,
                   (SELECT MAX(last_seen) FROM capacity_active_users c WHERE c.user_key = 'student:' || u.id) AS last_seen
            FROM users u JOIN students s ON s.user_id = u.id
            WHERE {where_sql}
            ORDER BY u.id DESC LIMIT ? OFFSET ?""",
        params + [per_page, (page - 1) * per_page]).fetchall()
    return [_with_activity(dict(r)) for r in rows], total, page, pages


def _with_activity(row):
    ts = row.pop("last_seen", None)
    row["last_activity"] = (datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                            if ts else None)
    row["status"] = "Active"
    return row


def get_student_account(db, user_id):
    """Account details + what a deletion would remove, or None when the id
    is not a student account."""
    row = db.execute(
        """SELECT u.id AS user_id, u.email, u.created_at, s.id AS student_id, s.full_name, s.phone, s.country,
                  (SELECT MAX(last_seen) FROM capacity_active_users c WHERE c.user_key = 'student:' || u.id) AS last_seen
           FROM users u JOIN students s ON s.user_id = u.id
           WHERE u.id = ? AND u.role = 'student'""", (user_id,)).fetchone()
    if not row:
        return None
    account = _with_activity(dict(row))
    sid = account["student_id"]
    account["applications"] = [dict(r) for r in db.execute(
        """SELECT a.reference_number, a.status, c.name AS cycle_name, a.created_at
           FROM funding_applications a LEFT JOIN funding_cycles c ON c.id = a.cycle_id
           WHERE a.student_id = ? ORDER BY a.id DESC""", (sid,)).fetchall()]
    account["impact"] = deletion_impact(db, sid, account["email"])
    account["identifiers"] = _personal_identifiers(db, account)
    return account


def _personal_identifiers(db, account):
    """This student's identifying values that must never be copied into the
    audit log's free-text reason (name, e-mail, phone, passport numbers,
    M-PESA codes, payment phone numbers)."""
    sid = account["student_id"]
    values = {account.get("email"), account.get("full_name"), account.get("phone")}
    queries = (
        ("SELECT full_name, email, phone, visa_document_passport_number FROM funding_applications WHERE student_id = ?", 1),
        ("SELECT full_name, email, phone FROM visa_requests WHERE student_id = ?", 1),
        ("""SELECT mpesa_transaction_code, transaction_reference, provider_reference, phone_number, student_name,
                   student_email, student_phone, admin_transaction_code, admin_sender_phone
            FROM visa_payments WHERE student_id = ? OR request_id IN (SELECT id FROM visa_requests WHERE student_id = ?)""", 2),
        ("SELECT account_holder_name, account_number, iban, mobile_money_number FROM student_bank_details WHERE student_id = ?", 1),
    )
    for sql, n in queries:
        for row in db.execute(sql, (sid,) * n):
            values.update(row)
    return sorted({" ".join(str(v).split()).lower() for v in values if v and len(str(v).strip()) >= 4})


_EMAIL_IN_TEXT = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_LONG_NUMBER_IN_TEXT = re.compile(r"\d(?:[\s-]?\d){6,}")        # phone / account / ID-like numbers


def deletion_impact(db, student_id, email=None):
    """How many records/files a deletion would remove (all parameterized)."""
    def count(sql, n_params=1):
        return db.execute(sql, (student_id,) * n_params).fetchone()[0]
    payments_of_student = ("(student_id = ? OR request_id IN "
                           "(SELECT id FROM visa_requests WHERE student_id = ?))")
    return {
        "applications": count("SELECT COUNT(*) FROM funding_applications WHERE student_id = ?"),
        "visa_requests": count("SELECT COUNT(*) FROM visa_requests WHERE student_id = ?"),
        "mpesa_payments": count(f"SELECT COUNT(*) FROM visa_payments WHERE {payments_of_student}", 2),
        "verified_payments": count("SELECT COUNT(*) FROM visa_payments WHERE payment_status = 'PAYMENT_VERIFIED' "
                                   f"AND {payments_of_student}", 2),
        "disbursements": count("SELECT COUNT(*) FROM funding_disbursements WHERE student_id = ?"),
        "notifications": count("SELECT COUNT(*) FROM notifications WHERE student_id = ?"),
        "stored_files": len(_stored_files(db, student_id)),
        **({"contact_messages": db.execute("SELECT COUNT(*) FROM contact_messages WHERE lower(email) = lower(?)",
                                           (email,)).fetchone()[0],
            "scam_reports": db.execute("SELECT COUNT(*) FROM scam_reports WHERE lower(reporter_email) = lower(?)",
                                       (email,)).fetchone()[0]} if email else {}),
    }


def _stored_files(db, student_id):
    """(directory key, stored name) for every personal file on disk that
    belongs to this student."""
    files = []
    for (name,) in db.execute("SELECT visa_document_path FROM funding_applications "
                              "WHERE student_id = ? AND visa_document_path IS NOT NULL", (student_id,)):
        files.append(("visa_documents", name))
    for (name,) in db.execute(
            "SELECT proof_file FROM visa_payments WHERE proof_file IS NOT NULL AND "
            "(student_id = ? OR request_id IN (SELECT id FROM visa_requests WHERE student_id = ?))",
            (student_id, student_id)):
        files.append(("payment_proofs", name))
    for (name,) in db.execute(
            "SELECT d.stored_file FROM visa_documents d JOIN visa_requests v ON v.id = d.request_id "
            "WHERE v.student_id = ? AND d.stored_file IS NOT NULL", (student_id,)):
        files.append(("visa_application_documents", name))
    # Step 7 funding documents: documents.file_path = "funding_documents/<random name>".
    # Other values in that column (legacy "uploads/demo-..." placeholders) have no
    # file behind them and are removed with their rows.
    for (path,) in db.execute(
            "SELECT d.file_path FROM documents d JOIN funding_applications a ON a.id = d.application_id "
            "WHERE a.student_id = ? AND d.file_path LIKE 'funding_documents/%'", (student_id,)):
        files.append(("funding_documents", path[len("funding_documents/"):]))
    return files


class ModerationError(Exception):
    pass


def validate_request(account, form):
    """Returns (reason, errors)."""
    errors = []
    reason = " ".join((form.get("reason") or "").split())
    if len(reason) < REASON_MIN:
        errors.append(f"Please enter a reason of at least {REASON_MIN} characters.")
    elif len(reason) > REASON_MAX:
        errors.append(f"The reason must be at most {REASON_MAX} characters.")
    lowered = reason.lower()
    if reason and (_EMAIL_IN_TEXT.search(reason) or _LONG_NUMBER_IN_TEXT.search(reason)
                   or any(v in lowered for v in account.get("identifiers", ()))):
        errors.append("The reason is kept in the audit log, so it must not contain the student's name, e-mail, "
                      "phone number, passport number, payment codes or other personal details. "
                      "Describe the reason in general terms.")
    typed = (form.get("confirm_email") or "").strip().lower()
    if typed != (account["email"] or "").strip().lower():
        errors.append("To confirm, type the student's e-mail address exactly.")
    if form.get("confirm_permanent") != "yes":
        errors.append("Tick the box confirming you understand the deletion is permanent.")
    return reason, errors


def delete_student_account(db, user_id, admin_user_id, admin_email, reason, upload_dirs):
    """Permanently deletes a STUDENT account.

    Returns ('deleted', details) | ('not_found', None) | ('refused', message).
    Raises on unexpected database errors after rolling back (nothing is
    deleted in that case).
    """
    db.commit()                                  # end any implicit transaction first
    if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ModerationError("Foreign keys are off on this connection; refusing to delete.")
    db.execute("PRAGMA secure_delete = ON")      # overwrite deleted content, don't leave it in free pages
    db.execute("BEGIN IMMEDIATE")                # serialises concurrent deletions
    try:
        target = db.execute(
            """SELECT u.id, u.email, s.id AS student_id FROM users u JOIN students s ON s.user_id = u.id
               WHERE u.id = ? AND u.role = 'student'""", (user_id,)).fetchone()
        if target is None:
            db.rollback()
            return "not_found", None
        if db.execute("SELECT 1 FROM admins WHERE user_id = ? UNION SELECT 1 FROM visa_admins WHERE user_id = ?",
                      (user_id, user_id)).fetchone():
            db.rollback()
            return "refused", "Administrator accounts cannot be deleted here."

        student_id, email = target["student_id"], target["email"]
        impact = deletion_impact(db, student_id)
        files = _stored_files(db, student_id)
        ids = _linked_ids(db, student_id)

        # Personal records tied only by e-mail address (no foreign key).
        impact["contact_messages"] = db.execute(
            "DELETE FROM contact_messages WHERE lower(email) = lower(?)", (email,)).rowcount
        impact["scam_reports"] = db.execute(
            "DELETE FROM scam_reports WHERE lower(reporter_email) = lower(?)", (email,)).rowcount
        db.execute("DELETE FROM capacity_active_users WHERE user_key = ?", (f"student:{user_id}",))

        # The account itself; ON DELETE CASCADE removes everything linked to it.
        cur = db.execute("DELETE FROM users WHERE id = ? AND role = 'student'", (user_id,))
        if cur.rowcount != 1:
            db.rollback()
            return "not_found", None

        left = _leftovers(db, user_id, ids)
        if left or db.execute("PRAGMA foreign_key_check").fetchall():
            raise ModerationError(f"Related records were not fully removed: {sorted(left)}")

        details = {"removed": impact, "files": {"to_delete": len(files)}}
        audit_id = _write_audit(db, admin_user_id, admin_email, reason, details)
        db.commit()
    except Exception:
        db.rollback()
        raise

    # Files only after the database change is permanent.
    removed = missing = failed = 0
    for key, name in files:
        path = _safe_path(upload_dirs.get(key), name)
        if path is None:
            failed += 1                                    # unexpected name: never touched, reported
            continue
        try:
            os.remove(path)
        except FileNotFoundError:
            missing += 1
            continue
        except OSError:
            failed += 1
            continue
        if os.path.exists(path):                           # verify it is really gone
            failed += 1
        else:
            removed += 1
    details["files"].update(removed=removed, already_missing=missing, failed=failed)
    try:
        db.execute("UPDATE admin_audit_log SET details = ? WHERE id = ?", (json.dumps(details), audit_id))
        db.commit()
    except Exception:  # noqa: BLE001 - the deletion itself already succeeded
        pass

    # Rebuild the database file without its free space, so copies of this
    # student's rows left there by earlier updates (e.g. written before
    # secure_delete was on for every connection) are gone too. Best effort:
    # if another request holds the database, it is skipped and recorded -
    # the deletion itself has already succeeded.
    if not _compact(db):
        logging.getLogger(__name__).warning(
            "Account deletion for user %s succeeded; database compaction (VACUUM) was skipped "
            "because the database was busy. It runs again on the next deletion.", user_id)
    return "deleted", details


def _write_audit(db, admin_user_id, admin_email, reason, details):
    """One DE-IDENTIFIED audit row: which admin deleted a student account, when,
    why (the reason is validated to contain no personal details) and how many
    records/files were removed. It deliberately stores NOTHING that identifies
    the deleted student - no e-mail (masked or not), name, or user/student id -
    so no student-specific record survives the deletion."""
    cur = db.execute(
        """INSERT INTO admin_audit_log (admin_user_id, admin_email, action, target_user_id,
               target_student_id, target_label, reason, details)
           VALUES (?, ?, 'delete_student_account', NULL, NULL, NULL, ?, ?)""",
        (admin_user_id, admin_email, reason, json.dumps(details)))
    return cur.lastrowid


def _compact(db):
    try:
        db.commit()
        db.execute("VACUUM")
        return True
    except Exception:  # noqa: BLE001 - e.g. database busy; never fails the deletion
        return False


def _safe_path(base, name):
    """Absolute path of a stored file ONLY if `name` is a plain file name that
    resolves inside `base` (no directories, no '..', no hidden files)."""
    if not base or not name or name != os.path.basename(name) or name.startswith("."):
        return None
    root = os.path.realpath(base)
    path = os.path.realpath(os.path.join(root, name))
    return path if os.path.dirname(path) == root else None


def _linked_ids(db, student_id):
    """Ids of the student's own rows, used to prove nothing is left after the delete."""
    apps = [r[0] for r in db.execute("SELECT id FROM funding_applications WHERE student_id = ?", (student_id,))]
    reqs = [r[0] for r in db.execute("SELECT id FROM visa_requests WHERE student_id = ?", (student_id,))]
    matches = [r[0] for r in db.execute(
        "SELECT id FROM funding_matches WHERE application_id IN (SELECT id FROM funding_applications WHERE student_id = ?)",
        (student_id,))]
    referrals = [r[0] for r in db.execute(
        "SELECT id FROM provider_referrals WHERE match_id IN (SELECT id FROM funding_matches WHERE application_id "
        "IN (SELECT id FROM funding_applications WHERE student_id = ?))", (student_id,))]
    return {"students": [student_id], "funding_applications": apps, "visa_requests": reqs,
            "funding_matches": matches, "provider_referrals": referrals}


def _leftovers(db, user_id, ids):
    """Every row, in any table, that still references the deleted user or
    one of the student's rows via a foreign key (should be none)."""
    ids = dict(ids, users=[user_id])
    left = {}
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        for fk in db.execute(f'PRAGMA foreign_key_list("{t}")').fetchall():
            parent, col = fk[2], fk[3]
            for value in ids.get(parent, []):
                n = db.execute(f'SELECT COUNT(*) FROM "{t}" WHERE "{col}" = ?', (value,)).fetchone()[0]
                if n:
                    left[f"{t}.{col}"] = left.get(f"{t}.{col}", 0) + n
    for t, key in (("students", "students"), ("users", "users")):
        for value in ids[key]:
            if db.execute(f'SELECT COUNT(*) FROM "{t}" WHERE id = ?', (value,)).fetchone()[0]:
                left[f"{t}.id"] = 1
    return left


def recent_audit(db, limit=20):
    return [dict(r) for r in db.execute(
        """SELECT created_at, admin_email, action, target_user_id, target_student_id, target_label, reason, details
           FROM admin_audit_log ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()]
