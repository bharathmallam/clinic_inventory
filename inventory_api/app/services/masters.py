"""The lists everything else picks from: categories, units, HSN codes, vendors,
and the clinic's own details. Each fact is kept once here."""
import re
from decimal import Decimal, InvalidOperation

from ..extensions import db
from ..gst import GST_RATES, gstin_problem, normalise_gstin
from ..models import (Category, HsnCode, Organisation, Product, StaffAccess, Unit, Vendor,
                      VendorContact)
from . import audit
from .core import WorkflowError, clean_text, key_of

VENDOR_FIELDS = ("address", "city", "pincode", "phone", "email", "drug_licence_no", "notes")


def _stamp(row, person, creating=False):
    if creating:
        row.created_by = person.id if person else None
    row.updated_by = person.id if person else None
    return row


# --------------------------------------------------------------------------- the clinic

def organisation():
    org = db.session.get(Organisation, 1)
    if org is None:
        org = Organisation(id=1, name="Ultrahealth", state_code="36")
        db.session.add(org)
        db.session.flush()
    return org


def update_organisation(person, data):
    org = organisation()
    before = org.to_dict()
    if "name" in data:
        org.name = clean_text(data["name"], 120) or org.name
    if "gstin" in data:
        gstin = normalise_gstin(data["gstin"])
        if gstin:
            problem = gstin_problem(gstin)
            if problem:
                raise WorkflowError(f"GSTIN {gstin}: {problem}")
        org.gstin = gstin or None
    _stamp(org, person)
    db.session.flush()
    audit.record("ORGANISATION_UPDATED", person, entity="organisation", entity_id=1,
                 details={"before": before, "after": org.to_dict()})
    return org


# --------------------------------------------------------------------------- categories

def create_category(person, data):
    name = clean_text(data.get("name"), 80)
    if not name:
        raise WorkflowError("Category name is required.")
    code = re.sub(r"[^A-Z0-9]+", "_", (clean_text(data.get("code")) or name).upper()).strip("_")[:30]
    if not code:
        raise WorkflowError("Category code is required.")
    if Category.query.filter((Category.code == code) | (db.func.lower(Category.name) == name.lower())).first():
        raise WorkflowError(f"A category called {name} already exists.", 409)
    category = Category(code=code, name=name, description=clean_text(data.get("description")),
                        tracks_expiry=_flag(data.get("tracks_expiry"), True),
                        needs_hsn=_flag(data.get("needs_hsn"), True))
    _stamp(category, person, creating=True)
    db.session.add(category)
    db.session.flush()
    audit.record("CATEGORY_CREATED", person, entity="category", entity_id=category.id,
                 details=category.to_dict())
    return category


def delete_category(person, category_id):
    """Remove a category nobody has used — a typo, not a part of the clinic.

    Anything that has been used is switched off instead, because products, bills
    and the audit trail still point at it."""
    category = db.session.get(Category, category_id)
    if not category:
        raise WorkflowError("Category not found.", 404)
    products = Product.query.filter_by(category_id=category.id).count()
    if products:
        raise WorkflowError(f"{category.name} still has {products} product"
                            f"{'s' if products > 1 else ''}. A category that has been used can only "
                            "be switched off, never deleted.", 409)
    if StaffAccess.query.filter_by(category_id=category.id).count():
        raise WorkflowError(f"People still have access to {category.name}. "
                            "Untick it on the Users page first.", 409)
    details = category.to_dict()
    name = category.name
    audit.record("CATEGORY_DELETED", person, entity="category", entity_id=category.id, details=details)
    db.session.delete(category)
    db.session.flush()
    return name


def update_category(person, category_id, data):
    category = db.session.get(Category, category_id)
    if not category:
        raise WorkflowError("Category not found.", 404)
    before = category.to_dict()
    if "name" in data:
        name = clean_text(data["name"], 80)
        if not name:
            raise WorkflowError("Category name is required.")
        clash = Category.query.filter(Category.id != category.id,
                                      db.func.lower(Category.name) == name.lower()).first()
        if clash:
            raise WorkflowError(f"A category called {name} already exists.", 409)
        category.name = name
    for field in ("description",):
        if field in data:
            setattr(category, field, clean_text(data[field]))
    for flag in ("tracks_expiry", "needs_hsn"):
        if flag in data:
            setattr(category, flag, _flag(data[flag], getattr(category, flag)))
    if "is_active" in data:
        active = _flag(data["is_active"], True)
        if not active and Product.query.filter_by(category_id=category.id, is_active=True).count():
            raise WorkflowError("This category still has active products. "
                                "Remove or move them before switching it off.", 409)
        category.is_active = active
    _stamp(category, person)
    db.session.flush()
    audit.record("CATEGORY_UPDATED", person, entity="category", entity_id=category.id,
                 details={"before": before, "after": category.to_dict()})
    return category


