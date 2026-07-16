"""
Agentic SDLC Pipeline - "Hello Rushikesh" UI Generator
=========================================================
6 agents that talk to each other automatically. You just run:
    python main.py

Pipeline:
  Agent 1 (Requirement Planner)  -> writes requirements.md
  Agent 2 (System Architect)     -> reads requirements.md, writes document.md
  Agent 3 (QA Auditor)           -> scores spec, loops back to Agent 2 until >= 75% (no cap)
  Agent 4 (UI Engineer)          -> writes index.html
  Agent 5 (Code Reviewer)        -> scores code, loops back to Agent 4 until >= 75% (no cap)
  Agent 6 (Deployment Engine)    -> starts local server + opens browser

A full Markdown log of the agent-to-agent conversation (what each agent
produced, what feedback/critique it received, and its accuracy score) is
saved to .vscode/agent_conversation_log.md after every run.

Requires: pip install groq python-dotenv
API key:  https://console.groq.com/keys (free)
"""

import os
import sys

# Force UTF-8 output so Windows' default console codepage (cp1252/cp437)
# doesn't crash on Unicode characters that models sometimes emit
# (curly quotes, non-breaking spaces, em-dashes, etc.)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import json
import time
from pathlib import Path

from groq import Groq
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")              # optional fallback (free tier)
LOCAL_LLM_URL = os.getenv("LOCAL_LLM_URL", "http://localhost:11434")  # optional last-resort (Ollama, free/unlimited)

if not GROQ_API_KEY:
    print("ERROR: GROQ_API_KEY not found.")
    print("1. Get a free key at https://console.groq.com/keys")
    print("2. Create a file named '.env' in this folder with:")
    print('   GROQ_API_KEY=your_key_here')
    sys.exit(1)

client = Groq(api_key=GROQ_API_KEY)

gemini_configured = False
if GEMINI_API_KEY:
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_configured = True

# ---------------------------------------------------------------------------
# CROSS-PROVIDER FALLBACK CHAINS: Groq (multiple models) -> Gemini (free) -> Ollama (free, local, unlimited)
# Each agent "role" has an ordered chain. If one entry is rate-limited or
# fails, the NEXT entry is tried immediately - including switching providers.
# Providers with no key configured (Gemini) are skipped automatically.
# ---------------------------------------------------------------------------
FALLBACK_CHAINS = {
    "planner": [
        {"provider": "groq", "model": "openai/gpt-oss-120b"},
        {"provider": "groq", "model": "llama-3.3-70b-versatile"},
        {"provider": "groq", "model": "llama-3.1-8b-instant"},
        {"provider": "gemini", "model": "gemini-2.5-flash"},
        {"provider": "local", "model": "llama3"},
    ],
    "architect": [
        {"provider": "groq", "model": "qwen/qwen3.6-27b"},
        {"provider": "groq", "model": "llama-3.3-70b-versatile"},
        {"provider": "groq", "model": "llama-3.1-8b-instant"},
        {"provider": "gemini", "model": "gemini-2.5-flash"},
        {"provider": "local", "model": "llama3"},
    ],
    "auditor": [
        {"provider": "groq", "model": "qwen/qwen3.6-27b"},
        {"provider": "groq", "model": "llama-3.3-70b-versatile"},
        {"provider": "gemini", "model": "gemini-2.5-flash"},
        {"provider": "local", "model": "llama3"},
    ],
    "coder": [
        {"provider": "groq", "model": "openai/gpt-oss-120b"},
        {"provider": "groq", "model": "llama-3.3-70b-versatile"},
        {"provider": "groq", "model": "llama-3.1-8b-instant"},
        {"provider": "gemini", "model": "gemini-2.5-flash"},
        {"provider": "local", "model": "llama3"},
    ],
    "reviewer": [
        {"provider": "groq", "model": "openai/gpt-oss-120b"},
        {"provider": "groq", "model": "llama-3.3-70b-versatile"},
        {"provider": "gemini", "model": "gemini-2.5-flash"},
        {"provider": "local", "model": "llama3"},
    ],
}

ACCURACY_THRESHOLD = 75
MAX_LOOPS = None  # no cap: Phase 1/2 loops retry indefinitely until ACCURACY_THRESHOLD is met

BASE_DIR = Path(".vscode")
BASE_DIR.mkdir(exist_ok=True)

USER_GOAL = ""  # empty until the user enters a requirement; updated dynamically each cycle

# ---------------------------------------------------------------------------
# REQUIREMENTS HISTORY "DB" - lives at the TOP level (not inside a run folder)
# since it needs to accumulate across every run, for merging.
# ---------------------------------------------------------------------------
REQUIREMENTS_DB_PATH = BASE_DIR / "requirements_history.json"


