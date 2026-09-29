"""
gunicorn.conf.py - loaded automatically by `gunicorn wsgi:app` (Render's
start command). Sized for Render's 512 MB / 0.5 CPU instance.

Memory budget (measured):  master + 1 worker ~100 MB
                           + at most ONE visa-reading process ~280-330 MB
                           = ~430 MB peak, under 512 MB.

* workers = 1 : a second worker would add ~70 MB and is what could push the
  instance over 512 MB while a visa is being read. (Render's own default
  for this plan is also 1 - WEB_CONCURRENCY=1.)
* threads = 8 : while one request waits for a visa to be read (~7 s on
  0.5 CPU), other visitors are still served. Threads share the worker's
  memory; the visa reader itself is limited to one at a time by a lock.
* timeout = 120: with threaded workers this only restarts a worker that is
  completely frozen; each visa read has its own 45 s hard limit.
"""
import threading

workers = 1
worker_class = "gthread"
threads = 8
timeout = 120
graceful_timeout = 30


def post_worker_init(worker):
    """Load the visa reader once in the background after (re)start, so the
    first student's upload isn't slowed by cold disk reads, and so Render's
    logs show immediately whether photo/scan reading is available."""
    def warm():
        import subprocess
        import sys
        import visa_verification as vv
        try:
            with vv._OcrLock() as acquired:
                if not acquired:
                    return
                proc = subprocess.run([sys.executable, vv._OCR_WORKER, "--warmup"],
                                      capture_output=True, timeout=90, env=vv._OCR_ENV)
            ok = proc.returncode == 0 and b"ready" in proc.stdout
        except Exception:
            ok = False
        worker.log.info("Visa document reader %s", "ready" if ok else
                        "NOT available - photo/scan visas will fail verification (fail closed)")
    threading.Thread(target=warm, daemon=True).start()
