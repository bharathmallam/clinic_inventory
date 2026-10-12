"""The only place that writes to audit_log. Rows are never updated or deleted."""
from flask import has_request_context, request as http_request

from ..extensions import db
from ..models import AuditLog


def record(action, actor, *, entity, entity_id=None, request_no=None, details=None):
    """Add an audit row to the current transaction, so it is committed (or
    rolled back) together with the change it describes."""
    ip = agent = None
    if has_request_context():
        forwarded = http_request.headers.get("X-Forwarded-For", "")
        ip = (forwarded.split(",")[0].strip() or http_request.remote_addr or "")[:45] or None
        agent = (http_request.user_agent.string or "")[:255] or None

    entry = AuditLog(
        action=action,
        entity=entity,
        entity_id=str(entity_id) if entity_id is not None else None,
        actor_id=actor.id if actor else None,
        actor_levels=", ".join(sorted(actor.levels())) if actor else None,
        request_no=request_no,
        details=details,
        ip_address=ip,
        user_agent=agent,
    )
    db.session.add(entry)
    return entry
