import logging

from flask import Flask
from .extensions import db, migrate


def create_app(config=None):
    app = Flask(__name__)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app.logger.setLevel(logging.INFO)

    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///ubti.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = "change-me-in-production"

    if config:
        app.config.update(config)

    db.init_app(app)
    migrate.init_app(app, db)

    # Import models so Flask-Migrate can detect them
    from .models.chat import Chat, Message  # noqa: F401

    # Register routes
    from .routes.main import main_bp
    from .routes.proposal import proposal_bp
    from .routes.websearch import websearch_bp
    from .routes.hubspot import hubspot_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(proposal_bp, url_prefix="/api/proposal")
    app.register_blueprint(websearch_bp, url_prefix="/api/websearch")
    app.register_blueprint(hubspot_bp, url_prefix="/api/hubspot")

    return app