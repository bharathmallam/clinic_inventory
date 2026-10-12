import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DEFAULT_SECRET = "dev-only-change-me"


def _read_version():
    for path in (os.path.join(BASE_DIR, "VERSION"), os.path.join(BASE_DIR, "..", "VERSION")):
        if os.path.exists(path):
            return open(path).read().strip()
    return "unknown"


APP_VERSION = _read_version()
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "inventory_dev.db"))
# "development" on your own computer, "production" on the live server. Test logins
# (seed-dev-users) only work in development. With no DATABASE_URL set we are on
# the local SQLite file, so default to development there.
APP_ENV = os.environ.get("APP_ENV", "production" if "DATABASE_URL" in os.environ else "development")

# Every MySQL connection runs in strict mode (bad data is rejected instead of
# silently truncated) and in UTC, whatever the hosting server's defaults are.
MYSQL_SESSION = (
    "SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION', time_zone = '+00:00'"
)


def engine_options(url):
    options = {"pool_pre_ping": True}
    if url.startswith("mysql"):
        options.update(
            pool_recycle=280,  # MySQL closes idle connections; reconnect before it does
            isolation_level="REPEATABLE READ",
            connect_args={"init_command": MYSQL_SESSION},
        )
    return options


class Config:
    APP_ENV = APP_ENV
    APP_VERSION = APP_VERSION
    SECRET_KEY = os.environ.get("SECRET_KEY", DEFAULT_SECRET)
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_ENGINE_OPTIONS = engine_options(DATABASE_URL)
    # A sign-in ends after this many hours, counted from signing in; never more than 24.
    JWT_EXPIRY_HOURS = max(1, min(int(os.environ.get("JWT_EXPIRY_HOURS", "24")), 24))
    JSON_SORT_KEYS = False


class TestConfig(Config):
    """Tests run on in-memory SQLite by default. Set TEST_DATABASE_URL to run
    the same tests on MySQL (its tables are dropped after each test)."""
    TESTING = True
    APP_ENV = "development"
    SECRET_KEY = "test-secret-key-that-is-long-enough-for-hs256"
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite://")
    SQLALCHEMY_ENGINE_OPTIONS = engine_options(SQLALCHEMY_DATABASE_URI) if "TEST_DATABASE_URL" in os.environ else {}
