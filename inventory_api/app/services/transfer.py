"""Loading data in and out as spreadsheets (CSV).

Every import is checked in full before anything is saved: if one row is wrong,
nothing is imported and the problems are listed by row number."""
import csv
import io

from ..extensions import db
from ..models import Category, HsnCode, Product, Staff, Unit
from . import accounts, catalogue, masters
from .core import WorkflowError, clean_text, key_of, parse_date, whole_number

PRODUCT_COLUMNS = ["category", "sku", "name", "unit", "reorder_level", "brand", "hsn",
                   "pack_size", "pack_label", "opening_stock", "batch_no", "expiry_date"]
PRODUCT_REQUIRED = ["category", "sku", "name", "unit"]
VENDOR_COLUMNS = ["name", "gstin", "address", "city", "pincode", "phone", "email",
                  "contact_person", "drug_licence_no", "is_composition"]
HSN_COLUMNS = ["code", "description", "gst_rate"]
STAFF_COLUMNS = ["username", "full_name", "email", "phone", "access"]
MAX_ROWS = 5000

RENAMED_COLUMNS = {
    "segment": "category",
    "replacement_quantity": "reorder_level",
    "supplier": "(not used: the vendor is recorded on the bill, not the product)",
    "vendor": "(not used: the vendor is recorded on the bill, not the product)",
    "vendor_gstin": "(not used: the vendor is recorded on the bill, not the product)",
    "ordered_on": "(not used: batches record when stock arrives)",
    "segments": "access",
    "role": "access",
    "hsn_code": "hsn",
}


class Report:
    """What an import would do, or did."""

    def __init__(self):
        self.created, self.skipped, self.errors = [], [], []

    @property
    def ok(self):
        return not self.errors

    def to_dict(self):
        return {"ok": self.ok, "created": self.created, "skipped": self.skipped,
                "errors": [{"row": row, "problem": problem} for row, problem in self.errors]}


def read_csv(text, required, allowed):
    """Rows as dicts, with the header checked. Unknown columns are refused, so a
    spelling mistake can never be silently ignored."""
    if not text or not text.strip():
        raise WorkflowError("The file is empty.")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        raise WorkflowError("The file has a header but no rows.")
    if len(rows) > MAX_ROWS:
        raise WorkflowError(f"The file has more than {MAX_ROWS} rows.")
    header = {(name or "").strip().lower() for name in rows[0].keys()}
    unknown = sorted(name for name in header if name and name not in allowed)
    if unknown:
        hints = [f"'{name}' — use '{RENAMED_COLUMNS[name]}'" for name in unknown
                 if name in RENAMED_COLUMNS]
        message = "The file has columns this import does not use: " + ", ".join(unknown) + "."
        if hints:
            message += " " + " ".join(hints) + "."
        raise WorkflowError(message + " Download the template and copy your data into it.")
    missing = [name for name in required if name not in header]
    if missing:
        raise WorkflowError("The file is missing these columns: " + ", ".join(missing))
    return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in rows]


# --------------------------------------------------------------------------- products