def get_today_date_dir() -> Path:
    """Creates (if needed) and returns .vscode/YYYY-MM-DD/ - one folder per day."""
    date_str = time.strftime("%Y-%m-%d")
    date_dir = BASE_DIR / date_str
    date_dir.mkdir(exist_ok=True)
    return date_dir


def get_next_run_number(date_dir: Path) -> int:
    """Looks at existing 'requirementN' folders INSIDE today's date folder
    and returns the next number (numbering restarts each new day)."""
    nums = []
    for p in date_dir.iterdir():
        if p.is_dir() and p.name.startswith("requirement"):
            suffix = p.name.replace("requirement", "")
            if suffix.isdigit():
                nums.append(int(suffix))
    return max(nums, default=0) + 1


def create_run_dir(date_dir: Path, run_number: int) -> Path:
    """Creates (if needed) and returns .vscode/YYYY-MM-DD/requirementN/ -
    every file for this run (requirements.md, document.md, audit_report.md,
    index.html, review_report.md, agent_conversation_log.md, history
    snapshot) lives here."""
    run_dir = date_dir / f"requirement{run_number}"
    run_dir.mkdir(exist_ok=True)
    return run_dir


def load_requirements_history() -> list:
    if REQUIREMENTS_DB_PATH.exists():
        try:
            return json.loads(REQUIREMENTS_DB_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def save_requirement_to_history(goal_text: str):
    history = load_requirements_history()
    history.append({
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "goal": goal_text,
    })
    REQUIREMENTS_DB_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")
    print(f"[DB] Saved new requirement to {REQUIREMENTS_DB_PATH} (history now has {len(history)} entries).")


def ask_for_new_requirement() -> str | None:
    """Interactive terminal prompt. Returns the new requirement text, or None
    if the user says no (meaning: just continue with what's already saved)."""
    global USER_GOAL

    history = load_requirements_history()
    if history:
        print(f"\n[Info] {len(history)} previous requirement(s) already saved.")
    else:
        print("\n[Info] No previous requirements found yet - this will be the first one.")

    answer = input("Do you have a new requirement you want added? (yes/no): ").strip().lower()
    if answer not in ("y", "yes"):
        print("[Info] No new requirement given. Continuing with existing saved requirements.")
        return None

    new_goal = input("What is your new requirement?: ").strip()
    while not new_goal:
        new_goal = input("Requirement cannot be empty. What is your new requirement?: ").strip()

    USER_GOAL = new_goal  # update so the conversation log header reflects the latest requirement
    save_requirement_to_history(new_goal)
    return new_goal


# ---------------------------------------------------------------------------
# CONVERSATION LOG - records every agent-to-agent exchange for this run
# ---------------------------------------------------------------------------

run_log = []  # list of dict entries, written to Markdown at the end


def log_step(agent_name: str, description: str, content: str,
             score: int = None, feedback_sent: str = None):
    """Record one step of the pipeline (an agent producing output, and/or
    handing feedback to another agent)."""
    run_log.append({
        "agent": agent_name,
        "description": description,
        "content": content,
        "score": score,
        "feedback_sent": feedback_sent,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def save_conversation_log(run_history: dict, run_dir: Path):
    """Write the full agent conversation (with scores + feedback trail) to
    a single readable Markdown file."""
    lines = []
    lines.append("# Agent Conversation Log")
    lines.append("")
    lines.append(f"**Goal:** {USER_GOAL}")
    lines.append(f"**Run started:** {run_log[0]['timestamp'] if run_log else 'N/A'}")
    lines.append(f"**Accuracy threshold:** {ACCURACY_THRESHOLD}%  |  **Loop cap:** none (retries until threshold is met)")
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, entry in enumerate(run_log, 1):
        lines.append(f"## Step {i} - {entry['agent']}")
        lines.append(f"*{entry['timestamp']}*")
        lines.append("")
        lines.append(f"**What happened:** {entry['description']}")
        lines.append("")
        if entry["score"] is not None:
            verdict = "PASSED" if entry["score"] >= ACCURACY_THRESHOLD else "BELOW THRESHOLD"
            lines.append(f"**Accuracy score:** {entry['score']}%  ({verdict})")
            lines.append("")
        if entry["feedback_sent"]:
            lines.append("**Feedback/suggestions sent to the next agent:**")
            lines.append("> " + entry["feedback_sent"].replace("\n", "\n> "))
            lines.append("")
        lines.append("<details>")
        lines.append("<summary>Full output produced at this step</summary>")
        lines.append("")
        lines.append("```")
        lines.append(entry["content"])
        lines.append("```")
        lines.append("</details>")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Accuracy summary table
    lines.append("## Accuracy Summary")
    lines.append("")
    lines.append("| Phase | Loop # | Score | Result |")
    lines.append("|---|---|---|---|")
    for idx, s in enumerate(run_history["phase1"], 1):
        result = "Approved" if s >= ACCURACY_THRESHOLD else "Sent back to Agent 2"
        lines.append(f"| Phase 1: Spec (Agent 2 <-> Agent 3) | {idx} | {s}% | {result} |")
    for idx, s in enumerate(run_history["phase2"], 1):
        result = "Approved" if s >= ACCURACY_THRESHOLD else "Sent back to Agent 4"
        lines.append(f"| Phase 2: Code (Agent 4 <-> Agent 5) | {idx} | {s}% | {result} |")
    lines.append("")

    path = run_dir / "agent_conversation_log.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[Log] Full agent conversation saved to {path}")


# ---------------------------------------------------------------------------
# HELPER: call the LLM
# ---------------------------------------------------------------------------

import re

# Known Groq account-level TPM ceilings per model, used only to pre-shrink
# Groq requests before sending (Gemini/Ollama don't need this).
MODEL_TPM_LIMITS = {
    "qwen/qwen3.6-27b": 8000,
    "openai/gpt-oss-120b": 8000,
    "llama-3.3-70b-versatile": 6000,
    "llama-3.1-8b-instant": 6000,
}


def _parse_retry_wait_seconds(error_text: str, default: float = 15.0) -> float:
    """Extract a wait time like 'try again in 8m17.232s' from a rate-limit
    error message. Falls back to `default` (plus buffer) if not found."""
    match = re.search(r"try again in\s+(?:(\d+)m)?([\d.]+)s", error_text)
    if not match:
        return default
    minutes = float(match.group(1)) if match.group(1) else 0.0
    seconds = float(match.group(2))
    return minutes * 60 + seconds + 2


def _call_groq(model, system_prompt, user_prompt, max_tokens, json_mode):
    approx_prompt_tokens = (len(system_prompt) + len(user_prompt)) // 4
    tpm_limit = MODEL_TPM_LIMITS.get(model, 6000)
    safe_max_tokens = max(512, min(max_tokens, tpm_limit - approx_prompt_tokens - 300))

    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
        "max_tokens": safe_max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if "gpt-oss" in model:
        kwargs["include_reasoning"] = False
    elif "qwen3.6" in model:
        kwargs["reasoning_effort"] = "none"

    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content


def _call_gemini(model, system_prompt, user_prompt, max_tokens, json_mode):
    if not gemini_configured:
        raise RuntimeError("GEMINI_API_KEY not set; Gemini fallback unavailable.")
    generation_config = {"temperature": 0.4, "max_output_tokens": max_tokens}
    if json_mode:
        generation_config["response_mime_type"] = "application/json"
    gm = genai.GenerativeModel(model_name=model, system_instruction=system_prompt)
    response = gm.generate_content(user_prompt, generation_config=generation_config)
    return response.text


def _call_local(model, system_prompt, user_prompt, max_tokens, json_mode):
    """Last-resort fallback: a local Ollama server. No tokens, no rate
    limits, no internet dependency. Requires Ollama running with `model` pulled."""
    import urllib.request
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.4},
    }
    req = urllib.request.Request(
        f"{LOCAL_LLM_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("message", {}).get("content", "")


PROVIDER_CALLERS = {
    "groq": _call_groq,
    "gemini": _call_gemini,
    "local": _call_local,
}


def call_llm(role: str, system_prompt: str, user_prompt: str, json_mode: bool = False,
             max_tokens: int = 4096) -> str:
    """Walks this role's Groq -> Gemini -> Ollama fallback chain. Skips Gemini
    if no key is configured. On rate limits/errors, tries the next entry
    immediately; only sleeps once the whole chain has failed in one pass."""
    chain = FALLBACK_CHAINS[role]
    idx = 0
    attempt = 0
    current_max_tokens = max_tokens

    while True:
        attempt += 1
        entry = chain[idx]
        provider, model = entry["provider"], entry["model"]

        if provider == "gemini" and not gemini_configured:
            idx = (idx + 1) % len(chain)
            continue

        try:
            result = PROVIDER_CALLERS[provider](model, system_prompt, user_prompt,
                                                 current_max_tokens, json_mode)
            if idx != 0:
                print(f"  [info] Succeeded using fallback: {provider}/{model}")
            return result
        except Exception as e:
            print(f"  [warning] {provider}/{model} failed (attempt {attempt}): {e}")
            err_str = str(e)
            is_rate_limit = ("rate_limit_exceeded" in err_str or "tokens per" in err_str
                              or "429" in err_str or "quota" in err_str.lower())

            if idx + 1 < len(chain):
                idx += 1
                current_max_tokens = max_tokens
                print(f"  [info] Switching to next fallback: {chain[idx]['provider']}/{chain[idx]['model']}")
                continue

            # Whole chain exhausted this pass.
            if is_rate_limit:
                wait_seconds = _parse_retry_wait_seconds(err_str)
                print(f"  [info] All providers exhausted. Waiting {wait_seconds:.0f}s, then retrying from the top...")
                time.sleep(wait_seconds)
            else:
                backoff = min(30, 2 * attempt)
                print(f"  [info] All providers failed. Retrying from the top in {backoff}s...")
                time.sleep(backoff)
                if json_mode:
                    user_prompt += ("\n\nIMPORTANT: Respond with ONLY a single valid JSON object. "
                                     "No markdown, no code fences, no extra text before or after.")
            idx = 0
            current_max_tokens = max_tokens


def extract_code_block(text: str) -> str:
    """Pull raw HTML out of a ```html ... ``` fence if present. Falls back to
    locating <!DOCTYPE or <html directly, in case a reasoning model added
    commentary before/after the fence (or skipped the fence entirely)."""
    if "```html" in text:
        candidate = text.split("```html")[1].split("```")[0].strip()
    elif "```" in text:
        parts = text.split("```")
        candidate = parts[1].strip() if len(parts) >= 2 else text.strip()
    else:
        candidate = text.strip()

    # Safety net: if what we extracted doesn't look like HTML, search the
    # full text for the first real HTML tag and cut from there.
    lower = candidate.lower()
    if "<!doctype" not in lower and "<html" not in lower:
        lower_full = text.lower()
        idx = lower_full.find("<!doctype")
        if idx == -1:
            idx = lower_full.find("<html")
        if idx != -1:
            candidate = text[idx:].strip()
            if "```" in candidate:
                candidate = candidate.split("```")[0].strip()

    return candidate


def extract_files(text: str) -> dict:
    """Parse Agent 4's multi-file output. Files are wrapped like:
        ===FILE: app.py===
        ...code...
        ===ENDFILE===
    Falls back to treating the whole output as index.html if no markers exist."""
    files = {}
    pattern = re.compile(r"===FILE:\s*(.+?)\s*===\s*\n(.*?)===ENDFILE===", re.DOTALL)
    for match in pattern.finditer(text):
        filename = match.group(1).strip()
        content = match.group(2).strip()
        # strip accidental code fences the model may have added inside the block
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\s*\n", "", content)
            content = re.sub(r"\n```\s*$", "", content)
        files[filename] = content
    if not files:
        files["index.html"] = extract_code_block(text)
    return files


# ---------------------------------------------------------------------------
# AGENT 1: Requirement Planner
# ---------------------------------------------------------------------------

def agent1_requirement_planner(goal: str, run_dir: Path) -> str:
    print("\n[Agent 1] Requirement Planner is working...")

    path = run_dir / "requirements.md"
    # Look across ALL date folders and ALL requirementN folders for the most
    # recently modified requirements.md, since run_dir itself starts empty.
    previous_run_files = sorted(
        BASE_DIR.glob("*/requirement*/requirements.md"),
        key=lambda p: p.stat().st_mtime
    )
    previous_requirements = previous_run_files[-1].read_text(encoding="utf-8") if previous_run_files else None

    history = load_requirements_history()
    history_summary = "\n".join(f"- ({h['timestamp']}) {h['goal']}" for h in history) if history else None

    # Save a snapshot of the history DB into this run's own folder too, so
    # every run folder is self-contained.
    (run_dir / "requirements_history.json").write_text(
        json.dumps(history, indent=2), encoding="utf-8"
    )

    if goal is None and previous_requirements:
        print("[Agent 1] No new requirement provided. Re-using existing requirements.md as-is.")
        path.write_text(previous_requirements, encoding="utf-8")
        log_step(
            agent_name="Agent 1 - Requirement Planner",
            description="No new requirement given; reused existing requirements.md unchanged.",
            content=previous_requirements,
        )
        return previous_requirements

    if previous_requirements:
        system = (
            "You are a requirements analyst. You are given an EXISTING structured "
            "requirements document, the FULL HISTORY of every requirement ever given "
            "(oldest to newest), and a NEW user goal to add on top of it. Merge them "
            "into a single, updated requirements document in Markdown that keeps every "
            "still-relevant requirement from the existing document AND incorporates the "
            "new goal. Do not silently drop old requirements unless the new goal directly "
            "conflicts with them (in which case, keep the newer one and note the change). "
            "Include: Overview, Functional Requirements, Visual/UX Requirements, "
            "Technical Constraints."
        )
        user = (
            f"EXISTING requirements document:\n{previous_requirements}\n\n"
            f"FULL REQUIREMENT HISTORY (oldest to newest):\n{history_summary}\n\n"
            f"NEW user goal to add:\n{goal}\n\n"
            "Produce the full, merged requirements document."
        )
        description = "Merged the new user goal with the existing requirements.md and full history into an updated version."
    else:
        system = (
            "You are a requirements analyst. Turn a one-line user goal into a clear, "
            "structured requirements document in Markdown. Include: Overview, "
            "Functional Requirements, Visual/UX Requirements, Technical Constraints.\n\n"
            "CRITICAL: The Functional Requirements section is the most important. Even if "
            "the user only mentions UI/design, you MUST spell out the full working behavior "
            "of the app: what every button/control does when clicked, what inputs produce "
            "what outputs, and what a user can actually accomplish. A calculator must "
            "calculate, a form must submit, a game must be playable. Never produce "
            "requirements for a static, non-functional mockup."
        )
        user = f"User goal: {goal}"
        description = "Turned the raw user goal into a structured requirements.md."

    result = call_llm("planner", system, user)
    path.write_text(result, encoding="utf-8")
    print(f"[Agent 1] Done. Requirements saved to {path}")
    print("-" * 60)
    print(result)
    print("-" * 60)

    log_step(
        agent_name="Agent 1 - Requirement Planner",
        description=description,
        content=result,
    )
    return result


# ---------------------------------------------------------------------------
# AGENT 2: System Architect
# ---------------------------------------------------------------------------

def agent2_system_architect(requirements: str, run_dir: Path, critique: str = None) -> str:
    print("\n[Agent 2] System Architect is working...")
    system = (
        "You are a senior FULL-STACK software architect. Turn requirements into a DETAILED "
        "technical specification in Markdown for a complete application. First DECIDE whether "
        "the requirements need a backend (data storage, APIs, server logic, database) or can be "
        "a frontend-only app. State the decision at the very top as exactly "
        "'Architecture: frontend-only' or 'Architecture: frontend + backend'. "
        "Include: Architecture decision, Layout structure, Color palette, Typography, "
        "Animations/transitions, Component breakdown, Responsive behavior. "
        "If a backend is needed, ALSO include: full API endpoint list (method, path, request and "
        "response JSON), data storage design (JSON file or SQLite), and how the frontend calls "
        "each endpoint with fetch(). The backend must be ONE Python Flask file named app.py "
        "running on port 8000 that also serves index.html from the same folder. "
        "Be specific and advanced.\n\n"
        "CRITICAL CONTRAST RULE: When specifying the color palette, the text color "
        "must have strong contrast against the background at EVERY point it appears "
        "(minimum WCAG AA contrast ratio of 4.5:1). If the background is a gradient, "
        "pick a text color (typically white or near-black) that stays readable across "
        "the ENTIRE gradient range, not just one end of it. Do not pick a text color "
        "that is close in hue or lightness to any part of the background. State the "
        "exact hex codes for both and briefly justify why they contrast well."
    )
    user = f"Requirements:\n{requirements}"
    if critique:
        user += f"\n\nThe previous version of this spec was reviewed and needs improvement:\n{critique}\nPlease produce an improved, more detailed version."

    result = call_llm("architect", system, user, max_tokens=8192)
    if not result.strip():
        print("[Agent 2] FATAL: No spec was generated after retries. Stopping.")
        sys.exit(1)
    path = run_dir / "document.md"
    path.write_text(result, encoding="utf-8")
    print(f"[Agent 2] Done. Spec saved to {path}")
    print("-" * 60)
    print(result)
    print("-" * 60)

    description = (
        "Read Agent 1's requirements.md and wrote the technical spec as document.md."
        if not critique else
        "Revised document.md based on Agent 3's critique (see feedback Agent 2 received, below)."
    )
    log_step(
        agent_name="Agent 2 - System Architect",
        description=description,
        content=result,
        feedback_sent=None,  # feedback Agent 2 *received* is logged on Agent 3's entry
    )
    return result


# ---------------------------------------------------------------------------
# AGENT 3: QA Auditor (checklist-based scoring, not a guessed %)
# ---------------------------------------------------------------------------

def agent3_qa_auditor(requirements: str, spec: str, run_dir: Path) -> dict:
    print("\n[Agent 3] QA Auditor is checking the spec...")
    system = (
        "You are a meticulous QA auditor reviewing a technical spec against requirements. "
        "Score EACH checklist item as true/false, then return ONLY valid JSON in this exact shape:\n"
        '{"checklist": {"covers_all_functional_requirements": true/false, '
        '"defines_layout_clearly": true/false, "defines_visual_style": true/false, '
        '"defines_animations": true/false, "defines_responsiveness": true/false, '
        '"technically_feasible": true/false, "document_is_complete": true/false}, '
        '"critique": "short text explaining any false items"}\n\n'
        "For document_is_complete: check whether the spec ends properly (e.g. closes its "
        "last section/sentence/code block) rather than stopping abruptly mid-word, "
        "mid-sentence, or mid-list. If it looks cut off, mark this false."
    )
    user = f"Requirements:\n{requirements}\n\nSpec to review:\n{spec}"
    raw = call_llm("auditor", system, user, json_mode=True)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"checklist": {}, "critique": "Could not parse auditor output; defaulting to fail."}

    checklist = data.get("checklist", {})
    total = len(checklist) if checklist else 1
    passed = sum(1 for v in checklist.values() if v is True)
    score = round((passed / total) * 100)

    data["score"] = score
    print(f"[Agent 3] Score: {score}%  (passed {passed}/{total} checklist items)")
    print("-" * 60)
    for item, ok in checklist.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {item}")
    print(f"  Critique: {data.get('critique', '(none)')}")
    print("-" * 60)

    checklist_report = "\n".join(
        f"- [{'x' if ok else ' '}] {item}" for item, ok in checklist.items()
    )
    full_content = f"Checklist:\n{checklist_report}\n\nCritique: {data.get('critique', '(none)')}"

    # Save Agent 3's own report to disk, same as every other agent's output.
    verdict = "PASSED" if score >= ACCURACY_THRESHOLD else "BELOW THRESHOLD"
    report_md = (
        f"# QA Audit Report\n\n"
        f"**Score:** {score}%  ({verdict})\n\n"
        f"## Checklist\n{checklist_report}\n\n"
        f"## Critique\n{data.get('critique', '(none)')}\n"
    )
    report_path = run_dir / "audit_report.md"
    report_path.write_text(report_md, encoding="utf-8")
    print(f"[Agent 3] Report saved to {report_path}")

    log_step(
        agent_name="Agent 3 - QA Auditor",
        description="Scored Agent 2's document.md against the checklist.",
        content=full_content,
        score=score,
        feedback_sent=data.get("critique") if score < ACCURACY_THRESHOLD else None,
    )
    return data


