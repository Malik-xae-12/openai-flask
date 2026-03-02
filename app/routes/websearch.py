import asyncio

from flask import Blueprint, jsonify, request

from app.services.chat_service import (
    add_message,
    build_conversation_history,
    create_chat,
    delete_chat,
    get_chat,
    get_messages,
    list_chats,
    update_chat,
)
from app.services.ai_service import run_websearch_workflow

websearch_bp = Blueprint("websearch", __name__)


# ---------------------------------------------------------------------------
# Chat CRUD
# ---------------------------------------------------------------------------

@websearch_bp.route("/chats", methods=["GET"])
def list_websearch_chats():
    return jsonify({"chats": list_chats(mode="websearch")})


@websearch_bp.route("/chats", methods=["POST"])
def create_websearch_chat():
    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "New Chat").strip() or "New Chat"
    chat_id = create_chat(title=title, mode="websearch")
    return jsonify(get_chat(chat_id)), 201


@websearch_bp.route("/chats/<int:chat_id>", methods=["GET"])
def get_websearch_chat(chat_id):
    chat = get_chat(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    return jsonify({"chat": chat, "messages": get_messages(chat_id)})


@websearch_bp.route("/chats/<int:chat_id>", methods=["DELETE"])
def delete_websearch_chat(chat_id):
    delete_chat(chat_id)
    return jsonify({"deleted": True})


# ---------------------------------------------------------------------------
# Send message
# ---------------------------------------------------------------------------

@websearch_bp.route("/chats/<int:chat_id>/messages", methods=["POST"])
def send_websearch_message(chat_id):
    chat = get_chat(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    payload = request.get_json(silent=True) or {}
    user_text = (payload.get("input_text") or "").strip()
    if not user_text:
        return jsonify({"error": "input_text is required"}), 400

    add_message(chat_id, "user", user_text)
    if chat.get("title") == "New Chat":
        update_chat(chat_id, title=user_text[:48] or "New Chat")

    history = build_conversation_history(get_messages(chat_id))
    output_text = asyncio.run(run_websearch_workflow(user_text, history))
    add_message(chat_id, "assistant", output_text)

    return jsonify(
        {
            "output_text": output_text,
            "chat_id": chat_id,
            "messages": get_messages(chat_id),
            "chat": get_chat(chat_id),
        }
    )
