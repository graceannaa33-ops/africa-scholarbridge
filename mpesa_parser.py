"""
mpesa_parser.py
---------------
Reads the text of an M-PESA confirmation SMS and pulls out the useful
fields (transaction code, amount, counterparty name/phone, date, time).

!!! PARSING IS NOT PAYMENT VERIFICATION !!!
An SMS pasted into a web form is just text typed by a user. It can be
edited, invented, or copied from someone else's phone. Nothing in this
module talks to Safaricom, and nothing here can prove money moved. The
output is used only to:
  * show the student what the system detected,
  * reject obviously wrong submissions (wrong amount, duplicate code,
    not an M-PESA message at all),
  * give the admin a side-by-side comparison with the message that
    actually arrived on the receiving phone.
A human admin still has to click VERIFY PAYMENT. See the README section
"Manual M-PESA verification" for how an official Daraja integration
would later replace that manual step.

Two common message shapes are recognised (plus a generic fallback):

  SENT (on the payer's phone):
    UIR1N8OV43 Confirmed. Ksh 1500.00 sent to JOYCE BAARIU 0181785792
    on 27/9/26 at 9:56 PM. New M-PESA balance is ...

  RECEIVED (on the recipient's phone):
    UIR1N8OSFE Confirmed. You have received Ksh1500.00 from EDWARD DAVID
    0758***959 on 27/9/26 at 10:10 PM. New M-PESA balance is ...

Pure Python, standard library only, no Flask imports - so it can be unit
tested on its own and reused by a future Daraja integration.
"""

import re
from decimal import Decimal, InvalidOperation

MAX_MESSAGE_LENGTH = 1200  # a real M-PESA SMS is ~300 chars; this leaves room for promos

# M-PESA receipt numbers are 10 upper-case letters/digits (e.g. UIR1N8OV43).
_CODE_AT_START = re.compile(r"^\s*([A-Z0-9]{10})\b\s*(?:Confirmed|confirmed|CONFIRMED)?")
_CODE_BEFORE_CONFIRMED = re.compile(r"\b([A-Z0-9]{10})\s+Confirmed\b", re.IGNORECASE)

# "Ksh 1500.00", "Ksh1,500.00", "KES 1,500", "Ksh.1500"
_AMOUNT = r"(?:Ksh|KES|Kshs)\.?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"

# "on 27/9/26 at 9:56 PM" (also 27/09/2026, 9:56PM, 21:56)
_DATE = r"(\d{1,2}/\d{1,2}/\d{2,4})"
_TIME = r"(\d{1,2}:\d{2}(?:\s*[AaPp][Mm])?)"
_ON_DATE_AT_TIME = re.compile(r"\bon\s+" + _DATE + r"\s+at\s+" + _TIME)

# A phone number, possibly masked by Safaricom: 0181785792, 254712345678,
# +254712345678, 0758***959, 07******59
_PHONE = r"(\+?\d[\d\*]{5,13}\d)"

_SENT = re.compile(
    _AMOUNT + r"\s+(?:sent|paid)\s+to\s+(.+?)"
    r"(?:\s+" + _PHONE + r")?"
    r"(?:\s+for\s+account\s+(\S+?))?"
    r"\.?\s+on\s+" + _DATE,
    re.IGNORECASE,
)
_RECEIVED = re.compile(
    r"(?:You\s+have\s+)?received\s+" + _AMOUNT + r"\s+from\s+(.+?)"
    r"(?:\s+" + _PHONE + r")?"
    r"\.?\s+on\s+" + _DATE,
    re.IGNORECASE,
)