# ---------------------------------------------------------------------------
# AGENT 4: Lead UI Engineer
# ---------------------------------------------------------------------------

def agent4_ui_engineer(spec: str, run_dir: Path, feedback: str = None) -> str:
    print("\n[Agent 4] Full-Stack Developer is generating code...")
    system = (
        "You are an expert FULL-STACK software developer. You are equally skilled at "
        "frontend (HTML/CSS/JS) and backend (Python Flask, REST APIs, databases). "
        "Implement the given spec COMPLETELY.\n\n"
        "OUTPUT FORMAT (STRICT) - wrap every file in these markers, nothing else:\n"
        "===FILE: index.html===\n<full file content>\n===ENDFILE===\n"
        "===FILE: app.py===\n<full file content>\n===ENDFILE===\n\n"
        "RULES:\n"
        "1. index.html must be a single self-contained file (inline CSS in <style>, inline JS "
        "in <script>). Make it visually advanced: gradients, smooth animations, modern layout.\n"
        "1b. THE APP MUST BE FULLY FUNCTIONAL, NOT A MOCKUP. Every button, input and control "
        "visible in the UI MUST have a working JavaScript event listener that performs its real "
        "action. A calculator must actually compute results; a list must actually add/delete "
        "items. NEVER ship a button that does nothing. Before finishing, mentally click every "
        "element and confirm it works. Also support keyboard input where natural (e.g. typing "
        "digits and Enter on a calculator).\n"
        "2. If the spec says 'Architecture: frontend-only', output ONLY index.html.\n"
        "3. If the spec needs a backend, ALSO output app.py: ONE Python Flask app on port 8000 "
        "that serves index.html from its own folder (use send_from_directory with '.') AND "
        "implements EVERY API endpoint in the spec. Store data in a local JSON file or SQLite "
        "database in the same folder. The frontend must call the APIs with fetch() using "
        "relative paths like /api/... .\n"
        "4. Backend may use ONLY Flask and the Python standard library - no other packages.\n"
        "5. Output ONLY the file markers and code. No explanations, no extra text."
    )
    user = f"Spec:\n{spec}"
    if feedback:
        user += f"\n\nThe previous version of this code was reviewed and needs improvement:\n{feedback}\nPlease produce an improved version of ALL files, complete and in full."

    raw = call_llm("coder", system, user, max_tokens=8192)
    if not raw.strip():
        print("[Agent 4] FATAL: No code was generated after retries. Stopping.")
        sys.exit(1)

    files = extract_files(raw)
    for filename, content in files.items():
        (run_dir / filename).write_text(content, encoding="utf-8")
        print(f"[Agent 4] Wrote {run_dir / filename}")

    code = "\n\n".join(f"----- FILE: {name} -----\n{content}" for name, content in files.items())
    print("-" * 60)
    print(code)
    print("-" * 60)

    description = (
        "Generated the full application code (frontend + backend if required) from Agent 2's document.md."
        if not feedback else
        "Revised the application code based on Agent 5's feedback (see feedback Agent 4 received, below)."
    )
    log_step(
        agent_name="Agent 4 - Full-Stack Developer",
        description=description,
        content=code,
    )
    return code


