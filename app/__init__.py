from flask import Flask

from .config import Config
from .extensions import db, migrate
from .routes.main import main_bp


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(Config)
    app.secret_key = app.config["SECRET_KEY"]

    db.init_app(app)
    migrate.init_app(app, db)

    from . import models  # noqa: F401

    app.register_blueprint(main_bp)
    return app


app = create_app()