def _flag(value, default=False):
    if value in (None, ""):
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


# --------------------------------------------------------------------------- units

def create_unit(person, data):
    name = clean_text(data.get("name"), 40)
    if not name:
        raise WorkflowError("Unit name is required (e.g. Box, Strip, Piece).")
    name = name[0].upper() + name[1:]
    code = re.sub(r"[^A-Z0-9]+", "_", (clean_text(data.get("code")) or name).upper()).strip("_")[:20]
    if Unit.query.filter((Unit.code == code) | (db.func.lower(Unit.name) == name.lower())).first():
        raise WorkflowError(f"The unit {name} already exists.", 409)
    unit = _stamp(Unit(code=code, name=name), person, creating=True)
    db.session.add(unit)
    db.session.flush()
    audit.record("UNIT_CREATED", person, entity="unit", entity_id=unit.id, details=unit.to_dict())
    return unit


def delete_unit(person, unit_id):
    """Remove a unit nothing counts in. One that is in use is switched off instead."""
    unit = db.session.get(Unit, unit_id)
    if not unit:
        raise WorkflowError("Unit not found.", 404)
    using = Product.query.filter_by(unit_id=unit.id).count()
    if using:
        raise WorkflowError(f"{using} product{'s' if using > 1 else ''} counted in {unit.name}. "
                            "A unit in use can only be switched off, never deleted.", 409)
    name = unit.name
    audit.record("UNIT_DELETED", person, entity="unit", entity_id=unit.id, details=unit.to_dict())
    db.session.delete(unit)
    db.session.flush()
    return name


def update_unit(person, unit_id, data):
    unit = db.session.get(Unit, unit_id)
    if not unit:
        raise WorkflowError("Unit not found.", 404)
    before = unit.to_dict()
    if "name" in data:
        name = clean_text(data["name"], 40)
        if not name:
            raise WorkflowError("Unit name is required.")
        clash = Unit.query.filter(Unit.id != unit.id, db.func.lower(Unit.name) == name.lower()).first()
        if clash:
            raise WorkflowError(f"The unit {name} already exists.", 409)
        unit.name = name[0].upper() + name[1:]
    if "is_active" in data:
        unit.is_active = _flag(data["is_active"], True)
    _stamp(unit, person)
    db.session.flush()
    audit.record("UNIT_UPDATED", person, entity="unit", entity_id=unit.id,
                 details={"before": before, "after": unit.to_dict()})
    return unit


# --------------------------------------------------------------------------- HSN codes

def parse_gst_rate(value):
    try:
        rate = Decimal(str(value).replace("%", "").strip())
    except (InvalidOperation, ValueError):
        raise WorkflowError("GST rate must be a number, e.g. 5, 12 or 18.")
    if 0 < rate < 1 and rate * 100 in [Decimal(str(r)) for r in GST_RATES]:
        rate = rate * 100                       # 0.05 typed for 5 %
    if float(rate) not in GST_RATES:
        raise WorkflowError(f"{rate}% is not a GST rate. Use one of: "
                            + ", ".join(f"{r:g}" for r in GST_RATES) + ".")
    return rate


def _hsn_values(data, row=None):
    values = {}
    if row is None or "code" in data:
        code = re.sub(r"\D", "", str(data.get("code") or ""))
        if len(code) not in (4, 6, 8):
            raise WorkflowError("An HSN code has 4, 6 or 8 digits.")
        values["code"] = code
    if row is None or "description" in data:
        description = clean_text(data.get("description"), 200)
        if not description:
            raise WorkflowError("Describe the HSN code (e.g. 'Cosmetic and skin-care preparations').")
        values["description"] = description
    if row is None or "gst_rate" in data:
        values["gst_rate"] = parse_gst_rate(data.get("gst_rate"))
    return values


def create_hsn(person, data):
    values = _hsn_values(data)
    if HsnCode.query.filter_by(code=values["code"]).first():
        raise WorkflowError(f"HSN {values['code']} already exists.", 409)
    row = _stamp(HsnCode(**values), person, creating=True)
    db.session.add(row)
    db.session.flush()
    audit.record("HSN_CREATED", person, entity="hsn", entity_id=row.id, details=row.to_dict())
    return row


def update_hsn(person, hsn_id, data):
    row = db.session.get(HsnCode, hsn_id)
    if not row:
        raise WorkflowError("HSN code not found.", 404)
    before = row.to_dict()
    values = _hsn_values(data, row)
    if "code" in values and values["code"] != row.code:
        raise WorkflowError("An HSN code can't be renumbered; add the new code instead.")
    for key, value in values.items():
        setattr(row, key, value)
    if "is_active" in data:
        row.is_active = _flag(data["is_active"], True)
    _stamp(row, person)
    db.session.flush()
    audit.record("HSN_UPDATED", person, entity="hsn", entity_id=row.id,
                 details={"before": before, "after": row.to_dict()})
    return row


