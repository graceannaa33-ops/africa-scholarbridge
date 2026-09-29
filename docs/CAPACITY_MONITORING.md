# Capacity monitoring & early warnings

Africa ScholarBridge watches how close it is to its infrastructure limits and
e-mails the main admin (**africascholarbridge@gmail.com** by default) *before*
students are affected. Everything runs inside the existing web service: no new
Render service, no change to the start command (`gunicorn wsgi:app`).

> **E-mail must be configured for alerts to arrive.** Alerts are sent with the
> site's existing e-mail settings (`MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`,
> `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER`). If those aren't set on Render, alerts
> are still recorded on the dashboard, marked "not sent: e-mail not configured".

## How it works

* **Per request:** only in-memory counters (≈1.6 µs; no measurable difference in
  response time). Static files are excluded.
* **Every `CAPACITY_SAMPLE_SECONDS` (30 s):** a background thread in each
  gunicorn worker records one small sample (its counters + the container's
  CPU/memory/disk) in the database.
* **Exactly one process per interval** (claimed through the database) combines
  all samples, applies the thresholds, records events and sends e-mail. More
  workers or instances never produce duplicate alerts.
* **Fails safe:** every part catches its own errors. A monitoring problem shows
  "monitoring degraded" on the dashboard. The website keeps working.
* **Honest numbers:** anything that can't be measured from inside the app is
  shown as *unavailable*, never estimated.

### What is measured, and from where

| Metric | Source | Notes |
|---|---|---|
| CPU % | Container cgroup (v2 `cpu.stat`/`cpu.max`, or v1) | % of the **container's CPU limit** (0.5 CPU on Starter). If the limit can't be read, % of the machine's cores; set `CAPACITY_CPU_LIMIT_CORES`. |
| Memory % | Container cgroup working set ÷ limit | Includes the visa reader process. Excludes reclaimable file cache. If the limit can't be read, set `CAPACITY_MEMORY_LIMIT_MB`; otherwise shown as unavailable. |
| Storage % | Disk holding the database and `UPLOAD_ROOT` (`/var/data`) | Plus the count and size of stored uploads (checked every 15 min). |
| HTTP error rate | 5xx responses ÷ all requests in the window | Only judged once there are ≥ 20 requests in the window. |
| Response time | Merged latency histogram | Average (exact) and p95 (upper bound of its bucket). |
| Concurrent requests | In-flight requests; peak per interval, summed over workers | Current capacity is 1 worker × 8 threads = 8. |
| Recently Active Users | Distinct signed-in accounts (students, admins, visa admins) seen in the last 15 min | **Not** simultaneous users. Signed-out visitors and people reading without clicking can't be counted reliably. |
| Database | `SELECT 1` + sample-write time; database errors | SQLite has no connection pool, so "connections" isn't measurable. |
| Visa reader (OCR) | Queue (waiting), running, processing time, busy/timeout failures | A busy/timeout failure means a student was moved to visa assistance because the reader was unavailable. |
| Network | Container `/proc/net/dev` if readable | Informational only; Render → Metrics is authoritative. |

**Not measurable from inside Flask:** exact simultaneous users, Render's load-balancer
queue, Render's own bandwidth and instance metrics. Use Render → Metrics for those.

## A. Files changed

| File | Change |
|---|---|
| `capacity_monitor.py` | **New.** Collection, thresholds, alert rules, e-mails, dashboard data. |
| `database.py` | 5 new tables: `capacity_samples`, `capacity_active_users`, `capacity_alert_state`, `capacity_events`, `capacity_meta` (created automatically on start). |
| `app.py` | Registers the monitor; adds the dashboard section; new routes `/admin/capacity` and `POST /admin/capacity/test-alert` (main admin only). |
| `visa_verification.py` | Three counter calls around the visa reader (queue, running, time). Verification results are unchanged. |
| `templates/admin/_capacity.html` | **New.** System Capacity section. |
| `templates/admin/capacity.html` | **New.** Detail page: event history and test-alert button. |
| `templates/admin/dashboard.html`, `templates/admin/_nav.html` | Include the section; add the menu link. |
| `tests/test_capacity_monitor.py` | **New.** 43 tests. |
| `docs/CAPACITY_MONITORING.md`, `.env.example` | Documentation. |

