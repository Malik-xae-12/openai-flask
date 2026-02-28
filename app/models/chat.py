from datetime import datetime, timezone

from app.extensions import db


def _isoformat(dt_value):
    if not dt_value:
        return None
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value.isoformat()


class Chat(db.Model):
    __tablename__ = "chats"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    mode = db.Column(db.String(50), nullable=False, default="with_pdf")
    context_text = db.Column(db.Text, nullable=False, default="")
    last_upload_name = db.Column(db.String(255))
    openai_file_id = db.Column(db.String(255))
    vector_store_id = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    messages = db.relationship(
        "Message",
        backref="chat",
        cascade="all, delete-orphan",
        lazy=True,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "mode": self.mode,
            "context_text": self.context_text,
            "last_upload_name": self.last_upload_name,
            "openai_file_id": self.openai_file_id,
            "vector_store_id": self.vector_store_id,
            "created_at": _isoformat(self.created_at),
        }


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey("chats.id"), nullable=False)
    role = db.Column(db.String(32), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "created_at": _isoformat(self.created_at),
        }