def _clean_amount(raw):
    if raw is None:
        return None
    try:
        return Decimal(raw.replace(",", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, AttributeError):
        return None


def _clean_name(raw):
    if not raw:
        return None
    name = re.sub(r"\s+", " ", raw).strip(" .,-")
    return name or None


def normalize_phone(raw):
    """Turn 0712345678 / 254712345678 / +254 712 345 678 into 0712345678.
    Returns None for empty input. Masked numbers (with *) are returned
    as-is (digits and * only) because they cannot be normalised."""
    if not raw:
        return None
    s = re.sub(r"[^\d\*]", "", str(raw))
    if "*" in s:
        return s
    if s.startswith("254") and len(s) == 12:
        s = "0" + s[3:]
    elif len(s) == 9 and s[0] in "17":
        s = "0" + s
    return s or None


def phones_match(a, b):
    """True/False when both numbers are fully visible, None when either is
    missing or masked (0758***959) and therefore can't be compared."""
    a, b = normalize_phone(a), normalize_phone(b)
    if not a or not b or "*" in a or "*" in b:
        return None
    return a == b


def masked_phone_compatible(masked, full):
    """0758***959 vs 0758123959 -> True. Used only as a weak hint."""
    masked, full = normalize_phone(masked), normalize_phone(full)
    if not masked or not full or "*" in full:
        return None
    if "*" not in masked:
        return masked == full
    prefix, _, rest = masked.partition("*")
    suffix = rest.lstrip("*")
    return full.startswith(prefix) and full.endswith(suffix)


def parse_mpesa_message(text):
    """Extract fields from an M-PESA SMS.

    Returns a dict (never raises):
      direction            'sent' | 'received' | 'unknown'
      transaction_code     'UIR1N8OV43' or None
      amount               Decimal('1500.00') or None
      counterparty_name    recipient (sent) or sender (received)
      counterparty_phone   as printed in the SMS (may be masked)
      account              paybill account number, if any
      date, time           strings exactly as printed
      has_confirmed_word   the SMS contains "Confirmed"
      looks_like_mpesa     code + amount + Confirmed + date all present
    """
    result = {
        "direction": "unknown", "transaction_code": None, "amount": None,
        "counterparty_name": None, "counterparty_phone": None, "account": None,
        "date": None, "time": None, "has_confirmed_word": False, "looks_like_mpesa": False,
    }
    if not text or not text.strip():
        return result

    text = re.sub(r"\s+", " ", text.strip())[:MAX_MESSAGE_LENGTH]
    result["has_confirmed_word"] = bool(re.search(r"\bconfirmed\b", text, re.IGNORECASE))

    m = _CODE_BEFORE_CONFIRMED.search(text) or _CODE_AT_START.search(text)
    if m:
        code = m.group(1).upper()
        # A real code mixes letters and digits; rejects things like "CONFIRMEDX".
        if re.search(r"[A-Z]", code) and re.search(r"\d", code):
            result["transaction_code"] = code

    # Only look at the part before the balance so "New M-PESA balance is
    # Ksh1,854.61" is never mistaken for the transaction amount.
    main_part = re.split(r"New\s+M-?PESA\s+balance", text, flags=re.IGNORECASE)[0]

    sent = _SENT.search(main_part)
    received = _RECEIVED.search(main_part)
    if received and (not sent or received.start() <= sent.start()):
        result.update({
            "direction": "received",
            "amount": _clean_amount(received.group(1)),
            "counterparty_name": _clean_name(received.group(2)),
            "counterparty_phone": received.group(3),
        })
    elif sent:
        result.update({
            "direction": "sent",
            "amount": _clean_amount(sent.group(1)),
            "counterparty_name": _clean_name(sent.group(2)),
            "counterparty_phone": sent.group(3),
            "account": sent.group(4),
        })
    else:
        # Unknown wording: fall back to the first amount in the message.
        am = re.search(_AMOUNT, main_part, re.IGNORECASE)
        if am:
            result["amount"] = _clean_amount(am.group(1))

    dt = _ON_DATE_AT_TIME.search(main_part)
    if dt:
        result["date"] = dt.group(1)
        result["time"] = re.sub(r"\s*([AaPp][Mm])$", r" \1", dt.group(2)).upper()

    result["looks_like_mpesa"] = bool(
        result["transaction_code"] and result["amount"] is not None
        and result["has_confirmed_word"] and result["date"]
    )
    return result


# ---------------------------------------------------------------------
# Submission checks for the STUDENT side. Returns a list of flags:
#   {"code": ..., "severity": "error"|"warning"|"ok", "message": ...}
# "error"   -> the submission is refused, the student is told why.
# "warning" -> accepted, but highlighted for the admin to look at.
# "ok"      -> a check that passed (shown to the admin as a green tick).
# None of these outcomes ever marks a payment verified.
# ---------------------------------------------------------------------
def validate_student_message(parsed, expected_amount, expected_phone):
    flags = []

    def add(code, severity, message):
        flags.append({"code": code, "severity": severity, "message": message})

    if parsed["direction"] == "received":
        add("wrong_direction", "error",
            "This looks like a 'You have received' message. Please paste the confirmation "
            "you received after SENDING the payment.")

    if not parsed["transaction_code"]:
        add("no_code", "error", "No M-PESA transaction code was found in the message.")
    else:
        add("code_found", "ok", f"Transaction code detected: {parsed['transaction_code']}")

    if parsed["amount"] is None:
        add("no_amount", "error", "No payment amount was found in the message.")
    elif parsed["amount"] != Decimal(str(expected_amount)).quantize(Decimal("0.01")):
        add("wrong_amount", "error",
            f"The message shows KSh {parsed['amount']:,.2f}, but the fee is exactly "
            f"KSh {Decimal(str(expected_amount)):,.0f}. Please contact support if you paid a different amount.")
    else:
        add("amount_ok", "ok", f"Amount matches the fee (KSh {parsed['amount']:,.2f}).")

    if not parsed["looks_like_mpesa"]:
        add("structure", "error" if not parsed["transaction_code"] else "warning",
            "The message does not have the usual M-PESA confirmation structure "
            "(code, 'Confirmed', amount, date).")
    else:
        add("structure_ok", "ok", "Message has a normal M-PESA confirmation structure.")

    if parsed["direction"] == "sent":
        match = phones_match(parsed["counterparty_phone"], expected_phone)
        if match is True:
            add("recipient_ok", "ok", "Recipient phone matches the configured M-PESA number.")
        elif match is False:
            add("recipient_mismatch", "warning",
                f"Recipient phone {parsed['counterparty_phone']} does NOT match the configured "
                f"M-PESA number {expected_phone}.")
        else:
            add("recipient_unknown", "warning",
                "The message does not show the recipient's full phone number, so it could not be compared.")
    elif parsed["direction"] == "unknown" and parsed["transaction_code"]:
        add("direction_unknown", "warning", "Could not tell who the money was sent to from this message.")

    if not parsed["date"]:
        add("no_datetime", "warning", "No transaction date/time was found in the message.")
    return flags


def has_errors(flags):
    return any(f["severity"] == "error" for f in flags)


# ---------------------------------------------------------------------
# Admin matching aid: student's SENT message vs the RECEIVED message the
# admin pasted from the receiving phone. Indicators only - never used to
# set a payment status automatically.
# ---------------------------------------------------------------------
def compare_student_and_incoming(student, incoming, expected_amount):
    """`student` / `incoming` are dicts with keys transaction_code, amount,
    date, time, name, phone (any may be None). Returns a list of
    {"label", "state": "match"|"mismatch"|"unknown", "detail"}."""
    rows = []

    def row(label, state, detail=""):
        rows.append({"label": label, "state": state, "detail": detail})

    def dec(v):
        if v in (None, ""):
            return None
        try:
            return Decimal(str(v)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None

    s_code, i_code = (student.get("transaction_code") or "").upper(), (incoming.get("transaction_code") or "").upper()
    if s_code and i_code:
        row("Transaction code matches", "match" if s_code == i_code else "mismatch", f"{s_code} vs {i_code}")
    else:
        row("Transaction code matches", "unknown", "Both codes are needed to compare.")

    s_amt, i_amt, exp = dec(student.get("amount")), dec(incoming.get("amount")), dec(expected_amount)
    if s_amt is not None and i_amt is not None:
        ok = s_amt == i_amt == exp
        row("Amount matches", "match" if ok else "mismatch", f"KSh {s_amt:,.2f} vs KSh {i_amt:,.2f} (fee KSh {exp:,.2f})")
    elif i_amt is not None:
        row("Amount matches", "match" if i_amt == exp else "mismatch", f"Incoming KSh {i_amt:,.2f} (fee KSh {exp:,.2f})")
    else:
        row("Amount matches", "unknown", "Incoming amount not available.")

    if student.get("date") and incoming.get("date"):
        same = student["date"] == incoming["date"] and (student.get("time") or "") == (incoming.get("time") or "")
        row("Date/time matches", "match" if same else "mismatch",
            f"{student['date']} {student.get('time') or ''} vs {incoming['date']} {incoming.get('time') or ''}")
    else:
        row("Date/time available", "unknown" if not incoming.get("date") else "match",
            "Incoming date/time available." if incoming.get("date") else "Incoming date/time not available.")

    row("Sender information available", "match" if incoming.get("name") else "unknown",
        f"{incoming.get('name') or '—'} {incoming.get('phone') or ''}".strip())
    row("Recipient information available", "match" if student.get("name") else "unknown",
        f"{student.get('name') or '—'} {student.get('phone') or ''}".strip())

    if incoming.get("phone") and student.get("payer_phone"):
        hint = masked_phone_compatible(incoming["phone"], student["payer_phone"])
        if hint is not None:
            row("Sender phone fits student's paying number", "match" if hint else "mismatch",
                f"{incoming['phone']} vs {student['payer_phone']}")
    return rows
