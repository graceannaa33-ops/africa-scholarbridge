"""
visa_verification.py
--------------------
Fully AUTOMATIC verification of a "Yes, I already have my U.S. visa"
submission. There is no manual/admin approval step.

The rule is fail-safe: a visa is VERIFIED only when every check below
passes against the uploaded document itself. Anything that is wrong,
missing, inconsistent - or that this system simply cannot read and
confirm - FAILS, and the caller sends the student to the
"I do not have a U.S. visa" assistance path.

Why the document must contain its machine-readable zone (MRZ)
-------------------------------------------------------------
Receiving a file proves nothing about what is in it. Every U.S. visa foil
carries a two-line machine-readable zone (ICAO 9303 "MRV-A": 2 x 44
characters) with check digits over the passport number, date of birth
and visa expiry date. That is the only part of a visa this server can
confirm reliably, so verification requires it:

  * PDF uploads: the text layer is read with pypdf. A scanner app that
    produces a searchable PDF (e.g. Adobe Scan, Microsoft Lens) works.
  * JPG/PNG uploads (and image-only PDFs) contain no machine-readable
    text. Unless OCR is installed on the server (optional: pytesseract +
    the tesseract binary), the contents cannot be read, so the upload
    fails verification rather than being assumed genuine.

Checks performed (all must pass)
--------------------------------
Form      : required fields; visa class is a U.S. student/exchange class
            (F-1, J-1, M-1); passport number format; valid ISO dates;
            not expired; issue date not in the future; issue < expiry;
            validity no longer than 10 years; not issued before birth.
File      : present; allowed extension; size limits; magic bytes match
            the extension; file actually parses (PDF opens, not
            encrypted, sane page count; image headers + dimensions).
Document  : a U.S. visa MRZ is present (document code V, issuer USA);
            every MRZ check digit is correct; MRZ passport number ==
            entered passport number; MRZ expiry == entered expiry; MRZ
            date of birth == applicant's date of birth; MRZ name ==
            applicant's name; the printed visa class and issue date on
            the foil match what was entered.
"""

import io
import re
import unicodedata
from datetime import date, datetime

ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}
MIN_FILE_BYTES = 512
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 10
MIN_IMAGE_SIDE = 300           # pixels; smaller than this can't hold a legible visa
MAX_VISA_VALIDITY_YEARS = 10   # longest validity the U.S. issues on any visa

# U.S. student / exchange-visitor visa classes accepted for this step.
STUDENT_VISA_CLASSES = {"F-1", "J-1", "M-1"}

