from types import SimpleNamespace

from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

import os
from openai import AsyncOpenAI
from dotenv import load_dotenv

db = SQLAlchemy()
migrate = Migrate()

load_dotenv()
api_key = os.environ.get("OPENAI_API_KEY")
if not api_key:
	raise RuntimeError("OPENAI_API_KEY is not set. Please set it in your .env file or environment.")
client = AsyncOpenAI(api_key=api_key)
ctx = SimpleNamespace(guardrail_llm=client)