# --------------------------------------------------------------------------- vendors

def _vendor_values(data, vendor=None):
    values = {}
    if vendor is None or "name" in data:
        name = clean_text(data.get("name"), 150)
        if not name:
            raise WorkflowError("Vendor name is required.")
        values["name"] = name.upper()
        values["name_key"] = key_of(name)
    if vendor is None or "gstin" in data:
        gstin = normalise_gstin(data.get("gstin"))
        if gstin:
            problem = gstin_problem(gstin)
            if problem:
                raise WorkflowError(f"GSTIN {gstin}: {problem}")
        values["gstin"] = gstin or None
    for field in VENDOR_FIELDS:
        if vendor is None or field in data:
            values[field] = clean_text(data.get(field), 300)
    if vendor is None or "is_composition" in data:
        values["is_composition"] = _flag(data.get("is_composition"))
    if values.get("pincode") and not re.fullmatch(r"\d{6}", values["pincode"]):
        raise WorkflowError("PIN code must be 6 digits.")
    if values.get("email") and "@" not in values["email"]:
        raise WorkflowError("That email address doesn't look right.")
    return values


def _check_vendor_duplicates(values, vendor_id=None):
    others = Vendor.query.filter(Vendor.id != vendor_id) if vendor_id else Vendor.query
    gstin = values.get("gstin")
    if gstin:
        same = others.filter_by(gstin=gstin).first()
        if same:
            raise WorkflowError(f"GSTIN {gstin} already belongs to {same.name}.", 409)
    if "name_key" in values:
        same_name = others.filter_by(name_key=values["name_key"]).all()
        if any(v.gstin == gstin for v in same_name) or (not gstin and same_name):
            raise WorkflowError(
                f"A vendor called {same_name[0].name} already exists"
                + (f" with GSTIN {same_name[0].gstin}" if same_name[0].gstin else "")
                + ". If this is a different branch, enter its own GSTIN.", 409)


def create_vendor(person, data):
    values = _vendor_values(data)
    _check_vendor_duplicates(values)
    vendor = _stamp(Vendor(**values), person, creating=True)
    db.session.add(vendor)
    db.session.flush()
    for contact in data.get("contacts") or []:
        add_contact(person, vendor.id, contact, quiet=True)
    audit.record("VENDOR_CREATED", person, entity="vendor", entity_id=vendor.id,
                 details=vendor.to_dict(with_contacts=True))
    return vendor


def update_vendor(person, vendor_id, data):
    vendor = db.session.query(Vendor).filter_by(id=vendor_id).with_for_update().first()
    if not vendor:
        raise WorkflowError("Vendor not found.", 404)
    before = vendor.to_dict()
    values = _vendor_values(data, vendor)
    if "gstin" in values and values["gstin"] != vendor.gstin and _vendor_has_bills(vendor):
        raise WorkflowError("This vendor already has purchase bills; its GSTIN can't be changed. "
                            "Add the new GSTIN as a separate vendor.", 409)
    _check_vendor_duplicates({**values, "gstin": values.get("gstin", vendor.gstin)}, vendor.id)
    for key, value in values.items():
        setattr(vendor, key, value)
    if "is_active" in data:
        vendor.is_active = _flag(data["is_active"], True)
    _stamp(vendor, person)
    db.session.flush()
    audit.record("VENDOR_UPDATED", person, entity="vendor", entity_id=vendor.id,
                 details={"before": before, "after": vendor.to_dict()})
    return vendor


def _vendor_has_bills(vendor):
    from ..models import Invoice
    return db.session.query(Invoice.id).filter_by(vendor_id=vendor.id).first() is not None


def add_contact(person, vendor_id, data, quiet=False):
    vendor = db.session.get(Vendor, vendor_id)
    if not vendor:
        raise WorkflowError("Vendor not found.", 404)
    name = clean_text(data.get("name"), 120)
    if not name:
        raise WorkflowError("The contact's name is required.")
    contact = _stamp(VendorContact(vendor_id=vendor.id, name=name,
                                   role=clean_text(data.get("role"), 60),
                                   phone=clean_text(data.get("phone"), 40),
                                   email=clean_text(data.get("email"), 120)), person, creating=True)
    db.session.add(contact)
    db.session.flush()
    if not quiet:
        audit.record("VENDOR_CONTACT_ADDED", person, entity="vendor", entity_id=vendor.id,
                     details={"vendor": vendor.name, **contact.to_dict()})
    return contact


def remove_contact(person, contact_id):
    contact = db.session.get(VendorContact, contact_id)
    if not contact:
        raise WorkflowError("Contact not found.", 404)
    details = {"vendor": contact.vendor.name, **contact.to_dict()}
    db.session.delete(contact)
    db.session.flush()
    audit.record("VENDOR_CONTACT_REMOVED", person, entity="vendor", entity_id=details and contact.vendor_id,
                 details=details)
