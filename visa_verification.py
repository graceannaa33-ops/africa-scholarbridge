"""
visa_verification.py
--------------------
Fully AUTOMATIC checking of a "Yes, I already have my U.S. visa"
submission. There is no manual/admin approval step.

What a PASS means - and what it does not
----------------------------------------
A pass means: the information the student entered is complete and valid,
and it is CONSISTENT with what this server could actually READ from the
uploaded visa document and with the student's own application. It does
NOT mean the visa has been authenticated with the U.S. government - no
file check, realistic-looking image, plausible field or mathematically
valid check digit can prove that. The wording shown to students
("Visa information verified successfully") says exactly this.

Fail closed
-----------
Anything wrong, missing, expired, inconsistent, unsupported - or that the
server cannot READ reliably from the document - FAILS. The caller then
moves the student to the "I do not have a U.S. visa" assistance path.
Nothing is ever passed because it "looks fine" or because reading failed.

Reading the document
--------------------
Every U.S. visa foil carries a two-line machine-readable zone (MRZ, ICAO
9303 "MRV-A": 2 x 44 characters) with check digits over the passport
number, date of birth and expiry date. The MRZ must be read from the
document itself:
  * PDF  - the text layer is read with pypdf. If it has no readable MRZ
           (a scanned, image-only PDF), the images embedded in the PDF
           are read with OCR instead.
  * JPG / JPEG / PNG - read with OCR (visa_ocr_worker.py: RapidOCR on
           ONNX Runtime, entirely on this server, in a short-lived
           subprocess with a timeout). A blurry, dark, cropped or
           otherwise unreadable image simply yields no valid MRZ -> FAIL.
OCR character slips are only corrected where the MRZ format fixes the
character type (digits-only or letters-only positions); every number is
still guarded by its check digit, so a misread can only cause a FAIL,
never a false PASS.

Checks performed (all must pass)
--------------------------------
Form      : required fields; visa class is F-1, J-1 or M-1; passport
            number format; valid ISO dates; not expired; issue date not in
            the future; issue < expiry; validity <= 10 years; not issued
            before birth.
File      : present; allowed extension; size limits; content signature
            matches the extension; the file actually opens.
Document  : a U.S. visa MRZ (document code V, issuer USA) is read from
            the document; all MRZ check digits are correct; MRZ passport
            number == entered; MRZ expiry == entered and not expired; MRZ
            date of birth == application; MRZ name == application; the
            visa class and issue date printed on the visa == entered.
"""

import io
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import date, datetime

ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}
MIN_FILE_BYTES = 512
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_PDF_IMAGES_TO_READ = 3
MIN_IMAGE_SIDE = 300           # pixels; smaller than this can't hold a legible visa
MAX_VISA_VALIDITY_YEARS = 10   # longest validity the U.S. issues on any visa

# OCR runs in a subprocess; keep the whole request under gunicorn's 30 s timeout.
OCR_TIMEOUT_SECONDS = 18
OCR_LOCK_WAIT_SECONDS = 8
_OCR_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "visa_ocr_worker.py")

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

# Characters OCR commonly confuses. Used ONLY where the MRZ format dictates
# the character type, or to compare two values that must be identical.
_TO_DIGIT = str.maketrans({"O": "0", "Q": "0", "D": "0", "U": "0", "I": "1", "L": "1", "T": "1",
                           "Z": "2", "S": "5", "G": "6", "B": "8"})
_TO_LETTER = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"})


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


def _same_despite_ocr(a, b):
    """True when two strings are identical once OCR look-alikes (O/0, I/1,
    S/5 ...) are treated as the same character."""
    return len(a) == len(b) and a.translate(_TO_DIGIT) == b.translate(_TO_DIGIT)


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
# OCR (subprocess)
# ---------------------------------------------------------------------
class _OcrLock:
    """One OCR at a time per server (memory), shared across gunicorn
    workers via a lock file. Not getting the lock in time = unreadable."""

    def __init__(self):
        self.fh = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:  # Windows dev machines: no cross-process lock
            return True
        lock_dir = os.environ.get("UPLOAD_ROOT") or os.path.dirname(os.path.abspath(__file__))
        os.makedirs(lock_dir, exist_ok=True)
        self.fh = open(os.path.join(lock_dir, ".visa_ocr.lock"), "w")
        deadline = time.monotonic() + OCR_LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.2)

    def __exit__(self, *exc):
        if self.fh:
            self.fh.close()  # releases the lock


def ocr_image_lines(image_bytes):
    """Returns the text lines read from an image, or None when the image
    could not be read for ANY reason (library missing, corrupt image,
    timeout, out of memory, server busy). None always means FAIL."""
    with _OcrLock() as acquired:
        if not acquired:
            return None
        try:
            proc = subprocess.run(
                [sys.executable, _OCR_WORKER], input=image_bytes, capture_output=True,
                timeout=OCR_TIMEOUT_SECONDS,
            )
            if proc.returncode != 0:
                return None
            lines = json.loads(proc.stdout.decode("utf-8") or "{}").get("lines")
            return [str(x) for x in lines] if isinstance(lines, list) else None
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return None


