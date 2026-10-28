#!/usr/bin/env python3
"""
VLe — a local Cursor agent. One question in, a debated answer out.

The brain is the Cursor SDK (cursor-sdk), not a chat-completions endpoint.
Each step is a local agent run. REA may read files. Everyone else is text-only.

Run it (Windows PowerShell):
    py -m venv .venv
    .venv\\Scripts\\Activate.ps1
    pip install -r requirements.txt
    py vle.py "your question"
    py vle.py --show-debate "your question"

The first live run opens a browser so you can sign in. Cursor stores that
login in ~/.cursor/sdk/auth.json. VLe reads that file. It does not use
CURSOR_API_KEY.

Test it with no key and no network (fake runtime):
    py agent.py --selftest

Rules live in RULES.md. Skills live in skills/. Both are loaded at run time.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

AGENT_NAME = "VLe"
WORK_DIR = Path(__file__).resolve().parent
RULES_FILE = WORK_DIR / "RULES.md"
KNOWLEDGE_FILE = WORK_DIR / "knowledge.txt"
SKILLS_DIR = WORK_DIR / "skills"

MODEL = os.environ.get("CURSOR_MODEL", "composer-2.5")
FAKE = os.environ.get("VLE_FAKE_MODEL") == "1"
LOGIN_DIR = WORK_DIR / "scripts"
AUTH_FILE = Path.home() / ".cursor" / "sdk" / "auth.json"
_RESOLVED_KEY: str | None = None
_BRIDGE_PATCHED = False

# Empty allowlist: the SDK offers no built-in tools, so the model can only talk.
TEXT_TOOLS: list[str] = []
# REA can open the tree you named. It cannot edit files or run a shell.
REA_TOOLS: list[str] = ["read", "grep", "glob", "ls"]

PIPELINE_SKILLS = {"humanizer", "reverse-engineer-anything", "using-agent-skills"}
INTERVIEW_SKILL = "interview-me"
MAX_SKILLS = 2

CODE_HINTS = (
    "how does this code",
    "how this code",
    "how the code",
    "understand the code",
    "understand this code",
    "reverse engineer",
)

DEBATERS = [
    {
        "name": "Direct",
        "stance": "Give the most straightforward precise answer. "
        "Trust the obvious reading of the question. No hedging.",
    },
    {
        "name": "Skeptic",
        "stance": "Assume the obvious answer is a trap. Hunt for edge "
        "cases, wrong assumptions, and off-by-one thinking. "
        "Answer only what survives that check.",
    },
    {
        "name": "Checker",
        "stance": "Answer only claims you can justify step by step. "
        "Show the steps. If a step cannot be justified, drop "
        "that claim and say what is unknown.",
    },
]

JUDGE_NAME = "Judge"
EXPECTED_STAGES = [
    "REA",
    "Router",
    "Direct (round 1)",
    "Skeptic (round 1)",
    "Checker (round 1)",
    "Direct (round 2)",
    "Skeptic (round 2)",
    "Checker (round 2)",
    "Judge",
    "Humanizer",
]


class Skill:
    def __init__(self, name: str, description: str, path: Path) -> None:
        self.name = name
        self.description = description
        self.path = path

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8", errors="replace")


def parse_frontmatter(text: str) -> dict[str, str]:
    """Read name and description from a SKILL.md frontmatter block."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    data: dict[str, str] = {}
    key: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal key, buf
        if key:
            data[key] = " ".join(part.strip() for part in buf if part.strip()).strip()
        key = None
        buf = []

    for line in text[3:end].splitlines():
        if line.startswith("---"):
            break
        if line and not line[0].isspace() and ":" in line:
            flush()
            raw_key, _, raw_val = line.partition(":")
            key = raw_key.strip()
            val = raw_val.strip()
            if val in {"", "|", ">", "|-", ">-", "|+", ">+"}:
                buf = []
            else:
                buf = [val.strip().strip("\"'")]
                flush()
        elif key is not None:
            buf.append(line)
    flush()
    return data


def load_skill_index() -> list[Skill]:
    skills: list[Skill] = []
    if not SKILLS_DIR.exists():
        return skills
    for path in sorted(SKILLS_DIR.rglob("SKILL.md")):
        meta = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        name = meta.get("name", "").strip()
        if not name:
            continue
        skills.append(Skill(name, meta.get("description", "").strip(), path))
    return skills


