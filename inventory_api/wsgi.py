"""WSGI entry point: Gunicorn on the Lightsail server, and `flask --app wsgi` locally.

    gunicorn --bind 127.0.0.1:8000 wsgi:application

Nginx serves the website and passes every /api/… request through with its path
unchanged, so the routes (which all start with /api) need no rewriting here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app  # noqa: E402

application = create_app()
