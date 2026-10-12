"""Reports, all calculated from the stock ledger (nothing extra is stored).

Monthly usage replaces the 'CONSUMABLE STOCK' workbook: per product, opening
stock, what came in, what was handed out on each day 1-31, adjustments and
closing stock."""
import calendar
import csv
import io
from collections import defaultdict
from datetime import date, datetime

from ..extensions import db
from ..models import Category, Product, StockMovement
from .core import WorkflowError


def parse_month(value):
    try:
        year, month = (int(part) for part in str(value).split("-"))
        return date(year, month, 1)
    except (ValueError, TypeError):
        raise WorkflowError("Month must look like 2026-09.")


def _products(category_id=None, visible=None):
    query = Product.query
    if category_id:
        query = query.filter(Product.category_id == int(category_id))
    if visible is not None:
        query = query.filter(Product.category_id.in_(visible or [0]))
    return query.order_by(Product.category_id, Product.name).all()


def monthly_usage(month_value, category_id=None, visible=None):
    first = parse_month(month_value)
    days = calendar.monthrange(first.year, first.month)[1]
    start = datetime(first.year, first.month, 1)
    end = datetime(first.year + (first.month == 12), first.month % 12 + 1, 1)
    products = _products(category_id, visible)
    ids = [p.id for p in products]

    opening = dict(db.session.query(StockMovement.product_id,
                                    db.func.sum(StockMovement.quantity_change))
                   .filter(StockMovement.product_id.in_(ids), StockMovement.created_dt < start)
                   .group_by(StockMovement.product_id))
    moves = StockMovement.query.filter(StockMovement.product_id.in_(ids),
                                       StockMovement.created_dt >= start,
                                       StockMovement.created_dt < end).all()
    per_product = defaultdict(lambda: {"received": 0, "adjusted": 0, "issued": [0] * days})
    for movement in moves:
        row = per_product[movement.product_id]
        if movement.reason == StockMovement.DELIVERY:
            row["issued"][movement.created_dt.day - 1] += -movement.quantity_change
        elif movement.reason == StockMovement.RESTOCK:
            row["received"] += movement.quantity_change
        else:
            row["adjusted"] += movement.quantity_change

    rows = []
    for product in products:
        data = per_product[product.id]
        open_qty = int(opening.get(product.id) or 0)
        issued_total = sum(data["issued"])
        closing = open_qty + data["received"] + data["adjusted"] - issued_total
        if not (open_qty or data["received"] or data["adjusted"] or issued_total or product.is_active):
            continue
        rows.append({"product_id": product.id, "sku": product.sku, "name": product.name,
                     "unit": product.unit,
                     "category": product.category.name if product.category else "",
                     "opening": open_qty, "received": data["received"], "adjusted": data["adjusted"],
                     "issued": data["issued"], "issued_total": issued_total, "closing": closing})
    return {"month": f"{first:%Y-%m}", "days": days, "rows": rows}


def monthly_usage_csv(report):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Category", "SKU", "Product", "Unit", "Opening", "Received", "Adjusted"]
                    + [str(day) for day in range(1, report["days"] + 1)] + ["Issued", "Closing"])
    for row in report["rows"]:
        writer.writerow([row["category"], row["sku"], row["name"], row["unit"], row["opening"],
                         row["received"], row["adjusted"]]
                        + [qty or "" for qty in row["issued"]] + [row["issued_total"], row["closing"]])
    return out.getvalue()


def stock_value(category_id=None, visible=None):
    """Value of stock on hand at bill cost (after discount, before GST). Batches
    that never came in on a bill have no cost and are counted separately."""
    rows, total, uncosted = [], 0.0, 0
    for product in _products(category_id, visible):
        value, qty_uncosted = 0.0, 0
        for batch in product.batches_in_stock():
            if batch.unit_cost is None:
                qty_uncosted += batch.quantity
            else:
                value += batch.quantity * batch.unit_cost
        if product.current_stock:
            rows.append({"product_id": product.id, "sku": product.sku, "name": product.name,
                         "category": product.category.name if product.category else "",
                         "quantity": product.current_stock, "unit": product.unit,
                         "value": round(value, 2), "uncosted_quantity": qty_uncosted})
        total += value
        uncosted += qty_uncosted
    rows.sort(key=lambda row: -row["value"])
    return {"total": round(total, 2), "uncosted_quantity": uncosted, "rows": rows}
