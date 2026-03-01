"""
proposal.py

Multi-file support per chat:
- context_text (already in Chat model) stores a JSON list of all file_ids uploaded in this chat
- openai_file_id stores the LATEST file_id (for display/badge)
- last_upload_name stores the LATEST filename (for badge display)

No schema changes needed — reuses existing columns.
"""

import json

from flask import Blueprint, request, jsonify
from app.extensions import db
from app.models.chat import Chat, Message
from app.services.ai_service import (
    upload_file_to_vector_store,
    delete_all_files_in_vector_store,
    run_proposal_workflow,
)

proposal_bp = Blueprint("proposal", __name__, url_prefix="/api/proposal")


def _get_file_ids(chat: Chat) -> list[str]:
    """
    Returns all file IDs uploaded in this chat, ordered oldest → newest.
    Stored as JSON in context_text e.g. '["file-abc", "file-xyz"]'
    """
    try:
        if chat.context_text:
            parsed = json.loads(chat.context_text)
            if isinstance(parsed, list):
                return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: if old record has openai_file_id but no context_text list
    if chat.openai_file_id:
        return [chat.openai_file_id]
    return []


def _set_file_ids(chat: Chat, file_ids: list[str]):
    """Persist the file_ids list into context_text."""
    chat.context_text = json.dumps(file_ids)


def _build_history(messages):
    history = []
    for m in messages:
        history.append({
            "role": m.role,
            "content": [{"type": "input_text" if m.role == "user" else "output_text",
                         "text": m.content}],
        })
    return history


# ── GET /api/proposal/chats ───────────────────────────────────────────────────
@proposal_bp.get("/chats")
def list_chats():
    chats = Chat.query.filter_by(mode="proposal").order_by(Chat.created_at.desc()).all()
    return jsonify({"chats": [c.to_dict() for c in chats]})


# ── POST /api/proposal/chats ──────────────────────────────────────────────────
@proposal_bp.post("/chats")
def create_chat():
    data = request.get_json(silent=True) or {}
    chat = Chat(title=data.get("title", "New Chat"), mode="proposal", context_text="[]")
    db.session.add(chat)
    db.session.commit()
    return jsonify(chat.to_dict()), 201


# ── GET /api/proposal/chats/<id> ──────────────────────────────────────────────
@proposal_bp.get("/chats/<int:chat_id>")
def get_chat(chat_id):
    chat = Chat.query.get_or_404(chat_id)
    messages = Message.query.filter_by(chat_id=chat_id).order_by(Message.created_at).all()
    return jsonify({
        "chat": chat.to_dict(),
        "messages": [m.to_dict() for m in messages],
    })


# ── DELETE /api/proposal/chats/<id> ──────────────────────────────────────────
@proposal_bp.delete("/chats/<int:chat_id>")
def delete_chat(chat_id):
    chat = Chat.query.get_or_404(chat_id)
    Message.query.filter_by(chat_id=chat_id).delete()
    db.session.delete(chat)
    db.session.commit()
    return jsonify({"deleted": chat_id})


# ── POST /api/proposal/chats/<id>/messages ───────────────────────────────────
@proposal_bp.post("/chats/<int:chat_id>/messages")
async def send_message(chat_id):
    chat = Chat.query.get_or_404(chat_id)

    input_text = request.form.get("input_text", "").strip()
    uploaded_file = request.files.get("file")
    upload_status = None

    # ── Handle file upload ────────────────────────────────────────────────────
    if uploaded_file and uploaded_file.filename:
        meta = await upload_file_to_vector_store(uploaded_file)

        # Append new file_id to the chat's list (supports multiple files)
        file_ids = _get_file_ids(chat)
        if meta["file_id"] not in file_ids:
            file_ids.append(meta["file_id"])
        _set_file_ids(chat, file_ids)

        # Always update latest file references for badge display
        chat.openai_file_id = meta["file_id"]
        chat.last_upload_name = meta["filename"]
        db.session.commit()

        upload_status = {
            "filename": meta["filename"],
            "file_id": meta["file_id"],
            "total_files": len(file_ids),
        }

    # ── Get all file IDs for this chat ────────────────────────────────────────
    file_ids = _get_file_ids(chat)

    # ── Build history ─────────────────────────────────────────────────────────
    past_messages = Message.query.filter_by(chat_id=chat_id).order_by(Message.created_at).all()
    history = _build_history(past_messages)

    # ── Run workflow ──────────────────────────────────────────────────────────
    file_ids_for_qa = [chat.openai_file_id] if chat.openai_file_id else file_ids
    output = await run_proposal_workflow(
        user_text=input_text or "evaluate",
        history=history,
        file_ids=file_ids_for_qa,               # latest file only for Q&A scope
        latest_file_name=chat.last_upload_name, # most recent filename
    )

    # ── Persist messages ──────────────────────────────────────────────────────
    if input_text:
        db.session.add(Message(chat_id=chat_id, role="user", content=input_text))
    db.session.add(Message(chat_id=chat_id, role="assistant", content=output))

    if not chat.title or chat.title == "New Chat":
        chat.title = (input_text or "Evaluation")[:60]
    db.session.commit()

    return jsonify({
        "output_text": output,
        "upload_status": upload_status,
        "chat": chat.to_dict(),
    })


# ── POST /api/proposal/reset-files ───────────────────────────────────────────
@proposal_bp.post("/reset-files")
async def reset_files():
    """
    Delete ALL files from the global vector store.
    Clears file references on all proposal chats.
    """
    result = await delete_all_files_in_vector_store()

    # Clear file references on all proposal chats
    chats = Chat.query.filter_by(mode="proposal").all()
    for chat in chats:
        chat.openai_file_id = None
        chat.last_upload_name = None
        chat.context_text = "[]"
    db.session.commit()

    return jsonify({
        "success": True,
        "deleted_files": result.get("deleted", 0),
        "failed_files": result.get("failed", 0),
        "vector_store_id": result.get("vector_store_id", ""),
        "message": f"Deleted {result.get('deleted', 0)} file(s) from vector store."
    })