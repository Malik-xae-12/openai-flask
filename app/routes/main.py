import asyncio
import json
import re
from dotenv import load_dotenv
from flask import Blueprint, jsonify, render_template, request

from ..extensions import client
from ..services.chat_service import (
    add_message,
    build_conversation_history,
    create_chat,
    get_chat,
    get_messages,
    list_chats,
    update_chat,
)

load_dotenv()

main_bp = Blueprint("main", __name__)

# ---------------------------------------------------------------------------
# Guardrails (lightweight, inline – no SDK dependency)
# ---------------------------------------------------------------------------
BLOCKED_MODERATION_CATEGORIES = [
    "sexual/minors",
    "hate/threatening",
    "harassment/threatening",
    "self-harm/instructions",
    "violence/graphic",
    "illicit/violent",
]

PII_PATTERNS = {
    "CREDIT_CARD": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "US_SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "US_PASSPORT": re.compile(r"\b[A-Z]{1,2}\d{6,9}\b"),
    "US_BANK_NUMBER": re.compile(r"\b\d{8,17}\b"),
}


def scrub_pii(text: str) -> tuple[str, list[str]]:
    """Replace PII with placeholders. Returns (scrubbed_text, detected_entity_types)."""
    detected = []
    for entity, pattern in PII_PATTERNS.items():
        if pattern.search(text):
            detected.append(entity)
            text = pattern.sub(f"[{entity}]", text)
    return text, detected


async def run_moderation(text: str) -> list[str]:
    """Call OpenAI moderation endpoint. Returns list of flagged categories."""
    try:
        resp = await client.moderations.create(input=text)
        result = resp.results[0]
        flagged = []
        cats = result.categories.model_dump()
        for cat in BLOCKED_MODERATION_CATEGORIES:
            key = cat.replace("/", "_").replace("-", "_")
            if cats.get(key) or cats.get(cat):
                flagged.append(cat)
        return flagged
    except Exception:
        return []


async def apply_guardrails(text: str):
    """
    Returns dict:
      blocked      – True if request must be rejected
      safe_text    – PII-scrubbed text to use downstream
      fail_output  – dict describing what failed (only set when blocked=True)
    """
    # 1. PII scrub (mask, don't block)
    safe_text, pii_entities = scrub_pii(text)

    # 2. Moderation check
    flagged_cats = await run_moderation(safe_text)

    if flagged_cats:
        return {
            "blocked": True,
            "safe_text": safe_text,
            "fail_output": {
                "moderation": {"failed": True, "flagged_categories": flagged_cats},
                "pii": {"detected": pii_entities},
            },
        }

    return {"blocked": False, "safe_text": safe_text, "fail_output": None}


# ---------------------------------------------------------------------------
# Vector store helpers  – FIX #1, #2, #3, #4
# Reuse the chat's existing vector store; only create a new one on first upload.
# ---------------------------------------------------------------------------

async def get_or_create_vector_store(chat_id: int, chat_data: dict) -> str:
    """Return the existing vector_store_id for the chat, or create a fresh one."""
    vs_id = (chat_data or {}).get("vector_store_id")
    if vs_id:
        return vs_id
    vector_store = await client.vector_stores.create(name=f"chat_store_{chat_id}")
    update_chat(chat_id, vector_store_id=vector_store.id)
    return vector_store.id


async def upload_file_to_vector_store(uploaded_file, vector_store_id: str) -> dict:
    """
    Upload a new file into the EXISTING vector store.
    Old files in the store are left intact (OpenAI handles retrieval ranking).
    If you want replace semantics, delete old files first (see comment below).
    """
    filename = uploaded_file.filename or "upload"
    content_type = uploaded_file.mimetype or "application/octet-stream"

    # Optional: remove previous files from the vector store so only the latest matters.
    # Uncomment the block below for strict single-file-per-chat behaviour.
    # try:
    #     existing = await client.vector_stores.files.list(vector_store_id=vector_store_id)
    #     for vf in existing.data:
    #         await client.vector_stores.files.delete(
    #             vector_store_id=vector_store_id, file_id=vf.id
    #         )
    # except Exception:
    #     pass

    created = await client.files.create(
        file=(filename, uploaded_file.stream, content_type),
        purpose="assistants",
    )

    vector_file = await client.vector_stores.files.create(
        vector_store_id=vector_store_id,
        file_id=created.id,
    )

    # Poll until indexed
    for _ in range(60):           # max ~60 s
        status = await client.vector_stores.files.retrieve(
            vector_store_id=vector_store_id,
            file_id=created.id,
        )
        if status.status == "completed":
            break
        await asyncio.sleep(1)

    return {
        "filename": filename,
        "content_type": content_type,
        "file_id": created.id,
        "vector_store_id": vector_store_id,
        "vector_store_file_id": getattr(vector_file, "id", None),
    }


# ---------------------------------------------------------------------------
# Direct OpenAI API agents – FIX #5  (no SDK agent builder, much faster)
# ---------------------------------------------------------------------------

