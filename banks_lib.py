"""
banks_lib.py
------------
Shared helpers and reference data for the African Bank Directory &
Funding Payout Information feature: the full list of African countries,
account-type choices, and small utilities like masking an account number.

Keeping this in its own module (mirroring visa.py, mpesa.py, matching.py)
keeps app.py focused on routes rather than reference data.
"""

# ---------------------------------------------------------------------
# All 54 internationally recognized African countries, with their ISO
# 3166-1 alpha-2 code and primary currency. This is reference data only -
# it does NOT imply Africa ScholarBridge has verified banks for every
# country yet (see the Kenya bank directory in seed_data.py, which IS
# verified against the Central Bank of Kenya). Countries with no banks
# yet in the `banks` table simply show "no banks listed yet" and let the
# student use "My bank is not listed" -> manual entry (MANUAL_REVIEW).
# ---------------------------------------------------------------------
AFRICAN_COUNTRIES = [
    ("Algeria", "DZ", "DZD"),
    ("Angola", "AO", "AOA"),
    ("Benin", "BJ", "XOF"),
    ("Botswana", "BW", "BWP"),
    ("Burkina Faso", "BF", "XOF"),
    ("Burundi", "BI", "BIF"),
    ("Cabo Verde", "CV", "CVE"),
    ("Cameroon", "CM", "XAF"),
    ("Central African Republic", "CF", "XAF"),
    ("Chad", "TD", "XAF"),
    ("Comoros", "KM", "KMF"),
    ("Congo, Democratic Republic of the", "CD", "CDF"),
    ("Congo, Republic of the", "CG", "XAF"),
    ("Cote d'Ivoire", "CI", "XOF"),
    ("Djibouti", "DJ", "DJF"),
    ("Egypt", "EG", "EGP"),
    ("Equatorial Guinea", "GQ", "XAF"),
    ("Eritrea", "ER", "ERN"),
    ("Eswatini", "SZ", "SZL"),
    ("Ethiopia", "ET", "ETB"),
    ("Gabon", "GA", "XAF"),
    ("Gambia", "GM", "GMD"),
    ("Ghana", "GH", "GHS"),
    ("Guinea", "GN", "GNF"),
    ("Guinea-Bissau", "GW", "XOF"),
    ("Kenya", "KE", "KES"),
    ("Lesotho", "LS", "LSL"),
    ("Liberia", "LR", "LRD"),
    ("Libya", "LY", "LYD"),
    ("Madagascar", "MG", "MGA"),
    ("Malawi", "MW", "MWK"),
    ("Mali", "ML", "XOF"),
    ("Mauritania", "MR", "MRU"),
    ("Mauritius", "MU", "MUR"),
    ("Morocco", "MA", "MAD"),
    ("Mozambique", "MZ", "MZN"),
    ("Namibia", "NA", "NAD"),
    ("Niger", "NE", "XOF"),
    ("Nigeria", "NG", "NGN"),
    ("Rwanda", "RW", "RWF"),
    ("Sao Tome and Principe", "ST", "STN"),
    ("Senegal", "SN", "XOF"),
    ("Seychelles", "SC", "SCR"),
    ("Sierra Leone", "SL", "SLE"),
    ("Somalia", "SO", "SOS"),
    ("South Africa", "ZA", "ZAR"),
    ("South Sudan", "SS", "SSP"),
    ("Sudan", "SD", "SDG"),
    ("Tanzania", "TZ", "TZS"),
    ("Togo", "TG", "XOF"),
    ("Tunisia", "TN", "TND"),
    ("Uganda", "UG", "UGX"),
    ("Zambia", "ZM", "ZMW"),
    ("Zimbabwe", "ZW", "ZWL"),
]

ACCOUNT_TYPES = ["Savings", "Current", "Other"]

# How the applicant would like to receive funding (Bank Account / Disbursement
# Information step). Stored in student_bank_details.payment_method; rows saved
# before this choice existed have NULL there and are bank-account rows.
DISBURSEMENT_METHODS = {"bank": "Bank Account", "mobile_money": "Mobile Money"}
MOBILE_MONEY_PROVIDERS = ["M-PESA", "Airtel Money", "Other"]

