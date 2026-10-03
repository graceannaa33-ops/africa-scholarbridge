"""
paystack_lib.py
---------------
Small, dependency-free Paystack client for the visa assistance fee.

Keys come ONLY from environment variables (set them in Render):
    PAYSTACK_SECRET_KEY   server-side only - never sent to the browser,
                          never logged, never written to the database
    PAYSTACK_PUBLIC_KEY   optional here (the integration is fully
                          server-side: Charge API + hosted checkout)

Nothing in this module decides that a visa request is paid. It only talks
to Paystack; app.py marks a payment successful ONLY after verify() returns
status "success" with the expected amount and currency.

Docs: https://paystack.com/docs/api/  (transaction/initialize, charge,
transaction/verify, webhooks with x-paystack-signature = HMAC-SHA512 of the
raw body using the secret key).
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.error
import urllib.request

API_BASE = "https://api.paystack.co"
TIMEOUT_SECONDS = 20
CURRENCY = "KES"

# Paystack transaction statuses (data.status) -> our gateway_status.
_STATUS_MAP = {
    "success": "successful",
    "failed": "failed",
    "reversed": "failed",
    "abandoned": "abandoned",
}
GATEWAY_STATUS_LABELS = {
    "pending": "Pending - waiting for payment",
    "successful": "Successful (verified with Paystack)",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "abandoned": "Abandoned (not completed)",
}


def secret_key():
    return (os.environ.get("PAYSTACK_SECRET_KEY") or "").strip()


def public_key():
    return (os.environ.get("PAYSTACK_PUBLIC_KEY") or "").strip()


def is_configured():
    """True when the secret key is set (all API calls need it)."""
    return bool(secret_key())


def is_test_mode():
    return secret_key().startswith("sk_test_")


def to_subunit(amount):
    """KSh 1,500 -> 150000 (Paystack amounts are in the currency subunit)."""
    return int(round(float(amount) * 100))


def new_reference(request_id):
    """Unique per attempt; only letters, digits, '-', '.' and '='."""
    return f"ASB-VISA-{int(request_id)}-{secrets.token_hex(8).upper()}"


def gateway_status(paystack_status):
    """Maps Paystack data.status to pending/successful/failed/abandoned.
    Anything unknown (ongoing, pending, processing, queued, send_otp,
    pay_offline, ...) stays 'pending' - never 'successful'."""
    return _STATUS_MAP.get((paystack_status or "").lower(), "pending")


def normalize_kenyan_phone(raw):
    """'0712 345 678', '254712345678', '+254712345678' -> '+254712345678'.
    Accepts Safaricom-style 07xx / 01xx numbers. Returns None if invalid."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("254") and len(digits) == 12:
        digits = "0" + digits[3:]
    elif len(digits) == 9 and digits[0] in "71":
        digits = "0" + digits
    if not re.fullmatch(r"0[71]\d{8}", digits):
        return None
    return "+254" + digits[1:]


def valid_signature(raw_body, signature):
    """Webhook check: x-paystack-signature must equal HMAC-SHA512 of the
    exact raw request body, keyed with the secret key."""
    key = secret_key()
    if not key or not signature or raw_body is None:
        return False
    expected = hmac.new(key.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


class PaystackError(Exception):
    """Network/API failure. The message is safe to log (no key)."""


def _call(method, path, body=None):
    key = secret_key()
    if not key:
        raise PaystackError("Paystack is not configured.")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API_BASE + path, data=data, method=method, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "AfricaScholarBridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        raise PaystackError(f"Paystack HTTP {exc.code}: {str(payload.get('message') or '')[:200]}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise PaystackError(f"Could not reach Paystack ({type(exc).__name__}).") from None
    if not isinstance(payload, dict) or not payload.get("status"):
        raise PaystackError(f"Paystack refused the request: {str((payload or {}).get('message') or '')[:200]}")
    return payload.get("data") or {}


def initialize_checkout(email, amount_subunit, reference, callback_url, metadata=None):
    """Hosted Paystack checkout (M-PESA and card). Returns authorization_url."""
    data = _call("POST", "/transaction/initialize", {
        "email": email, "amount": str(int(amount_subunit)), "currency": CURRENCY, "reference": reference,
        "callback_url": callback_url, "channels": ["mobile_money", "card"],
        "metadata": json.dumps(metadata or {}),
    })
    url = data.get("authorization_url") or ""
    if not url.startswith("https://"):
        raise PaystackError("Paystack did not return a checkout link.")
    return url


def charge_mpesa(email, amount_subunit, reference, phone_e164, metadata=None):
    """Kenya M-PESA via the Charge API: Paystack sends an M-PESA prompt to
    the phone. Returns (paystack_status, display_text)."""
    data = _call("POST", "/charge", {
        "email": email, "amount": str(int(amount_subunit)), "currency": CURRENCY, "reference": reference,
        "mobile_money": {"phone": phone_e164, "provider": "mpesa"}, "metadata": metadata or {},
    })
    return str(data.get("status") or ""), str(data.get("display_text") or data.get("message") or "")


def verify(reference):
    """GET /transaction/verify/:reference -> the transaction 'data' dict
    (status, amount, currency, reference, paid_at, channel, gateway_response)."""
    if not re.fullmatch(r"[A-Za-z0-9.=\-]{1,100}", reference or ""):
        raise PaystackError("Invalid reference.")
    return _call("GET", f"/transaction/verify/{reference}")
