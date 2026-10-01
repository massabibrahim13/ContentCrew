"""REST API blueprints, all mounted under /api."""

from flask import Flask


def register_api(app: Flask) -> None:
    from api import approval_routes, blog_routes, chat_routes, context_routes, status_routes

    for module in (context_routes, chat_routes, status_routes, approval_routes, blog_routes):
        app.register_blueprint(module.bp, url_prefix="/api")