def import_products(person, text, dry_run=False):
    rows = read_csv(text, PRODUCT_REQUIRED, set(PRODUCT_COLUMNS))
    report = Report()

    categories = {}
    for category in Category.query.filter_by(is_active=True):
        categories[category.name.lower()] = category
        categories[category.code.lower()] = category
    units = {unit.name.lower(): unit for unit in Unit.query.filter_by(is_active=True)}
    units.update({unit.code.lower(): unit for unit in Unit.query.filter_by(is_active=True)})
    hsn_codes = {row.code: row for row in HsnCode.query.filter_by(is_active=True)}

    products, seen_sku, names = {}, {}, {}
    for number, row in enumerate(rows, start=2):
        sku = (row.get("sku") or "").upper()
        if not sku:
            report.errors.append((number, "sku is required"))
            continue

        category = categories.get((row.get("category") or "").lower())
        if not category:
            report.errors.append((number, f"category '{row.get('category')}' is not on the list"))
            continue
        unit = units.get((row.get("unit") or "").lower())
        if not unit:
            report.errors.append((number, f"unit '{row.get('unit')}' is not on the list "
                                          "(add it under Lists → Units)"))
            continue

        hsn = None
        if row.get("hsn"):
            hsn = hsn_codes.get("".join(ch for ch in row["hsn"] if ch.isdigit()))
            if not hsn:
                report.errors.append((number, f"HSN {row['hsn']} is not on the list"))
                continue

        try:
            opening = whole_number(row.get("opening_stock"), "opening_stock")
            reorder = whole_number(row.get("reorder_level"), "reorder_level")
            pack_size = whole_number(row.get("pack_size"), "pack_size", default=0)
            expiry = parse_date(row.get("expiry_date"), "expiry_date", end_of_month=True)
        except WorkflowError as err:
            report.errors.append((number, err.message))
            continue

        name = clean_text(row.get("name"), 120)
        if not name:
            report.errors.append((number, "name is required"))
            continue
        key = (category.id, name.lower())
        existing_sku = Product.query.filter_by(sku=sku).first()
        clash = Product.query.filter_by(category_id=category.id, name=name).first()
        if clash and (not existing_sku or clash.id != existing_sku.id):
            report.errors.append((number, f"a product named '{name}' already exists in {category.name}"))
            continue
        if key in names and names[key] != sku:
            report.errors.append((number, f"'{name}' is already on row {names[key]} in the same category"))
            continue
        names[key] = sku

        batch = {"quantity": opening, "batch_no": row.get("batch_no", ""), "expiry": expiry,
                 "row": number}
        if sku in products:
            products[sku]["batches"].append(batch)            # another batch of the same product
            continue
        if Product.query.filter_by(sku=sku).first():
            if sku not in seen_sku:
                seen_sku[sku] = True
                report.skipped.append(f"{sku} (already exists)")
            continue
        products[sku] = {
            "row": number,
            "data": {"sku": sku, "name": name, "category_id": category.id, "unit_id": unit.id,
                     "hsn_id": hsn.id if hsn else None,
                     "brand": clean_text(row.get("brand"), 80), "reorder_level": reorder,
                     "pack_size": pack_size or None,
                     "pack_label": clean_text(row.get("pack_label"), 40)},
            "batches": [batch],
        }

    if not report.ok or dry_run:
        report.created = [p["data"]["sku"] for p in products.values()] if report.ok else []
        return report

    for sku, entry in products.items():
        product = catalogue.create_product(person, entry["data"])
        from . import stock as stock_service
        from ..models import StockMovement, utcnow
        today = utcnow().date()
        for batch in entry["batches"]:
            if not batch["quantity"]:
                continue
            target = stock_service.batch_for(product, batch["batch_no"], batch["expiry"], today)
            stock_service.receive_into_batch(product, batch["quantity"], target,
                                             dict(reason=StockMovement.RESTOCK, created_by=person.id,
                                                  note="Opening stock"))
        report.created.append(sku)
    return report


