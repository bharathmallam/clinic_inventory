"""Database model: the single source of truth for the schema.

database/schema.sql is generated from this file (`python manage.py export-schema`)
and migrations are generated from it (`flask db migrate`). Do not hand-edit either.

Design rules followed throughout:
- Every table: surrogate primary key `id`, InnoDB (transactions + foreign keys), utf8mb4.
- Every relationship is a real FOREIGN KEY; master data is never hard-deleted
  (deactivate instead), so FKs use the default RESTRICT behaviour.
- Business rules the database can check are CHECK constraints, not only Python.
- A table that can be updated carries created_by/created_dt and updated_by/updated_dt.
  A write-once table carries only created_by/created_dt: the database refuses updates
  to it, which is a stronger guarantee than a column saying who changed it.
- Nothing derived is stored: bill totals, batch quantities and a request's overall
  state are worked out from the rows they come from. The one exception is
  products.current_stock, which a trigger ties to the stock ledger.
- All timestamps are UTC.
"""
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import column_property, declared_attr

from .extensions import db

MAX_QUANTITY = 100000


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt):
    return dt.isoformat() + "Z" if dt else None


def table_args(*constraints, comment):
    return (*constraints, {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
        "comment": comment,
    })


def big_id():
    # BIGINT on MySQL; SQLite only auto-increments INTEGER primary keys.
    return db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)


def money(precision=12, scale=2):
    return db.Numeric(precision, scale)


def _brief(person):
    return {"id": person.id, "full_name": person.full_name} if person else None


class CreatedMixin:
    """For write-once tables: who wrote the row, and when."""

    created_dt = db.Column(db.DateTime, nullable=False, default=utcnow, comment="UTC")

    @declared_attr
    def created_by(cls):
        return db.Column(db.Integer, db.ForeignKey("staff.id"),
                         comment="empty only for rows the system seeds")

    @declared_attr
    def creator(cls):
        return db.relationship("Staff", foreign_keys=[cls.created_by], lazy="joined")


class AuditMixin(CreatedMixin):
    """For tables people edit: who wrote it, who last changed it, and when."""

    updated_dt = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="UTC")

    @declared_attr
    def updated_by(cls):
        return db.Column(db.Integer, db.ForeignKey("staff.id"))

    @declared_attr
    def updater(cls):
        return db.relationship("Staff", foreign_keys=[cls.updated_by], lazy="joined")


# =========================================================================== the clinic and its lists

class Organisation(AuditMixin, db.Model):
    """The clinic itself: one row, id 1."""

    __tablename__ = "organisation"
    __table_args__ = table_args(
        db.CheckConstraint("id = 1", name="single_row"),
        db.CheckConstraint("gstin IS NULL OR LENGTH(gstin) = 15", name="gstin_length"),
        comment="The clinic's own details (single row).",
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=False)
    name = db.Column(db.String(120), nullable=False)
    gstin = db.Column(db.String(15), comment="The clinic's GSTIN; its first two digits give the state")
    state_code = db.Column(db.String(2), nullable=False, default="36", comment="Used when no GSTIN is set")

    @property
    def effective_state_code(self):
        return (self.gstin or "")[:2] or self.state_code

    def to_dict(self):
        from .gst import state_name
        return {"name": self.name, "gstin": self.gstin, "state_code": self.effective_state_code,
                "state": state_name(self.effective_state_code)}


class Category(AuditMixin, db.Model):
    """A segment of the clinic's stock. Categories are data, not code: a new one
    can be added at any time, and it brings its own access and its own products."""

    __tablename__ = "categories"
    __table_args__ = table_args(
        db.UniqueConstraint("code", name="uq_categories_code"),
        db.UniqueConstraint("name", name="uq_categories_name"),
        db.CheckConstraint("code = UPPER(code) AND code <> ''", name="code_shape"),
        comment="Stock categories (Clinic Consumables, Pantry, …). New ones may be added.",
    )

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), nullable=False, comment="CLINIC_CONSUMABLES, PANTRY, …")
    name = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(255))
    tracks_expiry = db.Column(db.Boolean, nullable=False, default=True,
                              comment="Do its products carry expiry dates?")
    needs_hsn = db.Column(db.Boolean, nullable=False, default=True,
                          comment="Do its purchases need an HSN code and GST rate?")
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    @property
    def in_use(self):
        """Has anything ever been put in it? Only a category nobody has used can be deleted."""
        from .models import Product, StaffAccess                      # same module, read at call time
        return bool(db.session.query(Product.id).filter_by(category_id=self.id).first()
                    or db.session.query(StaffAccess.id).filter_by(category_id=self.id).first())

    def to_dict(self):
        return {"id": self.id, "code": self.code, "name": self.name, "description": self.description,
                "tracks_expiry": self.tracks_expiry, "needs_hsn": self.needs_hsn,
                "is_active": self.is_active, "in_use": self.in_use}


class Unit(AuditMixin, db.Model):
    """How a product is counted. Stock always uses the unit it is issued in."""

    __tablename__ = "units"
    __table_args__ = table_args(
        db.UniqueConstraint("code", name="uq_units_code"),
        db.UniqueConstraint("name", name="uq_units_name"),
        comment="Units of issue (Box, Strip, Piece …).",
    )

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(40), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    @property
    def in_use(self):
        """Does any product count in it? Only an unused unit can be deleted."""
        from .models import Product
        return bool(db.session.query(Product.id).filter_by(unit_id=self.id).first())

    def to_dict(self):
        return {"id": self.id, "code": self.code, "name": self.name, "is_active": self.is_active,
                "in_use": self.in_use}