# ---------------------------------------------------------------------------
# AGENT 5: Senior Code Reviewer (checklist-based scoring)
# ---------------------------------------------------------------------------

def agent5_code_reviewer(spec: str, code: str, run_dir: Path) -> dict:
    print("\n[Agent 5] Code Reviewer is checking the code...")
    system = (
        "You are a senior FULL-STACK code reviewer. The code may contain multiple files "
        "(frontend index.html and optionally a Flask backend app.py), each marked with "
        "'----- FILE: name -----'. Score EACH checklist item as true/false, then return "
        "ONLY valid JSON in this exact shape:\n"
        '{"checklist": {"valid_html_structure": true/false, "matches_spec_layout": true/false, '
        '"has_animations_or_transitions": true/false, "responsive_design": true/false, '
        '"no_obvious_bugs": true/false, "clean_code_structure": true/false, '
        '"text_has_strong_contrast": true/false, '
        '"backend_correct_or_not_needed": true/false, '
        '"all_ui_elements_functional": true/false, '
        '"core_feature_actually_works": true/false}, '
        '"feedback": "short actionable text on what to improve if anything failed"}\n\n'
        "For all_ui_elements_functional: go through EVERY button/input/control in the HTML "
        "and verify each one is wired to a JavaScript event listener (onclick, addEventListener, "
        "etc.) that performs a real action. If ANY visible element does nothing when clicked, "
        "mark false and list the dead elements in feedback.\n\n"
        "For core_feature_actually_works: trace the JavaScript logic end-to-end for the app's "
        "main purpose (e.g. for a calculator: pressing 5, +, 2, Enter must display 7 - check "
        "the code path that reads the expression, evaluates it, and updates the display). "
        "If the main feature is incomplete, broken, or just a visual placeholder, mark false "
        "and explain exactly what is missing.\n\n"
        "For backend_correct_or_not_needed: if the spec says frontend-only and there is no "
        "backend, mark true. If a backend exists, verify app.py implements every endpoint in "
        "the spec, serves index.html, runs on port 8000, persists data correctly, and that "
        "the frontend fetch() paths actually match the Flask routes. Mark false and explain "
        "any mismatch.\n\n"
        "For text_has_strong_contrast: check the actual hex/rgb color values in the CSS. "
        "Mentally verify the text color has at least WCAG AA contrast (4.5:1) against the "
        "background at every point, including all stops of any gradient. If text color is "
        "close in hue or lightness to the background anywhere, mark this false and explain "
        "exactly which colors clash in the feedback."
    )
    user = f"Spec:\n{spec}\n\nCode to review:\n{code}"
    raw = call_llm("reviewer", system, user, json_mode=True)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"checklist": {}, "feedback": "Could not parse reviewer output; defaulting to fail."}

    checklist = data.get("checklist", {})
    total = len(checklist) if checklist else 1
    passed = sum(1 for v in checklist.values() if v is True)
    score = round((passed / total) * 100)

    data["score"] = score
    print(f"[Agent 5] Score: {score}%  (passed {passed}/{total} checklist items)")
    print("-" * 60)
    for item, ok in checklist.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {item}")
    print(f"  Feedback: {data.get('feedback', '(none)')}")
    print("-" * 60)

    checklist_report = "\n".join(
        f"- [{'x' if ok else ' '}] {item}" for item, ok in checklist.items()
    )
    full_content = f"Checklist:\n{checklist_report}\n\nFeedback: {data.get('feedback', '(none)')}"

    # Save Agent 5's own report to disk, same as every other agent's output.
    verdict = "PASSED" if score >= ACCURACY_THRESHOLD else "BELOW THRESHOLD"
    report_md = (
        f"# Code Review Report\n\n"
        f"**Score:** {score}%  ({verdict})\n\n"
        f"## Checklist\n{checklist_report}\n\n"
        f"## Feedback\n{data.get('feedback', '(none)')}\n"
    )
    report_path = run_dir / "review_report.md"
    report_path.write_text(report_md, encoding="utf-8")
    print(f"[Agent 5] Report saved to {report_path}")

    log_step(
        agent_name="Agent 5 - Code Reviewer",
        description="Scored Agent 4's code against the checklist.",
        content=full_content,
        score=score,
        feedback_sent=data.get("feedback") if score < ACCURACY_THRESHOLD else None,
    )
    return data


