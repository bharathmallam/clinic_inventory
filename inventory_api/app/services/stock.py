"""Stock: the ledger, batches, and the quantities everything else asks about.

Single source of truth: products.current_stock is only ever written here, right
after the stock_movements row that explains it, and a database trigger refuses
any change that doesn't match that row. A batch's quantity is never stored — it
is the sum of its ledger rows.
"""
from datetime import date

from ..extensions import db
from ..models import Batch, Product, RequestItem, StockMovement, utcnow
from . import audit
from .core import WorkflowError, clean_note, clean_text, parse_date, positive_int

# What people type when a pack carries no batch number: all mean "not known".
UNKNOWN_BATCH = {"", "-", "--", "—", "–", "NA", "N/A", "NIL", "NONE", "UNKNOWN"}


def clean_batch_no(value):
    """The batch number as stored, or None when it isn't known (shown as "—")."""
    text = " ".join(str(value or "").split()).upper()[:40]
    return None if text in UNKNOWN_BATCH else text


# --------------------------------------------------------------------------- quantities

def usable_quantity(product):
    """Stock that may actually be handed out: expired batches don't count."""
    today = utcnow().date()
    return sum(batch.quantity for batch in product.batches
               if batch.quantity > 0 and not (batch.expiry_date and batch.expiry_date < today))


def expired_quantity(product):
    today = utcnow().date()
    return sum(batch.quantity for batch in product.batches
               if batch.quantity > 0 and batch.expiry_date and batch.expiry_date < today)


def reserved_quantity(product):
    """Approved but not yet handed over: promised to someone already."""
    total = db.session.query(db.func.coalesce(db.func.sum(RequestItem.quantity), 0)).filter(
        RequestItem.product_id == product.id, RequestItem.status == RequestItem.APPROVED).scalar()
    return int(total or 0)


def available_quantity(product):
    """What a person may ask for, or a checker may approve."""
    return usable_quantity(product) - reserved_quantity(product)


def reserved_map(product_ids):
    """reserved quantity for many products in one query."""
    rows = db.session.query(RequestItem.product_id,
                            db.func.sum(RequestItem.quantity)).filter(
        RequestItem.status == RequestItem.APPROVED,
        RequestItem.product_id.in_(product_ids) if product_ids else db.text("1=0")).group_by(
        RequestItem.product_id)
    return {product_id: int(total or 0) for product_id, total in rows}


# --------------------------------------------------------------------------- batches

def batch_for(product, batch_no, expiry_date, received_on):
    """The product's batch with this number, created if new. The same batch
    number can't carry two different expiry dates. Stock with no batch number is
    kept together per expiry date."""
    batch_no = clean_batch_no(batch_no)
    if batch_no is None:
        same_expiry = (Batch.expiry_date.is_(None) if expiry_date is None
                       else Batch.expiry_date == expiry_date)
        batch = (Batch.query.filter(Batch.product_id == product.id, Batch.batch_no.is_(None),
                                    same_expiry).order_by(Batch.id).first())
        if batch is None:
            batch = Batch(product_id=product.id, batch_no=None, expiry_date=expiry_date,
                          received_dt=received_on)
            db.session.add(batch)
            db.session.flush()
        return batch

    batch = Batch.query.filter_by(product_id=product.id, batch_no=batch_no).first()
    if batch is None:
        batch = Batch(product_id=product.id, batch_no=batch_no, expiry_date=expiry_date,
                      received_dt=received_on)
        db.session.add(batch)
        db.session.flush()
    elif expiry_date and batch.expiry_date and expiry_date != batch.expiry_date:
        raise WorkflowError(
            f"{product.name}: batch {batch_no} is already recorded with expiry "
            f"{batch.expiry_date:%d %b %Y}, not {expiry_date:%d %b %Y}. "
            "Check the batch number or the expiry.")
    elif expiry_date and not batch.expiry_date:
        batch.expiry_date = expiry_date
    return batch


