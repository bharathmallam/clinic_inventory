"""Setup and maintenance commands.

  Deployment (live server)
    python manage.py deploy             backup, apply migrations, triggers, levels/categories, then pre-flight
    python manage.py preflight          check the live setup is ready (exit code 1 if anything fails)
    python manage.py backup [--dir D]   compressed database snapshot (keeps the newest 14)
    python manage.py import-products FILE.csv --as USERNAME [--dry-run]
    python manage.py export-products FILE.csv
    python manage.py import-staff FILE.csv --as USERNAME [--dry-run]   (writes temporary passwords to a private file)

  Setup
    python manage.py init-db            create/upgrade tables (migrations) + read-only triggers
    python manage.py seed [--demo]      access levels, starter categories and units (+ sample products with --demo)
    python manage.py create-user USERNAME EMAIL "Full Name" --access "CHECKER: Pantry; STORE"
    python manage.py seed-dev-users     LOCAL ONLY: test logins admin/manager/manager2/store/staff, password "test"
    python manage.py bootstrap-dev      LOCAL ONLY: migrate, then add levels, test logins and demo products if missing
    python manage.py set-clinic --name "Ultrahealth" --gstin 36XXXXXXXXXXXZ5
    python manage.py check              verify the data is consistent
    python manage.py export-schema      regenerate database/schema.sql from the models
"""
import argparse
import csv
import getpass
import os
import sys
from datetime import date, timedelta

from flask import current_app
from flask_migrate import upgrade
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable
from werkzeug.security import generate_password_hash

from app import create_app
from app.extensions import db
from app.models import (
    AccessLevel, Category, HsnCode, LevelPermission, Product, Staff, StaffAccess, Unit, Vendor,
)
from app.services import accounts, catalogue, integrity, masters, ops, stock, transfer
from app.services.core import atomic

HERE = os.path.dirname(os.path.abspath(__file__))

# What each access level may do. Levels are fixed; categories are not, so a
# maker or checker is always given one or more categories on their account.
PERMISSIONS = {
    "request.create": "Raise requests",
    "request.decide": "Approve or reject requests",
    "request.deliver": "Hand goods over and mark them delivered",
    "order.acknowledge": "Confirm receipt of their own orders",
    "stock.manage": "Add stock and make adjustments",
    "product.manage": "Add and edit products",
    "product.deactivate": "Remove a product from the list",
    "purchase.manage": "Enter purchase bills, vendors and payments",
    "inventory.view": "See stock figures",
    "reports.view": "See reports",
    "user.manage": "Manage accounts and access",
    "category.manage": "Add and edit categories",
    "audit.view": "Read the audit log",
}

LEVELS = [
    ("MAKER", "Maker", "Raises requests in their categories", True,
     {"request.create", "order.acknowledge"}),
    ("CHECKER", "Checker", "Approves or rejects requests in their categories", True,
     {"request.create", "order.acknowledge", "request.decide", "product.manage",
      "product.deactivate", "list.manage", "inventory.view", "reports.view"}),
    ("STORE", "Store", "Delivers approved requests, adds stock and enters bills", False,
     {"request.create", "order.acknowledge", "request.deliver", "stock.manage", "product.manage",
      "purchase.manage", "list.manage", "inventory.view", "reports.view"}),
    # The Administrator keeps away from products and stock, but owns the lists the
    # clinic is built from — otherwise a brand-new system cannot be set up at all:
    # on day one the Administrator is the only account there is.
    ("ADMIN", "Administrator", "Accounts, access, categories, units and the audit log", False,
     {"user.manage", "category.manage", "list.manage", "audit.view", "inventory.view",
      "reports.view"}),
]

CATEGORIES = [
    ("CLINIC_CONSUMABLES", "Clinic Consumables", "Cotton, injectables, syringes, gloves, gauze", True, True),
    ("PANTRY", "Pantry", "Coffee, tea, snacks, sugar", True, False),
    ("HOUSEKEEPING", "Housekeeping", "Cleaners, tissue, garbage bags", False, False),
]