# ---------------------------------------------------------------------------
# AGENT 6: Deployment Engine (no LLM - just runs a local server)
# ---------------------------------------------------------------------------

class _QuietHTTPRequestHandler(__import__("http.server", fromlist=["SimpleHTTPRequestHandler"]).SimpleHTTPRequestHandler):
    """Same as SimpleHTTPRequestHandler, but skips noisy favicon.ico 404 logs."""
    def log_message(self, format, *args):
        if "favicon.ico" in (args[0] if args else ""):
            return
        super().log_message(format, *args)


def agent6_deployment_engine(run_dir: Path, server_state: dict, port: int = 8000):
    """Starts (or restarts) the app. If app.py exists (full-stack), it runs the
    Flask backend as a subprocess. Otherwise it serves index.html with a static
    server. Non-blocking. server_state tracks whatever is currently running."""
    import http.server
    import socketserver
    import threading
    import webbrowser
    import functools
    import subprocess

    print("\n[Agent 6] Deployment Engine starting...")

    # Shut down anything running from an earlier requirement cycle.
    if server_state.get("httpd"):
        print("[Agent 6] Stopping previous static server.")
        server_state["httpd"].shutdown()
        server_state["httpd"].server_close()
        server_state["httpd"] = None
    if server_state.get("process"):
        print("[Agent 6] Stopping previous backend process.")
        server_state["process"].terminate()
        try:
            server_state["process"].wait(timeout=5)
        except Exception:
            server_state["process"].kill()
        server_state["process"] = None

    if (run_dir / "app.py").exists():
        print("[Agent 6] Backend detected (app.py). Starting Flask server...")
        proc = subprocess.Popen([sys.executable, "app.py"], cwd=str(run_dir))
        server_state["process"] = proc
        time.sleep(2)  # give Flask a moment to boot
        url = f"http://localhost:{port}/"
    else:
        print("[Agent 6] Frontend-only app. Starting static server...")
        handler = functools.partial(_QuietHTTPRequestHandler, directory=str(run_dir))
        httpd = socketserver.TCPServer(("", port), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        server_state["httpd"] = httpd
        server_state["thread"] = thread
        url = f"http://localhost:{port}/index.html"

    print(f"[Agent 6] Running at {url}  (serving {run_dir})")
    webbrowser.open(url)
    print("[Agent 6] Browser opened.")


# ---------------------------------------------------------------------------
# ORCHESTRATOR - runs the whole pipeline automatically
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("AGENTIC SDLC PIPELINE STARTING")
    print("=" * 60)

    server_state = {"httpd": None, "thread": None, "process": None}

    while True:
        date_dir = get_today_date_dir()
        run_number = get_next_run_number(date_dir)
        run_dir = create_run_dir(date_dir, run_number)
        print(f"\n{'=' * 60}\nSTARTING CYCLE: {date_dir.name}/{run_dir.name}\n{'=' * 60}")

        # Ask in the terminal whether there's a new requirement to add this run.
        new_goal = ask_for_new_requirement()

        # Agent 1 (merges new_goal with everything saved so far, or reuses existing if new_goal is None)
        requirements = agent1_requirement_planner(new_goal, run_dir)

        run_history = {"phase1": [], "phase2": []}

        # Phase 1: Agent 2 <-> Agent 3 loop
        critique = None
        spec = None
        i = 0
        while True:
            i += 1
            print(f"\n--- Phase 1 Loop {i} ---")
            spec = agent2_system_architect(requirements, run_dir, critique)
            audit = agent3_qa_auditor(requirements, spec, run_dir)
            run_history["phase1"].append(audit["score"])
            if audit["score"] >= ACCURACY_THRESHOLD:
                print(f"[Phase 1] Spec approved at {audit['score']}% after {i} loop(s). Moving to Phase 2.")
                break
            critique = audit.get("critique", "Please improve the spec.")
            print(f"[Phase 1] Spec below threshold ({audit['score']}%). Sending back to Agent 2 (attempt {i + 1} next).")

        # Phase 2: Agent 4 <-> Agent 5 loop
        feedback = None
        code = None
        j = 0
        while True:
            j += 1
            print(f"\n--- Phase 2 Loop {j} ---")
            code = agent4_ui_engineer(spec, run_dir, feedback)
            review = agent5_code_reviewer(spec, code, run_dir)
            run_history["phase2"].append(review["score"])
            if review["score"] >= ACCURACY_THRESHOLD:
                print(f"[Phase 2] Code approved at {review['score']}% after {j} loop(s). Moving to deployment.")
                break
            feedback = review.get("feedback", "Please improve the code.")
            print(f"[Phase 2] Code below threshold ({review['score']}%). Sending back to Agent 4 (attempt {j + 1} next).")

        # Save the full agent-to-agent conversation + accuracy summary as Markdown
        save_conversation_log(run_history, run_dir)

        # Agent 6 - deploy this run's index.html (non-blocking now)
        agent6_deployment_engine(run_dir, server_state)

        # Ask whether to continue with another requirement.
        again = input("\nDo you have another new requirement? (yes/no): ").strip().lower()
        if again not in ("y", "yes"):
            print("[Info] No further requirements. Stopping the pipeline.")
            break

    # Pipeline is done generating, but keep the last deployed server alive
    # until the user manually stops it.
    if server_state.get("httpd") or server_state.get("process"):
        print("Press Ctrl+C to stop the server and exit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[Agent 6] Shutting down.")
            if server_state.get("httpd"):
                server_state["httpd"].shutdown()
                server_state["httpd"].server_close()
            if server_state.get("process"):
                server_state["process"].terminate()


if __name__ == "__main__":
    main()