"""Capacity monitoring: thresholds, sustain/cooldown/recovery rules, e-mail
behaviour, metric readers, aggregation, dashboard, security, and proof
that monitoring failures never affect the website."""
import json
import os
import time
from collections import namedtuple

import pytest

import capacity_monitor as cm
import database

SECRETS = {
    "SECRET_KEY": "sk-SUPER-SECRET-flask-key-123",
    "MAIL_PASSWORD": "smtp-PASSWORD-do-not-leak",
    "MAIL_USERNAME": "smtp-user-secret@example.com",
    "MPESA_CONSUMER_SECRET": "mpesa-SECRET-xyz",
    "MPESA_PASSKEY": "mpesa-PASSKEY-abc",
    "DATABASE_URL": "",
}


# ---------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------
@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A fresh, fully-migrated database for each test."""
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "cap.db"))
    database.init_db()
    conn = database.get_db()
    yield conn
    conn.close()


@pytest.fixture()
def cfg(monkeypatch):
    for k in list(os.environ):
        if k.startswith("CAPACITY_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CAPACITY_ALERT_EMAIL", "africascholarbridge@gmail.com")
    return cm.load_config()


class Outbox:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, to, subject, body):
        if self.fail:
            return False, "send_failed"
        self.sent.append((to, subject, body))
        return True, None


def normal_metrics(**over):
    m = {"cpu_percent": 20.0, "memory_percent": 40.0, "storage_percent": 30.0, "error_rate_percent": 0.0,
         "latency_p95_ms": 200.0, "concurrent_requests": 1, "ocr_queue": 0, "ocr_unavailable": 0,
         "db_latency_ms": 5.0, "db_errors": 0, "active_users": 10, "requests_per_minute": 30.0}
    m.update(over)
    return m


def run(db, cfg, metrics, t, outbox):
    return cm.evaluate(db, cfg, metrics, now=t, send=outbox)


def events(db):
    return db.execute("SELECT level, metric, notification_sent, notification_error FROM capacity_events "
                      "ORDER BY id").fetchall()


T0 = 1_800_000_000.0
MIN = 60.0


# ---------------------------------------------------------------------
# 1. normal / warning / critical / recovery
# ---------------------------------------------------------------------
def test_normal_state_sends_nothing(db, cfg):
    out = Outbox()
    for i in range(10):
        r = run(db, cfg, normal_metrics(), T0 + i * 30, out)
    assert r["status"] == "normal" and out.sent == [] and events(db) == []


def test_warning_needs_sustained_readings_then_emails_once(db, cfg):
    out = Outbox()
    # CPU sustain = 3 readings: two high readings are not enough
    run(db, cfg, normal_metrics(cpu_percent=74), T0, out)
    r = run(db, cfg, normal_metrics(cpu_percent=74), T0 + 30, out)
    assert r["status"] == "normal" and out.sent == []
    r = run(db, cfg, normal_metrics(cpu_percent=74), T0 + 60, out)
    assert r["status"] == "warning" and len(out.sent) == 1
    to, subject, body = out.sent[0]
    assert to == "africascholarbridge@gmail.com"
    assert "[WARNING]" in subject and "AFRICA SCHOLARBRIDGE CAPACITY WARNING" in body
    assert "CPU: 74%" in body and "Recommended action" in body and "CPU bottleneck" in body
    assert [tuple(e) for e in events(db)] == [("warning", "cpu_percent", 1, None)]


def test_critical_is_sent_even_right_after_a_warning(db, cfg):
    out = Outbox()
    for i in range(3):
        run(db, cfg, normal_metrics(memory_percent=75), T0 + i * 30, out)   # memory sustain = 2
    assert len(out.sent) == 1
    for i in range(3, 5):
        r = run(db, cfg, normal_metrics(memory_percent=91), T0 + i * 30, out)
    assert r["status"] == "critical" and len(out.sent) == 2
    subject, body = out.sent[1][1], out.sent[1][2]
    assert "[CRITICAL]" in subject and "IMMEDIATE ACTION MAY BE REQUIRED" in body and "RAM bottleneck" in body


def test_recovery_is_recorded_and_emailed(db, cfg):
    out = Outbox()
    for i in range(3):
        run(db, cfg, normal_metrics(cpu_percent=90), T0 + i * 30, out)
    assert len(out.sent) == 1
    run(db, cfg, normal_metrics(), T0 + 120, out)
    assert len(out.sent) == 1                        # one normal reading is not a recovery yet
    r = run(db, cfg, normal_metrics(), T0 + 150, out)
    assert r["status"] == "normal"
    assert "[RECOVERED]" in out.sent[-1][1]
    assert [e[0] for e in events(db)] == ["critical", "recovered"]


def test_recovery_email_can_be_disabled(db, cfg, monkeypatch):
    monkeypatch.setenv("CAPACITY_SEND_RECOVERY_EMAIL", "0")
    cfg = cm.load_config()
    out = Outbox()
    for i in range(3):
        run(db, cfg, normal_metrics(cpu_percent=90), T0 + i * 30, out)
    for i in range(3, 6):
        run(db, cfg, normal_metrics(), T0 + i * 30, out)
    assert len(out.sent) == 1 and events(db)[-1][0] == "recovered"


# ---------------------------------------------------------------------
# 2. cooldown / repeated alerts / no event spam
# ---------------------------------------------------------------------
def test_no_identical_alert_within_cooldown_then_one_reminder(db, cfg):
    out = Outbox()
    t = T0
    for _ in range(3):                                           # becomes warning, 1 e-mail
        run(db, cfg, normal_metrics(cpu_percent=75), t, out)
        t += 30
    for _ in range(int(29 * MIN / 30)):                          # 29 more minutes of warning
        run(db, cfg, normal_metrics(cpu_percent=75), t, out)
        t += 30
    assert len(out.sent) == 1
    run(db, cfg, normal_metrics(cpu_percent=75), T0 + 60 + 30 * MIN, out)   # cooldown over
    assert len(out.sent) == 2 and "[WARNING]" in out.sent[1][1]
    assert sum(1 for e in events(db) if e[0] == "warning") == 2   # original + one reminder, not 60


def test_flapping_warning_does_not_spam(db, cfg):
    """Warning -> normal -> warning within the cooldown sends only once."""
    out = Outbox()
    t = T0
    for cycle in range(6):
        for _ in range(3):
            run(db, cfg, normal_metrics(cpu_percent=75), t, out)
            t += 30
        for _ in range(2):
            run(db, cfg, normal_metrics(cpu_percent=10), t, out)
            t += 30
    warnings = [s for s in out.sent if "[WARNING]" in s[1]]
    assert len(warnings) == 1


def test_repeated_evaluations_do_not_create_duplicate_events(db, cfg):
    out = Outbox()
    for i in range(200):                                         # 200 checks at a steady WARNING
        run(db, cfg, normal_metrics(storage_percent=80), T0 + i * 5, out)
    assert len(events(db)) == 1 and len(out.sent) == 1


def test_reminders_can_be_disabled(db, cfg, monkeypatch):
    monkeypatch.setenv("CAPACITY_SEND_REMINDERS", "0")
    cfg = cm.load_config()
    out = Outbox()
    for i in range(400):
        run(db, cfg, normal_metrics(storage_percent=80), T0 + i * 30, out)   # 3+ hours
    assert len(out.sent) == 1


def test_cooldown_is_configurable(db, cfg, monkeypatch):
    monkeypatch.setenv("CAPACITY_ALERT_COOLDOWN_MINUTES", "5")
    cfg = cm.load_config()
    out = Outbox()
    run(db, cfg, normal_metrics(storage_percent=80), T0, out)
    run(db, cfg, normal_metrics(storage_percent=80), T0 + 4 * MIN, out)
    run(db, cfg, normal_metrics(storage_percent=80), T0 + 5 * MIN + 1, out)
    assert len(out.sent) == 2


# ---------------------------------------------------------------------
# 3. missing metrics, monitoring failure, e-mail failure
# ---------------------------------------------------------------------
def test_missing_metrics_are_never_guessed_or_alerted(db, cfg):
    out = Outbox()
    blank = {k: None for k in normal_metrics()}
    for i in range(10):
        r = run(db, cfg, blank, T0 + i * 30, out)
    assert out.sent == [] and events(db) == []
    assert set(r["levels"].values()) == {"unavailable"} and r["status"] == "normal"


def test_missing_metric_keeps_existing_alert_state(db, cfg):
    out = Outbox()
    for i in range(3):
        run(db, cfg, normal_metrics(cpu_percent=90), T0 + i * 30, out)
    r = run(db, cfg, normal_metrics(cpu_percent=None), T0 + 120, out)
    assert r["levels"]["cpu_percent"] == "critical"                # no fake recovery


def test_email_failure_is_recorded_and_retried_with_backoff(db, cfg):
    bad = Outbox(fail=True)
    for i in range(3):
        run(db, cfg, normal_metrics(cpu_percent=90), T0 + i * 30, bad)
    ev = events(db)[0]
    assert ev["notification_sent"] == 0 and ev["notification_error"] == "send_failed"
    good = Outbox()
    for i in range(3, 12):                                         # next 4.5 min: no retry yet
        run(db, cfg, normal_metrics(cpu_percent=90), T0 + i * 30, good)
    assert good.sent == []
    run(db, cfg, normal_metrics(cpu_percent=90), T0 + 60 + 5 * MIN, good)   # retry after 5 min
    assert len(good.sent) == 1 and "[CRITICAL]" in good.sent[0][1]


def test_email_exception_never_escapes(db, cfg):
    def explode(*a):
        raise RuntimeError("smtp exploded")
    for i in range(3):
        r = run(db, cfg, normal_metrics(cpu_percent=90), T0 + i * 30, explode)
    assert r["status"] == "critical" and r["email_error"] == "send_exception"


def test_monitoring_hook_failure_never_breaks_a_request(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("monitor broken")
    monkeypatch.setattr(cm._counters, "lock", None)               # every counter update now fails
    monkeypatch.setattr(cm, "ensure_started", boom)
    r = client.get("/")
    assert r.status_code == 200


def test_background_cycle_failure_is_contained(db, cfg, monkeypatch):
    monkeypatch.setattr(cm, "aggregate", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(cm.time, "sleep", lambda s: (_ for _ in ()).throw(SystemExit))
    before = cm._monitor_errors["count"]
    with pytest.raises(SystemExit):                                # loop survives the error, reaches sleep
        cm._loop()
    assert cm._monitor_errors["count"] == before + 1


def test_dashboard_still_renders_when_monitoring_fails(client, admin, monkeypatch):
    monkeypatch.setattr(cm, "dashboard_snapshot", lambda db: 1 / 0)
    r = client.get("/admin/dashboard")
    assert r.status_code == 200
    assert "Capacity monitoring is temporarily unavailable" in r.get_data(as_text=True)


# ---------------------------------------------------------------------
# 4. high users / requests / OCR queue / storage / memory
# ---------------------------------------------------------------------
def test_high_active_users_alerts_only_when_threshold_configured(db, cfg, monkeypatch):
    out = Outbox()
    run(db, cfg, normal_metrics(active_users=5000), T0, out)
    assert out.sent == []                                         # default: informational only
    monkeypatch.setenv("CAPACITY_ACTIVE_USERS_WARN", "150")
    monkeypatch.setenv("CAPACITY_ACTIVE_USERS_CRIT", "300")
    cfg = cm.load_config()
    r = run(db, cfg, normal_metrics(active_users=310), T0 + 30, out)
    assert r["levels"]["active_users"] == "critical" and "Recently Active Users" in out.sent[0][2]


def test_active_users_counted_from_distinct_signed_in_accounts(db, cfg):
    now = T0
    rows = [(f"student:{i}", now - 60) for i in range(143)] + [("admin:1", now - 60), ("student:999", now - 3600)]
    db.executemany("INSERT INTO capacity_active_users (user_key, last_seen) VALUES (?, ?)", rows)
    db.commit()
    m = cm.aggregate(db, cfg, now)
    assert m["active_users"] == 144 and m["active_students"] == 143   # 1h-old one excluded (15 min window)


def test_high_request_rate_alert_when_configured(db, cfg, monkeypatch):
    monkeypatch.setenv("CAPACITY_REQUESTS_PER_MIN_WARN", "600")
    monkeypatch.setenv("CAPACITY_REQUESTS_PER_MINUTE_SUSTAIN", "1")
    cfg = cm.load_config()
    for i in range(10):
        _insert_sample(db, T0 - 300 + i * 30, requests=400)       # 400 / 30 s = 800/min
    m = cm.aggregate(db, cfg, T0)
    assert m["requests_per_minute"] >= 600
    out = Outbox()
    r = run(db, cfg, m, T0, out)
    assert r["levels"]["requests_per_minute"] == "warning" and len(out.sent) == 1


def test_high_concurrent_requests(db, cfg):
    _insert_sample(db, T0 - 10, worker="1", peak_in_flight=5)
    _insert_sample(db, T0 - 10, worker="2", peak_in_flight=4)
    m = cm.aggregate(db, cfg, T0)
    assert m["concurrent_requests"] == 9                           # summed across workers
    out = Outbox()
    for i in range(2):
        r = run(db, cfg, m, T0 + i * 30, out)
    assert r["levels"]["concurrent_requests"] == "critical" and "Application/server bottleneck" in out.sent[0][2]


def test_high_ocr_queue_from_real_hooks(db, cfg):
    cm._take_interval()
    for _ in range(7):
        cm.ocr_waiting_started()                                    # 7 uploads waiting for the reader
    cm.ocr_waiting_finished(True)
    cm.ocr_job_finished(6.5, True)
    cm.flush_sample(db, cfg, T0 - 5)
    for _ in range(6):
        cm.ocr_waiting_finished(False)                              # the rest could not get the reader
    m = cm.aggregate(db, cfg, T0)
    assert m["ocr_queue"] == 7 and m["ocr_jobs"] == 1 and m["ocr_avg_seconds"] == 6.5
    out = Outbox()
    for i in range(2):
        r = run(db, cfg, m, T0 + i * 30, out)
    assert r["levels"]["ocr_queue"] == "critical"
    assert "Visa reader (OCR) bottleneck" in out.sent[0][2]
    cm._take_interval()


def test_ocr_busy_failures_alert_immediately(db, cfg):
    out = Outbox()
    r = run(db, cfg, normal_metrics(ocr_unavailable=1), T0, out)
    assert r["levels"]["ocr_unavailable"] == "warning" and len(out.sent) == 1


def test_high_storage_from_disk_usage(db, cfg, monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(cm.shutil, "disk_usage", lambda p: Usage(1_000_000_000, 930_000_000, 70_000_000))
    storage = cm.read_storage()
    assert storage["percent"] == 93.0
    out = Outbox()
    r = run(db, cfg, normal_metrics(storage_percent=storage["percent"]), T0, out)   # sustain 1
    assert r["levels"]["storage_percent"] == "critical"
    body = out.sent[0][2]
    assert "Storage: 93%" in body and "does NOT fix traffic" in body


def test_high_memory_from_cgroup_v2(tmp_path, db, cfg):
    (tmp_path / "memory.current").write_text(str(480 * 1048576))
    (tmp_path / "memory.max").write_text(str(512 * 1048576))
    (tmp_path / "memory.stat").write_text("anon 1\ninactive_file %d\n" % (20 * 1048576))
    used, limit = cm.read_memory(str(tmp_path))
    pct = round(100 * used / limit, 1)
    assert pct == 89.8                                             # (480-20)/512, cache excluded
    out = Outbox()
    for i in range(2):
        r = run(db, cfg, normal_metrics(memory_percent=pct), T0 + i * 30, out)
    assert r["levels"]["memory_percent"] == "critical"


def test_memory_cgroup_v1_and_unlimited(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    (d / "memory.usage_in_bytes").write_text(str(300 * 1048576))
    (d / "memory.limit_in_bytes").write_text("9223372036854771712")   # unlimited
    (d / "memory.stat").write_text("total_inactive_file %d\n" % (100 * 1048576))
    used, limit = cm.read_memory(str(tmp_path))
    assert used == 200 * 1048576 and limit is None                     # percent will be "unavailable"


def test_memory_limit_fallback_from_env(monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "read_memory", lambda: (256 * 1048576, None))
    monkeypatch.setenv("CAPACITY_MEMORY_LIMIT_MB", "512")
    s = cm._HostSampler().sample(cm.load_config())
    assert s["memory_percent"] == 50.0 and s["memory_limit_mb"] == 512


def test_cpu_percent_is_relative_to_the_container_limit(tmp_path, monkeypatch):
    (tmp_path / "cpu.max").write_text("50000 100000")              # 0.5 CPU, like Render Starter
    (tmp_path / "cpu.stat").write_text("usage_usec 1000000\n")
    used, limit = cm.read_cpu_usage_and_limit(str(tmp_path))
    assert used == 1.0 and limit == 0.5
    readings = iter([(10.0, 0.5), (10.4, 0.5)])                    # 0.4 CPU-seconds in 1 s
    clock = iter([100.0, 101.0])
    monkeypatch.setattr(cm, "read_cpu_usage_and_limit", lambda: next(readings))
    monkeypatch.setattr(cm.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(cm, "read_memory", lambda: (None, None))
    monkeypatch.setattr(cm, "read_network_bytes", lambda: None)
    h = cm._HostSampler()
    assert h.sample(cm.load_config())["cpu_percent"] is None       # first reading: no rate yet
    assert h.sample(cm.load_config())["cpu_percent"] == 80.0       # 0.4 / 0.5


def test_unreadable_host_metrics_are_unavailable(monkeypatch):
    monkeypatch.setattr(cm, "CGROUP_ROOT", "/nonexistent")
    assert cm.read_cpu_usage_and_limit("/nonexistent") == (None, None)
    assert cm.read_memory("/nonexistent") == (None, None)
    assert cm.read_network_bytes("/nonexistent") is None


# ---------------------------------------------------------------------
# 5. aggregation, p95, error rate, single evaluator
# ---------------------------------------------------------------------
def _insert_sample(db, ts, worker="1", host="h1", **data):
    base = {"requests": 0, "errors_5xx": 0, "latency_sum_ms": 0, "latency_count": 0,
            "latency_hist": [0] * len(cm.LATENCY_BUCKETS_MS), "peak_in_flight": 0, "peak_ocr_waiting": 0,
            "ocr_running": 0, "ocr_jobs": 0, "ocr_unavailable": 0, "db_errors": 0, "db_select_ms": 1.0,
            "db_write_ms": 2.0}
    base.update(data)
    db.execute("INSERT INTO capacity_samples (host, worker, created_at, data) VALUES (?, ?, ?, ?)",
               (host, worker, ts, json.dumps(base)))
    db.commit()


def test_error_rate_and_p95_merge_across_workers(db, cfg):
    hist_fast = [0] * len(cm.LATENCY_BUCKETS_MS)
    hist_fast[2] = 90                                              # 90 requests <= 100 ms
    hist_slow = [0] * len(cm.LATENCY_BUCKETS_MS)
    hist_slow[9] = 10                                              # 10 requests <= 2000 ms
    _insert_sample(db, T0 - 20, worker="1", requests=90, errors_5xx=1, latency_count=90,
                   latency_sum_ms=4500, latency_hist=hist_fast)
    _insert_sample(db, T0 - 20, worker="2", requests=10, errors_5xx=3, latency_count=10,
                   latency_sum_ms=15000, latency_hist=hist_slow)
    m = cm.aggregate(db, cfg, T0)
    assert m["error_rate_percent"] == 4.0 and m["latency_p95_ms"] == 2000.0
    assert m["latency_avg_ms"] == 195.0 and m["workers_reporting"] == 2


def test_rates_not_judged_on_too_little_traffic(db, cfg):
    _insert_sample(db, T0 - 20, requests=3, errors_5xx=3, latency_count=3, latency_sum_ms=30)
    m = cm.aggregate(db, cfg, T0)
    assert m["error_rate_percent"] is None and m["rates_note"]      # 3 errors out of 3 isn't "100% outage"


def test_only_one_process_evaluates_per_interval(db, cfg):
    assert cm._claim_evaluation(db, cfg, T0) is True
    assert cm._claim_evaluation(db, cfg, T0 + 1) is False          # another worker, same interval
    assert cm._claim_evaluation(db, cfg, T0 + cfg["sample_seconds"]) is True


def test_full_cycle_writes_sample_snapshot_and_prunes(db, cfg):
    _insert_sample(db, T0 - 3 * 86400)                              # ancient sample
    out = Outbox()
    snap = cm.run_cycle(db, cfg, now=T0 - (T0 % 3600), send=out)    # top of the hour -> prune
    assert snap and snap["status"] in ("normal", "warning", "critical")
    assert db.execute("SELECT COUNT(*) FROM capacity_samples WHERE created_at < ?", (T0 - 86400,)).fetchone()[0] == 0
    stored = json.loads(db.execute("SELECT value FROM capacity_meta WHERE key='last_snapshot'").fetchone()[0])
    assert "metrics" in stored


# ---------------------------------------------------------------------
# 6. request hooks (per-request cost), exceptions, static files
# ---------------------------------------------------------------------
def test_request_hooks_count_requests_and_exclude_static(client):
    cm._take_interval()
    client.get("/")
    client.get("/static/css/style.css")
    client.get("/definitely-not-a-page")
    data, _ = cm._take_interval()
    assert data["requests"] == 2 and data["in_flight"] == 0 and data["latency_count"] == 2


def test_server_errors_and_exceptions_are_counted(client, student, monkeypatch):
    import app as app_module
    cm._take_interval()
    monkeypatch.setattr(app_module, "get_current_cycle", lambda: 1 / 0)
    app_module.app.config["PROPAGATE_EXCEPTIONS"] = False
    try:
        r = client.get("/application/start")
    finally:
        app_module.app.config["PROPAGATE_EXCEPTIONS"] = None
    data, _ = cm._take_interval()
    assert r.status_code == 500
    assert data["exceptions"] == 1 and data["errors_5xx"] == 1 and data["in_flight"] == 0


def test_signed_in_users_are_tracked_by_id_only(client, student):
    cm._take_interval()
    client.get("/dashboard")
    _, users = cm._take_interval()
    assert list(users) == [f"student:{_user_id(student)}"]


def test_per_request_overhead_is_tiny():
    n = 20000
    t = time.perf_counter()
    for _ in range(n):
        cm.request_finished(cm.request_started("/x", "student:1"), 200)
    per_request_us = (time.perf_counter() - t) / n * 1e6
    cm._take_interval()
    assert per_request_us < 50, per_request_us


# ---------------------------------------------------------------------
# 7. dashboard, test alert, security
# ---------------------------------------------------------------------
@pytest.fixture()
def admin(client):
    with client.session_transaction() as s:
        s["role"] = "admin"
        s["user_id"] = 1
    return client


def test_dashboard_shows_system_capacity(client, admin):
    html = client.get("/admin/dashboard").get_data(as_text=True)
    assert "System Capacity" in html and "Recently Active Users" in html
    assert "not</em> an exact count of simultaneous users" in html
    assert "SQLite detected — PostgreSQL recommended before large-scale concurrent usage." in html
    assert "Local file storage detected — object storage recommended before large-scale horizontal scaling." in html


def test_capacity_page_and_test_alert(client, admin, monkeypatch):
    import email_lib
    sent = []
    monkeypatch.setattr(email_lib, "send_email", lambda to, s, b: (sent.append((to, s)) or (True, None)))
    assert client.get("/admin/capacity").status_code == 200
    r = client.post("/admin/capacity/test-alert")
    assert r.status_code == 302 and sent and "[TEST]" in sent[0][1]
    assert sent[0][0] == "africascholarbridge@gmail.com"
    html = client.get("/admin/capacity").get_data(as_text=True)
    assert "Test alert e-mail sent." in html


def test_capacity_pages_require_main_admin(client, student):
    assert client.get("/admin/capacity").status_code == 302
    assert client.post("/admin/capacity/test-alert").status_code == 302


def test_no_secrets_in_emails_dashboard_or_events(client, admin, db, cfg, monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    cfg = cm.load_config()
    out = Outbox()
    for i in range(3):
        run(db, cfg, normal_metrics(cpu_percent=95, memory_percent=95, storage_percent=95), T0 + i * 30, out)
    for i in range(3, 6):
        run(db, cfg, normal_metrics(), T0 + i * 30, out)
    blobs = [s[1] + s[2] for s in out.sent]
    blobs.append(json.dumps([tuple(r) for r in db.execute("SELECT * FROM capacity_events").fetchall()]))
    blobs.append(json.dumps(cm.dashboard_snapshot(db), default=str))
    blobs.append(client.get("/admin/capacity").get_data(as_text=True))
    blobs.append(client.get("/admin/dashboard").get_data(as_text=True))
    for blob in blobs:
        for secret in SECRETS.values():
            if secret:
                assert secret not in blob


def _user_id(student):
    db = database.get_db()
    uid = db.execute("SELECT user_id FROM students WHERE id = ?", (student["student_id"],)).fetchone()[0]
    db.close()
    return uid


def test_dashboard_renders_real_cycle_data_and_status(client, admin, db, cfg, monkeypatch):
    """After real monitoring cycles (real counters, real disk, this
    container's cgroup), the dashboard shows the stored snapshot."""
    monkeypatch.setenv("CAPACITY_MEMORY_LIMIT_MB", "512")
    cfg = cm.load_config()
    for path in ("/", "/login", "/register"):
        client.get(path)
    out = Outbox()
    now = time.time()
    cm.run_cycle(db, cfg, now=now - 40, send=out)
    snap = cm.run_cycle(db, cfg, now=now, send=out)
    assert snap["metrics"]["requests"] >= 3 and snap["metrics"]["storage_percent"] is not None
    html = client.get("/admin/dashboard").get_data(as_text=True)
    assert "Last check" in html and "NO DATA YET" not in html
    assert any(b in html for b in ("🟢 NORMAL", "🟡 WARNING", "🔴 CRITICAL"))
    assert "Visa reader (OCR):" in html and "Database:" in html


def test_dashboard_explains_unreadable_limits_instead_of_inventing(db, cfg):
    snapshot = {"at": T0, "status": "normal", "levels": {},
                "metrics": {"memory_limit_mb": None, "memory_percent": None,
                            "cpu_limit_source": "machine cores (no container limit found)"}}
    db.execute("INSERT INTO capacity_meta (key, value) VALUES ('last_snapshot', ?)", (json.dumps(snapshot),))
    db.commit()
    snap = cm.dashboard_snapshot(db)
    assert any("CAPACITY_MEMORY_LIMIT_MB" in h for h in snap["setup_hints"])
    assert any("CAPACITY_CPU_LIMIT_CORES" in h for h in snap["setup_hints"])
    mem = next(r for r in snap["rows"] if r["key"] == "memory_percent")
    assert mem["value"] is None and mem["display"] == "unavailable"


def test_slow_visa_uploads_do_not_distort_site_response_time():
    cm._take_interval()
    tok = cm.request_started("/application/visa-document/upload", "student:1")
    time.sleep(0.05)
    cm.request_finished(tok, 302)
    cm.request_finished(cm.request_started("/", None), 200)
    data, _ = cm._take_interval()
    assert data["requests"] == 2 and data["latency_count"] == 1 and data["latency_sum_ms"] < 50
