"""
email_lib.py
------------
Outgoing transactional email for Africa ScholarBridge (currently just the
"your application was submitted" confirmation), sent over plain SMTP
using Python's standard library - no third-party email service required,
but any real SMTP provider (Gmail SMTP, SendGrid's SMTP relay, Mailgun,
Amazon SES SMTP, your own mail server, etc.) works by setting the
environment variables below.

This project ships with NO real mail credentials (there are none to ship
safely) - `is_configured()` returns False out of the box, mirroring the
same honest pattern mpesa.py uses for M-Pesa. When unconfigured, sending
an email fails the same way a real network error would: the caller in
app.py records that failure as `confirmation_email_status = 'FAILED'`
and tells the student their APPLICATION still submitted successfully,
never that anything about the application itself failed.

Required environment variables (never hard-code these, never send them
to the browser/frontend, never commit them to source control):

    MAIL_SERVER            e.g. smtp.gmail.com
    MAIL_PORT              e.g. 587
    MAIL_USERNAME           the SMTP account username
    MAIL_PASSWORD           the SMTP account password / app password
    MAIL_DEFAULT_SENDER     the "From" address, e.g. "Africa ScholarBridge <no-reply@africascholarbridge.org>"

Optional:
    MAIL_USE_TLS = "1" (default) or "0"   - STARTTLS on the SMTP connection
"""

import os
import re
import smtplib
import ssl
import logging
from email.message import EmailMessage

logger = logging.getLogger("africa_scholarbridge.email")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def is_configured():
    """True only when every credential needed to actually send mail is
    present. The platform never pretends to be configured when it isn't -
    an unconfigured mail service simply means every send fails honestly
    (recorded as confirmation_email_status = 'FAILED'), the same way a
    real SMTP outage would.
    """
    return all([_env("MAIL_SERVER"), _env("MAIL_PORT"), _env("MAIL_USERNAME"),
                _env("MAIL_PASSWORD"), _env("MAIL_DEFAULT_SENDER")])


def is_valid_email(address):
    """A light sanity check before we ever attempt to send - not a full
    RFC 5322 validator, just enough to reject obviously-broken addresses
    before spending an SMTP round trip on them."""
    return bool(address) and bool(_EMAIL_RE.match(address.strip()))


def application_confirmation_email(student_name, reference_number, cycle_name, submitted_date):
    """Builds the (subject, plain-text body) for the post-submission
    confirmation email. Deliberately says ONLY that the application was
    received - never that funding was approved, awarded, or guaranteed
    (see the DO NOT CLAIM FUNDING APPROVAL requirement). A real funding
    decision is always a separate, later notification.
    """
    subject = "Africa ScholarBridge — Application Submitted Successfully"
    body = f"""Hello {student_name},

Your Africa ScholarBridge annual funding application has been successfully submitted.

Application Reference: {reference_number}
Application Cycle: {cycle_name}
Submission Date: {submitted_date}

You can log in to your Africa ScholarBridge account to view your application status and any future updates.

Please keep your application reference for your records.

Africa ScholarBridge
Helping students discover education funding opportunities.
"""
    return subject, body


def send_email(to_address, subject, body):
    """Sends one plain-text email. Returns (True, None) on success, or
    (False, error_message) on any failure - including "not configured".
    Never raises: a mail problem must never take down the request that
    called it (see how app.py uses this - the application stays
    submitted either way). The technical error is logged server-side
    only, never shown to the student and never containing credentials.
    """
    if not is_valid_email(to_address):
        logger.warning("Refusing to send email: invalid address format.")
        return False, "invalid_recipient"

    if not is_configured():
        logger.info("Email not sent: MAIL_* environment variables are not configured.")
        return False, "not_configured"

    server_host = _env("MAIL_SERVER")
    try:
        port = int(_env("MAIL_PORT"))
    except ValueError:
        logger.error("Email not sent: MAIL_PORT is not a valid number.")
        return False, "bad_port"
    username = _env("MAIL_USERNAME")
    password = _env("MAIL_PASSWORD")
    sender = _env("MAIL_DEFAULT_SENDER")
    use_tls = _env("MAIL_USE_TLS", "1") != "0"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_address
    msg.set_content(body)

    try:
        with smtplib.SMTP(server_host, port, timeout=15) as smtp:
            if use_tls:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(username, password)
            smtp.send_message(msg)
        return True, None
    except Exception as exc:  # noqa: BLE001 - we deliberately want to catch and log any SMTP failure
        # Log the technical detail server-side only - never expose SMTP
        # errors (which can include hostnames/usernames) to the student.
        logger.error("Failed to send email via SMTP: %s", exc)
        return False, "send_failed"
