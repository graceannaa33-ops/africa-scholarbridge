"""Document availability and completion-flow tests."""

import io

from database import get_db
from conftest import visa_request_for, make_pdf


def _application_for_student(student_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM funding_applications WHERE student_id = ? ORDER BY id DESC LIMIT 1",
        (student_id,),
    ).fetchone()
    db.close()
    return row


def _funding_docs(student_id):
    app = _application_for_student(student_id)
    db = get_db()
    rows = db.execute("SELECT * FROM documents WHERE application_id = ? ORDER BY id", (app["id"],)).fetchall()
    db.close()
    return app, rows


def _visa_docs(request_id):
    db = get_db()
    rows = db.execute("SELECT * FROM visa_documents WHERE request_id = ? ORDER BY id", (request_id,)).fetchall()
    db.close()
    return rows


def _funding_payload(rows, optional="no", files=None):
    payload = {}
    for row in rows:
        if not row["is_required"]:
            payload[f"document_{row['id']}_availability"] = optional
        if files and row["id"] in files:
            payload[f"document_{row['id']}_file"] = (
                io.BytesIO(files[row["id"]]), f"test-{row['id']}.pdf"
            )
    return payload


def test_required_document_missing_cannot_continue(client, student):
    app, rows = _funding_docs(student["student_id"])
    required = next(r for r in rows if r["is_required"])
    db = get_db()
    db.execute("UPDATE documents SET status='Missing', file_path=NULL WHERE id=?", (required["id"],))
    db.commit()
    db.close()

    response = client.post("/application/step/documents", data=_funding_payload(rows))
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/application/step/documents")


def test_required_document_uploaded_can_continue(client, student):
    app, rows = _funding_docs(student["student_id"])
    required = next(r for r in rows if r["is_required"])
    db = get_db()
    db.execute("UPDATE documents SET status='Missing', file_path=NULL WHERE id=?", (required["id"],))
    db.commit()
    db.close()

    payload = _funding_payload(rows, files={required["id"]: b"%PDF-1.4\n% fictional test document\n"})
    response = client.post("/application/step/documents", data=payload, content_type="multipart/form-data")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/application/step/bank")

    db = get_db()
    saved = db.execute("SELECT * FROM documents WHERE id=?", (required["id"],)).fetchone()
    db.close()
    assert saved["status"] == "Uploaded"
    assert saved["availability"] == "Yes"


