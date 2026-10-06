"""Admin system separated from the public student website.

* No admin login/links anywhere on public or student pages.
* Every /admin/* and /visa-admin/* page still requires its own login
  (checked against Flask's full route table, not a hand-picked list).
* Main Admin and Visa Admin stay separate.
* Optional ADMIN_HOST: admin answers only on the admin subdomain and is a
  404 on the public website; student pages on the admin host go to the public
  host. Unset = behaviour unchanged.

All test data is fictional.
"""
import re
import uuid

import pytest
from werkzeug.security import generate_password_hash

import app as app_module
from database import get_db

ADMIN_MARKERS = ('href="/admin', 'href="/visa-admin', "Admin Login", "Admin login", "admin/login",
                 "visa-admin/login")
PASSWORD = "Adm1n-Test-Pass!"


def _rules(prefixes):
    for rule in app_module.app.url_map.iter_rules():
        if rule.rule.startswith(prefixes):
            yield rule


def _url(rule):
    return re.sub(r"<(?:int:)?(\w+)>", "1", rule.rule)


def _make_user(role):
    email = f"{role}-{uuid.uuid4().hex[:8]}@example.org"
    db = get_db()
    uid = db.execute("INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)",
                     (email, generate_password_hash(PASSWORD), role)).lastrowid
    if role == "admin":
        db.execute("INSERT INTO admins (user_id, full_name) VALUES (?, 'Example Admin')", (uid,))
    elif role == "visa_admin":
        db.execute("INSERT INTO visa_admins (user_id, full_name) VALUES (?, 'Example Visa Admin')", (uid,))
    db.commit()
    db.close()
    return email


@pytest.fixture()
def no_admin_host(monkeypatch):
    monkeypatch.setattr(app_module, "ADMIN_HOST", None)
    monkeypatch.setattr(app_module, "PUBLIC_HOST", None)


@pytest.fixture()
def admin_host(monkeypatch):
    monkeypatch.setattr(app_module, "ADMIN_HOST", "admin.example.test")
    monkeypatch.setattr(app_module, "PUBLIC_HOST", "www.example.test")


PUBLIC = "https://www.example.test"
ADMIN = "https://admin.example.test"


# ---------------------------------------------------------------------
# 1. No admin login or links on public / student pages
# ---------------------------------------------------------------------
def _public_get_pages():
    skip = ("/admin", "/visa-admin", "/api", "/static", "/logout", "/paystack", "/application/submit")
    for rule in app_module.app.url_map.iter_rules():
        if "GET" in rule.methods and not rule.arguments and not rule.rule.startswith(skip):
            yield rule.rule


def test_public_pages_contain_no_admin_login_or_links(client, no_admin_host):
    checked = 0
    for path in _public_get_pages():
        r = client.get(path)
        if r.status_code != 200:
            continue
        body = r.get_data(as_text=True)
        for marker in ADMIN_MARKERS:
            assert marker not in body, (path, marker)
        checked += 1
    assert checked >= 8                                     # home, login, register, about, contact, ...


def test_student_pages_contain_no_admin_login_or_links(client, student, no_admin_host):
    checked = 0
    for path in list(_public_get_pages()) + ["/application/step/review", "/application/step/visa"]:
        r = client.get(path)
        if r.status_code != 200:
            continue
        body = r.get_data(as_text=True)
        for marker in ADMIN_MARKERS:
            assert marker not in body, (path, marker)
        checked += 1
    assert checked >= 10                                    # dashboard, applications, documents, ...


def test_home_footer_and_login_page_have_no_admin_link(client, no_admin_host):
    for path in ("/", "/login"):
        body = client.get(path).get_data(as_text=True)
        assert "Admin Login" not in body and "Admin login" not in body, path
        assert "/admin/login" not in body, path


# ---------------------------------------------------------------------
# 2. Every admin / visa-admin route still requires authentication
# ---------------------------------------------------------------------
PUBLIC_ENTRY = {"/admin/login", "/admin/logout", "/visa-admin", "/visa-admin/login", "/visa-admin/logout"}


@pytest.mark.parametrize("prefix,login", [("/admin", "/admin/login"), ("/visa-admin", "/visa-admin/login")])
def test_every_admin_route_requires_login(client, no_admin_host, prefix, login):
    checked = 0
    for rule in _rules((prefix + "/", prefix)):
        if rule.rule in PUBLIC_ENTRY:
            continue
        if prefix == "/admin" and rule.rule.startswith("/admin-"):
            continue
        for method in sorted(rule.methods & {"GET", "POST"}):
            r = client.open(_url(rule), method=method)
            assert r.status_code == 302, (method, rule.rule, r.status_code)
            assert r.headers["Location"].split("?")[0].endswith(login), (method, rule.rule, r.headers["Location"])
            assert "student" not in r.get_data(as_text=True).lower() or r.status_code == 302
            checked += 1
    assert checked >= 20


def test_entry_routes_reveal_nothing_without_login(client, no_admin_host):
    assert client.get("/admin/login").status_code == 200
    assert client.get("/visa-admin/login").status_code == 200
    r = client.get("/visa-admin")
    assert r.status_code == 302 and r.headers["Location"].endswith("/visa-admin/login")


def test_student_session_cannot_open_admin_or_visa_admin(client, student, no_admin_host):
    for path, login in (("/admin/dashboard", "/admin/login"), ("/admin/applications", "/admin/login"),
                        ("/visa-admin/dashboard", "/visa-admin/login"), ("/visa-admin/payments", "/visa-admin/login")):
        r = client.get(path)
        assert r.status_code == 302 and r.headers["Location"].split("?")[0].endswith(login), path


