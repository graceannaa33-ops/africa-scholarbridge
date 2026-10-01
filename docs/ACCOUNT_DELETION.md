# Permanent student account deletion

Main Admin → **User Accounts** → *View details* → **Permanently Delete Account**.

## Scope (read this first)

**Permanent deletion removes the student's data from the application's active
database and application-managed storage. It does not control copies that may
exist in infrastructure backups, snapshots, previously sent emails, or external
systems.**

This is *application-level* permanent deletion. It is not a guarantee of
forensic destruction on the physical storage device.

| Application-level (done by the app) | Infrastructure-level (NOT controlled by the app) |
|---|---|
| All of the student's rows are deleted from the active SQLite database in one transaction. | Render disk snapshots/backups taken before the deletion still contain the old database and files until they expire. Check your Render dashboard for retention. |
| SQLite `secure_delete` is on for every database connection, so freed space (from deletions *and* from updates that move a row) is overwritten with zeros. After a deletion the database file is also compacted (`VACUUM`, best effort; skipped and logged if the database is busy), which removes copies left in free space by earlier updates. | Filesystem/SSD-level remnants: deleted files are unlinked (not overwritten), and the temporary SQLite rollback journal is removed after commit. The storage device may keep old blocks until they are reused. |
| The student's uploaded files are removed from `uploads/visa_documents/`, `uploads/payment_proofs/` and `uploads/visa_application_documents/`. | Emails already sent to the student or to admins (confirmations, notifications, alerts). |
| | Server/Render logs, copies someone downloaded, external systems (M-PESA, banks, providers). |

Do not tell a student that their data is "unrecoverable from every system".
Tell them it has been deleted from the application.

## What is deleted

In one database transaction (`account_moderation.delete_student_account`):

- **Account:** `users` (login, password hash) and `students` (profile).
- **Funding:** `funding_applications` (all application answers, visa details on the application, passport number and uploaded-file references), `application_history`, `documents`, `funding_matches`, `provider_referrals`, `funding_decisions`, `student_bank_details`, `funding_disbursements`, `saved_opportunities`, `notifications`.
- **Visa / M-PESA:** `visa_requests`, `visa_documents`, `visa_status_history`, `visa_notes`, `visa_fee_transactions`, `visa_admin_notifications`, `visa_payments` (M-PESA codes, messages, phone numbers, proof file names).
- **Not linked by a foreign key, matched by the account's email (any capitalisation):** `contact_messages`, `scam_reports`.
- **Monitoring:** the `capacity_active_users` entry `student:<id>`. The capacity monitor also never re-creates this entry for an account that no longer exists, for example when a stale browser cookie is used.

Before committing, the app checks every table's foreign keys for any row still pointing at the student's ids, and runs `PRAGMA foreign_key_check`. If anything is left, the whole transaction is rolled back.

**Files**, deleted only after the database commit succeeds:
`UPLOAD_ROOT/visa_documents/<name>`, `UPLOAD_ROOT/payment_proofs/<name>` and `UPLOAD_ROOT/visa_application_documents/<name>` (visa assistance supporting documents) (on Render, `UPLOAD_ROOT=/var/data/uploads`).
These are the only folders the app stores files in. The paths in `documents.file_path`, `visa_documents.file_path` and the receipt-path columns are text placeholders with no file behind them; real supporting documents use `visa_documents.stored_file`. They are deleted with their rows.

## Registering again after deletion

After deletion, the same person can register again with the same email and/or phone. They get a completely new account.

- **Email:** unique among existing accounts through `users UNIQUE(email, role)` and the register check. The deleted `users` row no longer exists, so the email is free.
- **Phone:** not a uniqueness rule. Active students may share a phone.
- **Ids:** every table uses `AUTOINCREMENT`, so the new account always gets new user, student, application and visa ids. Old ids are never reused, and nothing looks up student data by email or phone. Old data therefore cannot attach to the new account, and an old session cookie cannot reach it.
- **Audit log:** registration never reads `admin_audit_log`. The old deletion record stays, unchanged, and never blocks sign-up.

## What is kept: the audit record

One row in `admin_audit_log`:

- date and time;
- admin user id and admin email;
- action;
- the deleted user id and student id numbers;
- a masked email (`a***@example.com`);
- the admin's reason;
- counts of removed records and files.

It never contains the student's full email, name, phone, file names, passport or visa numbers, M-PESA details or application answers. The reason is rejected if it contains the student's name, email, phone, passport number or payment codes, or any email address or long number. The admin is asked to describe the reason in general terms.

## Failure behaviour

| Situation | Result |
|---|---|
| Database error at any point | Full rollback. Nothing is deleted, including files. The admin sees "Nothing was changed." |
| Something still linked after the delete | Full rollback (same as above). |
| A file can't be removed after commit | The database deletion stands. The admin sees a warning with the number of files, and the audit row records `files.failed`. Someone with server access must remove the file manually. |
| Double click, or simultaneous requests | Serialised by `BEGIN IMMEDIATE`. Exactly one deletion and one audit row; the others see "already deleted". |
| Invalid or missing CSRF token, expired admin session, removed admin, student, Visa Admin, GET request | Rejected. Nothing is deleted. |

## Protections

- Main Admin only.
- POST only.
- CSRF token.
- Exact email confirmation.
- "I understand this deletion is permanent" checkbox.
- Required reason (5–500 characters, no personal details).
- Parameterized SQL.
- Atomic transaction.
- Main Admin and Visa Admin accounts can't be deleted here.

Tests: `tests/test_account_moderation.py`.
