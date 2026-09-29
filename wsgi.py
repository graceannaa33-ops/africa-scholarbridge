"""
wsgi.py - production entry point.

Render / any WSGI server:
    gunicorn wsgi:app

Importing app.py creates/upgrades the database (DATABASE_PATH) and the
upload folders (UPLOAD_ROOT) automatically; no separate setup step is
needed for the schema. Create the administrator once with:
    python create_admin.py
"""
from app import app  # noqa: F401

if __name__ == "__main__":
    app.run()
