"""Requests: raise, approve, reject, cancel, deliver.

A line is approved in full or rejected — quantities are never edited. A request
may finish partly fulfilled (some lines approved, others rejected); an
individual line never is.

Rows being changed are locked with SELECT ... FOR UPDATE, so two people acting
on the same line at once cannot both succeed, and two deliveries cannot oversell
the same stock. Lock order is always request line -> product.
"""
import secrets

from ..extensions import db
from ..models import AccessLevel, Product, Request, RequestItem, RequestItemHistory, StockMovement, utcnow
from . import audit, orders, stock
from .core import WorkflowError, clean_note, clean_text, positive_int

MAX_LINES = 60


def new_code():
    """A code nobody has used before, so it identifies a request on its own."""
    for _ in range(20):
        code = secrets.token_hex(3).upper()
        if not db.session.query(Request.id).filter_by(short_code=code).first():
            return code
    raise RuntimeError("Could not generate a request code.")


def _history(item, status, person, note=None):
    db.session.add(RequestItemHistory(request_item_id=item.id, status=status,
                                      created_by=person.id if person else None, note=note))


# --------------------------------------------------------------------------- raising

def create(person, lines, purpose=None):
    """Raise a request. `lines` = [{"product_id", "quantity"}, …].

    Nobody may ask for more than is available, so a request can always be met if
    stock stays put; a checker never has to edit a quantity."""
    if not person.can("request.create"):
        raise WorkflowError("Your access does not allow raising requests.", 403)
    if not lines:
        raise WorkflowError("Add at least one product to the request.")
    if len(lines) > MAX_LINES:
        raise WorkflowError(f"A request can have at most {MAX_LINES} products.")

    allowed = person.orderable_categories()
    wanted = {}
    for raw in lines:
        product_id = raw.get("product_id")
        quantity = positive_int(raw.get("quantity"), "Quantity", maximum=100000)
        if product_id in wanted:
            wanted[product_id] += quantity
        else:
            wanted[product_id] = quantity

    code = new_code()
    request = Request(request_no=f"REQ-{utcnow():%Y%m%d}-{code}", short_code=code,
                      requested_by=person.id, purpose=clean_text(purpose),
                      requested_dt=utcnow(), created_by=person.id)
    db.session.add(request)
    db.session.flush()

    items = []
    for number, (product_id, quantity) in enumerate(wanted.items(), start=1):
        product = db.session.query(Product).filter_by(id=product_id).with_for_update().first()
        if not product or not product.is_active:
            raise WorkflowError("One of the products is no longer on the list.", 404)
        if allowed is not None and product.category_id not in allowed:
            raise WorkflowError(f"You are not set up to order {product.category.name}.", 403)
        available = stock.available_quantity(product)
        if available <= 0:
            raise WorkflowError(f"{product.name} is out of stock, so it can't be requested yet.", 409)
        if quantity > available:
            raise WorkflowError(f"{product.name}: only {available} {product.unit} available. "
                                f"Ask for {available} or fewer.", 409)
        item = RequestItem(request_id=request.id, requested_by=person.id, line_no=number,
                           product_id=product.id, quantity=quantity,
                           status=RequestItem.PENDING, created_by=person.id)
        db.session.add(item)
        db.session.flush()
        _history(item, RequestItem.PENDING, person)
        items.append(item)

    audit.record("REQUEST_RAISED", person, entity="request", entity_id=request.id,
                 request_no=request.request_no,
                 details={"purpose": request.purpose,
                          "lines": [{"product": i.product.name, "quantity": i.quantity} for i in items]})
    return request


# --------------------------------------------------------------------------- deciding

def _locked_line(line_ref, statuses):
    """Find a line by EE87DA-2 (what people see) or REQ-20261005-EE87DA-2, and lock it."""
    reference, _, line_no = str(line_ref or "").rpartition("-")
    try:
        line_no = int(line_no)
    except ValueError:
        raise WorkflowError("Request line not found.", 404)
    reference = reference.upper()
    item = (db.session.query(RequestItem).join(Request, RequestItem.request_id == Request.id)
            .filter(db.or_(Request.short_code == reference, Request.request_no == reference),
                    RequestItem.line_no == line_no)
            .with_for_update(of=RequestItem).first())
    if not item:
        raise WorkflowError("Request line not found.", 404)
    if item.status not in statuses:
        raise WorkflowError(f"{line_ref} is {item.status.lower()}, so that can't be done now.", 409)
    return item


def decision_block_reason(person, item):
    """Why this person may not decide this line, or None if they may."""
    if not person.can("request.decide"):
        return "Your access does not allow approving requests."
    if item.requested_by == person.id:
        return "You cannot decide your own request; another checker must."
    if not person.may_decide_category(item.product.category_id):
        return f"You are not a checker for {item.product.category.name}."
    return None