# ---------------------------------------------------------------------
# File checks + reading the document
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


def _pdf_embedded_images(reader):
    """Largest images embedded in the PDF's pages (a scanned visa page)."""
    found = []
    for page in reader.pages:
        try:
            for img in page.images:
                data = img.data
                if data and len(data) > 5_000:
                    found.append(data)
        except Exception:
            continue
    found.sort(key=len, reverse=True)
    return found[:MAX_PDF_IMAGES_TO_READ]


def read_document(data, filename):
    """Checks the file and reads its text.

    Returns (text, source, errors). `text` is None when nothing
    machine-readable could be obtained. `source` is 'pdf-text', 'ocr' or None.
    """
    if not filename:
        return None, None, ["Please choose your visa document to upload."]
    ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return None, None, ["Unsupported file type. Please upload a PDF, JPG, JPEG, or PNG file."]
    if not data:
        return None, None, ["The uploaded file is empty."]
    if len(data) < MIN_FILE_BYTES:
        return None, None, ["The uploaded file is too small to be a visa document."]
    if len(data) > MAX_FILE_BYTES:
        return None, None, [f"The uploaded file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB."]
    if not data.startswith(_SIGNATURES[ext]):
        return None, None, ["This file is not a valid PDF/JPG/PNG (its contents don't match its type)."]

    if ext == "pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                return None, None, ["Password-protected PDFs can't be read. Please upload an unprotected copy."]
            if len(reader.pages) == 0:
                return None, None, ["The PDF has no pages."]
            if len(reader.pages) > MAX_PDF_PAGES:
                return None, None, [f"The PDF has more than {MAX_PDF_PAGES} pages. Please upload only your visa page."]
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception:
            return None, None, ["The PDF could not be read - it may be damaged."]
        if find_us_visa_mrz(text):
            return text, "pdf-text", []
        # Scanned (image-only) PDF: read the embedded page image(s).
        for image in _pdf_embedded_images(reader):
            lines = ocr_image_lines(image)
            if lines and find_us_visa_mrz("\n".join(lines)):
                return text + "\n" + "\n".join(lines), "ocr", []
        return (text if text.strip() else None), ("pdf-text" if text.strip() else None), []

    size = _png_size(data) if ext == "png" else _jpeg_size(data)
    if not size:
        return None, None, ["The image could not be read - it may be damaged."]
    if min(size) < MIN_IMAGE_SIDE:
        return None, None, ["The image is too small to show a legible visa."]
    lines = ocr_image_lines(data)
    if not lines:
        return None, None, []
    return "\n".join(lines), "ocr", []


# ---------------------------------------------------------------------
# MRZ parsing
# ---------------------------------------------------------------------
def _fix_line1(raw):
    """Normalise an OCR'd MRZ line 1 to exactly 44 characters, or None."""
    s = raw.rstrip("<")
    if not s.startswith("V") or len(s) < 8 or len(s) > 44:
        return None
    s = s[:2].replace("0", "O") + s[2:5] + s[5:].translate(_TO_LETTER)
    # A readable name field always has the '<<' surname/given-name separator.
    if s[2:5] != "USA" or "<<" not in s[5:]:
        return None
    return s.ljust(44, "<")


def _fix_line2(raw):
    """Normalise an OCR'd MRZ line 2 to exactly 44 characters, or None.
    Only positions whose type is fixed by the format are corrected."""
    s = raw.rstrip("<")
    if len(s) < 28 or len(s) > 44:
        return None
    s = (s[:9] + s[9].translate(_TO_DIGIT) + s[10:13].translate(_TO_LETTER)
         + s[13:20].translate(_TO_DIGIT) + s[20] + s[21:28].translate(_TO_DIGIT) + s[28:])
    return s.ljust(44, "<")


def _mrz_candidates(text):
    upper = (text or "").upper().replace("«", "<")
    compact = re.sub(r"\s+", "", upper)
    yield compact  # text-layer PDFs: exact MRZ somewhere in the text
    lines = [re.sub(r"\s+", "", ln) for ln in upper.splitlines()]
    lines = [ln for ln in lines if ln]
    for i, ln in enumerate(lines):
        l1 = _fix_line1(ln)
        if not l1:
            continue
        for nxt in lines[i + 1:i + 4]:
            l2 = _fix_line2(nxt)
            if l2:
                yield l1 + l2


