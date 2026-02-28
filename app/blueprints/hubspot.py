from flask import Blueprint, jsonify, request

hubspot_bp = Blueprint("hubspot", __name__)


@hubspot_bp.route("/status", methods=["GET"])
def status():
    return jsonify({"status": "coming_soon", "connected": False})


@hubspot_bp.route("/connect", methods=["POST"])
def connect():
    """Placeholder: will accept an API key and validate with HubSpot."""
    payload = request.get_json(silent=True) or {}
    if not payload.get("api_key"):
        return jsonify({"error": "api_key required"}), 400
    # TODO: validate against HubSpot API
    return jsonify({"message": "HubSpot integration endpoint ready — coming soon."})


@hubspot_bp.route("/contacts", methods=["GET"])
def list_contacts():
    """Placeholder: will return CRM contacts."""
    return jsonify({"contacts": [], "message": "HubSpot integration coming soon."})