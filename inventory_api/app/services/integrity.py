"""Database-level protections that plain constraints cannot express, and a
consistency check that can be run at any time."""
from collections import defaultdict

from sqlalchemy import func, text

from ..extensions import db
from ..models import AccessLevel, Batch, Product, Request, RequestItem, Staff, StockMovement

# Tables whose rows must never change once written.
READ_ONLY = {
    "audit_log": ("UPDATE", "DELETE"),
    "stock_movements": ("UPDATE", "DELETE"),
    "request_item_history": ("UPDATE", "DELETE"),
    "orders": ("UPDATE", "DELETE"),
    "order_acknowledgements": ("UPDATE", "DELETE"),
}

STOCK_GUARD = "trg_products_stock_via_ledger"
NEW_PRODUCT_GUARD = "trg_products_start_at_zero"
_LEDGER_MATCHES = (
    "EXISTS (SELECT 1 FROM stock_movements m WHERE m.product_id = NEW.id"
    " AND m.id = (SELECT MAX(m2.id) FROM stock_movements m2 WHERE m2.product_id = NEW.id)"
    " AND m.balance_after = NEW.current_stock"
    " AND m.quantity_change = NEW.current_stock - OLD.current_stock)"
)


def _stock_guard_sql(dialect):
    """Single source of truth for stock: products.current_stock may only change
    to match the ledger row just written for it, and new products start at 0."""
    msg = "products.current_stock can only change through a stock_movements row"
    start = "new products start at 0; add stock with a restock"
    if dialect == "mysql":
        return [
            f"DROP TRIGGER IF EXISTS {STOCK_GUARD}",
            f"CREATE TRIGGER {STOCK_GUARD} BEFORE UPDATE ON products FOR EACH ROW BEGIN "
            f"IF NEW.current_stock <> OLD.current_stock AND NOT {_LEDGER_MATCHES} THEN "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{msg}'; END IF; END",
            f"DROP TRIGGER IF EXISTS {NEW_PRODUCT_GUARD}",
            f"CREATE TRIGGER {NEW_PRODUCT_GUARD} BEFORE INSERT ON products FOR EACH ROW BEGIN "
            f"IF NEW.current_stock <> 0 THEN "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{start}'; END IF; END",
        ]
    return [
        f"DROP TRIGGER IF EXISTS {STOCK_GUARD}",
        f"CREATE TRIGGER {STOCK_GUARD} BEFORE UPDATE OF current_stock ON products "
        f"WHEN NEW.current_stock <> OLD.current_stock AND NOT {_LEDGER_MATCHES} "
        f"BEGIN SELECT RAISE(ABORT, '{msg}'); END",
        f"DROP TRIGGER IF EXISTS {NEW_PRODUCT_GUARD}",
        f"CREATE TRIGGER {NEW_PRODUCT_GUARD} BEFORE INSERT ON products WHEN NEW.current_stock <> 0 "
        f"BEGIN SELECT RAISE(ABORT, '{start}'); END",
    ]


def _trigger_sql(dialect, table, event):
    name = f"trg_{table}_no_{event.lower()}"
    message = f"{table} is read-only: {event} is not allowed"
    if dialect == "mysql":
        return [
            f"DROP TRIGGER IF EXISTS {name}",
            f"CREATE TRIGGER {name} BEFORE {event} ON {table} FOR EACH ROW "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'",
        ]
    return [
        f"DROP TRIGGER IF EXISTS {name}",
        f"CREATE TRIGGER {name} BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT, '{message}'); END",
    ]


def trigger_statements(dialect):
    return [sql for table, events in READ_ONLY.items() for event in events
            for sql in _trigger_sql(dialect, table, event)] + _stock_guard_sql(dialect)


TRIGGER_COUNT = sum(len(v) for v in READ_ONLY.values()) + 2


def install_triggers():
    dialect = db.engine.dialect.name
    with db.engine.begin() as conn:
        for sql in trigger_statements(dialect):
            conn.execute(text(sql))


def check():
    """Return a list of problems (empty list = database is consistent)."""
    problems = []

    # 1. products.current_stock equals the sum of its stock ledger.
    ledger = dict(db.session.query(StockMovement.product_id, func.sum(StockMovement.quantity_change))
                  .group_by(StockMovement.product_id))
    for product in Product.query:
        expected = int(ledger.get(product.id) or 0)
        if product.current_stock != expected:
            problems.append(f"{product.sku}: current_stock {product.current_stock} "
                            f"but the ledger says {expected}")

    # 2. No batch below zero, and the batches add up to the product's stock.
    batch_sums = defaultdict(int)
    for batch in Batch.query:
        if batch.quantity < 0:
            problems.append(f"{batch.product.sku} {batch.label}: quantity {batch.quantity} is below zero")
        batch_sums[batch.product_id] += batch.quantity
    for product in Product.query:
        total = batch_sums.get(product.id, 0)
        if total != product.current_stock:
            problems.append(f"{product.sku}: batches add up to {total}, "
                            f"stock says {product.current_stock}")

    # 3. A finished line carries who decided or delivered it; a pending one doesn't.
    for item in RequestItem.query:
        ref = f"{item.request.request_no}-{item.line_no}"
        if item.status in (RequestItem.APPROVED, RequestItem.DELIVERED) and not item.decided_by:
            problems.append(f"{ref} is {item.status} with nobody recorded as approving it")
        if item.status == RequestItem.REJECTED and not item.reason:
            problems.append(f"{ref} was rejected with no reason")
        if item.status == RequestItem.DELIVERED and not item.delivered_by:
            problems.append(f"{ref} is delivered with nobody recorded as delivering it")

    # 4. Every delivered line moved stock exactly once.
    delivered = {item.id for item in RequestItem.query.filter_by(status=RequestItem.DELIVERED)}
    moved = defaultdict(int)
    for (item_id,) in db.session.query(StockMovement.request_item_id).filter(
            StockMovement.request_item_id.isnot(None)):
        moved[item_id] += 1
    for item_id in delivered:
        if not moved.get(item_id):
            problems.append(f"request line {item_id} is delivered but moved no stock")

    # 5. Every active account has at least one access row, and makers/checkers have a category.
    for person in Staff.query.filter_by(is_active=True):
        if not person.access:
            problems.append(f"{person.username}: account has no access at all")
        for row in person.access:
            if row.level and row.level.needs_category and row.category_id is None:
                problems.append(f"{person.username}: {row.level.code} access needs a category")

    # 6. The four access levels exist.
    have = {level.code for level in AccessLevel.query}
    missing = set(AccessLevel.CODES) - have
    if missing:
        problems.append("missing access levels: " + ", ".join(sorted(missing)))

    return problems