def approve(person, line_ref, note=None):
    """Approve the line exactly as asked. If stock is short, reject it instead."""
    item = _locked_line(line_ref, (RequestItem.PENDING,))
    blocked = decision_block_reason(person, item)
    if blocked:
        raise WorkflowError(blocked, 403)

    product = db.session.query(Product).filter_by(id=item.product_id).with_for_update().one()
    available = stock.available_quantity(product)
    if item.quantity > available:
        raise WorkflowError(
            f"{product.name}: only {max(available, 0)} {product.unit} available now "
            f"({product.current_stock} in stock, {stock.reserved_quantity(product)} already approved "
            "for others). Restock first, or reject with a reason saying how many are available.", 409)

    item.status = RequestItem.APPROVED
    item.decided_by = person.id
    item.decided_dt = utcnow()
    item.reason = clean_note(note)
    item.updated_by = person.id
    db.session.flush()
    _history(item, RequestItem.APPROVED, person, item.reason)
    audit.record("REQUEST_APPROVED", person, entity="request_item", entity_id=item.id,
                 request_no=item.request.request_no,
                 details={"line": item.line_no, "product": product.name,
                          "quantity": item.quantity, "note": item.reason})
    return item


def reject(person, line_ref, reason):
    """Reject the line. The reason is required and is what the person reads."""
    item = _locked_line(line_ref, (RequestItem.PENDING,))
    blocked = decision_block_reason(person, item)
    if blocked:
        raise WorkflowError(blocked, 403)
    reason = clean_note(reason)
    if not reason:
        raise WorkflowError("Say why you are rejecting, and how many are available.")

    item.status = RequestItem.REJECTED
    item.decided_by = person.id
    item.decided_dt = utcnow()
    item.reason = reason
    item.updated_by = person.id
    db.session.flush()
    _history(item, RequestItem.REJECTED, person, reason)
    audit.record("REQUEST_REJECTED", person, entity="request_item", entity_id=item.id,
                 request_no=item.request.request_no,
                 details={"line": item.line_no, "product": item.product.name,
                          "quantity": item.quantity, "reason": reason})
    orders.create_if_complete(person, item.request)
    return item


def cancel(person, line_ref, note=None):
    """Only the person who raised it, and only while it is still pending."""
    item = _locked_line(line_ref, (RequestItem.PENDING,))
    if item.requested_by != person.id:
        raise WorkflowError("Only the person who raised a request can cancel it.", 403)

    item.status = RequestItem.CANCELLED
    item.reason = clean_note(note)
    item.updated_by = person.id
    db.session.flush()
    _history(item, RequestItem.CANCELLED, person, item.reason)
    audit.record("REQUEST_CANCELLED", person, entity="request_item", entity_id=item.id,
                 request_no=item.request.request_no,
                 details={"line": item.line_no, "product": item.product.name, "note": item.reason})
    orders.create_if_complete(person, item.request)
    return item


# --------------------------------------------------------------------------- delivering

def deliver(person, line_ref, note=None):
    """Hand over exactly what was approved, earliest expiry first."""
    if not person.can("request.deliver"):
        raise WorkflowError("Your access does not allow marking deliveries.", 403)
    item = _locked_line(line_ref, (RequestItem.APPROVED,))
    product = db.session.query(Product).filter_by(id=item.product_id).with_for_update().one()

    usable = stock.usable_quantity(product)
    if usable < item.quantity:
        expired = product.current_stock - usable
        raise WorkflowError(
            f"Not enough stock: {usable} {product.unit} usable, {item.quantity} needed"
            + (f" ({expired} more are expired and can't be handed out)" if expired else "") + ".", 409)

    now = utcnow()
    note = clean_note(note)
    used = stock.take_from_batches(product, item.quantity, dict(
        reason=StockMovement.DELIVERY, request_item_id=item.id, created_by=person.id,
        note=note, created_dt=now))

    item.status = RequestItem.DELIVERED
    item.delivered_by = person.id
    item.delivered_dt = now
    item.updated_by = person.id
    db.session.flush()
    _history(item, RequestItem.DELIVERED, person, note)
    audit.record("REQUEST_DELIVERED", person, entity="request_item", entity_id=item.id,
                 request_no=item.request.request_no,
                 details={"line": item.line_no, "product": product.name, "quantity": item.quantity,
                          "note": note, "stock_after": product.current_stock,
                          "batches": [{"batch_no": batch.batch_no, "quantity": quantity}
                                      for batch, quantity in used]})
    orders.create_if_complete(person, item.request)
    return item


# --------------------------------------------------------------------------- whole request at once

def find(reference):
    """By the code people quote (EE87DA) or the full number."""
    reference = str(reference or "").strip().upper()
    return (Request.query.filter_by(short_code=reference).first()
            or Request.query.filter_by(request_no=reference).first())


def _request(reference):
    request = find(reference)
    if not request:
        raise WorkflowError("Request not found.", 404)
    return request


def approve_all(person, reference, note=None):
    """Approve every pending line this person may decide, all or nothing."""
    request = _request(reference)
    mine = [item for item in request.items
            if item.status == RequestItem.PENDING and not decision_block_reason(person, item)]
    if not mine:
        raise WorkflowError("Nothing here is waiting for your approval.", 409)
    return [approve(person, f"{request.short_code}-{item.line_no}", note) for item in mine]


def deliver_all(person, reference, note=None):
    """Deliver every approved line of a request, all or nothing: if one product
    is short, nothing is delivered and the message says which."""
    request = _request(reference)
    approved = [item for item in request.items if item.status == RequestItem.APPROVED]
    if not approved:
        raise WorkflowError("Nothing in this request is approved and waiting for delivery.", 409)
    return [deliver(person, f"{request.short_code}-{item.line_no}", note) for item in approved]