Not changed: funding applications, the annual application workflow, student
login, Visa Admin, visa verification results, M-PESA payment and verification,
`email_lib.py`, `render.yaml`, `gunicorn.conf.py`, `wsgi.py`.

## B. Environment variables (all optional)

| Variable | Default | Meaning |
|---|---|---|
| `CAPACITY_MONITOR_ENABLED` | `1` | `0` turns off sampling and alerts. The request counters stay, at negligible cost. |
| `CAPACITY_ALERT_EMAIL` | `ADMIN_EMAIL`, else `africascholarbridge@gmail.com` | Where alerts go. |
| `CAPACITY_SAMPLE_SECONDS` | `30` | How often samples are taken and evaluated. |
| `CAPACITY_WINDOW_SECONDS` | `300` | Window for error rate, response time and requests/min. |
| `CAPACITY_ACTIVE_USER_MINUTES` | `15` | "Recently Active Users" window. |
| `CAPACITY_ALERT_COOLDOWN_MINUTES` | `30` | Don't repeat the same alert within this time. |
| `CAPACITY_EMAIL_RETRY_MINUTES` | `5` | Retry delay after a failed e-mail. |
| `CAPACITY_RECOVERY_SAMPLES` | `2` | Normal readings needed before "recovered". |
| `CAPACITY_SEND_RECOVERY_EMAIL` | `1` | E-mail when an alerted metric recovers. |
| `CAPACITY_SEND_REMINDERS` | `1` | Re-send a still-active alert once per cooldown. |
| `CAPACITY_MIN_REQUESTS_FOR_RATES` | `20` | Minimum requests before judging error rate and p95. |
| `CAPACITY_CPU_LIMIT_CORES` | *(read from container)* | Fallback CPU limit, e.g. `0.5`. |
| `CAPACITY_MEMORY_LIMIT_MB` | *(read from container)* | Fallback memory limit, e.g. `512`. |
| `CAPACITY_SAMPLE_RETENTION_HOURS` | `24` | How long raw samples are kept. |
| `CAPACITY_EVENT_RETENTION_DAYS` | `90` | How long events are kept. |
| Threshold variables | see section C | |
| `CAPACITY_<METRIC>_SUSTAIN` | see section C | Consecutive readings before alerting, e.g. `CAPACITY_CPU_PERCENT_SUSTAIN=3`. |

## C. Thresholds (defaults)

A threshold of `0` or empty disables that level.

| Metric | Warning | Critical | Must hold for | Variables |
|---|---|---|---|---|
| CPU | ≥ 70 % | ≥ 85 % | 3 readings (≈ 1.5 min) | `CAPACITY_CPU_WARN`, `CAPACITY_CPU_CRIT` |
| Memory (RAM) | ≥ 70 % | ≥ 85 % | 2 readings | `CAPACITY_MEMORY_WARN`, `CAPACITY_MEMORY_CRIT` |
| Storage | ≥ 75 % | ≥ 90 % | 1 reading | `CAPACITY_STORAGE_WARN`, `CAPACITY_STORAGE_CRIT` |
| HTTP error rate (5xx) | ≥ 2 % | ≥ 5 % | 2 readings | `CAPACITY_ERROR_RATE_WARN`, `CAPACITY_ERROR_RATE_CRIT` |
| Response time p95 | ≥ 1500 ms | ≥ 4000 ms | 3 readings (consistently high) | `CAPACITY_LATENCY_P95_WARN_MS`, `CAPACITY_LATENCY_P95_CRIT_MS` |
| Concurrent requests | ≥ 6 | ≥ 8 | 2 readings | `CAPACITY_CONCURRENT_WARN`, `CAPACITY_CONCURRENT_CRIT` |
| Visa reader queue | ≥ 3 waiting | ≥ 6 waiting | 2 readings | `CAPACITY_OCR_QUEUE_WARN`, `CAPACITY_OCR_QUEUE_CRIT` |
| Visa checks failed (reader busy/timeout) | ≥ 1 in window | ≥ 3 in window | 1 reading | `CAPACITY_OCR_UNAVAILABLE_WARN`, `CAPACITY_OCR_UNAVAILABLE_CRIT` |
| Database response time | ≥ 250 ms | ≥ 1000 ms | 2 readings | `CAPACITY_DB_LATENCY_WARN_MS`, `CAPACITY_DB_LATENCY_CRIT_MS` |
| Database errors | ≥ 1 in window | ≥ 5 in window | 1 reading | `CAPACITY_DB_ERRORS_WARN`, `CAPACITY_DB_ERRORS_CRIT` |
| Recently Active Users | off | off | 1 reading | `CAPACITY_ACTIVE_USERS_WARN`, `CAPACITY_ACTIVE_USERS_CRIT` |
| Requests per minute | off | off | 2 readings | `CAPACITY_REQUESTS_PER_MIN_WARN`, `CAPACITY_REQUESTS_PER_MIN_CRIT` |