BANK_STATUS_CHOICES = ["Licensed", "Not Currently Licensed", "Under Review"]


def mask_account_number(account_number):
    """Turn a full account number into a display-safe masked string,
    e.g. "1234567890" -> "•••• •••• 7890". Never show the full number in
    a template, a flash message, a log line, or a URL - callers should
    only ever pass the masked value to render_template unless they have
    an explicit, authorized reason to show the real one (see
    `reveal_full_account_number` gating in app.py).
    """
    if not account_number:
        return "—"
    digits = "".join(ch for ch in str(account_number) if ch.isalnum())
    if len(digits) <= 4:
        return "•••• " + digits
    last4 = digits[-4:]
    return "•••• •••• " + last4


def disbursement_method(details):
    """'bank' or 'mobile_money' for a stored student_bank_details row
    (None when there is no row). Rows saved before the choice existed
    have no payment_method and were always bank-account rows."""
    if not details:
        return None
    keys = details.keys() if hasattr(details, "keys") else []
    method = details["payment_method"] if "payment_method" in keys else None
    return method if method in DISBURSEMENT_METHODS else "bank"


def mobile_number_digits(number):
    return "".join(ch for ch in str(number or "") if ch.isdigit())


def is_valid_mobile_number(number):
    """Presence plus a light sanity check: only digits, spaces, '+', '-' or
    brackets, and 9-15 digits in total (covers 07XXXXXXXX and +2547XXXXXXXX)."""
    text = str(number or "").strip()
    if not text or any(not (ch.isdigit() or ch in " +-()") for ch in text):
        return False
    return 9 <= len(mobile_number_digits(text)) <= 15


def mask_mobile_number(number):
    """Display-safe mobile money number, e.g. "0712345645" -> "07******45".
    Like mask_account_number, the full number is never shown on a page."""
    digits = mobile_number_digits(number)
    if not digits:
        return "—"
    if len(digits) <= 4:
        return "*" * len(digits)
    return digits[:2] + "*" * (len(digits) - 4) + digits[-2:]


def country_names():
    return [c[0] for c in AFRICAN_COUNTRIES]


def currency_for_country(country_name):
    for name, iso, currency in AFRICAN_COUNTRIES:
        if name == country_name:
            return currency
    return None


# Bank NAMES only, from the Central Bank of Kenya directory of licensed
# commercial banks (no codes/SWIFT - never fabricated). Used by seed_data.py
# and to pre-fill an empty production database.
KENYA_CBK_LICENSED_BANKS = [
    "ABSA Bank Kenya PLC", "Access Bank (Kenya) PLC", "African Banking Corporation Limited",
    "Bank of Africa Kenya Limited", "Bank of Baroda (Kenya) Limited", "Bank of India",
    "Citibank N.A Kenya", "Consolidated Bank of Kenya Limited", "Co-operative Bank of Kenya Limited",
    "Credit Bank PLC", "Development Bank of Kenya Limited", "Diamond Trust Bank Kenya Limited",
    "DIB Bank Kenya Limited", "Ecobank Kenya Limited", "Equity Bank Kenya Limited",
    "Family Bank Limited", "First Community Bank Limited", "Guaranty Trust Bank (K) Ltd",
    "Guardian Bank Limited", "Gulf African Bank Limited", "Habib Bank A.G Zurich", "I&M Bank Limited",
    "KCB Bank Kenya Limited", "Kingdom Bank Limited", "Mayfair CIB Bank Limited",
    "Middle East Bank (K) Limited", "M-Oriental Bank Limited", "National Bank of Kenya Limited",
    "NCBA Bank Kenya PLC", "Paramount Bank Limited", "Prime Bank Limited", "SBM Bank Kenya Limited",
    "Sidian Bank Limited", "Spire Bank Ltd", "Stanbic Bank Kenya Limited",
    "Standard Chartered Bank Kenya Limited", "UBA Kenya Bank Limited", "Victoria Commercial Bank PLC",
]
