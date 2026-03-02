import json
import logging
import os
import re
import requests

from app.extensions import client
from app.hubmetadetails import PROPERTIES
from app.instruction import INSTRUCTIONS

chat_history = []
last_query_state = {
    "object_type": None,
    "properties": [],
    "search_criteria": {},
    "fetch_all": False,
    "paging_token": None,
}

logger = logging.getLogger(__name__)


def _get_headers() -> dict:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise ValueError("Missing HUBSPOT_ACCESS_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def get_company_name(company_id: str | None) -> str:
    if not company_id:
        return "No company associated"
    try:
        r = requests.get(
            f"https://api.hubapi.com/crm/v3/objects/companies/{company_id}?properties=name",
            headers=_get_headers(),
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("properties", {}).get("name", "Unknown")
    except Exception:
        return "Error fetching company"


def _summarize_history() -> str:
    return "\n".join(
        f"User: {h['user']}\nAI: {h.get('summary', 'Response given')}"
        for h in chat_history[-4:]
    )


def _summarize_curated() -> str:
    return "\n".join([f"{k}: {', '.join(v)}" for k, v in PROPERTIES.items()])


def _build_prompt(question: str) -> str:
    history_summary = _summarize_history()
    curated_summary = _summarize_curated()

    return (
        "You are a smart HubSpot assistant. Understand user intent and map to HubSpot objects.\n\n"
        f"{INSTRUCTIONS}\n\n"
        "Previous conversation (last few turns):\n"
        f"{history_summary}\n\n"
        f"Current question: \"{question}\"\n\n"
        "Available objects and basic fields:\n"
        f"{curated_summary}\n\n"
        "Rules:\n"
        "- \"next\", \"more\", \"show more\", \"continue\", \"page 2\" -> return {\"is_next\": true}\n"
        "- \"all details\", \"everything\", \"full record\", \"complete info\", \"every field\" -> set \"fetch_all\": true\n"
        "- \"how many\", \"count\", \"number of\" -> set \"count_only\": true\n"
        "- Leads -> usually contacts with lifecyclestage = \"lead\"\n"
        "- Company name for deals -> include \"hs_primary_associated_company\"\n"
        "- For numeric comparisons:\n"
        "  - \"over\", \"greater than\", \"more than\" → operator: \"GT\"\n"
        "  - \"at least\", \">=\" → operator: \"GTE\"\n"
        "  - \"less than\" → operator: \"LT\"\n"
        "  - \"at most\", \"<=\" → operator: \"LTE\"\n"
        "- Return ONLY clean JSON. No extra text.\n\n"
        "Example with operator:\n"
        "{\n"
        "  \"object_type\": \"deals\",\n"
        "  \"properties\": [\"dealname\", \"amount\"],\n"
        "  \"search_criteria\": {\"amount\": {\"operator\": \"GT\", \"value\": \"100000\"}},\n"
        "  \"fetch_all\": false,\n"
        "  \"count_only\": false,\n"
        "  \"is_next\": false,\n"
        "  \"limit\": 5\n"
        "}\n\n"
        "If unclear: {\"error\": \"Please clarify your question\"}"
    )


async def analyze_question(question: str) -> dict:
    prompt = _build_prompt(question)
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.2,
            max_tokens=500,
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```json"):
            text = text.split("```json", 1)[1].split("```", 1)[0].strip()
        data = json.loads(text)
        if "limit" not in data:
            limit = _extract_limit(question)
            if limit:
                data["limit"] = limit
        return data
    except Exception as exc:
        return {"error": f"OpenAI error: {str(exc)}"}


def _extract_limit(question: str) -> int | None:
    lowered = question.strip().lower()
    match = re.search(r"\b(top|first)\s+(\d+)\b", lowered)
    if match:
        return int(match.group(2))
    match = re.search(r"\b(limit|show|give me)\s+(\d+)\b", lowered)
    if match:
        return int(match.group(2))
    return None


def heuristic_parse_question(question: str) -> dict:
    lowered = question.strip().lower()
    if not lowered:
        return {"error": "Please be more specific (add name, email, stage, date...)"}

    # Simple pagination detection
    pagination_match = re.fullmatch(r"\s*(next|more|show more|continue|another page|page\s*\d+)\s*", lowered)
    if pagination_match:
        return {"is_next": True}

    limit = _extract_limit(question)
    count_only = bool(re.search(r"\b(how many|count|number of|total)\b", lowered))

    object_type = None
    search_criteria = {}

    if re.search(r"\b(leads|lead list|people in lead)\b", lowered):
        object_type = "contacts"
        # No lifecyclestage filter since property is not present
    elif re.search(r"\bcontacts?\b", lowered):
        object_type = "contacts"
    elif re.search(r"\bcompanies|company\b", lowered):
        object_type = "companies"
    elif re.search(r"\bdeals?|pipeline\b", lowered):
        object_type = "deals"
    # Removed carts, orders, line_items, quotes, subscriptions

    if not object_type:
        return {"error": "Please be more specific (add name, email, stage, date...)"}

    return {
        "object_type": object_type,
        "properties": PROPERTIES.get(object_type, ["hs_object_id"]),
        "search_criteria": search_criteria,
        "fetch_all": False,
        "count_only": count_only,
        "is_next": False,
        "limit": limit,
    }


def fetch_hubspot(
    object_type: str,
    properties: list[str],
    search_criteria: dict | None = None,
    paging_token: str | None = None,
    fetch_all: bool = False,
    count_only: bool = False,
    limit: int | None = None,
) -> tuple[dict, str | None, int | None]:
    """
    Fetch records from HubSpot.
    Returns: (data, next_token, total)
    - total is only reliable when using search endpoint (with filters)
    """
    if object_type not in PROPERTIES:
        raise ValueError(f"Unknown object: {object_type}")

    valid = set(PROPERTIES[object_type])

    if count_only:
        selected = ["hs_object_id"]
        limit = 1
    elif fetch_all:
        selected = list(valid)
        limit = limit or 10
    else:
        selected = [p for p in properties if p in valid] or PROPERTIES.get(
            object_type, ["hs_object_id"]
        )
        limit = limit or 10

    limit = max(1, min(int(limit or 10), 100))

    headers = _get_headers()
    data = None
    total = None
    next_token = None

    if search_criteria:
        # Prepare filters — support both simple value and {"operator": ..., "value": ...}
        filters = []
        for k, v in search_criteria.items():
            if isinstance(v, dict) and "operator" in v and "value" in v:
                filters.append({
                    "propertyName": k,
                    "operator": v["operator"],
                    "value": str(v["value"])
                })
            else:
                filters.append({
                    "propertyName": k,
                    "operator": "EQ",
                    "value": str(v)
                })

        url = f"https://api.hubapi.com/crm/v3/objects/{object_type}/search"
        payload = {
            "properties": selected,
            "filterGroups": [{"filters": filters}],
            "limit": limit,
            "after": paging_token,
        }
        logger.info("HubSpot API POST %s payload=%s", url, payload)
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        total = data.get("total")
        next_token = data.get("paging", {}).get("next", {}).get("after")
    else:
        params = {
            "properties": ",".join(selected),
            "limit": limit,
            "after": paging_token
        }
        url = f"https://api.hubapi.com/crm/v3/objects/{object_type}"
        logger.info("HubSpot API GET %s params=%s", url, params)
        r = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        next_token = data.get("paging", {}).get("next", {}).get("after")

    if count_only and total is not None:
        return {"total": total}, None, total

    return data, next_token, total


def normalize_results(data: dict, object_type: str, fetch_all: bool) -> list[dict]:
    items = []
    for item in data.get("results", []):
        props = item.get("properties", {})
        name = (
            f"{props.get('firstname', '')} {props.get('lastname', '')}".strip()
            or props.get("dealname")
            or props.get("name")
            or props.get("email")
            or f"ID {item.get('id')}"
        )

        if fetch_all:
            selected = {k: v for k, v in sorted(props.items()) if v is not None}
        else:
            selected = {
                k: props[k]
                for k in PROPERTIES.get(object_type, ["hs_object_id"])
                if k in props and props[k] is not None
            }

        entry = {
            "id": item.get("id"),
            "name": name,
            "properties": selected,
        }

        if object_type == "deals":
            comp_id = props.get("hs_primary_associated_company")
            if comp_id:
                entry["company_name"] = get_company_name(comp_id)

        items.append(entry)

    return items


def count_records(object_type: str, search_criteria: dict | None) -> dict:
    if search_criteria:
        # Optimized: single call → gets total directly
        _, _, total = fetch_hubspot(
            object_type,
            ["hs_object_id"],
            search_criteria,
            None,
            fetch_all=False,
            count_only=True,
            limit=1,
        )
        if total is not None:
            return {
                "total": total,
                "pages": (total + 99) // 100,
                "source": "search_api"
            }
        else:
            return {"error": "Could not retrieve total count from HubSpot search."}

    else:
        # Fallback: paginate list endpoint
        total_count = 0
        current_paging = None
        page_num = 0
        MAX_PAGES = 10  # safety limit ≈ 1000 records

        while True:
            page_num += 1
            data, next_token, _ = fetch_hubspot(
                object_type,
                ["hs_object_id"],
                None,
                current_paging,
                fetch_all=False,
                count_only=False,
                limit=100,
            )
            page_items = len(data.get("results", []))
            total_count += page_items

            if page_num >= MAX_PAGES or not next_token:
                result = {
                    "total": total_count,
                    "pages": page_num,
                }
                if page_num >= MAX_PAGES:
                    result["warning"] = f"Count capped at {total_count} records (max {MAX_PAGES*100}). Add filters for accurate full count."
                return result

            current_paging = next_token


def update_query_state(
    object_type: str,
    properties: list[str],
    search_criteria: dict,
    fetch_all: bool,
    paging_token: str | None,
):
    last_query_state.update(
        {
            "object_type": object_type,
            "properties": properties,
            "search_criteria": search_criteria,
            "fetch_all": fetch_all,
            "paging_token": paging_token,
        }
    )


def record_history(question: str, object_type: str, fetch_all: bool):
    chat_history.append(
        {
            "user": question,
            "summary": f"Queried {object_type}, {'all' if fetch_all else 'basic'} fields",
        }
    )


__all__ = [
    "analyze_question",
    "count_records",
    "fetch_hubspot",
    "get_company_name",
    "heuristic_parse_question",
    "last_query_state",
    "normalize_results",
    "record_history",
    "update_query_state",
]