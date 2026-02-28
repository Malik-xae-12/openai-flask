from app.extensions import db
from app.models.chat import Chat, Message


def build_conversation_history(messages):
    """
    Convert flat message dicts into the structured format expected by
    the OpenAI Responses API (input_text for user, output_text for assistant).
    """
    history = []
    for msg in messages or []:
        role = msg["role"]
        content_type = "output_text" if role == "assistant" else "input_text"
        history.append(
            {
                "role": role,
                "content": [{"type": content_type, "text": msg["content"]}],
            }
        )
    return history


def create_chat(title="New Chat", mode="with_pdf", context_text=""):
    chat = Chat(title=title, mode=mode, context_text=context_text)
    db.session.add(chat)
    db.session.commit()
    return chat.id


def list_chats(mode=None):
    q = Chat.query
    if mode:
        q = q.filter_by(mode=mode)
    chats = q.order_by(Chat.id.desc()).all()
    return [chat.to_dict() for chat in chats]


def get_chat(chat_id):
    chat = Chat.query.get(chat_id)
    return chat.to_dict() if chat else None


def update_chat(chat_id, **fields):
    chat = Chat.query.get(chat_id)
    if not chat:
        return
    for key, value in fields.items():
        if hasattr(chat, key):
            setattr(chat, key, value)
    db.session.commit()


def delete_chat(chat_id):
    chat = Chat.query.get(chat_id)
    if chat:
        db.session.delete(chat)
        db.session.commit()


def add_message(chat_id, role, content):
    message = Message(chat_id=chat_id, role=role, content=content)
    db.session.add(message)
    db.session.commit()


def get_messages(chat_id):
    messages = (
        Message.query
        .filter_by(chat_id=chat_id)
        .order_by(Message.id.asc())
        .all()
    )
    return [msg.to_dict() for msg in messages]


__all__ = [
    "add_message",
    "build_conversation_history",
    "create_chat",
    "delete_chat",
    "get_chat",
    "get_messages",
    "list_chats",
    "update_chat",
]