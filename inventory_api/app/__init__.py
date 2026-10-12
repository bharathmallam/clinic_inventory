from flask import Flask, jsonify
from sqlalchemy.exc import DBAPIError, IntegrityError
from werkzeug.exceptions import HTTPException

from .config import DEFAULT_SECRET, Config
from .extensions import db, migrate
from .services.core import WorkflowError

# MySQL reports a failed CHECK constraint (3819) and a blocked read-only table
# (trigger SIGNAL, 1644) as OperationalError rather than IntegrityError.
MYSQL_RULE_ERRORS = {3819, 1644}


def is_rule_violation(err):
    if isinstance(err, IntegrityError):
        return True
    args = getattr(err.orig, "args", ())
    return bool(args) and args[0] in MYSQL_RULE_ERRORS


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    if app.config["APP_ENV"] == "production" and app.config["SECRET_KEY"] == DEFAULT_SECRET:
        raise RuntimeError("Set SECRET_KEY before running in production.")

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)

    from .routes import admin, auth_routes, orders, products, purchasing, requests
    for module in (auth_routes, products, requests, admin, purchasing, orders):
        app.register_blueprint(module.bp)

    @app.errorhandler(WorkflowError)
    def workflow_error(err):
        db.session.rollback()
        return jsonify(error=err.message), err.status

    @app.errorhandler(DBAPIError)
    def database_error(err):
        db.session.rollback()
        if is_rule_violation(err):
            # A database rule caught something the code did not: nothing was saved.
            app.logger.warning("Rule violation: %s", err.orig)
            return jsonify(error="That change breaks a data rule and was not saved."), 409
        app.logger.exception(err)
        return jsonify(error="Something went wrong. Please try again."), 500

    @app.errorhandler(HTTPException)
    def http_error(err):
        return jsonify(error=err.description), err.code

    @app.errorhandler(Exception)
    def unexpected(err):
        db.session.rollback()
        app.logger.exception(err)
        return jsonify(error="Something went wrong. Please try again."), 500

    @app.get("/api/health")
    def health():
        return jsonify(status="ok", version=app.config["APP_VERSION"])

    return app
