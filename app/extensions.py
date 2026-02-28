from types import SimpleNamespace

from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from openai import AsyncOpenAI

db = SQLAlchemy()
migrate = Migrate()

client = AsyncOpenAI()
ctx = SimpleNamespace(guardrail_llm=client)