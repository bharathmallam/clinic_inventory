"""Purchase bills: enter as printed, post to add stock, cancel, and pay.

Amounts are never stored: taxable, GST and the net are worked out from the
lines. A bill line is copied the way the vendor printed it — so many packs, so
many units in a pack — and the stock it adds is always in the product's own
issue unit."""
from datetime import date
from decimal import Decimal, InvalidOperation

from ..extensions import db
from ..models import HsnCode, Invoice, InvoiceItem, Payment, Product, StockMovement, Vendor, utcnow
from . import audit, masters, stock
from .core import WorkflowError, clean_text, parse_date, positive_int, whole_number

MAX_LINES = 120


def _decimal(value, field, default="0"):
    try:
        number = Decimal(str(value if value not in (None, "") else default).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise WorkflowError(f"{field} must be a number.")
    return number


def _vendor(vendor_id):
    vendor = db.session.get(Vendor, int(vendor_id)) if str(vendor_id or "").isdigit() else None
    if vendor is None or not vendor.is_active:
        raise WorkflowError("Choose the vendor from the list.")
    return vendor


def _build_line(number, raw, invoice_date, vendor):
    label = f"Line {number}"
    line_type = (raw.get("line_type") or InvoiceItem.STOCK).upper()
    if line_type not in InvoiceItem.TYPES:
        raise WorkflowError(f"{label}: a line is either a product or a non-stock item.")

    hsn = None
    if raw.get("hsn_id"):
        hsn = db.session.get(HsnCode, int(raw["hsn_id"])) if str(raw["hsn_id"]).isdigit() else None
        if hsn is None:
            raise WorkflowError(f"{label}: choose the HSN code from the list.")

    values = {"line_no": number, "line_type": line_type}
    if line_type == InvoiceItem.STOCK:
        product = db.session.get(Product, int(raw["product_id"])) \
            if str(raw.get("product_id") or "").isdigit() else None
        if product is None or not product.is_active:
            raise WorkflowError(f"{label}: choose the product from the list.")
        hsn = hsn or product.hsn
        expiry = parse_date(raw.get("expiry_date"), f"{label} expiry", end_of_month=True)
        if expiry and expiry <= invoice_date:
            raise WorkflowError(f"{label} ({product.name}): expiry must be after the bill date.")
        units_per_pack = whole_number(raw.get("units_per_pack"), f"{label} units per pack",
                                      default=product.pack_size or 1, minimum=1) or 1
        values.update(product_id=product.id, description=None,
                      batch_no=stock.clean_batch_no(raw.get("batch_no")), expiry_date=expiry,
                      units_per_pack=units_per_pack)
        name = product.name
    else:
        description = clean_text(raw.get("description"), 200)
        if not description:
            raise WorkflowError(f"{label}: describe what was bought (e.g. CO2 laser machine).")
        values.update(product_id=None, description=description.upper(), batch_no=None,
                      expiry_date=None, units_per_pack=1)
        name = description

    rate = _decimal(raw.get("rate"), f"{label} rate")
    if rate < 0:
        raise WorkflowError(f"{label} ({name}): the rate cannot be negative.")
    discount = _decimal(raw.get("discount_percent"), f"{label} discount")
    if not 0 <= discount <= 100:
        raise WorkflowError(f"{label} ({name}): discount must be between 0 and 100%.")
    mrp = raw.get("mrp")
    gst_rate = Decimal("0") if vendor.is_composition else Decimal(str(hsn.gst_rate)) if hsn else Decimal("0")

    values.update(
        hsn_id=hsn.id if hsn else None,
        gst_rate=gst_rate,
        pack_quantity=positive_int(raw.get("pack_quantity") or raw.get("quantity"), f"{label} quantity"),
        free_packs=whole_number(raw.get("free_packs") or raw.get("free_quantity"), f"{label} free"),
        rate=rate,
        discount_percent=discount,
        mrp=_decimal(mrp, f"{label} MRP") if mrp not in (None, "") else None,
    )
    return values


def _header(data):
    vendor = _vendor(data.get("vendor_id"))
    invoice_no = clean_text(data.get("invoice_no"), 40)
    if not invoice_no:
        raise WorkflowError("Enter the bill number.")
    invoice_date = parse_date(data.get("invoice_date"), "Bill date")
    if not invoice_date:
        raise WorkflowError("Enter the bill date.")
    if invoice_date > utcnow().date():
        raise WorkflowError("The bill date is in the future.")
    round_off = _decimal(data.get("round_off"), "Round-off")
    if not -10 <= round_off <= 10:
        raise WorkflowError("Round-off should be a small correction, between -10 and 10.")
    return vendor, invoice_no.upper(), invoice_date, round_off


def _check_unique(vendor, invoice_no, invoice_id=None):
    query = Invoice.query.filter_by(vendor_id=vendor.id, invoice_no=invoice_no)
    if invoice_id:
        query = query.filter(Invoice.id != invoice_id)
    clash = query.first()
    if clash:
        raise WorkflowError(f"Bill {invoice_no} from {vendor.name} is already entered "
                            f"(dated {clash.invoice_date:%d %b %Y}, {clash.status.lower()}).", 409)


def save_draft(person, data, invoice_id=None, post_now=False):
    """Create or replace a draft bill. Posting it is a separate, final step."""
    if not person.can("purchase.manage"):
        raise WorkflowError("Your access does not allow entering bills.", 403)
    vendor, invoice_no, invoice_date, round_off = _header(data)
    lines = data.get("lines") or []
    if not lines:
        raise WorkflowError("A bill needs at least one line.")
    if len(lines) > MAX_LINES:
        raise WorkflowError(f"A bill can have at most {MAX_LINES} lines.")

    built = [_build_line(number, raw, invoice_date, vendor) for number, raw in enumerate(lines, start=1)]
    seen = {}
    for values in built:
        if values["line_type"] == InvoiceItem.STOCK:
            key = (values["product_id"], values["batch_no"] or f"no batch {values['expiry_date']}")
            if key in seen:
                raise WorkflowError(f"Line {values['line_no']} repeats line {seen[key]} "
                                    "(same product and batch): put the full quantity on one line.")
            seen[key] = values["line_no"]
    _check_unique(vendor, invoice_no, invoice_id)

    if invoice_id:
        invoice = db.session.query(Invoice).filter_by(id=invoice_id).with_for_update().first()
        if not invoice:
            raise WorkflowError("Bill not found.", 404)
        if invoice.status != Invoice.DRAFT:
            raise WorkflowError(f"This bill is {invoice.status.lower()} and can no longer be edited.", 409)
        invoice.items.clear()
        db.session.flush()
    else:
        invoice = Invoice(created_by=person.id)
        db.session.add(invoice)

    invoice.vendor_id = vendor.id
    invoice.invoice_no = invoice_no
    invoice.invoice_date = invoice_date
    invoice.round_off = round_off
    invoice.notes = clean_text(data.get("notes"))
    invoice.updated_by = person.id
    db.session.flush()
    for values in built:
        db.session.add(InvoiceItem(invoice_id=invoice.id, created_by=person.id,
                                   updated_by=person.id, **values))
    db.session.flush()
    db.session.refresh(invoice)
    audit.record("BILL_SAVED", person, entity="invoice", entity_id=invoice.id,
                 details={"invoice_no": invoice.invoice_no, "vendor": vendor.name,
                          "lines": len(built)})
    if post_now:
        post(person, invoice.id)
    return invoice


def delete_draft(person, invoice_id):
    if not person.can("purchase.manage"):
        raise WorkflowError("Your access does not allow entering bills.", 403)
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice:
        raise WorkflowError("Bill not found.", 404)
    if invoice.status != Invoice.DRAFT:
        raise WorkflowError("Only a draft can be deleted. Cancel the bill instead.", 409)
    details = {"invoice_no": invoice.invoice_no, "vendor": invoice.vendor.name}
    db.session.delete(invoice)
    db.session.flush()
    audit.record("BILL_DELETED", person, entity="invoice", entity_id=invoice_id, details=details)


def post(person, invoice_id):
    """Post the bill: every stock line goes into its batch and the ledger."""
    if not person.can("purchase.manage"):
        raise WorkflowError("Your access does not allow entering bills.", 403)
    invoice = db.session.query(Invoice).filter_by(id=invoice_id).with_for_update().first()
    if not invoice:
        raise WorkflowError("Bill not found.", 404)
    if invoice.status != Invoice.DRAFT:
        raise WorkflowError(f"This bill is already {invoice.status.lower()}.", 409)
    if not invoice.items:
        raise WorkflowError("This bill has no lines.")

    received = []
    for line in invoice.items:
        if line.line_type != InvoiceItem.STOCK:
            continue
        product = db.session.query(Product).filter_by(id=line.product_id).with_for_update().one()
        batch = stock.batch_for(product, line.batch_no, line.expiry_date, invoice.invoice_date)
        stock.receive_into_batch(product, line.received_quantity, batch,
                                 dict(reason=StockMovement.RESTOCK, invoice_item_id=line.id,
                                      created_by=person.id,
                                      note=f"Bill {invoice.invoice_no}"))
        received.append({"product": product.name, "batch_no": batch.batch_no,
                         "quantity": line.received_quantity, "unit": product.unit})

    invoice.status = Invoice.POSTED
    invoice.posted_dt = utcnow()
    invoice.updated_by = person.id
    db.session.flush()
    audit.record("BILL_POSTED", person, entity="invoice", entity_id=invoice.id,
                 details={"invoice_no": invoice.invoice_no, "vendor": invoice.vendor.name,
                          "received": received, "net": invoice.totals()["net"]})
    return invoice


def cancel(person, invoice_id, note=None):
    """Cancel a posted bill, taking the stock back out — only while it is all
    still there."""
    if not person.can("purchase.manage"):
        raise WorkflowError("Your access does not allow entering bills.", 403)
    note = clean_text(note)
    if not note:
        raise WorkflowError("Say why this bill is being cancelled.")
    invoice = db.session.query(Invoice).filter_by(id=invoice_id).with_for_update().first()
    if not invoice:
        raise WorkflowError("Bill not found.", 404)
    if invoice.status != Invoice.POSTED:
        raise WorkflowError(f"Only a posted bill can be cancelled (this one is "
                            f"{invoice.status.lower()}).", 409)
    if invoice.payments:
        raise WorkflowError("This bill has payments against it. Remove them first.", 409)

    movements = (StockMovement.query.filter(StockMovement.invoice_item_id.in_(
        [line.id for line in invoice.items])).all())
    for movement in movements:
        batch = movement.batch
        if batch.quantity < movement.quantity_change:
            raise WorkflowError(
                f"Some of {movement.product.name} {batch.label} from this bill has already been "
                "used, so it can't be cancelled. Correct it with a stock adjustment instead.", 409)
    for movement in movements:
        product = db.session.query(Product).filter_by(id=movement.product_id).with_for_update().one()
        stock._record_change(product, -movement.quantity_change, StockMovement(
            batch_id=movement.batch_id, reason=StockMovement.ADJUSTMENT, created_by=person.id,
            note=f"Bill {invoice.invoice_no} cancelled: {note}"))

    invoice.status = Invoice.CANCELLED
    invoice.notes = clean_text(f"{invoice.notes or ''} | Cancelled: {note}".strip(" |"))
    invoice.updated_by = person.id
    db.session.flush()
    audit.record("BILL_CANCELLED", person, entity="invoice", entity_id=invoice.id,
                 details={"invoice_no": invoice.invoice_no, "vendor": invoice.vendor.name,
                          "note": note, "stock_reversed": len(movements)})
    return invoice


# --------------------------------------------------------------------------- payments

def add_payment(person, invoice_id, data):
    """Record money paid. A bill may be settled in several payments."""
    if not person.can("purchase.manage"):
        raise WorkflowError("Your access does not allow recording payments.", 403)
    invoice = db.session.query(Invoice).filter_by(id=invoice_id).with_for_update().first()
    if not invoice:
        raise WorkflowError("Bill not found.", 404)
    if invoice.status != Invoice.POSTED:
        raise WorkflowError("Only a posted bill can be paid.", 409)

    amount = _decimal(data.get("amount"), "Amount")
    if amount <= 0:
        raise WorkflowError("The amount must be more than zero.")
    paid_on = parse_date(data.get("paid_on"), "Payment date") or utcnow().date()
    if paid_on > utcnow().date():
        raise WorkflowError("The payment date is in the future.")
    if paid_on < invoice.invoice_date:
        raise WorkflowError("The payment date is before the bill date.")
    method = (data.get("method") or "CASH").upper()
    if method not in Payment.METHODS:
        raise WorkflowError("Choose how it was paid: " + ", ".join(Payment.METHODS).title() + ".")

    state = masters.organisation().effective_state_code
    net = Decimal(str(invoice.totals(state)["net"]))
    already = Decimal(str(invoice.paid_amount))
    if already + amount > net + Decimal("0.01"):
        left = net - already
        raise WorkflowError(f"That is more than is left to pay (₹{left:.2f}).", 409)

    payment = Payment(invoice_id=invoice.id, amount=amount, paid_on=paid_on, method=method,
                      reference=clean_text(data.get("reference"), 60),
                      note=clean_text(data.get("note")), created_by=person.id, updated_by=person.id)
    db.session.add(payment)
    db.session.flush()
    db.session.refresh(invoice)
    audit.record("BILL_PAYMENT_ADDED", person, entity="invoice", entity_id=invoice.id,
                 details={"invoice_no": invoice.invoice_no, "vendor": invoice.vendor.name,
                          **payment.to_dict(), "paid_so_far": invoice.paid_amount,
                          "status": invoice.payment_status(state)})
    return payment


def remove_payment(person, payment_id, note=None):
    if not person.can("purchase.manage"):
        raise WorkflowError("Your access does not allow recording payments.", 403)
    payment = db.session.get(Payment, payment_id)
    if not payment:
        raise WorkflowError("Payment not found.", 404)
    invoice = payment.invoice
    details = {"invoice_no": invoice.invoice_no, **payment.to_dict(), "note": clean_text(note)}
    db.session.delete(payment)
    db.session.flush()
    db.session.refresh(invoice)
    audit.record("BILL_PAYMENT_REMOVED", person, entity="invoice", entity_id=invoice.id,
                 details=details)
    return invoice
