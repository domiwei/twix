import discord
import asyncio
import os
import json
import re
import subprocess
import time
import shutil
import uuid
import urllib.request

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CLAUDE_BIN = shutil.which("claude") or "/usr/local/bin/claude"
CODEX_BIN = shutil.which("codex") or "/usr/local/bin/codex"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WORK_ROOT = "/root/work"

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

PREFIX = "!claude "
MAX_MSG_LEN = 2000

# ── Pikmin Agent Pool ─────────────────────────────────────────────────────────

PIKMIN_POOL = [
    {"name": "紅皮", "color": "red", "avatar": "https://pikmin.wiki.gallery/images/5/56/P4_Red_Pikmin.png"},
    {"name": "藍皮", "color": "blue", "avatar": "https://pikmin.wiki.gallery/images/5/54/P4_Blue_Pikmin.png"},
    {"name": "黃皮", "color": "yellow", "avatar": "https://pikmin.wiki.gallery/images/8/8d/P4_Yellow_Pikmin.png"},
    {"name": "紫皮", "color": "purple", "avatar": "https://pikmin.wiki.gallery/images/e/e7/Pikmin_4_Purple_Pikmin.png"},
    {"name": "白皮", "color": "white", "avatar": "https://pikmin.wiki.gallery/images/f/fc/Pikmin_4_White_Pikmin.png"},
    {"name": "岩皮", "color": "rock", "avatar": "https://pikmin.wiki.gallery/images/a/a8/P4_Rock_Pikmin.png"},
    {"name": "翼皮", "color": "winged", "avatar": "https://pikmin.wiki.gallery/images/9/9e/P4_Winged_Pikmin.png"},
    {"name": "冰皮", "color": "ice", "avatar": "https://pikmin.wiki.gallery/images/8/84/P4_Leaf_Ice_Pikmin_Artwork.png"},
    {"name": "光皮", "color": "glow", "avatar": "https://pikmin.wiki.gallery/images/0/03/Glow_Pikmin.png"},
    {"name": "蟲皮", "color": "bulbmin", "avatar": "https://pikmin.wiki.gallery/images/2/2b/Bulbmin_Pikmin_icon.png"},
]

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-6")
CLAUDE_BACKUP_HOME = os.environ.get("CLAUDE_BACKUP_HOME", "")
_account_mode: str = "primary"  # "primary" or "backup"

_USAGE_LIMIT_PATTERNS = [
    "usage limit reached", "usage_limit_reached", "Usage Limit",
    "exceeded your usage", "upgrade your plan", "Your account has",
    "free usage", "monthly limit", "quota exceeded", "rate_limit_error",
    "Claude.ai Usage", "usage has been exceeded",
]


def _get_account_env() -> dict | None:
    if _account_mode == "backup" and CLAUDE_BACKUP_HOME:
        return {"HOME": CLAUDE_BACKUP_HOME}
    return None


def _is_limit_text(text: str) -> bool:
    t = text.lower()
    return any(p.lower() in t for p in _USAGE_LIMIT_PATTERNS)


# ── Prompt loading ──────────────────────────────────────────────────────────
# Prompts live in prompts/*.md — edit those files to change agent behavior
# without touching Python code.

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def _load_prompt(filename: str) -> str:
    """Load a prompt from prompts/ directory. Exits if file missing."""
    path = os.path.join(_PROMPTS_DIR, filename)
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        print(f"[FATAL] Prompt file not found: {path}", flush=True)
        print(f"        Bot cannot start without prompt files. Check prompts/ directory.", flush=True)
        raise SystemExit(1)


SYSTEM_PROMPT = _load_prompt("system.md")

EVALUATOR_PROMPT = _load_prompt("evaluator.md")

EVALUATOR_MODEL = os.environ.get("EVALUATOR_MODEL", "claude-sonnet-4-6")
EVALUATOR_BACKEND = os.environ.get("EVALUATOR_BACKEND", "codex")  # "claude" or "codex"


def _parse_verdict(review: str) -> str:
    """Parse evaluator verdict from review text. Returns 'FAIL', 'PASS_WITH_SUGGESTIONS', or 'PASS'.

    The evaluator is instructed to put the verdict as the VERY LAST LINE in the format **VERDICT**.
    We check the last few lines to avoid false positives from words like 'fail' appearing
    in the body of the review (e.g. 'no failures', '0 fail').
    """
    if not review:
        return "PASS"
    # Check last 5 lines for the verdict marker
    last_lines = "\n".join(review.strip().splitlines()[-5:]).upper()
    if "PASS_WITH_SUGGESTIONS" in last_lines:
        return "PASS_WITH_SUGGESTIONS"
    if "**FAIL**" in last_lines:
        return "FAIL"
    # Also check bare FAIL on its own line (some models omit the **)
    for line in review.strip().splitlines()[-5:]:
        stripped = line.strip().upper()
        if stripped == "FAIL" or stripped == "**FAIL**":
            return "FAIL"
    if "**PASS**" in last_lines:
        return "PASS"
    # Fallback: scan the whole text but only match the exact verdict markers
    upper = review.upper()
    if "**PASS_WITH_SUGGESTIONS**" in upper:
        return "PASS_WITH_SUGGESTIONS"
    if "**FAIL**" in upper:
        return "FAIL"
    # No clear verdict found — default to PASS (avoid infinite fix loops)
    print(f"[EVALUATOR] WARNING: no clear verdict found in review, defaulting to PASS", flush=True)
    return "PASS"


PLANNER_MODEL = os.environ.get("PLANNER_MODEL", "claude-opus-4-6")
WRITE_TOOLS = {"Edit", "Write", "Bash", "NotebookEdit"}

PLANNER_PROMPT = _load_prompt("planner.md")


def monitor_system_prompt(config: dict) -> str:
    service_name = config.get("service_name", config.get("name", "unknown"))
    nickname = config.get("nickname", service_name)
    knowledge = config.get("knowledge", [])

    base = (
        f"You are '{nickname}', a performance monitoring assistant for the {service_name} service. "
        "You will receive periodic system metrics (CPU, memory, etc.). "
        "Analyze the data and determine if there are anomalies or optimization opportunities. "
        'Respond in JSON format: {"anomaly": true/false, "summary": "brief description", "details": "detailed analysis", "lesson": "optional — new insight learned from this check, or null if nothing new"}. '
        "The 'lesson' field should capture reusable observations, e.g. 'this service normally uses 80% CPU during sync', "
        "'log errors about peer disconnection are routine', etc. Only include a lesson when you genuinely learn something new "
        "that is NOT already in your existing knowledge. Set to null otherwise. "
        "Consider: memory leaks (steady growth), CPU spikes, unusual resource consumption patterns. "
        "Compare with previous readings when available. Be concise. "
        "Always write your analysis in Traditional Chinese (繁體中文).\n\n"
        "When the user asks you to perform operations (restart, deploy, clean data, pull code, etc.), "
        "and you are not certain WHERE to execute them (local machine vs remote host), "
        "you MUST ask the user to confirm before executing. Never guess."
    )

    if knowledge:
        base += "\n\nYou have accumulated the following domain knowledge from past experience. "
        base += "Use these to make better judgments:\n"
        for i, k in enumerate(knowledge, 1):
            base += f"{i}. {k}\n"

    return base


# ── Persistence ──────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
MONITORS_FILE = os.path.join(BASE_DIR, "monitors.json")
SUMMARIES_FILE = os.path.join(BASE_DIR, "summaries.json")


def _load_summaries() -> dict[str, dict]:
    """Load thread summaries. {channel_id_str: {"summary": str, "topic": str, "updated_at": float}}"""
    try:
        with open(SUMMARIES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return {}


def _save_summaries():
    with open(SUMMARIES_FILE, "w") as f:
        json.dump(thread_summaries, f, ensure_ascii=False)


def _load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        workdir = {int(k): v for k, v in data.get("workdir", {}).items()}
        session = {int(k): v for k, v in data.get("session", {}).items()}
        threads = set(int(t) for t in data.get("threads", []))
        worktrees = {int(k): v for k, v in data.get("worktrees", {}).items()}
        pikmin = {int(k): v for k, v in data.get("pikmin", {}).items()}
        return workdir, session, threads, worktrees, pikmin
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return {}, {}, set(), {}, {}


def _save_state():
    data = {
        "workdir": {str(k): v for k, v in channel_workdir.items()},
        "session": {str(k): v for k, v in channel_session.items()},
        "threads": list(bot_threads),
        "worktrees": {str(k): v for k, v in channel_worktrees.items()},
        "pikmin": {str(k): v for k, v in pikmin_assignments.items()},
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)


def _load_monitors():
    try:
        with open(MONITORS_FILE) as f:
            data = json.load(f)
        return data.get("monitors", {}), data.get("incidents", {})
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return {}, {}


def _save_monitors():
    data = {
        "monitors": monitor_configs,
        "incidents": active_incidents,
    }
    with open(MONITORS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


channel_workdir, channel_session, _loaded_threads, channel_worktrees, pikmin_assignments = _load_state()
# pikmin_assignments: channel_id -> pikmin_index (int) for threads/monitors
monitor_configs, active_incidents = _load_monitors()
thread_summaries: dict[str, dict] = _load_summaries()

# Runtime state (not persisted)
active_monitor_tasks: dict[str, asyncio.Task] = {}  # monitor_id -> task
monitor_histories: dict[str, list[dict]] = {}  # monitor_id -> metric history
pending_monitors: dict[int, dict] = {}  # channel_id -> pending monitor config
pending_monitor_setup: set[int] = set()  # channel_ids awaiting monitor description
pending_monitor_removals: dict[int, dict] = {}  # channel_id -> pending removal info
channel_session_ts: dict[int, float] = {}  # channel_id -> last session activity timestamp
channel_cumulative_turns: dict[int, int] = {}  # channel_id -> total turns since last relay
channel_relay_summaries: dict[int, list[str]] = {}  # channel_id -> recent relay summaries (max 5)
channel_last_usage: dict[int, dict] = {}  # channel_id -> last usage data from Claude
# channel_id -> git sha from `git stash create` taken just before the last generator
# turn. Used by run_evaluator to scope verdict to this turn's delta; prior-turn
# uncommitted WIP stays as context but is out of verdict scope.
channel_turn_snapshot: dict[int, str | None] = {}
_thread_wt_skip: set[int] = set()  # threads where branch detection returned None — skip future attempts
CONTEXT_WINDOW_TOKENS = 1_000_000  # Opus 4.6+ context window
CONTEXT_RELAY_THRESHOLD = int(os.environ.get("CONTEXT_RELAY_THRESHOLD", "80"))
_channel_locks: dict[int, asyncio.Lock] = {}  # per-channel lock to prevent concurrent processing
_cwd_locks: dict[str, asyncio.Lock] = {}  # per-cwd lock: two threads on the same working dir serialize


def _get_channel_lock(channel_id: int) -> asyncio.Lock:
    if channel_id not in _channel_locks:
        _channel_locks[channel_id] = asyncio.Lock()
    return _channel_locks[channel_id]


def _get_cwd_lock(cwd: str) -> asyncio.Lock:
    key = os.path.realpath(cwd) if cwd else ""
    if key not in _cwd_locks:
        _cwd_locks[key] = asyncio.Lock()
    return _cwd_locks[key]


# ── Pikmin Webhook Helpers ────────────────────────────────────────────────────

_webhook_cache: dict[int, discord.Webhook] = {}  # parent_channel_id -> webhook


async def _get_webhook(channel) -> discord.Webhook:
    """Get or create a webhook for the channel (or its parent if it's a thread)."""
    # Resolve to parent channel if this is a thread
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    parent_id = parent.id

    if parent_id in _webhook_cache:
        return _webhook_cache[parent_id]

    # Check if we already have a webhook
    try:
        webhooks = await parent.webhooks()
        for wh in webhooks:
            if wh.name == "Pikmin":
                _webhook_cache[parent_id] = wh
                return wh
    except Exception:
        pass

    # Create new webhook
    wh = await parent.create_webhook(name="Pikmin")
    _webhook_cache[parent_id] = wh
    return wh


def _assign_pikmin(channel_id: int, monitor_id: str | None = None) -> int:
    """Assign a pikmin index to a channel/monitor. Avoids duplicates when possible."""
    # If monitor has a pikmin already, use it
    if monitor_id:
        config = monitor_configs.get(monitor_id, {})
        if "pikmin_index" in config:
            return config["pikmin_index"]

    # If channel already has one, reuse
    if channel_id in pikmin_assignments:
        return pikmin_assignments[channel_id]

    # Find least-used pikmin
    used = set(pikmin_assignments.values())
    for mc in monitor_configs.values():
        if "pikmin_index" in mc:
            used.add(mc["pikmin_index"])

    for i in range(len(PIKMIN_POOL)):
        if i not in used:
            return i

    # All used, pick round-robin based on total assignments
    return len(pikmin_assignments) % len(PIKMIN_POOL)


def _get_pikmin(channel_id: int) -> dict | None:
    """Get the pikmin assigned to a channel, or None."""
    idx = pikmin_assignments.get(channel_id)
    if idx is not None and 0 <= idx < len(PIKMIN_POOL):
        return PIKMIN_POOL[idx]
    return None


async def pikmin_send(channel, content: str, pikmin: dict, **kwargs):
    """Send a message as a pikmin via webhook."""
    try:
        wh = await _get_webhook(channel)
        thread = channel if isinstance(channel, discord.Thread) else discord.utils.MISSING
        return await wh.send(
            content,
            username=pikmin["name"],
            avatar_url=pikmin["avatar"],
            thread=thread,
            wait=True,
            **kwargs,
        )
    except Exception as e:
        print(f"[PIKMIN] webhook send failed: {e}, falling back to normal send", flush=True)
        return await channel.send(f"**{pikmin['name']}**: {content}")


async def pikmin_edit(message, content: str, channel):
    """Edit a webhook message."""
    try:
        wh = await _get_webhook(channel)
        thread = channel if isinstance(channel, discord.Thread) else discord.utils.MISSING
        await wh.edit_message(message.id, content=content, thread=thread)
    except Exception:
        try:
            await message.edit(content=content)
        except Exception:
            pass

bot_threads: set[int] = _loaded_threads


# ── Utility ──────────────────────────────────────────────────────────────────

def list_projects() -> list[str]:
    if not os.path.isdir(WORK_ROOT):
        return []
    return sorted(
        d for d in os.listdir(WORK_ROOT)
        if os.path.isdir(os.path.join(WORK_ROOT, d))
    )


def detect_project(text: str) -> str | None:
    projects = list_projects()
    text_lower = text.lower()
    for proj in projects:
        if proj.lower() in text_lower:
            return proj
    return None


def split_message(text: str, limit: int = 0) -> list[str]:
    max_len = limit or MAX_MSG_LEN
    chunks = []
    while len(text) > max_len:
        # Try splitting at newline
        split_at = text.rfind("\n", 0, max_len)
        if split_at <= 0:
            # Try splitting at space
            split_at = text.rfind(" ", 0, max_len)
        if split_at <= 0:
            # Hard cut
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n ")
    if text:
        chunks.append(text)
    return chunks


def parse_prompt(content: str, bot_id: int) -> str | None:
    mention_prefix = f"<@{bot_id}>"
    if content.startswith(PREFIX):
        return content[len(PREFIX):].strip()
    elif content.startswith(mention_prefix):
        return content[len(mention_prefix):].strip()
    return None


# ── Intent Classification ────────────────────────────────────────────────────

BRANCH_NAME_RE = re.compile(r"\b([a-zA-Z0-9_-]+/[a-zA-Z0-9_.#-]+)\b")

INTENT_SYSTEM_PROMPT = """\
You are an intent classifier for a Discord bot. Given a user message and context, classify the intent.

Available intents:
- monitor_add: User wants to SET UP recurring/continuous monitoring of a service (e.g. "監控 erigon", "watch nginx", "幫我盯著", "定期檢查"). One-time status checks like "看看狀況", "檢查一下", "跑得怎樣" are NOT monitor_add — those are "chat".
- monitor_remove: User EXPLICITLY wants to stop/remove the monitor itself (e.g. "停止監控", "別監控了", "取消監控", "把監控關掉"). Must mention "監控" or "monitor" explicitly.
- monitor_list: User wants to see active monitors (e.g. "有哪些監控", "list monitors")
- monitor_confirm: User confirms a pending monitor setup (e.g. "ok", "好", "確認", "開始吧", "就這樣")
- monitor_adjust: User wants to change pending monitor settings (e.g. "加 log", "改成每10分鐘", "不用 prometheus")
- monitor_teach: User wants to teach a monitor something, add knowledge or rules (e.g. "告訴阿鏈...", "阿鏈記一下...", "這個錯誤不重要", "peer disconnected 不用管")
- monitor_resume: User wants to restart/resume a stopped monitor (e.g. "重啟監控", "恢復監控", "把監控開回來", "resume monitor")
- monitor_move: User wants to move a monitor's reporting to a different channel (e.g. "阿鏈去 #ops 回報", "把 erigon 監控搬到 #alerts", "monitor 改到 #ops")
- branch_task: User wants to create a branch/worktree for parallel work (e.g. "開 branch fix/issue-42 在 erigon")
- task_done: User says a task is complete (e.g. "做完了", "收工", "done")
- task_list: User wants to list active tasks (e.g. "有哪些 task")
- monitor_dismiss: User dismisses an alert as not a real problem in an incident thread (e.g. "這不是問題", "誤報", "false alarm", "沒事", "正常的")
- fix: User confirms they want the bot to fix an issue (e.g. "修吧", "go ahead and fix it")
- restart: User wants to restart a service (e.g. "跑看看", "restart it")
- create_thread: User wants to open a discussion thread (e.g. "開個 thread 討論")
- coding: User wants to implement, modify, create, or fix code in the codebase (e.g. "implement X", "fix bug in Y", "add feature Z", "幫我寫", "幫我實作", "幫我改", "修這個 bug"). Only classify as coding when the user wants actual code changes — not just explanations or questions about code.
- codex_review: User wants Codex to review code changes (e.g. "幫我 review 一下", "codex review", "review 這次改動", "看看有沒有問題", "review --base main"). Distinct from "coding" — this is reviewing existing changes, not making new ones.
- chat: General conversation, questions, or anything that doesn't match the above

Important:
- If the user is DECLINING an action (e.g. "先不要修", "不用", "別重啟"), classify as "chat", not the action.
- "關掉吧", "先關掉", "關掉它" etc. referring to the monitored service/process (not the monitor itself) should be classified as "chat", not "monitor_remove". Only classify as "monitor_remove" when the user explicitly mentions stopping the MONITOR/監控 itself.
- Inside an incident thread, the user is talking about the monitored service, NOT the monitor. Do NOT classify as "monitor_remove" in incident threads — use "chat" instead.
- monitor_confirm/monitor_adjust only apply when context says there is a pending monitor setup.
- monitor_teach applies when user talks to a monitor by nickname or wants to teach/tell a monitor something. Context will list active monitor nicknames.
- When context says "User is replying to monitor ... webhook message", the user is talking TO or ABOUT that monitor, not the underlying service. Classify accordingly (e.g. monitor_resume, monitor_remove, monitor_teach). "restart" is only for restarting the monitored service itself in an incident thread.
- Asking about a service's status, health, or logs (e.g. "看看狀況", "跑得怎樣", "檢查一下", "process 還活著嗎") is "chat", NOT "monitor_add". Only classify as "monitor_add" when the user clearly wants to set up ongoing/recurring monitoring.
- If unsure, default to "chat".

Respond with ONLY a JSON object: {"intent": "<intent_name>"}"""

INTENT_MODEL = os.environ.get("INTENT_MODEL", "claude-haiku-4-5-20251001")


async def classify_intent(text: str, context: str = "") -> str:
    """Use Claude (Haiku) to classify user intent. Returns intent string."""
    prompt = text
    if context:
        prompt = f"[Context: {context}]\n\nUser message: {text}"

    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--model", INTENT_MODEL,
        "--system-prompt", INTENT_SYSTEM_PROMPT,
        "--output-format", "json",
        "--max-turns", "1",
    ]

    for attempt in range(2):
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=WORK_ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            raw = stdout.decode("utf-8", errors="replace").strip()

            if not raw:
                print(f"[INTENT] empty response (attempt {attempt + 1})", flush=True)
                if attempt == 0:
                    await asyncio.sleep(2)
                    continue
                return "chat"

            # Parse — could be wrapped in Claude JSON envelope
            print(f"[INTENT] raw response: {raw[:300]}", flush=True)
            data = json.loads(raw)

            def _extract_intent(result_text: str) -> str:
                """Extract intent from result text, handling markdown code blocks."""
                if not result_text:
                    return "chat"
                # Strip markdown code blocks
                cleaned = re.sub(r'```(?:json)?\s*', '', result_text).strip()
                try:
                    inner = json.loads(cleaned)
                    return inner.get("intent", "chat")
                except (json.JSONDecodeError, TypeError):
                    pass
                # Try to find JSON inside the text
                jm = re.search(r'\{[^}]*"intent"\s*:\s*"([^"]+)"', result_text)
                if jm:
                    return jm.group(1)
                return "chat"

            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict) and entry.get("type") == "result":
                        intent = _extract_intent(entry.get("result", ""))
                        print(f"[INTENT] '{text[:50]}' -> {intent}", flush=True)
                        return intent
            elif isinstance(data, dict):
                if "result" in data:
                    intent = _extract_intent(data["result"])
                    print(f"[INTENT] '{text[:50]}' -> {intent}", flush=True)
                    return intent
                if "intent" in data:
                    print(f"[INTENT] '{text[:50]}' -> {data['intent']}", flush=True)
                    return data["intent"]
        except Exception as e:
            print(f"[INTENT] classify error (attempt {attempt + 1}): {e}", flush=True)
            if attempt == 0:
                await asyncio.sleep(2)
                continue

    return "chat"


def parse_branch_task(text: str) -> tuple[str | None, str | None, str]:
    """Extract (project, branch, description) from natural language."""
    branch_match = BRANCH_NAME_RE.search(text)
    branch = branch_match.group(1) if branch_match else None
    project = detect_project(text)
    desc = text
    if branch:
        desc = desc.replace(branch, "").strip()
    if project:
        desc = desc.replace(project, "").strip()
    desc = re.sub(r"(開個?|建立?|create|open|start|幫我|在|的|上|做|個|on)\s*branch\s*", "", desc, flags=re.IGNORECASE).strip()
    desc = re.sub(r"\b(on|in)\b", "", desc, flags=re.IGNORECASE).strip()
    desc = re.sub(r"^[\s,，、在的上幫我開做個]+", "", desc).strip()
    desc = re.sub(r"[\s,，、在的上]+$", "", desc).strip()
    return project, branch, desc or (branch or "task")


# ── Worktree ─────────────────────────────────────────────────────────────────

async def create_worktree(repo_path: str, branch_name: str) -> str:
    dir_suffix = branch_name.replace("/", "-")
    repo_basename = os.path.basename(repo_path)
    worktree_path = os.path.join(WORK_ROOT, f"{repo_basename}-wt-{dir_suffix}")

    if os.path.exists(worktree_path):
        shutil.rmtree(worktree_path)

    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", "-b", branch_name, worktree_path,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        proc2 = await asyncio.create_subprocess_exec(
            "git", "worktree", "add", worktree_path, branch_name,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr2 = await proc2.communicate()
        if proc2.returncode != 0:
            raise RuntimeError(stderr.decode() + "\n" + stderr2.decode())

    return worktree_path


async def remove_worktree(repo_path: str, worktree_path: str):
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "remove", "--force", worktree_path,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.wait()
    if os.path.exists(worktree_path):
        shutil.rmtree(worktree_path)


async def find_main_repo_path(cwd: str) -> str | None:
    """Given a cwd (possibly a worktree), return the main repo path, or None if not a git repo."""
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "--git-common-dir",
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    gitdir = stdout.decode().strip()
    if not gitdir:
        return None
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(cwd, gitdir)
    gitdir = os.path.realpath(gitdir)
    if gitdir.endswith(os.sep + ".git") or gitdir.endswith("/.git"):
        return gitdir[:-len("/.git")]
    return None


async def find_worktree_for_branch(repo_path: str, branch_name: str) -> str | None:
    """Return path of existing worktree checked out at branch, or None."""
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "list", "--porcelain",
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    target_ref = f"refs/heads/{branch_name}"
    current_path = None
    for line in stdout.decode().splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):]
        elif line.startswith("branch ") and line[len("branch "):] == target_ref:
            return current_path
    return None


async def branch_exists(repo_path: str, branch_name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}",
        cwd=repo_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def ensure_worktree_for_branch(repo_path: str, branch_name: str) -> str | None:
    """Find an existing worktree for branch; create a fresh one under WORK_ROOT if branch exists but has no worktree."""
    existing = await find_worktree_for_branch(repo_path, branch_name)
    if existing:
        return existing
    if not await branch_exists(repo_path, branch_name):
        return None
    dir_suffix = branch_name.replace("/", "-")
    repo_basename = os.path.basename(repo_path)
    worktree_path = os.path.join(WORK_ROOT, f"{repo_basename}-wt-{dir_suffix}")
    if os.path.exists(worktree_path):
        # Stale directory not registered as a worktree — don't clobber.
        print(f"[WORKTREE] path exists but not registered, skipping: {worktree_path}", flush=True)
        return None
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", worktree_path, branch_name,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        print(f"[WORKTREE] add failed for {branch_name}: {stderr.decode()[:200]}", flush=True)
        return None
    return worktree_path


# ── Claude CLI ───────────────────────────────────────────────────────────────

def _format_tool_desc(name: str, input_data: dict) -> str:
    if name == "Read":
        path = input_data.get("file_path", "")
        return f"📖 讀取 `{os.path.basename(path)}`"
    elif name == "Edit":
        path = input_data.get("file_path", "")
        return f"✏️ 編輯 `{os.path.basename(path)}`"
    elif name == "Write":
        path = input_data.get("file_path", "")
        return f"📝 寫入 `{os.path.basename(path)}`"
    elif name == "Bash":
        cmd = input_data.get("command", "")
        return f"💻 `{cmd[:60]}`"
    elif name == "Grep":
        pattern = input_data.get("pattern", "")
        return f"🔍 搜尋 `{pattern[:40]}`"
    elif name == "Glob":
        pattern = input_data.get("pattern", "")
        return f"📁 找檔案 `{pattern[:40]}`"
    elif name in ("WebSearch", "WebFetch"):
        return f"🌐 {name}"
    elif name == "Agent":
        desc = input_data.get("description", "")
        return f"🤖 子任務：{desc[:40]}"
    else:
        return f"🔧 {name}"


CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "7200"))  # 2h default — generator turns
EVALUATOR_TIMEOUT = int(os.environ.get("EVALUATOR_TIMEOUT", "1800"))  # 30m default — evaluator / review passes
PROGRESS_INTERVAL = int(os.environ.get("PROGRESS_INTERVAL", "300"))  # seconds between progress pings (0 = off)


async def _run_claude_stream(cmd: list[str], cwd: str, status_msg=None, timeout: int | None = None, env_override: dict | None = None) -> tuple[str, str | None, list[str], int, dict, bool]:
    env = {**os.environ, **(env_override or {})}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=1024 * 1024,  # 1MB line buffer (default 64KB too small for large tool output)
    )

    result_text = ""
    session_id = None
    tools_used: list[str] = []
    assistant_texts: list[str] = []  # collect text blocks as fallback
    usage_data: dict = {}  # token usage from result event
    num_turns = 0
    is_limit_error = False
    stderr_lines: list[str] = []
    current_status = "🤔 思考中..."
    last_update = 0.0
    UPDATE_INTERVAL = 2.0

    async def update_status(new_status: str):
        nonlocal last_update, current_status
        current_status = new_status
        if not status_msg:
            return
        now = time.monotonic()
        if now - last_update >= UPDATE_INTERVAL:
            last_update = now
            display = current_status
            if tools_used:
                display += f"\n\n📊 已執行 {len(tools_used)} 個步驟"
                recent = tools_used[-3:]
                display += "\n" + "\n".join(f"  {t}" for t in recent)
            try:
                await status_msg.edit(content=display)
            except Exception:
                pass

    async def read_stderr():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                stderr_lines.append(text)
                print(f"[CLAUDE STDERR] {text[:200]}", flush=True)

    async def read_events():
        nonlocal result_text, session_id, is_limit_error
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                print(f"[CLAUDE] {text}", flush=True)
                if _is_limit_text(text):
                    is_limit_error = True
                continue

            etype = event.get("type", "")
            if etype == "error":
                err_str = json.dumps(event.get("error", event))
                print(f"[CLAUDE] error event: {err_str[:200]}", flush=True)
                if _is_limit_text(err_str):
                    is_limit_error = True
            elif etype == "assistant":
                msg = event.get("message", {})
                block_types = [b.get("type") for b in msg.get("content", []) if isinstance(b, dict)]
                print(f"[CLAUDE] assistant event: blocks={block_types}", flush=True)
                for block in msg.get("content", []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        name = block.get("name", "")
                        inp = block.get("input", {})
                        desc = _format_tool_desc(name, inp)
                        tools_used.append(desc)
                        print(f"[CLAUDE] tool: {desc}", flush=True)
                        await update_status(f"⏳ {desc}")
                    elif block.get("type") == "text" and block.get("text"):
                        txt = block["text"]
                        assistant_texts.append(txt)
                        print(f"[CLAUDE] text: ({len(txt)} chars) {txt[:80]}...", flush=True)
                        await update_status("💬 撰寫回覆中...")
            elif etype == "result":
                nonlocal num_turns, usage_data
                result_text = event.get("result", "")
                session_id = event.get("session_id")
                cost = event.get("total_cost_usd", 0)
                num_turns = event.get("num_turns", 0)
                usage_data = event.get("usage", {})
                if event.get("is_error") and _is_limit_text(result_text):
                    is_limit_error = True
                print(f"[CLAUDE] done: {num_turns} turns, ${cost:.4f}, result_len={len(result_text)}, input_tokens={usage_data.get('input_tokens', '?')}", flush=True)
                return  # Don't wait for EOF — child processes may keep stdout open

    start_time = time.monotonic()

    async def _progress_reporter():
        await asyncio.sleep(PROGRESS_INTERVAL)
        while True:
            elapsed = int(time.monotonic() - start_time)
            minutes, secs = divmod(elapsed, 60)
            channel = getattr(status_msg, 'channel', None)
            if channel:
                recent = tools_used[-5:] if tools_used else []
                tools_str = "\n".join(f"　{t}" for t in recent) if recent else "　（尚無工具呼叫）"
                try:
                    await channel.send(
                        f"⏱️ **進度更新** — 已執行 {minutes} 分鐘 {secs} 秒\n"
                        f"已完成 {len(tools_used)} 個步驟，目前：{current_status}\n"
                        f"最近操作：\n{tools_str}"
                    )
                except Exception:
                    pass
            await asyncio.sleep(PROGRESS_INTERVAL)

    async def _do_stream():
        events_task = asyncio.create_task(read_events())
        stderr_task = asyncio.create_task(read_stderr())
        progress_task = asyncio.create_task(_progress_reporter()) if status_msg and PROGRESS_INTERVAL > 0 else None
        # Wait for events (returns when result event received or EOF)
        await events_task
        # Don't wait for stderr — child processes (e.g. http.server) may keep pipes open
        stderr_task.cancel()
        if progress_task:
            progress_task.cancel()
        # Ensure the Claude process is fully stopped before we return.
        # Without this, background tasks (e.g. bash monitor scripts) keep
        # the process alive, and the next --resume spawns a competing
        # process on the same session — corrupting context.
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            print(f"[CLAUDE] process still alive after result, killing pid={proc.pid}", flush=True)
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        except Exception:
            pass

    effective_timeout = timeout if timeout is not None else CLAUDE_TIMEOUT
    try:
        await asyncio.wait_for(_do_stream(), timeout=effective_timeout)
    except asyncio.TimeoutError:
        print(f"[CLAUDE] TIMEOUT after {effective_timeout}s, killing process", flush=True)
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        # Don't discard collected content — append timeout notice and let it through
        if assistant_texts:
            assistant_texts.append(f"\n\n⏰ *（已執行 {effective_timeout // 60} 分鐘，自動截止。以上是截止前的分析。）*")
        if status_msg:
            try:
                await status_msg.edit(content=f"⏰ 已執行 {effective_timeout // 60} 分鐘，整理已有結果...")
            except Exception:
                pass
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    # stream-json: result field often only contains the LAST turn's text,
    # which may be a short progress note while the real answer is in earlier turns.
    # Strategy: if all_assistant_text is meaningfully longer, use it — it contains
    # the complete response the user would see in an interactive session.
    all_assistant_text = "\n\n".join(assistant_texts) if assistant_texts else ""
    if not result_text:
        result_text = all_assistant_text
        if result_text:
            print(f"[CLAUDE] result empty, using all assistant_texts (len={len(result_text)})", flush=True)
    elif all_assistant_text and len(all_assistant_text) > len(result_text) * 2:
        # all_assistant_text has substantially more content — result likely
        # only captured the last turn's text, not the full response.
        print(f"[CLAUDE] result partial ({len(result_text)} chars), using all assistant_texts ({len(all_assistant_text)} chars)", flush=True)
        result_text = all_assistant_text

    # Last resort: ask Claude to summarize via --resume
    if not result_text and session_id:
        print(f"[CLAUDE] result empty, asking for summary via --resume", flush=True)
        try:
            summary_proc = await asyncio.create_subprocess_exec(
                CLAUDE_BIN, "-p",
                "請用繁體中文簡短總結你剛才做了什麼、結果如何。不要用工具，直接回答。",
                "--resume", session_id,
                "--output-format", "text",
                "--max-turns", "1",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            s_stdout, _ = await asyncio.wait_for(summary_proc.communicate(), timeout=60)
            summary = s_stdout.decode("utf-8", errors="replace").strip()
            if summary:
                result_text = summary
                print(f"[CLAUDE] got summary (len={len(summary)})", flush=True)
        except Exception as e:
            print(f"[CLAUDE] summary failed: {e}", flush=True)

    if not result_text:
        print(f"[CLAUDE] WARNING: no result from any source", flush=True)

    # Check stderr for usage limit signals
    if not is_limit_error and stderr_lines:
        stderr_combined = "\n".join(stderr_lines)
        if _is_limit_text(stderr_combined):
            is_limit_error = True
            print(f"[CLAUDE] usage limit detected in stderr", flush=True)

    return result_text, session_id, tools_used, num_turns, usage_data, is_limit_error


async def fetch_thread_history(channel, limit: int = 20, max_chars: int = 8000) -> str:
    messages = []
    total = 0
    async for msg in channel.history(limit=limit, oldest_first=True):
        role = "assistant" if msg.author == client.user else "user"
        # Truncate long messages but keep enough context
        text = msg.content[:1500] if role == "assistant" else msg.content
        entry = f"[{role}] {text}"
        if total + len(entry) > max_chars:
            break
        messages.append(entry)
        total += len(entry)
    return "\n".join(messages)


MAX_TURNS = int(os.environ.get("MAX_TURNS", "200"))


async def _kill_stale_session_procs(session_id: str) -> None:
    """Kill any Claude CLI processes still --resume-ing this session.

    This handles the case where a previous bot instance (or a timed-out run
    whose process wasn't fully reaped) left a zombie Claude process attached
    to the same session JSONL. Without cleanup, two writers on one session
    corrupt the conversation context.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "pgrep", "-f", f"--resume {session_id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if not out:
            return
        my_pid = str(os.getpid())
        for line in out.decode().strip().split("\n"):
            pid = line.strip()
            if pid and pid != my_pid:
                print(f"[CLAUDE] killing stale session process pid={pid} session={session_id}", flush=True)
                try:
                    os.kill(int(pid), 9)
                except (ProcessLookupError, ValueError):
                    pass
    except Exception as e:
        print(f"[CLAUDE] _kill_stale_session_procs error: {e}", flush=True)


async def run_claude(prompt: str, cwd: str, session_id: str | None = None,
                     status_msg=None, channel=None, system_prompt: str | None = None,
                     max_turns: int | None = None) -> tuple[str, str | None, list[str], int, dict]:
    sys_prompt = system_prompt or SYSTEM_PROMPT

    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--model", CLAUDE_MODEL,
        "--system-prompt", sys_prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", str(max_turns if max_turns is not None else MAX_TURNS),
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
        # Kill any leftover Claude process that is still --resume-ing this
        # session (e.g. from before a bot restart when in-memory locks were
        # lost). Without this, two processes write to the same session JSONL.
        await _kill_stale_session_procs(session_id)

    # cwd-level lock: two threads/channels that land on the same working dir
    # must serialize their Claude runs or they'll stomp on each other's git state.
    async with _get_cwd_lock(cwd):
        # Snapshot working tree before generator runs. run_evaluator reads this
        # to scope the verdict to what THIS turn changed, while still showing the
        # full WIP as context. No-op outside a git repo.
        if channel is not None:
            channel_turn_snapshot[channel.id] = await _git_stash_snapshot(cwd)

        result, new_session_id, tools, turns, usage, is_limit = await _run_claude_stream(cmd, cwd, status_msg, env_override=_get_account_env())

        # If primary account hit usage limit, switch to backup and retry
        if is_limit and CLAUDE_BACKUP_HOME:
            global _account_mode
            if _account_mode == "primary":
                _account_mode = "backup"
                print(f"[CLAUDE] usage limit on primary — switching to backup (HOME={CLAUDE_BACKUP_HOME})", flush=True)
                if channel:
                    try:
                        await channel.send("⚠️ 主帳號已達用量上限，切換備用帳號重試...")
                    except Exception:
                        pass
            result, new_session_id, tools, turns, usage, _ = await _run_claude_stream(cmd, cwd, status_msg, env_override={"HOME": CLAUDE_BACKUP_HOME})

        # If resume failed, rebuild context from thread history
        if session_id and not result:
            context = ""
            if channel:
                try:
                    context = await fetch_thread_history(channel)
                except Exception:
                    pass
            rebuilt_prompt = (
                f"以下是之前的對話紀錄，請根據這些上下文繼續回答：\n\n{context}\n\n---\n用戶最新的訊息：\n{prompt}"
                if context else prompt
            )
            cmd_retry = [
                CLAUDE_BIN,
                "-p", rebuilt_prompt,
                "--model", CLAUDE_MODEL,
                "--system-prompt", sys_prompt,
                "--output-format", "stream-json",
                "--verbose",
                "--max-turns", str(MAX_TURNS),
            ]
            result, new_session_id, tools, turns, usage, _ = await _run_claude_stream(cmd_retry, cwd, status_msg, env_override=_get_account_env())

    # Track session activity timestamp
    if new_session_id and channel:
        channel_session_ts[channel.id] = time.time()

    return result or "(no output)", new_session_id, tools, turns, usage


# ── Git helpers for evaluator scoping ────────────────────────────────────────


async def _git_stash_snapshot(cwd: str) -> str | None:
    """Capture a no-side-effect snapshot of the current working tree.

    Returns the commit sha produced by `git stash create`, or None if the
    directory is not a git repo, the working tree is clean, or the command
    fails. `git stash create` builds a stash commit but does NOT move the
    stash stack or touch the index/working tree — safe to run transparently
    before each generator turn.
    """
    async def _git(args: list[str], timeout: float = 10.0) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode != 0:
                return None
            return out.decode("utf-8", errors="replace").strip()
        except Exception:
            return None

    is_git = await _git(["rev-parse", "--is-inside-work-tree"], timeout=5)
    if is_git != "true":
        return None
    sha = await _git(["stash", "create"])
    return sha or None


async def _git_diff(cwd: str, base: str, max_chars: int = 15000) -> tuple[str, str]:
    """Return (full_diff, summary) for `git diff <base>`.

    The full diff is truncated to max_chars with a trailing marker; summary is
    the `--stat` output. Either may be empty if the diff is empty or git fails.
    """
    async def _git(args: list[str], timeout: float = 20.0) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode != 0:
                return ""
            return out.decode("utf-8", errors="replace")
        except Exception:
            return ""

    full = await _git(["diff", base])
    stat = await _git(["diff", "--stat", base])
    if len(full) > max_chars:
        full = full[:max_chars] + f"\n… (truncated at {max_chars} chars — see --stat above)"
    return full.rstrip(), stat.rstrip()


async def _build_review_diff_sections(cwd: str, before_sha: str | None) -> str:
    """Build diff sections for the evaluator prompt.

    Produces up to two sections: full WIP context (`git diff HEAD`) and the
    current turn's scoped delta (`git diff <before_sha>`). Returns an empty
    string if cwd is not a git repo or there is nothing to show.
    """
    # Cheap repo check; skip entirely if not a git checkout.
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--is-inside-work-tree", cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if out.decode().strip() != "true":
            return ""
    except Exception:
        return ""

    async def _git(args: list[str], timeout: float = 10.0) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return out.decode("utf-8", errors="replace") if proc.returncode == 0 else ""
        except Exception:
            return ""

    wip_diff, wip_stat = await _git_diff(cwd, "HEAD")
    scope_base = before_sha or "HEAD"
    scope_diff, scope_stat = await _git_diff(cwd, scope_base)
    status = (await _git(["status", "--porcelain=v1"])).rstrip()

    parts = []

    wip_body = []
    if wip_diff:
        wip_body.append(f"```\n{wip_stat}\n```\n```diff\n{wip_diff}\n```")
    # Untracked files never show up in `git diff`; list them so the reviewer
    # knows to `Read` newly created files instead of trusting the generator.
    untracked = [line[3:] for line in status.splitlines() if line.startswith("?? ")]
    if untracked:
        wip_body.append(
            "Untracked / new files (not shown in diff — use Read to view):\n"
            + "\n".join(f"- {p}" for p in untracked)
        )
    if wip_body:
        parts.append(
            "## WIP Context（整個 working tree vs HEAD — 包含前幾回合累積；僅作為 context，不是 review 目標）\n"
            + "\n\n".join(wip_body)
        )
    else:
        parts.append("## WIP Context\n（working tree 跟 HEAD 一致，沒有未 commit 改動）")

    if before_sha:
        scope_body = []
        if scope_diff:
            scope_body.append(f"```\n{scope_stat}\n```\n```diff\n{scope_diff}\n```")
        if untracked:
            scope_body.append(
                "Untracked files present (may be new from this turn — cross-check "
                "against generator's description):\n"
                + "\n".join(f"- {p}" for p in untracked)
            )
        if scope_body:
            parts.append(
                "## 本回合淨改動（你的 VERDICT scope — 只針對這段下結論）\n"
                + "\n\n".join(scope_body)
            )
        else:
            parts.append(
                "## 本回合淨改動\n"
                "（generator 這回合沒有實際改動 working tree — 可能只是回答問題、讀檔、"
                "或在 repo 外操作。若回答聲稱改了檔案，要特別警覺。）"
            )
    else:
        parts.append(
            "## 本回合淨改動\n"
            "（無法取得 pre-turn snapshot — 可能非 git repo 或快照失敗。"
            "以 WIP Context 為準，但請注意可能包含前幾回合累積改動。）"
        )

    return "\n\n".join(parts)


# ── Context Relay ─────────────────────────────────────────────────────────────


async def _collect_codebase_state(cwd: str) -> dict:
    """Collect actual codebase state: git diff, recent log, modified files."""
    state = {"git_diff": "", "git_log": "", "modified_files": ""}

    async def _run(cmd: list[str], max_output: int = 8000) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            out = stdout.decode("utf-8", errors="replace").strip()
            return out[:max_output] if out else ""
        except Exception:
            return ""

    # Check if this is a git repo first
    is_git = await _run(["git", "rev-parse", "--is-inside-work-tree"])
    if is_git != "true":
        return state

    # Run all git commands in parallel
    diff_task = asyncio.create_task(_run(["git", "diff", "--stat"], 4000))
    diff_content_task = asyncio.create_task(_run(["git", "diff"], 12000))
    log_task = asyncio.create_task(_run(
        ["git", "log", "--oneline", "-10", "--no-decorate"], 2000
    ))
    status_task = asyncio.create_task(_run(["git", "status", "--short"], 4000))

    state["modified_files"] = await status_task
    state["git_diff_stat"] = await diff_task
    state["git_diff"] = await diff_content_task
    state["git_log"] = await log_task

    return state


async def _generate_conversation_summary(channel) -> str:
    """Generate a brief conversation summary from thread history using Haiku."""
    thread_history = ""
    try:
        thread_history = await fetch_thread_history(channel, limit=30, max_chars=10000)
    except Exception:
        pass
    if not thread_history:
        return ""

    prompt = (
        "以下是 Discord thread 的對話紀錄。用繁體中文寫一份摘要（500字以內），包含：\n"
        "1. 目標：在做什麼、為什麼要做\n"
        "2. 已嘗試的方案：做了哪些事，哪些成功、哪些失敗（含失敗原因）\n"
        "3. 關鍵技術決策：選了什麼方案、為什麼\n"
        "4. 目前狀態：進行到哪、最後的結果或錯誤\n"
        "5. 待辦：還有什麼沒完成\n\n"
        "重點放在「為什麼」而非「做了什麼」，因為程式碼改動會另外提供。只輸出摘要。\n\n"
        f"---\n{thread_history}"
    )
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--model", SUMMARY_MODEL,
        "--output-format", "text",
        "--max-turns", "1",
        "--allowedTools", "",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=WORK_ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        result = stdout.decode("utf-8", errors="replace").strip()
        # Filter out error messages that Claude might parrot back
        if result.startswith("Error:") or "Reached max turns" in result:
            print(f"[RELAY] summary returned error-like output, discarding: {result[:100]}", flush=True)
            return ""
        return result
    except Exception:
        return ""


def _format_relay_context(summary: str, codebase_state: dict) -> str:
    """Format relay context combining conversation summary + codebase state."""
    parts = []

    if summary:
        parts.append(f"## 先前對話摘要\n{summary}")

    if codebase_state.get("modified_files"):
        parts.append(f"## 目前檔案狀態 (git status)\n```\n{codebase_state['modified_files']}\n```")

    if codebase_state.get("git_log"):
        parts.append(f"## 最近 commits\n```\n{codebase_state['git_log']}\n```")

    if codebase_state.get("git_diff"):
        diff = codebase_state["git_diff"]
        # If diff is huge, use stat only
        if len(diff) > 8000 and codebase_state.get("git_diff_stat"):
            parts.append(f"## 未 commit 的改動 (summary)\n```\n{codebase_state['git_diff_stat']}\n```")
        elif diff:
            parts.append(f"## 未 commit 的改動 (git diff)\n```diff\n{diff}\n```")

    return "\n\n".join(parts)


async def maybe_context_relay(channel_id: int, turns: int, channel, cwd: str, force: bool = False) -> bool:
    """Perform context relay. Returns True if relay happened."""
    channel_cumulative_turns[channel_id] = channel_cumulative_turns.get(channel_id, 0) + turns
    total = channel_cumulative_turns[channel_id]

    if not force:
        return False  # Auto relay disabled — use !relay

    session_id = channel_session.get(channel_id)
    if not session_id:
        return False

    print(f"[RELAY] manual relay ({total} turns) for channel {channel_id}, collecting state", flush=True)

    # Collect codebase state + conversation summary in parallel
    state_task = asyncio.create_task(_collect_codebase_state(cwd))
    summary_task = asyncio.create_task(_generate_conversation_summary(channel))
    codebase_state, summary = await asyncio.gather(state_task, summary_task)

    relay_ctx = _format_relay_context(summary, codebase_state)
    if not relay_ctx:
        print(f"[RELAY] no context collected, skipping relay", flush=True)
        return False

    # Store relay context (keep last 3 — newer ones include git state so old ones are less useful)
    if channel_id not in channel_relay_summaries:
        channel_relay_summaries[channel_id] = []
    channel_relay_summaries[channel_id].append(relay_ctx)
    channel_relay_summaries[channel_id] = channel_relay_summaries[channel_id][-3:]

    # Reset session — next message will start fresh with relay context
    channel_session[channel_id] = None
    channel_cumulative_turns[channel_id] = 0
    _save_state()

    print(f"[RELAY] session reset for channel {channel_id}, relay context saved ({len(relay_ctx)} chars)", flush=True)
    try:
        await channel.send(f"🔄 **Context Relay** — 對話已累計 {total} 輪，自動整理上下文並開新 session。")
    except Exception:
        pass

    return True


def build_relay_context(channel_id: int) -> str:
    """Build context from relay data for injecting into new sessions.
    Only uses the latest relay (it already contains current git state)."""
    summaries = channel_relay_summaries.get(channel_id, [])
    if not summaries:
        return ""
    # Only inject the latest — older relays have stale git state
    return summaries[-1]


# ── Evaluator (GAN-inspired review agent) ───────────────────────────────────


async def _fetch_issue_context(text: str, cwd: str) -> str:
    """Extract GitHub issue numbers from text and fetch their content via gh CLI."""
    issue_nums = re.findall(r'#(\d{4,6})', text)
    if not issue_nums:
        return ""
    # Detect repo from git remote in cwd
    try:
        remote = await asyncio.to_thread(
            subprocess.check_output,
            ["git", "remote", "get-url", "origin"],
            cwd=cwd, stderr=subprocess.DEVNULL, text=True,
        )
        # Extract owner/repo from URL like https://...@github.com/owner/repo.git
        m = re.search(r'github\.com[:/]([^/]+/[^/.]+)', remote.strip())
        if not m:
            return ""
        repo = m.group(1)
    except Exception:
        return ""
    parts = []
    for num in dict.fromkeys(issue_nums):  # dedupe, preserve order
        try:
            out = await asyncio.to_thread(
                subprocess.check_output,
                ["gh", "issue", "view", num, "--repo", repo, "--json", "title,body,labels"],
                cwd=cwd, stderr=subprocess.DEVNULL, text=True, timeout=10,
            )
            data = json.loads(out)
            title = data.get("title", "")
            body = (data.get("body") or "")[:2000]
            labels = ", ".join(l.get("name", "") for l in data.get("labels", []))
            parts.append(f"### Issue #{num}: {title}\nLabels: {labels}\n{body}")
        except Exception:
            continue
    return "\n\n".join(parts)



async def _run_evaluator_claude(eval_prompt: str, cwd: str) -> str:
    """Run evaluator using Claude."""
    cmd = [
        CLAUDE_BIN,
        "-p", eval_prompt,
        "--model", EVALUATOR_MODEL,
        "--system-prompt", EVALUATOR_PROMPT,
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", "5",
        "--allowedTools", "Read", "Grep", "Glob", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    ]
    result, session_id, _, _, _, _ = await _run_claude_stream(cmd, cwd, timeout=EVALUATOR_TIMEOUT)

    # If result is missing verdict, resume and ask
    has_verdict = result and _parse_verdict(result) != "PASS"
    if result and "**PASS**" in result:
        has_verdict = True
    if not has_verdict and session_id:
        print(f"[EVALUATOR] no verdict found in result (len={len(result) if result else 0}), asking for review summary via --resume", flush=True)
        try:
            summary_proc = await asyncio.create_subprocess_exec(
                CLAUDE_BIN, "-p",
                "請用繁體中文總結你的 review 結果。"
                "最後一行必須寫上你的結論：**PASS** 或 **FAIL** 或 **PASS_WITH_SUGGESTIONS**。"
                "不要用工具，直接回答。",
                "--resume", session_id,
                "--output-format", "text",
                "--max-turns", "1",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            s_stdout, _ = await asyncio.wait_for(summary_proc.communicate(), timeout=60)
            result = s_stdout.decode("utf-8", errors="replace").strip()
            if result:
                print(f"[EVALUATOR] got review summary with verdict (len={len(result)})", flush=True)
        except Exception as e:
            print(f"[EVALUATOR] review summary failed: {e}", flush=True)

    return result or ""


CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")
CODEX_FALLBACK_MODEL = os.environ.get("CODEX_FALLBACK_MODEL", "gpt-5.5")
CODEX_CODER_MODEL = os.environ.get("CODEX_CODER_MODEL", "gpt-5.5")


async def _run_codex_exec(prompt: str, cwd: str, model: str | None = None,
                          timeout: int | None = None) -> tuple[str, bool]:
    """Run codex exec and return (result, success). success=False means capacity/auth error."""
    cmd = [
        CODEX_BIN, "exec",
        "--json",
        "-s", "read-only",
        "-C", cwd,
    ]
    if model:
        cmd.extend(["-m", model])
    cmd.append(prompt)

    env = os.environ.copy()
    if OPENAI_API_KEY:
        env["OPENAI_API_KEY"] = OPENAI_API_KEY

    effective_timeout = timeout if timeout is not None else EVALUATOR_TIMEOUT
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=1024 * 1024,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
        output = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        # Check for capacity/auth errors
        if "at capacity" in stderr_text or "at capacity" in output:
            print(f"[CODEX] model {model or 'default'} at capacity", flush=True)
            return "", False
        if "401 Unauthorized" in stderr_text or "500 Internal Server Error" in stderr_text:
            print(f"[CODEX] model {model or 'default'} API error", flush=True)
            return "", False

        # Parse JSONL output — collect agent_message texts from item.completed events
        result_parts = []
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message" and item.get("text"):
                    result_parts.append(item["text"])

        result = "\n\n".join(result_parts).strip()
        if not result:
            result = output

        if stderr_text:
            print(f"[CODEX] stderr: {stderr_text[:200]}", flush=True)

        return result or "", True
    except asyncio.TimeoutError:
        print(f"[CODEX] timeout after {effective_timeout}s (model={model})", flush=True)
        return "", False
    except Exception as e:
        print(f"[CODEX] error: {e} (model={model})", flush=True)
        return "", False


async def _run_evaluator_codex(eval_prompt: str, cwd: str) -> str:
    """Run evaluator using Codex, with model fallback."""
    # codex exec has no --system-prompt flag, so prepend EVALUATOR_PROMPT
    # to the user message to keep behavior aligned with the Claude backend.
    full_prompt = f"{EVALUATOR_PROMPT}\n\n---\n\n{eval_prompt}"

    result, ok = await _run_codex_exec(full_prompt, cwd, model=CODEX_MODEL)
    if ok and result:
        print(f"[EVALUATOR] codex {CODEX_MODEL} result (len={len(result)})", flush=True)
        return result

    # Fallback to mini
    if CODEX_FALLBACK_MODEL and CODEX_FALLBACK_MODEL != CODEX_MODEL:
        print(f"[EVALUATOR] falling back to {CODEX_FALLBACK_MODEL}", flush=True)
        result, ok = await _run_codex_exec(full_prompt, cwd, model=CODEX_FALLBACK_MODEL)
        if ok and result:
            print(f"[EVALUATOR] codex {CODEX_FALLBACK_MODEL} result (len={len(result)})", flush=True)
            return result

    print(f"[EVALUATOR] codex failed, no result", flush=True)
    return ""


CODING_MANAGER_SYSTEM_PROMPT = """\
You are a coding task manager. Analyze the user's coding request and output a routing decision as JSON.

Classify as "simple" if the task is:
- A small bug fix (1-2 files, obvious change)
- Adding a small function/method
- A config tweak or rename
- Anything a skilled developer would finish in under 5 minutes

Classify as "complex" if the task involves:
- Implementing a new feature across multiple files
- Architectural or design changes
- Debugging an unknown issue requiring investigation
- Extensive codebase exploration needed

For COMPLEX tasks, before outputting:
1. Run `git branch --show-current` to check the current branch.
2. Decide the target branch:
   - If the user specified a branch, use it.
   - If the user said "from latest main / new branch", propose a meaningful name like `feature/<short-desc>` or `fix/<short-desc>` based off the latest main.
   - Otherwise reuse the current branch.
3. Produce a detailed implementation plan covering: target branch, base branch (if new), files to create/modify, step-by-step changes, and acceptance criteria.

For SIMPLE tasks, just include a brief task description in `spec`. Branch can be the current branch.

Output ONLY valid JSON (no markdown fences, no preamble):
{"complexity": "simple"|"complex", "branch": "<target branch name>", "base_branch": "<base branch if creating new, else empty string>", "spec": "<for simple: brief task; for complex: full markdown plan with sections for branch, files, steps, acceptance>"}"""


async def _run_codex_coder(prompt: str, cwd: str, timeout: int | None = None) -> tuple[str, bool]:
    """Run codex exec with full-auto safety for coding tasks. Returns (result, success)."""
    cmd = [
        CODEX_BIN, "exec",
        "--json",
        "-s", "workspace-write",
        "-C", cwd,
        "-m", CODEX_CODER_MODEL,
        prompt,
    ]

    env = os.environ.copy()
    if OPENAI_API_KEY:
        env["OPENAI_API_KEY"] = OPENAI_API_KEY

    effective_timeout = timeout or CLAUDE_TIMEOUT
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=1024 * 1024,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
        output = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if "at capacity" in stderr_text or "at capacity" in output:
            print(f"[CODEX_CODER] model at capacity", flush=True)
            return "", False
        if "401 Unauthorized" in stderr_text or "500 Internal Server Error" in stderr_text:
            print(f"[CODEX_CODER] API error", flush=True)
            return "", False
        if proc.returncode != 0:
            print(f"[CODEX_CODER] exit {proc.returncode}: {stderr_text[:300]}", flush=True)
            return "", False

        result_parts = []
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message" and item.get("text"):
                    result_parts.append(item["text"])

        result = "\n\n".join(result_parts).strip() or output
        if stderr_text:
            print(f"[CODEX_CODER] stderr: {stderr_text[:200]}", flush=True)
        return result, True
    except asyncio.TimeoutError:
        print(f"[CODEX_CODER] timeout after {effective_timeout}s", flush=True)
        return "", False
    except Exception as e:
        print(f"[CODEX_CODER] error: {e}", flush=True)
        return "", False


async def run_evaluator(user_prompt: str, generator_result: str, cwd: str,
                        channel=None) -> str:
    """Run an evaluator agent to review the generator's output."""
    async def _noop():
        return ""

    before_sha = channel_turn_snapshot.get(channel.id) if channel else None

    tasks = [
        _fetch_issue_context(user_prompt + "\n" + generator_result, cwd),
        fetch_thread_history(channel, limit=15, max_chars=4000) if channel else _noop(),
        _build_review_diff_sections(cwd, before_sha),
    ]
    issue_ctx, thread_history, diff_sections = await asyncio.gather(*tasks)

    sections = [f"## 用戶原始問題\n{user_prompt}"]
    if thread_history:
        sections.append(f"## 對話歷史（Thread Context）\n{thread_history}")
    if issue_ctx:
        sections.append(f"## 相關 GitHub Issue\n{issue_ctx}")
    sections.append(f"## Generator 的回答\n{generator_result}")
    if diff_sections:
        sections.append(diff_sections)
    sections.append(f"## 工作目錄\n`{cwd}`\n\n請 review 以上內容。")

    eval_prompt = "\n\n".join(sections)

    backend = EVALUATOR_BACKEND

    print(f"[EVALUATOR] using backend: {backend}", flush=True)
    if backend == "codex":
        return await _run_evaluator_codex(eval_prompt, cwd)
    else:
        return await _run_evaluator_claude(eval_prompt, cwd)


def _has_write_tools(tools_used: list[str]) -> bool:
    """Check if any write-type tools were used."""
    for desc in tools_used:
        for tool in WRITE_TOOLS:
            if tool in desc:
                return True
    return False


TODO_DETECT_MIN_RESPONSE_LEN = 150
TODO_MAX_ITEMS = 8


async def _detect_actionable_todos(response: str) -> list[str]:
    """Use Haiku to decide whether `response` contains an actionable TODO list.

    Returns extracted TODO strings, or [] if none. Conservative — only counts
    items framed as concrete code fixes / modifications the user could hand off
    to another coding agent.
    """
    if not response or len(response) < TODO_DETECT_MIN_RESPONSE_LEN:
        return []

    prompt = (
        "以下是 coding assistant 的回答。判斷它是否列出 actionable 的 code TODO 項目 "
        "— 也就是使用者可以交給另一個 coding agent 逐條去執行的具體改動。\n\n"
        "算 TODO：具體的 bug fix、具體的 refactor、指名了檔案/函式/行為的改動項、"
        "檢查 code 後列出的 issue / 問題清單。\n"
        "不算 TODO：一般性建議、「可以考慮…」級別的意見、純解釋/回答、探索性討論、"
        "已經做完事情的總結、已經修好的項目。\n\n"
        "每一條 TODO 要自成一句完整敘述，帶上必要的上下文（檔案名、函式名、情境），"
        "因為接手的 agent 只會看到 TODO 文字本身，不會看到這份回答。\n\n"
        "嚴格輸出 JSON，不要任何額外文字、不要 markdown fence：\n"
        '{"todos": ["<具體描述 1>", "<具體描述 2>", ...]}\n\n'
        "沒有 actionable TODO 時輸出 {\"todos\": []}。\n\n"
        "Assistant 回答：\n---\n"
        f"{response}\n"
        "---"
    )
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--model", SUMMARY_MODEL,
        "--output-format", "text",
        "--max-turns", "1",
        "--allowedTools", "",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=WORK_ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
        text = stdout.decode("utf-8", errors="replace").strip()
    except Exception as e:
        print(f"[TODO_DETECT] subprocess error: {e}", flush=True)
        return []

    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        return []
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return []

    raw = data.get("todos", [])
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        s = str(item).strip()
        if not s:
            continue
        if len(s) > 500:
            s = s[:500] + "…"
        out.append(s)
    return out[:TODO_MAX_ITEMS]


# ── Persistent View Data Store ─────────────────────────────────────────────
# Keyed by message_id, stores data needed for button callbacks after restart.
# In-memory only — after restart, buttons gracefully report "bot restarted".
_review_data: dict[int, dict] = {}
_suggestion_data: dict[int, dict] = {}
_todo_dispatch_data: dict[int, dict] = {}

_EXPIRED_MSG = "⚠️ Bot 已重啟，此按鈕已失效。請重新發問或操作。"


class SuggestionView(discord.ui.View):
    """Buttons to accept or skip optional improvement suggestions from evaluator."""

    def __init__(self, review: str = "", user_prompt: str = "", cwd: str = "",
                 channel=None, session_id: str | None = None,
                 generator_pikmin: dict | None = None, eval_pikmin: dict | None = None):
        super().__init__(timeout=None)
        self.review = review
        self.user_prompt = user_prompt
        self.cwd = cwd
        self.channel = channel
        self.session_id = session_id
        self.generator_pikmin = generator_pikmin
        self.eval_pikmin = eval_pikmin

    def store(self, message_id: int):
        """Store data so it can be recovered by custom_id lookup."""
        _suggestion_data[message_id] = {
            "review": self.review, "user_prompt": self.user_prompt, "cwd": self.cwd,
            "channel_id": self.channel.id if self.channel else None,
            "session_id": self.session_id,
            "generator_pikmin": self.generator_pikmin, "eval_pikmin": self.eval_pikmin,
        }

    def _load(self, interaction: discord.Interaction) -> bool:
        data = _suggestion_data.get(interaction.message.id)
        if not data:
            return False
        self.review = data["review"]
        self.user_prompt = data["user_prompt"]
        self.cwd = data["cwd"]
        self.channel = interaction.channel
        self.session_id = data["session_id"]
        self.generator_pikmin = data["generator_pikmin"]
        self.eval_pikmin = data["eval_pikmin"]
        return True

    @discord.ui.button(label="✅ 採納建議", style=discord.ButtonStyle.success, custom_id="suggestion:accept")
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._load(interaction):
            await interaction.response.edit_message(content=_EXPIRED_MSG, view=None)
            return
        print(f"[EVALUATOR] Accept suggestions pressed by {interaction.user}", flush=True)
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.NotFound:
            return
        _suggestion_data.pop(interaction.message.id, None)

        pikmin = self.generator_pikmin or PIKMIN_POOL[0]
        fix_prompt = (
            f"Evaluator 的改進建議如下，請根據建議優化：\n\n{self.review}\n\n"
            f"修改完成後說明你改了什麼。"
        )
        thinking = await pikmin_send(self.channel, "🔧 根據建議優化中...", pikmin)
        try:
            fix_result, fix_sid, _, _, _ = await run_claude(
                fix_prompt, self.cwd, session_id=self.session_id,
                status_msg=thinking, channel=self.channel,
            )
            if fix_sid:
                channel_session[self.channel.id] = fix_sid
                _save_state()
            chunks = split_message(fix_result)
            try:
                await pikmin_edit(thinking, chunks[0], self.channel)
            except Exception:
                await pikmin_send(self.channel, chunks[0], pikmin)
            for chunk in chunks[1:]:
                await pikmin_send(self.channel, chunk, pikmin)
        except Exception as e:
            print(f"[EVALUATOR] Accept suggestion error: {e}", flush=True)
            try:
                await pikmin_edit(thinking, f"❌ 優化失敗: {e}", self.channel)
            except Exception:
                pass

    @discord.ui.button(label="⏭️ 跳過", style=discord.ButtonStyle.secondary, custom_id="suggestion:skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(f"[EVALUATOR] Skip suggestions pressed by {interaction.user}", flush=True)
        _suggestion_data.pop(interaction.message.id, None)
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.NotFound:
            pass


class ReviewView(discord.ui.View):
    """Button to request an evaluator review on a pure-answer response."""

    def __init__(self, user_prompt: str = "", generator_result: str = "", cwd: str = "",
                 channel=None, generator_pikmin: dict | None = None,
                 session_id: str | None = None):
        super().__init__(timeout=None)
        self.user_prompt = user_prompt
        self.generator_result = generator_result
        self.cwd = cwd
        self.channel = channel
        self.generator_pikmin = generator_pikmin
        self.session_id = session_id

    def store(self, message_id: int):
        """Store data so it can be recovered by custom_id lookup."""
        _review_data[message_id] = {
            "user_prompt": self.user_prompt, "generator_result": self.generator_result,
            "cwd": self.cwd, "channel_id": self.channel.id if self.channel else None,
            "generator_pikmin": self.generator_pikmin, "session_id": self.session_id,
        }

    def _load(self, interaction: discord.Interaction) -> bool:
        data = _review_data.get(interaction.message.id)
        if not data:
            return False
        self.user_prompt = data["user_prompt"]
        self.generator_result = data["generator_result"]
        self.cwd = data["cwd"]
        self.channel = interaction.channel
        self.generator_pikmin = data["generator_pikmin"]
        self.session_id = data["session_id"]
        return True

    @discord.ui.button(label="🔍 召喚 Review", style=discord.ButtonStyle.secondary, custom_id="review:summon")
    async def review_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._load(interaction):
            await interaction.response.edit_message(content=_EXPIRED_MSG, view=None)
            return
        print(f"[EVALUATOR] Review button pressed by {interaction.user}", flush=True)
        button.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.NotFound:
            print(f"[EVALUATOR] interaction expired", flush=True)
            return
        _review_data.pop(interaction.message.id, None)

        eval_pikmin = _pick_eval_pikmin(self.generator_pikmin) if self.generator_pikmin else PIKMIN_POOL[1]
        pikmin = self.generator_pikmin or PIKMIN_POOL[0]
        thinking = await pikmin_send(
            self.channel, "🔍 Review 中...", eval_pikmin
        )

        try:
            review = await run_evaluator(
                self.user_prompt, self.generator_result, self.cwd,
                channel=self.channel,
            )
            if not review:
                await pikmin_edit(thinking, "✅ 沒有發現問題。", self.channel)
                return

            verdict = _parse_verdict(review)
            print(f"[EVALUATOR] Manual review verdict: {verdict}", flush=True)

            # Display the review
            chunks = split_message(review)
            try:
                await pikmin_edit(thinking, chunks[0], self.channel)
            except Exception:
                await pikmin_send(self.channel, chunks[0], eval_pikmin)
            for chunk in chunks[1:]:
                await pikmin_send(self.channel, chunk, eval_pikmin)

            if verdict == "PASS":
                return  # All good, done

            if verdict == "PASS_WITH_SUGGESTIONS":
                # Show accept/skip buttons
                suggestion_view = SuggestionView(
                    review=review, user_prompt=self.user_prompt, cwd=self.cwd,
                    channel=self.channel, session_id=self.session_id,
                    generator_pikmin=self.generator_pikmin, eval_pikmin=eval_pikmin,
                )
                msg = await self.channel.send("💡 Evaluator 有改進建議，要採納嗎？", view=suggestion_view)
                suggestion_view.store(msg.id)
                return

            # FAIL — resume generator with feedback loop
            fix_session = self.session_id
            result = self.generator_result
            for fix_round in range(2, MAX_GEN_EVAL_ROUNDS + 1):
                fix_prompt = (
                    f"Evaluator 的回饋如下，請根據回饋修正：\n\n{review}\n\n"
                    f"修正完成後說明你改了什麼。"
                )
                fix_thinking = await pikmin_send(
                    self.channel, f"🔧 根據回饋修正中... (第 {fix_round}/{MAX_GEN_EVAL_ROUNDS} 輪)", pikmin
                )
                fix_result, fix_sid, _, _, _ = await run_claude(
                    fix_prompt, self.cwd, session_id=fix_session,
                    status_msg=fix_thinking, channel=self.channel,
                )
                if fix_sid:
                    fix_session = fix_sid
                result = fix_result

                fix_chunks = split_message(fix_result)
                try:
                    await pikmin_edit(fix_thinking, fix_chunks[0], self.channel)
                except Exception:
                    await pikmin_send(self.channel, fix_chunks[0], pikmin)
                for chunk in fix_chunks[1:]:
                    await pikmin_send(self.channel, chunk, pikmin)

                # Re-review
                re_thinking = await pikmin_send(
                    self.channel, f"🔍 Re-review 中... (第 {fix_round}/{MAX_GEN_EVAL_ROUNDS} 輪)", eval_pikmin
                )
                review = await run_evaluator(self.user_prompt, fix_result, self.cwd, channel=self.channel)
                if review:
                    re_chunks = split_message(review)
                    try:
                        await pikmin_edit(re_thinking, re_chunks[0], self.channel)
                    except Exception:
                        await pikmin_send(self.channel, re_chunks[0], eval_pikmin)
                    for chunk in re_chunks[1:]:
                        await pikmin_send(self.channel, chunk, eval_pikmin)
                else:
                    await pikmin_edit(re_thinking, "✅ 修正後沒有發現問題。", self.channel)

                verdict = _parse_verdict(review)
                if verdict == "PASS":
                    break
                if verdict == "PASS_WITH_SUGGESTIONS":
                    suggestion_view = SuggestionView(
                        review=review, user_prompt=self.user_prompt, cwd=self.cwd,
                        channel=self.channel, session_id=fix_session,
                        generator_pikmin=self.generator_pikmin, eval_pikmin=eval_pikmin,
                    )
                    msg = await self.channel.send("💡 Evaluator 有改進建議，要採納嗎？", view=suggestion_view)
                    suggestion_view.store(msg.id)
                    break
            else:
                await self.channel.send(f"⚠️ 經 {MAX_GEN_EVAL_ROUNDS} 輪修正仍有問題，請人工檢查。")
        except Exception as e:
            print(f"[EVALUATOR] ERROR: {e}", flush=True)
            try:
                await pikmin_edit(thinking, f"❌ Review 失敗: {e}", self.channel)
            except Exception:
                await pikmin_send(self.channel, f"❌ Review 失敗: {e}", eval_pikmin)


# ── Planner (Sprint Contract) ───────────────────────────────────────────────

# Active plans awaiting confirmation: channel_id -> plan data
pending_plans: dict[int, dict] = {}


async def generate_plan(task: str, cwd: str, channel=None) -> dict | None:
    """Use Planner agent to produce a sprint contract."""
    sections = [f"任務：{task}"]
    if channel:
        try:
            thread_history = await fetch_thread_history(channel, limit=20, max_chars=6000)
            if thread_history:
                sections.insert(0, f"## 對話上下文\n{thread_history}")
        except Exception:
            pass
    sections.append("請分析 codebase 並根據以上資訊產出 sprint contract。")
    prompt = "\n\n".join(sections)

    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--model", PLANNER_MODEL,
        "--system-prompt", PLANNER_PROMPT,
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", str(MAX_TURNS),
    ]
    result, _, _, _, _, _ = await _run_claude_stream(cmd, cwd)
    if not result:
        return None
    return _extract_plan_json(result)


def _extract_plan_json(text: str) -> dict | None:
    """Extract and parse a sprint contract JSON from planner output."""
    cleaned = re.sub(r'```(?:json)?\s*', '', text).strip().rstrip('`')
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as e:
            print(f"[PLANNER] JSON parse failed: {e}", flush=True)
    return None


def format_plan_display(plan: dict) -> str:
    """Format a sprint contract for Discord display."""
    lines = [f"## 📋 Sprint Contract\n**{plan.get('summary', '')}**\n"]
    steps = plan.get("steps", [])
    # Group into parallel batches
    for step in steps:
        deps = step.get("depends_on", [])
        dep_str = f" (依賴步驟 {', '.join(str(d) for d in deps)})" if deps else " ⚡ 可並行"
        lines.append(
            f"**{step['id']}.** {step['title']}{dep_str}\n"
            f"  {step['description']}\n"
            f"  ✅ 驗收：{step['acceptance']}\n"
        )
    return "\n".join(lines)


MAX_GEN_EVAL_ROUNDS = 3  # max back-and-forth rounds between generator and evaluator


def _pick_eval_pikmin(gen_pikmin: dict) -> dict:
    """Pick a different pikmin for the evaluator."""
    gen_idx = None
    for i, p in enumerate(PIKMIN_POOL):
        if p["name"] == gen_pikmin["name"]:
            gen_idx = i
            break
    eval_idx = 1 if gen_idx != 1 else 0
    return PIKMIN_POOL[eval_idx]


async def generator_evaluator_loop(
    initial_prompt: str,
    cwd: str,
    channel,
    gen_pikmin: dict,
    eval_context: str = "",
    max_rounds: int = MAX_GEN_EVAL_ROUNDS,
    show_status_prefix: str = "",
) -> tuple[str, bool]:
    """
    Run a Generator-Evaluator dialogue loop.
    Generator produces output, Evaluator reviews. If FAIL, Evaluator's feedback
    is sent back to Generator (resuming its session) for correction.
    Returns (final_result, passed).
    """
    eval_pikmin = _pick_eval_pikmin(gen_pikmin)
    session_id = None
    result = ""
    prefix = f"{show_status_prefix} " if show_status_prefix else ""

    for round_num in range(1, max_rounds + 1):
        round_label = f"(第 {round_num}/{max_rounds} 輪)" if max_rounds > 1 else ""

        # ── Generator turn ──
        if round_num == 1:
            gen_prompt = initial_prompt
            gen_thinking = await pikmin_send(channel, f"{prefix}⚙️ 執行中... {round_label}", gen_pikmin)
        else:
            # Resume with evaluator feedback
            gen_prompt = (
                f"Evaluator 的回饋如下，請根據回饋修正：\n\n{review}\n\n"
                f"修正完成後說明你改了什麼。"
            )
            gen_thinking = await pikmin_send(channel, f"{prefix}🔧 根據回饋修正中... {round_label}", gen_pikmin)

        result, new_session_id, _, _, _ = await run_claude(
            gen_prompt, cwd, session_id=session_id, status_msg=gen_thinking, channel=channel,
        )
        if new_session_id:
            session_id = new_session_id

        # Show generator result
        chunks = split_message(result)
        try:
            await pikmin_edit(gen_thinking, chunks[0], channel)
        except Exception:
            await pikmin_send(channel, chunks[0], gen_pikmin)
        for chunk in chunks[1:]:
            await pikmin_send(channel, chunk, gen_pikmin)

        # ── Evaluator turn ──
        eval_thinking = await pikmin_send(channel, f"{prefix}🔍 Review 中... {round_label}", eval_pikmin)

        eval_prompt_text = ""
        if eval_context:
            eval_prompt_text += f"{eval_context}\n\n"
        eval_prompt_text += f"## Generator 的回答\n{result}\n\n"
        if round_num > 1:
            eval_prompt_text += f"（這是 Generator 第 {round_num} 次修改）\n\n"
        eval_prompt_text += "請 review。如果有問題，具體指出要修正什麼。"

        review = await run_evaluator(eval_prompt_text, result, cwd, channel=channel)

        if review:
            review_chunks = split_message(review)
            try:
                await pikmin_edit(eval_thinking, review_chunks[0], channel)
            except Exception:
                await pikmin_send(channel, review_chunks[0], eval_pikmin)
            for chunk in review_chunks[1:]:
                await pikmin_send(channel, chunk, eval_pikmin)
        else:
            await pikmin_edit(eval_thinking, "✅ 沒有發現問題。", channel)

        verdict = _parse_verdict(review)
        if verdict == "PASS":
            return result, True
        if verdict == "PASS_WITH_SUGGESTIONS":
            # For planner steps, treat suggestions as pass (planner has its own acceptance criteria)
            return result, True

        # Last round and still failing
        if round_num == max_rounds:
            await channel.send(f"⚠️ 經 {max_rounds} 輪修正仍未完全通過 review。")
            return result, False

    return result, False


async def execute_plan_step(step: dict, cwd: str, channel, pikmin: dict,
                            plan_summary: str) -> tuple[str, bool]:
    """Execute a single plan step with Generator-Evaluator dialogue."""
    initial_prompt = (
        f"## Sprint Contract 步驟 {step['id']}: {step['title']}\n\n"
        f"**整體任務**：{plan_summary}\n\n"
        f"**本步驟要求**：{step['description']}\n\n"
        f"**驗收標準**：{step['acceptance']}\n\n"
        f"請完成這個步驟。完成後說明你做了什麼。"
    )

    eval_context = (
        f"## Sprint Contract 步驟 {step['id']}: {step['title']}\n"
        f"**驗收標準**：{step['acceptance']}"
    )

    return await generator_evaluator_loop(
        initial_prompt=initial_prompt,
        cwd=cwd,
        channel=channel,
        gen_pikmin=pikmin,
        eval_context=eval_context,
        show_status_prefix=f"步驟 {step['id']}",
    )


async def execute_plan(plan: dict, cwd: str, channel, base_pikmin_idx: int):
    """Execute a full sprint contract with dependency resolution."""
    steps = plan.get("steps", [])
    summary = plan.get("summary", "")
    completed: set[int] = set()
    results: dict[int, str] = {}

    await channel.send(f"🚀 **開始執行 Sprint Contract** — 共 {len(steps)} 個步驟")

    while len(completed) < len(steps):
        # Find steps that are ready (all dependencies met)
        ready = [
            s for s in steps
            if s["id"] not in completed
            and all(d in completed for d in s.get("depends_on", []))
        ]
        if not ready:
            await channel.send("❌ 無法繼續 — 有步驟的依賴無法滿足。")
            break

        if len(ready) == 1:
            step = ready[0]
            pidx = (base_pikmin_idx + step["id"]) % len(PIKMIN_POOL)
            pikmin = PIKMIN_POOL[pidx]
            result, passed = await execute_plan_step(step, cwd, channel, pikmin, summary)
            completed.add(step["id"])
            results[step["id"]] = result
            if not passed:
                await channel.send(f"⚠️ 步驟 {step['id']} 未完全通過驗收，繼續下一步。")
        else:
            # Parallel execution
            await channel.send(f"⚡ 並行執行 {len(ready)} 個步驟: {', '.join(s['title'] for s in ready)}")

            async def _exec_step(step):
                pidx = (base_pikmin_idx + step["id"]) % len(PIKMIN_POOL)
                pikmin = PIKMIN_POOL[pidx]
                return step["id"], *(await execute_plan_step(step, cwd, channel, pikmin, summary))

            tasks = [asyncio.create_task(_exec_step(s)) for s in ready]
            for coro in asyncio.as_completed(tasks):
                step_id, result, passed = await coro
                completed.add(step_id)
                results[step_id] = result
                if not passed:
                    await channel.send(f"⚠️ 步驟 {step_id} 未完全通過驗收。")

    await channel.send(f"🏁 **Sprint Contract 完成！** {len(completed)}/{len(steps)} 步驟已完成。")


async def _detect_base_branch(cwd: str, channel) -> str | None:
    """Infer which branch the conversation is about so subagent worktrees fork
    from the right place instead of blindly using HEAD (which is often main).

    Detection order (first non-main hit wins):
    1. channel_worktrees — thread already has a worktree with an explicit branch
    2. cwd's current git branch — if not main/master, it's likely intentional
    3. Recent channel messages — regex scan for branch-like refs
    """
    ch_id = channel.id if hasattr(channel, "id") else channel

    # 1. Thread has an explicit worktree
    wt_info = channel_worktrees.get(ch_id)
    if wt_info:
        return wt_info.get("branch")

    # Step 2 (cwd current branch) intentionally removed:
    # The repo's HEAD may reflect a previous unrelated operation, causing all
    # new threads to be assigned to the wrong worktree. Branch must be
    # explicitly mentioned in messages (step 3) to be trustworthy.

    # 3. Scan recent messages for branch-like patterns
    _branch_re = re.compile(
        r'(?:branch|Branch|🌿)\s*[:`：]*\s*`?'           # label prefix
        r'((?:feature|fix|bugfix|hotfix|release|chore|refactor|perf|ci|test|wip)'
        r'/[\w./-]+)',                                     # conventional branch name
        re.IGNORECASE,
    )
    _worktree_path_re = re.compile(
        r'/root/work/(erigon[\w-]*)',                      # worktree dir name
    )
    try:
        async for msg in channel.history(limit=15, oldest_first=False):
            if not msg.content:
                continue
            m = _branch_re.search(msg.content)
            if m:
                return m.group(1)
            # Infer from worktree path — look up the branch of that worktree
            pm = _worktree_path_re.search(msg.content)
            if pm:
                wt_dir = os.path.join(WORK_ROOT, pm.group(1))
                if os.path.isdir(wt_dir):
                    try:
                        p = await asyncio.create_subprocess_exec(
                            "git", "rev-parse", "--abbrev-ref", "HEAD",
                            cwd=wt_dir, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        o, _ = await asyncio.wait_for(p.communicate(), timeout=5)
                        if p.returncode == 0:
                            b = o.decode("utf-8", errors="replace").strip()
                            if b and b not in ("main", "master", "HEAD"):
                                return b
                    except Exception:
                        pass
    except Exception:
        pass

    return None


async def _switch_worktree_branch(wt_path: str, main_repo: str, branch: str) -> tuple[bool, bool]:
    """Switch an existing worktree to a different branch.

    Returns (success, stashed). When stashed=True, caller should notify the
    user that uncommitted changes were saved to git stash.
    """
    # Fetch latest from origin
    fetch = await asyncio.create_subprocess_exec(
        "git", "fetch", "origin", branch,
        cwd=wt_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(fetch.communicate(), timeout=30)
    except asyncio.TimeoutError:
        try:
            fetch.kill()
            await fetch.wait()
        except Exception:
            pass

    # Stash any dirty state so it doesn't leak into the new branch
    status_proc = await asyncio.create_subprocess_exec(
        "git", "status", "--porcelain",
        cwd=wt_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    status_out, _ = await status_proc.communicate()
    stashed = False
    if status_out and status_out.strip():
        stash_proc = await asyncio.create_subprocess_exec(
            "git", "stash", "push", "-u", "-m", f"auto-stash before switch to {branch}",
            cwd=wt_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stash_out, _ = await stash_proc.communicate()
        # -u includes untracked files; rc=0 means something was actually stashed
        stashed = stash_proc.returncode == 0 and b"No local changes" not in stash_out
        if stashed:
            print(f"[THREAD_WT] stashed dirty state before switching to {branch}", flush=True)

    # Try normal checkout first
    proc = await asyncio.create_subprocess_exec(
        "git", "checkout", branch,
        cwd=wt_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode == 0:
        return True, stashed

    # Branch might be checked out elsewhere — try detached HEAD
    for ref in [f"origin/{branch}", branch]:
        proc = await asyncio.create_subprocess_exec(
            "git", "checkout", "--detach", ref,
            cwd=wt_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode == 0:
            return True, stashed

    # Switch failed — restore stashed changes
    if stashed:
        restore = await asyncio.create_subprocess_exec(
            "git", "stash", "pop",
            cwd=wt_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await restore.communicate()

    print(f"[THREAD_WT] switch to {branch} failed in {wt_path}", flush=True)
    return False, False


async def _ensure_thread_worktree(channel, cwd: str) -> tuple[str, str | None, bool]:
    """Auto-create an isolated worktree for a Discord thread.

    Only acts on threads in git repos where a branch is detectable from
    conversation context. Returns (effective_cwd, branch_name, newly_created).
    """
    if not isinstance(channel, discord.Thread):
        return cwd, None, False

    # Already has a worktree — validate it still exists
    wt_info = channel_worktrees.get(channel.id)
    if wt_info:
        if os.path.isdir(wt_info["worktree"]):
            return wt_info["worktree"], wt_info.get("branch"), False
        # Stale entry — clean up
        del channel_worktrees[channel.id]
        _save_state()

    # Detect which branch this conversation is about
    branch = await _detect_base_branch(cwd, channel)
    if not branch:
        return cwd, None, False

    main_repo = await find_main_repo_path(cwd)
    if not main_repo:
        return cwd, branch, False  # branch detected but no git repo — retryable

    # Fetch so recently-pushed branches are available locally
    fetch = await asyncio.create_subprocess_exec(
        "git", "fetch", "origin", branch,
        cwd=main_repo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(fetch.communicate(), timeout=30)
    except asyncio.TimeoutError:
        try:
            fetch.kill()
            await fetch.wait()
        except Exception:
            pass

    # Try standard worktree approach (reuse existing worktree for this branch)
    # But only if no other channel already claims it — otherwise we'd share state.
    wt_path = await ensure_worktree_for_branch(main_repo, branch)
    if wt_path and os.path.realpath(wt_path) != os.path.realpath(main_repo):
        # Check no other channel already owns this worktree
        wt_real = os.path.realpath(wt_path)
        claimed = any(
            os.path.realpath(v.get("worktree", "")) == wt_real
            for ch_id, v in channel_worktrees.items()
            if ch_id != channel.id
        )
        if not claimed:
            channel_workdir[channel.id] = wt_path
            channel_worktrees[channel.id] = {
                "repo": main_repo, "worktree": wt_path, "branch": branch,
            }
            _save_state()
            return wt_path, branch, True

    # Branch is checked out in main repo or worktree is claimed — create a
    # per-thread detached copy for isolation.
    repo_name = os.path.basename(main_repo)
    thread_suffix = str(channel.id)[-6:]
    dir_slug = branch.replace("/", "-")
    wt_path = os.path.join(WORK_ROOT, f"{repo_name}-wt-t{thread_suffix}-{dir_slug}")

    if os.path.exists(wt_path):
        # Leftover from previous session — validate it is a functional git worktree
        check = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--git-dir",
            cwd=wt_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        await check.communicate()
        if check.returncode == 0:
            # Verify not claimed by another channel
            wt_real = os.path.realpath(wt_path)
            claimed = any(
                os.path.realpath(v.get("worktree", "")) == wt_real
                for ch_id, v in channel_worktrees.items()
                if ch_id != channel.id
            )
            if not claimed:
                # Update to the correct branch HEAD (leftover may be on stale commit)
                updated = False
                for ref in [f"origin/{branch}", branch]:
                    up = await asyncio.create_subprocess_exec(
                        "git", "checkout", "--detach", ref,
                        cwd=wt_path, stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await up.communicate()
                    if up.returncode == 0:
                        updated = True
                        break
                if updated:
                    channel_workdir[channel.id] = wt_path
                    channel_worktrees[channel.id] = {
                        "repo": main_repo, "worktree": wt_path, "branch": branch,
                    }
                    _save_state()
                    return wt_path, branch, True
                # Checkout failed — fall through to prune+recreate
        # Invalid or claimed leftover — prune stale worktree metadata, then remove
        prune = await asyncio.create_subprocess_exec(
            "git", "worktree", "prune",
            cwd=main_repo, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await prune.communicate()
        shutil.rmtree(wt_path, ignore_errors=True)

    # Resolve ref: local branch commit → detached HEAD (avoids branch-in-use conflict)
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", "--detach", wt_path, branch,
        cwd=main_repo,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        # Try origin/<branch>
        proc2 = await asyncio.create_subprocess_exec(
            "git", "worktree", "add", "--detach", wt_path, f"origin/{branch}",
            cwd=main_repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr2 = await proc2.communicate()
        if proc2.returncode != 0:
            print(f"[THREAD_WT] worktree add failed: {stderr2.decode()[:200]}", flush=True)
            return cwd, branch, False  # branch detected but creation failed — retryable

    channel_workdir[channel.id] = wt_path
    channel_worktrees[channel.id] = {
        "repo": main_repo, "worktree": wt_path, "branch": branch,
    }
    _save_state()
    return wt_path, branch, True


async def _create_subagent_worktree(cwd: str,
                                    base_ref: str | None = None,
                                    ) -> tuple[str, str, str, str | None] | None:
    """Create an isolated git worktree + branch for TODO dispatch subagents.

    The new worktree is placed at `<repo-parent>/<repo-name>-subagents/<ts>`
    and checked out to a fresh branch `subagent/<YYYY-MM-DD_HHMMSS>` based on
    *base_ref* (falls back to HEAD when None). The original working tree is
    untouched — any uncommitted WIP stays where it is.

    Returns (worktree_path, branch_name, wip_notice, actual_base_ref) on
    success, or None if `cwd` is not a git checkout or worktree creation fails.
    `wip_notice` is an empty string when the main checkout is clean.
    `actual_base_ref` is the ref the worktree was forked from (for display).
    """
    async def _git(args: list[str], work_dir: str | None = None,
                   timeout: float = 15.0) -> tuple[str, int]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args, cwd=work_dir or cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode != 0:
                print(f"[WORKTREE] git {args[0]} failed: {err.decode(errors='replace').strip()}",
                      flush=True)
            return out.decode("utf-8", errors="replace").strip(), proc.returncode
        except Exception as e:
            print(f"[WORKTREE] git {args[0]} exception: {e}", flush=True)
            return "", 1

    is_git, rc = await _git(["rev-parse", "--is-inside-work-tree"])
    if rc != 0 or is_git != "true":
        return None

    repo_root, rc = await _git(["rev-parse", "--show-toplevel"])
    if rc != 0 or not repo_root:
        return None

    repo_name = os.path.basename(repo_root)
    ts = time.strftime("%Y-%m-%d_%H%M%S")
    branch = f"subagent/{ts}"
    wt_parent = os.path.join(os.path.dirname(repo_root), f"{repo_name}-subagents")
    try:
        os.makedirs(wt_parent, exist_ok=True)
    except OSError as e:
        print(f"[WORKTREE] mkdir {wt_parent} failed: {e}", flush=True)
        return None
    wt_path = os.path.join(wt_parent, ts)

    status, _ = await _git(["status", "--porcelain"])
    wip_notice = ""
    if status:
        wip_count = len(status.splitlines())
        wip_notice = (
            f"📝 **提醒**：原 working tree `{repo_root}` 有 {wip_count} 項未 commit 的改動，"
            f"會原地保留不動。新 worktree 從 `{base_ref or 'HEAD'}` 起建，**不含這些 WIP**。"
        )

    # Resolve base_ref: try as-is, then origin/<ref>, then fall back to HEAD
    actual_ref = "HEAD"
    if base_ref:
        # Ensure we have the latest remote state for the branch
        await _git(["fetch", "origin", base_ref], timeout=30.0)
        # Prefer local branch if it exists
        _, rc_local = await _git(["rev-parse", "--verify", f"refs/heads/{base_ref}"])
        if rc_local == 0:
            actual_ref = base_ref
        else:
            # Try remote tracking branch
            _, rc_remote = await _git(["rev-parse", "--verify", f"origin/{base_ref}"])
            if rc_remote == 0:
                actual_ref = f"origin/{base_ref}"
            else:
                print(f"[WORKTREE] base_ref '{base_ref}' not found locally or on origin, "
                      f"falling back to HEAD", flush=True)

    _, rc = await _git(["worktree", "add", wt_path, "-b", branch, actual_ref])
    if rc != 0:
        return None

    display_ref = base_ref if actual_ref != "HEAD" else None
    return wt_path, branch, wip_notice, display_ref


async def _run_todo_dispatch(todos: list[str], cwd: str, channel,
                             base_pikmin_idx: int,
                             base_branch: str | None = None) -> None:
    """Run detected TODOs strictly serially: each as a gen-eval convergence loop.

    Creates an isolated worktree + branch first so subagent changes don't
    pollute the user's main checkout. *base_branch*, when set, tells the
    worktree to fork from that branch instead of HEAD (which may be main).
    Rotates the generator pikmin per TODO so each item visibly runs as a
    different subagent; evaluator pikmin is picked by `_pick_eval_pikmin`
    inside `generator_evaluator_loop`.
    """
    total = len(todos)

    # Auto-detect base branch from thread context if not explicitly given
    if not base_branch:
        base_branch = await _detect_base_branch(cwd, channel)
        if base_branch:
            print(f"[TODO_DISPATCH] auto-detected base branch: {base_branch}", flush=True)

    wt_info = await _create_subagent_worktree(cwd, base_ref=base_branch)
    if wt_info:
        wt_path, branch, wip_notice, forked_from = wt_info
        if wip_notice:
            await channel.send(wip_notice)
        fork_label = f"（基於 `{forked_from}`）" if forked_from else ""
        await channel.send(
            f"🌿 **建立隔離 worktree**{fork_label}\n"
            f"Branch：`{branch}`\n"
            f"Path：`{wt_path}`\n"
            f"所有 subagent 改動都會在這裡，主 checkout 不受影響。"
        )
        work_cwd = wt_path
    else:
        await channel.send(
            f"⚠️ 無法建立 worktree（不是 git repo 或建立失敗），**將在原目錄執行**。"
        )
        work_cwd = cwd
        branch = None
        wt_path = None
        forked_from = None

    await channel.send(
        f"🚀 **開始逐項處理 {total} 項 TODO**（串行：一項跑到收斂再進下一項）"
    )

    for i, todo in enumerate(todos, 1):
        pidx = (base_pikmin_idx + i) % len(PIKMIN_POOL)
        gen_pikmin = PIKMIN_POOL[pidx]

        await channel.send(f"---\n## 🔧 TODO {i}/{total}\n{todo}")

        initial_prompt = (
            f"請處理以下 TODO（來自剛才的 code review / 檢查）：\n\n{todo}\n\n"
            f"修改完成後，用一兩句話說明你改了什麼、動到哪些檔案。"
        )
        eval_context = f"## TODO 項目（本次 generator 的改動目標）\n{todo}"

        try:
            _, passed = await generator_evaluator_loop(
                initial_prompt=initial_prompt,
                cwd=work_cwd,
                channel=channel,
                gen_pikmin=gen_pikmin,
                eval_context=eval_context,
                show_status_prefix=f"TODO {i}/{total}",
            )
            status = "✅ 通過" if passed else "⚠️ 未完全收斂（繼續下一項）"
            await channel.send(f"**TODO {i}/{total} {status}**")
        except Exception as e:
            print(f"[TODO_DISPATCH] error on TODO {i}: {e}", flush=True)
            await channel.send(f"❌ TODO {i} 執行失敗：`{e}`")

    if branch and wt_path:
        diff_ref = forked_from or "main"
        await channel.send(
            f"🏁 **全部 {total} 項 TODO 處理完成**\n"
            f"改動在 branch **`{branch}`**，worktree `{wt_path}`\n"
            f"• 切過去看：`cd {wt_path}`\n"
            f"• 看所有 commits：`git -C {wt_path} log HEAD --not {diff_ref} --oneline`\n"
            f"• 不需要就丟掉：`git worktree remove {wt_path} && git branch -D {branch}`"
        )
    else:
        await channel.send(f"🏁 **全部 {total} 項 TODO 處理完成**")


class TodoDispatchView(discord.ui.View):
    """Buttons to dispatch or skip detected TODOs for serial subagent processing."""

    def __init__(self, todos: list[str] | None = None, cwd: str = "",
                 channel=None, base_pikmin_idx: int = 0,
                 base_branch: str | None = None):
        super().__init__(timeout=None)
        self.todos = todos or []
        self.cwd = cwd
        self.channel = channel
        self.base_pikmin_idx = base_pikmin_idx
        self.base_branch = base_branch

    def store(self, message_id: int):
        _todo_dispatch_data[message_id] = {
            "todos": self.todos,
            "cwd": self.cwd,
            "channel_id": self.channel.id if self.channel else None,
            "base_pikmin_idx": self.base_pikmin_idx,
            "base_branch": self.base_branch,
        }

    def _load(self, interaction: discord.Interaction) -> bool:
        data = _todo_dispatch_data.get(interaction.message.id)
        if not data:
            return False
        self.todos = data["todos"]
        self.cwd = data["cwd"]
        self.channel = interaction.channel
        self.base_pikmin_idx = data["base_pikmin_idx"]
        self.base_branch = data.get("base_branch")
        return True

    @discord.ui.button(label="🚀 逐項派 subagent 處理", style=discord.ButtonStyle.primary,
                       custom_id="todo_dispatch:run")
    async def run_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._load(interaction):
            await interaction.response.edit_message(content=_EXPIRED_MSG, view=None)
            return
        print(f"[TODO_DISPATCH] run pressed by {interaction.user} — {len(self.todos)} items", flush=True)
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.NotFound:
            return
        _todo_dispatch_data.pop(interaction.message.id, None)
        asyncio.create_task(_run_todo_dispatch(
            todos=self.todos, cwd=self.cwd, channel=self.channel,
            base_pikmin_idx=self.base_pikmin_idx,
            base_branch=self.base_branch,
        ))

    @discord.ui.button(label="🌿 換 branch", style=discord.ButtonStyle.secondary,
                       custom_id="todo_dispatch:change_branch")
    async def change_branch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._load(interaction):
            await interaction.response.edit_message(content=_EXPIRED_MSG, view=None)
            return
        print(f"[TODO_DISPATCH] change_branch pressed by {interaction.user}", flush=True)

        # Show a modal to collect the correct branch name
        modal = _BranchInputModal(
            message_id=interaction.message.id,
            current_branch=self.base_branch,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="⏭️ 跳過", style=discord.ButtonStyle.secondary,
                       custom_id="todo_dispatch:skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(f"[TODO_DISPATCH] skip pressed by {interaction.user}", flush=True)
        _todo_dispatch_data.pop(interaction.message.id, None)
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.NotFound:
            pass


class _BranchInputModal(discord.ui.Modal, title="指定 base branch"):
    """Modal that lets the user correct the auto-detected branch before dispatch."""

    branch_input = discord.ui.TextInput(
        label="Branch name",
        placeholder="例如 feature/caplin_gloas_rebase",
        required=True,
        max_length=200,
    )

    def __init__(self, message_id: int, current_branch: str | None = None):
        super().__init__()
        self._dispatch_msg_id = message_id
        if current_branch:
            self.branch_input.default = current_branch

    async def on_submit(self, interaction: discord.Interaction):
        data = _todo_dispatch_data.get(self._dispatch_msg_id)
        if not data:
            await interaction.response.send_message("⚠️ 資料已過期，請重新發問。", ephemeral=True)
            return

        new_branch = self.branch_input.value.strip()
        if not new_branch:
            await interaction.response.send_message("⚠️ Branch 不能是空的。", ephemeral=True)
            return

        # Update stored data with the corrected branch
        data["base_branch"] = new_branch
        print(f"[TODO_DISPATCH] branch corrected to: {new_branch}", flush=True)

        # Disable buttons on the original message
        try:
            orig_msg = await interaction.channel.fetch_message(self._dispatch_msg_id)
            await orig_msg.edit(view=None)
        except Exception:
            pass

        _todo_dispatch_data.pop(self._dispatch_msg_id, None)

        await interaction.response.send_message(
            f"🌿 Branch 已更新為 `{new_branch}`，開始 dispatch...",
        )
        asyncio.create_task(_run_todo_dispatch(
            todos=data["todos"], cwd=data["cwd"], channel=interaction.channel,
            base_pikmin_idx=data["base_pikmin_idx"],
            base_branch=new_branch,
        ))


_plan_data: dict[int, dict] = {}


class PlanView(discord.ui.View):
    """Buttons for plan confirmation: Start / Modify / Cancel."""

    def __init__(self, plan: dict | None = None, task: str = "", cwd: str = "",
                 channel=None, pikmin_idx: int = 0):
        super().__init__(timeout=None)
        self.plan = plan
        self.task = task
        self.cwd = cwd
        self.channel = channel
        self.pikmin_idx = pikmin_idx

    def store(self, message_id: int):
        _plan_data[message_id] = {
            "plan": self.plan, "task": self.task, "cwd": self.cwd,
            "channel_id": self.channel.id if self.channel else None,
            "pikmin_idx": self.pikmin_idx,
        }

    def _load(self, interaction: discord.Interaction) -> bool:
        data = _plan_data.get(interaction.message.id)
        if not data:
            return False
        self.plan = data["plan"]
        self.task = data["task"]
        self.cwd = data["cwd"]
        self.channel = interaction.channel
        self.pikmin_idx = data["pikmin_idx"]
        return True

    def _disable_all(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="✅ 開始執行", style=discord.ButtonStyle.success, custom_id="plan:start")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._load(interaction):
            await interaction.response.edit_message(content=_EXPIRED_MSG, view=None)
            return
        self._disable_all()
        await interaction.response.edit_message(view=self)
        _plan_data.pop(interaction.message.id, None)
        pending_plans.pop(self.channel.id, None)
        asyncio.create_task(execute_plan(self.plan, self.cwd, self.channel, self.pikmin_idx))

    @discord.ui.button(label="✏️ 修改", style=discord.ButtonStyle.primary, custom_id="plan:modify")
    async def modify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._load(interaction):
            await interaction.response.edit_message(content=_EXPIRED_MSG, view=None)
            return
        self._disable_all()
        await interaction.response.edit_message(view=self)
        _plan_data.pop(interaction.message.id, None)
        await self.channel.send("請說明要怎麼修改這個計畫，例如：「步驟 2 和 3 合併」、「加一個測試步驟」等。")
        pending_plans[self.channel.id] = {
            "plan": self.plan, "task": self.task, "cwd": self.cwd, "pikmin_idx": self.pikmin_idx,
        }

    @discord.ui.button(label="❌ 取消", style=discord.ButtonStyle.danger, custom_id="plan:cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._load(interaction):
            await interaction.response.edit_message(content=_EXPIRED_MSG, view=None)
            return
        self._disable_all()
        await interaction.response.edit_message(view=self)
        _plan_data.pop(interaction.message.id, None)
        pending_plans.pop(self.channel.id, None)
        await self.channel.send("📋 計畫已取消。")


# ── Thread Summaries ─────────────────────────────────────────────────────────

SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "claude-haiku-4-5-20251001")


async def update_thread_summary(channel_id: int, user_msg: str, bot_reply: str, topic: str = ""):
    """Background task: generate a short summary for this thread using Haiku."""
    prompt = (
        f"以下是一個 Discord thread 的最新一輪對話。請用繁體中文寫一句話摘要（30字以內），"
        f"描述這個 thread 目前在做什麼。只輸出摘要本身，不要加標點以外的格式。\n\n"
        f"用戶：{user_msg[:500]}\n\nBot：{bot_reply[:500]}"
    )
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--model", SUMMARY_MODEL,
        "--output-format", "text",
        "--max-turns", "1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=WORK_ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        summary = stdout.decode("utf-8", errors="replace").strip()
        if summary:
            thread_summaries[str(channel_id)] = {
                "summary": summary[:100],
                "topic": topic,
                "updated_at": time.time(),
            }
            _save_summaries()
            print(f"[SUMMARY] #{channel_id}: {summary[:60]}", flush=True)
    except Exception as e:
        print(f"[SUMMARY] error: {e}", flush=True)


def build_cross_thread_context(current_channel_id: int) -> str:
    """Build a short context string from other threads' summaries."""
    if not thread_summaries:
        return ""
    lines = []
    now = time.time()
    for cid_str, info in thread_summaries.items():
        if int(cid_str) == current_channel_id:
            continue
        # Skip summaries older than 24 hours
        if now - info.get("updated_at", 0) > 86400:
            continue
        topic = info.get("topic", "")
        summary = info.get("summary", "")
        if summary:
            label = f"{topic}: {summary}" if topic else summary
            lines.append(f"- {label}")
    if not lines:
        return ""
    return "其他進行中的討論：\n" + "\n".join(lines[-10:])


# ── Monitoring System ────────────────────────────────────────────────────────

def collect_metrics(config: dict) -> str:
    """Collect metrics based on monitor config. Works for both local and remote targets."""
    service = config["service_name"]
    lines = [f"=== {service} Metrics @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"]

    checks = config.get("checks", ["cpu", "memory"])

    # Process stats
    if "cpu" in checks or "memory" in checks:
        try:
            ps_out = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=10
            ).stdout
            for line in ps_out.splitlines():
                if service.lower() in line.lower() and "grep" not in line:
                    lines.append(f"[process] {line}")
        except Exception as e:
            lines.append(f"[process] error: {e}")

    # System memory
    if "memory" in checks:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if any(k in line for k in ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached")):
                        lines.append(f"[system] {line.strip()}")
        except Exception as e:
            lines.append(f"[system] error: {e}")

    # Log tail
    if "log" in checks and config.get("log_path"):
        try:
            result = subprocess.run(
                ["tail", "-n", "50", config["log_path"]],
                capture_output=True, text=True, timeout=10,
            )
            # Only include lines with errors/warnings
            for line in result.stdout.splitlines():
                lower = line.lower()
                if any(w in lower for w in ("error", "err", "warn", "fatal", "panic", "critical")):
                    lines.append(f"[log] {line}")
        except Exception as e:
            lines.append(f"[log] error: {e}")

    # Prometheus
    prom_url = config.get("prometheus_url")
    if prom_url:
        try:
            with urllib.request.urlopen(prom_url, timeout=5) as resp:
                prom_text = resp.read().decode()
            interesting = (
                "process_resident_memory", "process_virtual_memory",
                "process_cpu_seconds", "go_memstats_alloc_bytes",
                "go_memstats_sys_bytes", "go_goroutines",
            )
            for line in prom_text.splitlines():
                if line.startswith("#"):
                    continue
                if any(k in line for k in interesting):
                    lines.append(f"[prom] {line}")
        except Exception as e:
            lines.append(f"[prom] error: {e}")

    return "\n".join(lines)


COLLECT_SYSTEM_PROMPT = (
    "You are a metrics collector. Execute the requested commands, collect the data, "
    "and return ONLY a JSON object. No commentary, no markdown fences. "
    "Always respond in Traditional Chinese (繁體中文) for string values."
)

JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-haiku-4-5-20251001")


async def collect_metrics_via_claude(config: dict) -> dict:
    """Use Claude to collect data. Returns structured JSON dict.

    The output format adapts to the monitor's purpose: infrastructure monitors
    get a metrics-oriented schema, while task/issue scanners get a findings
    schema. Claude sees the check_instruction and picks the right format.
    """
    instruction = config.get("check_instruction", "")
    check_commands = config.get("check_commands", "")
    checks = config.get("checks", ["cpu", "memory"])
    service = config["service_name"]

    prompt = (
        f"你是 {service} 的監控助手。請根據以下描述執行檢查並收集數據。\n\n"
        f"## 監控目的\n{instruction}\n\n"
        f"## 檢查項目\n{', '.join(checks)}\n"
    )
    if check_commands:
        prompt += f"\n## 檢查方式\n{check_commands}\n"
    if config.get("log_path"):
        prompt += f"\n## Log 路徑\n{config['log_path']}\n"
    prompt += (
        "\n請直接執行必要的指令（SSH、curl、gh 等）收集數據。\n\n"
        "完成後，根據監控目的選擇合適的 JSON 格式回傳（不要包含 markdown code fence）：\n\n"
        "**如果是基礎設施監控**（CPU/memory/process/sync 等）：\n"
        '{"metrics": {"cpu_percent": <number or null>, "memory_mb": <number or null>, '
        '"disk_percent": <number or null>, "process_running": <true/false>, '
        '"sync_status": "<描述 or null>", "peer_count": <number or null>, '
        '"block_height": <number or null>, "error_count": <number or null>}, '
        '"signals": ["<觀察到的趨勢或異常跡象>"], '
        '"raw_notes": "<其他觀察>"}\n\n'
        "**如果是任務掃描**（bug scan、issue triage、code audit 等）：\n"
        '{"findings": [{"title": "<簡述>", "severity": "high/medium/low", '
        '"effort": "trivial/small/medium/large", "details": "<詳細描述>"}], '
        '"signals": ["<觀察到的趨勢>"], '
        '"raw_notes": "<其他觀察>"}\n\n'
        "根據「監控目的」選擇格式。兩種格式都有 signals 和 raw_notes 欄位。\n"
        "只回傳 JSON，不要加其他文字。缺少的欄位用 null。"
    )

    result, _, _, _, _ = await run_claude(
        prompt, config.get("project_path", WORK_ROOT),
        system_prompt=COLLECT_SYSTEM_PROMPT,
    )

    # Parse structured output
    cleaned = re.sub(r'```(?:json)?\s*', '', result).strip()
    try:
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: wrap raw text (shape-neutral — no assumed metrics/findings key)
    return {"raw_notes": result[:500]}


def _build_judge_context(config: dict, history: list[dict], open_incident: dict | None) -> dict:
    """Build structured context for judgment from history."""
    service = config.get("service_name", config.get("name", "unknown"))
    knowledge = config.get("knowledge", [])

    readings = []
    for h in history[-6:]:
        entry = {"time": h["time"]}
        data = h["data"]
        if isinstance(data, dict):
            entry.update(data)
        else:
            entry["raw"] = str(data)[:300]
        readings.append(entry)

    context = {
        "service": service,
        "purpose": config.get("check_instruction", ""),
        "readings": readings,
        "has_open_incident": bool(open_incident),
    }
    if knowledge:
        context["knowledge"] = knowledge[-10:]
    return context


def _parse_judge_result(result: str) -> dict:
    """Parse JSON from judge response with fallback."""
    cleaned = re.sub(r'```(?:json)?\s*', '', result).strip()
    try:
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
    except (json.JSONDecodeError, TypeError):
        pass
    is_anomaly = any(w in result.lower() for w in ("異常", "anomaly", "error", "critical"))
    return {"anomaly": is_anomaly, "summary": result[:200], "details": result, "confidence": "low"}


JUDGE_PROMPT_TEMPLATE = (
    "你是 {service} 的監控判斷助手。\n\n"
    "## 監控目的\n"
    "context 中的 `purpose` 欄位描述了使用者建立這個監控的意圖和關注點。\n"
    "你的判斷標準必須對齊這個意圖：\n"
    "- 如果 purpose 是偵測異常（CPU/memory/sync 等）→ 判斷數據是否有問題、趨勢是否異常\n"
    "- 如果 purpose 是掃描工作項目（bug/issue/task 等）→ 判斷是否有值得報告或行動的發現\n"
    "- 看 readings 之間的**變化趨勢**，不只看單一數值。例如 memory 持續上升 = 潛在 leak，"
    "peer 數量持續下降 = 網路問題\n\n"
    "## 數據\n{context_json}\n\n"
    "回覆一個 JSON object（不要 markdown code fence）：\n"
    '{{"anomaly": true/false, "summary": "簡短描述（繁體中文）", '
    '"details": "詳細分析（繁體中文）", '
    '"confidence": "high/medium/low", '
    '"lesson": "新學到的知識 or null"}}\n\n'
    "anomaly=true 代表「有需要報告給使用者的事項」，不限於故障，也包含 purpose 中描述的任何觸發條件。\n\n"
    "confidence 說明：\n"
    "- high: 數據明確，判斷有把握\n"
    "- medium: 有些跡象但不確定，可能需要更多數據\n"
    "- low: 數據不足或訊號矛盾，無法確定判斷"
)

ESCALATE_PROMPT_TEMPLATE = (
    "你是 {service} 的資深監控分析師。Haiku 初步判斷的 confidence 為 low，需要你做深度分析。\n\n"
    "context 中的 `purpose` 欄位描述了使用者的監控意圖，你的判斷必須對齊這個意圖。\n"
    "anomaly=true 代表「有需要報告給使用者的事項」，不限於故障。\n\n"
    "=== Haiku 的初步判斷 ===\n{haiku_result}\n\n"
    "=== 完整監控數據 ===\n{context_json}\n\n"
    "請仔細分析所有 signals、metrics 或 findings 的趨勢，做出最終判斷。\n"
    "回覆 JSON（不要 markdown code fence）：\n"
    '{{"anomaly": true/false, "summary": "簡短描述（繁體中文）", '
    '"details": "詳細分析（繁體中文）", '
    '"lesson": "新學到的知識 or null"}}'
)


async def judge_metrics(config: dict, history: list[dict], open_incident: dict | None) -> dict:
    """Two-tier judgment: Haiku first, escalate to Opus if confidence is low."""
    service = config.get("service_name", config.get("name", "unknown"))
    context = _build_judge_context(config, history, open_incident)
    context_json = json.dumps(context, ensure_ascii=False, indent=2)

    # ── Tier 1: Haiku (fast, cheap) ──
    prompt = JUDGE_PROMPT_TEMPLATE.format(service=service, context_json=context_json)
    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--model", JUDGE_MODEL,
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", "1",
    ]
    result, _, _, _, _, _ = await _run_claude_stream(cmd, config.get("project_path", WORK_ROOT))
    analysis = _parse_judge_result(result or "")

    confidence = analysis.get("confidence", "high")
    print(f"[JUDGE] {config['name']} Haiku verdict: anomaly={analysis.get('anomaly')}, confidence={confidence}", flush=True)

    # ── Tier 2: Escalate to Opus if low confidence ──
    if confidence == "low":
        print(f"[JUDGE] {config['name']} escalating to Opus for deep analysis", flush=True)
        haiku_summary = json.dumps(analysis, ensure_ascii=False)
        escalate_prompt = ESCALATE_PROMPT_TEMPLATE.format(
            service=service, haiku_result=haiku_summary, context_json=context_json,
        )
        escalate_cmd = [
            CLAUDE_BIN,
            "-p", escalate_prompt,
            "--model", CLAUDE_MODEL,
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", "1",
        ]
        escalate_result, _, _, _, _, _ = await _run_claude_stream(escalate_cmd, config.get("project_path", WORK_ROOT))
        opus_analysis = _parse_judge_result(escalate_result or "")
        print(f"[JUDGE] {config['name']} Opus verdict: anomaly={opus_analysis.get('anomaly')}", flush=True)
        return opus_analysis

    return analysis


def quick_threshold_check(metrics: str) -> dict | None:
    """Fast Python-based threshold check. Returns anomaly info or None."""
    # Extract CPU % from ps output
    for line in metrics.splitlines():
        if "[process]" in line:
            parts = line.split()
            # ps aux format: USER PID %CPU %MEM ...
            try:
                idx = next(i for i, p in enumerate(parts) if p == "[process]") + 1
                if idx + 3 < len(parts):
                    cpu = float(parts[idx + 2])
                    mem = float(parts[idx + 3])
                    if cpu > 95:
                        return {"type": "cpu", "value": cpu, "msg": f"CPU 使用率 {cpu}%"}
                    if mem > 90:
                        return {"type": "memory", "value": mem, "msg": f"Memory 使用率 {mem}%"}
            except (StopIteration, ValueError, IndexError):
                pass

    # Check for error lines in logs
    error_lines = [l for l in metrics.splitlines() if "[log]" in l]
    if len(error_lines) > 10:
        return {"type": "log", "value": len(error_lines), "msg": f"Log 中有 {len(error_lines)} 行錯誤"}

    return None


MAX_KNOWLEDGE = 50  # cap to prevent unbounded growth


def _maybe_learn(config: dict, analysis: dict) -> str | None:
    """Extract and store a lesson from Claude's analysis, if any."""
    lesson = analysis.get("lesson")
    if not lesson or lesson == "null":
        return None
    lesson = str(lesson).strip()
    if not lesson:
        return None
    knowledge = config.setdefault("knowledge", [])
    # Skip if too similar to existing knowledge
    for existing in knowledge:
        if lesson in existing or existing in lesson:
            return None
    if len(knowledge) >= MAX_KNOWLEDGE:
        # Remove oldest non-user-taught lesson
        for i, k in enumerate(knowledge):
            if k.startswith("自動學習："):
                knowledge.pop(i)
                break
        else:
            return None  # all are user-taught, don't evict
    knowledge.append(f"自動學習：{lesson}")
    _save_monitors()
    return lesson


def find_incident_by_thread(thread_id: int) -> dict | None:
    """Find an active incident by its thread ID."""
    for inc in active_incidents.values():
        if inc.get("thread_id") == thread_id and inc.get("status") != "resolved":
            return inc
    return None


def find_incident_by_monitor(monitor_id: str) -> dict | None:
    """Find an open incident for a given monitor."""
    for inc in active_incidents.values():
        if inc.get("monitor_id") == monitor_id and inc.get("status") not in ("resolved",):
            return inc
    return None


async def create_incident(monitor_id: str, channel, analysis_summary: str, analysis_details: str):
    """Create a new incident thread."""
    config = monitor_configs[monitor_id]
    inc_id = f"inc_{uuid.uuid4().hex[:8]}"

    nickname = config.get("nickname", config["name"])
    pidx = config.get("pikmin_index", 0)
    pikmin = PIKMIN_POOL[pidx % len(PIKMIN_POOL)]
    alert_msg = await pikmin_send(channel, f"⚠️ **{nickname} 監控偵測到異常**", pikmin)
    thread = await alert_msg.create_thread(
        name=f"🚨 {nickname} 異常 - {time.strftime('%m/%d %H:%M')}"
    )
    bot_threads.add(thread.id)
    channel_workdir[thread.id] = config.get("project_path", WORK_ROOT)

    incident = {
        "id": inc_id,
        "monitor_id": monitor_id,
        "thread_id": thread.id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "open",
        "summary": analysis_summary,
        "worktree_info": None,
        "consecutive_ok": 0,
    }
    active_incidents[inc_id] = incident
    _save_monitors()
    _save_state()

    chunks = split_message(f"**{analysis_summary}**\n\n{analysis_details}")
    for chunk in chunks:
        await pikmin_send(thread, chunk, pikmin)
    await pikmin_send(thread, "如果要修，說「修吧」，我會開 branch 來處理。", pikmin)

    return incident


async def resolve_incident(incident: dict):
    """Resolve an incident and archive the thread."""
    thread = client.get_channel(incident["thread_id"])
    if thread:
        config = monitor_configs.get(incident.get("monitor_id"), {})
        pidx = config.get("pikmin_index", 0)
        pikmin = PIKMIN_POOL[pidx % len(PIKMIN_POOL)]
        await pikmin_send(thread, "✅ 連續多次檢查正常，問題已解決。關閉此 thread。", pikmin)
        try:
            await thread.edit(archived=True)
        except Exception:
            pass

    # Clean up worktree if any
    if incident.get("worktree_info"):
        wt = incident["worktree_info"]
        try:
            await remove_worktree(wt["repo"], wt["worktree"])
        except Exception:
            pass

    incident["status"] = "resolved"
    _save_monitors()


async def handle_fix_request(message, incident: dict):
    """User said '修吧' — create worktree and let Claude fix."""
    config = monitor_configs.get(incident["monitor_id"])
    if not config or not config.get("project_path"):
        await message.reply("❌ 這個監控沒有設定專案路徑，無法自動修復。")
        return

    repo_path = config["project_path"]
    branch_name = f"fix/{incident['id']}"

    status = await message.reply("⏳ 正在建立 worktree...")
    try:
        worktree_path = await create_worktree(repo_path, branch_name)
    except Exception as e:
        await status.edit(content=f"❌ 建立 worktree 失敗：{e}")
        return

    incident["worktree_info"] = {
        "repo": repo_path,
        "worktree": worktree_path,
        "branch": branch_name,
    }
    incident["status"] = "fixing"
    channel_workdir[message.channel.id] = worktree_path
    channel_worktrees[message.channel.id] = incident["worktree_info"]
    _save_monitors()
    _save_state()

    await status.edit(content=f"✅ Worktree 建好了\n📂 `{worktree_path}`\n🌿 `{branch_name}`")

    # Let Claude analyze and fix
    fix_prompt = (
        f"監控偵測到以下問題：\n{incident['summary']}\n\n"
        f"請分析 code 找出問題原因並修復。修完後告訴我改了什麼。"
    )
    thinking = await message.channel.send("⏳ 分析中...")
    try:
        result, new_session_id, _, _, _ = await run_claude(
            fix_prompt, worktree_path, status_msg=thinking, channel=message.channel
        )
        if new_session_id:
            channel_session[message.channel.id] = new_session_id
            _save_state()
        chunks = split_message(result)
        await thinking.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await message.channel.send(chunk)
        await message.channel.send("修完了。要「跑看看」嗎？")
    except Exception as e:
        await thinking.edit(content=f"❌ Error: {e}")


async def handle_restart_request(message, incident: dict):
    """User said '跑看看' — restart the service."""
    config = monitor_configs.get(incident["monitor_id"])
    if not config:
        await message.reply("❌ 找不到監控設定。")
        return

    service = config["service_name"]
    status = await message.reply(f"⏳ 正在重啟 `{service}`...")

    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "restart", service,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            await status.edit(content=f"❌ 重啟失敗：{stderr.decode()[:500]}")
            return
    except Exception as e:
        await status.edit(content=f"❌ 重啟失敗：{e}")
        return

    incident["status"] = "testing"
    incident["consecutive_ok"] = 0
    _save_monitors()

    await status.edit(content=f"✅ `{service}` 已重啟。監控會持續檢查，正常的話會自動關閉這個 thread。")


async def monitor_loop(monitor_id: str, channel):
    """Per-monitor background loop with incident lifecycle."""
    config = monitor_configs[monitor_id]
    check_interval = config.get("check_interval", 300)
    summary_interval = config.get("summary_interval", 10800)
    last_summary_time = time.time()
    consecutive_ok_for_summary = 0

    nickname = config.get("nickname", config["name"])
    display = f"{nickname} ({config['name']})" if nickname != config["name"] else config["name"]
    knowledge_count = len(config.get("knowledge", []))
    pidx = config.get("pikmin_index", 0)
    pikmin = PIKMIN_POOL[pidx % len(PIKMIN_POOL)]
    startup_msg = f"📊 **{display}** 監控已啟動\n每 {check_interval // 60} 分鐘檢查，每 {summary_interval // 3600} 小時回報狀態。"
    if knowledge_count:
        startup_msg += f"\n🧠 已載入 {knowledge_count} 條知識"
    await pikmin_send(channel, startup_msg, pikmin)

    # Determine if this monitor needs Claude to collect metrics
    # If check_commands exists, always use Claude (it was generated by Claude's analysis)
    use_claude_collect = bool(config.get("check_commands"))

    # Calculate initial sleep: if enough time has elapsed since last check,
    # run immediately instead of waiting a full interval. This prevents
    # long-interval monitors (e.g., 24h) from never firing when the bot
    # restarts more frequently than the interval.
    last_check_ts = config.get("last_check_ts", 0)
    elapsed = time.time() - last_check_ts if last_check_ts else float("inf")
    initial_sleep = max(check_interval - elapsed, 0)
    if initial_sleep > 0:
        print(f"[MONITOR] {config['name']} sleeping {initial_sleep:.0f}s "
              f"(last check {elapsed:.0f}s ago)", flush=True)

    while True:
        await asyncio.sleep(initial_sleep)
        initial_sleep = check_interval  # subsequent iterations use full interval
        if not config.get("enabled", True):
            print(f"[MONITOR] {config['name']} disabled, exiting loop", flush=True)
            break
        try:
            # ── Step 1: Collect (fresh Claude session, structured output) ──
            if use_claude_collect:
                metrics = await collect_metrics_via_claude(config)
            else:
                metrics = collect_metrics(config)

            # ── Step 2: Store structured artifact in history ──
            history = monitor_histories.setdefault(monitor_id, [])
            history.append({"time": time.strftime("%H:%M:%S"), "data": metrics})
            if len(history) > 36:
                monitor_histories[monitor_id] = history[-36:]

            open_incident = find_incident_by_monitor(monitor_id)

            # ── Step 3: Judge (clean context via Haiku, no tool-call pollution) ──
            if use_claude_collect:
                analysis = await judge_metrics(config, history, open_incident)
                is_anomaly = analysis.get("anomaly", False)
                summary = analysis.get("summary", "異常")
                details = analysis.get("details", "")
                _maybe_learn(config, analysis)
            else:
                threshold_hit = quick_threshold_check(metrics)
                if threshold_hit:
                    analysis = await judge_metrics(config, history, open_incident)
                    is_anomaly = analysis.get("anomaly", False)
                    summary = analysis.get("summary", threshold_hit["msg"])
                    details = analysis.get("details", "")
                    _maybe_learn(config, analysis)
                else:
                    is_anomaly = False
                    summary = ""
                    details = ""

            # ── Step 4: Act on judgment ──
            if is_anomaly:
                if open_incident:
                    open_incident["consecutive_ok"] = 0
                    thread = client.get_channel(open_incident["thread_id"])
                    if thread:
                        update_text = f"📊 **持續異常** ({time.strftime('%H:%M')})\n{summary}\n\n{details}"
                        for chunk in split_message(update_text):
                            await pikmin_send(thread, chunk, pikmin)
                else:
                    try:
                        await create_incident(monitor_id, channel, summary, details)
                    except Exception as e:
                        print(f"[MONITOR] Failed to create incident: {e}", flush=True)
                consecutive_ok_for_summary = 0
            else:
                consecutive_ok_for_summary += 1
                if open_incident:
                    open_incident["consecutive_ok"] = open_incident.get("consecutive_ok", 0) + 1
                    if open_incident["consecutive_ok"] >= 3:
                        await resolve_incident(open_incident)
                    else:
                        thread = client.get_channel(open_incident["thread_id"])
                        if thread:
                            await pikmin_send(thread,
                                f"📊 檢查正常 ({open_incident['consecutive_ok']}/3)，"
                                f"連續 3 次正常就自動關閉。",
                                pikmin)
                    _save_monitors()

            # ── Periodic summary ──
            now = time.time()
            if now - last_summary_time >= summary_interval:
                last_summary_time = now
                active_count = len([i for i in active_incidents.values()
                                    if i.get("monitor_id") == monitor_id and i.get("status") != "resolved"])
                readings = len(monitor_histories.get(monitor_id, []))
                summary_text = (
                    f"📊 **{nickname} 定期報告** ({time.strftime('%m/%d %H:%M')})\n"
                    f"已收集 {readings} 筆數據\n"
                )
                if active_count:
                    summary_text += f"⚠️ 有 {active_count} 個未解決的 incident\n"
                else:
                    summary_text += "✅ 一切正常\n"
                await pikmin_send(channel, summary_text, pikmin)

            # Record last check time so restarts can calculate remaining sleep
            config["last_check_ts"] = time.time()
            _save_monitors()

            print(
                f"[MONITOR] {config['name']} {time.strftime('%H:%M:%S')} - "
                f"{'ANOMALY' if is_anomaly else 'OK'}",
                flush=True,
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[MONITOR] {config['name']} Error: {e}", flush=True)


async def parse_monitor_config(text: str, channel_id: int) -> dict | None:
    """Parse natural language into a monitor config using Claude for analysis."""
    project = detect_project(text)
    project_path = os.path.join(WORK_ROOT, project) if project else WORK_ROOT

    # Let Claude analyze everything
    analyze_prompt = (
        f"使用者想建立一個監控：「{text}」\n\n"
        "請分析這個需求並回覆 JSON（不要有其他文字）：\n"
        "{\n"
        '  "name": "簡短的監控名稱，例如 erigon、gloas-spec-monitor",\n'
        '  "nickname": "使用者指定的暱稱，如果沒有就跟 name 一樣",\n'
        '  "checks": ["要檢查的項目，例如 cpu, memory, log, sync, disk, http, port, process, github, custom"],\n'
        '  "check_interval_min": 檢查頻率（分鐘，預設5）,\n'
        '  "summary_interval_hour": 定期回報間隔（小時，預設3）,\n'
        '  "check_commands": "具體的檢查指令說明，例如：SSH 到 X 執行 Y、curl Z、gh api 等",\n'
        '  "log_path": "log路徑或null",\n'
        '  "prometheus_url": "prometheus URL或null"\n'
        "}\n\n"
        "根據使用者的描述推斷合理的檢查項目和方式。如果提到遠端機器，check_commands 要包含 SSH 指令。"
        "如果提到 GitHub repo，check_commands 要包含 gh api 或 curl GitHub API 的指令。"
    )

    try:
        result, _, _, _, _ = await run_claude(analyze_prompt, WORK_ROOT)
        print(f"[MONITOR] Claude analysis raw: {result[:500]}", flush=True)
        cleaned = re.sub(r'```(?:json)?\s*', '', result).strip()
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start >= 0 and end > start:
            analysis = json.loads(cleaned[start:end + 1])
        else:
            analysis = json.loads(cleaned)
        print(f"[MONITOR] Parsed analysis: {analysis}", flush=True)
    except Exception as e:
        print(f"[MONITOR] Analysis parse failed: {e}", flush=True)
        return None

    name = analysis.get("name", f"monitor-{uuid.uuid4().hex[:6]}")
    nickname = analysis.get("nickname", name)
    checks = analysis.get("checks", ["custom"])

    try:
        check_interval = int(analysis.get("check_interval_min", 5)) * 60
    except (TypeError, ValueError):
        check_interval = 300
    try:
        summary_interval = int(analysis.get("summary_interval_hour", 3)) * 3600
    except (TypeError, ValueError):
        summary_interval = 10800

    log_path = analysis.get("log_path")
    if not log_path or log_path == "null":
        log_path = None
    prom_url = analysis.get("prometheus_url")
    if not prom_url or prom_url == "null":
        prom_url = None
    check_commands = analysis.get("check_commands", "")

    mid = f"m_{uuid.uuid4().hex[:8]}"
    return {
        "id": mid,
        "name": name,
        "nickname": nickname,
        "channel_id": channel_id,
        "check_interval": check_interval,
        "summary_interval": summary_interval,
        "checks": checks,
        "service_name": name,
        "log_path": log_path,
        "prometheus_url": prom_url,
        "project_path": project_path,
        "check_instruction": text,
        "check_commands": check_commands,
        "knowledge": [],
        "enabled": True,
    }


async def adjust_monitor_config(config: dict, text: str) -> dict:
    """Adjust a pending monitor config based on user feedback using Claude."""
    # Use Claude to understand the adjustment
    current = json.dumps({
        "checks": config.get("checks", []),
        "check_interval_min": config.get("check_interval", 300) // 60,
        "summary_interval_hour": config.get("summary_interval", 10800) // 3600,
        "check_commands": config.get("check_commands", ""),
        "log_path": config.get("log_path"),
        "nickname": config.get("nickname", config.get("name")),
    }, ensure_ascii=False, indent=2)

    adjust_prompt = (
        f"目前的監控設定：\n{current}\n\n"
        f"使用者的調整要求：「{text}」\n\n"
        "請根據使用者的要求修改設定，回覆完整的 JSON（不要有其他文字）：\n"
        "{\n"
        '  "checks": [...],\n'
        '  "check_interval_min": 數字,\n'
        '  "summary_interval_hour": 數字,\n'
        '  "check_commands": "更新後的檢查方式",\n'
        '  "log_path": "路徑或null",\n'
        '  "nickname": "暱稱"\n'
        "}\n"
        "只修改使用者要求改的部分，其他保持不變。"
    )

    try:
        result, _, _, _, _ = await run_claude(adjust_prompt, WORK_ROOT)
        start = result.find('{')
        end = result.rfind('}')
        if start >= 0 and end > start:
            updates = json.loads(result[start:end + 1])
        else:
            updates = json.loads(result)

        if "checks" in updates:
            config["checks"] = updates["checks"]
        if "check_interval_min" in updates:
            config["check_interval"] = int(updates["check_interval_min"]) * 60
        if "summary_interval_hour" in updates:
            config["summary_interval"] = int(updates["summary_interval_hour"]) * 3600
        if "check_commands" in updates:
            config["check_commands"] = updates["check_commands"]
        if "log_path" in updates and updates["log_path"] != "null":
            config["log_path"] = updates["log_path"]
        if "nickname" in updates:
            config["nickname"] = updates["nickname"]
    except Exception as e:
        print(f"[MONITOR] adjust error: {e}", flush=True)
        # Fallback: simple keyword adjustments
        nick_match = re.search(r"(?:叫[他她它]?|叫做|改名|暱稱|nickname)\s*(\S+)", text)
        if nick_match:
            config["nickname"] = nick_match.group(1).strip("，。、")
        interval_match = re.search(r"(?:每|改成?\s*每?)\s*(\d+)\s*分鐘", text)
        if interval_match:
            config["check_interval"] = int(interval_match.group(1)) * 60

    return config


def _find_monitor_by_text(text: str) -> dict | None:
    """Find a monitor config by nickname or service name mentioned in text."""
    text_lower = text.lower()
    for config in monitor_configs.values():
        nickname = config.get("nickname", "").lower()
        name = config.get("name", "").lower()
        if nickname and nickname in text_lower:
            return config
        if name and name in text_lower:
            return config
    return None


def _find_monitor_by_pikmin_name(pikmin_name: str) -> dict | None:
    """Find a monitor config by its assigned pikmin's display name."""
    for config in monitor_configs.values():
        pidx = config.get("pikmin_index")
        if pidx is not None and 0 <= pidx < len(PIKMIN_POOL):
            if PIKMIN_POOL[pidx]["name"] == pikmin_name:
                return config
    return None


def _extract_knowledge(text: str, config: dict) -> str:
    """Extract the knowledge/rule from a teach message, stripping addressing prefixes."""
    # Remove addressing like "告訴阿鏈，" "阿鏈記一下，"
    nickname = config.get("nickname", config["name"])
    cleaned = text
    # Strip patterns like "告訴X，..." / "X記一下，..."
    patterns = [
        rf"(?:告訴|跟)\s*{re.escape(nickname)}[，,：:\s]*",
        rf"{re.escape(nickname)}[，,：:\s]*(?:記一下|記住|學一下|注意)[，,：:\s]*",
        rf"(?:教|讓)\s*{re.escape(nickname)}[，,：:\s]*",
    ]
    for pat in patterns:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE).strip()
    # Also try with service name
    name = config["name"]
    if name != nickname:
        for pat_tmpl in [r"(?:告訴|跟)\s*{n}[，,：:\s]*", r"{n}[，,：:\s]*(?:記一下|記住|學一下|注意)[，,：:\s]*"]:
            cleaned = re.sub(pat_tmpl.format(n=re.escape(name)), "", cleaned, flags=re.IGNORECASE).strip()
    # Strip leading punctuation
    cleaned = cleaned.lstrip("，,：:、 ")
    return cleaned if cleaned else None


def format_monitor_proposal(config: dict) -> str:
    """Format a monitor config as a human-readable proposal."""
    nickname = config.get("nickname", config["name"])
    display_name = f"{nickname} ({config['name']})" if nickname != config["name"] else config["name"]
    checks_str = "、".join(config["checks"]) or "（無）"
    lines = [
        f"📊 **{display_name}** 監控設定：",
        f"  檢查項目：{checks_str}",
        f"  檢查頻率：每 {config['check_interval'] // 60} 分鐘",
        f"  狀態回報：每 {config['summary_interval'] // 3600} 小時",
    ]
    if config.get("log_path"):
        lines.append(f"  Log 路徑：`{config['log_path']}`")
    if config.get("prometheus_url"):
        lines.append(f"  Prometheus：`{config['prometheus_url']}`")
    if config.get("check_commands"):
        lines.append(f"  檢查方式：{config['check_commands']}")
    # Show if this is a flexible (Claude-powered) monitor
    if config.get("check_instruction") and not detect_project(config.get("check_instruction", "")):
        lines.append(f"  模式：🤖 Claude 自動收集（支援 SSH、curl 等）")
    knowledge = config.get("knowledge", [])
    if knowledge:
        lines.append(f"  已累積知識：{len(knowledge)} 條")
    lines.append("")
    lines.append("確認開始嗎？或是要調整什麼？")
    return "\n".join(lines)


# ── Help Text ────────────────────────────────────────────────────────────────

HELP_TEXT = """**Twix Bot 指令說明**

**基本用法：**
`!claude <問題>` 或 `@bot <問題>`

**開 Thread 聊天（獨立上下文）：**
`!thread <主題>` — 開一個 thread，裡面直接打字就能對話

**專案操作：**
直接在訊息中提到專案名稱，Bot 會自動切換到對應目錄

**監控：**
`!monitor <描述>` — 建立監控（任意目標，不限本地專案）
`!monitor` — 列出所有監控
`@bot 監控 erigon 叫他阿鏈` — 用自然語言開始監控
`列出監控` / `停止監控 erigon` — 管理監控

**教 Monitor：**
`告訴阿鏈，peer disconnected 偶爾出現是正常的` — 累積知識
`阿鏈記一下，重啟後 CPU 會飆高 10 分鐘` — 教它判斷規則
在 incident thread 說「誤報」或「這不是問題」— 自動學習

異常時自動開 thread → 說「修吧」開始修復 → 說「跑看看」重啟服務 → 正常後自動關閉

**平行任務（Worktree）：**
`@bot 在 erigon 開 branch fix/issue-42 修復 memory leak`
或 `!task erigon fix/issue-42 修復 memory leak`
完成後說「做完了」或 `!done`
`有哪些 task` — 列出進行中的 tasks

**Sprint Plan（大型任務）：**
`!plan <任務描述>` — 讓 Planner 分析 codebase 並產出執行計畫
計畫確認後自動分步執行，每步都有 Evaluator 驗收

**Super Manager：**
`!manager` — 查看所有 thread 的狀態總覽
`!manager <問題>` — 跨 thread 提問（例如：「erigon 修好了嗎？」「目前什麼最緊急？」）

**手動指令：**
`!repo` / `!repo <名稱>` — 查看/切換專案
`!projects` — 列出所有專案
`!reset` — 重置對話上下文
"""


_STALE_PATTERNS = ("⏳ 處理中...", "⏳ 分析中...", "⏳ 正在建立", "⏳ 正在整理", "🔍 分析監控", "🔍 Codex", "🤖 Codex", "📋 Planner", "🔧 調整中", "🧠 Manager")

async def _cleanup_stale_status_messages():
    """Update any ⏳ 處理中... messages left hanging from the previous bot session."""
    await asyncio.sleep(2)  # let Discord connection settle
    checked = set()
    all_channel_ids = list(channel_session.keys()) + [m.get("channel_id") for m in monitor_configs.values() if m.get("channel_id")]
    for channel_id in all_channel_ids:
        if channel_id in checked:
            continue
        checked.add(channel_id)
        try:
            ch = client.get_channel(channel_id)
            if not ch:
                continue
            async for msg in ch.history(limit=15):
                if msg.author != client.user:
                    continue
                if any(msg.content.startswith(p) for p in _STALE_PATTERNS):
                    try:
                        await msg.edit(content="⚠️ Bot 重啟，上一個操作被中斷。請重新發送指令。")
                    except Exception:
                        pass
                    break  # only fix the most recent stale message per channel
        except Exception as e:
            print(f"[STARTUP] cleanup channel {channel_id}: {e}", flush=True)


# ── Events ───────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    # Register persistent views so buttons survive restarts
    client.add_view(ReviewView())
    client.add_view(SuggestionView())
    client.add_view(PlanView())
    client.add_view(TodoDispatchView())
    print(f"Bot is online as {client.user}", flush=True)
    for guild in client.guilds:
        print(f"  Connected to server: {guild.name} (id: {guild.id})", flush=True)
    projects = list_projects()
    print(f"  Projects in {WORK_ROOT}: {projects or '(empty)'}", flush=True)

    # Restart enabled monitors
    for mid, config in monitor_configs.items():
        if config.get("enabled"):
            ch = client.get_channel(config["channel_id"])
            if ch:
                active_monitor_tasks[mid] = asyncio.create_task(monitor_loop(mid, ch))
                print(f"  Resumed monitor: {config['name']}", flush=True)

    # Re-attach open incident threads
    for inc in active_incidents.values():
        if inc.get("status") != "resolved":
            bot_threads.add(inc["thread_id"])

    # Clean up stale "⏳ 處理中..." messages left by the previous session
    asyncio.create_task(_cleanup_stale_status_messages())


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return
    if message.webhook_id:
        return

    content = message.content.strip()
    print(f"[MSG] {message.author}: {content}", flush=True)

    # ── Static commands ──

    if content == "!help":
        await message.reply(HELP_TEXT)
        return

    # Handle pending monitor setup (user sent !monitor alone, now sending description)
    if message.channel.id in pending_monitor_setup and not content.startswith("!"):
        pending_monitor_setup.discard(message.channel.id)
        thinking = await message.reply("🔍 分析監控需求中...")
        config = await parse_monitor_config(content, message.channel.id)
        if not config:
            await thinking.edit(content="❌ 找不到監控目標。請描述要監控什麼，例如：`!monitor 監控 erigon 的 CPU 和 memory`")
            return
        for existing in monitor_configs.values():
            if existing["name"] == config["name"] and existing.get("enabled"):
                await thinking.edit(content=f"📊 **{config['name']}** 已經在監控中了。")
                return
        pending_monitors[message.channel.id] = config
        await thinking.edit(content=format_monitor_proposal(config))
        return

    if content.startswith("!manager"):
        question = content[8:].strip()
        if not question:
            # No question — just show all summaries
            if not thread_summaries:
                await message.reply("目前沒有任何 thread 摘要。")
                return
            now = time.time()
            lines = ["**各 Thread 狀態總覽：**"]
            for cid_str, info in thread_summaries.items():
                age_h = (now - info.get("updated_at", 0)) / 3600
                topic = info.get("topic", "")
                summary = info.get("summary", "")
                stale = " *(>24h)*" if age_h > 24 else ""
                label = f"**{topic}**：{summary}" if topic else summary
                lines.append(f"• <#{cid_str}> {label} ({age_h:.0f}h ago){stale}")
            await message.reply("\n".join(lines))
            return
        # Has a question — let Claude answer with all thread summaries as context
        all_summaries = []
        now = time.time()
        for cid_str, info in thread_summaries.items():
            age_h = (now - info.get("updated_at", 0)) / 3600
            topic = info.get("topic", "")
            summary = info.get("summary", "")
            label = f"{topic}: {summary}" if topic else summary
            all_summaries.append(f"- <#{cid_str}> {label} ({age_h:.0f}h ago)")
        # Also include active monitors and incidents
        monitor_lines = []
        for mid, config in monitor_configs.items():
            running = mid in active_monitor_tasks and not active_monitor_tasks[mid].done()
            status = "運行中" if running else "已停止"
            inc = find_incident_by_monitor(mid)
            if inc:
                status += f", incident: {inc['status']}"
            monitor_lines.append(f"- {config['name']}: {status}")
        worktree_lines = []
        for tid, info in channel_worktrees.items():
            worktree_lines.append(f"- <#{tid}> branch `{info['branch']}`")

        ctx = "你是一個 Discord server 的總管（Super Manager）。以下是目前所有進行中的狀態：\n\n"
        if all_summaries:
            ctx += "**Thread 摘要：**\n" + "\n".join(all_summaries) + "\n\n"
        else:
            ctx += "目前沒有 thread 摘要。\n\n"
        if monitor_lines:
            ctx += "**監控：**\n" + "\n".join(monitor_lines) + "\n\n"
        if worktree_lines:
            ctx += "**進行中的 Tasks：**\n" + "\n".join(worktree_lines) + "\n\n"
        ctx += f"用戶的問題：{question}"

        thinking = await message.reply("🧠 Manager 思考中...")
        try:
            result, _, _, _, _ = await run_claude(ctx, WORK_ROOT, system_prompt=(
                "你是一個 Discord server 的 Super Manager。你掌握所有 thread 的摘要、監控狀態和進行中的任務。"
                "根據這些資訊回答用戶的問題。簡潔扼要，用繁體中文回答。"
                "如果資訊不足以回答，誠實說明。"
            ))
            chunks = split_message(result)
            await thinking.edit(content=chunks[0])
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
        except Exception as e:
            await thinking.edit(content=f"❌ Error: {e}")
        return

    if content.startswith("!monitor"):
        args = content[8:].strip()
        if args == "list":
            # Explicit list command
            pass  # fall through to list below
        elif args:
            # Has description — create monitor
            thinking = await message.reply("🔍 分析監控需求中...")
            try:
                config = await parse_monitor_config(args, message.channel.id)
                if not config:
                    await thinking.edit(content="❌ 請描述要監控什麼，例如：`!monitor erigon 的 CPU 和 memory`")
                    return
                for existing in monitor_configs.values():
                    if existing["name"] == config["name"] and existing.get("enabled"):
                        await thinking.edit(content=f"📊 **{config['name']}** 已經在監控中了。")
                        return
                pending_monitors[message.channel.id] = config
                await thinking.edit(content=format_monitor_proposal(config))
            except Exception as e:
                print(f"[MONITOR] parse error: {e}", flush=True)
                import traceback
                traceback.print_exc()
                await thinking.edit(content=f"❌ 分析監控需求時出錯：{e}")
            return
        else:
            # No args — if monitors exist, list them; otherwise prompt for description
            if monitor_configs:
                pass  # fall through to list below
            else:
                # Mark this channel as awaiting monitor description
                pending_monitor_setup.add(message.channel.id)
                await message.reply("📝 請描述你想監控什麼，例如：\n• `監控 hoodi 的 process 狀況`\n• `每 10 分鐘檢查 https://example.com 是否正常`")
                return
        # List monitors
        if not monitor_configs:
            await message.reply("目前沒有設定任何監控。用 `!monitor <描述>` 建立一個。")
        else:
            lines = ["📊 **監控列表：**"]
            for mid, config in monitor_configs.items():
                running = mid in active_monitor_tasks and not active_monitor_tasks[mid].done()
                nickname = config.get("nickname", config["name"])
                display = f"{nickname} ({config['name']})" if nickname != config["name"] else config["name"]
                status = "✅ 運行中" if running else "⏹ 已停止"
                inc = find_incident_by_monitor(mid)
                if inc:
                    status += f" ⚠️ incident: {inc['status']}"
                knowledge_count = len(config.get("knowledge", []))
                knowledge_info = f" / 🧠 {knowledge_count} 條知識" if knowledge_count else ""
                lines.append(
                    f"• **{display}** — {status}\n"
                    f"  檢查：{', '.join(config.get('checks', []))} / "
                    f"每 {config.get('check_interval', 300) // 60} 分鐘{knowledge_info}"
                )
            await message.reply("\n".join(lines))
        return

    if content == "!projects":
        projects = list_projects()
        if projects:
            proj_list = "\n".join(f"• `{p}`" for p in projects)
            await message.reply(f"**{WORK_ROOT} 底下的專案：**\n{proj_list}")
        else:
            await message.reply(f"`{WORK_ROOT}` 底下還沒有專案目錄")
        return

    if content.startswith("!repo"):
        args = content[5:].strip()
        if not args:
            cwd = channel_workdir.get(message.channel.id, WORK_ROOT)
            await message.reply(f"目前工作目錄：`{cwd}`")
            return
        if os.path.isdir(args):
            path = args
        elif os.path.isdir(os.path.join(WORK_ROOT, args)):
            path = os.path.join(WORK_ROOT, args)
        else:
            await message.reply(f"❌ 找不到專案：`{args}`")
            return
        channel_workdir[message.channel.id] = path
        channel_session[message.channel.id] = None
        _save_state()
        await message.reply(f"✅ 工作目錄：`{path}`")
        return

    if content.startswith("!codex"):
        args = content[6:].strip()
        cwd = channel_workdir.get(message.channel.id, WORK_ROOT)
        pikmin = _get_pikmin(message.channel.id)

        if args == "review" or args.startswith("review "):
            # !codex review [--base branch] [free-form instructions] — review git changes
            # Codex CLI: scope flags (--uncommitted/--base) are mutually exclusive with [PROMPT].
            # When user provides instructions, embed scope into prompt text instead.
            thinking = await (pikmin_send(message.channel, "🔍 Codex reviewing...", pikmin) if pikmin else message.reply("🔍 Codex reviewing..."))
            review_args = args[6:].strip()
            cmd = [CODEX_BIN, "review"]
            base_branch = None
            instructions = ""
            if review_args:
                base_m = re.search(r"--base\s+(\S+)", review_args)
                if base_m:
                    base_branch = base_m.group(1)
                    review_args = (review_args[:base_m.start()] + review_args[base_m.end():]).strip()
                instructions = review_args
            if instructions:
                scope_hint = (
                    f"Review the changes against base branch '{base_branch}'."
                    if base_branch else "Review the uncommitted changes."
                )
                cmd.append(f"{scope_hint} {instructions}")
            elif base_branch:
                cmd.extend(["--base", base_branch])
            else:
                cmd.append("--uncommitted")
            env = os.environ.copy()
            if OPENAI_API_KEY:
                env["OPENAI_API_KEY"] = OPENAI_API_KEY
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=EVALUATOR_TIMEOUT)
                # codex review outputs to stderr, not stdout
                result = stdout.decode("utf-8", errors="replace").strip()
                if not result:
                    result = stderr.decode("utf-8", errors="replace").strip()
                if not result:
                    result = "（Codex review 沒有輸出）"
                # Filter out codex CLI boilerplate (header lines before actual review)
                lines = result.splitlines()
                review_start = 0
                for i, line in enumerate(lines):
                    if line.startswith("codex") or line.startswith("##") or line.startswith("**"):
                        review_start = i
                        break
                if review_start > 0:
                    result = "\n".join(lines[review_start:])
            except asyncio.TimeoutError:
                result = f"⏰ Codex review 超時（{EVALUATOR_TIMEOUT // 60} 分鐘）"
            except Exception as e:
                result = f"❌ Codex review 失敗: {e}"
            chunks = split_message(result)
            if pikmin:
                try:
                    await pikmin_edit(thinking, chunks[0], message.channel)
                except Exception:
                    await pikmin_send(message.channel, chunks[0], pikmin)
                for chunk in chunks[1:]:
                    await pikmin_send(message.channel, chunk, pikmin)
            else:
                await thinking.edit(content=chunks[0])
                for chunk in chunks[1:]:
                    await message.channel.send(chunk)
        elif args:
            # !codex <prompt> — ask Codex anything, with thread context
            thinking = await (pikmin_send(message.channel, "🤖 Codex 處理中...", pikmin) if pikmin else message.reply("🤖 Codex 處理中..."))
            # Build context from thread history
            try:
                thread_hist = await fetch_thread_history(message.channel, limit=15, max_chars=6000)
            except Exception:
                thread_hist = ""
            if thread_hist:
                full_prompt = f"## 對話上下文\n{thread_hist}\n\n---\n## 用戶問題\n{args}"
            else:
                full_prompt = args
            result, ok = await _run_codex_exec(full_prompt, cwd, model=CODEX_MODEL)
            if not ok or not result:
                if CODEX_FALLBACK_MODEL and CODEX_FALLBACK_MODEL != CODEX_MODEL:
                    result, ok = await _run_codex_exec(full_prompt, cwd, model=CODEX_FALLBACK_MODEL)
            if not result:
                result = "（Codex 沒有輸出）"
            chunks = split_message(result)
            if pikmin:
                try:
                    await pikmin_edit(thinking, chunks[0], message.channel)
                except Exception:
                    await pikmin_send(message.channel, chunks[0], pikmin)
                for chunk in chunks[1:]:
                    await pikmin_send(message.channel, chunk, pikmin)
            else:
                await thinking.edit(content=chunks[0])
                for chunk in chunks[1:]:
                    await message.channel.send(chunk)
        else:
            await message.reply(
                "**Codex 指令：**\n"
                "• `!codex review` — review 目前未 commit 的改動\n"
                "• `!codex review --base main` — review 跟 main 的差異\n"
                "• `!codex <問題>` — 直接問 Codex"
            )
        return

    if content == "!ctx":
        ch_id = message.channel.id
        turns = channel_cumulative_turns.get(ch_id, 0)
        usage = channel_last_usage.get(ch_id, {})
        session = channel_session.get(ch_id)
        input_tokens = usage.get("input_tokens", 0)
        pct = (input_tokens / CONTEXT_WINDOW_TOKENS * 100) if input_tokens else 0
        bar_len = 20
        filled = int(pct / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        lines = [
            f"**Context 狀態**",
            f"Session: `{session[:12]}...`" if session else "Session: *(none)*",
            f"累計 turns: **{turns}**",
            f"Input tokens: **{input_tokens:,}** / {CONTEXT_WINDOW_TOKENS:,}",
            f"使用率: `[{bar}]` **{pct:.1f}%**",
        ]
        if pct > 80:
            lines.append("⚠️ Context 快滿了，建議 `!relay` 或 `!reset`")
        await message.reply("\n".join(lines))
        return

    if content == "!relay":
        ch_id = message.channel.id
        cwd = channel_workdir.get(ch_id, WORK_ROOT)
        total = channel_cumulative_turns.get(ch_id, 0)
        session_id = channel_session.get(ch_id)
        if not session_id:
            await message.reply("⚠️ 目前沒有 session，不需要 relay。")
            return
        thinking = await message.reply("🔄 正在整理上下文...")
        relayed = await maybe_context_relay(ch_id, 0, message.channel, cwd, force=True)
        if relayed:
            await thinking.edit(content=f"🔄 **Context Relay 完成** — 累計 {total} 輪，已整理上下文並開新 session。下一條訊息會帶摘要 + git 狀態。")
        else:
            await thinking.edit(content="⚠️ Relay 失敗，可能無法取得 context。")
        return

    if content == "!reset":
        ch_id = message.channel.id
        channel_session[ch_id] = None
        channel_cumulative_turns.pop(ch_id, None)
        channel_relay_summaries.pop(ch_id, None)
        _save_state()
        await message.reply("✅ 對話已重置（含歷史摘要）")
        return

    if content.startswith("!account"):
        global _account_mode
        arg = content[8:].strip().lower()
        if arg == "primary":
            _account_mode = "primary"
            await message.reply("✅ 已切換回主帳號")
        elif arg == "backup":
            if not CLAUDE_BACKUP_HOME:
                await message.reply("❌ 尚未設定 `CLAUDE_BACKUP_HOME` 環境變數")
            else:
                _account_mode = "backup"
                await message.reply(f"✅ 已切換至備用帳號（HOME={CLAUDE_BACKUP_HOME}）")
        else:
            status = f"目前使用：**{_account_mode}** 帳號"
            if CLAUDE_BACKUP_HOME:
                status += f"\n備用帳號 HOME：`{CLAUDE_BACKUP_HOME}`"
            else:
                status += "\n⚠️ `CLAUDE_BACKUP_HOME` 未設定（無備用帳號）"
            await message.reply(status)
        return

    if content.startswith("!thread"):
        topic = content[7:].strip() or "Claude 對話"
        try:
            thread = await message.create_thread(name=topic)
            bot_threads.add(thread.id)
            channel_workdir[thread.id] = channel_workdir.get(message.channel.id, WORK_ROOT)
            pidx = _assign_pikmin(thread.id)
            pikmin_assignments[thread.id] = pidx
            pikmin = PIKMIN_POOL[pidx]
            _save_state()
            await pikmin_send(thread, f"🧵 Thread 已建立！我是 **{pikmin['name']}**，由我來負責。\n工作目錄：`{channel_workdir[thread.id]}`", pikmin)
        except Exception as e:
            await message.reply(f"❌ 無法建立 thread: {e}")
        return

    if content == "!done":
        content = "!task done"

    # ── !plan command ──
    if content.startswith("!plan"):
        task_desc = content[5:].strip()
        # If no description provided but replying to a bot message, use that as the task
        if not task_desc and message.reference:
            try:
                ref_msg = message.reference.resolved or await message.channel.fetch_message(message.reference.message_id)
                if ref_msg and ref_msg.content:
                    task_desc = ref_msg.content[:4000]
                    print(f"[PLANNER] using replied message as task (len={len(task_desc)})", flush=True)
            except Exception as e:
                print(f"[PLANNER] failed to fetch referenced message: {e}", flush=True)
        if not task_desc:
            await message.reply("用法：`!plan <任務描述>`\n例如：`!plan 為 ePBS 加上 builder bid 驗證機制`\n或回覆 Bot 訊息並輸入 `!plan`")
            return

        cwd = channel_workdir.get(message.channel.id, WORK_ROOT)
        thinking = await message.reply("📋 Planner 分析中...")

        try:
            plan = await generate_plan(task_desc, cwd, channel=message.channel)
            if not plan or not plan.get("steps"):
                await thinking.edit(content="❌ Planner 無法產生計畫，請提供更具體的任務描述。")
                return

            display = format_plan_display(plan)
            pidx = pikmin_assignments.get(message.channel.id, 0)
            view = PlanView(plan, task_desc, cwd, message.channel, pidx)

            chunks = split_message(display)
            await thinking.edit(content=chunks[0])
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
            plan_msg = await message.channel.send("請確認計畫：", view=view)
            view.store(plan_msg.id)
        except Exception as e:
            print(f"[PLANNER] ERROR: {e}", flush=True)
            await thinking.edit(content=f"❌ 計畫產生失敗：{e}")
        return

    # ── Plan modification (pending_plans) ──
    if message.channel.id in pending_plans:
        pending = pending_plans.pop(message.channel.id)
        modification = content
        cwd = pending["cwd"]
        thinking = await message.reply("📋 修改計畫中...")

        try:
            modify_prompt = (
                f"以下是原始計畫（JSON）：\n```json\n{json.dumps(pending['plan'], ensure_ascii=False, indent=2)}\n```\n\n"
                f"用戶要求修改：{modification}\n\n"
                f"請根據修改要求輸出新的完整 sprint contract JSON。格式跟原本一樣。只輸出 JSON，不要加任何說明文字。"
            )
            cmd = [
                CLAUDE_BIN, "-p", modify_prompt,
                "--model", PLANNER_MODEL,
                "--system-prompt", PLANNER_PROMPT,
                "--output-format", "stream-json",
                "--verbose",
                "--max-turns", str(MAX_TURNS),
            ]
            result, _, _, _, _, _ = await _run_claude_stream(cmd, cwd)
            new_plan = None
            if result:
                new_plan = _extract_plan_json(result)
            # Retry once if parse failed but got a result
            if not new_plan and result:
                print(f"[PLANNER] modify: first attempt parse failed, retrying. Result preview: {result[:200]}", flush=True)
                retry_prompt = (
                    f"你剛才的回覆無法解析為 JSON。請只輸出 JSON，不要加任何 markdown 或說明。\n\n"
                    f"原始計畫：\n{json.dumps(pending['plan'], ensure_ascii=False)}\n\n"
                    f"修改要求：{modification}"
                )
                cmd_retry = [
                    CLAUDE_BIN, "-p", retry_prompt,
                    "--model", PLANNER_MODEL,
                    "--system-prompt", "Output ONLY valid JSON. No markdown, no explanation.",
                    "--output-format", "stream-json",
                    "--verbose",
                    "--max-turns", "3",
                ]
                result2, _, _, _, _, _ = await _run_claude_stream(cmd_retry, cwd)
                if result2:
                    new_plan = _extract_plan_json(result2)
            if new_plan:
                display = format_plan_display(new_plan)
                view = PlanView(new_plan, pending["task"], cwd, message.channel, pending["pikmin_idx"])
                chunks = split_message(display)
                await thinking.edit(content=chunks[0])
                for chunk in chunks[1:]:
                    await message.channel.send(chunk)
                plan_msg = await message.channel.send("請確認修改後的計畫：", view=view)
                view.store(plan_msg.id)
            elif not result:
                print("[PLANNER] modify: no result from claude", flush=True)
                await thinking.edit(content="❌ 修改失敗 — Planner 沒有回應。")
            else:
                print(f"[PLANNER] modify: JSON parse failed. Result: {result[:500]}", flush=True)
                await thinking.edit(content="❌ 無法解析修改後的計畫 — Planner 回覆格式不正確。")
        except Exception as e:
            print(f"[PLANNER] modify exception: {e}", flush=True)
            await thinking.edit(content=f"❌ 修改失敗：{e}")
        return

    if content.startswith("!task"):
        args = content[5:].strip()
        if args == "done":
            wt_info = channel_worktrees.get(message.channel.id)
            if not wt_info:
                await message.reply("❌ 這個 thread 沒有關聯的 worktree。")
                return
            try:
                await remove_worktree(wt_info["repo"], wt_info["worktree"])
                branch = wt_info["branch"]
                del channel_worktrees[message.channel.id]
                # Reset cwd to main repo so future messages don't use deleted path
                channel_workdir[message.channel.id] = wt_info["repo"]
                _save_state()
                await message.reply(f"✅ Worktree 已清理。Branch `{branch}` 保留在 repo 中，可以用來開 PR。")
            except Exception as e:
                await message.reply(f"❌ 清理 worktree 失敗：{e}")
            return

        if args in ("list", "ls", ""):
            if not channel_worktrees:
                await message.reply("目前沒有進行中的 task。")
                return
            lines = ["**進行中的 Tasks：**"]
            for tid, info in channel_worktrees.items():
                lines.append(f"• <#{tid}> — `{info['branch']}` (`{info['worktree']}`)")
            await message.reply("\n".join(lines))
            return

        parts = args.split(None, 2)
        if len(parts) < 2:
            await message.reply("用法：`!task <專案> <branch名稱> [描述]`\n例如：`!task erigon fix/issue-42 修復 memory leak`")
            return

        project_name, branch_name = parts[0], parts[1]
        description = parts[2] if len(parts) > 2 else branch_name

        if os.path.isdir(project_name):
            repo_path = project_name
        elif os.path.isdir(os.path.join(WORK_ROOT, project_name)):
            repo_path = os.path.join(WORK_ROOT, project_name)
        else:
            await message.reply(f"❌ 找不到專案：`{project_name}`")
            return

        status = await message.reply("⏳ 正在建立 worktree...")
        try:
            worktree_path = await create_worktree(repo_path, branch_name)
        except Exception as e:
            await status.edit(content=f"❌ 建立 worktree 失敗：{e}")
            return
        try:
            thread_name = f"🔧 {description[:90]}"
            thread = await message.create_thread(name=thread_name)
            bot_threads.add(thread.id)
            channel_workdir[thread.id] = worktree_path
            channel_worktrees[thread.id] = {
                "repo": repo_path, "worktree": worktree_path, "branch": branch_name,
            }
            pidx = _assign_pikmin(thread.id)
            pikmin_assignments[thread.id] = pidx
            pikmin = PIKMIN_POOL[pidx]
            _save_state()
            await status.edit(content=f"✅ Task 已建立！由 **{pikmin['name']}** 負責\n📂 Worktree: `{worktree_path}`\n🌿 Branch: `{branch_name}`")
            await pikmin_send(thread,
                f"🔧 **Task: {description}**\n📂 工作目錄：`{worktree_path}`\n🌿 Branch：`{branch_name}`\n\n"
                f"我是 **{pikmin['name']}**，直接在這裡打字開始工作。完成後說「做完了」或 `!done`。",
                pikmin)
        except Exception as e:
            await remove_worktree(repo_path, worktree_path)
            await status.edit(content=f"❌ 建立 thread 失敗：{e}")
        return

    # ── NLP: classify intent via Claude ──

    prompt = parse_prompt(content, client.user.id)
    reply_monitor_config = None
    if prompt is None:
        if message.reference and message.reference.message_id:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                print(f"[REPLY-DETECT] ref author={ref_msg.author.display_name}, webhook_id={ref_msg.webhook_id}, is_bot_user={ref_msg.author == client.user}", flush=True)
                if ref_msg.author == client.user:
                    prompt = content
                elif ref_msg.webhook_id:
                    found = _find_monitor_by_pikmin_name(ref_msg.author.display_name)
                    print(f"[REPLY-DETECT] pikmin lookup '{ref_msg.author.display_name}' -> {found.get('name') if found else None}", flush=True)
                    if found:
                        reply_monitor_config = found
                        prompt = content
            except Exception as e:
                print(f"[REPLY-DETECT] error: {e}", flush=True)
    if prompt is None and isinstance(message.channel, discord.Thread) and message.channel.id in bot_threads:
        prompt = content
    if prompt is None and message.channel.id in pending_monitors:
        prompt = content
    if prompt is None:
        return
    if not prompt:
        await message.reply("請在指令後輸入內容，例如：`!claude 你好`")
        return

    # Classify intent via Claude Haiku (fast + accurate)
    incident = find_incident_by_thread(message.channel.id)
    ctx_parts = []
    if incident:
        ctx_parts.append(f"In incident thread, status={incident['status']}")
    if message.channel.id in channel_worktrees:
        ctx_parts.append("In worktree task thread")
    if isinstance(message.channel, discord.Thread):
        ctx_parts.append("In a thread")
    if message.channel.id in pending_monitors:
        ctx_parts.append("Pending monitor setup awaiting confirmation")
    if reply_monitor_config:
        rn = reply_monitor_config.get("nickname", reply_monitor_config["name"])
        ctx_parts.append(f"User is replying to monitor '{rn}' webhook message")
    if monitor_configs:
        nicknames = [f"{c.get('nickname', c['name'])}({c['name']})" for c in monitor_configs.values() if c.get("enabled")]
        if nicknames:
            ctx_parts.append(f"Active monitors: {', '.join(nicknames)}")
    # Add recent conversation context so classifier knows what's being discussed
    try:
        recent_msgs = []
        async for msg in message.channel.history(limit=4, before=message):
            role = "bot" if msg.author == client.user or msg.webhook_id else "user"
            recent_msgs.append(f"{role}: {msg.content[:80]}")
        if recent_msgs:
            recent_msgs.reverse()
            ctx_parts.append(f"Recent conversation: {' | '.join(recent_msgs)}")
    except Exception:
        pass
    context = "; ".join(ctx_parts)

    # Determine which intents are valid in this context
    thread_scoped = incident or message.channel.id in channel_worktrees
    if thread_scoped:
        # In incident/worktree threads, only allow thread-relevant intents
        # Worktree threads also allow coding/codex_review (they exist for coding work)
        allowed_intents = {"monitor_dismiss", "task_done", "chat"}
        if incident:
            allowed_intents.add("monitor_teach")
        if message.channel.id in channel_worktrees:
            allowed_intents.update({"coding", "codex_review"})
        intent = await classify_intent(prompt, context)
        if intent not in allowed_intents:
            intent = "chat"
    else:
        intent = await classify_intent(prompt, context)

    # ── Handle intents ──

    if intent == "fix" and incident and incident["status"] == "open":
        await handle_fix_request(message, incident)
        return

    if intent == "restart" and incident and incident["status"] == "fixing":
        await handle_restart_request(message, incident)
        return

    if intent == "monitor_dismiss" and incident:
        config = monitor_configs.get(incident.get("monitor_id"))
        if config:
            # Learn from dismissal — record what was a false alarm
            summary = incident.get("summary", "")
            knowledge_text = f"誤報經驗：「{summary}」被使用者判定為正常，不需要告警"
            if prompt.strip() not in ("誤報", "沒事", "正常的", "false alarm"):
                # User gave a reason — use it
                knowledge_text = f"誤報經驗：「{summary}」— 使用者說：{prompt}"
            if "knowledge" not in config:
                config["knowledge"] = []
            config["knowledge"].append(knowledge_text)
            nickname = config.get("nickname", config["name"])
            await resolve_incident(incident)
            _save_monitors()
            await message.reply(
                f"✅ 已關閉此 incident 並記錄為誤報。\n"
                f"🧠 **{nickname}** 已學到：{knowledge_text}"
            )
        else:
            await resolve_incident(incident)
            await message.reply("✅ 已關閉此 incident。")
        return

    if intent == "monitor_remove":
        # First check if there's a pending (unconfirmed) monitor to cancel
        if message.channel.id in pending_monitors:
            cancelled = pending_monitors.pop(message.channel.id)
            await message.reply(f"✅ 已取消 **{cancelled['name']}** 的監控設定。")
            return
        target = detect_project(prompt)
        matched_config = None
        matched_mid = None
        for mid, config in list(monitor_configs.items()):
            if target and target.lower() not in config["name"].lower():
                continue
            matched_config = config
            matched_mid = mid
            break
        if not matched_config:
            await message.reply("❌ 找不到對應的監控。用「列出監控」查看。")
            _save_monitors()
            return
        # Store pending removal for confirmation
        pending_monitor_removals[message.channel.id] = {
            "mid": matched_mid,
            "config": matched_config,
            "user_id": message.author.id,
        }
        await message.reply(
            f"⚠️ 確定要停止監控 **{matched_config['name']}** 嗎？回覆「確定」或「對」來確認。"
        )
        return

    if intent == "monitor_resume":
        target = detect_project(prompt)
        resumed = False
        for mid, config in monitor_configs.items():
            if config.get("enabled"):
                continue  # Already running
            if target and target.lower() not in config["name"].lower():
                continue
            config["enabled"] = True
            _save_monitors()
            active_monitor_tasks[mid] = asyncio.create_task(
                monitor_loop(mid, message.channel)
            )
            nickname = config.get("nickname", config["name"])
            display = f"{nickname} ({config['name']})" if nickname != config["name"] else config["name"]
            await message.reply(f"✅ **{display}** 已重新啟動！")
            resumed = True
            break
        if not resumed:
            # Check if there are any disabled monitors
            disabled = [c for c in monitor_configs.values() if not c.get("enabled")]
            if disabled:
                names = ", ".join(c["name"] for c in disabled)
                await message.reply(f"❌ 找不到匹配的已停用監控。已停用的有：{names}")
            else:
                await message.reply("❌ 沒有已停用的監控可以恢復。")
        return

    if intent == "monitor_list":
        if not monitor_configs:
            await message.reply("目前沒有設定任何監控。")
        else:
            lines = ["**監控列表：**"]
            for mid, config in monitor_configs.items():
                running = mid in active_monitor_tasks and not active_monitor_tasks[mid].done()
                if not config.get("enabled"):
                    status = "⏸️ 已停用（可用「重啟監控」恢復）"
                elif running:
                    status = "🟢 運行中"
                else:
                    status = "🔴 已停止"
                inc = find_incident_by_monitor(mid)
                if inc:
                    status += f" ⚠️ incident: {inc['status']}"
                nickname = config.get("nickname", config["name"])
                display = f"{nickname} ({config['name']})" if nickname != config["name"] else config["name"]
                knowledge_count = len(config.get("knowledge", []))
                knowledge_info = f" / 🧠 {knowledge_count} 條知識" if knowledge_count else ""
                lines.append(
                    f"• **{display}** — {status}\n"
                    f"  檢查：{', '.join(config.get('checks', []))} / "
                    f"每 {config.get('check_interval', 300) // 60} 分鐘{knowledge_info}"
                )
            await message.reply("\n".join(lines))
        return

    if intent == "monitor_add":
        config = await parse_monitor_config(prompt, message.channel.id)
        if not config:
            await message.reply("❌ 找不到監控目標。例如：「監控 erigon」或用 `!monitor <描述>` 建立")
            return
        for existing in monitor_configs.values():
            if existing["name"] == config["name"] and existing.get("enabled"):
                await message.reply(f"📊 **{config['name']}** 已經在監控中了。")
                return
        # Save as pending, ask for confirmation
        pending_monitors[message.channel.id] = config
        await message.reply(format_monitor_proposal(config))
        return

    if intent == "monitor_adjust" and message.channel.id in pending_monitors:
        config = pending_monitors[message.channel.id]
        thinking = await message.reply("🔧 調整中...")
        config = await adjust_monitor_config(config, prompt)
        pending_monitors[message.channel.id] = config
        await thinking.edit(content=format_monitor_proposal(config))
        return

    # Handle pending monitor removal confirmation
    if message.channel.id in pending_monitor_removals:
        removal = pending_monitor_removals[message.channel.id]
        if intent == "monitor_confirm" or prompt.strip() in ("確定", "對", "是", "好", "yes", "y"):
            pending_monitor_removals.pop(message.channel.id)
            mid = removal["mid"]
            config = removal["config"]
            task = active_monitor_tasks.pop(mid, None)
            if task:
                task.cancel()
            config["enabled"] = False
            _save_monitors()
            await message.reply(f"✅ 已停止監控 **{config['name']}**（config 保留，可用「重啟監控 {config['name']}」恢復）")
            return
        else:
            # User said something else — cancel the pending removal
            pending_monitor_removals.pop(message.channel.id)

    if intent == "monitor_confirm" and message.channel.id in pending_monitors:
        config = pending_monitors.pop(message.channel.id)
        # Assign a pikmin to this monitor
        pidx = _assign_pikmin(message.channel.id, monitor_id=config["id"])
        config["pikmin_index"] = pidx
        monitor_configs[config["id"]] = config
        _save_monitors()
        active_monitor_tasks[config["id"]] = asyncio.create_task(
            monitor_loop(config["id"], message.channel)
        )
        nickname = config.get("nickname", config["name"])
        checks_str = "、".join(config["checks"])
        display = f"{nickname} ({config['name']})" if nickname != config["name"] else config["name"]
        pikmin = PIKMIN_POOL[pidx]
        await pikmin_send(
            message.channel,
            f"✅ **{display}** 已上線！由 **{pikmin['name']}** 負責 🫡\n"
            f"檢查項目：{checks_str}\n"
            f"每 {config['check_interval'] // 60} 分鐘檢查，每 {config['summary_interval'] // 3600} 小時回報",
            pikmin,
        )
        return

    if intent == "monitor_teach":
        target_config = reply_monitor_config or _find_monitor_by_text(prompt)
        if not target_config:
            # If only one monitor, assume that one
            enabled = [c for c in monitor_configs.values() if c.get("enabled")]
            if len(enabled) == 1:
                target_config = enabled[0]
            else:
                await message.reply("❌ 找不到對應的監控。請提到監控名稱或暱稱。")
                return
        knowledge_text = _extract_knowledge(prompt, target_config)
        if not knowledge_text:
            await message.reply("❌ 不確定要記住什麼，請說清楚一點。")
            return
        if "knowledge" not in target_config:
            target_config["knowledge"] = []
        target_config["knowledge"].append(knowledge_text)
        _save_monitors()
        nickname = target_config.get("nickname", target_config["name"])
        count = len(target_config["knowledge"])
        await message.reply(f"✅ **{nickname}** 已記住：{knowledge_text}\n（目前累積 {count} 條知識）")
        return

    if intent == "monitor_move":
        # Parse channel mention: <#123456>
        ch_match = re.search(r"<#(\d+)>", prompt)
        if not ch_match:
            await message.reply("❌ 請 mention 一個 channel，例如：「阿鏈去 #ops 回報」")
            return
        new_channel_id = int(ch_match.group(1))
        new_channel = client.get_channel(new_channel_id)
        if not new_channel:
            await message.reply("❌ 找不到那個 channel，確認 bot 有權限存取。")
            return
        target_config = reply_monitor_config or _find_monitor_by_text(prompt)
        if not target_config:
            enabled = [c for c in monitor_configs.values() if c.get("enabled")]
            if len(enabled) == 1:
                target_config = enabled[0]
            else:
                await message.reply("❌ 找不到對應的監控。請提到監控名稱或暱稱。")
                return
        old_channel_id = target_config["channel_id"]
        target_config["channel_id"] = new_channel_id
        _save_monitors()
        # Restart monitor loop with new channel
        mid = target_config["id"]
        old_task = active_monitor_tasks.pop(mid, None)
        if old_task:
            old_task.cancel()
        active_monitor_tasks[mid] = asyncio.create_task(monitor_loop(mid, new_channel))
        nickname = target_config.get("nickname", target_config["name"])
        await message.reply(f"✅ **{nickname}** 已搬到 <#{new_channel_id}> 回報")
        return

    if intent == "task_done" and message.channel.id in channel_worktrees:
        wt_info = channel_worktrees[message.channel.id]
        try:
            await remove_worktree(wt_info["repo"], wt_info["worktree"])
            branch = wt_info["branch"]
            del channel_worktrees[message.channel.id]
            # Reset cwd to main repo so future messages don't use deleted path
            channel_workdir[message.channel.id] = wt_info["repo"]
            _save_state()
            await message.reply(f"✅ Worktree 已清理。Branch `{branch}` 保留在 repo 中，可以用來開 PR。")
        except Exception as e:
            await message.reply(f"❌ 清理 worktree 失敗：{e}")
        return

    if intent == "task_list":
        if not channel_worktrees:
            await message.reply("目前沒有進行中的 task。")
        else:
            lines = ["**進行中的 Tasks：**"]
            for tid, info in channel_worktrees.items():
                lines.append(f"• <#{tid}> — `{info['branch']}` (`{info['worktree']}`)")
            await message.reply("\n".join(lines))
        return

    if intent == "branch_task" and not isinstance(message.channel, discord.Thread):
        project, branch, description = parse_branch_task(prompt)
        if not project:
            await message.reply("❌ 找不到專案名稱，例如：「在 erigon 開 branch fix/issue-42」")
            return
        if not branch:
            await message.reply("❌ 找不到 branch 名稱，請用 `xxx/yyy` 格式")
            return
        repo_path = os.path.join(WORK_ROOT, project)
        status = await message.reply("⏳ 正在建立 worktree...")
        try:
            worktree_path = await create_worktree(repo_path, branch)
        except Exception as e:
            await status.edit(content=f"❌ 建立 worktree 失敗：{e}")
            return
        try:
            thread_name = f"🔧 {description[:90]}"
            thread = await message.create_thread(name=thread_name)
            bot_threads.add(thread.id)
            channel_workdir[thread.id] = worktree_path
            channel_worktrees[thread.id] = {
                "repo": repo_path, "worktree": worktree_path, "branch": branch,
            }
            pidx = _assign_pikmin(thread.id)
            pikmin_assignments[thread.id] = pidx
            pikmin = PIKMIN_POOL[pidx]
            _save_state()
            await status.edit(content=f"✅ Task 已建立！由 **{pikmin['name']}** 負責\n📂 Worktree: `{worktree_path}`\n🌿 Branch: `{branch}`")
            await pikmin_send(thread,
                f"🔧 **Task: {description}**\n📂 工作目錄：`{worktree_path}`\n🌿 Branch：`{branch}`\n\n"
                f"我是 **{pikmin['name']}**，直接在這裡打字開始工作。完成後說「做完了」或 `!done`。",
                pikmin)
        except Exception as e:
            await remove_worktree(repo_path, worktree_path)
            await status.edit(content=f"❌ 建立 thread 失敗：{e}")
        return

    if intent == "create_thread" and not isinstance(message.channel, discord.Thread):
        topic = prompt[:50] if len(prompt) > 5 else "Claude 對話"
        try:
            thread = await message.create_thread(name=topic)
            bot_threads.add(thread.id)
            channel_workdir[thread.id] = channel_workdir.get(message.channel.id, WORK_ROOT)
            pidx = _assign_pikmin(thread.id)
            pikmin_assignments[thread.id] = pidx
            pikmin = PIKMIN_POOL[pidx]
            _save_state()
            await pikmin_send(thread, f"🧵 Thread 已建立！我是 **{pikmin['name']}**，由我來負責。\n工作目錄：`{channel_workdir[thread.id]}`", pikmin)
        except Exception as e:
            await message.reply(f"❌ 無法建立 thread: {e}")
        return

    if intent == "codex_review":
        cwd = channel_workdir.get(message.channel.id, WORK_ROOT)
        pikmin = _get_pikmin(message.channel.id)
        # Extract --base <branch> if mentioned in prompt
        base_match = re.search(r"--base\s+(\S+)|base\s+(\S+)|跟\s*(\S+)\s*比|compare.*?(\S+)", prompt)
        base_branch = None
        if base_match:
            base_branch = next(g for g in base_match.groups() if g)
        # Strip common trigger phrases to get the meaningful instruction
        review_instruction = re.sub(
            r"(codex\s*)?(幫我\s*)?review\s*(一下|看看|這次改動|這個|這裡|吧|下)?|"
            r"看看有沒有問題|幫我看看|--base\s+\S+|base\s+\S+|跟\s*\S+\s*比",
            "", prompt, flags=re.IGNORECASE
        ).strip()
        # Codex CLI: scope flags can't be combined with PROMPT — embed scope into prompt text.
        cmd = [CODEX_BIN, "review"]
        if review_instruction:
            scope_hint = (
                f"Review the changes against base branch '{base_branch}'."
                if base_branch else "Review the uncommitted changes."
            )
            cmd.append(f"{scope_hint} {review_instruction}")
        elif base_branch:
            cmd.extend(["--base", base_branch])
        else:
            cmd.append("--uncommitted")
        thinking = await (pikmin_send(message.channel, "🔍 Codex reviewing...", pikmin) if pikmin else message.reply("🔍 Codex reviewing..."))
        env = os.environ.copy()
        if OPENAI_API_KEY:
            env["OPENAI_API_KEY"] = OPENAI_API_KEY
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=EVALUATOR_TIMEOUT)
            result = stdout.decode("utf-8", errors="replace").strip()
            if not result:
                result = stderr.decode("utf-8", errors="replace").strip()
            if not result:
                result = "（Codex review 沒有輸出）"
            lines = result.splitlines()
            for i, line in enumerate(lines):
                if line.startswith("codex") or line.startswith("##") or line.startswith("**"):
                    result = "\n".join(lines[i:])
                    break
        except asyncio.TimeoutError:
            result = f"⏰ Codex review 超時（{EVALUATOR_TIMEOUT // 60} 分鐘）"
        except Exception as e:
            result = f"❌ Codex review 失敗: {e}"
        chunks = split_message(result)
        if pikmin:
            try:
                await pikmin_edit(thinking, chunks[0], message.channel)
            except Exception:
                await pikmin_send(message.channel, chunks[0], pikmin)
            for chunk in chunks[1:]:
                await pikmin_send(message.channel, chunk, pikmin)
        else:
            await thinking.edit(content=chunks[0])
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
        return

    if intent == "coding":
        detected = detect_project(prompt)
        if detected:
            new_cwd = os.path.join(WORK_ROOT, detected)
            old_cwd = channel_workdir.get(message.channel.id)
            if old_cwd != new_cwd:
                channel_workdir[message.channel.id] = new_cwd
                if old_cwd is not None:
                    channel_session[message.channel.id] = None
                _save_state()

        lock = _get_channel_lock(message.channel.id)
        if lock.locked():
            await message.reply("⏳ 上一個請求還在處理中，請稍後再試。")
            return

        async with lock:
            cwd = channel_workdir.get(message.channel.id, WORK_ROOT)
            session_id = channel_session.get(message.channel.id)
            pikmin = _get_pikmin(message.channel.id)

            # Build enriched prompt with thread/relay context
            enriched_prompt = prompt
            if message.reference and message.reference.message_id:
                try:
                    ref_msg = message.reference.resolved or await message.channel.fetch_message(message.reference.message_id)
                    if ref_msg and ref_msg.content:
                        enriched_prompt = f"[用戶引用的訊息]\n{ref_msg.content[:3000]}\n\n[用戶的回覆]\n{prompt}"
                except Exception:
                    pass
            # Manager always runs in a fresh session, so it needs thread history
            # injected regardless of whether the channel has an existing session.
            ctx_parts = []
            if isinstance(message.channel, discord.Thread):
                try:
                    thread_hist = await fetch_thread_history(message.channel, limit=30, max_chars=12000)
                    if thread_hist:
                        ctx_parts.append(f"## 這個 thread 之前的對話\n{thread_hist}")
                except Exception:
                    pass
            relay_ctx = build_relay_context(message.channel.id)
            if relay_ctx:
                ctx_parts.append(relay_ctx)
            if ctx_parts:
                enriched_prompt = "\n\n---\n".join(ctx_parts) + f"\n\n---\n{prompt}"

            if pikmin:
                thinking = await pikmin_send(message.channel, "🤖 Manager 分析任務中...", pikmin)
            else:
                thinking = await message.reply("🤖 Manager 分析任務中...")

            try:
                # Step 1: Claude Manager assesses complexity and produces spec
                manager_result, _, _, _, _ = await run_claude(
                    enriched_prompt, cwd, None,
                    status_msg=thinking,
                    system_prompt=CODING_MANAGER_SYSTEM_PROMPT,
                    max_turns=3,
                )
                print(f"[CODING] manager output: {manager_result[:200]}", flush=True)

                # Parse JSON decision; fall back to complex routing on failure
                complexity = "complex"
                spec = manager_result
                plan_branch = ""
                plan_base = ""
                try:
                    raw = manager_result.strip()
                    # Strip markdown code fences if present
                    if raw.startswith("```"):
                        raw = re.sub(r"^```[^\n]*\n?", "", raw)
                        raw = re.sub(r"\n?```$", "", raw.strip())
                    decision = json.loads(raw)
                    complexity = decision.get("complexity", "complex")
                    spec = decision.get("spec", manager_result)
                    plan_branch = decision.get("branch", "") or ""
                    plan_base = decision.get("base_branch", "") or ""
                except (json.JSONDecodeError, ValueError, AttributeError):
                    print(f"[CODING] manager JSON parse failed, defaulting to complex", flush=True)

                print(f"[CODING] complexity={complexity}, branch={plan_branch!r}, base={plan_base!r}, spec_len={len(spec)}", flush=True)

                if complexity == "simple":
                    # Simple task: Claude handles directly
                    status_label = "⚡ Claude 執行中..."
                    if pikmin:
                        try:
                            await pikmin_edit(thinking, status_label, message.channel)
                        except Exception:
                            pass
                    else:
                        try:
                            await thinking.edit(content=status_label)
                        except Exception:
                            pass

                    exec_result, exec_session_id, exec_tools, exec_turns, exec_usage = await run_claude(
                        spec, cwd, None,
                        status_msg=thinking,
                        channel=message.channel,
                    )
                    if exec_session_id:
                        channel_session[message.channel.id] = exec_session_id
                        _save_state()
                    gen_result = exec_result
                else:
                    # Complex task: show plan first, then Codex executes
                    plan_header = "📋 **Manager Plan**"
                    branch_line = ""
                    if plan_branch and plan_base:
                        branch_line = f"🌿 Branch: `{plan_branch}`（從 `{plan_base}` 切出）\n"
                    elif plan_branch:
                        branch_line = f"🌿 Branch: `{plan_branch}`\n"
                    plan_display = f"{plan_header}\n{branch_line}\n{spec}"
                    plan_chunks = split_message(plan_display)
                    if pikmin:
                        try:
                            await pikmin_edit(thinking, plan_chunks[0], message.channel)
                        except Exception:
                            await pikmin_send(message.channel, plan_chunks[0], pikmin)
                        for chunk in plan_chunks[1:]:
                            await pikmin_send(message.channel, chunk, pikmin)
                    else:
                        try:
                            await thinking.edit(content=plan_chunks[0])
                        except Exception:
                            await message.channel.send(plan_chunks[0])
                        for chunk in plan_chunks[1:]:
                            await message.channel.send(chunk)

                    # New status message for Codex execution
                    if pikmin:
                        thinking = await pikmin_send(message.channel, "⚙️ Codex 執行中...", pikmin)
                    else:
                        thinking = await message.channel.send("⚙️ Codex 執行中...")

                    # Embed plan branch into the spec so Codex knows where to work
                    codex_spec = spec
                    if plan_branch:
                        branch_instr = (
                            f"Work on branch `{plan_branch}` (create from `{plan_base}` if it doesn't exist). "
                            if plan_base else f"Work on branch `{plan_branch}`. "
                        )
                        codex_spec = branch_instr + "\n\n" + spec
                    codex_result, codex_ok = await _run_codex_coder(codex_spec, cwd)
                    print(f"[CODING] codex ok={codex_ok}, result_len={len(codex_result)}", flush=True)

                    if not codex_ok or not codex_result:
                        err_text = "❌ Codex 執行失敗，請稍後再試。"
                        if pikmin:
                            try:
                                await pikmin_edit(thinking, err_text, message.channel)
                            except Exception:
                                await pikmin_send(message.channel, err_text, pikmin)
                        else:
                            try:
                                await thinking.edit(content=err_text)
                            except Exception:
                                pass
                        return
                    gen_result = codex_result

                executor_tag = "⚡ `Claude`" if complexity == "simple" else f"⚙️ `Codex {CODEX_CODER_MODEL}`"
                wt_info = channel_worktrees.get(message.channel.id)
                prefix = executor_tag
                if wt_info:
                    prefix += f" · 📌 `{wt_info['branch']}`"
                elif detected:
                    prefix += f" · 📂 `{cwd}`"
                display_result = f"{prefix}\n\n{gen_result}"

                result_chunks = split_message(display_result)
                if pikmin:
                    try:
                        await pikmin_edit(thinking, result_chunks[0], message.channel)
                    except Exception:
                        await pikmin_send(message.channel, result_chunks[0], pikmin)
                    for chunk in result_chunks[1:]:
                        await pikmin_send(message.channel, chunk, pikmin)
                else:
                    try:
                        await thinking.edit(content=result_chunks[0])
                    except Exception:
                        await message.channel.send(result_chunks[0])
                    for chunk in result_chunks[1:]:
                        await message.channel.send(chunk)

                # Step 3: Claude Evaluator reviews
                if pikmin:
                    eval_pikmin = _pick_eval_pikmin(pikmin)
                    eval_thinking = await pikmin_send(message.channel, "🔍 Claude Review 中...", eval_pikmin)
                    try:
                        review = await run_evaluator(prompt, gen_result, cwd, channel=message.channel)
                        verdict = _parse_verdict(review)
                        print(f"[CODING] evaluator verdict={verdict}", flush=True)

                        if review:
                            review_chunks = split_message(review)
                            try:
                                await pikmin_edit(eval_thinking, review_chunks[0], message.channel)
                            except Exception:
                                await pikmin_send(message.channel, review_chunks[0], eval_pikmin)
                            for chunk in review_chunks[1:]:
                                await pikmin_send(message.channel, chunk, eval_pikmin)
                        else:
                            await pikmin_edit(eval_thinking, "✅ Review 完成，沒有發現問題。", message.channel)

                        if verdict == "FAIL":
                            # Escalate to Claude generator for fixes
                            fix_session = None
                            for fix_round in range(2, MAX_GEN_EVAL_ROUNDS + 1):
                                fix_prompt = (
                                    f"Codex 執行了以下編碼任務但 Review 不通過。\n"
                                    f"原始需求：{prompt}\n\n"
                                    f"Evaluator 的回饋：\n{review}\n\n"
                                    f"請修正並說明改了什麼。"
                                )
                                fix_thinking = await pikmin_send(
                                    message.channel, f"🔧 Claude 修正中... (第 {fix_round}/{MAX_GEN_EVAL_ROUNDS} 輪)", pikmin
                                )
                                fix_result, fix_sid, _, _, _ = await run_claude(
                                    fix_prompt, cwd, fix_session, status_msg=fix_thinking, channel=message.channel,
                                )
                                if fix_sid:
                                    fix_session = fix_sid

                                fix_chunks = split_message(fix_result)
                                try:
                                    await pikmin_edit(fix_thinking, fix_chunks[0], message.channel)
                                except Exception:
                                    await pikmin_send(message.channel, fix_chunks[0], pikmin)
                                for chunk in fix_chunks[1:]:
                                    await pikmin_send(message.channel, chunk, pikmin)

                                re_thinking = await pikmin_send(
                                    message.channel, f"🔍 Re-review 中... (第 {fix_round}/{MAX_GEN_EVAL_ROUNDS} 輪)", eval_pikmin
                                )
                                review = await run_evaluator(prompt, fix_result, cwd, channel=message.channel)
                                if review:
                                    re_chunks = split_message(review)
                                    try:
                                        await pikmin_edit(re_thinking, re_chunks[0], message.channel)
                                    except Exception:
                                        await pikmin_send(message.channel, re_chunks[0], eval_pikmin)
                                    for chunk in re_chunks[1:]:
                                        await pikmin_send(message.channel, chunk, eval_pikmin)
                                else:
                                    await pikmin_edit(re_thinking, "✅ 修正後沒有發現問題。", message.channel)

                                verdict = _parse_verdict(review)
                                if verdict in ("PASS", "PASS_WITH_SUGGESTIONS"):
                                    break
                            else:
                                await message.channel.send(f"⚠️ 經 {MAX_GEN_EVAL_ROUNDS} 輪修正仍有問題，請人工檢查。")
                    except Exception as eval_err:
                        print(f"[CODING] eval error: {eval_err}", flush=True)
                        try:
                            await pikmin_edit(eval_thinking, f"⚠️ Review 失敗: {eval_err}", message.channel)
                        except Exception:
                            pass

            except asyncio.CancelledError:
                try:
                    await thinking.edit(content="⚠️ 被中斷，請重新發送指令。")
                except Exception:
                    pass
                raise
            except Exception as e:
                print(f"[CODING] ERROR: {e}", flush=True)
                try:
                    await thinking.edit(content=f"❌ Error: {e}")
                except Exception:
                    pass
        return

    # ── intent == "chat" or fallback: send to Claude ──

    # Auto-detect project
    detected = detect_project(prompt)
    if detected:
        new_cwd = os.path.join(WORK_ROOT, detected)
        old_cwd = channel_workdir.get(message.channel.id)
        if old_cwd != new_cwd:
            channel_workdir[message.channel.id] = new_cwd
            if old_cwd is not None:
                channel_session[message.channel.id] = None
            _save_state()

    lock = _get_channel_lock(message.channel.id)
    if lock.locked():
        await message.reply("⏳ 上一個請求還在處理中，請稍後再試。")
        return

    async with lock:
        cwd = channel_workdir.get(message.channel.id, WORK_ROOT)
        session_id = channel_session.get(message.channel.id)

        # ── Branch-mention auto-switch: if the prompt names a real git branch,
        # pin this channel to that branch's worktree so threads don't share HEAD.
        branch_candidates = BRANCH_NAME_RE.findall(prompt)
        if branch_candidates:
            main_repo = await find_main_repo_path(cwd)
            if main_repo:
                picked_branch = None
                for cand in branch_candidates:
                    if await branch_exists(main_repo, cand):
                        picked_branch = cand
                        break
                if picked_branch:
                    existing_wt = channel_worktrees.get(message.channel.id)
                    old_branch = existing_wt.get("branch") if existing_wt else None
                    if old_branch and old_branch != picked_branch:
                        # Thread already has a worktree — switch branch in-place
                        switched, was_stashed = await _switch_worktree_branch(
                            existing_wt["worktree"], main_repo, picked_branch,
                        )
                        if switched:
                            print(f"[BRANCH] in-place switch {old_branch} -> {picked_branch} in {existing_wt['worktree']}", flush=True)
                            channel_worktrees[message.channel.id]["branch"] = picked_branch
                            channel_session[message.channel.id] = None
                            _save_state()
                            session_id = None
                            try:
                                notice = f"🔀 `{old_branch}` → `{picked_branch}`"
                                if was_stashed:
                                    notice += f"\n📦 `{old_branch}` 上的未 commit 改動已存入 `git stash`"
                                await message.channel.send(notice)
                            except Exception:
                                pass
                    elif not existing_wt:
                        # No worktree yet — create/find one
                        wt = await ensure_worktree_for_branch(main_repo, picked_branch)
                        if wt and os.path.realpath(wt) != os.path.realpath(cwd):
                            print(f"[BRANCH] switching channel {message.channel.id} cwd: {cwd} -> {wt} (branch {picked_branch})", flush=True)
                            channel_workdir[message.channel.id] = wt
                            channel_session[message.channel.id] = None
                            channel_worktrees[message.channel.id] = {
                                "repo": main_repo, "worktree": wt, "branch": picked_branch,
                            }
                            _save_state()
                            _thread_wt_skip.discard(message.channel.id)
                            cwd = wt
                            session_id = None
                            try:
                                await message.channel.send(f"🔀 已切換至 `{picked_branch}` · `{wt}`")
                            except Exception:
                                pass

        # ── Auto-create isolated worktree for threads ──
        if (isinstance(message.channel, discord.Thread)
                and message.channel.id not in channel_worktrees
                and message.channel.id not in _thread_wt_skip):
            new_cwd, wt_branch, created = await _ensure_thread_worktree(
                message.channel, cwd,
            )
            if created:
                print(f"[THREAD_WT] auto-created worktree for thread {message.channel.id}: {new_cwd} (branch {wt_branch})", flush=True)
                cwd = new_cwd
                session_id = None
                channel_session[message.channel.id] = None
                try:
                    await message.channel.send(
                        f"🔀 已為此 thread 建立工作區\n📌 `{wt_branch}` · `{new_cwd}`"
                    )
                except Exception:
                    pass
            elif wt_branch is None:
                # No branch detected — skip future auto-detect attempts.
                # (Don't skip when branch was found but worktree creation failed —
                # that could be a transient error worth retrying.)
                _thread_wt_skip.add(message.channel.id)

        # Only inject cross-thread context on first message (no existing session)
        # When resuming, pass raw prompt to avoid polluting the conversation
        enriched_prompt = prompt
        # If user is replying to a message, include that message's content
        if message.reference and message.reference.message_id:
            try:
                ref_msg = message.reference.resolved or await message.channel.fetch_message(message.reference.message_id)
                if ref_msg and ref_msg.content:
                    ref_text = ref_msg.content[:3000]
                    enriched_prompt = f"[用戶引用的訊息]\n{ref_text}\n\n[用戶的回覆]\n{prompt}"
            except Exception:
                pass
        if not session_id:
            # New session — inject thread history so Claude knows what was discussed before
            context_parts = []
            if isinstance(message.channel, discord.Thread):
                try:
                    thread_hist = await fetch_thread_history(message.channel, limit=20, max_chars=8000)
                    if thread_hist:
                        context_parts.append(f"## 這個 thread 之前的對話\n{thread_hist}")
                except Exception:
                    pass
            # Inject relay summaries from previous sessions (context relay)
            relay_ctx = build_relay_context(message.channel.id)
            cross_ctx = build_cross_thread_context(message.channel.id)
            if relay_ctx:
                context_parts.append(relay_ctx)
            if cross_ctx:
                context_parts.append(f"[其他頻道背景，僅供參考]\n{cross_ctx}")
            if context_parts:
                enriched_prompt = "\n\n---\n".join(context_parts) + f"\n\n---\n{prompt}"

        # Inject monitor context if replying to a monitor's message or in incident thread
        monitor_sys_prompt = None
        target_monitor = None
        if reply_monitor_config:
            target_monitor = reply_monitor_config
        elif incident:
            target_monitor = monitor_configs.get(incident.get("monitor_id"), {})

        if target_monitor:
            monitor_sys_prompt = monitor_system_prompt(target_monitor)
            monitor_ctx_parts = []
            if target_monitor.get("check_commands"):
                monitor_ctx_parts.append(f"監控檢查方式：\n{target_monitor['check_commands']}")
            if target_monitor.get("check_instruction"):
                monitor_ctx_parts.append(f"監控描述：\n{target_monitor['check_instruction']}")
            mid = target_monitor.get("id")
            hist = monitor_histories.get(mid, []) if mid else []
            if hist:
                recent = hist[-3:]
                hist_text = "\n\n".join(f"--- {h['time']} ---\n{h['data']}" for h in recent)
                monitor_ctx_parts.append(f"最近的監控數據：\n{hist_text}")
            if monitor_ctx_parts:
                enriched_prompt = "\n\n".join(monitor_ctx_parts) + f"\n\n---\n用戶問題：{enriched_prompt}"

        # Determine pikmin for this channel
        pikmin = _get_pikmin(message.channel.id)
        # If replying to a monitor or in incident thread, use that monitor's pikmin
        if not pikmin and target_monitor:
            pidx = target_monitor.get("pikmin_index")
            if pidx is not None and 0 <= pidx < len(PIKMIN_POOL):
                pikmin = PIKMIN_POOL[pidx]
        if pikmin:
            thinking = await pikmin_send(message.channel, "⏳ 處理中...", pikmin)
        else:
            thinking = await message.reply("⏳ 處理中...")

        try:
            result, new_session_id, tools_used, num_turns, last_usage = await run_claude(
                enriched_prompt, cwd, session_id, status_msg=thinking, channel=message.channel,
                system_prompt=monitor_sys_prompt,
            )
            print(f"[REPLY] result length={len(result)}, session={new_session_id}, tools={len(tools_used)}", flush=True)
            if new_session_id:
                channel_session[message.channel.id] = new_session_id
                _save_state()

            # Branch indicator (worktree) takes priority over cwd indicator (project detect)
            wt_info = channel_worktrees.get(message.channel.id)
            if wt_info:
                result = f"📌 `{wt_info['branch']}`\n\n{result}"
            elif detected:
                result = f"📂 `{cwd}`\n\n{result}"

            wrote_code = _has_write_tools(tools_used)

            # ── Send generator's response ──
            chunks = split_message(result)
            print(f"[REPLY] sending {len(chunks)} chunks, wrote_code={wrote_code}", flush=True)

            # For pure answers, attach review button; for write ops, no button (auto-review below)
            view = None
            if not wrote_code and pikmin:
                view = ReviewView(
                    user_prompt=prompt,
                    generator_result=result,
                    cwd=cwd,
                    channel=message.channel,
                    generator_pikmin=pikmin,
                    session_id=new_session_id,
                )

            if pikmin:
                try:
                    await pikmin_edit(thinking, chunks[0], message.channel)
                except Exception as edit_err:
                    print(f"[REPLY] pikmin edit failed: {edit_err}, sending new", flush=True)
                    await pikmin_send(message.channel, chunks[0], pikmin)
                for chunk in chunks[1:]:
                    await pikmin_send(message.channel, chunk, pikmin)
                # Send review button as a separate message (webhooks can't have views)
                if view:
                    review_msg = await message.channel.send("", view=view)
                    view.store(review_msg.id)
            else:
                try:
                    await thinking.edit(content=chunks[0], view=view)
                    if view:
                        view.store(thinking.id)
                except Exception as edit_err:
                    print(f"[REPLY] edit failed: {edit_err}, sending as new message", flush=True)
                    fallback_msg = await message.channel.send(chunks[0], view=view)
                    if view:
                        view.store(fallback_msg.id)
                for chunk in chunks[1:]:
                    await message.channel.send(chunk)
            print(f"[REPLY] all chunks sent", flush=True)

            # ── TODO dispatch offer (pure-answer responses only) ──
            # If the response listed actionable TODOs, offer a button to run
            # each one serially through a gen-eval loop. Skipped when auto-review
            # is about to fire (write tools path) — that means code was already
            # written, not merely planned.
            if not wrote_code and pikmin and result:
                try:
                    todos = await _detect_actionable_todos(result)
                except Exception as detect_err:
                    print(f"[TODO_DETECT] unexpected error: {detect_err}", flush=True)
                    todos = []
                if todos:
                    print(f"[TODO_DETECT] found {len(todos)} actionable TODOs", flush=True)
                    base_pidx = pikmin_assignments.get(message.channel.id, 0)
                    # Pre-detect base branch so the dispatch message can show it
                    detected_branch = await _detect_base_branch(cwd, message.channel)
                    if detected_branch:
                        print(f"[TODO_DETECT] detected base branch: {detected_branch}", flush=True)
                    dispatch_view = TodoDispatchView(
                        todos=todos, cwd=cwd, channel=message.channel,
                        base_pikmin_idx=base_pidx,
                        base_branch=detected_branch,
                    )
                    preview = "\n".join(
                        f"**{idx}.** {t[:140]}{'…' if len(t) > 140 else ''}"
                        for idx, t in enumerate(todos, 1)
                    )
                    branch_hint = f"\n🌿 偵測到 branch：`{detected_branch}`" if detected_branch else ""
                    dispatch_msg = await message.channel.send(
                        f"📋 **偵測到 {len(todos)} 項 TODO**。要派 subagent 逐項處理嗎？\n"
                        f"（串行：每項跑 generator + evaluator 到收斂再進下一項）"
                        f"{branch_hint}\n\n{preview}",
                        view=dispatch_view,
                    )
                    dispatch_view.store(dispatch_msg.id)

            # ── Auto-review for write operations (with dialogue loop) ──
            if wrote_code and pikmin:
                print(f"[EVALUATOR] auto-review triggered (write tools detected)", flush=True)
                eval_pikmin = _pick_eval_pikmin(pikmin)
                eval_thinking = await pikmin_send(message.channel, "🔍 Review 中...", eval_pikmin)
                try:
                    review = await run_evaluator(prompt, result, cwd, channel=message.channel)
                    verdict = _parse_verdict(review)
                    print(f"[EVALUATOR] Auto-review verdict: {verdict}", flush=True)

                    # Display the review
                    if review:
                        review_chunks = split_message(review)
                        try:
                            await pikmin_edit(eval_thinking, review_chunks[0], message.channel)
                        except Exception:
                            await pikmin_send(message.channel, review_chunks[0], eval_pikmin)
                        for chunk in review_chunks[1:]:
                            await pikmin_send(message.channel, chunk, eval_pikmin)
                    else:
                        await pikmin_edit(eval_thinking, "✅ Review 完成，沒有發現問題。", message.channel)

                    if verdict == "PASS_WITH_SUGGESTIONS":
                        suggestion_view = SuggestionView(
                            review=review, user_prompt=prompt, cwd=cwd,
                            channel=message.channel, session_id=new_session_id,
                            generator_pikmin=pikmin, eval_pikmin=eval_pikmin,
                        )
                        sug_msg = await message.channel.send("💡 Evaluator 有改進建議，要採納嗎？", view=suggestion_view)
                        suggestion_view.store(sug_msg.id)
                    elif verdict == "FAIL":
                        # Resume generator with evaluator feedback for correction rounds
                        fix_session = new_session_id
                        for fix_round in range(2, MAX_GEN_EVAL_ROUNDS + 1):
                            fix_prompt = (
                                f"Evaluator 的回饋如下，請根據回饋修正：\n\n{review}\n\n"
                                f"修正完成後說明你改了什麼。"
                            )
                            fix_thinking = await pikmin_send(
                                message.channel, f"🔧 根據回饋修正中... (第 {fix_round}/{MAX_GEN_EVAL_ROUNDS} 輪)", pikmin
                            )
                            fix_result, fix_sid, _, _, _ = await run_claude(
                                fix_prompt, cwd, session_id=fix_session, status_msg=fix_thinking, channel=message.channel,
                            )
                            if fix_sid:
                                fix_session = fix_sid
                            result = fix_result

                            fix_chunks = split_message(fix_result)
                            try:
                                await pikmin_edit(fix_thinking, fix_chunks[0], message.channel)
                            except Exception:
                                await pikmin_send(message.channel, fix_chunks[0], pikmin)
                            for chunk in fix_chunks[1:]:
                                await pikmin_send(message.channel, chunk, pikmin)

                            # Evaluator re-review
                            re_thinking = await pikmin_send(
                                message.channel, f"🔍 Re-review 中... (第 {fix_round}/{MAX_GEN_EVAL_ROUNDS} 輪)", eval_pikmin
                            )
                            review = await run_evaluator(prompt, fix_result, cwd, channel=message.channel)
                            if review:
                                re_chunks = split_message(review)
                                try:
                                    await pikmin_edit(re_thinking, re_chunks[0], message.channel)
                                except Exception:
                                    await pikmin_send(message.channel, re_chunks[0], eval_pikmin)
                                for chunk in re_chunks[1:]:
                                    await pikmin_send(message.channel, chunk, eval_pikmin)
                            else:
                                await pikmin_edit(re_thinking, "✅ 修正後沒有發現問題。", message.channel)

                            verdict = _parse_verdict(review)
                            if verdict == "PASS":
                                break
                            if verdict == "PASS_WITH_SUGGESTIONS":
                                suggestion_view = SuggestionView(
                                    review=review, user_prompt=prompt, cwd=cwd,
                                    channel=message.channel, session_id=fix_session,
                                    generator_pikmin=pikmin, eval_pikmin=eval_pikmin,
                                )
                                sug_msg = await message.channel.send("💡 Evaluator 有改進建議，要採納嗎？", view=suggestion_view)
                                suggestion_view.store(sug_msg.id)
                                break
                        else:
                            await message.channel.send(f"⚠️ 經 {MAX_GEN_EVAL_ROUNDS} 輪修正仍有問題，請人工檢查。")
                except Exception as eval_err:
                    print(f"[EVALUATOR] ERROR: {eval_err}", flush=True)
                    try:
                        await pikmin_edit(eval_thinking, f"⚠️ Review 失敗: {eval_err}", message.channel)
                    except Exception:
                        pass

            # Track cumulative turns and usage (for !ctx and !relay)
            channel_cumulative_turns[message.channel.id] = channel_cumulative_turns.get(message.channel.id, 0) + num_turns
            if last_usage:
                channel_last_usage[message.channel.id] = last_usage

            # Update thread summary in background (non-blocking)
            thread_topic = ""
            if isinstance(message.channel, discord.Thread):
                thread_topic = message.channel.name
            asyncio.create_task(
                update_thread_summary(message.channel.id, prompt, result, thread_topic)
            )
        except asyncio.CancelledError:
            try:
                await thinking.edit(content="⚠️ 被中斷，請重新發送指令。")
            except Exception:
                pass
            raise
        except Exception as e:
            print(f"[REPLY] ERROR: {e}", flush=True)
            err_msg = f"❌ Error: {e}"
            try:
                await thinking.edit(content=err_msg)
            except Exception:
                await message.channel.send(err_msg)


if __name__ == "__main__":
    if not TOKEN:
        print("Error: Set DISCORD_BOT_TOKEN environment variable")
        exit(1)
    client.run(TOKEN)