# How stock is handed out, nothing more: the carton it arrives in is the product's
# pack size, not a unit. Add one on the Lists page the day something needs it.
UNITS = ["Piece", "Box", "Bottle", "Packet", "Tube", "Kit", "Roll", "Jar"]

DEMO_VENDORS = [("MOHAN MEDICAL & SURGICAL", "36AFJPM6523Q2ZR"), ("CARE PHARMACY", "36BCNPA9810Q2Z0")]
DEMO_HSN = [("30059090", "Wadding, gauze, bandages and similar articles", 5),
            ("40151100", "Surgical gloves of rubber", 18),
            ("90183100", "Syringes, with or without needles", 12),
            ("09024010", "Black tea", 5),
            ("48182000", "Tissue paper", 18)]
DEMO_PRODUCTS = {
    "CLINIC_CONSUMABLES": [
        # sku, name, unit, stock, reorder, pack size, pack label, hsn
        ("CC-COT-500", "Absorbent Cotton Roll 500g", "Roll", 20, 5, None, None, "30059090"),
        ("CC-GLV-L", "Sterile Surgical Gloves (L)", "Piece", 250, 50, 50, "Box", "40151100"),
        ("CC-SYR-5", "Disposable Syringe 5ml", "Piece", 400, 100, 100, "Box", "90183100"),
    ],
    "PANTRY": [
        ("PN-TEA-GRN", "Green Tea Bags", "Box", 10, 3, None, None, "09024010"),
        ("PN-COF-BLK", "Black Coffee (instant)", "Jar", 6, 2, None, None, None),
    ],
    "HOUSEKEEPING": [
        ("HK-TIS-BOX", "Tissue Paper Box", "Box", 30, 10, 24, "Carton", "48182000"),
        ("HK-FLR-CLN", "Floor Cleaner 1L", "Bottle", 8, 3, None, None, None),
    ],
}

BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.expanduser("~/inventory_backups"))


# --------------------------------------------------------------------------- setup

def init_db():
    upgrade(directory=os.path.join(HERE, "migrations"))
    print("Tables are up to date.")
    try:
        integrity.install_triggers()
        print("Read-only triggers installed.")
    except Exception as exc:  # e.g. a database account without the TRIGGER privilege
        print(f"WARNING: could not install read-only triggers ({exc}).\n"
              "The app still enforces these rules itself.")


def seed_lists():
    """What a new system starts with, and nothing more:

      * the four access levels with their permissions (the app cannot run without them)
      * the clinic's own row
      * its three categories: Clinic Consumables, Pantry, Housekeeping
      * the eight units its stock is counted in

    No vendors, no HSN codes, no products, no accounts. Those come from the
    clinic's own spreadsheets. Anything here can be renamed or deleted on the
    Lists page afterwards."""
    with atomic():
        for code, name, description, needs_category, permissions in LEVELS:
            level = AccessLevel.query.filter_by(code=code).first() or AccessLevel(code=code)
            level.name, level.description, level.needs_category = name, description, needs_category
            db.session.add(level)
            db.session.flush()
            have = {row.permission: row for row in level.permissions}
            for permission in permissions:
                if permission not in have:
                    db.session.add(LevelPermission(level_id=level.id, permission=permission))
            for permission, row in have.items():
                if permission not in permissions:
                    db.session.delete(row)

        # First install only. On every later upgrade the clinic's own lists are left
        # exactly as they are — otherwise a category or unit deleted on purpose would
        # quietly come back.
        first_install = not Category.query.first()
        if first_install:
            for code, name, description, tracks_expiry, needs_hsn in CATEGORIES:
                db.session.add(Category(code=code, name=name, description=description,
                                        tracks_expiry=tracks_expiry, needs_hsn=needs_hsn))
        if not Unit.query.first():
            for name in UNITS:
                db.session.add(Unit(code=name.upper(), name=name))
        masters.organisation()
    if first_install:
        print(f"Ready: 4 access levels, {len(CATEGORIES)} categories "
              f"({', '.join(name for _, name, _, _, _ in CATEGORIES)}), {len(UNITS)} units "
              f"({', '.join(UNITS)}). Nothing else.")
    else:
        print("Access levels up to date. Your categories and units were left alone.")


