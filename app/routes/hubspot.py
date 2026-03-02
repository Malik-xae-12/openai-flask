import asyncio
import json
import logging
import re

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.models.chat import Chat, Message

from app.services.hubspot_service import (
    analyze_question,
    count_records,
    fetch_hubspot,
    heuristic_parse_question,
    last_query_state,
    normalize_results,
    record_history,
    update_query_state,
)

hubspot_bp = Blueprint("hubspot", __name__)
logger = logging.getLogger(__name__)


def _run_query(question: str):
    analysis = asyncio.run(analyze_question(question))
    if "error" in analysis:
        analysis = heuristic_parse_question(question)
        if "error" in analysis:
            return analysis, 400
    logger.info("HubSpot AI analysis: %s", analysis)

    is_next = analysis.get("is_next", False)
    if is_next:
        # Only allow pagination when the question is explicitly a pagination command.
        pagination_match = re.fullmatch(
            r"\s*(next|more|show more|continue|another page|page\s*\d+)\s*",
            question.strip().lower(),
        )
        if not pagination_match:
            is_next = False

    # If AI didn't provide a usable object_type, fall back to heuristic parsing.
    if not is_next and not analysis.get("object_type"):
        heuristic = heuristic_parse_question(question)
        if "error" in heuristic:
            return heuristic, 400
        analysis = {**analysis, **heuristic}
    fetch_all = analysis.get("fetch_all", False)
    count_only = analysis.get("count_only", False)
    limit = analysis.get("limit")

    if is_next:
        if not last_query_state.get("object_type"):
            return {"error": "No previous query to continue from."}, 400
        object_type = last_query_state["object_type"]
        properties = last_query_state["properties"]
        search_criteria = last_query_state["search_criteria"]
        paging_token = last_query_state["paging_token"]
    else:
        object_type = analysis.get("object_type")
        properties = analysis.get("properties", [])
        search_criteria = analysis.get("search_criteria", {})
        paging_token = None

    if not object_type:
        return {"error": "Could not understand the object type. Try again."}, 400

    try:
        if count_only:
            count_data = count_records(object_type, search_criteria)
            update_query_state(object_type, properties, search_criteria, fetch_all, None)
            record_history(question, object_type, fetch_all)
            return (
                {
                    "object_type": object_type,
                    "search_criteria": search_criteria,
                    "count": count_data["total"],
                    "pages": count_data["pages"],
                    "analysis": analysis,
                },
                200,
            )

        data, next_token, _ = fetch_hubspot(
            object_type,
            properties,
            search_criteria,
            paging_token,
            fetch_all,
            count_only=False,
            limit=limit,
        )
        results = normalize_results(data, object_type, fetch_all)
        update_query_state(object_type, properties, search_criteria, fetch_all, next_token)
        record_history(question, object_type, fetch_all)
        return (
            {
                "object_type": object_type,
                "search_criteria": search_criteria,
                "fetch_all": fetch_all,
                "results": results,
                "has_more": bool(next_token),
                "next_token": next_token,
                "analysis": analysis,
            },
            200,
        )
    except Exception as exc:
        return {"error": str(exc)}, 500


@hubspot_bp.route("/status", methods=["GET"])
def status():
    has_token = True
    error = None
    try:
        # Validate that the token is present without hitting the API.
        from app.services.hubspot_service import _get_headers

        _get_headers()
    except Exception as exc:
        has_token = False
        error = str(exc)

    return jsonify({"connected": has_token, "error": error})


@hubspot_bp.route("/query", methods=["POST"])
def query():
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400
    response, status = _run_query(question)
    return jsonify(response), status


@hubspot_bp.get("/chats")
def list_chats():
    chats = Chat.query.filter_by(mode="hubspot").order_by(Chat.created_at.desc()).all()
    return jsonify({"chats": [c.to_dict() for c in chats]})


@hubspot_bp.post("/chats")
def create_chat():
    data = request.get_json(silent=True) or {}
    chat = Chat(title=data.get("title", "New Chat"), mode="hubspot", context_text="")
    db.session.add(chat)
    db.session.commit()
    return jsonify(chat.to_dict()), 201


@hubspot_bp.get("/chats/<int:chat_id>")
def get_chat(chat_id):
    chat = Chat.query.get_or_404(chat_id)
    if chat.mode != "hubspot":
        return jsonify({"error": "Chat not found"}), 404
    messages = Message.query.filter_by(chat_id=chat_id).order_by(Message.created_at).all()
    return jsonify({
        "chat": chat.to_dict(),
        "messages": [m.to_dict() for m in messages],
    })


@hubspot_bp.delete("/chats/<int:chat_id>")
def delete_chat(chat_id):
    chat = Chat.query.get_or_404(chat_id)
    if chat.mode != "hubspot":
        return jsonify({"error": "Chat not found"}), 404
    Message.query.filter_by(chat_id=chat_id).delete()
    db.session.delete(chat)
    db.session.commit()
    return jsonify({"deleted": chat_id})


@hubspot_bp.post("/chats/<int:chat_id>/messages")
def send_message(chat_id):
    chat = Chat.query.get_or_404(chat_id)
    if chat.mode != "hubspot":
        return jsonify({"error": "Chat not found"}), 404

    import json as json_module

    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    response, status = _run_query(question)
    # Persist messages (store assistant response as JSON string)
    db.session.add(Message(chat_id=chat_id, role="user", content=question))
    db.session.add(Message(chat_id=chat_id, role="assistant", content=json_module.dumps(response)))
    if not chat.title or chat.title == "New Chat":
        chat.title = question[:60]
    db.session.commit()

    return jsonify({
        "response": response,
        "status": status,
        "chat": chat.to_dict(),
    }), status
