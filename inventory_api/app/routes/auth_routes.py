"""Signing in, who am I, and setting your own password."""
from flask import Blueprint, g, jsonify, request
from werkzeug.security import check_password_hash

from ..auth import issue_token, login_required
from ..extensions import db
from ..models import Staff, utcnow
from ..services import accounts, audit
from ..services.core import atomic

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@bp.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    login_name = (data.get("username") or data.get("email") or "").strip().lower()
    person = Staff.query.filter(db.or_(Staff.username == login_name,
                                       Staff.email == login_name)).first()
    if not person or not person.is_active \
            or not check_password_hash(person.password_hash, data.get("password") or ""):
        with atomic():
            audit.record("LOGIN_FAILED", None, entity="auth", details={"login": login_name[:190]})
        return jsonify(error="Incorrect username or password."), 401

    with atomic():
        person.last_login_dt = utcnow()
        audit.record("LOGIN", person, entity="auth", entity_id=person.id)
    return jsonify(token=issue_token(person), user=person.to_dict())


@bp.get("/me")
@login_required(allow_password_change_pending=True)
def me():
    return jsonify(user=g.user.to_dict())


@bp.post("/change-password")
@login_required(allow_password_change_pending=True)
def change_password():
    data = request.get_json(silent=True) or {}
    with atomic():
        accounts.change_own_password(g.user, data.get("current_password") or "",
                                     data.get("new_password") or "")
    return jsonify(user=g.user.to_dict(), message="Password changed.")
