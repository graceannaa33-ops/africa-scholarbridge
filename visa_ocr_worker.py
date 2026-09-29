"""
visa_ocr_worker.py
------------------
Reads the text off a visa IMAGE (JPG/PNG, or images embedded in a scanned
PDF) with a local OCR engine (RapidOCR / ONNX Runtime - no internet, no
external service). visa_verification.py runs this file as a short-lived
SUBPROCESS:

    python visa_ocr_worker.py < image-bytes   ->   JSON {"lines": [...]} on stdout

Running it out-of-process keeps the web worker small (the OCR models and
buffers are freed as soon as the child exits) and makes every failure mode
- missing library, corrupt image, out-of-memory, timeout - an ordinary
"could not read" result, which the caller treats as UNVERIFIABLE.
Nothing here decides whether a visa passes; it only returns text.
"""
import io
import json
import os
import sys

# Keep the child small and predictable on a 512 MB instance.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

MAX_SIDE = 1280           # longest side fed to the OCR engine (keeps peak memory ~300 MB)
MAX_PIXELS = 40_000_000   # refuse decompression bombs outright


def _prepare(data):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    img = Image.open(io.BytesIO(data))
    img.load()
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > MAX_SIDE:
        scale = MAX_SIDE / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img


def _ocr(engine, img):
    import numpy as np
    result, _ = engine(np.asarray(img)[:, :, ::-1].copy())  # RGB -> BGR
    return [r[1] for r in (result or []) if r and len(r) > 1]


def main():
    data = sys.stdin.buffer.read()
    img = _prepare(data)
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1, use_cls=False,
                      det_limit_side_len=480)  # caps the detector's internal upscaling

    lines = _ocr(engine, img)
    # Always take a second, closer look at the bottom of the document, where
    # the two machine-readable lines are printed: read on their own they come
    # out far more reliably (e.g. short names followed by long <<< fillers).
    w, h = img.size
    bottom = img.crop((0, int(h * 0.55), w, h))
    if bottom.width != MAX_SIDE:
        from PIL import Image
        s = MAX_SIDE / bottom.width
        bottom = bottom.resize((MAX_SIDE, max(1, int(bottom.height * s))), Image.LANCZOS)
    lines += _ocr(engine, bottom)
    json.dump({"lines": lines}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # any failure = unreadable (caller fails closed)
        json.dump({"error": type(exc).__name__}, sys.stdout)
        sys.exit(2)
