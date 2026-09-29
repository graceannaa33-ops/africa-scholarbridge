"""
visa_ocr_worker.py
------------------
Reads an uploaded visa document and returns its TEXT. It never decides
whether a visa passes - visa_verification.py does that.

visa_verification.py runs this file as ONE short-lived subprocess per
verification:

    python visa_ocr_worker.py <document-file> <ext> <deadline-seconds>
    -> prints JSON: {"text_layer": str, "ocr_lines": [str], "error": null|code}

Why a subprocess
  * Memory: the OCR engine (RapidOCR on ONNX Runtime) peaks at roughly
    280-330 MB while it works. All of it is returned to the system the
    moment this process exits, so the web worker stays small (~100 MB).
  * Isolation: corrupt files, decompression bombs, crashes, time-outs and
    out-of-memory all end THIS process only; the caller treats any of
    them as "could not read" -> verification FAILS (fail closed).

Safety limits applied here (the caller adds a lock, a hard timeout and an
RSS watchdog on top):
  * dies automatically if the web worker that started it dies
    (PR_SET_PDEATHSIG) - no orphaned OCR processes;
  * address-space ceiling (RLIMIT_AS) so a runaway allocation fails fast;
  * images larger than MAX_PIXELS are refused; JPEGs are decoded directly
    at reduced size; everything is scaled to MAX_SIDE before OCR;
  * single-threaded inference; optional extra passes stop at the deadline.
"""
import io
import json
import os
import sys
import time

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

MAX_SIDE = 1280                 # longest side fed to the OCR engine
ROTATED_SIDE = 1024             # extra passes for rotated scans (smaller = less memory)
MAX_PIXELS = 24_000_000         # PNG / other: e.g. 6000 x 4000; larger are refused
MAX_JPEG_PIXELS = 120_000_000   # JPEG is decoded directly at reduced size, so 48-108 MP
                                # phone photos are fine
MIN_IMAGE_SIDE = 300
MAX_PDF_PAGES = 10
MAX_PDF_IMAGES = 2              # largest embedded images of a scanned PDF
ADDRESS_SPACE_LIMIT = 900 * 1024 * 1024   # measured virtual peak ~600 MB


def _release_memory():
    """Return freed memory to the OS between stages (cheap, done a few times)."""
    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


class ReadError(Exception):
    """A document problem with a specific, student-facing reason code."""


def _protect_process():
    try:  # die with the parent (Linux)
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
        if os.getppid() == 1:
            sys.exit(3)
    except Exception:
        pass
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_LIMIT, ADDRESS_SPACE_LIMIT))
    except Exception:
        pass


# ---------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------
def _load_image(data):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_JPEG_PIXELS
    try:
        img = Image.open(io.BytesIO(data))
        w, h = img.size
    except Exception:
        raise ReadError("image_damaged")
    if w * h > (MAX_JPEG_PIXELS if img.format == "JPEG" else MAX_PIXELS):
        raise ReadError("image_too_large")
    if min(w, h) < MIN_IMAGE_SIDE:
        raise ReadError("image_too_small")
    try:
        if img.format == "JPEG":
            img.draft("RGB", (MAX_SIDE, MAX_SIDE))   # decode at reduced size (less memory)
        img.load()
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            if img.mode in ("RGBA", "LA", "P", "PA"):
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
        if max(img.size) > MAX_SIDE:
            scale = MAX_SIDE / max(img.size)
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    except ReadError:
        raise
    except Exception:
        raise ReadError("image_damaged")
    _release_memory()
    return img


class _Reader:
    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self.engine = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1, use_cls=False,
                               det_limit_side_len=480)  # caps the detector's internal upscaling

    def lines(self, img):
        import numpy as np
        result, _ = self.engine(np.asarray(img)[:, :, ::-1].copy())  # RGB -> BGR
        out = [r[1] for r in (result or []) if r and len(r) > 1]
        del result
        _release_memory()
        return out

    @staticmethod
    def _bottom(img, width=MAX_SIDE):
        """The bottom of the page, where the machine-readable lines are,
        at a standard width - read far more reliably on its own."""
        from PIL import Image
        w, h = img.size
        crop = img.crop((0, int(h * 0.55), w, h))
        s = width / crop.width
        return crop.resize((width, max(1, int(crop.height * s))), Image.LANCZOS)

    def read(self, img, deadline):
        lines = self.lines(img) + self.lines(self._bottom(img))
        # Upside-down or sideways scans without orientation data: look at
        # the (rotated) bottom strip only while time allows.
        if not _has_valid_mrz(lines):
            for angle in (180, 90, 270):
                if time.monotonic() > deadline:
                    break
                turned = img.rotate(angle, expand=True)
                if max(turned.size) > ROTATED_SIDE:      # smaller passes = less memory
                    k = ROTATED_SIDE / max(turned.size)
                    turned = turned.resize((int(turned.width * k), int(turned.height * k)))
                rotated = self.lines(self._bottom(turned, ROTATED_SIDE))
                lines += rotated
                if _has_valid_mrz(rotated):
                    # Right way up at last: read the whole page this way too
                    # (visa class, issue date).
                    lines += self.lines(turned)
                    break
        return lines


