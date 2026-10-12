"""The lists people pick from, purchase bills, payments and reports."""
from functools import wraps

from flask import Blueprint, Response, g, jsonify, request

from ..auth import login_required
from ..extensions import db
from ..gst import gstin_problem, normalise_gstin, pan, state_name
from ..models import AccessLevel, Category, HsnCode, Invoice, Unit, Vendor
from ..services import masters, purchases, reports, transfer
from ..services.core import atomic

bp = Blueprint("purchasing", __name__, url_prefix="/api")


def _body():
    return request.get_json(silent=True) or {}


def inventory_viewer(view):
    """Anyone who may see stock figures: checkers, store and admin."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.user.sees_inventory:
            return jsonify(error="You don't have access to this."), 403
        return view(*args, **kwargs)
    return wrapper


def _state():
    return masters.organisation().effective_state_code


# --------------------------------------------------------------------------- categories

@bp.get("/categories")
@login_required()
def list_categories():
    rows = Category.query.order_by(Category.name).all()
    return jsonify(categories=[row.to_dict() for row in rows])


@bp.post("/categories")
@login_required("category.manage")
def create_category():
    with atomic():
        category = masters.create_category(g.user, _body())
        return jsonify(category=category.to_dict(), message=f"{category.name} added."), 201


@bp.patch("/categories/<int:category_id>")
@login_required("category.manage")
def update_category(category_id):
    with atomic():
        category = masters.update_category(g.user, category_id, _body())
        return jsonify(category=category.to_dict(), message=f"{category.name} updated.")


@bp.delete("/categories/<int:category_id>")
@login_required("category.manage")
def remove_category(category_id):
    """Only one nobody has used; anything else is switched off."""
    with atomic():
        name = masters.delete_category(g.user, category_id)
        return jsonify(message=f"{name} removed.")


# --------------------------------------------------------------------------- vendors

@bp.get("/vendors")
@login_required()
@inventory_viewer
def list_vendors():
    rows = Vendor.query.order_by(Vendor.name).all()
    return jsonify(vendors=[row.to_dict(with_contacts=True) for row in rows])


@bp.post("/vendors")
@login_required("purchase.manage")
def create_vendor():
    with atomic():
        vendor = masters.create_vendor(g.user, _body())
        return jsonify(vendor=vendor.to_dict(with_contacts=True),
                       message=f"{vendor.name} added."), 201


@bp.patch("/vendors/<int:vendor_id>")
@login_required("purchase.manage")
def update_vendor(vendor_id):
    with atomic():
        vendor = masters.update_vendor(g.user, vendor_id, _body())
        return jsonify(vendor=vendor.to_dict(with_contacts=True),
                       message=f"{vendor.name} updated.")


@bp.post("/vendors/<int:vendor_id>/contacts")
@login_required("purchase.manage")
def add_contact(vendor_id):
    with atomic():
        contact = masters.add_contact(g.user, vendor_id, _body())
        return jsonify(contact=contact.to_dict(), message=f"{contact.name} added."), 201


@bp.delete("/vendors/contacts/<int:contact_id>")
@login_required("purchase.manage")
def remove_contact(contact_id):
    with atomic():
        masters.remove_contact(g.user, contact_id)
        return jsonify(message="Contact removed.")


@bp.post("/vendors/import")
@login_required("purchase.manage")
def import_vendors():
    data = _body()
    dry_run = bool(data.get("dry_run"))
    with atomic():
        report = transfer.import_vendors(g.user, data.get("csv") or "", dry_run=dry_run)
        if not report.ok:
            db.session.rollback()
        return jsonify(report=report.to_dict(), dry_run=dry_run), (200 if report.ok else 400)


@bp.get("/gstin/<gstin>")
@login_required()
@inventory_viewer
def check_gstin(gstin):
    """Live check while someone types: is it valid, whose state, already used?"""
    value = normalise_gstin(gstin)
    problem = gstin_problem(value)
    used = Vendor.query.filter_by(gstin=value).first() if not problem else None
    return jsonify(gstin=value, ok=not problem, problem=problem,
                   state=state_name(value[:2]) if not problem else None,
                   pan=pan(value) if not problem else None,
                   used_by={"id": used.id, "name": used.name} if used else None)


# --------------------------------------------------------------------------- units, HSN, the clinic

@bp.get("/units")
@login_required()
def list_units():
    return jsonify(units=[row.to_dict() for row in Unit.query.order_by(Unit.name)])


@bp.post("/units")
@login_required("list.manage")
def create_unit():
    with atomic():
        unit = masters.create_unit(g.user, _body())
        return jsonify(unit=unit.to_dict(), message=f"{unit.name} added."), 201


@bp.patch("/units/<int:unit_id>")
@login_required("list.manage")
def update_unit(unit_id):
    with atomic():
        unit = masters.update_unit(g.user, unit_id, _body())
        return jsonify(unit=unit.to_dict(), message=f"{unit.name} updated.")


@bp.delete("/units/<int:unit_id>")
@login_required("list.manage")
def remove_unit(unit_id):
    """Only a unit nothing counts in; one in use is switched off instead."""
    with atomic():
        name = masters.delete_unit(g.user, unit_id)
        return jsonify(message=f"{name} removed.")


@bp.get("/hsn")
@login_required()
def list_hsn():
    return jsonify(hsn=[row.to_dict() for row in HsnCode.query.order_by(HsnCode.code)])


@bp.post("/hsn")
@login_required("list.manage")
def create_hsn():
    with atomic():
        row = masters.create_hsn(g.user, _body())
        return jsonify(hsn=row.to_dict(), message=f"HSN {row.code} added."), 201


@bp.patch("/hsn/<int:hsn_id>")
@login_required("list.manage")
def update_hsn(hsn_id):
    with atomic():
        row = masters.update_hsn(g.user, hsn_id, _body())
        return jsonify(hsn=row.to_dict(), message=f"HSN {row.code} updated.")


@bp.post("/hsn/import")
@login_required("list.manage")
def import_hsn():
    data = _body()
    dry_run = bool(data.get("dry_run"))
    with atomic():
        report = transfer.import_hsn(g.user, data.get("csv") or "", dry_run=dry_run)
        if not report.ok:
            db.session.rollback()
        return jsonify(report=report.to_dict(), dry_run=dry_run), (200 if report.ok else 400)


@bp.get("/organisation")
@login_required()
def get_organisation():
    return jsonify(organisation=masters.organisation().to_dict())


@bp.patch("/organisation")
@login_required("user.manage")
def update_organisation():
    with atomic():
        org = masters.update_organisation(g.user, _body())
        return jsonify(organisation=org.to_dict(), message="Clinic details saved.")


# --------------------------------------------------------------------------- bills

@bp.get("/bills")
@login_required()
@inventory_viewer
def list_bills():
    state = _state()
    query = Invoice.query.order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
    if status := request.args.get("status"):
        query = query.filter(Invoice.status == status.upper())
    if vendor_id := request.args.get("vendor_id"):
        query = query.filter(Invoice.vendor_id == int(vendor_id))
    if month := request.args.get("month"):
        query = query.filter(db.func.substr(db.func.cast(Invoice.invoice_date, db.String), 1, 7) == month)
    if text := (request.args.get("q") or "").strip():
        query = query.filter(Invoice.invoice_no.ilike(f"%{text}%"))
    bills = query.limit(300).all()
    rows = [bill.to_dict(state, with_items=False) for bill in bills]
    if payment := request.args.get("payment_status"):
        rows = [row for row in rows if row["payment_status"] == payment.upper()]
    summary = {
        "count": len(rows),
        "total": round(sum(row["net"] for row in rows if row["status"] == Invoice.POSTED), 2),
        "unpaid": round(sum(row["net"] - row["paid_amount"] for row in rows
                            if row["status"] == Invoice.POSTED), 2),
        "gst": round(sum(row["gst"] for row in rows if row["status"] == Invoice.POSTED), 2),
    }
    return jsonify(bills=rows, summary=summary)


@bp.get("/bills/<int:invoice_id>")
@login_required()
@inventory_viewer
def one_bill(invoice_id):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice:
        return jsonify(error="Bill not found."), 404
    return jsonify(bill=invoice.to_dict(_state()))


@bp.post("/bills")
@login_required("purchase.manage")
def create_bill():
    data = _body()
    with atomic():
        invoice = purchases.save_draft(g.user, data, post_now=bool(data.get("post")))
        return jsonify(bill=invoice.to_dict(_state()),
                       message="Bill posted: stock added." if data.get("post") else "Draft saved."), 201


@bp.put("/bills/<int:invoice_id>")
@login_required("purchase.manage")
def update_bill(invoice_id):
    data = _body()
    with atomic():
        invoice = purchases.save_draft(g.user, data, invoice_id=invoice_id,
                                       post_now=bool(data.get("post")))
        return jsonify(bill=invoice.to_dict(_state()),
                       message="Bill posted: stock added." if data.get("post") else "Draft saved.")


@bp.delete("/bills/<int:invoice_id>")
@login_required("purchase.manage")
def delete_bill(invoice_id):
    with atomic():
        purchases.delete_draft(g.user, invoice_id)
        return jsonify(message="Draft deleted.")


@bp.post("/bills/<int:invoice_id>/post")
@login_required("purchase.manage")
def post_bill(invoice_id):
    with atomic():
        invoice = purchases.post(g.user, invoice_id)
        return jsonify(bill=invoice.to_dict(_state()), message="Bill posted: stock added.")


@bp.post("/bills/<int:invoice_id>/cancel")
@login_required("purchase.manage")
def cancel_bill(invoice_id):
    with atomic():
        invoice = purchases.cancel(g.user, invoice_id, _body().get("note"))
        return jsonify(bill=invoice.to_dict(_state()), message="Bill cancelled: stock taken back out.")


@bp.post("/bills/<int:invoice_id>/payments")
@login_required("purchase.manage")
def add_payment(invoice_id):
    with atomic():
        payment = purchases.add_payment(g.user, invoice_id, _body())
        return jsonify(bill=payment.invoice.to_dict(_state()),
                       message=f"₹{float(payment.amount):,.2f} recorded."), 201


@bp.delete("/bills/payments/<int:payment_id>")
@login_required("purchase.manage")
def remove_payment(payment_id):
    with atomic():
        invoice = purchases.remove_payment(g.user, payment_id, _body().get("note"))
        return jsonify(bill=invoice.to_dict(_state()), message="Payment removed.")


# --------------------------------------------------------------------------- reports

@bp.get("/reports/usage")
@login_required()
@inventory_viewer
def usage_report():
    report = reports.monthly_usage(request.args.get("month"), request.args.get("category_id"),
                                   g.user.visible_categories())
    if request.args.get("format") == "csv":
        return Response(reports.monthly_usage_csv(report), mimetype="text/csv",
                        headers={"Content-Disposition":
                                 f"attachment; filename=usage-{report['month']}.csv"})
    return jsonify(report=report)


@bp.get("/reports/stock-value")
@login_required()
@inventory_viewer
def stock_value_report():
    return jsonify(report=reports.stock_value(request.args.get("category_id"),
                                              g.user.visible_categories()))
