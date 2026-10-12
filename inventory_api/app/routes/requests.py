"""Requests: raise them, decide them, deliver them."""
from flask import Blueprint, g, jsonify, request

from ..auth import login_required
from ..extensions import db
from ..models import AuditLog, Request, RequestItem
from ..services import requests as request_service
from ..services.core import atomic

bp = Blueprint("requests", __name__, url_prefix="/api/requests")


def _body():
    return request.get_json(silent=True) or {}


def _limit(default=100, cap=500):
    try:
        return max(1, min(int(request.args.get("limit", default)), cap))
    except ValueError:
        return default


def _for_viewer(data):
    """A maker never sees stock figures, costs or prices — only their own lines."""
    return data


def _own_only(query, owner_column):
    """People who only raise requests see their own; checkers and store see all."""
    return query.filter(owner_column == g.user.id) if g.user.sees_only_own_requests else query


def _decidable(item):
    blocked = request_service.decision_block_reason(g.user, item)
    data = item.to_dict()
    data["can_decide"] = blocked is None
    data["blocked_reason"] = blocked
    product = item.product
    from ..services import stock
    data["product"]["available"] = max(stock.available_quantity(product), 0)
    data["product"]["current_stock"] = product.current_stock
    return data


@bp.post("")
@login_required("request.create")
def create_request():
    data = _body()
    lines = data.get("items") or data.get("lines") or []
    with atomic():
        created = request_service.create(g.user, lines, data.get("purpose"))
        return jsonify(request=created.to_dict(), request_no=created.request_no,
                       code=created.short_code,
                       message=f"Request {created.short_code} sent for approval."), 201


@bp.get("/mine")
@login_required("request.create")
def mine():
    """Still waiting: pending approval, or approved and waiting to be handed over."""
    items = (RequestItem.query.join(Request, RequestItem.request_id == Request.id)
             .filter(Request.requested_by == g.user.id,
                     RequestItem.status.in_((RequestItem.PENDING, RequestItem.APPROVED)))
             .order_by(Request.requested_dt.desc(), RequestItem.line_no).limit(_limit()).all())
    return jsonify(items=[item.to_dict() for item in items])


@bp.get("/pending")
@login_required()
def pending():
    query = RequestItem.query.join(Request, RequestItem.request_id == Request.id).filter(RequestItem.status == RequestItem.PENDING)
    items = _own_only(query, Request.requested_by).order_by(Request.requested_dt,
                                                            RequestItem.line_no).limit(_limit()).all()
    return jsonify(items=[_decidable(item) for item in items])


@bp.get("/approved")
@login_required()
def approved():
    query = RequestItem.query.join(Request, RequestItem.request_id == Request.id).filter(RequestItem.status == RequestItem.APPROVED)
    items = _own_only(query, Request.requested_by).order_by(RequestItem.decided_dt,
                                                            RequestItem.line_no).limit(_limit()).all()
    out = []
    for item in items:
        data = item.to_dict()
        data["product"]["current_stock"] = item.product.current_stock
        out.append(data)
    return jsonify(items=out)


@bp.get("/closed")
@login_required()
def closed():
    """Rejected and cancelled lines."""
    query = RequestItem.query.join(Request, RequestItem.request_id == Request.id).filter(
        RequestItem.status.in_((RequestItem.REJECTED, RequestItem.CANCELLED)))
    items = _own_only(query, Request.requested_by).order_by(
        RequestItem.decided_dt.desc(), RequestItem.id.desc()).limit(_limit()).all()
    return jsonify(items=[item.to_dict() for item in items])


@bp.get("/<reference>")
@login_required()
def one_request(reference):
    """By the code people quote (EE87DA) or the full number."""
    found = request_service.find(reference)
    if not found:
        return jsonify(error="Request not found."), 404
    if g.user.sees_only_own_requests and found.requested_by != g.user.id:
        return jsonify(error="You can only open your own requests."), 403
    data = found.to_dict()
    data["items"] = [item.to_dict(with_history=True) for item in found.items]
    trail = (AuditLog.query.filter_by(request_no=found.request_no)
             .order_by(AuditLog.id).limit(100).all())
    data["audit"] = [{"action": row.action, "by": row.actor.full_name if row.actor else None,
                      "at": row.created_dt.isoformat() + "Z",
                      "note": (row.details or {}).get("note") or (row.details or {}).get("reason")}
                     for row in trail]
    return jsonify(request=data)


@bp.post("/<line_ref>/approve")
@login_required("request.decide")
def approve(line_ref):
    with atomic():
        item = request_service.approve(g.user, line_ref, _body().get("note"))
        return jsonify(item=item.to_dict(),
                       message=f"{line_ref} approved ({item.quantity} {item.product.unit}).")


@bp.post("/<line_ref>/reject")
@login_required("request.decide")
def reject(line_ref):
    with atomic():
        item = request_service.reject(g.user, line_ref, _body().get("reason") or _body().get("note"))
        return jsonify(item=item.to_dict(), message=f"{line_ref} rejected.")


@bp.post("/<line_ref>/cancel")
@login_required("request.create")
def cancel(line_ref):
    with atomic():
        item = request_service.cancel(g.user, line_ref, _body().get("note"))
        return jsonify(item=item.to_dict(), message=f"{line_ref} cancelled.")


@bp.post("/<line_ref>/deliver")
@login_required("request.deliver")
def deliver(line_ref):
    with atomic():
        item = request_service.deliver(g.user, line_ref, _body().get("note"))
        return jsonify(item=item.to_dict(),
                       message=f"{line_ref}: {item.quantity} {item.product.unit} delivered to "
                               f"{item.request.requester.full_name}.")


@bp.post("/<reference>/approve-all")
@login_required("request.decide")
def approve_all(reference):
    with atomic():
        items = request_service.approve_all(g.user, reference, _body().get("note"))
        code = items[0].request.short_code
        return jsonify(items=[item.to_dict() for item in items],
                       message=f"{len(items)} line{'s' if len(items) > 1 else ''} of {code} approved.")


@bp.post("/<reference>/deliver-all")
@login_required("request.deliver")
def deliver_all(reference):
    with atomic():
        items = request_service.deliver_all(g.user, reference, _body().get("note"))
        code = items[0].request.short_code
        return jsonify(items=[item.to_dict() for item in items],
                       message=f"{len(items)} line{'s' if len(items) > 1 else ''} of {code} delivered.")
