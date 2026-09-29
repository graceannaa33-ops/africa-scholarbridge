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
MAX_VISA_VALIDITY_YEARS = 10   # longest validity the U.S. issues on any visa

# Reading runs in ONE subprocess per verification (visa_ocr_worker.py).
# Measured on a single core: ~3.5 s per photo (~7 s on Render's 0.5 CPU),
# peak ~280-330 MB resident. Limits (any breach = unreadable = FAIL):
OCR_LOCK_WAIT_SECONDS = 90     # queue behind other verifications (one reader at a time)
OCR_TIMEOUT_SECONDS = 45       # hard kill of the reading process
OCR_WORK_BUDGET_SECONDS = 25   # after this the reader stops optional extra passes
OCR_MAX_RSS_MB = 400           # watchdog: killed if it ever uses more than this
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


# glibc allocator: one arena for the (single-threaded) reader, which keeps
# its peak lower; visa_ocr_worker.py also returns freed memory to the OS
# between passes (malloc_trim). Measured: ~240-300 MB peak, no slowdown.
_OCR_ENV = dict(os.environ, MALLOC_ARENA_MAX="1", OMP_NUM_THREADS="1")


def _rss_mb(pid):
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return 0.0


_READ_ERRORS = {
    "pdf_encrypted": "Password-protected PDFs can't be read. Please upload an unprotected copy.",
    "pdf_no_pages": "The PDF has no pages.",
    "pdf_too_many_pages": "The PDF has too many pages. Please upload only your visa page.",
    "pdf_damaged": "The PDF could not be read - it may be damaged.",
    "image_damaged": "The image could not be read - it may be damaged.",
    "image_too_small": "The image is too small to show a legible visa.",
    "image_too_large": "The image has too many pixels to process (over 24 MP for PNG, 120 MP for JPG). "
                       "Please upload a normal photo or scan.",
}


def read_document_contents(file_bytes, ext):
    """Runs the reader subprocess (one at a time per server) and returns
    (text_layer, ocr_text, error_message). Returns (None, None, None) when
    the document could not be read for ANY technical reason - missing
    library, crash, timeout, memory limit, server busy. The caller treats
    that as unreadable, i.e. FAIL."""
    import tempfile
    _metric("ocr_waiting_started")
    lock = _OcrLock()
    try:
        acquired = lock.__enter__()
    except BaseException:
        _metric("ocr_waiting_finished", False)
        raise
    with _released(lock):
        _metric("ocr_waiting_finished", acquired)
        if not acquired:
            return None, None, None
        started_at = time.monotonic()
        readable = False
        try:
            result = _run_reader(file_bytes, ext, tempfile)
            readable = result is not None
        finally:
            _metric("ocr_job_finished", time.monotonic() - started_at, readable)
    if result is None:
        return None, None, None
    if result.get("error"):
        return None, None, _READ_ERRORS.get(result["error"], "The document could not be read.")
    lines = result.get("ocr_lines") or []
    return str(result.get("text_layer") or ""), "\n".join(str(x) for x in lines), None


class _released:
    """Context manager that releases an already-entered _OcrLock."""

    def __init__(self, lock):
        self.lock = lock

    def __enter__(self):
        return self.lock

    def __exit__(self, *exc):
        self.lock.__exit__(*exc)
        return False


def _metric(name, *args):
    """Capacity-monitor hook (queue length, processing time). Monitoring
    can never affect the verification result."""
    try:
        import capacity_monitor
        getattr(capacity_monitor, name)(*args)
    except Exception:  # noqa: BLE001
        pass


def _run_reader(file_bytes, ext, tempfile):
    """Runs the reader subprocess (caller holds the lock). Returns its JSON
    result dict, or None when the document could not be read for a
    technical reason (crash, timeout, memory watchdog, bad output)."""
    with tempfile.TemporaryDirectory(prefix="visa-read-") as tmp:
        src = os.path.join(tmp, "document")
        out = os.path.join(tmp, "result.json")
        with open(src, "wb") as fh:
            fh.write(file_bytes)
        try:
            with open(out, "wb") as out_fh:
                proc = subprocess.Popen(
                    [sys.executable, _OCR_WORKER, src, ext, str(OCR_WORK_BUDGET_SECONDS)],
                    stdin=subprocess.DEVNULL, stdout=out_fh, stderr=subprocess.DEVNULL,
                    close_fds=True, env=_OCR_ENV,
                )
                started = time.monotonic()
                while proc.poll() is None:
                    if (time.monotonic() - started > OCR_TIMEOUT_SECONDS
                            or _rss_mb(proc.pid) > OCR_MAX_RSS_MB):
                        proc.kill()
                        proc.wait()
                        return None
                    time.sleep(0.05)
            if proc.returncode != 0:
                return None
            with open(out, "rb") as fh:
                result = json.loads(fh.read().decode("utf-8") or "{}")
        except (OSError, ValueError):
            return None
    return result if isinstance(result, dict) else None


