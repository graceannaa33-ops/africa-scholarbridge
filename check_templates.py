"""
check_templates.py
------------------
Pre-deploy check for Africa ScholarBridge (safe to run anywhere, including
the Render Shell). It never touches your real database: it points the app at
a throw-away temporary database before importing it.

    python check_templates.py

Checks:
  1. every render_template("...") in every .py file -> file exists in templates/
  2. every {% extends %} / {% include %} / {% import %} target exists
  3. every template parses (no Jinja syntax errors)
  4. every url_for('endpoint') in templates and Python -> a real Flask endpoint
  5. every url_for('static', filename=...) -> file exists in static/
  6. every <form action="{{ url_for(...) }}" method="..."> -> route accepts that method
  7. every .py file compiles (no Python syntax errors)

Exit code 0 = all good, 1 = problems found (listed).
"""

import ast
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(ROOT, "templates")
STATIC = os.path.join(ROOT, "static")

# Import the app against a temporary database / upload folder.
_tmp = tempfile.mkdtemp(prefix="asb-check-")
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "check.db")
os.environ["UPLOAD_ROOT"] = os.path.join(_tmp, "uploads")
os.environ.setdefault("SECRET_KEY", "template-check-only")
sys.path.insert(0, ROOT)

problems = []
report = []


def py_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in ("venv", ".venv", "__pycache__", ".git", "node_modules")]
        for f in filenames:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def template_files():
    for dirpath, _, filenames in os.walk(TEMPLATES):
        for f in filenames:
            if f.endswith(".html"):
                yield os.path.relpath(os.path.join(dirpath, f), TEMPLATES).replace(os.sep, "/")


# 7. Python syntax ------------------------------------------------------------
sources = {}
for path in py_files():
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    try:
        sources[path] = ast.parse(src, filename=path)
    except SyntaxError as exc:
        problems.append(f"Python syntax error: {os.path.relpath(path, ROOT)} line {exc.lineno}: {exc.msg}")

# 1. render_template references ----------------------------------------------
referenced = {}  # template -> [locations]
for path, tree in sources.items():
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "render_template" and node.args:
            arg = node.args[0]
            where = f"{os.path.relpath(path, ROOT)}:{node.lineno}"
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                referenced.setdefault(arg.value, []).append(where)
            elif isinstance(arg, ast.JoinedStr):
                # f"errors/{code}.html" -> check every errors/*.html handler code
                pattern = "".join(v.value if isinstance(v, ast.Constant) else "*" for v in arg.values)
                referenced.setdefault(pattern, []).append(where + " (dynamic)")
            else:
                report.append(f"  note: non-literal render_template at {where} (checked at runtime only)")

existing = set(template_files())
report.append("RENDER_TEMPLATE CHECKLIST")
for name in sorted(referenced):
    if "*" in name:
        regex = re.compile("^" + re.escape(name).replace(r"\*", "[^/]+") + "$")
        matches = sorted(t for t in existing if regex.match(t))
        ok = bool(matches)
        report.append(f"  [{'OK' if ok else 'MISSING'}] {name}  -> {', '.join(matches) or 'no match'}")
    else:
        ok = name in existing
        report.append(f"  [{'OK' if ok else 'MISSING'}] {name}")
    if not ok:
        problems.append(f"Missing template {name} (used at {', '.join(referenced[name])})")
for code in ("404", "500"):
    if f"errors/{code}.html" not in existing:
        problems.append(f"Missing templates/errors/{code}.html")

# 2 + 3. Jinja parse + extends/include targets ---------------------------------
from jinja2 import Environment, FileSystemLoader, TemplateSyntaxError, meta  # noqa: E402

env = Environment(loader=FileSystemLoader(TEMPLATES))
template_sources = {}
for name in sorted(existing):
    src = env.loader.get_source(env, name)[0]
    template_sources[name] = src
    try:
        ast_ = env.parse(src)
    except TemplateSyntaxError as exc:
        problems.append(f"Jinja syntax error: templates/{name} line {exc.lineno}: {exc.message}")
        continue
    for ref in meta.find_referenced_templates(ast_):
        if ref and ref not in existing:
            problems.append(f"templates/{name} extends/includes missing template {ref}")

# 4 + 5 + 6. url_for / static / form methods --------------------------------------
from app import app  # noqa: E402  (imported against the temporary database)

endpoints = {rule.endpoint: rule for rule in app.url_map.iter_rules()}
methods = {}
for rule in app.url_map.iter_rules():
    methods.setdefault(rule.endpoint, set()).update(rule.methods)

URL_FOR = re.compile(r"url_for\(\s*['\"]([\w.]+)['\"]([^)]*)\)")
FORM = re.compile(r"<form\b([^>]*)>", re.IGNORECASE | re.DOTALL)
bad_endpoints, bad_static, bad_forms, used = [], [], [], set()

for name, src in template_sources.items():
    for ep, rest in URL_FOR.findall(src):
        used.add(ep)
        if ep not in endpoints:
            bad_endpoints.append(f"templates/{name}: url_for('{ep}')")
        elif ep == "static":
            m = re.search(r"filename\s*=\s*['\"]([^'\"]+)['\"]", rest)
            if m and not os.path.isfile(os.path.join(STATIC, m.group(1))):
                bad_static.append(f"templates/{name}: static/{m.group(1)}")
    for attrs in FORM.findall(src):
        a = re.search(r"action=\"\{\{\s*url_for\(\s*['\"]([\w.]+)['\"]", attrs)
        m = re.search(r"method=\"(\w+)\"", attrs, re.IGNORECASE)
        method = (m.group(1) if m else "GET").upper()
        if a and a.group(1) in methods and method not in methods[a.group(1)]:
            bad_forms.append(f"templates/{name}: form {method} -> {a.group(1)} (route allows {sorted(methods[a.group(1)] - {'HEAD', 'OPTIONS'})})")

for path, tree in sources.items():
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "url_for" and node.args \
                and isinstance(node.args[0], ast.Constant):
            ep = node.args[0].value
            used.add(ep)
            if ep not in endpoints:
                bad_endpoints.append(f"{os.path.relpath(path, ROOT)}:{node.lineno}: url_for('{ep}')")

for label, items in (("Broken url_for endpoint", bad_endpoints), ("Missing static file", bad_static),
                     ("Form method not accepted by route", bad_forms)):
    problems.extend(f"{label}: {i}" for i in items)

report.append("")
report.append(f"Templates on disk: {len(existing)} | referenced by render_template: {len(referenced)} | "
              f"Flask endpoints: {len(endpoints)} | url_for endpoints used: {len(used)}")

print("\n".join(report))
print()
if problems:
    print(f"PROBLEMS FOUND ({len(problems)}):")
    for p in problems:
        print("  - " + p)
    sys.exit(1)
print("ALL CHECKS PASSED: every template, url_for endpoint, static file and form method is valid.")
