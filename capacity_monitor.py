"""
capacity_monitor.py
-------------------
Production capacity monitoring and early-warning e-mails for Africa
ScholarBridge. It watches how close the service is to its infrastructure
limits and tells the main admin BEFORE students are affected.

Design rules
  * It can never take the website down. Every entry point catches its own
    errors; a monitoring failure only shows up as "monitoring degraded".
  * Almost no per-request cost: requests only bump in-memory counters
    (a lock + a few integer operations). Everything else - reading
    CPU/memory/disk, database writes, evaluating thresholds, e-mail - runs
    in ONE background thread per worker every CAPACITY_SAMPLE_SECONDS.
  * Honest numbers. Values that cannot be measured reliably from inside the
    app are reported as "unavailable", never guessed. "Recently Active
    Users" is signed-in accounts seen in a time window, NOT simultaneous
    users.
  * Scales with the architecture: each worker/instance writes its own
    samples; the dashboard and alerts aggregate them, and exactly one
    process per interval evaluates and e-mails (claimed through the
    database), so adding workers or instances never duplicates alerts.

What is measured and where it comes from
  CPU %            container cgroup CPU usage vs. its CPU limit (v2 or v1).
                   Fallback limit: CAPACITY_CPU_LIMIT_CORES, else machine cores.
  Memory %         container cgroup working set (excl. reclaimable file cache)
                   vs. its memory limit. Includes the visa reader process.
                   Fallback limit: CAPACITY_MEMORY_LIMIT_MB; else unavailable.
  Storage %        disk holding the database and the uploads folder.
  Requests         per-worker counters: volume, 5xx errors, unhandled
                   exceptions, latency histogram (avg / p95), concurrent
                   (in-flight) requests.
  Database         SELECT round trip + sample write time, database errors.
  Visa reader      queue (waiting), running, processing time, and "reader
                   unavailable" failures (busy / timeout / crash).
  Network          container network bytes if /proc/net/dev is readable -
                   informational only; Render's metrics are authoritative.
Not measurable from inside Flask: exact simultaneous users, Render's own
bandwidth/instance metrics, Render's load balancer queue.
"""

import json
import logging
import math
import os
import shutil
import socket
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("africa_scholarbridge.capacity")

DEFAULT_ALERT_EMAIL = "africascholarbridge@gmail.com"
LEVELS = ("normal", "warning", "critical")
_RANK = {"normal": 0, "warning": 1, "critical": 2}

# Latency histogram bucket upper bounds (ms). Histograms merge exactly
# across workers and instances, so p95 stays meaningful at any scale.
LATENCY_BUCKETS_MS = (25, 50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000,
                      4000, 6000, 8000, 12000, 20000, 30000, 60000, math.inf)

EXCLUDED_PATH_PREFIXES = ("/static/", "/favicon")
# Requests that are slow by design (a visa photo is read for several
# seconds). They still count as requests/errors/concurrency, but are kept
# out of the response-time figures - otherwise one upload on a quiet day
# would look like the whole site is slow. Their timing is reported by the
# visa reader metrics instead.
HEAVY_PATHS = ("/application/visa-document/upload",)


