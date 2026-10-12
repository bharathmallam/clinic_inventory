"""Shared plumbing for every service: errors, transactions, small parsers.

Transactions (ACID): service functions never commit. Callers wrap them in
`atomic()`, which commits once at the end or rolls everything back on any
error — so a change, its stock movement and its audit row are saved together
or not at all.
"""
import re
from contextlib import contextmanager
from datetime import date, datetime

from ..extensions import db


class WorkflowError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@contextmanager
def atomic():
    """One unit of work: commit on success, roll back on any exception."""
    try:
        yield
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def clean_text(value, limit=255):
    text = " ".join(str(value or "").split())
    return text[:limit] or None


def clean_note(value):
    return clean_text(value, 255)


def positive_int(value, field, maximum=None):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        raise WorkflowError(f"{field} must be a whole number.")
    if number <= 0:
        raise WorkflowError(f"{field} must be greater than zero.")
    if maximum is not None and number > maximum:
        raise WorkflowError(f"{field} cannot be more than {maximum}.")
    return number


def whole_number(value, field, default=0, minimum=0):
    if value in (None, ""):
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise WorkflowError(f"{field} must be a whole number.")
    if number < minimum:
        raise WorkflowError(f"{field} cannot be less than {minimum}.")
    return number


DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d %b %Y", "%d-%b-%Y", "%m/%Y", "%m-%Y")


def parse_date(value, field, end_of_month=False):
    """Accept what people actually type, including a bill's 'MM/YYYY' expiry."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if fmt in ("%m/%Y", "%m-%Y") or end_of_month:
            next_month = parsed.replace(day=28) + __import__("datetime").timedelta(days=4)
            parsed = next_month - __import__("datetime").timedelta(days=next_month.day)
        return parsed
    raise WorkflowError(f"{field}: use a date like 2026-09-30 or 30/09/2026.")


def key_of(name):
    """A name without case or punctuation, for catching near-duplicates."""
    return " ".join(re.sub(r"[^A-Z0-9 ]+", " ", str(name or "").upper()).split())