def seed_demo_products():
    keeper = next((person for person in Staff.query.filter_by(is_active=True)
                   if person.can("product.manage") and person.can("stock.manage")), None)
    if not keeper:
        sys.exit("Create a STORE account first (create-user … --access STORE), then re-run seed --demo.")
    today = date.today()
    with atomic():
        for name, gstin in DEMO_VENDORS:
            if not Vendor.query.filter_by(gstin=gstin).first():
                masters.create_vendor(keeper, {"name": name, "gstin": gstin, "city": "Hyderabad"})
        for code, description, rate in DEMO_HSN:
            if not HsnCode.query.filter_by(code=code).first():
                masters.create_hsn(keeper, {"code": code, "description": description, "gst_rate": rate})

        units = {unit.name: unit.id for unit in Unit.query}
        hsn = {row.code: row.id for row in HsnCode.query}
        for category_code, rows in DEMO_PRODUCTS.items():
            category = Category.query.filter_by(code=category_code).first()
            if not category:
                continue
            for sku, name, unit, quantity, reorder, pack_size, pack_label, hsn_code in rows:
                if Product.query.filter_by(sku=sku).first():
                    continue
                catalogue.create_product(keeper, {
                    "sku": sku, "name": name, "category_id": category.id, "unit_id": units[unit],
                    "hsn_id": hsn.get(hsn_code), "reorder_level": reorder,
                    "pack_size": pack_size, "pack_label": pack_label,
                    "opening_stock": quantity, "batch_no": None,
                    "expiry_date": (today + timedelta(days=400)).isoformat()
                    if category.tracks_expiry else None,
                })
    print("Demo products added.")


def create_user(username, email, full_name, access, password=None, min_length=8):
    categories = {category.name.lower(): category for category in Category.query}
    categories.update({category.code.lower(): category for category in Category.query})
    entries = transfer.parse_access(access, categories)
    if password is None:
        password = getpass.getpass("Password (8+ characters): ")
        if password != getpass.getpass("Repeat: "):
            sys.exit("The passwords do not match.")
    if len(password) < min_length:
        sys.exit(f"The password must be at least {min_length} characters.")
    with atomic():
        person, _ = accounts.create_staff(None, {
            "username": username, "email": email, "full_name": full_name,
            "password": password, "access": entries})
    print(f"Created {person.username} ({', '.join(sorted(person.levels()))}).")
    return person


# --------------------------------------------------------------------------- local development

DEV_USERS = [
    ("admin", "Clinic Admin", "ADMIN"),
    ("manager", "Anita Rao", "CHECKER: all"),
    ("manager2", "Vikram Shah", "CHECKER: all"),
    ("store", "Store Desk", "STORE"),
    ("staff", "Ravi Kumar", "MAKER: all"),
]


def is_development():
    return current_app.config["APP_ENV"] == "development"


def _access_entries(spec):
    """'CHECKER: all' means that level in every category."""
    level_code, _, scope = spec.partition(":")
    level_code, scope = level_code.strip().upper(), scope.strip().lower()
    level = AccessLevel.query.filter_by(code=level_code).one()
    if not level.needs_category:
        return [{"level": level_code, "category_id": None}]
    categories = Category.query.filter_by(is_active=True).all()
    if scope in ("", "all"):
        return [{"level": level_code, "category_id": category.id} for category in categories]
    wanted = {part.strip().lower() for part in scope.split(",")}
    return [{"level": level_code, "category_id": category.id} for category in categories
            if category.name.lower() in wanted or category.code.lower() in wanted]


