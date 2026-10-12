from datetime import timedelta
from functools import wraps

import jwt
from flask import current_app, g, jsonify, request

from .extensions import db
from .models import Staff, utcnow


def issue_token(user):
    now = utcnow()
    payload = {
        "sub": str(user.id),
        "iat": now,
        "exp": now + timedelta(hours=current_app.config["JWT_EXPIRY_HOURS"]),
    }
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")


def _current_user():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        payload = jwt.decode(header[7:], current_app.config["SECRET_KEY"], algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    person = db.session.get(Staff, int(payload["sub"]))
    return person if person and person.is_active else None


def login_required(permission=None, allow_password_change_pending=False):
    """Require a valid token; optionally require a permission such as 'stock.manage'.
    People who still have a temporary password can only reach the endpoints that
    let them set a new one."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = _current_user()
            if not user:
                return jsonify(error="Please sign in."), 401
            if user.must_change_password and not allow_password_change_pending:
                return jsonify(error="Please set a new password first.", code="PASSWORD_CHANGE_REQUIRED"), 403
            if permission and not user.can(permission):
                return jsonify(error="You do not have permission to do this."), 403
            g.user = user
            return view(*args, **kwargs)
        return wrapper
    return decorator