class HsnCode(AuditMixin, db.Model):
    """HSN code and the GST rate that applies to it today."""

    __tablename__ = "hsn_codes"
    __table_args__ = table_args(
        db.UniqueConstraint("code", name="uq_hsn_codes_code"),
        db.CheckConstraint("LENGTH(code) IN (4, 6, 8)", name="code_length"),
        db.CheckConstraint("gst_rate >= 0 AND gst_rate <= 40", name="gst_rate_range"),
        comment="HSN codes with their current GST rate. Bills keep the rate they were entered with.",
    )

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(8), nullable=False, comment="4, 6 or 8 digits")
    description = db.Column(db.String(200), nullable=False)
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, comment="Current GST % for this code")
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    def to_dict(self):
        return {"id": self.id, "code": self.code, "description": self.description,
                "gst_rate": float(self.gst_rate), "is_active": self.is_active}


class Vendor(AuditMixin, db.Model):
    """Who the clinic buys from. One row per GSTIN."""

    __tablename__ = "vendors"
    __table_args__ = table_args(
        db.UniqueConstraint("gstin", name="uq_vendors_gstin"),
        db.CheckConstraint("gstin IS NULL OR LENGTH(gstin) = 15", name="gstin_length"),
        db.CheckConstraint("pincode IS NULL OR LENGTH(pincode) = 6", name="pincode_length"),
        comment="Vendors. The GSTIN is verified and decides whether a bill is CGST+SGST or IGST.",
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    name_key = db.Column(db.String(150), nullable=False, index=True,
                         comment="Name without case or punctuation, to catch near-duplicates")
    gstin = db.Column(db.String(15))
    address = db.Column(db.String(300))
    city = db.Column(db.String(80))
    pincode = db.Column(db.String(6))
    phone = db.Column(db.String(40))
    email = db.Column(db.String(120))
    drug_licence_no = db.Column(db.String(60))
    is_composition = db.Column(db.Boolean, nullable=False, default=False,
                               comment="Composition-scheme vendors charge no GST")
    notes = db.Column(db.String(300))
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    contacts = db.relationship("VendorContact", back_populates="vendor", lazy="selectin",
                               order_by="VendorContact.id")

    def to_dict(self, with_contacts=False):
        from .gst import pan, state_name
        data = {
            "id": self.id, "name": self.name, "gstin": self.gstin,
            "state": state_name((self.gstin or "")[:2]) if self.gstin else None,
            "pan": pan(self.gstin) if self.gstin else None,
            "address": self.address, "city": self.city, "pincode": self.pincode,
            "phone": self.phone, "email": self.email,
            "drug_licence_no": self.drug_licence_no, "is_composition": self.is_composition,
            "notes": self.notes, "is_active": self.is_active,
        }
        if with_contacts:
            data["contacts"] = [c.to_dict() for c in self.contacts]
        return data


class VendorContact(AuditMixin, db.Model):
    """A person at the vendor: a sales rep, someone in accounts."""

    __tablename__ = "vendor_contacts"
    __table_args__ = table_args(comment="People to speak to at a vendor.")

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(60), comment="Sales, Accounts, Delivery …")
    phone = db.Column(db.String(40))
    email = db.Column(db.String(120))

    vendor = db.relationship("Vendor", back_populates="contacts", foreign_keys=[vendor_id])

    def to_dict(self):
        return {"id": self.id, "name": self.name, "role": self.role,
                "phone": self.phone, "email": self.email}


# =========================================================================== products

class Product(AuditMixin, db.Model):
    """One row per product, in every category.

    current_stock is the running total of this product's stock ledger; a trigger
    refuses any other change to it, so the number can never drift."""

    __tablename__ = "products"
    __table_args__ = table_args(
        db.UniqueConstraint("sku", name="uq_products_sku"),
        db.UniqueConstraint("category_id", "name", name="uq_products_category_id_name"),
        db.CheckConstraint("current_stock >= 0", name="stock_not_negative"),
        db.CheckConstraint("reorder_level >= 0", name="reorder_level_not_negative"),
        db.CheckConstraint("pack_size IS NULL OR pack_size > 0", name="pack_size_positive"),
        comment="All products. current_stock always equals the sum of its stock_movements.",
    )

    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(40), nullable=False, comment="The clinic's own code, e.g. CC-017")
    name = db.Column(db.String(120), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False, index=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=False,
                        comment="The unit stock is counted in: what you issue in")
    hsn_id = db.Column(db.Integer, db.ForeignKey("hsn_codes.id"))
    brand = db.Column(db.String(80))
    pack_size = db.Column(db.Integer, comment="Issue units in one pack, e.g. 10 strips in a box")
    pack_label = db.Column(db.String(40), comment="What a pack is called: Box, Carton, Case")
    reorder_level = db.Column(db.Integer, nullable=False, default=0,
                              comment="At or below this the product counts as low on stock")
    current_stock = db.Column(db.Integer, nullable=False, default=0,
                              comment="Running total of the ledger; only the ledger may change it")
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    category = db.relationship("Category", lazy="joined", foreign_keys=[category_id])
    unit_ref = db.relationship("Unit", lazy="joined", foreign_keys=[unit_id])
    hsn = db.relationship("HsnCode", lazy="joined", foreign_keys=[hsn_id])
    batches = db.relationship("Batch", lazy="selectin", back_populates="product", order_by="Batch.id")

    @property
    def unit(self):
        return self.unit_ref.name if self.unit_ref else None

    def batches_in_stock(self):
        """Batches with stock, earliest expiry first (no-expiry batches last)."""
        live = [b for b in self.batches if b.quantity > 0]
        return sorted(live, key=lambda b: (b.expiry_date is None, b.expiry_date or 0, b.id))

    @property
    def nearest_expiry(self):
        dated = [b.expiry_date for b in self.batches_in_stock() if b.expiry_date]
        return min(dated) if dated else None

    @property
    def last_received(self):
        dates = [b.received_dt for b in self.batches if b.received_dt]
        return max(dates) if dates else None

    def to_dict(self, with_batches=False):
        from .services import stock as stock_service
        expiry = self.nearest_expiry
        data = {
            "id": self.id,
            "sku": self.sku,
            "name": self.name,
            "unit": self.unit,
            "unit_id": self.unit_id,
            "category": self.category.to_dict() if self.category else None,
            "category_id": self.category_id,
            "hsn": self.hsn.to_dict() if self.hsn else None,
            "hsn_id": self.hsn_id,
            "brand": self.brand,
            "pack_size": self.pack_size,
            "pack_label": self.pack_label,
            "current_stock": self.current_stock,
            "reorder_level": self.reorder_level,
            "is_low_stock": self.current_stock <= self.reorder_level,
            "nearest_expiry": self.nearest_expiry.isoformat() if expiry else None,
            "last_received": self.last_received.isoformat() if self.last_received else None,
            "expires_soon": bool(expiry and (expiry - utcnow().date()).days <= EXPIRY_WARNING_DAYS),
            "is_active": self.is_active,
        }
        if with_batches:
            data["batches"] = [b.to_dict() for b in self.batches_in_stock()]
            data["expired_quantity"] = stock_service.expired_quantity(self)
        return data