def seed_dev_users():
    if not is_development():
        sys.exit("Refusing: APP_ENV is not development. Use create-user on the live site.")
    seed_lists()
    with atomic():
        for username, full_name, spec in DEV_USERS:
            person = Staff.query.filter_by(username=username).first()
            if person:
                continue
            accounts.create_staff(None, {
                "username": username, "full_name": full_name, "email": f"{username}@clinic.local",
                "password": "test", "access": _access_entries(spec)}, allow_weak=True)
    print("Test logins ready (password 'test'): " + ", ".join(name for name, _, _ in DEV_USERS))


def bootstrap_dev():
    if not is_development():
        sys.exit("Refusing: APP_ENV is not development.")
    init_db()
    seed_dev_users()
    if not Product.query.first():
        seed_demo_products()


# --------------------------------------------------------------------------- deployment

def print_preflight():
    results = ops.preflight()
    width = max(len(check) for _, check, _ in results)
    worst_is_fail = False
    for status, check, detail in results:
        print(f"  {status:4}  {check:<{width}}  {detail}")
        worst_is_fail = worst_is_fail or status == ops.FAIL
    print("\n" + ("NOT READY: fix the FAIL lines above." if worst_is_fail else "READY."))
    return not worst_is_fail


def backup(directory=BACKUP_DIR, keep=14):
    path = ops.backup(directory, keep)
    print(f"Backup written: {path}")
    return path


def deploy(skip_backup=False):
    if ops.has_tables() and not skip_backup:
        try:
            backup()
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: backup failed ({exc}). Continuing.")
    init_db()
    seed_lists()
    print("\nPre-flight:")
    return print_preflight()


# --------------------------------------------------------------------------- data in and out

def find_actor(username):
    person = Staff.query.filter_by(username=username).first()
    if not person:
        sys.exit(f"No account called {username}.")
    return person


def print_report(report, dry_run, what):
    for row, problem in report.errors:
        print(f"  row {row}: {problem}")
    for skipped in report.skipped:
        print(f"  skipped {skipped}")
    if report.errors:
        print(f"\nNothing was imported: fix the {len(report.errors)} problem(s) above.")
        return False
    print(f"{'Would import' if dry_run else 'Imported'} {len(report.created)} {what}.")
    return True


def import_products_cmd(path, username, dry_run):
    actor = find_actor(username)
    with atomic():
        report = transfer.import_products(actor, open(path, encoding="utf-8-sig").read(), dry_run=dry_run)
        if not report.ok:
            db.session.rollback()
    return print_report(report, dry_run, "products")


def export_products_cmd(path):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(transfer.export_products())
    print(f"Products written to {path}")


def import_staff_cmd(path, username, dry_run):
    actor = find_actor(username)
    with atomic():
        report, passwords = transfer.import_staff(actor, open(path, encoding="utf-8-sig").read(),
                                                  dry_run=dry_run)
        if not report.ok:
            db.session.rollback()
    ok = print_report(report, dry_run, "accounts")
    if ok and passwords:
        out = os.path.splitext(path)[0] + "-temporary-passwords.csv"
        with open(out, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["username", "temporary_password"])
            writer.writerows(passwords.items())
        os.chmod(out, 0o600)
        print(f"Temporary passwords written to {out} — hand them out, then delete the file.")
    return ok


def set_clinic(name=None, gstin=None):
    data = {}
    if name:
        data["name"] = name
    if gstin:
        data["gstin"] = gstin
    if not data:
        org = masters.organisation()
        print(f"{org.name} · GSTIN {org.gstin or 'not set'} · state {org.effective_state_code}")
        return
    with atomic():
        org = masters.update_organisation(None, data)
    print(f"Clinic saved: {org.name} · GSTIN {org.gstin or 'not set'} · state {org.effective_state_code}")


def check():
    problems = integrity.check()
    if problems:
        for problem in problems:
            print(f"  {problem}")
        sys.exit(f"{len(problems)} problem(s) found.")
    print("OK: stock matches the ledger, batches add up, and every account has access.")


