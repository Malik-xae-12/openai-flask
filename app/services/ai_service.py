import asyncio
import re

from app.extensions import client

# ---------------------------------------------------------------------------
# Guardrails
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
    detected = []
    for entity, pattern in PII_PATTERNS.items():
        if pattern.search(text):
            detected.append(entity)
            text = pattern.sub(f"[{entity}]", text)
    return text, detected


async def run_moderation(text: str) -> list[str]:
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


async def apply_guardrails(text: str) -> dict:
    """
    Returns:
        blocked  – True when request must be rejected
        safe_text – PII-scrubbed version of the input
        reason   – human-readable failure reason (only when blocked)
    """
    safe_text, _ = scrub_pii(text)
    flagged_cats = await run_moderation(safe_text)
    if flagged_cats:
        return {
            "blocked": True,
            "safe_text": safe_text,
            "reason": f"Content flagged: {', '.join(flagged_cats)}",
        }
    return {"blocked": False, "safe_text": safe_text, "reason": None}


# ---------------------------------------------------------------------------
# Vector store helpers  (fix: reuse per-chat store, no new store per upload)
# ---------------------------------------------------------------------------

async def get_or_create_vector_store(chat_id: int, existing_vs_id: str | None) -> str:
    if existing_vs_id:
        return existing_vs_id
    vs = await client.vector_stores.create(name=f"ubti_chat_{chat_id}")
    return vs.id


async def upload_file_to_vector_store(uploaded_file, vector_store_id: str) -> dict:
    filename = uploaded_file.filename or "upload"
    content_type = uploaded_file.mimetype or "application/octet-stream"

    created = await client.files.create(
        file=(filename, uploaded_file.stream, content_type),
        purpose="assistants",
    )

    vector_file = await client.vector_stores.files.create(
        vector_store_id=vector_store_id,
        file_id=created.id,
    )

    # Poll until indexed (max ~60 s)
    for _ in range(60):
        status = await client.vector_stores.files.retrieve(
            vector_store_id=vector_store_id,
            file_id=created.id,
        )
        if status.status == "completed":
            break
        await asyncio.sleep(1)

    return {
        "filename": filename,
        "file_id": created.id,
        "vector_store_id": vector_store_id,
        "vector_store_file_id": getattr(vector_file, "id", None),
    }


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

EVALUATOR_SYSTEM = """You are a strict, evidence-based evaluator of uploaded documents (PPTX, PDF, or text).
Evaluate using five criteria (0-10 each):
A. Problem Statement – Clarity, context, supporting data, explicit identification.
B. Solution Clarity – Clear description, structure, logical flow, visual support.
C. Feasibility – Implementation details, resourcing, technical specifics, evidence of readiness.
D. Impact & ROI – Quantified outcomes, measurement plans, linkage to solution mechanisms.
E. Design & Storytelling – Visual consistency, readability, professional design, narrative flow.

Output EXACTLY this template (no deviations):

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

Region: US → "UB Technology Innovations"; India → "Unlimited Innovations"; UAE → "UB Infinite Solutions".
Template guidelines: Font ≥17pt, Open Sans family, Blue #0059B8 titles, Ink #1F2020 body copy."""

DOC_QA_SYSTEM = """You answer questions about the uploaded document using file search.
Only answer from document content. Cite slide/page numbers where possible.
If the answer is not in the document, say so clearly."""

WEB_SEARCH_SYSTEM = """You are a research assistant. Provide helpful, accurate, up-to-date information.
For company queries, return a clean markdown table including:
- Company overview, recent revenue (with year), employee count, founding year & location,
  key executives, headquarters, main products/services, notable awards, website URL.
Include source links. Be concise but complete (8-12 rows). Always cite sources."""

EVALUATE_KEYWORDS = {
    "evaluate", "analyse", "analyze", "assessment", "review",
    "check", "inspect", "test", "validate", "verify", "examine",
    "scrutinize", "audit",
}

IDENTITY_PROMPTS = {
    "who are you", "who r you", "who are u", "what are you",
    "identify yourself", "introduce yourself",
}

CAPABILITY_PROMPTS = {
    "capabilities", "what can you do", "your capabilities",
    "tell me your capabilities", "what are your capabilities",
}


# ---------------------------------------------------------------------------
# Direct OpenAI API helpers (no SDK Runner — faster 5-10 s responses)
# ---------------------------------------------------------------------------

def _flatten_history(history: list) -> list:
    """Convert structured history to plain chat messages for Chat Completions."""
    messages = []
    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            text = " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") in ("input_text", "output_text", "text")
            )
        else:
            text = str(content)
        messages.append({"role": role, "content": text})
    return messages


async def _call_with_file_search(system_prompt: str, history: list, vector_store_id: str) -> str:
    tools = [{"type": "file_search", "vector_store_ids": [vector_store_id]}]
    input_messages = []
    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
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


async def _call_chat(system_prompt: str, history: list) -> str:
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(_flatten_history(history))
    response = await client.chat.completions.create(
        model="gpt-4.1",
        messages=messages,
    )
    return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Public workflow functions
# ---------------------------------------------------------------------------

async def run_proposal_workflow(
    user_text: str,
    history: list,
    vector_store_id: str | None,
) -> str:
    lower = user_text.strip().lower()

    if lower in IDENTITY_PROMPTS:
        return "I am UBTI AI Powered Assistant."
    if lower in CAPABILITY_PROMPTS:
        return (
            "I am UBTI AI Agent. I can evaluate proposals against organisation standards "
            "and answer document questions."
        )

    guardrail = await apply_guardrails(user_text)
    if guardrail["blocked"]:
        return f"Your message could not be processed. {guardrail['reason']}"

    safe_text = guardrail["safe_text"]
    history.append({"role": "user", "content": [{"type": "input_text", "text": safe_text}]})

    if lower in EVALUATE_KEYWORDS:
        if not vector_store_id:
            return "No document available to evaluate. Please upload a file first."
        return await _call_with_file_search(EVALUATOR_SYSTEM, history, vector_store_id)

    if vector_store_id:
        return await _call_with_file_search(DOC_QA_SYSTEM, history, vector_store_id)

    return "Please upload a document first, then ask questions or type 'evaluate'."


async def run_websearch_workflow(user_text: str, history: list) -> str:
    lower = user_text.strip().lower()

    if lower in IDENTITY_PROMPTS:
        return "I am UBTI AI Powered Assistant."
    if lower in CAPABILITY_PROMPTS:
        return (
            "I am UBTI AI Agent. I can perform web searches and provide "
            "structured research results."
        )

    guardrail = await apply_guardrails(user_text)
    if guardrail["blocked"]:
        return f"Your message could not be processed. {guardrail['reason']}"

    safe_text = guardrail["safe_text"]
    history.append({"role": "user", "content": [{"type": "input_text", "text": safe_text}]})
    return await _call_chat(WEB_SEARCH_SYSTEM, history)


__all__ = [
    "apply_guardrails",
    "get_or_create_vector_store",
    "upload_file_to_vector_store",
    "run_proposal_workflow",
    "run_websearch_workflow",
]