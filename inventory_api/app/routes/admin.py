"""Accounts, access and the audit log — the Administrator's screens."""
from flask import Blueprint, g, jsonify, request

from ..auth import login_required
from ..extensions import db
from ..models import AccessLevel, AuditLog, Staff
from ..services import accounts, transfer
from ..services.core import atomic

bp = Blueprint("admin", __name__, url_prefix="/api")


def _body():
    return request.get_json(silent=True) or {}


@bp.get("/levels")
@login_required()
def list_levels():
    rows = AccessLevel.query.order_by(AccessLevel.id).all()
    return jsonify(levels=[row.to_dict() for row in rows])


@bp.get("/staff")
@login_required("user.manage")
def list_staff():
    rows = Staff.query.order_by(Staff.full_name).all()
    return jsonify(staff=[row.to_dict() for row in rows])


@bp.post("/staff")
@login_required("user.manage")
def create_staff():
    with atomic():
        person, password = accounts.create_staff(g.user, _body())
        return jsonify(staff=person.to_dict(), temporary_password=password,
                       message=f"{person.full_name} added."), 201


@bp.patch("/staff/<int:staff_id>")
@login_required("user.manage")
def update_staff(staff_id):
    with atomic():
        person = accounts.update_staff(g.user, staff_id, _body())
        return jsonify(staff=person.to_dict(), message=f"{person.full_name} updated.")


@bp.post("/staff/<int:staff_id>/reset-password")
@login_required("user.manage")
def reset_password(staff_id):
    with atomic():
        person, password = accounts.reset_password(g.user, staff_id)
        return jsonify(staff=person.to_dict(), temporary_password=password,
                       message=f"New temporary password for {person.full_name}.")


@bp.post("/staff/import")
@login_required("user.manage")
def import_staff():
    data = _body()
    dry_run = bool(data.get("dry_run"))
    with atomic():
        report, passwords = transfer.import_staff(g.user, data.get("csv") or "", dry_run=dry_run)
        if not report.ok:
            db.session.rollback()
        return jsonify(report=report.to_dict(), passwords=passwords, dry_run=dry_run), \
            (200 if report.ok else 400)


@bp.get("/audit")
@login_required("audit.view")
def audit_log():
    query = AuditLog.query.order_by(AuditLog.id.desc())
    if action := request.args.get("action"):
        query = query.filter(AuditLog.action == action)
    if reference := request.args.get("request_no"):
        # People type the short code they see (EE87DA); the log keeps the full number.
        query = query.filter(db.or_(AuditLog.request_no == reference,
                                    AuditLog.request_no.like(f"%-{reference}")))
    if entity := request.args.get("entity"):
        query = query.filter(AuditLog.entity == entity)
    if actor_id := request.args.get("actor_id"):
        query = query.filter(AuditLog.actor_id == int(actor_id))
    if start := request.args.get("from"):
        query = query.filter(AuditLog.created_dt >= start)
    if end := request.args.get("to"):
        query = query.filter(AuditLog.created_dt <= f"{end} 23:59:59")
    try:
        limit = max(1, min(int(request.args.get("limit", 200)), 1000))
    except ValueError:
        limit = 200
    return jsonify(entries=[row.to_dict() for row in query.limit(limit)])


@bp.get("/audit/actions")
@login_required("audit.view")
def audit_actions():
    from ..extensions import db
    rows = db.session.query(AuditLog.action).distinct().order_by(AuditLog.action).all()
    return jsonify(actions=[row[0] for row in rows])