# =====================================================================
# Configuration (environment variables; read fresh on every evaluation)
# =====================================================================
def _env(name, default, cast=float):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return cast(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid value for %s; using default.", name)
        return default


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# key: (label, unit, WARN env, default, CRIT env, default, sustain env/default, bottleneck)
# A threshold of 0 (or empty) disables that level.
METRICS = {
    "cpu_percent":          ("CPU", "%", "CAPACITY_CPU_WARN", 70, "CAPACITY_CPU_CRIT", 85, 3, "cpu"),
    "memory_percent":       ("Memory (RAM)", "%", "CAPACITY_MEMORY_WARN", 70, "CAPACITY_MEMORY_CRIT", 85, 2, "memory"),
    "storage_percent":      ("Storage (disk)", "%", "CAPACITY_STORAGE_WARN", 75, "CAPACITY_STORAGE_CRIT", 90, 1, "storage"),
    "error_rate_percent":   ("HTTP error rate (5xx)", "%", "CAPACITY_ERROR_RATE_WARN", 2, "CAPACITY_ERROR_RATE_CRIT", 5, 2, "application"),
    "latency_p95_ms":       ("Response time p95", "ms", "CAPACITY_LATENCY_P95_WARN_MS", 1500, "CAPACITY_LATENCY_P95_CRIT_MS", 4000, 3, "application"),
    "concurrent_requests":  ("Concurrent requests", "", "CAPACITY_CONCURRENT_WARN", 6, "CAPACITY_CONCURRENT_CRIT", 8, 2, "application"),
    "ocr_queue":            ("Visa reader queue", "", "CAPACITY_OCR_QUEUE_WARN", 3, "CAPACITY_OCR_QUEUE_CRIT", 6, 2, "ocr"),
    "ocr_unavailable":      ("Visa checks failed: reader busy/timeout", "", "CAPACITY_OCR_UNAVAILABLE_WARN", 1, "CAPACITY_OCR_UNAVAILABLE_CRIT", 3, 1, "ocr"),
    "db_latency_ms":        ("Database response time", "ms", "CAPACITY_DB_LATENCY_WARN_MS", 250, "CAPACITY_DB_LATENCY_CRIT_MS", 1000, 2, "database"),
    "db_errors":            ("Database errors", "", "CAPACITY_DB_ERRORS_WARN", 1, "CAPACITY_DB_ERRORS_CRIT", 5, 1, "database"),
    "active_users":         ("Recently Active Users", "", "CAPACITY_ACTIVE_USERS_WARN", 0, "CAPACITY_ACTIVE_USERS_CRIT", 0, 1, "application"),
    "requests_per_minute":  ("Requests per minute", "/min", "CAPACITY_REQUESTS_PER_MIN_WARN", 0, "CAPACITY_REQUESTS_PER_MIN_CRIT", 0, 2, "application"),
}

BOTTLENECK_ADVICE = {
    "cpu": "CPU bottleneck: move to an instance type with more CPU (vertical), or add instances "
           "(horizontal - requires PostgreSQL and object storage first). More storage will NOT help.",
    "memory": "RAM bottleneck: move to an instance type with more memory. The visa reader uses ~300 MB "
              "while it works; running it as a separate background worker service removes that from "
              "the web service. More storage will NOT help.",
    "storage": "Disk/storage bottleneck: increase the Render persistent disk, and plan to move uploaded "
               "documents to object storage. Storage fixes space only - it does NOT fix traffic or speed problems.",
    "application": "Application/server bottleneck: requests are waiting for free workers or erroring. Check "
                   "Render logs for errors first; then add gunicorn threads/workers (needs RAM) or instances.",
    "ocr": "Visa reader (OCR) bottleneck: uploads are queuing or failing because the single reader is busy. "
           "Add CPU, or move OCR to a separate background worker service with its own queue.",
    "database": "Database bottleneck: SQLite allows one writer at a time. Migrate to PostgreSQL (Render "
                "Postgres) before large-scale concurrent use.",
    "network": "Network: check Render's bandwidth metrics; serve static files/uploads from a CDN/object storage.",
}


def load_config():
    cfg = {
        "enabled": _env_bool("CAPACITY_MONITOR_ENABLED", True),
        "sample_seconds": max(5.0, _env("CAPACITY_SAMPLE_SECONDS", 30.0)),
        "window_seconds": max(60.0, _env("CAPACITY_WINDOW_SECONDS", 300.0)),
        "active_user_minutes": max(1.0, _env("CAPACITY_ACTIVE_USER_MINUTES", 15.0)),
        "cooldown_minutes": max(1.0, _env("CAPACITY_ALERT_COOLDOWN_MINUTES", 30.0)),
        "email_retry_minutes": max(1.0, _env("CAPACITY_EMAIL_RETRY_MINUTES", 5.0)),
        "recovery_samples": max(1, int(_env("CAPACITY_RECOVERY_SAMPLES", 2, float))),
        "min_requests_for_rates": max(1, int(_env("CAPACITY_MIN_REQUESTS_FOR_RATES", 20, float))),
        "send_recovery_email": _env_bool("CAPACITY_SEND_RECOVERY_EMAIL", True),
        "send_reminders": _env_bool("CAPACITY_SEND_REMINDERS", True),
        "alert_email": (os.environ.get("CAPACITY_ALERT_EMAIL") or os.environ.get("ADMIN_EMAIL")
                        or DEFAULT_ALERT_EMAIL).strip(),
        "cpu_limit_cores": _env("CAPACITY_CPU_LIMIT_CORES", None),
        "memory_limit_mb": _env("CAPACITY_MEMORY_LIMIT_MB", None),
        "sample_retention_hours": max(1.0, _env("CAPACITY_SAMPLE_RETENTION_HOURS", 24.0)),
        "event_retention_days": max(1.0, _env("CAPACITY_EVENT_RETENTION_DAYS", 90.0)),
        "thresholds": {},
    }
    for key, (label, unit, wenv, wdef, cenv, cdef, sustain, bottleneck) in METRICS.items():
        warn = _env(wenv, wdef) or None
        crit = _env(cenv, cdef) or None
        cfg["thresholds"][key] = {
            "label": label, "unit": unit, "warning": warn, "critical": crit,
            "sustain": max(1, int(_env(f"CAPACITY_{key.upper()}_SUSTAIN", sustain, float))),
            "bottleneck": bottleneck,
        }
    return cfg


# =====================================================================
# Per-process request counters (the only thing touched per request)
# =====================================================================
class _Counters:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset_interval()
        self.in_flight = 0
        self.ocr_waiting = 0
        self.ocr_running = 0
        self.users_seen = {}

    def reset_interval(self):
        self.requests = 0
        self.errors_5xx = 0
        self.exceptions = 0
        self.db_errors = 0
        self.latency_sum_ms = 0.0
        self.latency_count = 0
        self.latency_hist = [0] * len(LATENCY_BUCKETS_MS)
        self.peak_in_flight = getattr(self, "in_flight", 0)
        self.peak_ocr_waiting = getattr(self, "ocr_waiting", 0)
        self.ocr_jobs = 0
        self.ocr_time_sum_s = 0.0
        self.ocr_time_max_s = 0.0
        self.ocr_unavailable = 0


_counters = _Counters()


def _safe(fn):
    """Decorator: monitoring must never raise into the application."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            _note_monitor_error("hook")
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


_monitor_errors = {"count": 0, "last": None, "last_logged": 0.0}


def _note_monitor_error(where):
    _monitor_errors["count"] += 1
    _monitor_errors["last"] = where
    now = time.time()
    if now - _monitor_errors["last_logged"] > 300:   # log at most every 5 minutes
        _monitor_errors["last_logged"] = now
        logger.exception("Capacity monitor error in %s (the website is unaffected).", where)


@_safe
def request_started(path, user_key=None):
    """Returns a token for request_finished, or None to skip."""
    if path.startswith(EXCLUDED_PATH_PREFIXES):
        return None
    c = _counters
    with c.lock:
        c.in_flight += 1
        if c.in_flight > c.peak_in_flight:
            c.peak_in_flight = c.in_flight
        if user_key:
            c.users_seen[user_key] = time.time()
    return (time.perf_counter(), path in HEAVY_PATHS)


@_safe
def request_finished(token, status_code):
    if token is None:
        return
    started, heavy = token
    ms = (time.perf_counter() - started) * 1000.0
    c = _counters
    with c.lock:
        c.in_flight = max(0, c.in_flight - 1)
        c.requests += 1
        if status_code >= 500:
            c.errors_5xx += 1
        if heavy:
            return
        c.latency_sum_ms += ms
        c.latency_count += 1
        for i, bound in enumerate(LATENCY_BUCKETS_MS):
            if ms <= bound:
                c.latency_hist[i] += 1
                break


@_safe
def record_exception(exc):
    c = _counters
    with c.lock:
        c.exceptions += 1
        if type(exc).__module__.startswith("sqlite3") or type(exc).__name__ in ("OperationalError", "DatabaseError"):
            c.db_errors += 1


# ---- visa reader (OCR) hooks: called from visa_verification.py -------
@_safe
def ocr_waiting_started():
    c = _counters
    with c.lock:
        c.ocr_waiting += 1
        c.peak_ocr_waiting = max(c.peak_ocr_waiting, c.ocr_waiting)


@_safe
def ocr_waiting_finished(acquired):
    c = _counters
    with c.lock:
        c.ocr_waiting = max(0, c.ocr_waiting - 1)
        if acquired:
            c.ocr_running += 1
        else:
            c.ocr_unavailable += 1


@_safe
def ocr_job_finished(seconds, readable):
    c = _counters
    with c.lock:
        c.ocr_running = max(0, c.ocr_running - 1)
        c.ocr_jobs += 1
        c.ocr_time_sum_s += seconds
        c.ocr_time_max_s = max(c.ocr_time_max_s, seconds)
        if not readable:
            c.ocr_unavailable += 1


def _take_interval():
    """Atomically read-and-reset this worker's counters for one interval."""
    c = _counters
    with c.lock:
        data = {
            "requests": c.requests, "errors_5xx": c.errors_5xx, "exceptions": c.exceptions,
            "db_errors": c.db_errors, "latency_sum_ms": round(c.latency_sum_ms, 1),
            "latency_count": c.latency_count, "latency_hist": list(c.latency_hist),
            "peak_in_flight": c.peak_in_flight, "in_flight": c.in_flight,
            "peak_ocr_waiting": c.peak_ocr_waiting, "ocr_waiting": c.ocr_waiting,
            "ocr_running": c.ocr_running, "ocr_jobs": c.ocr_jobs,
            "ocr_time_sum_s": round(c.ocr_time_sum_s, 2), "ocr_time_max_s": round(c.ocr_time_max_s, 2),
            "ocr_unavailable": c.ocr_unavailable,
        }
        users = c.users_seen
        c.users_seen = {}
        c.reset_interval()
    return data, users


# =====================================================================
# Host metrics (container cgroup, disk, network) - background thread only
# =====================================================================
CGROUP_ROOT = "/sys/fs/cgroup"


def _read(path):
    with open(path) as fh:
        return fh.read().strip()


def _read_int(path):
    try:
        return int(_read(path).split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _stat_value(path, key):
    try:
        with open(path) as fh:
            for line in fh:
                k, _, v = line.partition(" ")
                if k == key:
                    return int(v)
    except (OSError, ValueError):
        pass
    return None


def read_cpu_usage_and_limit(root=CGROUP_ROOT):
    """(cumulative CPU seconds used by the container, CPU limit in cores or
    None). Supports cgroup v2 and v1. (None, None) if unreadable."""
    usage = _stat_value(os.path.join(root, "cpu.stat"), "usage_usec")
    if usage is not None:                                   # cgroup v2
        limit = None
        try:
            quota, period = _read(os.path.join(root, "cpu.max")).split()[:2]
            if quota != "max":
                limit = int(quota) / int(period)
        except (OSError, ValueError):
            pass
        return usage / 1e6, limit
    for acct in ("cpuacct/cpuacct.usage", "cpu,cpuacct/cpuacct.usage", "cpuacct.usage"):
        ns = _read_int(os.path.join(root, acct))           # cgroup v1
        if ns is not None:
            limit = None
            for d in ("cpu", "cpu,cpuacct", ""):
                q = _read_int(os.path.join(root, d, "cpu.cfs_quota_us"))
                p = _read_int(os.path.join(root, d, "cpu.cfs_period_us"))
                if q and p and q > 0:
                    limit = q / p
                    break
            return ns / 1e9, limit
    return None, None


def read_memory(root=CGROUP_ROOT):
    """(working-set bytes, limit bytes or None). Working set = usage minus
    reclaimable file cache (what the out-of-memory killer looks at)."""
    cur = _read_int(os.path.join(root, "memory.current"))
    if cur is not None:                                     # cgroup v2
        inactive = _stat_value(os.path.join(root, "memory.stat"), "inactive_file") or 0
        limit = None
        try:
            raw = _read(os.path.join(root, "memory.max"))
            limit = None if raw == "max" else int(raw)
        except (OSError, ValueError):
            pass
        return max(0, cur - inactive), limit
    for d in ("memory",):                                   # cgroup v1
        usage = _read_int(os.path.join(root, d, "memory.usage_in_bytes"))
        if usage is not None:
            stat = os.path.join(root, d, "memory.stat")
            inactive = _stat_value(stat, "total_inactive_file") or _stat_value(stat, "inactive_file") or 0
            limit = _read_int(os.path.join(root, d, "memory.limit_in_bytes"))
            if limit is not None and limit >= 2 ** 60:        # "unlimited"
                limit = None
            return max(0, usage - inactive), limit
    return None, None


def read_network_bytes(path="/proc/net/dev"):
    try:
        total = 0
        with open(path) as fh:
            for line in fh.readlines()[2:]:
                name, _, rest = line.partition(":")
                if name.strip() == "lo":
                    continue
                parts = rest.split()
                total += int(parts[0]) + int(parts[8])
        return total
    except (OSError, ValueError, IndexError):
        return None


def storage_paths():
    import database
    paths = {"database": os.path.dirname(os.path.abspath(database.DB_PATH))}
    upload_root = os.environ.get("UPLOAD_ROOT") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "uploads")
    paths["uploads"] = upload_root
    return paths


def read_storage():
    """{'percent': max used % across the disks we write to, 'disks': {...}}"""
    disks, worst = {}, None
    for name, path in storage_paths().items():
        try:
            u = shutil.disk_usage(path)
            pct = round(100.0 * u.used / u.total, 1) if u.total else None
            disks[name] = {"used_gb": round(u.used / 1e9, 2), "total_gb": round(u.total / 1e9, 2), "percent": pct}
            if pct is not None:
                worst = pct if worst is None else max(worst, pct)
        except OSError:
            disks[name] = None
    return {"percent": worst, "disks": disks}


_uploads_cache = {"at": 0.0, "value": None}


def uploads_size(max_files=200_000, cache_seconds=900):
    """Size of locally stored uploads. Cached (walking the folder is slow)."""
    now = time.time()
    if now - _uploads_cache["at"] < cache_seconds:
        return _uploads_cache["value"]
    root = storage_paths()["uploads"]
    files, size = 0, 0
    try:
        for dirpath, _, names in os.walk(root):
            for n in names:
                if n.startswith("."):
                    continue
                files += 1
                try:
                    size += os.path.getsize(os.path.join(dirpath, n))
                except OSError:
                    pass
                if files >= max_files:
                    break
        value = {"files": files, "mb": round(size / 1e6, 1)}
    except OSError:
        value = None
    _uploads_cache.update(at=now, value=value)
    return value


class _HostSampler:
    """Keeps the previous cumulative CPU/network readings to turn them into rates."""

    def __init__(self):
        self.prev_cpu = None
        self.prev_net = None

    def sample(self, cfg):
        now = time.monotonic()
        out = {}
        cpu_used, cpu_limit = read_cpu_usage_and_limit()
        limit = cpu_limit or cfg.get("cpu_limit_cores") or float(os.cpu_count() or 1)
        out["cpu_limit_cores"] = round(limit, 2)
        out["cpu_limit_source"] = "container limit" if cpu_limit else (
            "CAPACITY_CPU_LIMIT_CORES" if cfg.get("cpu_limit_cores") else "machine cores (no container limit found)")
        if cpu_used is not None and self.prev_cpu is not None and now > self.prev_cpu[1]:
            used = (cpu_used - self.prev_cpu[0]) / (now - self.prev_cpu[1])
            out["cpu_percent"] = round(max(0.0, min(100.0 * used / limit, 999.0)), 1)
        else:
            out["cpu_percent"] = None          # first reading, or not readable
        self.prev_cpu = (cpu_used, now) if cpu_used is not None else None

        mem_used, mem_limit = read_memory()
        if mem_limit is None and cfg.get("memory_limit_mb"):
            mem_limit = int(cfg["memory_limit_mb"] * 1024 * 1024)
        out["memory_used_mb"] = round(mem_used / 1048576, 1) if mem_used is not None else None
        out["memory_limit_mb"] = round(mem_limit / 1048576) if mem_limit else None
        out["memory_percent"] = (round(100.0 * mem_used / mem_limit, 1)
                                 if mem_used is not None and mem_limit else None)

        storage = read_storage()
        out["storage_percent"] = storage["percent"]
        out["storage_disks"] = storage["disks"]

        net = read_network_bytes()
        if net is not None and self.prev_net is not None and now > self.prev_net[1]:
            out["network_kbps"] = round((net - self.prev_net[0]) / (now - self.prev_net[1]) / 1024, 1)
        else:
            out["network_kbps"] = None
        self.prev_net = (net, now) if net is not None else None
        return out


_host = _HostSampler()


# =====================================================================
# Sampling + aggregation (background thread)
# =====================================================================
def _worker_id():
    return f"{os.getpid()}"


def _host_id():
    return os.environ.get("RENDER_INSTANCE_ID") or socket.gethostname()


def _db_roundtrip_ms(db):
    t = time.perf_counter()
    db.execute("SELECT 1").fetchone()
    return (time.perf_counter() - t) * 1000.0


def flush_sample(db, cfg, now=None):
    """Writes this worker's counters (+ host metrics) for the last interval."""
    now = now or time.time()
    data, users = _take_interval()
    data.update(_host.sample(cfg))
    data["interval_s"] = cfg["sample_seconds"]
    try:
        data["db_select_ms"] = round(_db_roundtrip_ms(db), 2)
    except Exception:  # noqa: BLE001
        data["db_select_ms"] = None
        data["db_errors"] += 1
    t = time.perf_counter()
    try:
        db.execute("INSERT INTO capacity_samples (host, worker, created_at, data) VALUES (?, ?, ?, ?)",
                   (_host_id(), _worker_id(), now, json.dumps(data)))
        if users:
            # Only for accounts that still exist: a stale session cookie of a
            # deleted account must not re-create its activity row.
            db.executemany(
                "INSERT INTO capacity_active_users (user_key, last_seen) SELECT ?, ? "
                "WHERE EXISTS (SELECT 1 FROM users WHERE id = CAST(substr(?, instr(?, ':') + 1) AS INTEGER)) "
                "ON CONFLICT(user_key) DO UPDATE SET last_seen = MAX(last_seen, excluded.last_seen)",
                [(k, v, k, k) for k, v in users.items()])
        db.commit()
        data["db_write_ms"] = round((time.perf_counter() - t) * 1000.0, 2)
    except Exception:  # noqa: BLE001
        data["db_write_ms"] = None
        with _counters.lock:
            _counters.db_errors += 1
        raise
    return data


def aggregate(db, cfg, now=None):
    """Combines recent samples from every worker/instance into one view.
    Returns a dict of metric values; unmeasurable values are None."""
    now = now or time.time()
    window_start = now - cfg["window_seconds"]
    fresh_start = now - 2.5 * cfg["sample_seconds"]
    rows = db.execute("SELECT host, worker, created_at, data FROM capacity_samples WHERE created_at >= ? "
                      "ORDER BY created_at", (window_start,)).fetchall()
    samples = []
    for r in rows:
        try:
            samples.append((r[0], r[1], r[2], json.loads(r[3])))
        except (TypeError, ValueError):
            continue

    requests = sum(s[3].get("requests", 0) for s in samples)
    errors = sum(s[3].get("errors_5xx", 0) for s in samples)
    exceptions = sum(s[3].get("exceptions", 0) for s in samples)
    lat_sum = sum(s[3].get("latency_sum_ms", 0) for s in samples)
    lat_n = sum(s[3].get("latency_count", 0) for s in samples)
    hist = [0] * len(LATENCY_BUCKETS_MS)
    for s in samples:
        for i, v in enumerate(s[3].get("latency_hist", [])[:len(hist)]):
            hist[i] += v
    covered = 0.0
    if samples:
        covered = min(cfg["window_seconds"], max(cfg["sample_seconds"], now - samples[0][2] + cfg["sample_seconds"]))

    enough = requests >= cfg["min_requests_for_rates"]
    out = {
        "window_minutes": round(cfg["window_seconds"] / 60, 1),
        "requests": requests,
        "requests_per_minute": round(requests / (covered / 60.0), 1) if covered else None,
        "errors_5xx": errors,
        "exceptions": exceptions,
        "error_rate_percent": round(100.0 * errors / requests, 2) if enough else None,
        "latency_avg_ms": round(lat_sum / lat_n, 1) if lat_n else None,
        "latency_p95_ms": _percentile_from_hist(hist, 95) if lat_n >= cfg["min_requests_for_rates"] else None,
        "rates_note": None if enough else f"fewer than {cfg['min_requests_for_rates']} requests in the window",
    }

    # "current" values: latest sample per worker that is still fresh
    latest = {}
    for host, worker, ts, d in samples:
        if ts >= fresh_start:
            latest[(host, worker)] = (ts, d)
    per_host = {}
    for (host, _), (_, d) in latest.items():
        per_host.setdefault(host, []).append(d)
    out["workers_reporting"] = len(latest)
    out["instances_reporting"] = len(per_host)
    out["concurrent_requests"] = sum(d.get("peak_in_flight", 0) for _, d in latest.values()) if latest else None
    out["ocr_queue"] = sum(d.get("peak_ocr_waiting", 0) for _, d in latest.values()) if latest else None
    out["ocr_running"] = sum(d.get("ocr_running", 0) for _, d in latest.values()) if latest else None

    ocr_jobs = sum(s[3].get("ocr_jobs", 0) for s in samples)
    out["ocr_jobs"] = ocr_jobs
    out["ocr_avg_seconds"] = (round(sum(s[3].get("ocr_time_sum_s", 0) for s in samples) / ocr_jobs, 1)
                              if ocr_jobs else None)
    out["ocr_max_seconds"] = max((s[3].get("ocr_time_max_s", 0) for s in samples), default=None) if ocr_jobs else None
    out["ocr_unavailable"] = sum(s[3].get("ocr_unavailable", 0) for s in samples)

    def worst(key):
        vals = [d.get(key) for ds in per_host.values() for d in ds if d.get(key) is not None]
        return max(vals) if vals else None
    for key in ("cpu_percent", "memory_percent", "storage_percent", "memory_used_mb", "network_kbps"):
        out[key] = worst(key)
    any_d = next(iter(latest.values()), (None, {}))[1]
    out["memory_limit_mb"] = any_d.get("memory_limit_mb")
    out["cpu_limit_cores"] = any_d.get("cpu_limit_cores")
    out["cpu_limit_source"] = any_d.get("cpu_limit_source")
    out["storage_disks"] = any_d.get("storage_disks")
    db_ms = [max(d.get("db_select_ms") or 0, d.get("db_write_ms") or 0) for _, d in latest.values()
             if d.get("db_select_ms") is not None]
    out["db_latency_ms"] = round(max(db_ms), 1) if db_ms else None
    out["db_errors"] = sum(s[3].get("db_errors", 0) for s in samples)

    cutoff = now - cfg["active_user_minutes"] * 60
    out["active_users"] = db.execute("SELECT COUNT(*) FROM capacity_active_users WHERE last_seen >= ?",
                                     (cutoff,)).fetchone()[0]
    out["active_students"] = db.execute(
        "SELECT COUNT(*) FROM capacity_active_users WHERE last_seen >= ? AND user_key LIKE 'student:%'",
        (cutoff,)).fetchone()[0]
    return out


def _percentile_from_hist(hist, pct):
    total = sum(hist)
    if not total:
        return None
    target = math.ceil(total * pct / 100.0)
    running = 0
    for i, n in enumerate(hist):
        running += n
        if running >= target:
            bound = LATENCY_BUCKETS_MS[i]
            return float(bound) if bound != math.inf else float(LATENCY_BUCKETS_MS[-2])
    return None


# =====================================================================
# Threshold evaluation, alert state, events, e-mail
# =====================================================================
def level_for(value, threshold):
    if value is None:
        return None                                # unavailable: never guessed
    crit, warn = threshold.get("critical"), threshold.get("warning")
    if crit is not None and value >= crit:
        return "critical"
    if warn is not None and value >= warn:
        return "warning"
    return "normal"


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt(value, unit):
    if value is None:
        return "unavailable"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value}{unit}" if unit in ("%", "ms") else (f"{value} {unit}".strip())


