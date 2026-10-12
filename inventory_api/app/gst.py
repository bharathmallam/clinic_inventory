"""Indian GST helpers. A GSTIN carries its own state, PAN and a check digit, so
those are derived from it here rather than typed (and mistyped) separately."""
import re

GSTIN_PATTERN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# GST state/UT codes (first two digits of a GSTIN)
STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory", "99": "Centre Jurisdiction",
}

# GST rates in use (percent)
GST_RATES = (0, 0.1, 0.25, 1, 1.5, 3, 5, 6, 7.5, 12, 18, 28, 40)


def normalise_gstin(value):
    return re.sub(r"\s+", "", str(value or "")).upper()


def _check_char(first14):
    total = 0
    for i, c in enumerate(first14):
        product = _CHARS.index(c) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _CHARS[(36 - total % 36) % 36]


def gstin_problem(value):
    """None if `value` is a valid GSTIN, otherwise a plain-language reason."""
    g = normalise_gstin(value)
    if len(g) != 15:
        return f"A GSTIN has 15 characters; this has {len(g)}."
    if not GSTIN_PATTERN.match(g):
        return "This is not in GSTIN format (2 digits, 10-character PAN, entity code, Z, check character)."
    if g[:2] not in STATES:
        return f"{g[:2]} is not a GST state code."
    if _check_char(g[:14]) != g[14]:
        return "The last (check) character doesn't match: one of the characters is mistyped."
    return None


def state_code(gstin):
    return gstin[:2] if gstin else None


def state_name(gstin):
    return STATES.get(state_code(gstin)) if gstin else None


def pan(gstin):
    return gstin[2:12] if gstin else None