def skill_map(index: list[Skill]) -> dict[str, Skill]:
    return {skill.name: skill for skill in index}


def load_rules() -> str:
    if RULES_FILE.exists():
        return RULES_FILE.read_text(encoding="utf-8", errors="replace")
    return "(no RULES.md found — answer precisely, never invent facts)"


def load_knowledge() -> str:
    if not KNOWLEDGE_FILE.exists():
        return ""
    return KNOWLEDGE_FILE.read_text(encoding="utf-8", errors="replace").strip()[:2000]


def find_code_target(question: str) -> Path | None:
    """A path the user named, or this workspace when they ask how code works."""
    candidates: list[str] = []
    for match in re.finditer(r'"([^"]+)"|\'([^\']+)\'', question):
        candidates.append(next(group for group in match.groups() if group))
    for match in re.finditer(r"[A-Za-z]:\\[^\s\"']+", question):
        candidates.append(match.group(0))
    for match in re.finditer(r"(?:(?:\./|\.\\)[^\s\"']+)", question):
        candidates.append(match.group(0))
    for raw in candidates:
        path = Path(raw.strip())
        if not path.is_absolute():
            path = (WORK_DIR / path).resolve()
        if path.exists():
            return path
    lowered = question.lower()
    if any(hint in lowered for hint in CODE_HINTS):
        return WORK_DIR
    return None


