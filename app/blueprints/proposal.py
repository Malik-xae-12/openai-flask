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
from app.services.ai_service import (
    get_or_create_vector_store,
    run_proposal_workflow,
    upload_file_to_vector_store,
)

proposal_bp = Blueprint("proposal", __name__)


# ---------------------------------------------------------------------------
# Chat CRUD
# ---------------------------------------------------------------------------

@proposal_bp.route("/chats", methods=["GET"])
def list_proposal_chats():
    return jsonify({"chats": list_chats(mode="proposal")})


@proposal_bp.route("/chats", methods=["POST"])
def create_proposal_chat():
    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "New Chat").strip() or "New Chat"
    chat_id = create_chat(title=title, mode="proposal")
    return jsonify(get_chat(chat_id)), 201


@proposal_bp.route("/chats/<int:chat_id>", methods=["GET"])
def get_proposal_chat(chat_id):
    chat = get_chat(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    return jsonify({"chat": chat, "messages": get_messages(chat_id)})


@proposal_bp.route("/chats/<int:chat_id>", methods=["DELETE"])
def delete_proposal_chat(chat_id):
    delete_chat(chat_id)
    return jsonify({"deleted": True})


# ---------------------------------------------------------------------------
# Send message
# ---------------------------------------------------------------------------

@proposal_bp.route("/chats/<int:chat_id>/messages", methods=["POST"])
def send_proposal_message(chat_id):
    chat = get_chat(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    user_text = (request.form.get("input_text") or "").strip() or "evaluate"
    uploaded = request.files.get("file")
    upload_status = None

    # Persist user message and auto-title
    add_message(chat_id, "user", user_text)
    if chat.get("title") == "New Chat":
        update_chat(chat_id, title=user_text[:48] or "New Chat")

    async def _run():
        nonlocal upload_status
        chat_data = get_chat(chat_id) or {}

        # Upload new file into the SAME vector store (created once per chat)
        if uploaded:
            vs_id = await get_or_create_vector_store(
                chat_id, chat_data.get("vector_store_id")
            )
            upload_status = await upload_file_to_vector_store(uploaded, vs_id)
            update_chat(
                chat_id,
                last_upload_name=uploaded.filename,
                openai_file_id=upload_status["file_id"],
                vector_store_id=vs_id,
            )
            chat_data = get_chat(chat_id) or {}

        history = build_conversation_history(get_messages(chat_id))
        return await run_proposal_workflow(
            user_text,
            history,
            vector_store_id=chat_data.get("vector_store_id"),
        )

    output_text = asyncio.run(_run())
    add_message(chat_id, "assistant", output_text)

    return jsonify(
        {
            "output_text": output_text,
            "upload_status": upload_status,
            "chat_id": chat_id,
            "messages": get_messages(chat_id),
            "chat": get_chat(chat_id),
        }
    )