EXPIRY_WARNING_DAYS = 30


# =========================================================================== people and access

class AccessLevel(AuditMixin, db.Model):
    """The four things a person can be. Levels are fixed; categories are not."""

    MAKER, CHECKER, STORE, ADMIN = "MAKER", "CHECKER", "STORE", "ADMIN"
    CODES = (MAKER, CHECKER, STORE, ADMIN)

    __tablename__ = "access_levels"
    __table_args__ = table_args(
        db.UniqueConstraint("code", name="uq_access_levels_code"),
        db.CheckConstraint(f"code IN {CODES}", name="known_level"),
        comment="MAKER, CHECKER, STORE, ADMIN. What each may do is in level_permissions.",
    )

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(60), nullable=False)
    description = db.Column(db.String(255))
    needs_category = db.Column(db.Boolean, nullable=False, default=False,
                               comment="MAKER and CHECKER are given per category")

    permissions = db.relationship("LevelPermission", back_populates="level", lazy="selectin",
                                  order_by="LevelPermission.permission")

    def to_dict(self):
        return {"id": self.id, "code": self.code, "name": self.name,
                "description": self.description, "needs_category": self.needs_category,
                "permissions": [p.permission for p in self.permissions]}


class LevelPermission(AuditMixin, db.Model):
    """What a level may do. A new permission is a row, not a schema change."""

    __tablename__ = "level_permissions"
    __table_args__ = table_args(
        db.UniqueConstraint("level_id", "permission", name="uq_level_permissions_level_id_permission"),
        comment="Permissions granted to each access level.",
    )

    id = db.Column(db.Integer, primary_key=True)
    level_id = db.Column(db.Integer, db.ForeignKey("access_levels.id"), nullable=False, index=True)
    permission = db.Column(db.String(40), nullable=False, comment="request.create, stock.manage, …")

    level = db.relationship("AccessLevel", back_populates="permissions", foreign_keys=[level_id])


class Staff(AuditMixin, db.Model):
    """An account."""

    __tablename__ = "staff"
    __table_args__ = table_args(
        db.UniqueConstraint("username", name="uq_staff_username"),
        db.UniqueConstraint("email", name="uq_staff_email"),
        db.CheckConstraint("username = LOWER(username) AND username <> ''", name="username_shape"),
        comment="Accounts. What each may do comes from staff_access.",
    )

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(40))
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    last_login_dt = db.Column(db.DateTime, comment="UTC")

    access = db.relationship("StaffAccess", lazy="selectin", back_populates="staff",
                             foreign_keys="StaffAccess.staff_id")

    # ---- what this person may do
    def levels(self):
        return {row.level.code for row in self.access if row.level}

    def permissions(self):
        return {p.permission for row in self.access if row.level for p in row.level.permissions}

    def can(self, permission):
        return permission in self.permissions()

    def categories_for(self, level_code):
        """Category ids this person holds `level_code` in. None = every category."""
        ids = set()
        for row in self.access:
            if row.level and row.level.code == level_code:
                if row.category_id is None:
                    return None
                ids.add(row.category_id)
        return ids

    def _category_ids(self, level_codes):
        ids = set()
        for code in level_codes:
            held = self.categories_for(code)
            if held is None:
                return None
            ids |= held
        return ids

    def orderable_categories(self):
        """Where this person may raise requests."""
        return self._category_ids([AccessLevel.MAKER, AccessLevel.CHECKER, AccessLevel.STORE]) \
            if self.can("request.create") else set()

    def visible_categories(self):
        """Where this person may see stock figures."""
        return self._category_ids([AccessLevel.CHECKER, AccessLevel.STORE, AccessLevel.ADMIN]) \
            if self.can("inventory.view") else set()

    def may_decide_category(self, category_id):
        held = self.categories_for(AccessLevel.CHECKER)
        return self.can("request.decide") and (held is None or category_id in held)

    @property
    def sees_inventory(self):
        return self.can("inventory.view")

    @property
    def sees_only_own_requests(self):
        return not self.can("request.decide") and not self.can("request.deliver") \
            and not self.can("audit.view")

    def to_dict(self, with_access=True):
        data = {
            "id": self.id, "username": self.username, "full_name": self.full_name,
            "email": self.email, "phone": self.phone, "is_active": self.is_active,
            "must_change_password": self.must_change_password,
            "last_login_dt": iso(self.last_login_dt),
            "levels": sorted(self.levels()),
            "permissions": sorted(self.permissions()),
            "sees_inventory": self.sees_inventory,
            "sees_only_own_requests": self.sees_only_own_requests,
        }
        if with_access:
            data["access"] = [row.to_dict() for row in self.access]
            orderable = self.orderable_categories()
            visible = self.visible_categories()
            data["orderable_categories"] = None if orderable is None else sorted(orderable)
            data["visible_categories"] = None if visible is None else sorted(visible)
        return data


