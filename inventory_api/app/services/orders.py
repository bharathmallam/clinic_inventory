"""Orders: one order number per completed request, and the receipt for it.

An order is created by the app itself the moment a request has nothing left
waiting — every line delivered, rejected or cancelled, with at least one line
actually handed over. People see and quote the short code (AF470D), which is
unique on its own; the database also keeps the full ORD-20260926-AF470D.
"""
import secrets

from ..extensions import db
from ..models import Order, OrderAcknowledgement, RequestItem, utcnow
from . import audit
from .core import WorkflowError, clean_note


def new_code():
    """A short code nobody has used before, so it identifies an order on its own."""
    for _ in range(20):
        code = secrets.token_hex(3).upper()
        if not db.session.query(Order.id).filter_by(short_code=code).first():
            return code
    raise RuntimeError("Could not generate an order code.")


def create_if_complete(person, request):
    """Give a finished request its order number. Returns the Order, or None if
    the request is still running or already has one."""
    if db.session.query(Order.id).filter_by(request_id=request.id).first():
        return None
    db.session.flush()
    if not request.is_finished:
        return None
    delivered = [item for item in request.items if item.status == RequestItem.DELIVERED]
    if not delivered:
        return None                 # everything was rejected or cancelled: no handover, no order

    code = new_code()
    order = Order(order_no=f"ORD-{utcnow():%Y%m%d}-{code}", short_code=code,
                  request_id=request.id, requested_by=request.requested_by,
                  completed_dt=max(item.delivered_dt for item in delivered),
                  created_by=person.id if person else None)
    db.session.add(order)
    db.session.flush()
    audit.record("ORDER_CREATED", person, entity="order", entity_id=order.id,
                 request_no=request.request_no,
                 details={"order_no": order.order_no, "code": code,
                          "lines": [{"product": item.product.name, "quantity": item.quantity}
                                    for item in delivered]})
    return order


def find(code):
    """By the short code people see, or the full order number."""
    code = (code or "").strip().upper()
    order = Order.query.filter_by(short_code=code).first()
    return order or Order.query.filter_by(order_no=code).first()


def acknowledge(person, code, note=None):
    """The person who raised the request confirms the goods reached them.

    Only they can, and only once, so a handover can never be disputed as
    "delivered but not received" without it showing here."""
    order = find(code)
    if order is None:
        raise WorkflowError("Order not found.", 404)
    order = db.session.query(Order).filter_by(id=order.id).with_for_update().one()
    if order.requested_by != person.id:
        raise WorkflowError("Only the person who raised the request can confirm they received it.", 403)
    if order.acknowledgement:
        raise WorkflowError(f"{order.short_code} was already confirmed as received on "
                            f"{order.acknowledgement.acknowledged_dt:%d %b %Y}.", 409)

    receipt = OrderAcknowledgement(order_id=order.id, acknowledged_by=person.id,
                                   acknowledged_dt=utcnow(), note=clean_note(note),
                                   created_by=person.id)
    db.session.add(receipt)
    db.session.flush()
    db.session.refresh(order)
    audit.record("ORDER_ACKNOWLEDGED", person, entity="order", entity_id=order.id,
                 request_no=order.request.request_no,
                 details={"order_no": order.order_no, "code": order.short_code,
                          "note": receipt.note,
                          "lines": [{"product": item.product.name, "quantity": item.quantity}
                                    for item in order.delivered_items]})
    return receipt