def export_products(visible_categories=None):
    """One row per batch, so the file can be imported back as it is."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=PRODUCT_COLUMNS)
    writer.writeheader()
    query = Product.query.filter_by(is_active=True)
    if visible_categories is not None:
        query = query.filter(Product.category_id.in_(visible_categories or [0]))
    for product in query.order_by(Product.category_id, Product.name):
        base = {
            "category": product.category.name if product.category else "",
            "sku": product.sku, "name": product.name, "unit": product.unit,
            "reorder_level": product.reorder_level, "brand": product.brand or "",
            "hsn": product.hsn.code if product.hsn else "",
            "pack_size": product.pack_size or "", "pack_label": product.pack_label or "",
        }
        batches = product.batches_in_stock()
        if not batches:
            writer.writerow({**base, "opening_stock": 0, "batch_no": "", "expiry_date": ""})
            continue
        for batch in batches:
            writer.writerow({**base, "opening_stock": batch.quantity,
                             "batch_no": batch.batch_no or "",
                             "expiry_date": batch.expiry_date.isoformat() if batch.expiry_date else ""})
    return out.getvalue()


# --------------------------------------------------------------------------- vendors and HSN codes

def import_vendors(person, text, dry_run=False):
    rows = read_csv(text, ["name"], set(VENDOR_COLUMNS))
    report = Report()
    pending = []
    for number, row in enumerate(rows, start=2):
        try:
            values = masters._vendor_values(row)
            masters._check_vendor_duplicates(values)
        except WorkflowError as err:
            if "already" in err.message:
                report.skipped.append(f"{row.get('name')} ({err.message})")
            else:
                report.errors.append((number, err.message))
            continue
        if any(entry[0]["name_key"] == values["name_key"] and entry[0]["gstin"] == values["gstin"]
               for entry in pending):
            report.skipped.append(f"{row.get('name')} (twice in the file)")
            continue
        pending.append((values, row.get("contact_person")))

    if not report.ok or dry_run:
        report.created = [values["name"] for values, _ in pending] if report.ok else []
        return report

    for values, contact in pending:
        vendor = masters.create_vendor(person, {**values, "is_composition": values["is_composition"]})
        if contact:
            masters.add_contact(person, vendor.id, {"name": contact}, quiet=True)
        report.created.append(vendor.name)
    return report


def import_hsn(person, text, dry_run=False):
    rows = read_csv(text, HSN_COLUMNS, set(HSN_COLUMNS))
    report = Report()
    pending = []
    for number, row in enumerate(rows, start=2):
        try:
            values = masters._hsn_values(row)
        except WorkflowError as err:
            report.errors.append((number, err.message))
            continue
        if HsnCode.query.filter_by(code=values["code"]).first() or \
                any(entry["code"] == values["code"] for entry in pending):
            report.skipped.append(f"{values['code']} (already on the list)")
            continue
        pending.append(values)

    if not report.ok or dry_run:
        report.created = [values["code"] for values in pending] if report.ok else []
        return report
    for values in pending:
        masters.create_hsn(person, values)
        report.created.append(values["code"])
    return report


# --------------------------------------------------------------------------- accounts

def parse_access(text_value, categories):
    """'MAKER: Pantry; CHECKER: Pantry; STORE' -> access rows."""
    entries = []
    for part in str(text_value or "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        level, _, category_name = part.partition(":")
        level = level.strip().upper()
        category_name = category_name.strip()
        category = None
        if category_name and category_name.lower() not in ("all", "all categories"):
            category = categories.get(category_name.lower())
            if not category:
                raise WorkflowError(f"category '{category_name}' is not on the list")
        entries.append({"level": level, "category_id": category.id if category else None})
    if not entries:
        raise WorkflowError("access is required, e.g. 'MAKER: Pantry' or 'STORE'")
    return entries


def import_staff(actor, text, dry_run=False):
    rows = read_csv(text, ["username", "full_name", "access"], set(STAFF_COLUMNS))
    report = Report()
    categories = {category.name.lower(): category for category in Category.query.filter_by(is_active=True)}
    categories.update({category.code.lower(): category
                       for category in Category.query.filter_by(is_active=True)})
    pending, passwords = [], {}

    for number, row in enumerate(rows, start=2):
        username = (row.get("username") or "").strip().lower()
        if Staff.query.filter_by(username=username).first():
            report.skipped.append(f"{username} (already exists)")
            continue
        if any(entry["username"] == username for entry in pending):
            report.skipped.append(f"{username} (twice in the file)")
            continue
        try:
            access = parse_access(row.get("access"), categories)
            accounts._access_rows(access)
        except WorkflowError as err:
            report.errors.append((number, err.message))
            continue
        pending.append({"username": username, "full_name": row.get("full_name"),
                        "email": row.get("email") or None, "phone": row.get("phone") or None,
                        "access": access})

    if not report.ok or dry_run:
        report.created = [entry["username"] for entry in pending] if report.ok else []
        return report, passwords

    for entry in pending:
        person, password = accounts.create_staff(actor, entry)
        passwords[person.username] = password
        report.created.append(person.username)
    return report, passwords