class StaffAccess(AuditMixin, db.Model):
    """One row per person, per level, per category.

    A row with no category means every category, including ones added later."""

    __tablename__ = "staff_access"
    __table_args__ = table_args(
        db.UniqueConstraint("staff_id", "level_id", "category_id", name="uq_staff_access_row"),
        comment="Who is a maker/checker/store/admin, and in which categories.",
    )

    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False, index=True)
    level_id = db.Column(db.Integer, db.ForeignKey("access_levels.id"), nullable=False, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"),
                            comment="Empty = all categories")

    staff = db.relationship("Staff", back_populates="access", foreign_keys=[staff_id])
    level = db.relationship("AccessLevel", lazy="joined", foreign_keys=[level_id])
    category = db.relationship("Category", lazy="joined", foreign_keys=[category_id])

    def to_dict(self):
        return {"id": self.id, "level": self.level.code if self.level else None,
                "level_id": self.level_id, "category_id": self.category_id,
                "category": self.category.name if self.category else "All categories"}


# =========================================================================== requests

class Request(AuditMixin, db.Model):
    """One submission: the products sent together, with one reference number."""

    __tablename__ = "requests"
    __table_args__ = table_args(
        db.UniqueConstraint("request_no", name="uq_requests_request_no"),
        db.UniqueConstraint("short_code", name="uq_requests_short_code"),
        # Target of request_items' composite FK: every line carries the requester.
        db.UniqueConstraint("id", "requested_by", name="uq_requests_id_requested_by"),
        comment="A request as submitted: one or more product lines sent together.",
    )

    id = db.Column(db.Integer, primary_key=True)
    request_no = db.Column(db.String(24), nullable=False, comment="REQ-20260926-7F3A2C")
    short_code = db.Column(db.String(8), nullable=False,
                           comment="7F3A2C — what people see and quote; unique on its own")
    requested_by = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False, index=True)
    purpose = db.Column(db.String(255), comment="One note for the whole request")
    requested_dt = db.Column(db.DateTime, nullable=False, default=utcnow, comment="UTC")

    requester = db.relationship("Staff", lazy="joined", foreign_keys=[requested_by])
    items = db.relationship("RequestItem", back_populates="request", lazy="selectin",
                            order_by="RequestItem.line_no", foreign_keys="RequestItem.request_id")
    order = db.relationship("Order", back_populates="request", uselist=False,
                            foreign_keys="Order.request_id")

    @property
    def state(self):
        """Worked out from the lines, never stored."""
        statuses = {item.status for item in self.items}
        if statuses <= {RequestItem.REJECTED, RequestItem.CANCELLED}:
            return "CLOSED"
        if RequestItem.PENDING in statuses or RequestItem.APPROVED in statuses:
            return "IN_PROGRESS"
        return "COMPLETED"

    @property
    def is_finished(self):
        return not any(item.status in (RequestItem.PENDING, RequestItem.APPROVED)
                       for item in self.items)

    def to_dict(self, with_items=True):
        data = {
            "request_no": self.request_no,
            "code": self.short_code,
            "requested_by": _brief(self.requester),
            "purpose": self.purpose,
            "requested_dt": iso(self.requested_dt),
            "state": self.state,
            "line_count": len(self.items),
            "order_no": self.order.order_no if self.order else None,
            "order_code": self.order.short_code if self.order else None,
        }
        if with_items:
            data["items"] = [item.to_dict() for item in self.items]
        return data


class RequestItem(AuditMixin, db.Model):
    """One product line of a request, and the stage it is in.

    A line is approved in full or rejected: quantities are never edited."""

    PENDING, APPROVED, REJECTED, CANCELLED, DELIVERED = \
        "PENDING", "APPROVED", "REJECTED", "CANCELLED", "DELIVERED"
    STATUSES = (PENDING, APPROVED, REJECTED, CANCELLED, DELIVERED)

    __tablename__ = "request_items"
    __table_args__ = table_args(
        db.UniqueConstraint("request_id", "line_no", name="uq_request_items_request_id_line_no"),
        # The line's requester is the request's requester, and cannot disagree with it.
        db.ForeignKeyConstraint(["request_id", "requested_by"], ["requests.id", "requests.requested_by"],
                                name="fk_request_items_request"),
        db.CheckConstraint(f"status IN {STATUSES}", name="known_status"),
        db.CheckConstraint(f"quantity > 0 AND quantity <= {MAX_QUANTITY}", name="quantity_range"),
        db.CheckConstraint("decided_by IS NULL OR decided_by <> requested_by", name="not_self_decided"),
        db.CheckConstraint("status <> 'REJECTED' OR (decided_by IS NOT NULL AND reason IS NOT NULL)",
                           name="rejection_needs_reason"),
        db.CheckConstraint("status NOT IN ('APPROVED', 'DELIVERED') OR decided_by IS NOT NULL",
                           name="approval_needs_decider"),
        db.CheckConstraint("status <> 'DELIVERED' OR (delivered_by IS NOT NULL AND delivered_dt IS NOT NULL)",
                           name="delivery_needs_deliverer"),
        db.CheckConstraint("status <> 'CANCELLED' OR decided_by IS NULL", name="cancel_has_no_decider"),
        comment="Product lines. Approved in full or rejected; the quantity never changes.",
    )

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("requests.id"), nullable=False, index=True)
    requested_by = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False,
                             comment="Copied from the request through a composite key")
    line_no = db.Column(db.Integer, nullable=False, comment="1, 2, 3 … within the request")
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False, comment="In the product's own unit")
    status = db.Column(db.String(12), nullable=False, default=PENDING, index=True)
    decided_by = db.Column(db.Integer, db.ForeignKey("staff.id"))
    decided_dt = db.Column(db.DateTime, comment="UTC")
    reason = db.Column(db.String(255), comment="Required when rejected")
    delivered_by = db.Column(db.Integer, db.ForeignKey("staff.id"))
    delivered_dt = db.Column(db.DateTime, comment="UTC")

    request = db.relationship("Request", back_populates="items", foreign_keys=[request_id])
    product = db.relationship("Product", lazy="joined", foreign_keys=[product_id])
    decider = db.relationship("Staff", lazy="joined", foreign_keys=[decided_by])
    deliverer = db.relationship("Staff", lazy="joined", foreign_keys=[delivered_by])
    history = db.relationship("RequestItemHistory", lazy="selectin",
                              order_by="RequestItemHistory.id",
                              foreign_keys="RequestItemHistory.request_item_id")

    @property
    def request_line_no(self):
        return f"{self.request.request_no}-{self.line_no}"

    @property
    def line_code(self):
        """What people see and quote: EE87DA-2."""
        return f"{self.request.short_code}-{self.line_no}"

    def to_dict(self, with_history=False):
        product = self.product
        data = {
            "id": self.id,
            "line_no": self.line_no,
            "request_no": self.request.request_no if self.request else None,
            "request_code": self.request.short_code if self.request else None,
            "line_ref": self.request_line_no if self.request else None,
            "line_code": self.line_code if self.request else None,
            "product": {"id": product.id, "sku": product.sku, "name": product.name,
                        "unit": product.unit, "category_id": product.category_id,
                        "category": product.category.name if product.category else None},
            "quantity": self.quantity,
            "status": self.status,
            "decided_by": _brief(self.decider),
            "decided_dt": iso(self.decided_dt),
            "reason": self.reason,
            "delivered_by": _brief(self.deliverer),
            "delivered_dt": iso(self.delivered_dt),
            "requested_by": _brief(self.request.requester) if self.request else None,
            "requested_dt": iso(self.request.requested_dt) if self.request else None,
            "purpose": self.request.purpose if self.request else None,
        }
        if with_history:
            data["history"] = [h.to_dict() for h in self.history]
        return data