def _record_event(db, now, level, metric, value, threshold, message, sent=False, error=None):
    db.execute("INSERT INTO capacity_events (created_at, level, metric, value, threshold, message, "
               "notification_sent, notification_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (_iso(now), level, metric, value, threshold, message, 1 if sent else 0, error))
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def evaluate(db, cfg, metrics, now=None, send=None):
    """Applies thresholds with sustain/cooldown rules, records events on
    level CHANGES only, and e-mails the admin when needed.

    Returns {'status', 'levels', 'notified', 'email_error'}.
    `send(to, subject, body) -> (ok, error)`; defaults to email_lib.send_email.
    """
    now = now or time.time()
    if send is None:
        import email_lib
        send = email_lib.send_email
    th = cfg["thresholds"]
    cooldown = cfg["cooldown_minutes"] * 60
    retry = cfg["email_retry_minutes"] * 60
    levels, to_notify, recoveries = {}, [], []

    for key, t in th.items():
        value = metrics.get(key)
        observed = level_for(value, t)
        cur = db.execute("SELECT * FROM capacity_alert_state WHERE metric = ?", (key,))
        row = cur.fetchone()
        if row is None:
            db.execute("INSERT INTO capacity_alert_state (metric, level, since) VALUES (?, 'normal', ?)", (key, now))
            cur = db.execute("SELECT * FROM capacity_alert_state WHERE metric = ?", (key,))
            row = cur.fetchone()
        state = dict(zip([c[0] for c in cur.description], row))
        current = state["level"]
        if observed is None:                       # missing metric: keep state, never alert on a guess
            levels[key] = current if current != "normal" else "unavailable"
            continue

        if observed == current:
            state["pending_level"], state["pending_count"] = None, 0
        else:
            if state["pending_level"] == observed:
                state["pending_count"] += 1
            else:
                state["pending_level"], state["pending_count"] = observed, 1
            # Raising a level needs `sustain` consecutive readings; lowering it
            # needs `recovery_samples` - so brief spikes don't flap.
            needed = cfg["recovery_samples"] if _RANK[observed] < _RANK[current] else t["sustain"]
            if state["pending_count"] >= needed:
                previous, current = current, observed
                state["level"] = current
                state["pending_level"], state["pending_count"] = None, 0
                thr = t["critical"] if current == "critical" else t["warning"]
                if current == "normal":
                    msg = f"{t['label']} back to normal ({_fmt(value, t['unit'])})."
                    eid = _record_event(db, now, "recovered", key, value, None, msg)
                    alerted = state["last_notified_at"] and state["since"] and state["last_notified_at"] >= state["since"]
                    if cfg["send_recovery_email"] and alerted:
                        recoveries.append((key, value, eid))
                    state["since"] = now
                else:
                    if previous == "normal":
                        state["since"] = now       # start of this problem episode
                    direction = "rose to" if _RANK[current] > _RANK[previous] else "dropped to"
                    msg = (f"{t['label']} {direction} {current.upper()}: {_fmt(value, t['unit'])} "
                           f"(threshold {_fmt(thr, t['unit'])}).")
                    eid = _record_event(db, now, current, key, value, thr, msg)
                    if _RANK[current] > _RANK[previous]:
                        # Identical (or lower) alert already sent within the cooldown -> suppress.
                        # A CRITICAL after a recent WARNING is NOT identical -> always sent.
                        recent_same = (state["last_notified_at"] is not None
                                       and now - state["last_notified_at"] < cooldown
                                       and state["last_notified_level"] is not None
                                       and _RANK[state["last_notified_level"]] >= _RANK[current])
                        if not recent_same:
                            to_notify.append((key, current, value, thr, eid))

        # Still elevated with nothing queued: retry a failed e-mail after
        # `email_retry_minutes`, or remind once per cooldown period.
        if current != "normal" and not any(n[0] == key for n in to_notify):
            last_ok, last_try = state["last_notified_at"], state["last_attempt_at"]
            failed_last = last_try is not None and (last_ok is None or last_try > last_ok)
            if failed_last:
                due = now - last_try >= retry
            elif last_ok is None or _RANK.get(state["last_notified_level"] or "normal", 0) < _RANK[current]:
                due = True                          # this level was never successfully announced
            else:
                due = cfg["send_reminders"] and now - last_ok >= cooldown
            if due:
                thr = t["critical"] if current == "critical" else t["warning"]
                to_notify.append((key, current, value, thr, None))
        state["last_value"] = value
        levels[key] = current
        db.execute("""UPDATE capacity_alert_state SET level=?, pending_level=?, pending_count=?, since=?,
                      last_value=? WHERE metric=?""",
                   (state["level"], state["pending_level"], state["pending_count"], state["since"],
                    state["last_value"], key))
    db.commit()

    status = max((lv for lv in levels.values() if lv in _RANK), key=lambda lv: _RANK[lv], default="normal")
    result = {"status": status, "levels": levels, "notified": False, "email_error": None}

    if to_notify:
        worst_level = max((n[1] for n in to_notify), key=lambda lv: _RANK[lv])
        subject, body = build_alert_email(worst_level, metrics, levels, to_notify, cfg)
        ok, err = _send_safely(send, cfg["alert_email"], subject, body)
        for key, level, _, _, eid in to_notify:
            db.execute("UPDATE capacity_alert_state SET last_attempt_at=? WHERE metric=?", (now, key))
            if ok:
                db.execute("UPDATE capacity_alert_state SET last_notified_at=?, last_notified_level=? WHERE metric=?",
                           (now, level, key))
            if eid is not None:
                db.execute("UPDATE capacity_events SET notification_sent=?, notification_error=? WHERE id=?",
                           (1 if ok else 0, err, eid))
            elif ok:
                _record_event(db, now, level, key, metrics.get(key),
                              th[key]["critical"] if level == "critical" else th[key]["warning"],
                              f"{th[key]['label']} still {level.upper()} "
                              f"({_fmt(metrics.get(key), th[key]['unit'])}) - e-mail sent.", sent=True)
        result.update(notified=ok, email_error=err)
    if recoveries:
        subject, body = build_recovery_email(metrics, [r[0] for r in recoveries], cfg, status)
        ok, err = _send_safely(send, cfg["alert_email"], subject, body)
        for _, _, eid in recoveries:
            db.execute("UPDATE capacity_events SET notification_sent=?, notification_error=? WHERE id=?",
                       (1 if ok else 0, err, eid))
    db.commit()
    return result


def _send_safely(send, to, subject, body):
    try:
        ok, err = send(to, subject, body)
        return bool(ok), (None if ok else (str(err)[:60] if err else "send_failed"))
    except Exception:  # noqa: BLE001
        return False, "send_exception"


def _metric_lines(metrics, levels, cfg):
    icon = {"normal": "OK", "warning": "WARNING", "critical": "CRITICAL", "unavailable": "n/a"}
    lines = []
    for key, t in cfg["thresholds"].items():
        if t["warning"] is None and t["critical"] is None and metrics.get(key) is None:
            continue
        lv = levels.get(key, "normal")
        lines.append(f"  {t['label']}: {_fmt(metrics.get(key), t['unit'])}  [{icon.get(lv, lv)}]")
    return lines


def build_alert_email(level, metrics, levels, triggered, cfg):
    critical = level == "critical"
    title = "AFRICA SCHOLARBRIDGE CAPACITY CRITICAL ALERT" if critical else "AFRICA SCHOLARBRIDGE CAPACITY WARNING"
    subject = ("[CRITICAL] Africa ScholarBridge - immediate action may be required" if critical
               else "[WARNING] Africa ScholarBridge is approaching its resource limits")
    th = cfg["thresholds"]
    trig = [f"  - {th[k]['label']}: {_fmt(v, th[k]['unit'])} (threshold {_fmt(thr, th[k]['unit'])}, {lv.upper()})"
            for k, lv, v, thr, _ in triggered]
    bottlenecks = []
    for k, *_ in triggered:
        b = th[k]["bottleneck"]
        if b not in bottlenecks:
            bottlenecks.append(b)
    body = [title, ""]
    body.append("IMMEDIATE ACTION MAY BE REQUIRED. Students may already be affected."
                if critical else "Your website is approaching its current resource limits.")
    body += ["", "Triggered:", *trig, "", "Current readings:"]
    body += [
        f"  CPU: {_fmt(metrics.get('cpu_percent'), '%')}",
        f"  Memory: {_fmt(metrics.get('memory_percent'), '%')}",
        f"  Storage: {_fmt(metrics.get('storage_percent'), '%')}",
        f"  Recently Active Users (signed in, last {int(cfg['active_user_minutes'])} min): "
        f"{_fmt(metrics.get('active_users'), '')}",
        f"  Concurrent requests (peak, last {int(cfg['sample_seconds'])} s): {_fmt(metrics.get('concurrent_requests'), '')}",
        f"  HTTP error rate: {_fmt(metrics.get('error_rate_percent'), '%')}",
        f"  Response time p95: {_fmt(metrics.get('latency_p95_ms'), 'ms')}",
        f"  Visa reader queue: {_fmt(metrics.get('ocr_queue'), '')}",
        "", "All monitored metrics:", *_metric_lines(metrics, levels, cfg),
        "", "Recommended action:",
    ]
    body += [f"  - {BOTTLENECK_ADVICE[b]}" for b in bottlenecks]
    body += ["  - Review Render metrics (CPU, memory, bandwidth) and the Admin Dashboard -> System Capacity.",
             "", "\"Recently Active Users\" counts signed-in accounts seen recently; it is not an exact number of "
             "simultaneous users.",
             "", f"Time: {_iso(time.time())}",
             "This is an automated infrastructure " + ("alert." if critical else "warning.")]
    return subject, "\n".join(body)


def build_recovery_email(metrics, keys, cfg, overall="normal"):
    th = cfg["thresholds"]
    subject = ("[RECOVERED] Africa ScholarBridge capacity back to normal" if overall == "normal"
               else f"[PARTLY RECOVERED] Africa ScholarBridge - overall status still {overall.upper()}")
    body = ["AFRICA SCHOLARBRIDGE CAPACITY RECOVERED", "",
            "These metrics have returned to normal:"]
    body += [f"  - {th[k]['label']}: {_fmt(metrics.get(k), th[k]['unit'])}" for k in keys]
    if overall != "normal":
        body += ["", f"Overall status is still {overall.upper()} - see the Admin Dashboard -> System Capacity."]
    body += ["", f"Time: {_iso(time.time())}", "This is an automated infrastructure notice."]
    return subject, "\n".join(body)


# =====================================================================
# One monitoring cycle + background thread
# =====================================================================
def _claim_evaluation(db, cfg, now):
    """Exactly one process (across workers/instances sharing the database)
    evaluates per interval."""
    db.execute("INSERT OR IGNORE INTO capacity_meta (key, value) VALUES ('last_eval', '0')")
    cur = db.execute("UPDATE capacity_meta SET value=? WHERE key='last_eval' AND CAST(value AS REAL) <= ?",
                     (str(now), now - 0.8 * cfg["sample_seconds"]))
    db.commit()
    return cur.rowcount == 1


def _prune(db, cfg, now):
    db.execute("DELETE FROM capacity_samples WHERE created_at < ?", (now - cfg["sample_retention_hours"] * 3600,))
    db.execute("DELETE FROM capacity_active_users WHERE last_seen < ?", (now - 86400,))
    db.execute("DELETE FROM capacity_events WHERE created_at < ?",
               (_iso(now - cfg["event_retention_days"] * 86400),))
    db.commit()


def run_cycle(db, cfg=None, now=None, send=None):
    """Flush this worker's sample; if this process wins the claim, evaluate
    and store the dashboard snapshot. Returns the snapshot or None."""
    cfg = cfg or load_config()
    now = now or time.time()
    flush_sample(db, cfg, now)
    if not _claim_evaluation(db, cfg, now):
        return None
    metrics = aggregate(db, cfg, now)
    result = evaluate(db, cfg, metrics, now, send=send)
    snapshot = {"at": now, "metrics": metrics, "status": result["status"], "levels": result["levels"]}
    db.execute("INSERT INTO capacity_meta (key, value) VALUES ('last_snapshot', ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(snapshot),))
    row = db.execute("SELECT value FROM capacity_meta WHERE key='last_prune'").fetchone()
    if row is None or now - float(row[0] or 0) >= 3600:  # once an hour
        _prune(db, cfg, now)
        db.execute("INSERT INTO capacity_meta (key, value) VALUES ('last_prune', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(now),))
    db.commit()
    return snapshot


_thread_state = {"pid": None, "thread": None}
_start_lock = threading.Lock()


def _loop():
    import database
    while True:
        cfg = None
        try:
            cfg = load_config()
            if cfg["enabled"]:
                db = database.get_db()
                try:
                    run_cycle(db, cfg)
                finally:
                    db.close()
        except Exception:  # noqa: BLE001
            _note_monitor_error("background cycle")
        time.sleep((cfg or {}).get("sample_seconds", 30.0))


@_safe
def ensure_started():
    """Starts the background thread once per process (after gunicorn forks)."""
    if _thread_state["pid"] == os.getpid():
        return
    with _start_lock:
        if _thread_state["pid"] == os.getpid():
            return
        if not load_config()["enabled"]:
            _thread_state["pid"] = os.getpid()
            return
        t = threading.Thread(target=_loop, name="capacity-monitor", daemon=True)
        t.start()
        _thread_state.update(pid=os.getpid(), thread=t)


# =====================================================================
# Flask integration
# =====================================================================
def init_app(app):
    """Registers the lightweight request hooks. Safe to call once."""
    from flask import g, request, session, got_request_exception

    def _user_key():
        if session.get("role") in ("student", "admin") and session.get("user_id"):
            return f"{session['role']}:{session['user_id']}"
        if session.get("visa_admin_user_id"):
            return f"visa_admin:{session['visa_admin_user_id']}"
        return None

    def _before():
        try:
            if not app.config.get("TESTING"):
                ensure_started()
            path = request.path
            if not path.startswith(EXCLUDED_PATH_PREFIXES):
                g._cap_token = request_started(path, _user_key())
        except Exception:  # noqa: BLE001
            _note_monitor_error("before_request")
        return None

    def _after(response):
        try:
            g._cap_status = response.status_code
        except Exception:  # noqa: BLE001
            pass
        return response

    def _teardown(exc):
        try:
            token = g.pop("_cap_token", None)
            status = 500 if exc is not None else g.pop("_cap_status", 200)
            request_finished(token, status)
        except Exception:  # noqa: BLE001
            _note_monitor_error("teardown_request")

    def _on_exception(sender, exception, **extra):
        record_exception(exception)

    app.before_request_funcs.setdefault(None, []).insert(0, _before)   # first: time the whole request
    app.after_request(_after)
    app.teardown_request(_teardown)
    got_request_exception.connect(_on_exception, app, weak=False)


# =====================================================================
# Dashboard data
# =====================================================================
def infrastructure_warnings():
    """Detected from the actual configuration, not assumed."""
    import database
    warnings = []
    if not os.environ.get("DATABASE_URL") and str(database.DB_PATH).endswith((".db", ".sqlite", ".sqlite3")):
        warnings.append("SQLite detected — PostgreSQL recommended before large-scale concurrent usage.")
    upload_root = storage_paths()["uploads"]
    if upload_root and not upload_root.startswith(("s3://", "gs://", "https://")):
        warnings.append("Local file storage detected — object storage recommended before large-scale "
                        "horizontal scaling.")
    warnings.append("The visa document reader (OCR) runs inside the web service, one document at a time. "
                    "A separate background worker service with a job queue is recommended for high upload volumes.")
    return warnings


DASHBOARD_EXTRA_KEYS = ["window_minutes", "requests", "latency_avg_ms", "rates_note", "errors_5xx",
                        "exceptions", "workers_reporting", "instances_reporting", "ocr_running", "ocr_jobs",
                        "ocr_avg_seconds", "ocr_max_seconds", "memory_used_mb", "memory_limit_mb",
                        "cpu_limit_cores", "cpu_limit_source", "storage_disks", "network_kbps", "active_students"]


def dashboard_snapshot(db):
    """Everything the admin dashboard shows. Never raises (returns a
    'degraded' snapshot instead)."""
    cfg = load_config()
    snap = {"enabled": cfg["enabled"], "config": cfg, "degraded": None, "stale": False}
    try:
        row = db.execute("SELECT value FROM capacity_meta WHERE key='last_snapshot'").fetchone()
        last = json.loads(row[0]) if row and row[0] else None
    except Exception:  # noqa: BLE001
        last = None
        snap["degraded"] = "Could not read monitoring data."
    if last:
        snap.update(metrics=last["metrics"], status=last["status"], levels=last["levels"], at=last["at"],
                    at_iso=_iso(last["at"]), age_seconds=int(time.time() - last["at"]))
        snap["stale"] = snap["age_seconds"] > 4 * cfg["sample_seconds"]
    else:
        snap.update(metrics={}, status="unknown", levels={}, at=None, at_iso=None, age_seconds=None)
    # Every value the dashboard shows exists (None = "—"), even before the
    # first background check has run - e.g. right after a deploy.
    for key in list(METRICS) + DASHBOARD_EXTRA_KEYS:
        snap["metrics"].setdefault(key, None)
    try:
        storage = read_storage()                  # cheap, always live
        if snap["metrics"].get("storage_percent") is None:
            snap["metrics"]["storage_percent"] = storage["percent"]
        snap["metrics"]["storage_disks"] = snap["metrics"].get("storage_disks") or storage["disks"]
    except Exception:  # noqa: BLE001
        pass
    try:
        snap["events"] = [dict(zip(("created_at", "level", "metric", "value", "threshold", "message",
                                     "notification_sent", "notification_error"), r)) for r in db.execute(
            "SELECT created_at, level, metric, value, threshold, message, notification_sent, notification_error "
            "FROM capacity_events ORDER BY id DESC LIMIT 25").fetchall()]
    except Exception:  # noqa: BLE001
        snap["events"] = []
    try:
        snap["uploads"] = uploads_size()
    except Exception:  # noqa: BLE001
        snap["uploads"] = None
    snap["infrastructure_warnings"] = infrastructure_warnings()
    hints = []
    m = snap["metrics"]
    if snap["at"] and not m.get("memory_limit_mb"):
        hints.append("The container's memory limit can't be read, so Memory % (and memory alerts) are unavailable. "
                     "Set CAPACITY_MEMORY_LIMIT_MB on Render to your plan's RAM (e.g. 512).")
    if snap["at"] and (m.get("cpu_limit_source") or "").startswith("machine cores"):
        hints.append("The container's CPU limit can't be read, so CPU % is measured against the machine's cores and "
                     "may look too low. Set CAPACITY_CPU_LIMIT_CORES on Render to your plan's CPU (e.g. 0.5).")
    snap["setup_hints"] = hints
    snap["monitor_errors"] = _monitor_errors["count"]
    if snap["stale"] and cfg["enabled"]:
        snap["degraded"] = snap["degraded"] or "No fresh monitoring data - the background monitor may not be running."
    rows = []
    for key, t in cfg["thresholds"].items():
        value = snap["metrics"].get(key)
        reading = level_for(value, t) or "unavailable"          # what the value is right now
        alert = snap["levels"].get(key)                         # confirmed alert state
        rows.append({"key": key, "label": t["label"], "unit": t["unit"], "value": value,
                     "display": _fmt(value, t["unit"]), "warning": t["warning"], "critical": t["critical"],
                     "level": reading, "alert_level": alert if alert in _RANK else None,
                     "pending": (reading in _RANK and _RANK[reading] > _RANK.get(alert or "normal", 0)),
                     "sustain": t["sustain"], "bottleneck": t["bottleneck"]})
    snap["rows"] = rows
    return snap


def send_test_alert(db, send=None):
    """Sends a clearly-labelled TEST e-mail and records it. Returns (ok, error)."""
    cfg = load_config()
    if send is None:
        import email_lib
        send = email_lib.send_email
    subject = "[TEST] Africa ScholarBridge capacity alert"
    body = ("AFRICA SCHOLARBRIDGE CAPACITY ALERT - TEST\n\nThis is a test of the capacity monitoring "
            "e-mail. No action is needed.\n\nIf you received this, capacity warnings will reach this inbox.\n"
            f"Time: {_iso(time.time())}")
    ok, err = _send_safely(send, cfg["alert_email"], subject, body)
    _record_event(db, time.time(), "test", "test_alert", None, None,
                  "Test alert e-mail " + ("sent." if ok else "could not be sent."), sent=ok, error=err)
    db.commit()
    return ok, err


if __name__ == "__main__":
    import sys
    import database
    database.init_db()
    conn = database.get_db()
    if "--test-alert" in sys.argv:
        ok, err = send_test_alert(conn)
        print("Test alert sent to", load_config()["alert_email"] if ok else f"NOT sent ({err})")
    else:
        print(json.dumps(dashboard_snapshot(conn), indent=2, default=str)[:4000])