def find_us_visa_mrz(text):
    """Finds and parses a U.S. visa MRZ in the document text. Returns a dict
    or None. Check digits are NOT validated here (see verify_visa_submission)."""
    m = None
    for candidate in _mrz_candidates(text):
        m = _MRZ_RE.search(candidate)
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
    if not app_tokens or not mrz["surname_tokens"] or not mrz["given_tokens"]:
        return False
    all_mrz = mrz["surname_tokens"] + mrz["given_tokens"]
    for i, tok in enumerate(all_mrz):
        may_be_truncated = mrz["name_truncated"] and i == len(all_mrz) - 1
        if tok not in app_tokens and not (may_be_truncated and any(a.startswith(tok) for a in app_tokens)):
            return False
    return True


# ---------------------------------------------------------------------
# The whole decision
# ---------------------------------------------------------------------
UNREADABLE_MESSAGE = (
    "The visa information could not be read reliably from your document (it may be blurry, dark, "
    "cropped, too small, or not a U.S. visa). Upload a clear, straight, well-lit photo or scan of the "
    "whole visa page, including the two lines with <<< characters at the bottom."
)


def verify_visa_submission(form, file_bytes, filename, applicant, today=None):
    """applicant: {'full_name': str, 'date_of_birth': 'YYYY-MM-DD'}.

    Returns (passed: bool, cleaned: dict, errors: list[str]). `passed` is
    True ONLY when every check has passed on information actually read
    from the document.
    """
    today = today or date.today()
    cleaned, errors = validate_details(form, today)

    text, source, file_errors = read_document(file_bytes, filename)
    errors += file_errors
    cleaned["read_from"] = source

    dob = parse_iso_date(applicant.get("date_of_birth"))
    if not (applicant.get("full_name") or "").strip() or not dob:
        errors.append("Your full name and date of birth must be completed in your application "
                      "before a visa can be verified.")
    issue = parse_iso_date(cleaned["issue_date"])
    if dob and issue and issue <= dob:
        errors.append("Visa issue date is before your date of birth.")

    if file_errors:
        return False, cleaned, errors
    mrz = find_us_visa_mrz(text) if text else None
    if not mrz:
        errors.append(UNREADABLE_MESSAGE)
        return False, cleaned, errors

    # Passport number: accept an OCR look-alike reading ONLY if it is the
    # same number as entered; the check digit must then pass on the
    # entered value, so a misread can never pass for a different number.
    passport_field = mrz["passport_field"]
    entered_pp = cleaned["passport_number"]
    if entered_pp and source == "ocr" and _same_despite_ocr(passport_field.rstrip("<"), entered_pp):
        passport_field = entered_pp + passport_field[len(entered_pp):]
    mrz_passport = passport_field.replace("<", "")

    if (mrz_check_digit(passport_field) != mrz["passport_cd"]
            or mrz_check_digit(mrz["dob_raw"]) != mrz["dob_cd"]
            or mrz_check_digit(mrz["expiry_raw"]) != mrz["expiry_cd"]):
        errors.append("The visa's machine-readable lines failed their check digits "
                      "(the document is unreadable, altered, or not a genuine layout).")
        return False, cleaned, errors

    mrz_dob = _mrz_date(mrz["dob_raw"], future=False)
    mrz_exp = _mrz_date(mrz["expiry_raw"], future=True)
    if not mrz_dob or not mrz_exp:
        errors.append("The dates in the visa's machine-readable lines are invalid.")
        return False, cleaned, errors

    if entered_pp and mrz_passport != entered_pp:
        errors.append("The passport number you entered does not match the passport number on the visa.")
    expiry = parse_iso_date(cleaned["expiry_date"])
    if expiry and mrz_exp != expiry:
        errors.append("The expiry date you entered does not match the expiry date on the visa.")
    if mrz_exp < today:
        errors.append("The visa document shows that this visa has expired.")
    if dob and mrz_dob != dob:
        errors.append("The date of birth on the visa does not match the date of birth in your application.")
    if not _names_match(mrz, applicant.get("full_name")):
        errors.append("The name on the visa does not match (or could not be read clearly as) "
                      "the name in your application.")

    printed = (text or "").upper()
    if cleaned["visa_type"] in STUDENT_VISA_CLASSES:
        letter = cleaned["visa_type"][0]
        if not re.search(rf"(?<![A-Z0-9]){letter}-?[1I](?![A-Z0-9])", printed):
            errors.append(f"The visa class {cleaned['visa_type']} could not be read on the visa document.")
    if issue:
        wanted = _foil_date(issue).translate(_TO_DIGIT)
        if wanted not in re.sub(r"\s+", "", printed).translate(_TO_DIGIT):
            errors.append("The issue date you entered does not match (or could not be read from) the visa.")

    if errors and source == "ocr":
        errors.append("If your details are correct, the image may not be clear enough to read - "
                      "a sharper, well-lit photo or scan of the whole visa page may pass.")
    return (not errors), cleaned, errors