**Concurrent requests** should track your gunicorn capacity (`workers × threads`,
currently 1 × 8). If you change `gunicorn.conf.py`, set the warning to about 75 %
of the new capacity and the critical level to 100 %.

**Active users and request rate are off by default.** They aren't limits in
themselves; CPU, memory and response time show when they become a problem. Turn
them on if you want a traffic notification.

## D. How alerts work

1. **Levels.** Every 30 s each metric is classed normal, warning or critical.
2. **Sustain.** A higher level must hold for the "must hold for" number of
   readings, so a single spike (for example one visa photo being read) doesn't
   alert. Going back down needs `CAPACITY_RECOVERY_SAMPLES` normal readings.
3. **First warning:** an e-mail is sent. Everything elevated in the same check
   goes in **one** e-mail.
4. **Cooldown:** the same (or a lower) alert for a metric isn't repeated within
   30 min. This includes flapping warning → normal → warning.
5. **Becoming critical:** a CRITICAL e-mail is sent even if a warning was just
   sent. The subject is `[CRITICAL] … immediate action may be required`.
6. **Still elevated after the cooldown:** one reminder per cooldown period
   (`CAPACITY_SEND_REMINDERS`).
7. **Recovery:** recorded as an event. If an alert e-mail had been sent, a
   `[RECOVERED]` e-mail follows. If other metrics are still elevated, it's
   `[PARTLY RECOVERED]` instead.
8. **E-mail failure:** recorded on the event as "not sent", then retried after
   5 minutes, not every 30 s.
9. **Missing metric:** no alert and no change of state. It's shown as
   *unavailable*.

Events are written only when a level **changes** (plus e-mailed reminders).
There are never events every second. Each event records the time (UTC), level,
metric, measured value, threshold, message, and whether the e-mail was sent.

Each e-mail lists what triggered it, the current readings (CPU, memory, storage,
recently active users, concurrent requests, error rate, p95, visa reader queue)
and the recommended action **for that kind of bottleneck**. It never contains
passwords, keys, SMTP settings, M-PESA credentials or student documents; only
numbers and fixed text are used.

## E. How to change thresholds

Render dashboard → your service → **Environment** → add or edit, for example
`CAPACITY_CPU_WARN=65`, then **Save**. Render redeploys and the new values apply
at once. Nothing needs changing in the code. The current values are shown next to
each metric on **Admin → System Capacity**.

## F. How to test an alert manually

* **E-mail delivery:** Admin → **System Capacity** → **Send test alert e-mail**.
  A `[TEST]` e-mail arrives if e-mail is configured; the result is shown on the
  page and in the event history. From a shell: `python capacity_monitor.py --test-alert`.
* **The full alert path:** temporarily set `CAPACITY_STORAGE_WARN=1` on Render.
  Storage is above 1 %, so within about a minute you get a real `[WARNING]` e-mail
  and a warning event. Remove the variable afterwards; after 2 normal readings you
  get `[RECOVERED]`.

## G. What to change on Render as traffic grows

