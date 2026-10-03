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
import logging
import os
import re
import secrets
import urllib.error
import urllib.request

API_BASE = "https://api.paystack.co"
# Child of Flask's "app" logger (app = Flask(__name__) in app.py), so these
# lines reach the same output (Render logs) in the same format.
log = logging.getLogger("app.paystack")
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


def mask_phone(phone):
    """'+254712345678' -> '+2547******78' (for logs)."""
    phone = str(phone or "")
    return phone[:5] + "*" * max(len(phone) - 7, 0) + phone[-2:] if len(phone) > 7 else "***"


# Fields of Paystack's `data` object that are safe to keep and log: they
# describe the charge, never the customer's credentials (no PIN/OTP/card).
_SAFE_DATA_FIELDS = ("status", "message", "gateway_response", "reference", "display_text")


def _safe_data(data):
    if not isinstance(data, dict):
        return {}
    return {k: str(data[k])[:200] for k in _SAFE_DATA_FIELDS if data.get(k) not in (None, "")}


class PaystackError(Exception):
    """Network/API failure. Carries Paystack's own answer (safe fields
    only) so the real reason can be logged and shown. Never holds the key."""

    def __init__(self, message, http_status=None, api_status=None, api_message=None, data=None,
                 code=None, error_type=None, next_step=None):
        super().__init__(message)
        self.http_status, self.api_status, self.api_message = http_status, api_status, api_message
        self.data = _safe_data(data)
        self.code, self.error_type, self.next_step = code, error_type, next_step

    @property
    def charge_attempted(self):
        """Paystack created/attempted a transaction for this reference
        (it returned the charge's own status or reference), so its final
        state must be read with verify() - not assumed."""
        return bool(self.data.get("status") or self.data.get("reference"))

    @property
    def reason(self):
        """Paystack's most specific explanation, safe to show the student."""
        return (self.data.get("message") or self.data.get("gateway_response") or self.api_message or "")[:200]

    def diagnostics(self):
        """One log line with every safe field Paystack returned."""
        parts = [f"http={self.http_status}", f"status={self.api_status}", f"message={self.api_message!r}"]
        parts += [f"{k}={v!r}" for k, v in (("type", self.error_type), ("code", self.code),
                                            ("next_step", self.next_step)) if v]
        parts += [f"data.{k}={v!r}" for k, v in self.data.items()]
        return _mask_in_text(" ".join(parts))          # no full phone number, even inside messages


_PHONE_LIKE = re.compile(r"(?<![\w\-+])\+?\d[\d ]{7,14}\d(?![\w\-])")   # standalone numbers only


def _mask_in_text(value):
    """Masks anything that looks like a phone number inside a logged string."""
    return _PHONE_LIKE.sub(lambda m: mask_phone(re.sub(r"[^\d+]", "", m.group())), str(value))


def log_http_error_diagnostics(err):
    """TEMPORARY diagnostic line for Paystack HTTP errors (e.g. HTTP 400 on
    /charge). Logs only Paystack's answer - never the key, the
    Authorization header, PIN/OTP or a full phone number."""
    d = err.data
    fields = (("http", err.http_status), ("status", err.api_status), ("message", err.api_message),
              ("data_status", d.get("status")), ("data_message", d.get("message")),
              ("gateway_response", d.get("gateway_response")), ("reference", d.get("reference")))
    log.warning("Paystack HTTP error diagnostics: %s",
                " ".join(f"{k}={_mask_in_text(v)!r}" if isinstance(v, str) else f"{k}={v!r}" for k, v in fields))


def _error_from_payload(prefix, http_status, payload):
    payload = payload if isinstance(payload, dict) else {}
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    api_message = str(payload.get("message") or "")[:200]
    return PaystackError(f"{prefix}: {api_message}", http_status=http_status, api_status=payload.get("status"),
                         api_message=api_message, data=payload.get("data"),
                         code=str(payload.get("code") or "")[:80] or None,
                         error_type=str(payload.get("type") or "")[:80] or None,
                         next_step=str(meta.get("nextStep") or "")[:200] or None)


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
        # Keep Paystack's whole (safe) answer: for a charge, the real reason
        # is in data.status / data.message / data.gateway_response, while the
        # top-level message is often just "Charge attempted".
        err = _error_from_payload(f"Paystack HTTP {exc.code}", exc.code, payload)
        log_http_error_diagnostics(err)
        raise err from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise PaystackError(f"Could not reach Paystack ({type(exc).__name__}).") from None
    if not isinstance(payload, dict) or payload.get("status") is not True:
        raise _error_from_payload("Paystack refused the request", 200, payload)
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
    the phone. A started charge comes back as status true, "Charge
    attempted", data.status "pay_offline" - that only means the prompt was
    SENT; it is never proof of payment.
    Returns {"status", "display_text", "reference"} from Paystack's data."""
    data = _call("POST", "/charge", {
        "email": email, "amount": str(int(amount_subunit)), "currency": CURRENCY, "reference": reference,
        "mobile_money": {"phone": phone_e164, "provider": "mpesa"}, "metadata": metadata or {},
    })
    return {"status": str(data.get("status") or ""),
            "display_text": str(data.get("display_text") or data.get("message") or "")[:300],
            "reference": str(data.get("reference") or "")[:100]}


def verify(reference):
    """GET /transaction/verify/:reference -> the transaction 'data' dict
    (status, amount, currency, reference, paid_at, channel, gateway_response)."""
    if not re.fullmatch(r"[A-Za-z0-9.=\-]{1,100}", reference or ""):
        raise PaystackError("Invalid reference.")
    return _call("GET", f"/transaction/verify/{reference}")