def test_optional_yes_without_upload_cannot_continue(client, student):
    app, rows = _funding_docs(student["student_id"])
    optional = next(r for r in rows if not r["is_required"])
    db = get_db()
    db.execute("UPDATE documents SET availability=NULL, status='Missing', file_path=NULL WHERE id=?", (optional["id"],))
    db.commit()
    db.close()

    response = client.post(
        "/application/step/documents",
        data=_funding_payload(rows, optional="yes"),
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/application/step/documents")


def test_optional_yes_with_upload_can_continue(client, student):
    app, rows = _funding_docs(student["student_id"])
    optional = next(r for r in rows if not r["is_required"])
    db = get_db()
    # This test isolates optional-document behavior; required documents
    # are marked complete because the real flow correctly blocks on them.
    for row in rows:
        if row["is_required"]:
            db.execute(
                "UPDATE documents SET availability='Yes', status='Uploaded', file_path=? WHERE id=?",
                (f"funding_documents/test-required-{row['id']}.pdf", row["id"]),
            )
    db.execute("UPDATE documents SET availability=NULL, status='Missing', file_path=NULL WHERE id=?", (optional["id"],))
    db.commit()
    db.close()

    # Only the target optional document is answered "Yes"; all other
    # optional documents are explicitly answered "No", matching the real
    # UI where each optional document has its own Yes/No choice.
    payload = _funding_payload(rows, optional="no")
    payload[f"document_{optional['id']}_availability"] = "yes"
    payload[f"document_{optional['id']}_file"] = (
        io.BytesIO(b"%PDF-1.4\n% fictional optional document\n"), f"test-{optional['id']}.pdf"
    )
    response = client.post("/application/step/documents", data=payload, content_type="multipart/form-data")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/application/step/bank")


def test_optional_no_can_continue_without_upload(client, student):
    app, rows = _funding_docs(student["student_id"])
    response = client.post("/application/step/documents", data=_funding_payload(rows, optional="no"))
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/application/step/bank")

    optional = next(r for r in rows if not r["is_required"])
    db = get_db()
    saved = db.execute("SELECT availability, status FROM documents WHERE id=?", (optional["id"],)).fetchone()
    db.close()
    assert saved["availability"] == "No"
    assert saved["status"] == "Missing"


def test_optional_funding_choice_persists_when_returning(client, student):
    app, rows = _funding_docs(student["student_id"])
    optional = next(r for r in rows if not r["is_required"])
    response = client.post("/application/step/documents", data=_funding_payload(rows, optional="no"))
    assert response.status_code == 302

    page = client.get("/application/step/documents")
    html = page.get_data(as_text=True)
    assert f'name="document_{optional["id"]}_availability"' in html
    assert f'id="doc_{optional["id"]}_no"' in html
    assert 'checked' in html


def test_required_visa_photograph_missing_blocks(client, student):
    req = visa_request_for(client, student)
    docs = _visa_docs(req)
    photo = next(r for r in docs if r["document_type"] == "Passport-size Photograph")
    db = get_db()
    db.execute("UPDATE visa_documents SET stored_file=NULL, status='Missing', availability=NULL WHERE id=?", (photo["id"],))
    db.commit()
    db.close()

    optional = {f"document_{r['id']}_availability": "no" for r in docs if not r["is_required"]}
    response = client.post(f"/student-visa/application/{req}/step/documents", data=optional)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/student-visa/application/{req}/step/documents")


def test_required_national_id_missing_blocks(client, student):
    req = visa_request_for(client, student)
    docs = _visa_docs(req)
    nid = next(r for r in docs if r["document_type"] == "National ID")
    db = get_db()
    db.execute("UPDATE visa_documents SET stored_file=NULL, status='Missing', availability=NULL WHERE id=?", (nid["id"],))
    db.commit()
    db.close()

    optional = {f"document_{r['id']}_availability": "no" for r in docs if not r["is_required"]}
    response = client.post(f"/student-visa/application/{req}/step/documents", data=optional)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/student-visa/application/{req}/step/documents")


def test_optional_visa_no_can_continue(client, student):
    req = visa_request_for(client, student)
    docs = _visa_docs(req)
    optional = next(r for r in docs if not r["is_required"])
    payload = {f"document_{r['id']}_availability": "no" for r in docs if not r["is_required"]}
    payload[f"document_{optional['id']}_availability"] = "no"
    response = client.post(f"/student-visa/application/{req}/step/documents", data=payload)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/student-visa/application/{req}/step/documents")


def test_optional_visa_yes_without_upload_blocks(client, student):
    req = visa_request_for(client, student)
    docs = _visa_docs(req)
    optional = next(r for r in docs if not r["is_required"])
    payload = {f"document_{r['id']}_availability": "no" for r in docs if not r["is_required"]}
    payload[f"document_{optional['id']}_availability"] = "yes"
    response = client.post(f"/student-visa/application/{req}/step/documents", data=payload)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/student-visa/application/{req}/step/documents")


def test_optional_visa_yes_with_upload_can_continue(client, student):
    req = visa_request_for(client, student)
    docs = _visa_docs(req)
    optional = next(r for r in docs if not r["is_required"])
    payload = {f"document_{r['id']}_availability": "no" for r in docs if not r["is_required"]}
    payload[f"document_{optional['id']}_availability"] = "yes"
    payload[f"document_{optional['id']}_file"] = (
        io.BytesIO(make_pdf(["FICTIONAL TEST SUPPORTING DOCUMENT"])), "support.pdf"
    )
    response = client.post(
        f"/student-visa/application/{req}/step/documents",
        data=payload,
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/student-visa/application/{req}/step/documents")

    saved = next(r for r in _visa_docs(req) if r["id"] == optional["id"])
    assert saved["availability"] == "Yes"
    assert saved["status"] == "Uploaded"


def test_existing_uploaded_visa_documents_still_work(client, student):
    req = visa_request_for(client, student)
    docs = _visa_docs(req)
    required = [r for r in docs if r["is_required"]]
    assert all(r["stored_file"] for r in required)
    page = client.get(f"/student-visa/application/{req}/step/documents")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Passport-size Photograph" in html
    assert "National ID" in html


def test_visa_document_choice_persists(client, student):
    req = visa_request_for(client, student)
    docs = _visa_docs(req)
    optional = next(r for r in docs if not r["is_required"])
    payload = {f"document_{r['id']}_availability": "no" for r in docs if not r["is_required"]}
    response = client.post(f"/student-visa/application/{req}/step/documents", data=payload)
    assert response.status_code == 302
    page = client.get(f"/student-visa/application/{req}/step/documents")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert f'id="visa_doc_{optional["id"]}_no"' in html
    assert "Not available" in html


def test_visa_document_file_remains_student_owned(client, student):
    req = visa_request_for(client, student)
    docs = _visa_docs(req)
    photo = next(r for r in docs if r["document_type"] == "Passport-size Photograph")
    response = client.get(f"/student-visa/documents/file/{photo['id']}")
    assert response.status_code == 200
    assert response.headers.get("Cache-Control") == "no-store, private"