class RequestItemHistory(CreatedMixin, db.Model):
    """Every status a line passed through: who moved it, when, and why."""

    __tablename__ = "request_item_history"
    __table_args__ = table_args(
        db.CheckConstraint(f"status IN {RequestItem.STATUSES}", name="known_status"),
        comment="Status history of a request line (write-once).",
    )

    id = big_id()
    request_item_id = db.Column(db.Integer, db.ForeignKey("request_items.id"), nullable=False, index=True)
    status = db.Column(db.String(12), nullable=False)
    note = db.Column(db.String(255))

    item = db.relationship("RequestItem", back_populates="history", foreign_keys=[request_item_id])

    def to_dict(self):
        return {"status": self.status, "note": self.note,
                "by": _brief(self.creator), "at": iso(self.created_dt)}


class Order(CreatedMixin, db.Model):
    """A finished handover: one order number per request, created by the app
    once nothing in that request is left waiting."""

    __tablename__ = "orders"
    __table_args__ = table_args(
        db.UniqueConstraint("order_no", name="uq_orders_order_no"),
        db.UniqueConstraint("short_code", name="uq_orders_short_code"),
        db.UniqueConstraint("request_id", name="uq_orders_request_id"),
        # Target of the acknowledgement's composite FK, and ties the order to its requester.
        db.UniqueConstraint("id", "requested_by", name="uq_orders_id_requested_by"),
        db.ForeignKeyConstraint(["request_id", "requested_by"], ["requests.id", "requests.requested_by"],
                                name="fk_orders_request"),
        comment="One order number per completed request. The figures stay on the request lines.",
    )

    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(24), nullable=False, comment="ORD-20260926-AF470D")
    short_code = db.Column(db.String(8), nullable=False, comment="AF470D — what people see and quote")
    request_id = db.Column(db.Integer, db.ForeignKey("requests.id"), nullable=False)
    requested_by = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False, index=True)
    completed_dt = db.Column(db.DateTime, nullable=False, comment="UTC; when the last line was handed over")

    request = db.relationship("Request", back_populates="order", foreign_keys=[request_id])
    requester = db.relationship("Staff", lazy="joined", foreign_keys=[requested_by])
    acknowledgement = db.relationship("OrderAcknowledgement", back_populates="order", uselist=False,
                                      lazy="joined", foreign_keys="OrderAcknowledgement.order_id")

    @property
    def delivered_items(self):
        return [item for item in self.request.items if item.status == RequestItem.DELIVERED]

    def to_dict(self, with_items=True):
        delivered = self.delivered_items
        data = {
            "order_no": self.order_no,
            "code": self.short_code,
            "request_no": self.request.request_no,
            "request_code": self.request.short_code,
            "purpose": self.request.purpose,
            "requested_by": _brief(self.requester),
            "requested_dt": iso(self.request.requested_dt),
            "completed_dt": iso(self.completed_dt),
            "line_count": len(delivered),
            "not_given_count": sum(1 for item in self.request.items if item.status != "DELIVERED"),
            "approved_by": list({row.decided_by: _brief(row.decider) for row in delivered}.values()),
            "delivered_by": list({row.delivered_by: _brief(row.deliverer) for row in delivered}.values()),
            "categories": sorted({item.product.category.name for item in delivered if item.product.category}),
            "acknowledged": self.acknowledgement.to_dict() if self.acknowledgement else None,
        }
        if with_items:
            data["items"] = [item.to_dict() for item in delivered]
            # Every line of the request, so the order shows what was rejected too.
            data["request_items"] = [item.to_dict() for item in self.request.items]
        return data


