import asyncio
import os
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv
from openai import AsyncOpenAI
from openai.types.shared.reasoning import Reasoning
from pydantic import BaseModel

from agents import Agent, FileSearchTool, ModelSettings, RunConfig, Runner, TResponseInputItem, trace
try:
    from guardrails.runtime import load_config_bundle, instantiate_guardrails, run_guardrails
except ModuleNotFoundError:
    # Fallback for guardrails versions without runtime module.
    def load_config_bundle(config):
        return config

    def instantiate_guardrails(config):
        return config

    async def run_guardrails(_ctx, _text, _content_type, _config, **_kwargs):
        return []

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")

VECTOR_STORE_ID = os.environ.get("OPENAI_VECTOR_STORE_ID", "vs_69957e2424d88191999bfe86e31a849e")
DB_PATH = os.environ.get(
    "CHAT_DB_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "chat_history.db")),
)

# Shared client for guardrails and file search
client = AsyncOpenAI()
ctx = SimpleNamespace(guardrail_llm=client)

def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                mode TEXT NOT NULL,
                context_text TEXT NOT NULL,
                last_upload_name TEXT,
                openai_file_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats (id)
            )
            """
        )


def db_query(query, params=(), fetchone=False, fetchall=False):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(query, params)
        conn.commit()
        if fetchone:
            return cur.fetchone()
        if fetchall:
            return cur.fetchall()
    return None


def create_chat(title="New Chat", mode="with_pdf", context_text=""):
    now = _utc_now()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO chats (title, mode, context_text, created_at) VALUES (?, ?, ?, ?)",
            (title, mode, context_text, now),
        )
        conn.commit()
        return cur.lastrowid


def list_chats():
    rows = db_query(
        "SELECT id, title, mode, last_upload_name, created_at FROM chats ORDER BY id DESC",
        fetchall=True,
    )
    return [dict(row) for row in (rows or [])]


def get_chat(chat_id):
    row = db_query(
        "SELECT id, title, mode, context_text, last_upload_name, openai_file_id, created_at "
        "FROM chats WHERE id = ?",
        (chat_id,),
        fetchone=True,
    )
    return dict(row) if row else None


def update_chat(chat_id, **fields):
    if not fields:
        return
    columns = []
    values = []
    for key, value in fields.items():
        columns.append(f"{key} = ?")
        values.append(value)
    values.append(chat_id)
    db_query(f"UPDATE chats SET {', '.join(columns)} WHERE id = ?", tuple(values))


def add_message(chat_id, role, content):
    now = _utc_now()
    db_query(
        "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, role, content, now),
    )


def get_messages(chat_id):
    rows = db_query(
        "SELECT id, role, content, created_at FROM messages WHERE chat_id = ? ORDER BY id ASC",
        (chat_id,),
        fetchall=True,
    )
    return [dict(row) for row in (rows or [])]


def build_conversation_history(messages):
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


init_db()

# Tool definitions
file_search = FileSearchTool(vector_store_ids=[VECTOR_STORE_ID])

# Guardrails definitions
guardrails_config = {
    "guardrails": [
        {
            "name": "Contains PII",
            "config": {
                "block": False,
                "detect_encoded_pii": True,
                "entities": ["CREDIT_CARD", "US_BANK_NUMBER", "US_PASSPORT", "US_SSN"],
            },
        },
        {
            "name": "Moderation",
            "config": {
                "categories": [
                    "sexual/minors",
                    "hate/threatening",
                    "harassment/threatening",
                    "self-harm/instructions",
                    "violence/graphic",
                    "illicit/violent",
                ]
            },
        },
    ]
}


def guardrails_has_tripwire(results):
    return any(
        (hasattr(r, "tripwire_triggered") and (r.tripwire_triggered is True))
        for r in (results or [])
    )


def get_guardrail_safe_text(results, fallback_text):
    for r in (results or []):
        info = (r.info if hasattr(r, "info") else None) or {}
        if isinstance(info, dict) and ("checked_text" in info):
            return info.get("checked_text") or fallback_text
    pii = next(
        (
            (r.info if hasattr(r, "info") else {})
            for r in (results or [])
            if isinstance((r.info if hasattr(r, "info") else None) or {}, dict)
            and ("anonymized_text" in ((r.info if hasattr(r, "info") else None) or {}))
        ),
        None,
    )
    if isinstance(pii, dict) and ("anonymized_text" in pii):
        return pii.get("anonymized_text") or fallback_text
    return fallback_text


async def scrub_conversation_history(history, config):
    try:
        guardrails = (config or {}).get("guardrails") or []
        pii = next((g for g in guardrails if (g or {}).get("name") == "Contains PII"), None)
        if not pii:
            return
        pii_only = {"guardrails": [pii]}
        for msg in (history or []):
            content = (msg or {}).get("content") or []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "input_text" and isinstance(
                    part.get("text"), str
                ):
                    res = await run_guardrails(
                        ctx,
                        part["text"],
                        "text/plain",
                        instantiate_guardrails(load_config_bundle(pii_only)),
                        suppress_tripwire=True,
                        raise_guardrail_errors=True,
                    )
                    part["text"] = get_guardrail_safe_text(res, part["text"])
    except Exception:
        pass


async def scrub_workflow_input(workflow, input_key, config):
    try:
        guardrails = (config or {}).get("guardrails") or []
        pii = next((g for g in guardrails if (g or {}).get("name") == "Contains PII"), None)
        if not pii:
            return
        if not isinstance(workflow, dict):
            return
        value = workflow.get(input_key)
        if not isinstance(value, str):
            return
        pii_only = {"guardrails": [pii]}
        res = await run_guardrails(
            ctx,
            value,
            "text/plain",
            instantiate_guardrails(load_config_bundle(pii_only)),
            suppress_tripwire=True,
            raise_guardrail_errors=True,
        )
        workflow[input_key] = get_guardrail_safe_text(res, value)
    except Exception:
        pass


async def run_and_apply_guardrails(input_text, config, history, workflow):
    results = await run_guardrails(
        ctx,
        input_text,
        "text/plain",
        instantiate_guardrails(load_config_bundle(config)),
        suppress_tripwire=True,
        raise_guardrail_errors=True,
    )
    guardrails = (config or {}).get("guardrails") or []
    mask_pii = (
        next(
            (
                g
                for g in guardrails
                if (g or {}).get("name") == "Contains PII"
                and ((g or {}).get("config") or {}).get("block") is False
            ),
            None,
        )
        is not None
    )
    if mask_pii:
        await scrub_conversation_history(history, config)
        await scrub_workflow_input(workflow, "input_as_text", config)
        await scrub_workflow_input(workflow, "input_text", config)
    has_tripwire = guardrails_has_tripwire(results)
    safe_text = get_guardrail_safe_text(results, input_text)
    fail_output = build_guardrail_fail_output(results or [])
    pass_output = {"safe_text": (get_guardrail_safe_text(results, input_text) or input_text)}
    return {
        "results": results,
        "has_tripwire": has_tripwire,
        "safe_text": safe_text,
        "fail_output": fail_output,
        "pass_output": pass_output,
    }


def build_guardrail_fail_output(results):
    def _get(name: str):
        for r in (results or []):
            info = (r.info if hasattr(r, "info") else None) or {}
            gname = (info.get("guardrail_name") if isinstance(info, dict) else None) or (
                info.get("guardrailName") if isinstance(info, dict) else None
            )
            if gname == name:
                return r
        return None

    pii, mod, jb, hal, nsfw, url, custom, pid = map(
        _get,
        [
            "Contains PII",
            "Moderation",
            "Jailbreak",
            "Hallucination Detection",
            "NSFW Text",
            "URL Filter",
            "Custom Prompt Check",
            "Prompt Injection Detection",
        ],
    )

    def _tripwire(r):
        return bool(getattr(r, "tripwire_triggered", False))

    def _info(r):
        return (r.info if hasattr(r, "info") else None) or {}

    jb_info, hal_info, nsfw_info, url_info, custom_info, pid_info, mod_info, pii_info = map(
        _info, [jb, hal, nsfw, url, custom, pid, mod, pii]
    )
    detected_entities = pii_info.get("detected_entities") if isinstance(pii_info, dict) else {}
    pii_counts = []
    if isinstance(detected_entities, dict):
        for k, v in detected_entities.items():
            if isinstance(v, list):
                pii_counts.append(f"{k}:{len(v)}")
    flagged_categories = (mod_info.get("flagged_categories") if isinstance(mod_info, dict) else None) or []

    return {
        "pii": {"failed": (len(pii_counts) > 0) or _tripwire(pii), "detected_counts": pii_counts},
        "moderation": {
            "failed": _tripwire(mod) or (len(flagged_categories) > 0),
            "flagged_categories": flagged_categories,
        },
        "jailbreak": {"failed": _tripwire(jb)},
        "hallucination": {
            "failed": _tripwire(hal),
            "reasoning": (hal_info.get("reasoning") if isinstance(hal_info, dict) else None),
            "hallucination_type": (
                hal_info.get("hallucination_type") if isinstance(hal_info, dict) else None
            ),
            "hallucinated_statements": (
                hal_info.get("hallucinated_statements") if isinstance(hal_info, dict) else None
            ),
            "verified_statements": (
                hal_info.get("verified_statements") if isinstance(hal_info, dict) else None
            ),
        },
        "nsfw": {"failed": _tripwire(nsfw)},
        "url_filter": {"failed": _tripwire(url)},
        "custom_prompt_check": {"failed": _tripwire(custom)},
        "prompt_injection": {"failed": _tripwire(pid)},
    }


proposal_evaluator = Agent(
    name="Proposal Evaluator",
    instructions=(
        "You are a strict, evidence-based evaluator of uploaded documents (PPTX, PDF, image sequence, or text document). "
        "Your task is to perform a detailed analysis of the uploaded file against the provided multi-criteria rubric, "
        "ensuring each judgment is grounded solely in visible evidence with clear slide/page/frame references. "
        "Do not make any assumptions or interpretations outside what is explicitly shown in the document. "
        "Neutrality, consistency, and fairness are paramount; penalize vagueness, missing evidence, and structure gaps. "
        "Follow the full evaluation process, step-by-step, as outlined:\n\n"
        "# Steps\n\n"
        "1. Preparation and Scanning\n"
        "- Scan the entire document end-to-end for structure: count slides/pages/frames, note section headings, "
        "and identify visuals (charts, timelines, tables, diagrams).\n"
        "- On a second pass, read deeply, extracting exact headings, bullets, captions, "
        "and noting presence/clarity of visuals (axes/labels/legends/units).\n\n"
        "2. Evidence and Indexing\n"
        "- For each slide/page/frame, index explicit evidence (e.g., \"Slide 2: Problem statement...\").\n"
        "- Do not infer or interpret unstated intentions, causes, or data.\n\n"
        "3. Criterion-Based Scoring\n"
        "- Analyze and score each of the following five criteria independently using the explicit rubrics below, "
        "citing specific evidence for each:\n"
        "- A. Problem Statement (0-10): Clarity, context, supporting data, and explicit identification.\n"
        "- B. Solution Clarity (0-10): Clear description, structure, logical flow, visual support.\n"
        "- C. Feasibility (0-10): Implementation details, resourcing, technical specifics, evidence of readiness.\n"
        "- D. Impact & ROI (0-10): Quantified outcomes, measurement plans, linkage to solution mechanisms.\n"
        "- E. Design & Storytelling (0-10): Visual consistency, readability, professional design, narrative flow.\n\n"
        "- For each criterion, provide a score per rubric (with strict adherence to prescribed score ranges/penalties) "
        "and a reasoned justification referencing the indexed document evidence.\n\n"
        "4. Strict Use of Evidence\n"
        "- Whenever scoring, cite slide/page/frame numbers and direct quotes or paraphrases.\n"
        "- Penalize missing, unclear, or implied content as detailed in the rubrics.\n"
        "- State explicitly if content is illegible, missing, or unclear, and penalize accordingly.\n\n"
        "5. Scoring Mechanics\n"
        "- Assign an integer score (0-10) to each criterion without rounding up artificially.\n"
        "- Calculate the final weighted score as the simple average of the five criteria, rounded to one decimal place.\n\n"
        "6. Executive Summary\n"
        "- After scoring, synthesize your findings in an executive summary that:\n"
        "- States overall quality in 1-2 sentences based on scores.\n"
        "- Cites STRENGTHS and WEAKNESSES, referencing precise slide numbers and evidence.\n"
        "- Lists clear, actionable AREAS FOR IMPROVEMENT tied to the document and rubric.\n\n"
        "7. Handling File Types and Quality\n"
        "- For non-text or image-based PDFs: treat each image or frame as a page, extract all visible content.\n"
        "- If content is illegible (e.g., low-quality scan, missing text): note the limitation, "
        "penalize impacted criteria.\n\n"
        "8. Enforce Complete Neutrality\n"
        "- Use professional, non-emotive language.\n"
        "- Refuse to speculate about missing or implied data. Favor penalization for every gap per the rubric.\n\n"
        "# Output Format\n\n"
        "Strictly use the following output template, without altering its structure:\n\n"
        "Evaluation Report\n"
        "------------------\n"
        "1. Problem Statement: X/10\n"
        "- Evidence-based Reason:\n"
        "2. Solution Clarity: X/10\n"
        "- Evidence-based Reason:\n"
        "3. Feasibility: X/10\n"
        "- Evidence-based Reason:\n"
        "4. Impact & ROI: X/10\n"
        "- Evidence-based Reason:\n"
        "5. Design & Storytelling: X/10\n"
        "- Evidence-based Reason:\n"
        "Final Weighted Score: X/10\n\n"
        "Executive Summary:\n"
        "- Overall Quality:\n"
        "- Strengths:\n"
        "- Weaknesses:\n"
        "- Areas of Improvement:\n"
        "Slide Template Evaluation:\n"
        "- Template Match Score: X/10\n"
        "- Deviations:\n\n"
        "Your response must be clear, properly sectioned, and formatted exactly as above. "
        "All claims must be tied to explicit evidence in the document with precise slide/page/frame numbers.\n\n"
        "In Title slide the left logo represent Unlimited Innovations which is not client, the right side logo which has been used as client logo "
        "and subtitle of the first slide represent the client name. based on this provide information about the client\n\n"
        "Also please analyse that the uploaded PDF has these contents in the slides\n"
        "This PowerPoint template offers multiple slide layouts to accommodate various presentation needs. Please adhere to the following guidelines when creating decks:\n"
        "Do not make edits in the template or in the Slide Master. Always make a copy of this deck for your own needs.\n"
        "Aim to use 17+ font size. Avoid text smaller than 12, with the exception of tables, graphs, and footnotes (as appropriate).\n"
        "Use the theme fonts and colors for consistency.\n"
        "Slide title text font is Open Sans Light. Subheader text font is Open Sans Bold, and body text font is Open Sans Regular. "
        "Slide titles are in titlecase, and subheaders and body text are in sentence case.\n"
        "Slide titles and subheaders use font color Blue HEX #0059B8 unless it is on a dark background, "
        "in which case it should be the font color Light HEX #F3F5F5.\n"
        "Slide body copy use font color Ink HEX #1F2020 unless it is on a dark background, "
        "in which case it should be the font color Light HEX #F3F5F5.\n"
        "The icon and graph colors can be changed as long as it is within the template color palette.\n"
        "When copying and pasting content from other decks, avoid copying the entire slide. "
        "Copy only the slide content (text boxes, images, etc.) and right-click to paste using Use Destination Theme in the deck.\n"
        "Contact the Marketing team if you have feedback or if you have ideas for additional slide layouts.\n\n"
        "Also the prompt and ask that which region it is for if it is US the first slide need to be with UB Technology Innovations and If India then Unlimited Innovations and if it is UAE then UB Infinite solutions and then finally after the Improvement section give section for slide template evaluation score based on the match and if there is deviation then need to showcase the points.\n\n"
        "Reminder: Your primary duties are to evaluate explicit content only, score strictly according to the rubric, "
        "and cite all evidence by location, presenting your findings in the mandated format with step-by-step justification for every score."
    ),
    model="gpt-5.2",
    tools=[file_search],
    model_settings=ModelSettings(
        store=True,
        reasoning=Reasoning(
            effort="low",
            summary="auto",
        ),
    ),
)


web_search_agent = Agent(
    name="Web search Agent",
    instructions=(
        "Provide helpful assistance by searching the web for necessary and up-to-date information about a given search query. "
        "For example, if a user inquires about a company, research recent and reliable online sources to gather comprehensive details, "
        "then present the results in a clear, well-structured table format. Include all relevant information such as: \n\n"
        "- Company overview/about\n"
        "- Recent revenue figures (state the year)\n"
        "- Number of employees (most recent figure available)\n"
        "- Founding year & location\n"
        "- Key executives/leadership\n"
        "- Headquarters location\n"
        "- Main products/services\n"
        "- Any notable awards or achievements\n"
        "- Website URL\n\n"
        "If additional pertinent or commonly requested information is found (e.g., market cap, major acquisitions), include this as well. "
        "Always cite the source for each data point in the table with a direct link or short reference.\n\n"
        "Output Format:\n"
        "- Response: A single, well-formatted markdown table summarizing all collected data.\n"
        "- Length: Table should be concise but as complete as possible, typically 8-12 rows.\n"
        "- Sources must appear as hyperlinks within each table cell or a footnote below the table.\n\n"
        "Reminder: Your objective is to provide clean, well-sourced, and easily readable tables with as much verified detail as you can find."
    ),
    model="gpt-5.2",
    model_settings=ModelSettings(
        store=True,
        reasoning=Reasoning(
            effort="low",
            summary="auto",
        ),
    ),
)


document_qa_agent = Agent(
    name="Document QA Agent",
    instructions=(
        "You answer questions about the uploaded document using file search results. "
        "Use the file_search tool to find relevant content and only answer from the document. "
        "If the answer is not in the document, say so. "
        "Cite slide/page numbers when possible."
    ),
    model="gpt-5.2",
    tools=[file_search],
    model_settings=ModelSettings(
        store=True,
        reasoning=Reasoning(
            effort="low",
            summary="auto",
        ),
    ),
)


class WorkflowInput(BaseModel):
    input_as_text: str


async def upload_to_vector_store(uploaded_file):
    if not uploaded_file:
        return None
    filename = uploaded_file.filename or "upload"
    content_type = uploaded_file.mimetype or "application/octet-stream"
    print(f"[upload] Sending file to OpenAI: name={filename} type={content_type}")
    created = await client.files.create(
        file=(filename, uploaded_file.stream, content_type),
        purpose="assistants",
    )
    print(f"[upload] OpenAI file_id={created.id}")
    vector_file = await client.vector_stores.files.create(
        vector_store_id=VECTOR_STORE_ID,
        file_id=created.id,
    )
    print(
        f"[vector-store] Indexed file_id={created.id} in vector_store_id={VECTOR_STORE_ID} "
        f"vector_store_file_id={getattr(vector_file, 'id', None)}"
    )
    return {
        "filename": filename,
        "content_type": content_type,
        "file_id": created.id,
        "vector_store_id": VECTOR_STORE_ID,
        "vector_store_file_id": getattr(vector_file, "id", None),
    }


async def run_workflow(workflow_input: WorkflowInput, conversation_history, use_document_qa=False):
    with trace("Proposal Evaluator"):
        print("[run] Starting workflow")
        print(f"[run] Using vector_store_id={VECTOR_STORE_ID}")
        workflow = workflow_input.model_dump()
        conversation_history.append(
            {
                "role": "user",
                "content": [{"type": "input_text", "text": workflow["input_as_text"]}],
            }
        )
        print(f"[run] User input={workflow['input_as_text']!r}")
        guardrails_input_text = workflow["input_as_text"]
        guardrails_result = await run_and_apply_guardrails(
            guardrails_input_text, guardrails_config, conversation_history, workflow
        )
        guardrails_hastripwire = guardrails_result["has_tripwire"]
        guardrails_output = (
            guardrails_hastripwire and guardrails_result["fail_output"]
        ) or guardrails_result["pass_output"]
        if guardrails_hastripwire:
            print("[guardrails] Tripwire triggered, returning guardrails output")
            return guardrails_output, conversation_history
        if workflow["input_as_text"].strip().lower() in {
            "evaluate",
            "analyse",
            "assessment",
            "review",
            "check",
            "inspect",
            "test",
            "validate",
            "verify",
            "examine",
            "scrutinize",
            "audit",
        }:
            proposal_evaluator_result_temp = await Runner.run(
                proposal_evaluator,
                input=[*conversation_history],
                run_config=RunConfig(
                    trace_metadata={
                        "__trace_source__": "agent-builder",
                        "workflow_id": "wf_6995798aa648819081ebbbb27d899f550d41e630ed9d94c6",
                    }
                ),
            )
            conversation_history.extend(
                [item.to_input_item() for item in proposal_evaluator_result_temp.new_items]
            )
            proposal_evaluator_result = {
                "output_text": proposal_evaluator_result_temp.final_output_as(str)
            }
            return proposal_evaluator_result, conversation_history

        agent_to_use = document_qa_agent if use_document_qa else web_search_agent
        web_search_agent_result_temp = await Runner.run(
            agent_to_use,
            input=[*conversation_history],
            run_config=RunConfig(
                trace_metadata={
                    "__trace_source__": "agent-builder",
                    "workflow_id": "wf_6995798aa648819081ebbbb27d899f550d41e630ed9d94c6",
                }
            ),
        )
        conversation_history.extend(
            [item.to_input_item() for item in web_search_agent_result_temp.new_items]
        )
        web_search_agent_result = {"output_text": web_search_agent_result_temp.final_output_as(str)}
        return web_search_agent_result, conversation_history


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/chats", methods=["GET", "POST"])
def chats():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        title = (payload.get("title") or "New Chat").strip() or "New Chat"
        mode = (payload.get("mode") or "with_pdf").strip() or "with_pdf"
        context_text = (payload.get("context_text") or "").strip()
        chat_id = create_chat(title=title, mode=mode, context_text=context_text)
        return jsonify({"id": chat_id, "title": title, "mode": mode}), 201
    return jsonify({"chats": list_chats()})


@app.route("/api/chats/<int:chat_id>", methods=["GET"])
def chat_detail(chat_id):
    chat = get_chat(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    messages = get_messages(chat_id)
    return jsonify({"chat": chat, "messages": messages})


@app.route("/api/chats/<int:chat_id>/messages", methods=["POST"])
def chat_messages(chat_id):
    chat = get_chat(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    user_text = request.form.get("input_text", "").strip()
    mode = request.form.get("mode", chat.get("mode") or "with_pdf").strip()
    context_text = request.form.get("context_text", chat.get("context_text") or "").strip()
    uploaded = request.files.get("file")
    upload_status = None

    if not user_text:
        user_text = "evaluate"

    identity_prompts = {
        "who are you",
        "who r you",
        "who are u",
        "what are you",
        "identify yourself",
        "introduce yourself",
    }
    capability_prompts = {
        "tell me your capabilities",
        "what are your capabilities",
        "capabilities",
        "what can you do",
        "your capabilities",
    }
    if user_text.strip().lower() in identity_prompts:
        response_text = "I am UBTI AI Powered Assistant."
        add_message(chat_id, "user", user_text)
        add_message(chat_id, "assistant", response_text)
        if chat.get("title") == "New Chat" and user_text:
            new_title = user_text.strip().splitlines()[0][:48]
            update_chat(chat_id, title=new_title or "New Chat")
        return jsonify(
            {
                "output_text": response_text,
                "chat_id": chat_id,
                "messages": get_messages(chat_id),
                "chat": get_chat(chat_id),
            }
        )
    if user_text.strip().lower() in capability_prompts:
        response_text = (
            "I am UBTI AI Agent. I can perform web search and evaluate proposals "
            "to meet organization standards."
        )
        add_message(chat_id, "user", user_text)
        add_message(chat_id, "assistant", response_text)
        if chat.get("title") == "New Chat" and user_text:
            new_title = user_text.strip().splitlines()[0][:48]
            update_chat(chat_id, title=new_title or "New Chat")
        return jsonify(
            {
                "output_text": response_text,
                "chat_id": chat_id,
                "messages": get_messages(chat_id),
                "chat": get_chat(chat_id),
            }
        )

    if mode in {"with_pdf", "only_context"} and context_text:
        user_text = f"Context:\n{context_text}\n\nQuestion: {user_text}"

    add_message(chat_id, "user", user_text)
    if chat.get("title") == "New Chat" and user_text:
        new_title = user_text.strip().splitlines()[0][:48]
        update_chat(chat_id, title=new_title or "New Chat")

    async def _run():
        nonlocal upload_status
        if uploaded and mode == "with_pdf":
            upload_status = await upload_to_vector_store(uploaded)
            update_chat(
                chat_id,
                last_upload_name=uploaded.filename or "upload",
                openai_file_id=(upload_status or {}).get("file_id"),
            )
        workflow_input = WorkflowInput(input_as_text=user_text)
        history = build_conversation_history(get_messages(chat_id))
        use_document_qa = mode == "with_pdf"
        return await run_workflow(workflow_input, history, use_document_qa=use_document_qa)

    result, _ = asyncio.run(_run())
    add_message(chat_id, "assistant", result.get("output_text", ""))

    update_chat(chat_id, mode=mode, context_text=context_text)

    if isinstance(result, dict):
        result["upload_status"] = upload_status
        result["chat_id"] = chat_id
        result["messages"] = get_messages(chat_id)
        result["chat"] = get_chat(chat_id)
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