def export_schema():
    """Regenerate database/schema.sql from the models (readable reference)."""
    dialect = mysql.dialect()
    lines = ["-- " + "=" * 69,
             "--  Ultrahealth Clinic Inventory: MySQL 8 / MariaDB 10.4+ schema",
             "--  GENERATED from backend/app/models.py by `python manage.py export-schema`.",
             "--  Do not edit by hand. To set up a database use `python manage.py init-db`",
             "--  (migrations); this file is the readable reference.",
             "-- " + "=" * 69, "", "SET NAMES utf8mb4;", ""]
    for table in db.metadata.sorted_tables:
        comment = table.comment or ""
        if comment:
            lines.append(f"-- {comment}")
        lines.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        for index in sorted(table.indexes, key=lambda i: i.name):
            lines.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")
        lines.append("")
    lines += ["-- Protective triggers (installed by `python manage.py init-db`):", ""]
    for statement in integrity.trigger_statements("mysql"):
        if statement.startswith("DROP"):
            continue
        lines.append("-- " + statement.split(" FOR EACH ROW")[0].split(" BEGIN")[0] + " …")
    path = os.path.join(os.path.dirname(HERE), "database", "schema.sql")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print(f"Wrote {path}")


# --------------------------------------------------------------------------- command line

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    seed_p = sub.add_parser("seed")
    seed_p.add_argument("--demo", action="store_true", help="also add sample products with stock")
    deploy_p = sub.add_parser("deploy")
    deploy_p.add_argument("--no-backup", action="store_true")
    sub.add_parser("preflight")
    backup_p = sub.add_parser("backup")
    backup_p.add_argument("--dir", default=BACKUP_DIR)
    backup_p.add_argument("--keep", type=int, default=14)
    for name in ("import-products", "import-staff"):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--as", dest="actor", required=True, help="username the import is recorded under")
        p.add_argument("--dry-run", action="store_true", help="check the file without importing")
    exp_p = sub.add_parser("export-products")
    exp_p.add_argument("file")
    user_p = sub.add_parser("create-user")
    user_p.add_argument("username")
    user_p.add_argument("email")
    user_p.add_argument("full_name")
    user_p.add_argument("--access", required=True,
                        help='e.g. "CHECKER: Pantry; MAKER: Clinic Consumables" or "ADMIN"')
    sub.add_parser("seed-dev-users")
    sub.add_parser("bootstrap-dev")
    clinic_p = sub.add_parser("set-clinic")
    clinic_p.add_argument("--name")
    clinic_p.add_argument("--gstin", help="the clinic's GSTIN (its first two digits set the state)")
    sub.add_parser("check")
    sub.add_parser("export-schema")
    args = parser.parse_args()

    with create_app().app_context():
        if args.cmd == "init-db":
            init_db()
        elif args.cmd == "seed":
            seed_lists()
            if args.demo:
                seed_demo_products()
        elif args.cmd == "create-user":
            create_user(args.username, args.email, args.full_name, args.access)
        elif args.cmd == "seed-dev-users":
            seed_dev_users()
        elif args.cmd == "bootstrap-dev":
            bootstrap_dev()
        elif args.cmd == "deploy":
            sys.exit(0 if deploy(skip_backup=args.no_backup) else 1)
        elif args.cmd == "preflight":
            sys.exit(0 if print_preflight() else 1)
        elif args.cmd == "backup":
            backup(args.dir, args.keep)
        elif args.cmd == "import-products":
            sys.exit(0 if import_products_cmd(args.file, args.actor, args.dry_run) else 1)
        elif args.cmd == "import-staff":
            sys.exit(0 if import_staff_cmd(args.file, args.actor, args.dry_run) else 1)
        elif args.cmd == "export-products":
            export_products_cmd(args.file)
        elif args.cmd == "set-clinic":
            set_clinic(args.name, args.gstin)
        elif args.cmd == "check":
            check()
        else:
            export_schema()


if __name__ == "__main__":
    main()