class OrderAcknowledgement(CreatedMixin, db.Model):
    """The requester confirming the goods reached them. Write-once, once only."""

    __tablename__ = "order_acknowledgements"
    __table_args__ = table_args(
        db.UniqueConstraint("order_id", name="uq_order_acknowledgements_order_id"),
        # Only the order's own requester can be the one confirming it.
        db.ForeignKeyConstraint(["order_id", "acknowledged_by"], ["orders.id", "orders.requested_by"],
                                name="fk_order_acknowledgements_order"),
        comment="Receipt confirmed by the person who raised the request (write-once).",
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    acknowledged_by = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False, index=True)
    acknowledged_dt = db.Column(db.DateTime, nullable=False, default=utcnow, comment="UTC")
    note = db.Column(db.String(255), comment="Anything to add on receiving")

    order = db.relationship("Order", back_populates="acknowledgement", foreign_keys=[order_id])
    person = db.relationship("Staff", lazy="joined", foreign_keys=[acknowledged_by])

    def to_dict(self):
        return {"by": _brief(self.person), "at": iso(self.acknowledged_dt), "note": self.note}


# =========================================================================== stock

class Batch(AuditMixin, db.Model):
    """A batch of one product. Its quantity is not stored: it is the sum of its
    ledger rows, so the two can never disagree."""

    __tablename__ = "batches"
    __table_args__ = table_args(
        db.UniqueConstraint("product_id", "batch_no", name="uq_batches_product_id_batch_no"),
        # Target of stock_movements' composite FK: a ledger row's batch belongs to its product.
        db.UniqueConstraint("id", "product_id", name="uq_batches_id_product_id"),
        db.CheckConstraint("batch_no IS NULL OR (batch_no = TRIM(batch_no) AND batch_no <> '')",
                           name="batch_no_trimmed"),
        comment="Stock batches (batch no + expiry). Quantity = SUM of the batch's stock_movements.",
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    batch_no = db.Column(db.String(40), comment="From the bill or pack; empty when not known")
    expiry_date = db.Column(db.Date, comment="Empty for products that don't expire")
    received_dt = db.Column(db.Date, nullable=False, comment="When this batch first came in")

    product = db.relationship("Product", back_populates="batches", foreign_keys=[product_id])
    receipts = db.relationship(
        "StockMovement", viewonly=True,
        primaryjoin="and_(StockMovement.batch_id == Batch.id, StockMovement.invoice_item_id.isnot(None))")

    @property
    def label(self):
        return f"batch {self.batch_no}" if self.batch_no else "stock without a batch number"

    @property
    def unit_cost(self):
        """Weighted average of what the bills paid for this batch."""
        total_cost = Decimal("0")
        total_units = 0
        for movement in self.receipts:
            line = movement.invoice_item
            if line is None:
                continue
            per_unit = line.unit_cost
            if per_unit is None:
                continue
            total_cost += per_unit * movement.quantity_change
            total_units += movement.quantity_change
        if not total_units:
            return None
        return float((total_cost / total_units).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    @property
    def mrp(self):
        for movement in sorted(self.receipts, key=lambda m: m.id, reverse=True):
            if movement.invoice_item and movement.invoice_item.mrp is not None:
                return float(movement.invoice_item.mrp)
        return None

    def to_dict(self):
        return {"id": self.id, "batch_no": self.batch_no, "quantity": int(self.quantity),
                "expiry_date": self.expiry_date.isoformat() if self.expiry_date else None,
                "received_dt": self.received_dt.isoformat() if self.received_dt else None,
                "unit_cost": self.unit_cost, "mrp": self.mrp}


class StockMovement(CreatedMixin, db.Model):
    """The stock ledger: the single source of truth for stock.

    Rows can never be edited or deleted, and products.current_stock must match
    the newest row (enforced by a trigger)."""

    RESTOCK, DELIVERY, ADJUSTMENT = "RESTOCK", "DELIVERY", "ADJUSTMENT"
    REASONS = (RESTOCK, DELIVERY, ADJUSTMENT)

    __tablename__ = "stock_movements"
    __table_args__ = table_args(
        db.ForeignKeyConstraint(["batch_id", "product_id"], ["batches.id", "batches.product_id"],
                                name="fk_stock_movements_batch"),
        db.CheckConstraint(f"reason IN {REASONS}", name="known_reason"),
        db.CheckConstraint("quantity_change <> 0", name="change_not_zero"),
        db.CheckConstraint("balance_after >= 0", name="balance_not_negative"),
        db.CheckConstraint("invoice_item_id IS NULL OR reason = 'RESTOCK'", name="bill_line_only_on_restock"),
        db.CheckConstraint("request_item_id IS NULL OR reason = 'DELIVERY'", name="request_only_on_delivery"),
        comment="Every stock change, with its reason. Nothing else may change stock.",
    )

    id = big_id()
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False, index=True)
    quantity_change = db.Column(db.Integer, nullable=False, comment="+ in, − out")
    balance_after = db.Column(db.Integer, nullable=False, comment="The product's stock after this row")
    reason = db.Column(db.String(12), nullable=False)
    request_item_id = db.Column(db.Integer, db.ForeignKey("request_items.id"),
                                comment="The line handed over")
    invoice_item_id = db.Column(db.Integer, db.ForeignKey("invoice_items.id"),
                                comment="The bill line it came in on")
    note = db.Column(db.String(255), comment="Required on an adjustment")

    product = db.relationship("Product", lazy="joined", foreign_keys=[product_id])
    batch = db.relationship("Batch", lazy="joined", foreign_keys=[batch_id])
    request_item = db.relationship("RequestItem", foreign_keys=[request_item_id])
    invoice_item = db.relationship("InvoiceItem", lazy="joined", foreign_keys=[invoice_item_id])

    def to_dict(self):
        return {
            "id": self.id, "quantity_change": self.quantity_change,
            "balance_after": self.balance_after, "reason": self.reason,
            "batch_no": self.batch.batch_no if self.batch else None,
            "expiry_date": self.batch.expiry_date.isoformat()
                           if self.batch and self.batch.expiry_date else None,
            "request_no": self.request_item.request_line_no if self.request_item else None,
            "invoice_no": self.invoice_item.invoice.invoice_no if self.invoice_item else None,
            "by": _brief(self.creator), "note": self.note, "at": iso(self.created_dt),
        }


# Defined after StockMovement: a batch's quantity is the sum of its ledger rows.
Batch.quantity = column_property(
    db.select(db.cast(db.func.coalesce(db.func.sum(StockMovement.quantity_change), 0), db.Integer))
    .where(StockMovement.batch_id == Batch.id)
    .correlate_except(StockMovement)
    .scalar_subquery()
)


# =========================================================================== purchases

class Invoice(AuditMixin, db.Model):
    """A vendor's bill. Totals are calculated from the lines, never stored."""

    DRAFT, POSTED, CANCELLED = "DRAFT", "POSTED", "CANCELLED"
    STATUSES = (DRAFT, POSTED, CANCELLED)

    __tablename__ = "invoices"
    __table_args__ = table_args(
        db.UniqueConstraint("vendor_id", "invoice_no", name="uq_invoices_vendor_id_invoice_no"),
        db.CheckConstraint(f"status IN {STATUSES}", name="known_status"),
        db.CheckConstraint("round_off >= -10 AND round_off <= 10", name="round_off_small"),
        comment="Purchase bills. Amounts are calculated from invoice_items.",
    )

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"), nullable=False, index=True)
    invoice_no = db.Column(db.String(40), nullable=False)
    invoice_date = db.Column(db.Date, nullable=False)
    round_off = db.Column(money(8, 2), nullable=False, default=0, comment="As printed on the bill")
    status = db.Column(db.String(12), nullable=False, default=DRAFT, index=True)
    notes = db.Column(db.String(255))
    posted_dt = db.Column(db.DateTime, comment="UTC")

    vendor = db.relationship("Vendor", lazy="joined", foreign_keys=[vendor_id])
    items = db.relationship("InvoiceItem", back_populates="invoice", lazy="selectin",
                            order_by="InvoiceItem.line_no", cascade="all, delete-orphan",
                            foreign_keys="InvoiceItem.invoice_id")
    payments = db.relationship("Payment", back_populates="invoice", lazy="selectin",
                               order_by="Payment.paid_on", foreign_keys="Payment.invoice_id")

    def tax_type(self, clinic_state_code):
        if self.vendor.is_composition:
            return "NONE"
        vendor_state = (self.vendor.gstin or "")[:2]
        if not vendor_state:
            return "CGST_SGST"
        return "CGST_SGST" if vendor_state == clinic_state_code else "IGST"

    def totals(self, clinic_state_code="36"):
        taxable = sum((line.taxable for line in self.items), Decimal("0"))
        gst = sum((line.gst_amount for line in self.items), Decimal("0"))
        kind = self.tax_type(clinic_state_code)
        half = (gst / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return {
            "taxable": float(taxable.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "gst": float(gst.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "cgst": float(half) if kind == "CGST_SGST" else 0.0,
            "sgst": float(gst - half) if kind == "CGST_SGST" else 0.0,
            "igst": float(gst.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if kind == "IGST" else 0.0,
            "round_off": float(self.round_off or 0),
            "net": float((taxable + gst + (self.round_off or 0)).quantize(Decimal("0.01"),
                                                                         rounding=ROUND_HALF_UP)),
            "tax_type": kind,
        }

    @property
    def paid_amount(self):
        return float(sum((payment.amount for payment in self.payments), Decimal("0")))

    def payment_status(self, clinic_state_code="36"):
        net = self.totals(clinic_state_code)["net"]
        paid = self.paid_amount
        if self.status == self.CANCELLED or net <= 0:
            return "NA" if self.status == self.CANCELLED else "UNPAID"
        if paid <= 0:
            return "UNPAID"
        return "PAID" if paid + 0.009 >= net else "PART_PAID"

    def to_dict(self, clinic_state_code="36", with_items=True):
        totals = self.totals(clinic_state_code)
        data = {
            "id": self.id,
            "invoice_no": self.invoice_no,
            "invoice_date": self.invoice_date.isoformat(),
            "vendor": self.vendor.to_dict(),
            "status": self.status,
            "notes": self.notes,
            "posted_dt": iso(self.posted_dt),
            "entered_by": _brief(self.creator),
            "line_count": len(self.items),
            "paid_amount": self.paid_amount,
            "payment_status": self.payment_status(clinic_state_code),
            "payments": [payment.to_dict() for payment in self.payments],
            **totals,
        }
        if with_items:
            data["items"] = [line.to_dict() for line in self.items]
        return data


class InvoiceItem(AuditMixin, db.Model):
    """One line of a bill.

    The bill is copied as printed: so many packs, so many units in a pack, a rate
    per pack. Stock is always counted in the product's own issue unit."""

    STOCK, NON_STOCK = "STOCK", "NON_STOCK"
    TYPES = (STOCK, NON_STOCK)

    __tablename__ = "invoice_items"
    __table_args__ = table_args(
        db.UniqueConstraint("invoice_id", "line_no", name="uq_invoice_items_invoice_id_line_no"),
        db.CheckConstraint(f"line_type IN {TYPES}", name="known_line_type"),
        db.CheckConstraint("(line_type = 'STOCK' AND product_id IS NOT NULL AND description IS NULL)"
                           " OR (line_type = 'NON_STOCK' AND product_id IS NULL"
                           " AND batch_no IS NULL AND description IS NOT NULL)",
                           name="line_type_rules"),
        db.CheckConstraint("pack_quantity > 0 AND units_per_pack > 0 AND free_packs >= 0",
                           name="quantities_positive"),
        db.CheckConstraint("rate >= 0 AND (mrp IS NULL OR mrp >= 0)", name="amounts_not_negative"),
        db.CheckConstraint("discount_percent >= 0 AND discount_percent <= 100", name="discount_range"),
        db.CheckConstraint("gst_rate >= 0 AND gst_rate <= 40", name="gst_rate_range"),
        comment="Bill lines. Taxable, GST and totals are calculated from these columns.",
    )

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    line_no = db.Column(db.Integer, nullable=False)
    line_type = db.Column(db.String(12), nullable=False, default=STOCK)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), index=True)
    description = db.Column(db.String(200), comment="For machines and services")
    hsn_id = db.Column(db.Integer, db.ForeignKey("hsn_codes.id"))
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0,
                         comment="Copied when entered, so old bills never change")
    batch_no = db.Column(db.String(40))
    expiry_date = db.Column(db.Date)
    pack_quantity = db.Column(db.Integer, nullable=False, default=1, comment="Packs, as printed")
    units_per_pack = db.Column(db.Integer, nullable=False, default=1,
                               comment="Issue units in one pack (1 when sold loose)")
    free_packs = db.Column(db.Integer, nullable=False, default=0, comment="Free packs, as printed")
    rate = db.Column(db.Numeric(12, 3), nullable=False, comment="Price of one pack before discount and GST")
    discount_percent = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    mrp = db.Column(money(), comment="Per pack, as printed")

    invoice = db.relationship("Invoice", back_populates="items", foreign_keys=[invoice_id])
    product = db.relationship("Product", lazy="joined", foreign_keys=[product_id])
    hsn = db.relationship("HsnCode", lazy="joined", foreign_keys=[hsn_id])

    # ---- everything below is calculated, never stored
    @property
    def quantity(self):
        """Issue units bought (excluding free)."""
        return self.pack_quantity * self.units_per_pack

    @property
    def free_quantity(self):
        return self.free_packs * self.units_per_pack

    @property
    def received_quantity(self):
        return self.quantity + self.free_quantity

    @property
    def gross(self):
        return (Decimal(self.pack_quantity) * Decimal(self.rate)).quantize(Decimal("0.01"),
                                                                           rounding=ROUND_HALF_UP)

    @property
    def discount_amount(self):
        return (self.gross * Decimal(self.discount_percent) / 100).quantize(Decimal("0.01"),
                                                                            rounding=ROUND_HALF_UP)

    @property
    def taxable(self):
        return self.gross - self.discount_amount

    @property
    def gst_amount(self):
        return (self.taxable * Decimal(self.gst_rate) / 100).quantize(Decimal("0.01"),
                                                                      rounding=ROUND_HALF_UP)

    @property
    def line_total(self):
        return self.taxable + self.gst_amount

    @property
    def unit_cost(self):
        """What one issue unit cost, free units included."""
        units = self.received_quantity
        if not units:
            return None
        return (self.taxable / units).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    def to_dict(self):
        product = self.product
        return {
            "id": self.id,
            "line_no": self.line_no,
            "line_type": self.line_type,
            "product": {"id": product.id, "sku": product.sku, "name": product.name,
                        "unit": product.unit} if product else None,
            "product_id": self.product_id,
            "description": self.description,
            "hsn": self.hsn.to_dict() if self.hsn else None,
            "hsn_id": self.hsn_id,
            "gst_rate": float(self.gst_rate),
            "batch_no": self.batch_no,
            "expiry_date": self.expiry_date.isoformat() if self.expiry_date else None,
            "pack_quantity": self.pack_quantity,
            "units_per_pack": self.units_per_pack,
            "free_packs": self.free_packs,
            "quantity": self.quantity,
            "free_quantity": self.free_quantity,
            "rate": float(self.rate),
            "discount_percent": float(self.discount_percent),
            "mrp": float(self.mrp) if self.mrp is not None else None,
            "taxable": float(self.taxable),
            "gst_amount": float(self.gst_amount),
            "line_total": float(self.line_total),
            "unit_cost": float(self.unit_cost) if self.unit_cost is not None else None,
        }


class Payment(AuditMixin, db.Model):
    """Money paid against a bill. Several payments may settle one bill."""

    METHODS = ("CASH", "UPI", "BANK", "CHEQUE", "OTHER")

    __tablename__ = "payments"
    __table_args__ = table_args(
        db.CheckConstraint("amount > 0", name="amount_positive"),
        db.CheckConstraint(f"method IN {METHODS}", name="known_method"),
        comment="Payments against purchase bills; a bill may have several.",
    )

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False, index=True)
    amount = db.Column(money(), nullable=False)
    paid_on = db.Column(db.Date, nullable=False)
    method = db.Column(db.String(10), nullable=False, default="CASH")
    reference = db.Column(db.String(60), comment="UTR, cheque number …")
    note = db.Column(db.String(255))

    invoice = db.relationship("Invoice", back_populates="payments", foreign_keys=[invoice_id])

    def to_dict(self):
        return {"id": self.id, "amount": float(self.amount), "paid_on": self.paid_on.isoformat(),
                "method": self.method, "reference": self.reference, "note": self.note,
                "by": _brief(self.creator)}


# =========================================================================== trail

class AuditLog(db.Model):
    """Every action, kept for good. Cannot be edited or deleted."""

    __tablename__ = "audit_log"
    __table_args__ = table_args(comment="Audit trail: who did what, when, with before/after details.")

    id = big_id()
    action = db.Column(db.String(60), nullable=False, index=True)
    entity = db.Column(db.String(40), comment="product, request, invoice, order …")
    entity_id = db.Column(db.String(40), comment="Text: one log row may describe any table")
    actor_id = db.Column(db.Integer, db.ForeignKey("staff.id"), index=True)
    actor_levels = db.Column(db.String(120), comment="The levels they held at that moment")
    request_no = db.Column(db.String(24), index=True)
    details = db.Column(db.JSON)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(255))
    created_dt = db.Column(db.DateTime, nullable=False, default=utcnow, index=True, comment="UTC")

    actor = db.relationship("Staff", lazy="joined", foreign_keys=[actor_id])

    def to_dict(self):
        return {
            "id": self.id, "action": self.action, "entity": self.entity, "entity_id": self.entity_id,
            "actor": _brief(self.actor), "actor_levels": self.actor_levels,
            "request_no": self.request_no, "details": self.details,
            "ip_address": self.ip_address, "created_dt": iso(self.created_dt),
        }