| Stage | What to do |
|---|---|
| **Today** (1 × Starter 0.5 CPU / 512 MB, SQLite + uploads on a 1 GB disk) | Only vertical scaling is possible. Render won't run more than one instance of a service with a disk attached, and each deploy briefly stops the site. |
| **~200 simultaneous users** | Move to a larger instance type (more CPU and RAM). With more RAM, raise gunicorn workers (each ≈ 70 MB) and the `CAPACITY_CONCURRENT_*` thresholds. Grow the disk if storage warns. |
| **~1,000** | **PostgreSQL** (Render Postgres) instead of SQLite. **Object storage** (e.g. S3-compatible) for uploads, so the disk can be removed. **A separate background worker service** for the visa reader (OCR) with a job queue (e.g. Render Key Value / Redis). Once the disk is gone, run 2+ instances. |
| **~10,000** | Several web instances behind Render's load balancer (autoscaling on CPU/memory), a larger Postgres plan with connection pooling, a CDN for static files, and more OCR workers scaled on queue length. |
| **~50,000–100,000** | Multiple autoscaled web instances, a high-availability Postgres with read replicas, several queue-driven worker pools (OCR, e-mail, matching), a CDN, and a dedicated monitoring service (Render metrics, an APM, log alerts). Load-test before each stage. |

The monitor is built for this path: every instance and worker reports separately,
the dashboard sums them, and only one process sends e-mail. When the database
moves to PostgreSQL, the monitor's handful of SQLite-specific statements (like
the rest of the app's SQL) must be ported. When uploads move to object storage,
the storage metric should switch to the database disk only.

## H. Vertical vs horizontal scaling

| Resource | Vertical (bigger instance) | Horizontal (more instances/workers) |
|---|---|---|
| CPU | ✅ Immediate fix today | ✅ After PostgreSQL + object storage |
| RAM | ✅ Immediate fix today | Spreads load, but each instance needs enough RAM for the visa reader |
| Disk / storage | Bigger disk for **space** only | ❌ Local disks don't scale out; use object storage + PostgreSQL |
| Database (SQLite) | Helps a little | ❌ SQLite is one file with one writer; use PostgreSQL |
| Visa reader (OCR) | More CPU = faster reads | ✅ Separate OCR worker service(s) with a queue |
| Application threads | More RAM allows more workers | ✅ More instances |
| Network | Render handles bandwidth | CDN / object storage for files |

## I. What needs PostgreSQL, object storage or background workers

* **PostgreSQL:** all application data (currently SQLite in `/var/data`), and the
  monitor's own tables. SQLite allows one writer at a time and is tied to one disk
  on one instance.
* **Object storage:** visa documents (`uploads/visa_documents`) and M-PESA
  screenshots (`uploads/payment_proofs`). Once they're off the local disk, the disk
  can be removed and the service can run on several instances with zero-downtime
  deploys.
* **Background workers + a queue:** the visa reader (≈ 300 MB and several seconds
  per document; today it runs inside the web service, one at a time). Also worth
  moving later: outgoing e-mail and funding matching.

**Increasing storage alone does not fix high traffic.** Storage only adds space.
Traffic problems show up as CPU, memory, response time, concurrent requests,
database or visa reader alerts, and each needs the fix in the table below.

## When to upgrade

| Warning type | What it means | Recommended action |
|---|---|---|
| **CPU** | The instance is computing flat-out | Larger instance type (more CPU). Long term: more instances after PostgreSQL + object storage. |
| **Memory (RAM)** | Close to the RAM limit; risk of Render restarting the service | Larger instance type (more RAM). Move the visa reader to its own worker service. |
| **Storage** | The disk is filling up | Increase the Render disk; plan object storage for uploads. *Does not fix traffic.* |
| **HTTP error rate** | Requests are failing | Check Render logs first: often a bug or dependency, not capacity. If errors come with high CPU/RAM, scale up. |
| **Response time (p95)** | Pages are consistently slow | Look at which other metric is high (CPU, database, concurrent requests). Scale that resource. |
| **Concurrent requests** | All gunicorn threads are busy; new requests wait | More threads/workers (needs RAM) or more instances. |
| **Visa reader queue / busy failures** | Uploads are waiting, or students are failed because the reader was busy | More CPU now; a separate OCR worker service with a queue next. |
| **Database response time / errors** | SQLite is contended or failing | Migrate to PostgreSQL. Check free disk space. |
| **Recently Active Users / request rate** (if enabled) | Traffic milestone | Review the other metrics; plan the next stage in section G. |
| **Network** | Not alerted on (not reliably measurable in-app) | Check Render → Metrics bandwidth; use a CDN or object storage for files. |
