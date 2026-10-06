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
| The student's uploaded files are removed from `uploads/funding_documents/`, `uploads/visa_documents/`, `uploads/payment_proofs/` and `uploads/visa_application_documents/`. | Emails already sent to the student or to admins (confirmations, notifications, alerts). |
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

**Files**, deleted only after the database commit succeeds, each one checked afterwards to be really gone:
`UPLOAD_ROOT/funding_documents/<name>` (Step 7 funding documents, from `documents.file_path = 'funding_documents/<name>'`),
`UPLOAD_ROOT/visa_documents/<name>` (verified visas), `UPLOAD_ROOT/payment_proofs/<name>` (M-PESA screenshots) and
`UPLOAD_ROOT/visa_application_documents/<name>` (passport-size photo, National ID and other visa supporting documents)
(on Render, `UPLOAD_ROOT=/var/data/uploads`).
These are the only folders the app stores files in. Only files listed against this student's own rows are
removed - never a whole folder - and every name must be a plain file name inside its folder. Other values in
`documents.file_path`, `visa_documents.file_path` and the receipt-path columns are text placeholders with no
file behind them; they are deleted with their rows. A file that is already missing is counted, not an error.

## Registering again after deletion

After deletion, the same person can register again with the same email and/or phone. They get a completely new account.

- **Email:** unique among existing accounts through `users UNIQUE(email, role)` and the register check. The deleted `users` row no longer exists, so the email is free.
- **Phone:** not a uniqueness rule. Active students may share a phone.
- **Ids:** every table uses `AUTOINCREMENT`, so the new account always gets new user, student, application and visa ids. Old ids are never reused, and nothing looks up student data by email or phone. Old data therefore cannot attach to the new account, and an old session cookie cannot reach it.
- **Audit log:** registration never reads `admin_audit_log`, and the de-identified deletion record cannot be linked to the person anyway, so it never blocks sign-up.

## What is kept: a de-identified audit record

One row in `admin_audit_log` records the ADMIN's action, with nothing that identifies the deleted student:

- date and time;
- admin user id and admin email;
- action (`delete_student_account`);
- the admin's reason;
- counts of removed records and files.

`target_user_id`, `target_student_id` and `target_label` are always empty (NULL): no student e-mail (masked or
not), name, phone, user or student id, file names, passport or visa numbers, M-PESA details or application answers.
Audit rows written by earlier versions (which held a masked e-mail and the ids) are cleared the same way on
startup. The reason is rejected if it contains the student's name, email, phone, passport number or payment
codes, or any email address or long number. The admin is asked to describe the reason in general terms.

## What the application cannot delete

| Data | Why | Where | Personal data? |
|---|---|---|---|
| Disk snapshots / backups | Controlled by the hosting provider, not the app | Render (persistent disk backups) | Yes, until they expire |
| Server logs | Written to the hosting provider's log stream; the app cannot edit them. The app never writes a full phone number or e-mail address to the logs: Paystack/M-PESA lines show a masked number (e.g. `07******78`, `+254*******78`) and mail errors a masked address (e.g. `a***@example.com`). Lines can contain payment references and internal ids | Render logs | Only masked fragments and references, until log retention expires |
| Payment-provider records | Kept by Paystack / Safaricom under their own rules | Paystack, M-PESA | Yes |
| Emails already sent | Delivered to inboxes | Recipients' mailboxes | Yes |

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
