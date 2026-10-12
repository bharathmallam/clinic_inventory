"""Orders: completed handovers, one order code per request."""
import csv
import io

from flask import Blueprint, Response, g, jsonify, request
from sqlalchemy import or_

from ..auth import login_required
from ..extensions import db
from ..models import Order, Product, Request, RequestItem, Staff
from ..services import orders as order_service
from ..services.core import atomic

bp = Blueprint("orders", __name__, url_prefix="/api/orders")


def _visible():
    """People who only raise requests see their own orders; everyone else sees all."""
    query = Order.query.order_by(Order.completed_dt.desc())
    return query.filter(Order.requested_by == g.user.id) if g.user.sees_only_own_requests else query


def _month(column):
    return db.func.substr(db.func.cast(column, db.String), 1, 7)      # 'YYYY-MM' on MySQL and SQLite


def _filtered():
    query = _visible()
    if month := request.args.get("month"):
        query = query.filter(_month(Order.completed_dt) == month)
    if category := request.args.get("category_id"):
        query = query.filter(Order.request.has(Request.items.any(db.and_(
            RequestItem.status == RequestItem.DELIVERED,
            RequestItem.product.has(Product.category_id == int(category))))))
    if text := (request.args.get("q") or "").strip():
        like = f"%{text}%"
        query = query.filter(or_(
            Order.short_code.ilike(like),
            Order.order_no.ilike(like),
            Order.request.has(Request.request_no.ilike(like)),
            Order.requester.has(Staff.full_name.ilike(like)),
            Order.request.has(Request.items.any(RequestItem.product.has(
                or_(Product.name.ilike(like), Product.sku.ilike(like))))),
        ))
    return query


def _limit(default=100, cap=500):
    try:
        return max(1, min(int(request.args.get("limit", default)), cap))
    except ValueError:
        return default


@bp.get("")
@login_required()
def list_orders():
    rows = _filtered().limit(_limit()).all()
    return jsonify(orders=[order.to_dict(with_items=False) for order in rows])


@bp.get("/<code>")
@login_required()
def one_order(code):
    order = order_service.find(code)
    if not order or (g.user.sees_only_own_requests and order.requested_by != g.user.id):
        return jsonify(error="Order not found."), 404
    return jsonify(order=order.to_dict())


@bp.post("/<code>/acknowledge")
@login_required("order.acknowledge")
def acknowledge(code):
    with atomic():
        receipt = order_service.acknowledge(g.user, code, (request.get_json(silent=True) or {}).get("note"))
        return jsonify(order=receipt.order.to_dict(),
                       message=f"{receipt.order.short_code}: receipt confirmed.")


@bp.get("/export")
@login_required()
def export_orders():
    """One row per product handed over, for a spreadsheet."""
    if g.user.sees_only_own_requests:
        return jsonify(error="You don't have access to this."), 403
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["order_code", "request_code", "category", "sku", "product", "unit",
                     "quantity", "requested_by", "approved_by", "delivered_by",
                     "requested_on", "delivered_on", "purpose", "received_confirmed_on",
                     "received_note"])
    for order in _filtered().limit(5000).all():
        data = order.to_dict()
        received = data["acknowledged"]
        for line in data["items"]:
            writer.writerow([
                data["code"], data["request_code"], line["product"]["category"],
                line["product"]["sku"], line["product"]["name"], line["product"]["unit"],
                line["quantity"],
                data["requested_by"]["full_name"] if data["requested_by"] else "",
                line["decided_by"]["full_name"] if line["decided_by"] else "",
                line["delivered_by"]["full_name"] if line["delivered_by"] else "",
                data["requested_dt"][:10], (line["delivered_dt"] or "")[:10], data["purpose"] or "",
                (received["at"][:10] if received else ""), (received["note"] or "") if received else "",
            ])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=orders.csv"})
