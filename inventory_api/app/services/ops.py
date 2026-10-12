"""Deployment operations: pre-flight checks and database backups."""
import glob
import gzip
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from flask import current_app
from sqlalchemy import inspect, text
from werkzeug.security import check_password_hash

from ..config import DEFAULT_SECRET
from ..extensions import db
from ..models import AccessLevel, Category, Staff, Unit
from . import integrity

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
# mysqldump records which account created each trigger. Restoring as any other
# account (even the same user connecting a different way) needs SUPER, which
# the app's own database account does not have, so backups leave it out: the account doing the
# restore becomes the owner.
DEFINER = re.compile(rb"/\*!50017 DEFINER=`[^`]*`@`[^`]*`\s*\*/\s?")
WEAK_PASSWORDS = ("test", "password", "password123", "admin", "12345678")


def migrations_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "migrations")


def schema_versions():
    """(version the database is on, newest version in the code)."""
    head = ScriptDirectory(migrations_dir()).get_current_head()
    with db.engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    return current, head


def has_tables():
    return bool(inspect(db.engine).get_table_names())


# --------------------------------------------------------------------------- pre-flight

def preflight():
    """Everything that must be true before real people use the live site.
    Returns a list of (status, check, detail)."""
    results = []
    add = lambda status, check, detail="": results.append((status, check, detail))  # noqa: E731
    cfg = current_app.config
    production = cfg["APP_ENV"] == "production"

    add(PASS if production else WARN, "APP_ENV", f"{cfg['APP_ENV']}" + ("" if production else " (should be production on the live site)"))
    secret = cfg["SECRET_KEY"]
    add(PASS if secret != DEFAULT_SECRET and len(secret) >= 32 else FAIL, "SECRET_KEY",
        "set" if secret != DEFAULT_SECRET and len(secret) >= 32 else "missing or shorter than 32 characters")

    dialect = db.engine.dialect.name
    try:
        with db.engine.connect() as conn:
            if dialect == "mysql":
                version, mode, charset, tz = conn.execute(text(
                    "SELECT VERSION(), @@SESSION.sql_mode, @@character_set_database, @@SESSION.time_zone")).one()
                add(PASS, "Database connection", f"MySQL/MariaDB {version}")
                add(PASS if "STRICT_TRANS_TABLES" in mode else FAIL, "Strict mode", mode)
                add(PASS if charset == "utf8mb4" else WARN, "Database character set",
                    charset + ("" if charset == "utf8mb4" else " (create the database as utf8mb4)"))
                add(PASS if tz in ("+00:00", "UTC") else WARN, "Session time zone", tz)
                engines = conn.execute(text(
                    "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' AND ENGINE <> 'InnoDB'")).fetchall()
                add(FAIL if engines else PASS, "All tables InnoDB (transactions)",
                    ", ".join(f"{t}={e}" for t, e in engines) or "yes")
                triggers = conn.execute(text(
                    "SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()")).scalar()
            else:
                add(FAIL if production else PASS, "Database connection", f"{dialect} (the live site must use MySQL)")
                triggers = conn.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'")).scalar()
    except Exception as exc:  # noqa: BLE001
        add(FAIL, "Database connection", str(exc).splitlines()[0])
        return results

    expected_triggers = integrity.TRIGGER_COUNT
    add(PASS if triggers >= expected_triggers else WARN, "Read-only history triggers",
        f"{triggers}/{expected_triggers}" + ("" if triggers >= expected_triggers else " (run: python manage.py deploy)"))

    current, head = schema_versions()
    add(PASS if current == head else FAIL, "Database schema up to date",
        f"database {current or 'empty'}, code {head}" + ("" if current == head else " (run: python manage.py deploy)"))
    if current != head:
        return results

    categories = [c.name for c in Category.query.filter_by(is_active=True)]
    units = Unit.query.filter_by(is_active=True).count()
    # A new system has none of either: that is the starting point, not a fault.
    # Nothing can be stocked until both exist, so it is a WARN with the way out.
    add(PASS if categories else WARN, "Categories",
        ", ".join(sorted(categories)) or "none yet — add them on the Lists page")
    add(PASS if units else WARN, "Units",
        f"{units} on the list" if units else "none yet — add them on the Lists page")
    levels = {level.code for level in AccessLevel.query}
    missing = set(AccessLevel.CODES) - levels
    add(PASS if not missing else FAIL, "Access levels",
        ", ".join(sorted(levels)) + (f" (missing {', '.join(sorted(missing))})" if missing else ""))

    active = Staff.query.filter_by(is_active=True).all()
    if not active:
        add(WARN, "Accounts", "none yet (first install): next run "
            "python manage.py create-user <username> <email> \"<Name>\" ADMIN")
        problems = integrity.check()
        add(PASS if not problems else FAIL, "Data consistency", "; ".join(problems[:5]) or "OK")
        return results

    admins = [p for p in active if p.can("user.manage")]
    checkers = [p for p in active if p.can("request.decide")]
    store = [p for p in active if p.can("request.deliver")]
    add(PASS if admins else FAIL, "Administrator account", f"{len(admins)} active")
    add(PASS if len(checkers) >= 2 else WARN, "At least two checkers",
        f"{len(checkers)} active" + ("" if len(checkers) >= 2 else
                                     ": a checker's own requests need a second checker"))
    add(PASS if store else WARN, "Someone who can deliver", f"{len(store)} active")

    # Every category that people may order from needs a checker who can approve there.
    without = []
    for category in Category.query.filter_by(is_active=True):
        able = [p for p in checkers
                if p.may_decide_category(category.id)]
        if not able:
            without.append(category.name)
    add(PASS if not without else WARN, "Every category has a checker",
        ", ".join(without) + " has nobody to approve requests" if without else "yes")

    weak = [p.username for p in active
            if any(check_password_hash(p.password_hash, word) for word in WEAK_PASSWORDS)]
    if weak:
        add(FAIL if production else WARN, "No test/weak passwords", ", ".join(weak))
    else:
        add(PASS, "No test/weak passwords")

    problems = integrity.check()
    add(PASS if not problems else FAIL, "Data consistency", "; ".join(problems[:5]) or "OK")
    return results