def parse_json_object(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def fake_model(system: str, user: str) -> str:
    """Canned replies so --selftest can run the full pipeline offline.

    The Judge is recognized by the start of its prompt. The word "Judge"
    inside RULES.md must not steal the verdict.
    """
    if system.startswith(f"You are the {JUDGE_NAME}"):
        return json.dumps(
            {
                "winner": "Skeptic",
                "answer": "The answer is 42. (fake-model test answer)",
            }
        )
    if system.startswith("You are Humanizer"):
        marker = "WINNING ANSWER:"
        if marker in user:
            return user.split(marker, 1)[1].strip()
        return user.strip()
    if system.startswith("You are the skill router"):
        return json.dumps({"skills": [], "question": ""})
    if system.startswith("You are REA"):
        return "Evidence brief: files were listed. Unknowns remain where the files are silent."
    if "ROUND 2" in user:
        return (
            "Attack: the rival's weakest claim is its unnamed edge case. "
            "REVISED ANSWER: Revised answer, tightened after argument."
        )
    for debater in DEBATERS:
        if system.startswith(f"You are {debater['name']}"):
            return f"{debater['name']}'s precise answer to the question."
    return "Fake answer."


def finish_run(run) -> str:
    result = run.wait()
    if result.status != "finished":
        run_id = getattr(result, "id", "") or getattr(run, "id", "")
        raise RuntimeError(f"run failed: status={result.status} id={run_id}")
    return result.result or ""


def stored_api_key() -> str | None:
    """Read the key Cursor's browser login already saved. Never print it."""
    if not AUTH_FILE.exists():
        return None
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    key = data.get("apiKey")
    if not isinstance(key, str) or not key.strip():
        return None
    expires = data.get("apiKeyExpiresAtMs")
    if isinstance(expires, (int, float)) and expires <= time.time() * 1000:
        return None
    return key.strip()


def browser_login() -> None:
    """Mint a Cursor SDK login through the browser. The key stays in AUTH_FILE."""
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise RuntimeError(
            "Signing in needs Node.js 22 or newer on PATH. "
            "VLe does not read CURSOR_API_KEY."
        )
    if not (LOGIN_DIR / "node_modules" / "@cursor" / "sdk").exists():
        install = subprocess.run(
            [npm, "install", "--prefix", str(LOGIN_DIR)],
            cwd=LOGIN_DIR,
        )
        if install.returncode != 0:
            raise RuntimeError(
                "Could not install the Cursor sign-in package in scripts/."
            )
    login = subprocess.run([node, str(LOGIN_DIR / "cursor_login.mjs")], cwd=LOGIN_DIR)
    if login.returncode != 0:
        raise RuntimeError("Cursor sign-in did not finish.")


def resolve_api_key() -> str:
    global _RESOLVED_KEY
    if _RESOLVED_KEY:
        return _RESOLVED_KEY
    key = stored_api_key()
    if not key:
        print(
            "Opening a browser so you can sign in to Cursor. "
            "The login is saved by the Cursor SDK on this computer. "
            "No API key is placed in the environment."
        )
        browser_login()
        key = stored_api_key()
    if not key:
        raise RuntimeError(
            "Cursor sign-in did not leave a usable login in "
            f"{AUTH_FILE}."
        )
    _RESOLVED_KEY = key
    return key


def install_windows_bridge_fix() -> None:
    """Windows select() cannot wait on the bridge pipe. Poll the pipe instead."""
    global _BRIDGE_PATCHED
    if _BRIDGE_PATCHED or os.name != "nt":
        return
    import codecs

    import cursor_sdk._bridge as bridge

    def _read_discovery(process, timeout):
        if process.stderr is None:
            raise bridge.CursorSDKError("Bridge process stderr is unavailable")
        stderr_fd = process.stderr.fileno()
        was_blocking = os.get_blocking(stderr_fd)
        os.set_blocking(stderr_fd, False)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        deadline = time.monotonic() + timeout
        stderr_lines: list[str] = []
        pending = ""

        def take_line(line: str):
            stderr_lines.append(line)
            return bridge.parse_discovery_line(line)

        try:
            while time.monotonic() < deadline:
                chunk: bytes | None
                try:
                    chunk = os.read(stderr_fd, 8192)
                except (BlockingIOError, InterruptedError):
                    chunk = None
                except OSError as err:
                    if getattr(err, "winerror", None) in (232, 10035):
                        chunk = None
                    else:
                        raise
                if chunk:
                    pending += decoder.decode(chunk)
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        found = take_line(line + "\n")
                        if found is not None:
                            return found
                elif chunk == b"":
                    pending += decoder.decode(b"", final=True)
                    if pending:
                        found = take_line(pending if pending.endswith("\n") else pending + "\n")
                        if found is not None:
                            return found
                    raise bridge.CursorSDKError(
                        "Bridge exited before discovery with status "
                        f"{process.poll()}: {''.join(stderr_lines)}{pending}"
                    )
                if process.poll() is not None:
                    try:
                        extra = os.read(stderr_fd, 8192)
                    except OSError:
                        extra = b""
                    if extra:
                        pending += decoder.decode(extra)
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        found = take_line(line + "\n")
                        if found is not None:
                            return found
                    raise bridge.CursorSDKError(
                        "Bridge exited before discovery with status "
                        f"{process.poll()}: {''.join(stderr_lines)}{pending}"
                    )
                time.sleep(0.05)
            raise bridge.CursorSDKError("Timed out waiting for bridge discovery")
        finally:
            os.set_blocking(stderr_fd, was_blocking)

    bridge._read_discovery = _read_discovery
    _BRIDGE_PATCHED = True


def one_shot(
    system: str,
    user: str,
    tools: list[str],
    cwd: Path,
    record: list[list[str]] | None = None,
) -> str:
    if record is not None:
        record.append(list(tools))
    if FAKE:
        return fake_model(system, user)
    install_windows_bridge_fix()
    from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions

    try:
        result = Agent.prompt(
            f"{system}\n\n{user}",
            AgentOptions(
                api_key=resolve_api_key(),
                model=MODEL,
                tools=list(tools),
                local=LocalAgentOptions(cwd=str(cwd)),
            ),
        )
    except CursorAgentError as err:
        message = getattr(err, "message", str(err))
        retryable = getattr(err, "is_retryable", None)
        raise RuntimeError(
            f"startup failed: {message}, retryable={retryable}"
        ) from err
    if result.status != "finished":
        raise RuntimeError(
            f"run failed: status={result.status} id={getattr(result, 'id', '')}"
        )
    return result.result or ""


class TextSession:
    """One text-only Cursor agent. Round 2 is a follow-up send on the same agent."""

    def __init__(
        self,
        system: str,
        cwd: Path,
        record: list[list[str]] | None = None,
    ) -> None:
        self.system = system
        self.cwd = str(cwd)
        self.record = record
        self._cm = None
        self._agent = None
        self._first = True

    def __enter__(self) -> TextSession:
        if self.record is not None:
            self.record.append(list(TEXT_TOOLS))
        if FAKE:
            return self
        install_windows_bridge_fix()
        from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions

        try:
            self._cm = Agent.create(
                AgentOptions(
                    api_key=resolve_api_key(),
                    model=MODEL,
                    tools=list(TEXT_TOOLS),
                    local=LocalAgentOptions(cwd=self.cwd),
                )
            )
            self._agent = self._cm.__enter__()
        except CursorAgentError as err:
            self._close(None, None, None)
            message = getattr(err, "message", str(err))
            retryable = getattr(err, "is_retryable", None)
            raise RuntimeError(
                f"startup failed: {message}, retryable={retryable}"
            ) from err
        except Exception:
            self._close(*sys.exc_info())
            raise
        return self

    def _close(self, exc_type, exc, tb) -> None:
        cm = self._cm
        self._cm = None
        self._agent = None
        if cm is not None:
            cm.__exit__(exc_type, exc, tb)

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._close(exc_type, exc, tb)
        return False

    def send(self, user: str) -> str:
        if FAKE:
            return fake_model(self.system, user)
        prompt = f"{self.system}\n\n{user}" if self._first else user
        self._first = False
        return finish_run(self._agent.send(prompt))


def mark(stages: list[str], show: bool, name: str, text: str) -> None:
    stages.append(name)
    if show:
        print(f"\n--- {name} ---\n{text}")


def skill_block(skills: list[Skill]) -> str:
    if not skills:
        return ""
    chunks = ["Follow these skills for this question:"]
    for skill in skills:
        chunks.append(f"# Skill: {skill.name}\n{skill.text()}")
    return "\n\n".join(chunks)


def run_rea(
    question: str,
    stages: list[str],
    show: bool,
    index: dict[str, Skill],
    record: list[list[str]] | None,
) -> str:
    target = find_code_target(question)
    if target is None:
        brief = "no target"
        mark(stages, show, "REA", brief)
        return brief
    cwd = target if target.is_dir() else target.parent
    skill = index.get("reverse-engineer-anything")
    skill_text = f"\n\nFollow this skill:\n{skill.text()}" if skill else ""
    system = (
        f"You are REA, the first sub-agent of {AGENT_NAME}. "
        "Read the target and return an evidence brief: what you opened, "
        "what the files show, and what is still unknown. "
        "Explain the code. Do not write exploits, payloads, or bypass steps."
        f"{skill_text}\n\nRules:\n{load_rules()}"
    )
    user = f"Question: {question}\n\nTarget path: {target}"
    brief = one_shot(system, user, REA_TOOLS, cwd, record)
    mark(stages, show, "REA", brief)
    return brief


def run_router(
    question: str,
    brief: str,
    index: dict[str, Skill],
    stages: list[str],
    show: bool,
    record: list[list[str]] | None,
) -> tuple[list[Skill], str]:
    catalog = {
        name: skill
        for name, skill in index.items()
        if name not in PIPELINE_SKILLS
    }
    meta = index.get("using-agent-skills")
    meta_text = meta.text() if meta else "Pick the skill whose description matches the question."
    listing = "\n".join(
        f"- {skill.name}: {skill.description}" for skill in catalog.values()
    )
    system = (
        f"You are the skill router of {AGENT_NAME}. "
        "Follow the meta-skill below. Choose at most 2 catalog names. "
        "Reply with EXACTLY one JSON object and nothing else:\n"
        '{"skills": ["<catalog name>"], "question": ""}\n'
        "If the right skill is interview-me, return only that skill and put "
        "the single next question in \"question\".\n\n"
        f"Meta-skill:\n{meta_text}"
    )
    user = f"Question: {question}\n\nREA evidence brief:\n{brief}\n\nCatalog:\n{listing}"
    raw = one_shot(system, user, TEXT_TOOLS, WORK_DIR, record)
    mark(stages, show, "Router", raw)
    data = parse_json_object(raw) or {}
    names = data.get("skills") or []
    if isinstance(names, str):
        names = [names]
    chosen: list[Skill] = []
    for name in names:
        if not isinstance(name, str):
            continue
        skill = catalog.get(name.strip())
        if skill and all(skill.name != item.name for item in chosen):
            chosen.append(skill)
        if len(chosen) >= MAX_SKILLS:
            break
    if any(skill.name == INTERVIEW_SKILL for skill in chosen):
        chosen = [catalog[INTERVIEW_SKILL]]
    question_out = data.get("question") or ""
    if not isinstance(question_out, str):
        question_out = ""
    return chosen, question_out.strip()


def format_interview(question: str) -> str:
    ask = question.strip() or "What outcome do you want from this?"
    return f"[Skill: {INTERVIEW_SKILL}]\n{ask}"


def debater_system(debater: dict, skills: list[Skill]) -> str:
    parts = [
        f"You are {debater['name']}, a sub-agent of {AGENT_NAME}.",
        f"Your stance: {debater['stance']}",
        "Follow these rules exactly:",
        load_rules(),
    ]
    block = skill_block(skills)
    if block:
        parts.append(block)
    return "\n\n".join(parts)


def round1_user(question: str, brief: str, knowledge: str) -> str:
    parts = [
        f"Question: {question}",
        f"REA evidence brief:\n{brief}",
    ]
    if knowledge:
        parts.append(f"Known facts you may use:\n{knowledge}")
    parts.append(
        "Answer the question. Any claim about code must come from the evidence brief. "
        "If the brief does not show it, say that part is unknown."
    )
    return "\n\n".join(parts)


def round2_user(question: str, mine: str, others: str) -> str:
    return (
        f"ROUND 2 — ARGUMENT.\nQuestion: {question}\n\n"
        f"Rules:\n{load_rules()}\n\n"
        f"Your round 1 answer:\n{mine}\n\n"
        f"Rival answers:\n{others}\n\n"
        "Attack the single weakest claim in a rival answer, naming the "
        "exact claim (rule VLE-D1). Concede any of your claims that a "
        "rival answer proves wrong (rule VLE-D2). Then output your "
        "REVISED final answer, precise and complete, after the line "
        "'REVISED ANSWER:'."
    )


def revised_answer(reply: str) -> str:
    marker = "REVISED ANSWER:"
    if marker in reply:
        return reply.split(marker, 1)[1].strip()
    return reply.strip()


def debate_rounds(
    question: str,
    brief: str,
    chosen: list[Skill],
    stages: list[str],
    show: bool,
    record: list[list[str]] | None,
) -> str:
    knowledge = load_knowledge()
    opening = round1_user(question, brief, knowledge)
    sessions: list[tuple[dict, TextSession]] = []
    answers: dict[str, str] = {}
    try:
        for debater in DEBATERS:
            session = TextSession(debater_system(debater, chosen), WORK_DIR, record)
            session.__enter__()
            sessions.append((debater, session))
            first = session.send(opening)
            answers[debater["name"]] = first
            mark(stages, show, f"{debater['name']} (round 1)", first)

        packets: list[str] = []
        for debater, session in sessions:
            others = "\n\n".join(
                f"{name}: {text}"
                for name, text in answers.items()
                if name != debater["name"]
            )
            reply = session.send(round2_user(question, answers[debater["name"]], others))
            mark(stages, show, f"{debater['name']} (round 2)", reply)
            packets.append(
                f"{debater['name']}\n{reply}\n\nREVISED ANSWER USED:\n{revised_answer(reply)}"
            )
    finally:
        for _, session in reversed(sessions):
            session.__exit__(None, None, None)

    board = "\n\n".join(packets)
    judge_skills = skill_block(chosen)
    judge_system = (
        f"You are the {JUDGE_NAME}, the final scoring sub-agent of {AGENT_NAME}. "
        "You do not write a new answer. You pick ONE winner under rule "
        "VLE-D3 and return that winner's revised answer, cleaned only of wording "
        "that breaks a rule. Reply with EXACTLY one JSON object:\n"
        '{"winner": "<debater name>", "answer": "<the winning answer>"}\n\n'
        f"Rules:\n{load_rules()}"
    )
    if judge_skills:
        judge_system = f"{judge_system}\n\n{judge_skills}"
    verdict = one_shot(
        judge_system,
        f"Question: {question}\n\nREA evidence brief:\n{brief}\n\n"
        f"Arguments and revised answers:\n{board}",
        TEXT_TOOLS,
        WORK_DIR,
        record,
    )
    mark(stages, show, JUDGE_NAME, verdict)
    data = parse_json_object(verdict) or {}
    winner = str(data.get("winner", "?"))
    answer = data.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        answer = verdict.strip()
        winner = "?"

    human = index_humanizer()
    human_system = (
        "You are Humanizer, the last sub-agent of VLe. "
        "Embedded mode: return only the final text. "
        "Keep every claim, name, number, date, and quote. "
        "Leave JSON, code, and evidence lines unchanged. "
        "Do not add a fact that is not in the winning answer or the evidence brief.\n\n"
        f"Follow this skill:\n{human}\n\n"
        f"Rules:\n{load_rules()}"
    )
    rewritten = one_shot(
        human_system,
        f"REA evidence brief:\n{brief}\n\nWINNING ANSWER:\n{answer.strip()}",
        TEXT_TOOLS,
        WORK_DIR,
        record,
    ).strip()
    mark(stages, show, "Humanizer", rewritten)
    return f"[Winner: {winner}]\n{rewritten}"


def index_humanizer() -> str:
    for skill in load_skill_index():
        if skill.name == "humanizer":
            return skill.text()
    return "Rewrite the answer so it reads like a person. Keep every claim."


def run_pipeline(
    question: str,
    show: bool = False,
    stages: list[str] | None = None,
    record: list[list[str]] | None = None,
) -> str:
    stages = stages if stages is not None else []
    index = skill_map(load_skill_index())
    brief = run_rea(question, stages, show, index, record)
    chosen, interview_question = run_router(
        question, brief, index, stages, show, record
    )
    if any(skill.name == INTERVIEW_SKILL for skill in chosen):
        text = format_interview(interview_question)
        mark(stages, show, "Interview", text)
        return text
    return debate_rounds(question, brief, chosen, stages, show, record)


def selftest() -> int:
    global FAKE
    FAKE = True
    index = load_skill_index()
    names = [skill.name for skill in index]
    required = {
        "using-agent-skills",
        "humanizer",
        "reverse-engineer-anything",
        "uxui-evaluator",
        "interface-auditor",
        "ai-interface-reviewer",
        "flow-checker",
        "vibe-coding-advisor",
        "interview-me",
    }
    missing = sorted(required - set(names))
    files = list(SKILLS_DIR.rglob("SKILL.md"))
    unique = len(names) == len(set(names))
    named = len(index) == len(files) and all(names)
    print(f"Skills indexed: {len(index)}")

    stages: list[str] = []
    recorded: list[list[str]] = []
    result = run_pipeline(
        "What is the answer? (selftest)",
        show=True,
        stages=stages,
        record=recorded,
    )
    rea_record: list[list[str]] = []
    run_rea(
        "How does this code work?",
        stages=[],
        show=False,
        index=skill_map(index),
        record=rea_record,
    )
    interview = format_interview("")
    text_only = recorded == [list(TEXT_TOOLS)] * 6
    rea_tools_ok = rea_record == [list(REA_TOOLS)]
    order_ok = stages == EXPECTED_STAGES
    answer_ok = "Winner: Skeptic" in result and "42" in result
    interview_ok = interview.startswith(f"[Skill: {INTERVIEW_SKILL}]\n")
    ok = (
        not missing
        and unique
        and named
        and order_ok
        and answer_ok
        and text_only
        and rea_tools_ok
        and interview_ok
        and result.startswith("[Winner: Skeptic]\n")
        and "42" in result.split("\n", 1)[1]
    )
    if ok:
        print(
            "\nSELFTEST: PASS — skill index loaded, REA ran first, "
            "Humanizer ran last, judge picked a winner, winning answer returned."
        )
        return 0
    print(
        "\nSELFTEST: FAIL — "
        f"missing={missing or 'none'}, unique={unique}, named={named}, "
        f"stages={stages}, tools={recorded}, rea_tools={rea_record}, "
        f"result={result!r}"
    )
    return 1


def main() -> int:
    global FAKE
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    show = "--show-debate" in sys.argv
    if "--selftest" in sys.argv:
        return selftest()
    question = " ".join(args) or input(f"Ask {AGENT_NAME}: ")
    try:
        print(run_pipeline(question, show=show))
    except (RuntimeError, OSError) as err:
        print(f"Model call failed: {err}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