def test_main_admin_and_visa_admin_stay_separate(client, no_admin_host):
    admin_email = _make_user("admin")
    r = client.post("/admin/login", data={"email": admin_email, "password": PASSWORD})
    assert r.headers["Location"].endswith("/admin/dashboard")
    assert client.get("/admin/dashboard").status_code == 200
    r = client.get("/visa-admin/payments")                  # main admin login is not a visa admin login
    assert r.status_code == 302 and "/visa-admin/login" in r.headers["Location"]
    client.get("/admin/logout")

    visa_email = _make_user("visa_admin")
    r = client.post("/visa-admin/login", data={"email": visa_email, "password": PASSWORD})
    assert r.status_code == 302
    assert client.get("/visa-admin/dashboard").status_code == 200
    r = client.get("/admin/dashboard")                       # visa admin login is not a main admin login
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/login")


def test_wrong_admin_password_is_refused(client, no_admin_host):
    email = _make_user("admin")
    client.post("/admin/login", data={"email": email, "password": "wrong"})
    r = client.get("/admin/dashboard")
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/login")


def test_admin_pages_are_marked_noindex(client, no_admin_host):
    for path in ("/admin/login", "/visa-admin/login", "/admin/dashboard"):
        assert client.get(path).headers.get("X-Robots-Tag") == "noindex, nofollow", path
    assert "X-Robots-Tag" not in client.get("/").headers


def test_no_credentials_or_secrets_in_templates_or_static_files():
    import pathlib
    root = pathlib.Path(app_module.__file__).parent
    secret_patterns = (r"pbkdf2:sha256", r"scrypt:\d", r"password\s*[:=]\s*['\"][^'\"]+['\"]",
                       r"secret_key\s*[:=]", r"sk_(?:live|test)_[0-9a-z]+")
    for path in list(root.glob("templates/**/*.html")) + list(root.glob("static/js/*.js")):
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for pattern in secret_patterns:
            assert not re.search(pattern, text), (path.name, pattern)


# ---------------------------------------------------------------------
# 3. Optional admin subdomain (ADMIN_HOST)
# ---------------------------------------------------------------------
def test_without_admin_host_the_existing_paths_keep_working(client, no_admin_host):
    assert client.get("/admin/login", base_url=PUBLIC).status_code == 200
    assert client.get("/visa-admin/login", base_url=PUBLIC).status_code == 200


def test_public_host_hides_admin_completely(client, admin_host):
    for path in ("/admin", "/admin/login", "/admin/dashboard", "/admin/applications/1",
                 "/visa-admin", "/visa-admin/login", "/visa-admin/payments", "/visa-admin/payment-proof/1/file"):
        r = client.get(path, base_url=PUBLIC)
        assert r.status_code == 404, path
    for path in ("/admin/login", "/visa-admin/login"):
        assert client.post(path, data={"email": "x", "password": "y"}, base_url=PUBLIC).status_code == 404
    # The default Render address is treated as public too.
    assert client.get("/admin/login", base_url="https://africa-scholarbridge.onrender.com").status_code == 404
    # Student/public pages are unaffected.
    for path in ("/", "/login", "/register"):
        assert client.get(path, base_url=PUBLIC).status_code == 200, path


def test_admin_host_serves_admin_and_still_requires_login(client, admin_host):
    assert client.get("/admin/login", base_url=ADMIN).status_code == 200
    assert client.get("/visa-admin/login", base_url=ADMIN).status_code == 200
    r = client.get("/", base_url=ADMIN)
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/dashboard")
    r = client.get("/admin/dashboard", base_url=ADMIN)
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/login")
    r = client.get("/visa-admin/payments", base_url=ADMIN)
    assert r.status_code == 302 and "/visa-admin/login" in r.headers["Location"]
    assert client.get("/static/css/style.css", base_url=ADMIN).status_code == 200


def test_admin_host_login_works_end_to_end(client, admin_host):
    email = _make_user("admin")
    r = client.post("/admin/login", data={"email": email, "password": PASSWORD}, base_url=ADMIN)
    assert r.headers["Location"].endswith("/admin/dashboard")
    assert client.get("/admin/dashboard", base_url=ADMIN).status_code == 200
    cookie = r.headers.get("Set-Cookie", "")
    assert "session=" in cookie and "domain=" not in cookie.lower()   # host-only: never sent to the public site


def test_admin_host_sends_student_pages_to_the_public_site(client, admin_host):
    for path in ("/login", "/register", "/dashboard", "/application/step/personal"):
        r = client.get(path + "?x=1", base_url=ADMIN)
        assert r.status_code == 302, path
        assert r.headers["Location"] == f"https://www.example.test{path}?x=1", path


def test_admin_host_without_public_host_returns_404_for_student_pages(client, monkeypatch):
    monkeypatch.setattr(app_module, "ADMIN_HOST", "admin.example.test")
    monkeypatch.setattr(app_module, "PUBLIC_HOST", None)
    assert client.get("/login", base_url=ADMIN).status_code == 404
    assert client.get("/admin/login", base_url=ADMIN).status_code == 200


def test_session_cookie_is_host_only():
    assert not app_module.app.config.get("SESSION_COOKIE_DOMAIN")
    assert not app_module.app.config.get("SERVER_NAME")