EVALUATOR_SYSTEM_PROMPT = """You are a strict, evidence-based evaluator of uploaded documents (PPTX, PDF, image sequence, or text document).
Your task is to perform a detailed analysis of the uploaded file against the provided multi-criteria rubric,
ensuring each judgment is grounded solely in visible evidence with clear slide/page/frame references.
Do not make any assumptions or interpretations outside what is explicitly shown in the document.
Neutrality, consistency, and fairness are paramount; penalize vagueness, missing evidence, and structure gaps.

Evaluate using these five criteria (0-10 each):
A. Problem Statement – Clarity, context, supporting data, explicit identification.
B. Solution Clarity – Clear description, structure, logical flow, visual support.
C. Feasibility – Implementation details, resourcing, technical specifics, evidence of readiness.
D. Impact & ROI – Quantified outcomes, measurement plans, linkage to solution mechanisms.
E. Design & Storytelling – Visual consistency, readability, professional design, narrative flow.

Output EXACTLY this template:

Evaluation Report
------------------
1. Problem Statement: X/10
- Evidence-based Reason:
2. Solution Clarity: X/10
- Evidence-based Reason:
3. Feasibility: X/10
- Evidence-based Reason:
4. Impact & ROI: X/10
- Evidence-based Reason:
5. Design & Storytelling: X/10
- Evidence-based Reason:
Final Weighted Score: X/10

Executive Summary:
- Overall Quality:
- Strengths:
- Weaknesses:
- Areas of Improvement:
Slide Template Evaluation:
- Template Match Score: X/10
- Deviations:

In the Title slide the left logo represents Unlimited Innovations (not the client);
the right-side logo is the client logo; the subtitle of the first slide is the client name.

Also check that the document follows these template guidelines:
- Font ≥17pt; never <12pt except tables/graphs/footnotes.
- Theme fonts: Open Sans Light (titles), Open Sans Bold (subheaders), Open Sans Regular (body).
- Titles in Title Case; subheaders and body in Sentence case.
- Titles/subheaders: Blue #0059B8 on light bg, Light #F3F5F5 on dark bg.
- Body copy: Ink #1F2020 on light bg, Light #F3F5F5 on dark bg.

Region note: if US → first slide uses "UB Technology Innovations";
if India → "Unlimited Innovations"; if UAE → "UB Infinite Solutions"."""

WEB_SEARCH_SYSTEM_PROMPT = """You provide helpful assistance by searching the web for up-to-date information.
When asked about a company, present results in a clear markdown table including:
- Company overview, recent revenue (with year), employee count, founding year & location,
  key executives, headquarters, main products/services, notable awards, website URL.
Include source links. Be concise but complete (8-12 rows). Always cite sources."""

DOC_QA_SYSTEM_PROMPT = """You answer questions about the uploaded document.
Only answer from document content retrieved via file_search.
If the answer is not in the document, say so clearly.
Cite slide/page numbers when possible."""

EVALUATE_KEYWORDS = {
    "evaluate", "analyse", "analyze", "assessment", "review",
    "check", "inspect", "test", "validate", "verify", "examine",
    "scrutinize", "audit",
}

IDENTITY_PROMPTS = {"who are you", "who r you", "who are u", "what are you",
                    "identify yourself", "introduce yourself"}
CAPABILITY_PROMPTS = {"tell me your capabilities", "what are your capabilities",
                      "capabilities", "what can you do", "your capabilities"}


async def call_openai_with_file_search(
    system_prompt: str,
    conversation_history: list,
    vector_store_id: str,
) -> str:
    """
    Direct Responses API call with file_search tool attached to a vector store.
    No SDK agent runner – typically responds in 5-10 s.
    """
    tools = [
        {
            "type": "file_search",
            "vector_store_ids": [vector_store_id],
        }
    ]

    # Build input list for the Responses API
    input_messages = []
    for msg in conversation_history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Already structured
            input_messages.append({"role": role, "content": content})
        else:
            input_messages.append({"role": role, "content": str(content)})

    response = await client.responses.create(
        model="gpt-4.1",
        instructions=system_prompt,
        tools=tools,
        input=input_messages,
        store=False,
    )
    return response.output_text


async def call_openai_chat(
    system_prompt: str,
    conversation_history: list,
) -> str:
    """
    Direct Chat Completions call – no tools, fastest path.
    """
    messages = [{"role": "system", "content": system_prompt}]
    for msg in conversation_history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Flatten structured content to plain text for chat completions
            text_parts = [
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") in ("input_text", "text")
            ]
            content = " ".join(text_parts)
        messages.append({"role": role, "content": str(content)})

    response = await client.chat.completions.create(
        model="gpt-4.1",
        messages=messages,
    )
    return response.choices[0].message.content or ""


