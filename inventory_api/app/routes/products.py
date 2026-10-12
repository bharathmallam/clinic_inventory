"""Products: the inventory list, what a requester may order, and stock changes."""
from flask import Blueprint, Response, g, jsonify, request

from ..auth import login_required
from ..extensions import db
from ..models import Category, Product, StockMovement
from ..services import catalogue, stock, transfer
from ..services.core import atomic

bp = Blueprint("products", __name__, url_prefix="/api")

PAGE_SIZE, MAX_PAGE = 10, 100


def _body():
    return request.get_json(silent=True) or {}


def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _visible_or_403():
    if not g.user.sees_inventory:
        return jsonify(error="You don't have access to the inventory."), 403
    return None


def _in_my_categories(query):
    visible = g.user.visible_categories()
    if visible is None:
        return query
    return query.filter(Product.category_id.in_(visible or [0]))


@bp.get("/products/search")
@login_required("request.create")
def search_products():
    """What a requester sees: the products they may order, with only the
    quantity available to order — never stock levels, costs or prices.

    With no search text it lists everything they may order, a page at a time."""
    query = Product.query.filter(Product.is_active.is_(True))
    allowed = g.user.orderable_categories()
    if allowed is not None:
        query = query.filter(Product.category_id.in_(allowed or [0]))
    if category := request.args.get("category_id"):
        query = query.filter(Product.category_id == int(category))
    if text := (request.args.get("q") or "").strip():
        like = f"%{text}%"
        query = query.filter(db.or_(Product.name.ilike(like), Product.sku.ilike(like)))

    per_page = max(1, min(_int_arg("per_page", PAGE_SIZE), MAX_PAGE))
    total = query.order_by(None).count()
    pages = max(1, -(-total // per_page))
    page = max(1, min(_int_arg("page", 1), pages))
    found = query.order_by(Product.name).offset((page - 1) * per_page).limit(per_page).all()
    return jsonify(
        products=[{
            "id": p.id, "sku": p.sku, "name": p.name, "unit": p.unit,
            "category": {"id": p.category_id, "name": p.category.name},
            "pack_size": p.pack_size, "pack_label": p.pack_label,
            "available": max(stock.available_quantity(p), 0),
        } for p in found],
        total=total, page=page, pages=pages, per_page=per_page)


@bp.get("/products")
@login_required()
def list_products():
    if (denied := _visible_or_403()):
        return denied
    query = _in_my_categories(Product.query)
    if category := request.args.get("category_id"):
        query = query.filter(Product.category_id == int(category))
    if request.args.get("include_inactive") != "1":
        query = query.filter(Product.is_active.is_(True))
    if text := (request.args.get("q") or "").strip():
        like = f"%{text}%"
        query = query.filter(db.or_(Product.name.ilike(like), Product.sku.ilike(like)))

    products = query.order_by(Product.category_id, Product.name).all()
    reserved = stock.reserved_map([p.id for p in products])
    out = []
    for product in products:
        data = product.to_dict(with_batches=True)
        data["reserved"] = reserved.get(product.id, 0)
        data["available"] = max(stock.usable_quantity(product) - data["reserved"], 0)
        out.append(data)
    return jsonify(products=out)


@bp.post("/products")
@login_required("product.manage")
def create_product():
    with atomic():
        product = catalogue.create_product(g.user, _body())
        return jsonify(product=product.to_dict(with_batches=True),
                       message=f"{product.name} added."), 201


@bp.patch("/products/<int:product_id>")
@login_required("product.manage")
def update_product(product_id):
    with atomic():
        product = catalogue.update_product(g.user, product_id, _body())
        return jsonify(product=product.to_dict(with_batches=True),
                       message=f"{product.name} updated.")


@bp.post("/products/<int:product_id>/stock")
@login_required("stock.manage")
def change_stock(product_id):
    data = _body()
    with atomic():
        moved = stock.change_stock(
            g.user, product_id,
            quantity_change=data.get("quantity_change"),
            reason=(data.get("reason") or StockMovement.RESTOCK).upper(),
            note=data.get("note"),
            batch_no=data.get("batch_no"),
            expiry_date=data.get("expiry_date"),
            batch_id=data.get("batch_id"),
        )
        product = db.session.get(Product, product_id)
        return jsonify(moved=[{"batch_no": batch.batch_no, "quantity": quantity}
                              for batch, quantity in moved],
                       product=product.to_dict(with_batches=True),
                       message=f"{product.name}: stock is now {product.current_stock} {product.unit}.")


@bp.get("/products/<int:product_id>/movements")
@login_required()
def movements(product_id):
    if (denied := _visible_or_403()):
        return denied
    rows = (StockMovement.query.filter_by(product_id=product_id)
            .order_by(StockMovement.id.desc()).limit(200).all())
    return jsonify(movements=[row.to_dict() for row in rows])


@bp.get("/products/export")
@login_required()
def export_products():
    if (denied := _visible_or_403()):
        return denied
    return Response(transfer.export_products(g.user.visible_categories()), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=products.csv"})


@bp.post("/products/import")
@login_required("product.manage")
def import_products():
    data = _body()
    dry_run = bool(data.get("dry_run"))
    with atomic():
        report = transfer.import_products(g.user, data.get("csv") or "", dry_run=dry_run)
        if not report.ok:
            db.session.rollback()
        return jsonify(report=report.to_dict(), dry_run=dry_run), (200 if report.ok else 400)