# --------------------------------------------------------------------------- backup

def _mysqldump_path():
    for candidate in (shutil.which("mysqldump"), os.path.expanduser("~/mysql/current/bin/mysqldump")):
        if candidate and os.path.exists(candidate):
            return candidate
    raise RuntimeError("mysqldump not found. Install it with: sudo apt-get install mysql-client")


def backup(directory, keep=14):
    """Write a compressed, consistent snapshot of the database and keep the
    newest `keep` backups. Returns the file path."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")   # unique even for two backups in one second
    url = db.engine.url

    if url.get_backend_name() == "sqlite":
        if not url.database:
            raise RuntimeError("This database is in memory only, so there is nothing to back up.")
        path = os.path.join(directory, f"inventory-{stamp}.sqlite3")
        source = sqlite3.connect(url.database)
        target = sqlite3.connect(path)
        with target:
            source.backup(target)  # consistent copy even while in use
        source.close()
        target.close()
    else:
        path = os.path.join(directory, f"inventory-{stamp}.sql.gz")
        # Credentials go in a private temporary file, never on the command line.
        with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as cnf:
            cnf.write(f"[client]\nuser={url.username}\npassword={url.password or ''}\n"
                      f"host={url.host or 'localhost'}\nport={url.port or 3306}\n")
        os.chmod(cnf.name, 0o600)
        try:
            dump = subprocess.run(
                [_mysqldump_path(), f"--defaults-extra-file={cnf.name}",
                 "--single-transaction",       # consistent snapshot without locking the app
                 "--triggers", "--no-tablespaces",   # no --routines: none exist
                 "--default-character-set=utf8mb4", url.database],
                check=True, capture_output=True)
        finally:
            os.unlink(cnf.name)
        with gzip.open(path, "wb") as fh:
            fh.write(DEFINER.sub(b"", dump.stdout))
    os.chmod(path, 0o600)

    pattern = os.path.join(directory, "inventory-*")
    for old in sorted(glob.glob(pattern))[:-keep] if keep > 0 else []:
        os.remove(old)
    return path