def _has_valid_mrz(lines_or_text):
    """True when a U.S. visa MRZ with correct check digits is present."""
    import visa_verification as vv
    text = lines_or_text if isinstance(lines_or_text, str) else "\n".join(lines_or_text)
    m = vv.find_us_visa_mrz(text)
    return bool(m) and vv.mrz_check_digit(m["passport_field"]) == m["passport_cd"] \
        and vv.mrz_check_digit(m["dob_raw"]) == m["dob_cd"] \
        and vv.mrz_check_digit(m["expiry_raw"]) == m["expiry_cd"]


# ---------------------------------------------------------------------
# PDFs
# ---------------------------------------------------------------------
def _pdf_page_images(reader):
    """Embedded page images (a scanned visa), largest first, size-checked
    BEFORE decoding."""
    candidates = []
    for page in reader.pages:
        try:
            xobjects = page["/Resources"].get_object().get("/XObject")
            xobjects = xobjects.get_object() if xobjects else {}
        except Exception:
            continue
        for name in xobjects:
            try:
                obj = xobjects[name].get_object()
                if obj.get("/Subtype") != "/Image":
                    continue
                w, h = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
                is_jpeg = "/DCTDecode" in str(obj.get("/Filter"))
                if w * h > (MAX_JPEG_PIXELS if is_jpeg else MAX_PIXELS) or min(w, h) < MIN_IMAGE_SIDE:
                    continue
                candidates.append((w * h, page, name, obj))
            except Exception:
                continue
    candidates.sort(key=lambda c: c[0], reverse=True)
    for _, page, name, obj in candidates[:MAX_PDF_IMAGES]:
        try:
            filters = obj.get("/Filter")
            filters = filters if isinstance(filters, list) else [filters]
            if "/DCTDecode" in [str(f) for f in filters]:
                yield obj._data  # raw JPEG bytes -> decoded at reduced size
            else:
                for img in page.images:
                    if img.name.lstrip("/").split(".")[0] == str(name).lstrip("/"):
                        buf = io.BytesIO()
                        img.image.save(buf, "PNG")
                        yield buf.getvalue()
                        break
        except Exception:
            continue


def read_document(path, ext, deadline):
    with open(path, "rb") as fh:
        data = fh.read()
    result = {"text_layer": "", "ocr_lines": [], "error": None}

    if ext == "pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise ReadError("pdf_encrypted")
            n = len(reader.pages)
            if n == 0:
                raise ReadError("pdf_no_pages")
            if n > MAX_PDF_PAGES:
                raise ReadError("pdf_too_many_pages")
            result["text_layer"] = "\n".join((p.extract_text() or "") for p in reader.pages)
        except ReadError:
            raise
        except Exception:
            raise ReadError("pdf_damaged")
        if _has_valid_mrz(result["text_layer"]):
            return result                      # text layer is enough; no OCR needed
        images = list(_pdf_page_images(reader))   # raw bytes only; drop the parsed PDF
        del reader, data
        _release_memory()
        reader_engine = None
        for image_bytes in images:
            if time.monotonic() > deadline:
                break
            try:
                img = _load_image(image_bytes)
            except ReadError:
                continue
            reader_engine = reader_engine or _Reader()
            lines = reader_engine.read(img, deadline)
            result["ocr_lines"] += lines
            if _has_valid_mrz(lines):
                break
        return result

    img = _load_image(data)
    result["ocr_lines"] = _Reader().read(img, deadline)
    return result


def main():
    _protect_process()
    path, ext, budget = sys.argv[1], sys.argv[2], float(sys.argv[3])
    deadline = time.monotonic() + budget
    try:
        out = read_document(path, ext, deadline)
    except ReadError as exc:
        out = {"text_layer": "", "ocr_lines": [], "error": str(exc)}
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if len(sys.argv) == 2 and sys.argv[1] == "--warmup":
        # Loads the libraries and models once (e.g. after a deploy) so the
        # first real verification isn't slowed by cold disk reads.
        _protect_process()
        _Reader()
        print("ready")
        sys.exit(0)
    try:
        main()
    except MemoryError:
        sys.exit(4)
    except Exception:
        sys.exit(2)                              # caller treats as unreadable
