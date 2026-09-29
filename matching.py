"""
matching.py
------------
The funding-matching engine.

This is intentionally simple and rule-based (no machine learning) so a
beginner can read it top to bottom and understand exactly why a student
matched - or didn't - with a given funding opportunity.

How it works
------------
For every open funding opportunity, we compare the student's application
answers against the opportunity's stored criteria and add up points.
The final score (0-100) decides the match strength, and we keep a plain
-English list of "reasons" so the match is transparent to the student.
"""


def _contains(csv_field, needle):
    """Case-insensitive check: is `needle` one of the comma-separated
    values stored in `csv_field`? An empty csv_field means "open to all",
    so it always counts as a match.
    """
    if not csv_field:
        return True
    values = [v.strip().lower() for v in csv_field.split(",")]
    return needle.strip().lower() in values or "all" in values or "any" in values


def score_application_against_opportunity(application, opportunity):
    """Return (score, reasons) for one application vs one opportunity."""
    score = 0
    max_score = 0
    reasons = []

    # Country / citizenship eligibility (20 points)
    max_score += 20
    if _contains(opportunity["eligible_countries"], application["country"] or ""):
        score += 20
        reasons.append(f"Accepts students from {application['country']}")

    # Education level (20 points)
    max_score += 20
    if _contains(opportunity["education_levels"], application["education_level"] or ""):
        score += 20
        reasons.append(f"Supports {application['education_level']} students")

    # Field of study (20 points)
    max_score += 20
    if _contains(opportunity["fields"], application["field_of_study"] or ""):
        score += 20
        reasons.append(f"Covers your field of study ({application['field_of_study']})")

    # Funding type requested vs offered (15 points)
    max_score += 15
    prefs = (application["preferences"] or "").lower()
    funding_type = (opportunity["funding_type"] or "").lower()
    if funding_type in prefs or not prefs:
        score += 15
        reasons.append(f"Offers the funding type you are looking for ({opportunity['funding_type']})")

    # Study destination (15 points)
    max_score += 15
    if _contains(opportunity["study_destination"], application["country"] or "") or not opportunity["study_destination"]:
        score += 15
        reasons.append("Matches your preferred study destination")

    # Deadline still open (10 points) - only reward opportunities that
    # are still accepting applications.
    max_score += 10
    if opportunity["is_open"]:
        score += 10
        reasons.append("Currently open for applications")

    # Normalize to a 0-100 scale in case max_score ever changes.
    final_score = round((score / max_score) * 100) if max_score else 0
    return final_score, reasons


def strength_label(score):
    if score >= 90:
        return "Strong Match"
    elif score >= 70:
        return "Good Match"
    elif score >= 50:
        return "Potential Match"
    else:
        return "Low Match"


def match_type_for(score, opportunity):
    """Decide which of the five match types applies."""
    if score < 50:
        return "Not Eligible"
    if opportunity["application_method"] == "Partner Application":
        return "Partner Referral"
    if score >= 90:
        return "Eligible Match"
    if opportunity["application_method"] == "Official External Application":
        return "Application Required"
    return "Potential Match"


def application_needs_bank_details(db, application):
    """Preview check used by the annual application's "bank" step: does
    ANY currently-open opportunity that this application would plausibly
    match (score >= 50, the same bar run_matching_for_application uses)
    require bank details? If not a single one does, the student is never
    shown the bank information form at all - see the "bank" step in
    APPLICATION_STEPS in app.py.

    This is a preview, not a persisted match - real funding_matches rows
    are only created at submission time (run_matching_for_application).
    """
    opportunities = db.execute("SELECT * FROM funding_opportunities WHERE is_open = 1").fetchall()
    for opp in opportunities:
        score, _ = score_application_against_opportunity(application, opp)
        if score >= 50 and opp["bank_details_required"] in ("TRUE", "PROVIDER_SPECIFIC"):
            return True
    return False


def run_matching_for_application(db, application_id):
    """Score an application against every open opportunity and store the
    results in funding_matches. Existing matches for this application are
    replaced so re-running matching always reflects the latest data.
    """
    application = db.execute(
        "SELECT * FROM funding_applications WHERE id = ?", (application_id,)
    ).fetchone()
    if application is None:
        return []

    opportunities = db.execute("SELECT * FROM funding_opportunities WHERE is_open = 1").fetchall()

    db.execute("DELETE FROM funding_matches WHERE application_id = ?", (application_id,))

    created = []
    for opp in opportunities:
        score, reasons = score_application_against_opportunity(application, opp)
        if score < 50:
            continue  # Only store matches worth showing the student.
        strength = strength_label(score)
        mtype = match_type_for(score, opp)
        db.execute(
            """INSERT INTO funding_matches
               (application_id, opportunity_id, score, match_strength, match_type, reasons)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (application_id, opp["id"], score, strength, mtype, " | ".join(reasons)),
        )
        created.append((opp["id"], score))

    db.commit()
    return created