# ---------------------------------------------------------------------
# File checks (cheap, done in the web process - no decoding here)
# ---------------------------------------------------------------------
def check_file(data, filename):
    """Returns (ext, errors)."""
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
    return ext, []


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


def _checked_mrz(text, source, entered_pp):
    """(mrz, passport_field, check_digits_ok) for one text source, or None.

    Passport number: an OCR look-alike reading (O/0, I/1 ...) is replaced by
    the entered number ONLY if it is the same number; the check digit must
    then pass on the entered value, so a misread can never pass for a
    different number."""
    mrz = find_us_visa_mrz(text) if text else None
    if not mrz:
        return None
    passport_field = mrz["passport_field"]
    if entered_pp and source == "ocr" and _same_despite_ocr(passport_field.rstrip("<"), entered_pp):
        passport_field = entered_pp + passport_field[len(entered_pp):]
    ok = (mrz_check_digit(passport_field) == mrz["passport_cd"]
          and mrz_check_digit(mrz["dob_raw"]) == mrz["dob_cd"]
          and mrz_check_digit(mrz["expiry_raw"]) == mrz["expiry_cd"])
    return mrz, passport_field, ok


def verify_visa_submission(form, file_bytes, filename, applicant, today=None):
    """applicant: {'full_name': str, 'date_of_birth': 'YYYY-MM-DD'}.

    Returns (passed: bool, cleaned: dict, errors: list[str]). `passed` is
    True ONLY when every check has passed on information actually read
    from the document.
    """
    today = today or date.today()
    cleaned, errors = validate_details(form, today)

    ext, file_errors = check_file(file_bytes, filename)
    errors += file_errors
    cleaned["read_from"] = None

    dob = parse_iso_date(applicant.get("date_of_birth"))
    if not (applicant.get("full_name") or "").strip() or not dob:
        errors.append("Your full name and date of birth must be completed in your application "
                      "before a visa can be verified.")
    issue = parse_iso_date(cleaned["issue_date"])
    if dob and issue and issue <= dob:
        errors.append("Visa issue date is before your date of birth.")

    if file_errors:
        return False, cleaned, errors

    text_layer, ocr_text, read_error = read_document_contents(file_bytes, ext)
    if read_error:
        errors.append(read_error)
        return False, cleaned, errors

    # Each source is judged on its own; the first whose machine-readable
    # lines pass their check digits is the ONLY text every later check uses.
    entered_pp = cleaned["passport_number"]
    chosen = None
    for source, text in (("pdf-text", text_layer), ("ocr", ocr_text)):
        found = _checked_mrz(text, source, entered_pp)
        if found and (found[2] or chosen is None):
            chosen = (source, text) + found
            if found[2]:
                break
    if not chosen:
        errors.append(UNREADABLE_MESSAGE)
        return False, cleaned, errors
    source, text, mrz, passport_field, check_digits_ok = chosen
    cleaned["read_from"] = source
    mrz_passport = passport_field.replace("<", "")

    if not check_digits_ok:
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
        if not re.search(rf"(?<![A-Z0-9]){letter}-?[1IL](?![A-Z0-9])", printed):
            errors.append(f"The visa class {cleaned['visa_type']} could not be read on the visa document.")
    if issue:
        wanted = _foil_date(issue).translate(_TO_DIGIT)
        if wanted not in re.sub(r"\s+", "", printed).translate(_TO_DIGIT):
            errors.append("The issue date you entered does not match (or could not be read from) the visa.")

    if errors and source == "ocr":
        errors.append("If your details are correct, the image may not be clear enough to read - "
                      "a sharper, well-lit photo or scan of the whole visa page may pass.")
    return (not errors), cleaned, errors