async def run_workflow(
    user_text: str,
    conversation_history: list,
    vector_store_id: str | None,
    use_document_qa: bool,
) -> str:
    """
    Route the request to the correct agent and return the output string.
    No SDK Runner – direct API calls only.
    """
    # Guard: short-circuit for identity/capability
    lower = user_text.strip().lower()
    if lower in IDENTITY_PROMPTS:
        return "I am UBTI AI Powered Assistant."
    if lower in CAPABILITY_PROMPTS:
        return (
            "I am UBTI AI Agent. I can perform web search and evaluate proposals "
            "to meet organisation standards."
        )

    # Apply guardrails
    guardrail = await apply_guardrails(user_text)
    if guardrail["blocked"]:
        fail = guardrail["fail_output"]
        cats = (fail.get("moderation") or {}).get("flagged_categories", [])
        return (
            "Your message was flagged and could not be processed. "
            f"Reason: {', '.join(cats) or 'policy violation'}."
        )

    # Use the scrubbed text going forward
    safe_text = guardrail["safe_text"]

    # Append the (possibly scrubbed) user message to history
    conversation_history.append(
        {"role": "user", "content": [{"type": "input_text", "text": safe_text}]}
    )

    # -- Evaluation branch
    if lower in EVALUATE_KEYWORDS:
        if not vector_store_id:
            return "No document is available to evaluate. Please upload a file first."
        return await call_openai_with_file_search(
            EVALUATOR_SYSTEM_PROMPT, conversation_history, vector_store_id
        )

    # -- Document QA branch (file uploaded / with_pdf mode)
    if use_document_qa and vector_store_id:
        return await call_openai_with_file_search(
            DOC_QA_SYSTEM_PROMPT, conversation_history, vector_store_id
        )

    # -- Web search / general chat branch
    return await call_openai_chat(WEB_SEARCH_SYSTEM_PROMPT, conversation_history)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@main_bp.route("/")
def index():
    return render_template("index.html")


@main_bp.route("/api/chats", methods=["GET", "POST"])
def chats():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        title = (payload.get("title") or "New Chat").strip() or "New Chat"
        mode = (payload.get("mode") or "with_pdf").strip() or "with_pdf"
        context_text = (payload.get("context_text") or "").strip()
        chat_id = create_chat(title=title, mode=mode, context_text=context_text)
        return jsonify({"id": chat_id, "title": title, "mode": mode}), 201
    return jsonify({"chats": list_chats()})


@main_bp.route("/api/chats/<int:chat_id>", methods=["GET"])
def chat_detail(chat_id):
    chat = get_chat(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    messages = get_messages(chat_id)
    return jsonify({"chat": chat, "messages": messages})


@main_bp.route("/api/chats/<int:chat_id>/messages", methods=["POST"])
def chat_messages(chat_id):
    chat = get_chat(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    user_text = request.form.get("input_text", "").strip() or "evaluate"
    mode = request.form.get("mode", chat.get("mode") or "with_pdf").strip()
    context_text = request.form.get("context_text", chat.get("context_text") or "").strip()
    uploaded = request.files.get("file")
    upload_status = None

    # Prepend context for context-aware modes
    if mode in {"with_pdf", "only_context"} and context_text:
        user_text = f"Context:\n{context_text}\n\nQuestion: {user_text}"

    # Persist user message & update title
    add_message(chat_id, "user", user_text)
    if chat.get("title") == "New Chat" and user_text:
        new_title = user_text.strip().splitlines()[0][:48]
        update_chat(chat_id, title=new_title or "New Chat")

    async def _run():
        nonlocal upload_status

        chat_data = get_chat(chat_id) or {}

        # FIX #1 & #2 & #3 & #4:
        # Reuse the chat's vector store; only upload when a NEW file arrives.
        if uploaded and mode == "with_pdf":
            vs_id = await get_or_create_vector_store(chat_id, chat_data)
            upload_status = await upload_file_to_vector_store(uploaded, vs_id)
            # Persist latest file metadata against this chat
            update_chat(
                chat_id,
                last_upload_name=uploaded.filename or "upload",
                openai_file_id=upload_status.get("file_id"),
                vector_store_id=vs_id,           # same store reused
            )
            chat_data = get_chat(chat_id) or {}  # refresh after update

        vector_store_id = chat_data.get("vector_store_id")
        use_document_qa = mode == "with_pdf"

        history = build_conversation_history(get_messages(chat_id))

        # FIX #5: direct API, no SDK Runner
        output_text = await run_workflow(
            user_text,
            history,
            vector_store_id=vector_store_id,
            use_document_qa=use_document_qa,
        )
        return output_text

    output_text = asyncio.run(_run())

    add_message(chat_id, "assistant", output_text)
    update_chat(chat_id, mode=mode, context_text=context_text)

    return jsonify(
        {
            "output_text": output_text,
            "upload_status": upload_status,
            "chat_id": chat_id,
            "messages": get_messages(chat_id),
            "chat": get_chat(chat_id),
        }
    )