_PASSPORT_RE = re.compile(r"^[A-Z0-9]{6,12}$")
_SIGNATURES = {
    "pdf": (b"%PDF-",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
}
# MRV-A: line 1 = 'V' + type + 'USA' + name (44); line 2 = passport(9) +
# check + nationality(3) + DOB(6) + check + sex + expiry(6) + check +
# optional data(16).
_MRZ_RE = re.compile(
    r"(V[A-Z<]USA[A-Z<]{39})"
    r"([A-Z0-9<]{9})([0-9<])([A-Z<]{3})([0-9]{6})([0-9])([MFX<])([0-9]{6})([0-9])([A-Z0-9<]{16})"
)
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------
def normalize_visa_class(value):
    """'f1', 'F 1', 'f-1' -> 'F-1'. Returns '' for anything unrecognisable."""
    m = re.fullmatch(r"\s*([A-Za-z])\s*-?\s*([0-9])\s*", value or "")
    return f"{m.group(1).upper()}-{m.group(2)}" if m else ""


def parse_iso_date(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _add_years(d, years):
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + years, day=28)


def mrz_check_digit(field):
    total = 0
    for i, ch in enumerate(field):
        if ch.isdigit():
            v = int(ch)
        elif "A" <= ch <= "Z":
            v = ord(ch) - 55
        else:  # '<'
            v = 0
        total += v * (7, 3, 1)[i % 3]
    return str(total % 10)


def _mrz_date(yymmdd, *, future):
    """YYMMDD -> date. Expiry dates are 20YY; birth dates use the century
    that doesn't put them in the future."""
    try:
        yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
        century = 2000 if future or yy <= date.today().year % 100 else 1900
        return date(century + yy, mm, dd)
    except ValueError:
        return None


def _name_tokens(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().upper()
    return [t for t in re.split(r"[^A-Z]+", text) if t]


def _foil_date(d):
    return f"{d.day:02d}{_MONTHS[d.month - 1]}{d.year}"


# ---------------------------------------------------------------------
# Form validation
# ---------------------------------------------------------------------
def validate_details(form, today=None):
    """Returns (cleaned, errors). `cleaned` echoes the student's input."""
    today = today or date.today()
    cleaned = {
        "visa_type": (form.get("visa_type_category") or "").strip(),
        "passport_number": re.sub(r"\s+", "", form.get("passport_number") or "").upper(),
        "issue_date": (form.get("visa_issue_date") or "").strip() or None,
        "expiry_date": (form.get("visa_expiry_date") or "").strip() or None,
        "notes": (form.get("additional_info") or "").strip()[:1000],
    }
    errors = []

    visa_class = normalize_visa_class(cleaned["visa_type"])
    if not cleaned["visa_type"]:
        errors.append("Visa type / category is required (F-1, J-1 or M-1).")
    elif visa_class not in STUDENT_VISA_CLASSES:
        errors.append("Visa type must be a U.S. student or exchange visa (F-1, J-1 or M-1).")
    else:
        cleaned["visa_type"] = visa_class

    if not cleaned["passport_number"]:
        errors.append("Passport number is required.")
    elif not _PASSPORT_RE.match(cleaned["passport_number"]):
        errors.append("Passport number must be 6-12 letters/numbers.")

    issue = parse_iso_date(cleaned["issue_date"])
    expiry = parse_iso_date(cleaned["expiry_date"])
    if not cleaned["issue_date"]:
        errors.append("Visa issue date is required.")
    elif not issue:
        errors.append("Visa issue date is not a valid date.")
    if not cleaned["expiry_date"]:
        errors.append("Visa expiry date is required.")
    elif not expiry:
        errors.append("Visa expiry date is not a valid date.")

    if issue and issue > today:
        errors.append("Visa issue date cannot be in the future.")
    if expiry and expiry < today:
        errors.append("This visa has already expired.")
    if issue and expiry:
        if issue >= expiry:
            errors.append("Visa issue date must be before the expiry date.")
        elif expiry > _add_years(issue, MAX_VISA_VALIDITY_YEARS):
            errors.append("Visa validity is longer than any U.S. visa allows - please check the dates.")
    if expiry and expiry > _add_years(today, MAX_VISA_VALIDITY_YEARS):
        errors.append("Visa expiry date is too far in the future - please check the date.")

    return cleaned, errors


# ---------------------------------------------------------------------
# File checks + text extraction
# ---------------------------------------------------------------------
def _png_size(data):
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _jpeg_size(data):
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + seg_len
    return None


def _ocr_image(data):
    """Optional OCR. Returns text, or None when OCR isn't available on this
    server (then the image is treated as unverifiable - never as valid)."""
    try:
        import pytesseract
        from PIL import Image
        return pytesseract.image_to_string(Image.open(io.BytesIO(data)))
    except Exception:
        return None


def check_file_and_extract_text(data, filename):
    """Returns (text, errors). `text` is None when nothing machine-readable
    could be obtained from the document."""
    if not filename:
        return None, ["Please choose your visa document to upload."]
    ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return None, ["Unsupported file type. Please upload a PDF, JPG, JPEG, or PNG file."]
    if not data:
        return None, ["The uploaded file is empty."]
    if len(data) < MIN_FILE_BYTES:
        return None, ["The uploaded file is too small to be a visa document."]
    if len(data) > MAX_FILE_BYTES:
        return None, [f"The uploaded file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB."]
    if not data.startswith(_SIGNATURES[ext]):
        return None, ["This file is not a valid PDF/JPG/PNG (its contents don't match its type)."]

    if ext == "pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                return None, ["Password-protected PDFs can't be read. Please upload an unprotected copy."]
            pages = reader.pages
            if len(pages) == 0:
                return None, ["The PDF has no pages."]
            if len(pages) > MAX_PDF_PAGES:
                return None, [f"The PDF has more than {MAX_PDF_PAGES} pages. Please upload only your visa page."]
            text = "\n".join((p.extract_text() or "") for p in pages)
        except Exception:
            return None, ["The PDF could not be read - it may be damaged."]
        return (text if text.strip() else None), []

    size = _png_size(data) if ext == "png" else _jpeg_size(data)
    if not size:
        return None, ["The image could not be read - it may be damaged."]
    if min(size) < MIN_IMAGE_SIDE:
        return None, ["The image is too small to show a legible visa."]
    text = _ocr_image(data)
    return (text if text and text.strip() else None), []


# ---------------------------------------------------------------------
# MRZ parsing
# ---------------------------------------------------------------------
_DIGIT_FIX = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8"})


def find_us_visa_mrz(text):
    """Finds and parses a U.S. visa MRZ in extracted text. Returns a dict or
    None. Check digits are NOT validated here (see verify_mrz)."""
    compact = re.sub(r"\s+", "", (text or "").upper()).replace("«", "<")
    m = _MRZ_RE.search(compact)
    if not m:
        # Common OCR slips inside digit-only fields (O->0, I->1 ...) are
        # corrected ONLY in those fields; check digits still have to pass.
        for m1 in re.finditer(r"V[A-Z<]USA[A-Z<]{39}", compact):
            tail = compact[m1.end():m1.end() + 44]
            if len(tail) < 44:
                continue
            fixed = (tail[:13] + tail[13:20].translate(_DIGIT_FIX) + tail[20]
                     + tail[21:28].translate(_DIGIT_FIX) + tail[28:])
            m = _MRZ_RE.fullmatch(m1.group(0) + fixed)
            if m:
                break
    if not m:
        return None
    line1, pp, pp_cd, nat, dob, dob_cd, sex, exp, exp_cd, opt = m.groups()
    names = line1[5:].rstrip("<")
    surname, _, given = names.partition("<<")
    passport_field = pp
    if pp_cd == "<":  # passport numbers longer than 9 chars overflow into optional data
        overflow = opt.split("<", 1)[0]
        passport_field, pp_cd = pp + overflow[:-1], overflow[-1:] or "<"
    return {
        "line1": line1,
        "passport_field": passport_field, "passport_cd": pp_cd,
        "passport_number": passport_field.replace("<", ""),
        "nationality": nat.replace("<", ""),
        "dob_raw": dob, "dob_cd": dob_cd, "sex": sex,
        "expiry_raw": exp, "expiry_cd": exp_cd,
        "surname_tokens": [t for t in surname.split("<") if t],
        "given_tokens": [t for t in given.split("<") if t],
        "name_truncated": not line1.endswith("<"),
    }


def _names_match(mrz, applicant_name):
    app_tokens = _name_tokens(applicant_name)
    if not app_tokens or not mrz["surname_tokens"]:
        return False
    all_mrz = mrz["surname_tokens"] + mrz["given_tokens"]

    def present(tok, may_be_truncated):
        return tok in app_tokens or (may_be_truncated and any(a.startswith(tok) for a in app_tokens))

    for i, tok in enumerate(all_mrz):
        if not present(tok, mrz["name_truncated"] and i == len(all_mrz) - 1):
            return False
    return bool(mrz["given_tokens"])


# ---------------------------------------------------------------------
# The whole decision
# ---------------------------------------------------------------------
def verify_visa_submission(form, file_bytes, filename, applicant, today=None):
    """applicant: {'full_name': str, 'date_of_birth': 'YYYY-MM-DD'}.

    Returns (verified: bool, cleaned: dict, errors: list[str]). `verified`
    is True ONLY when every check has passed.
    """
    today = today or date.today()
    cleaned, errors = validate_details(form, today)

    text, file_errors = check_file_and_extract_text(file_bytes, filename)
    errors += file_errors

    dob = parse_iso_date(applicant.get("date_of_birth"))
    if not (applicant.get("full_name") or "").strip() or not dob:
        errors.append("Your full name and date of birth must be completed in your application "
                      "before a visa can be verified.")
    issue = parse_iso_date(cleaned["issue_date"])
    if dob and issue and issue <= dob:
        errors.append("Visa issue date is before your date of birth.")

    if file_errors:
        return False, cleaned, errors
    if text is None:
        errors.append("Your visa could not be verified automatically: the document has no readable text. "
                      "Upload a searchable PDF scan of your visa page that shows the two machine-readable "
                      "lines (with <<< characters) at the bottom.")
        return False, cleaned, errors

    mrz = find_us_visa_mrz(text)
    if not mrz:
        errors.append("No U.S. visa machine-readable zone was found in the document, so it cannot be "
                      "verified as a U.S. visa.")
        return False, cleaned, errors

    if (mrz_check_digit(mrz["passport_field"]) != mrz["passport_cd"]
            or mrz_check_digit(mrz["dob_raw"]) != mrz["dob_cd"]
            or mrz_check_digit(mrz["expiry_raw"]) != mrz["expiry_cd"]):
        errors.append("The visa's machine-readable zone failed its security check digits.")
        return False, cleaned, errors

    mrz_dob = _mrz_date(mrz["dob_raw"], future=False)
    mrz_exp = _mrz_date(mrz["expiry_raw"], future=True)
    if not mrz_dob or not mrz_exp:
        errors.append("The dates in the visa's machine-readable zone are invalid.")
        return False, cleaned, errors

    if cleaned["passport_number"] and mrz["passport_number"] != cleaned["passport_number"]:
        errors.append("The passport number you entered does not match the passport number on the visa.")
    expiry = parse_iso_date(cleaned["expiry_date"])
    if expiry and mrz_exp != expiry:
        errors.append("The expiry date you entered does not match the expiry date on the visa.")
    if mrz_exp < today:
        errors.append("The visa document shows that this visa has expired.")
    if dob and mrz_dob != dob:
        errors.append("The date of birth on the visa does not match the date of birth in your application.")
    if not _names_match(mrz, applicant.get("full_name")):
        errors.append("The name on the visa does not match the name in your application.")

    printed = (text or "").upper()
    compact = re.sub(r"\s+", "", printed)
    if cleaned["visa_type"] in STUDENT_VISA_CLASSES:
        letter = cleaned["visa_type"][0]
        if not re.search(rf"(?<![A-Z0-9]){letter}-?1(?![0-9])", printed):
            errors.append(f"The visa class {cleaned['visa_type']} could not be found on the visa document.")
    if issue and _foil_date(issue) not in compact:
        errors.append("The issue date you entered does not match the issue date on the visa.")

    return (not errors), cleaned, errors
