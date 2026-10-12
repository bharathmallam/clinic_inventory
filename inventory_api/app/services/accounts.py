"""Accounts and who may do what: staff rows and their access.

Access is one row per person, per level, per category. A row with no category
means every category, including ones added later."""
import re
import secrets
import string

from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db
from ..models import AccessLevel, Category, Staff, StaffAccess
from . import audit
from .core import WorkflowError, clean_text

MIN_PASSWORD = 8
USERNAME_SHAPE = re.compile(r"^[a-z0-9._-]{3,40}$")


def temporary_password(length=10):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def check_password_rules(password, allow_weak=False):
    """`allow_weak` is only ever used by the local test logins (password 'test')."""
    if allow_weak:
        if not password:
            raise WorkflowError("A password is required.")
        return password
    if not password or len(password) < MIN_PASSWORD:
        raise WorkflowError(f"The password must be at least {MIN_PASSWORD} characters.")
    return password


# --------------------------------------------------------------------------- access rows

def _level(code):
    level = AccessLevel.query.filter_by(code=str(code or "").strip().upper()).first()
    if not level:
        raise WorkflowError(f"'{code}' is not an access level. "
                            f"Use one of: {', '.join(AccessLevel.CODES)}.")
    return level


def _access_rows(entries):
    """Validate [{'level': 'MAKER', 'category_id': 3}, …] into (level, category_id) pairs."""
    rows, seen = [], set()
    for entry in entries or []:
        level = _level(entry.get("level") or entry.get("level_code"))
        category_id = entry.get("category_id")
        category_id = int(category_id) if str(category_id or "").isdigit() else None
        if level.needs_category and category_id is None:
            raise WorkflowError(f"{level.name} access must say which category it is for.")
        if not level.needs_category:
            category_id = None                      # STORE and ADMIN always cover everything
        if category_id is not None:
            category = db.session.get(Category, category_id)
            if category is None or not category.is_active:
                raise WorkflowError("Choose a category from the list.")
        key = (level.id, category_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append((level, category_id))
    if not rows:
        raise WorkflowError("Give the account at least one access: maker, checker, store or admin.")
    return rows


def _apply_access(person, actor, entries):
    rows = _access_rows(entries)
    StaffAccess.query.filter_by(staff_id=person.id).delete()
    for level, category_id in rows:
        db.session.add(StaffAccess(staff_id=person.id, level_id=level.id, category_id=category_id,
                                   created_by=actor.id if actor else None,
                                   updated_by=actor.id if actor else None))
    db.session.flush()
    db.session.refresh(person)


# --------------------------------------------------------------------------- accounts

def create_staff(actor, data, allow_weak=False):
    if actor is not None and not actor.can("user.manage"):
        raise WorkflowError("Your access does not allow managing accounts.", 403)
    username = clean_text(data.get("username"), 40, ).lower() if data.get("username") else None
    if not username or not USERNAME_SHAPE.match(username):
        raise WorkflowError("Username: 3-40 characters, lowercase letters, numbers, dot, dash "
                            "or underscore.")
    if Staff.query.filter_by(username=username).first():
        raise WorkflowError(f"The username {username} is already taken.", 409)
    full_name = clean_text(data.get("full_name"), 120)
    if not full_name:
        raise WorkflowError("Full name is required.")
    email = clean_text(data.get("email"), 120)
    if email and "@" not in email:
        raise WorkflowError("That email address doesn't look right.")
    if email and Staff.query.filter_by(email=email).first():
        raise WorkflowError("That email address is already used.", 409)

    password = data.get("password") or temporary_password()
    check_password_rules(password, allow_weak)
    person = Staff(username=username, full_name=full_name, email=email,
                   phone=clean_text(data.get("phone"), 40),
                   password_hash=generate_password_hash(password),
                   must_change_password=not data.get("password"),
                   created_by=actor.id if actor else None,
                   updated_by=actor.id if actor else None)
    db.session.add(person)
    db.session.flush()
    _apply_access(person, actor, data.get("access"))
    audit.record("USER_CREATED", actor, entity="staff", entity_id=person.id,
                 details=person.to_dict())
    return person, password


def update_staff(actor, staff_id, data):
    if not actor.can("user.manage"):
        raise WorkflowError("Your access does not allow managing accounts.", 403)
    person = db.session.query(Staff).filter_by(id=staff_id).with_for_update().first()
    if not person:
        raise WorkflowError("Account not found.", 404)
    before = person.to_dict()

    if "full_name" in data:
        person.full_name = clean_text(data["full_name"], 120) or person.full_name
    if "email" in data:
        email = clean_text(data["email"], 120)
        if email and "@" not in email:
            raise WorkflowError("That email address doesn't look right.")
        if email and Staff.query.filter(Staff.id != person.id, Staff.email == email).first():
            raise WorkflowError("That email address is already used.", 409)
        person.email = email
    if "phone" in data:
        person.phone = clean_text(data["phone"], 40)
    if "is_active" in data:
        active = str(data["is_active"]).lower() in ("1", "true", "yes", "y", "on")
        if not active and person.id == actor.id:
            raise WorkflowError("You cannot switch off your own account.", 403)
        person.is_active = active
    if "access" in data:
        if person.id == actor.id:
            raise WorkflowError("You cannot change your own access.", 403)
        _apply_access(person, actor, data["access"])
    person.updated_by = actor.id
    db.session.flush()
    audit.record("USER_UPDATED", actor, entity="staff", entity_id=person.id,
                 details={"before": before, "after": person.to_dict()})
    return person


def reset_password(actor, staff_id):
    if not actor.can("user.manage"):
        raise WorkflowError("Your access does not allow managing accounts.", 403)
    person = db.session.get(Staff, staff_id)
    if not person:
        raise WorkflowError("Account not found.", 404)
    password = temporary_password()
    person.password_hash = generate_password_hash(password)
    person.must_change_password = True
    person.updated_by = actor.id
    db.session.flush()
    audit.record("USER_PASSWORD_RESET", actor, entity="staff", entity_id=person.id,
                 details={"username": person.username})
    return person, password


def change_own_password(person, current_password, new_password):
    if not person.must_change_password:
        if not current_password or not check_password_hash(person.password_hash, current_password):
            raise WorkflowError("Your current password is not right.", 403)
    check_password_rules(new_password)
    if current_password and current_password == new_password:
        raise WorkflowError("Choose a password different from the current one.")
    person.password_hash = generate_password_hash(new_password)
    person.must_change_password = False
    person.updated_by = person.id
    db.session.flush()
    audit.record("PASSWORD_CHANGED", person, entity="staff", entity_id=person.id,
                 details={"username": person.username})
    return person