def take_from_batches(product, quantity, movement_fields):
    """Hand out `quantity`, earliest expiry first, skipping expired batches.
    Returns the batches used as [(batch, quantity)]."""
    today = utcnow().date()
    left = quantity
    used = []
    for batch in product.batches_in_stock():
        if left <= 0:
            break
        if batch.expiry_date and batch.expiry_date < today:
            continue
        take = min(batch.quantity, left)
        if take <= 0:
            continue
        _record_change(product, -take, StockMovement(batch_id=batch.id, **movement_fields))
        used.append((batch, take))
        left -= take
    if left > 0:
        raise WorkflowError(f"{product.name}: only {quantity - left} {product.unit} could be "
                            f"taken from the batches in stock.", 409)
    return used


def receive_into_batch(product, quantity, batch, movement_fields):
    return _record_change(product, quantity, StockMovement(batch_id=batch.id, **movement_fields))


# --------------------------------------------------------------------------- the ledger

def _record_change(product, change, movement):
    """The ONLY place products.current_stock changes.

    The ledger row is written first, then the stock level; the database refuses
    any stock change that does not match the ledger row just written."""
    new_balance = product.current_stock + change
    if new_balance < 0:
        raise WorkflowError(f"{product.name}: stock cannot go below zero "
                            f"({product.current_stock} in stock, {-change} asked for).", 409)
    movement.product_id = product.id
    movement.quantity_change = change
    movement.balance_after = new_balance
    db.session.add(movement)
    db.session.flush()
    product.current_stock = new_balance
    db.session.flush()
    db.session.expire(movement.batch, ["quantity"])
    db.session.expire(product, ["batches"])
    return movement


def change_stock(person, product_id, quantity_change, reason, note=None,
                 batch_no=None, expiry_date=None, batch_id=None):
    """Add stock (restock) or correct it (adjustment). Returns the batches moved."""
    if not person.can("stock.manage"):
        raise WorkflowError("Your access does not allow changing stock.", 403)
    product = db.session.query(Product).filter_by(id=product_id).with_for_update().first()
    if not product:
        raise WorkflowError("Product not found.", 404)
    if reason not in (StockMovement.RESTOCK, StockMovement.ADJUSTMENT):
        raise WorkflowError("Stock changes are either a restock or an adjustment.")

    try:
        change = int(quantity_change)
    except (TypeError, ValueError):
        raise WorkflowError("Enter a whole number.")
    if change == 0:
        raise WorkflowError("Enter how many to add, or a minus number to take away.")
    if reason == StockMovement.RESTOCK and change < 0:
        raise WorkflowError("A restock adds stock. Use an adjustment to take stock away.")
    note = clean_note(note)
    if reason == StockMovement.ADJUSTMENT and not note:
        raise WorkflowError("An adjustment needs a note saying why.")

    today = utcnow().date()
    expiry = parse_date(expiry_date, "Expiry date", end_of_month=True)
    if expiry and expiry < today:
        raise WorkflowError("That expiry date has already passed.")

    fields = dict(reason=reason, created_by=person.id, note=note)
    if change > 0:
        batch = batch_for(product, batch_no, expiry, today)
        moved = [(batch, change)]
        receive_into_batch(product, change, batch, fields)
    elif batch_id:
        batch = Batch.query.filter_by(id=batch_id, product_id=product.id).first()
        if not batch:
            raise WorkflowError("That batch does not belong to this product.", 404)
        if batch.quantity < -change:
            raise WorkflowError(f"{batch.label[0].upper()}{batch.label[1:]} only has "
                                f"{batch.quantity} {product.unit}.", 409)
        _record_change(product, change, StockMovement(batch_id=batch.id, **fields))
        moved = [(batch, change)]
    else:
        moved = [(batch, -taken) for batch, taken in take_from_batches(product, -change, fields)]

    audit.record("STOCK_" + reason, person, entity="product", entity_id=product.id,
                 details={"product": product.name, "change": change, "reason": reason,
                          "note": note, "stock_after": product.current_stock,
                          "batches": [{"batch_no": batch.batch_no, "quantity": quantity,
                                       "expiry": batch.expiry_date.isoformat()
                                       if batch.expiry_date else None} for batch, quantity in moved]})
    return moved
