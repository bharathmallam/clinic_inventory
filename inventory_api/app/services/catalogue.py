"""Products: adding, editing and retiring them.

A product's opening stock is not a column — it goes in as a ledger row like
every other movement, so the stock figure always has a reason behind it."""
import re

from ..extensions import db
from ..models import Category, HsnCode, Product, StockMovement, Unit, utcnow
from . import audit, stock
from .core import WorkflowError, clean_text, parse_date, whole_number


def _pick(model, value, label, required=False):
    """Find a row of a list by id, and refuse anything that isn't on it."""
    if value in (None, "", 0, "0"):
        if required:
            raise WorkflowError(f"{label} is required.")
        return None
    row = db.session.get(model, int(value)) if str(value).isdigit() else None
    if row is None or not getattr(row, "is_active", True):
        raise WorkflowError(f"Choose a {label.lower()} from the list.")
    return row


def _values(data, person, product=None):
    values = {}
    if product is None or "name" in data:
        name = clean_text(data.get("name"), 120)
        if not name:
            raise WorkflowError("Product name is required.")
        values["name"] = name
    if product is None or "category_id" in data:
        values["category_id"] = _pick(Category, data.get("category_id"), "Category", required=True).id
    if product is None or "unit_id" in data:
        values["unit_id"] = _pick(Unit, data.get("unit_id"), "Unit", required=True).id
    if product is None or "hsn_id" in data:
        hsn = _pick(HsnCode, data.get("hsn_id"), "HSN code")
        values["hsn_id"] = hsn.id if hsn else None
    if product is None or "brand" in data:
        values["brand"] = clean_text(data.get("brand"), 80)
    if product is None or "reorder_level" in data:
        values["reorder_level"] = whole_number(data.get("reorder_level"), "Replacement quantity")
    if product is None or "pack_size" in data:
        pack = whole_number(data.get("pack_size"), "Pack size", default=0)
        values["pack_size"] = pack or None
    if product is None or "pack_label" in data:
        values["pack_label"] = clean_text(data.get("pack_label"), 40)
    return values


def category_prefix(category):
    """CLINIC_CONSUMABLES -> CC, PANTRY -> PA: the start of its product codes."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", category.code or category.name) if w]
    letters = "".join(w[0] for w in words)[:3] if len(words) > 1 else (words[0][:2] if words else "")
    return (letters or "PR").upper()


def next_sku(category):
    """The next free code for this category: CC-0001, CC-0002, …

    Generated rather than typed, so two people cannot invent the same code and
    nobody has to think one up."""
    prefix = category_prefix(category)
    used = {row[0] for row in db.session.query(Product.sku)
            .filter(Product.sku.like(f"{prefix}-%")).all()}
    # Only codes of the generated shape count, so the clinic's own CC-COT-500
    # does not push the next one to CC-0501.
    shape = re.compile(rf"^{re.escape(prefix)}-(\d{{4,}})$")
    highest = 0
    for sku in used:
        found = shape.match(sku)
        if found:
            highest = max(highest, int(found.group(1)))
    for number in range(highest + 1, highest + 1000):
        candidate = f"{prefix}-{number:04d}"
        if candidate not in used and not Product.query.filter_by(sku=candidate).first():
            return candidate
    raise WorkflowError("Could not make a product code. Tell the Administrator.")


def create_product(person, data):
    if not person.can("product.manage"):
        raise WorkflowError("Your access does not allow adding products.", 403)
    # A code may be given (the clinic's own codes come in through the import);
    # left out, the app makes the next one for that category.
    sku = (clean_text(data.get("sku"), 40) or "").upper()
    if sku and Product.query.filter_by(sku=sku).first():
        raise WorkflowError(f"Product code {sku} is already used.", 409)

    values = _values(data, person)
    _check_allowed_category(person, values["category_id"])
    if not sku:
        sku = next_sku(db.session.get(Category, values["category_id"]))
    if Product.query.filter_by(category_id=values["category_id"], name=values["name"]).first():
        raise WorkflowError(f"{values['name']} already exists in that category.", 409)

    product = Product(sku=sku, created_by=person.id, updated_by=person.id, **values)
    db.session.add(product)
    db.session.flush()

    audit.record("PRODUCT_CREATED", person, entity="product", entity_id=product.id,
                 details=product.to_dict())

    opening = whole_number(data.get("opening_stock"), "Opening stock")
    if opening:
        if not person.can("stock.manage"):
            raise WorkflowError("Your access does not allow adding stock.", 403)
        batch = stock.batch_for(product, data.get("batch_no"),
                                parse_date(data.get("expiry_date"), "Expiry date", end_of_month=True),
                                utcnow().date())
        stock.receive_into_batch(product, opening, batch,
                                 dict(reason=StockMovement.RESTOCK, created_by=person.id,
                                      note="Opening stock"))
    return product


def update_product(person, product_id, data):
    if not person.can("product.manage"):
        raise WorkflowError("Your access does not allow editing products.", 403)
    product = db.session.query(Product).filter_by(id=product_id).with_for_update().first()
    if not product:
        raise WorkflowError("Product not found.", 404)
    _check_allowed_category(person, product.category_id)
    before = product.to_dict()

    values = _values(data, person, product)
    if "category_id" in values and values["category_id"] != product.category_id:
        _check_allowed_category(person, values["category_id"])
    name = values.get("name", product.name)
    category_id = values.get("category_id", product.category_id)
    clash = Product.query.filter(Product.id != product.id, Product.category_id == category_id,
                                 Product.name == name).first()
    if clash:
        raise WorkflowError(f"{name} already exists in that category.", 409)

    for key, value in values.items():
        setattr(product, key, value)
    if "is_active" in data:
        active = str(data["is_active"]).lower() in ("1", "true", "yes", "y", "on")
        if not active and not person.can("product.deactivate"):
            raise WorkflowError("Only a checker can make a product unavailable to order.", 403)
        # Stock left on the shelf is no reason to refuse: an unavailable product
        # keeps its stock, its batches and its place in the reports — it simply
        # stops appearing on New request.
        product.is_active = active
    product.updated_by = person.id
    db.session.flush()
    audit.record("PRODUCT_UPDATED", person, entity="product", entity_id=product.id,
                 details={"before": before, "after": product.to_dict()})
    return product


def _check_allowed_category(person, category_id):
    """A checker manages only their own categories; STORE manages all of theirs."""
    from ..models import AccessLevel
    for level in (AccessLevel.CHECKER, AccessLevel.STORE):
        held = person.categories_for(level)
        if held is None or (held and category_id in held):
            return
    category = db.session.get(Category, category_id)
    raise WorkflowError(f"You are not set up to manage {category.name if category else 'that category'}.",
                        403)
