"""
ContentCrew - Flask entry point.

Run with:   python app.py
Then open:  http://127.0.0.1:5000

This file only wires the pieces together. The work lives in:
  api/        HTTP endpoints (thin: validate input, call a service, return JSON)
  services/   workflow runner and the UI event model
  storage/    SQLite database and repositories
  graph/      LangGraph state, runtime context and the workflow graph
  supervisor/ agents/ nodes/ tools/   the agent workflow (see README)
  frontend/   landing page, onboarding and the dashboard
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from flask import Flask, redirect, request, send_from_directory

from api import register_api
from api.errors import register_error_handlers
from config import FRONTEND_DIR, Settings, load_settings
from services.workflow_runner import WorkflowRunner
from storage.database import Database
from storage.repositories import CompanyRepository

log = logging.getLogger("contentcrew")

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])


def configure_logging(settings: Settings) -> None:
    root = logging.getLogger()
    if getattr(root, "_contentcrew_configured", False):
        return
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        settings.log_dir / "contentcrew.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)

    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)
    root._contentcrew_configured = True  # type: ignore[attr-defined]


def create_app(settings: Settings | None = None, llm_factory=None) -> Flask:
    """`llm_factory` replaces the real language model (used by tests)."""
    settings = settings or load_settings()
    configure_logging(settings)

    app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="/static")
    app.config.update(
        SECRET_KEY=settings.secret_key,
        MAX_CONTENT_LENGTH=512 * 1024,  # 512 KB is plenty for any request this app takes
    )
    app.json.sort_keys = False

    db = Database(settings.database_path)
    db.initialize()
    app.extensions["contentcrew.settings"] = settings
    app.extensions["contentcrew.db"] = db
    app.extensions["contentcrew.runner"] = WorkflowRunner(db, settings, llm_factory=llm_factory)

    register_error_handlers(app)
    register_api(app)
    _register_pages(app, db)
    _register_security_headers(app)

    configured = [name for name, ok in settings.integrations.items() if ok]
    log.info("Integrations configured: %s", ", ".join(configured) or "none yet")
    return app


def _register_pages(app: Flask, db: Database) -> None:
    def page(name: str):
        return send_from_directory(FRONTEND_DIR, name)

    @app.get("/")
    def landing():
        return page("index.html")

    @app.get("/onboarding")
    def onboarding():
        return page("onboarding.html")

    @app.get("/app")
    def dashboard():
        if not CompanyRepository(db).is_onboarded():
            return redirect("/onboarding")
        return page("app.html")

    @app.get("/blog/<slug>")
    def published_post(slug: str):
        return page("post.html")

    # POST /blog: the blog API the Publish Blog tool calls (same handler as POST /api/blog).
    from api.blog_routes import publish_blog
    app.add_url_rule("/blog", endpoint="blog_api_publish", view_func=publish_blog, methods=["POST"])

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(FRONTEND_DIR / "assets", "favicon.svg", mimetype="image/svg+xml")


def _register_security_headers(app: Flask) -> None:
    @app.after_request
    def add_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


if __name__ == "__main__":
    settings = load_settings()
    app = create_app(settings)
    print(f"\n  ContentCrew is running at http://{settings.host}:{settings.port}\n")
    app.run(host=settings.host, port=settings.port, debug=settings.debug, threaded=True)
