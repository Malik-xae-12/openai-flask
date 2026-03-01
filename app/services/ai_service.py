import asyncio
import os
import re

from app.extensions import client

# ---------------------------------------------------------------------------
# Global vector store – set once in .env, reused everywhere
# ---------------------------------------------------------------------------
GLOBAL_VECTOR_STORE_ID = os.getenv("VECTOR_STORE_ID", "")

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
    "US_SSN":        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "US_PASSPORT":   re.compile(r"\b[A-Z]{1,2}\d{6,9}\b"),
    "US_BANK_NUMBER":re.compile(r"\b\d{8,17}\b"),
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
# Vector store helpers – global store, no per-chat creation
# ---------------------------------------------------------------------------

def get_vector_store_id() -> str:
    """Always returns the single global vector store ID."""
    if not GLOBAL_VECTOR_STORE_ID:
        raise ValueError("VECTOR_STORE_ID is not set in environment variables.")
    return GLOBAL_VECTOR_STORE_ID


async def upload_file_to_vector_store(uploaded_file) -> dict:
    """
    Upload a file to the GLOBAL vector store.
    Returns file metadata including file_id so callers can track the latest file.
    """
    vector_store_id = get_vector_store_id()
    filename = uploaded_file.filename or "upload"
    content_type = uploaded_file.mimetype or "application/octet-stream"

    # Upload file to OpenAI
    created = await client.files.create(
        file=(filename, uploaded_file.stream, content_type),
        purpose="assistants",
    )

    # Attach to global vector store
    await client.vector_stores.files.create(
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
        if status.status == "failed":
            break
        await asyncio.sleep(1)

    return {
        "filename": filename,
        "file_id": created.id,
        "vector_store_id": vector_store_id,
    }


async def delete_all_files_in_vector_store() -> dict:
    """
    Delete ALL files from the global vector store and from OpenAI storage.
    Used by the 'Reset Files' button.
    """
    vector_store_id = get_vector_store_id()

    # List all files in the vector store
    vs_files = await client.vector_stores.files.list(vector_store_id=vector_store_id)
    deleted_count = 0
    failed_count = 0

    for vs_file in vs_files.data:
        try:
            # Remove from vector store
            await client.vector_stores.files.delete(
                vector_store_id=vector_store_id,
                file_id=vs_file.id,
            )
            # Delete the actual file from OpenAI storage
            await client.files.delete(vs_file.id)
            deleted_count += 1
        except Exception:
            failed_count += 1

    return {
        "deleted": deleted_count,
        "failed": failed_count,
        "vector_store_id": vector_store_id,
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

DOC_QA_SYSTEM = """You are a document assistant. The user has uploaded one or more files in this chat.
Answer ONLY from the content of those files.
If the answer is not in the documents, say so clearly."""

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
# API call helpers
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


async def _call_evaluate(system_prompt: str, history: list, file_id: str) -> str:
    """
    EVALUATE path: uses Responses API with file_search scoped to ONE specific file.
    This is accurate but slightly slower — acceptable for evaluation.
    """
    vector_store_id = get_vector_store_id()
    tools = [{"type": "file_search", "vector_store_ids": [vector_store_id]}]

    # Inject file scoping into the prompt so the model focuses on the latest file
    scoped_system = (
        f"{system_prompt}\n\n"
        f"IMPORTANT: Evaluate ONLY the file with file_id={file_id}. "
        f"Ignore all other files in the vector store."
    )

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
        instructions=scoped_system,
        tools=tools,
        input=input_messages,
        store=False,
    )
    return response.output_text


async def _call_doc_qa(system_prompt: str, history: list, file_ids: list[str]) -> str:
    """
    Q&A path: uses Responses API with file_search so the model can read files.
    We scope the prompt to the file_ids belonging to this chat.
    """
    vector_store_id = get_vector_store_id()
    tools = [{"type": "file_search", "vector_store_ids": [vector_store_id]}]

    scoped_system = (
        f"{system_prompt}\n\n"
        f"IMPORTANT: Answer only using these file_ids from this chat: {', '.join(file_ids)}. "
        f"Ignore any other files in the vector store."
    )

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
        instructions=scoped_system,
        tools=tools,
        input=input_messages,
        store=False,
    )
    return response.output_text


async def _call_chat(system_prompt: str, history: list) -> str:
    """Standard chat completions for web search."""
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
    file_ids: list[str],               # all file IDs for this chat, oldest -> newest
    latest_file_name: str | None = None,
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

    latest_file_id = file_ids[-1] if file_ids else None

    # No file uploaded yet
    if not latest_file_id:
        return "Please upload a document first, then ask questions or type 'evaluate'."

    # EVALUATE — use file_search via Responses API (accurate, slightly slower)
    if any(kw in lower for kw in EVALUATE_KEYWORDS):
        return await _call_evaluate(EVALUATOR_SYSTEM, history, latest_file_id)

    # Q&A — use Responses API with file_search across all chat files
    return await _call_doc_qa(DOC_QA_SYSTEM, history, file_ids)


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
    "get_vector_store_id",
    "upload_file_to_vector_store",
    "delete_all_files_in_vector_store",
    "run_proposal_workflow",
    "run_websearch_workflow",
]