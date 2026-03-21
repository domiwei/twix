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
WORK_ROOT = "/root/work"

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

PREFIX = "!claude "
MAX_MSG_LEN = 2000

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-6")

SYSTEM_PROMPT = (
    "You are Bobo, a senior software engineering assistant embedded in a Discord server. "
    "You have full access to the project's codebase via CLI tools (Read, Edit, Write, Bash, Grep, Glob). "
    "When asked about code, always read the relevant files first before answering. "
    "Be direct, concise, and technically precise. "
    "When debugging, show root causes, not just symptoms. "
    "When writing code, follow existing project conventions. "
    "Always respond in Traditional Chinese (繁體中文) unless the user writes in English. "
    "Do not over-explain obvious things. Prioritize actionable answers."
)


def monitor_system_prompt(config: dict) -> str:
    service_name = config.get("service_name", config.get("name", "unknown"))
    nickname = config.get("nickname", service_name)
    knowledge = config.get("knowledge", [])

    base = (
        f"You are '{nickname}', a performance monitoring assistant for the {service_name} service. "
        "You will receive periodic system metrics (CPU, memory, etc.). "
        "Analyze the data and determine if there are anomalies or optimization opportunities. "
        'Respond in JSON format: {"anomaly": true/false, "summary": "brief description", "details": "detailed analysis"}. '
        "Consider: memory leaks (steady growth), CPU spikes, unusual resource consumption patterns. "
        "Compare with previous readings when available. Be concise. "
        "Always write your analysis in Traditional Chinese (繁體中文)."
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
        return workdir, session, threads, worktrees
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return {}, {}, set(), {}


def _save_state():
    data = {
        "workdir": {str(k): v for k, v in channel_workdir.items()},
        "session": {str(k): v for k, v in channel_session.items()},
        "threads": list(bot_threads),
        "worktrees": {str(k): v for k, v in channel_worktrees.items()},
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


channel_workdir, channel_session, _loaded_threads, channel_worktrees = _load_state()
monitor_configs, active_incidents = _load_monitors()
thread_summaries: dict[str, dict] = _load_summaries()

# Runtime state (not persisted)
active_monitor_tasks: dict[str, asyncio.Task] = {}  # monitor_id -> task
monitor_histories: dict[str, list[dict]] = {}  # monitor_id -> metric history
pending_monitors: dict[int, dict] = {}  # channel_id -> pending monitor config
channel_session_ts: dict[int, float] = {}  # channel_id -> last session activity timestamp
_channel_locks: dict[int, asyncio.Lock] = {}  # per-channel lock to prevent concurrent processing


def _get_channel_lock(channel_id: int) -> asyncio.Lock:
    if channel_id not in _channel_locks:
        _channel_locks[channel_id] = asyncio.Lock()
    return _channel_locks[channel_id]

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


def split_message(text: str) -> list[str]:
    chunks = []
    while len(text) > MAX_MSG_LEN:
        split_at = text.rfind("\n", 0, MAX_MSG_LEN)
        if split_at == -1:
            split_at = MAX_MSG_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
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
- monitor_add: User wants to start monitoring a service (e.g. "監控 erigon", "watch nginx")
- monitor_remove: User wants to stop monitoring (e.g. "停止監控", "別監控了")
- monitor_list: User wants to see active monitors (e.g. "有哪些監控", "list monitors")
- monitor_confirm: User confirms a pending monitor setup (e.g. "ok", "好", "確認", "開始吧", "就這樣")
- monitor_adjust: User wants to change pending monitor settings (e.g. "加 log", "改成每10分鐘", "不用 prometheus")
- monitor_teach: User wants to teach a monitor something, add knowledge or rules (e.g. "告訴阿鏈...", "阿鏈記一下...", "這個錯誤不重要", "peer disconnected 不用管")
- monitor_move: User wants to move a monitor's reporting to a different channel (e.g. "阿鏈去 #ops 回報", "把 erigon 監控搬到 #alerts", "monitor 改到 #ops")
- branch_task: User wants to create a branch/worktree for parallel work (e.g. "開 branch fix/issue-42 在 erigon")
- task_done: User says a task is complete (e.g. "做完了", "收工", "done")
- task_list: User wants to list active tasks (e.g. "有哪些 task")
- monitor_dismiss: User dismisses an alert as not a real problem in an incident thread (e.g. "這不是問題", "誤報", "false alarm", "沒事", "正常的")
- fix: User confirms they want the bot to fix an issue (e.g. "修吧", "go ahead and fix it")
- restart: User wants to restart a service (e.g. "跑看看", "restart it")
- create_thread: User wants to open a discussion thread (e.g. "開個 thread 討論")
- chat: General conversation, questions, or anything that doesn't match the above

Important:
- If the user is DECLINING an action (e.g. "先不要修", "不用", "別重啟"), classify as "chat", not the action.
- monitor_confirm/monitor_adjust only apply when context says there is a pending monitor setup.
- monitor_teach applies when user talks to a monitor by nickname or wants to teach/tell a monitor something. Context will list active monitor nicknames.
- If unsure, default to "chat".

Respond with ONLY a JSON object: {"intent": "<intent_name>"}"""

INTENT_MODEL = os.environ.get("INTENT_MODEL", "claude-haiku-4-5-20251001")


async def classify_intent(text: str, context: str = "") -> str:
    """Use Claude (Haiku) to classify user intent. Returns intent string."""
    prompt = text
    if context:
        prompt = f"[Context: {context}]\n\nUser message: {text}"

    cmd = [
        "claude", "-p", prompt,
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
            data = json.loads(raw)
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict) and entry.get("type") == "result":
                        result_text = entry.get("result", "")
                        inner = json.loads(result_text)
                        intent = inner.get("intent", "chat")
                        print(f"[INTENT] '{text[:50]}' -> {intent}", flush=True)
                        return intent
            elif isinstance(data, dict):
                if "result" in data:
                    inner = json.loads(data["result"])
                    intent = inner.get("intent", "chat")
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


CLAUDE_TIMEOUT = 600  # 10 minutes max per Claude call


async def _run_claude_stream(cmd: list[str], cwd: str, status_msg=None) -> tuple[str, str | None]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    result_text = ""
    session_id = None
    tools_used: list[str] = []
    assistant_texts: list[str] = []  # collect text blocks as fallback
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

    async def read_events():
        nonlocal result_text, session_id
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
                continue

            etype = event.get("type", "")
            if etype == "assistant":
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
                result_text = event.get("result", "")
                session_id = event.get("session_id")
                cost = event.get("total_cost_usd", 0)
                turns = event.get("num_turns", 0)
                print(f"[CLAUDE] done: {turns} turns, ${cost:.4f}, result_len={len(result_text)}", flush=True)

    async def _do_stream():
        events_task = asyncio.create_task(read_events())
        stderr_task = asyncio.create_task(proc.stderr.read())
        await asyncio.gather(events_task, stderr_task)
        await proc.wait()

    try:
        await asyncio.wait_for(_do_stream(), timeout=CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"[CLAUDE] TIMEOUT after {CLAUDE_TIMEOUT}s, killing process", flush=True)
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        if status_msg:
            try:
                await status_msg.edit(content="⏰ Claude 回應超時，請重試或簡化問題。")
            except Exception:
                pass
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    # stream-json result field is often empty in multi-tool scenarios.
    # Prefer the last assistant text block (usually the final answer) over --resume,
    # because --resume often causes Claude to say "被中斷" incorrectly.
    if not result_text and assistant_texts:
        result_text = assistant_texts[-1]
        print(f"[CLAUDE] using last assistant_text (len={len(result_text)})", flush=True)

    # Last resort: ask Claude to summarize via --resume
    if not result_text and session_id:
        print(f"[CLAUDE] result empty, asking for summary via --resume", flush=True)
        try:
            summary_proc = await asyncio.create_subprocess_exec(
                "claude", "-p",
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

    return result_text, session_id


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


MAX_TURNS = 25  # prevent runaway tool loops


async def run_claude(prompt: str, cwd: str, session_id: str | None = None,
                     status_msg=None, channel=None, system_prompt: str | None = None) -> tuple[str, str | None]:
    sys_prompt = system_prompt or SYSTEM_PROMPT
    cmd = [
        "claude",
        "-p", prompt,
        "--model", CLAUDE_MODEL,
        "--system-prompt", sys_prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", str(MAX_TURNS),
    ]
    if session_id:
        cmd.extend(["--resume", session_id])

    result, new_session_id = await _run_claude_stream(cmd, cwd, status_msg)

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
            "claude",
            "-p", rebuilt_prompt,
            "--model", CLAUDE_MODEL,
            "--system-prompt", sys_prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", str(MAX_TURNS),
        ]
        result, new_session_id = await _run_claude_stream(cmd_retry, cwd, status_msg)

    # Track session activity timestamp
    if new_session_id and channel:
        channel_session_ts[channel.id] = time.time()

    return result or "(no output)", new_session_id


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
        "claude", "-p", prompt,
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
    """Collect metrics based on monitor config."""
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
    alert_msg = await channel.send(f"⚠️ **{nickname} 監控偵測到異常**")
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
        await thread.send(chunk)
    await thread.send("如果要修，說「修吧」，我會開 branch 來處理。")

    return incident


async def resolve_incident(incident: dict):
    """Resolve an incident and archive the thread."""
    thread = client.get_channel(incident["thread_id"])
    if thread:
        await thread.send("✅ 連續多次檢查正常，問題已解決。關閉此 thread。")
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
        result, new_session_id = await run_claude(
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
    startup_msg = f"📊 **{display}** 監控已啟動\n每 {check_interval // 60} 分鐘檢查，每 {summary_interval // 3600} 小時回報狀態。"
    if knowledge_count:
        startup_msg += f"\n🧠 已載入 {knowledge_count} 條知識"
    await channel.send(startup_msg)

    while True:
        await asyncio.sleep(check_interval)
        try:
            metrics = collect_metrics(config)

            # Keep history
            history = monitor_histories.setdefault(monitor_id, [])
            history.append({"time": time.strftime("%H:%M:%S"), "data": metrics})
            if len(history) > 36:  # ~3 hours at 5-min intervals
                monitor_histories[monitor_id] = history[-36:]

            # Quick threshold check first (free, no Claude call)
            threshold_hit = quick_threshold_check(metrics)

            open_incident = find_incident_by_monitor(monitor_id)

            if threshold_hit:
                # Threshold hit — call Claude for detailed analysis
                history_text = "\n\n".join(
                    f"--- {h['time']} ---\n{h['data']}"
                    for h in monitor_histories[monitor_id][-6:]
                )
                prompt = (
                    f"以下是 {config['name']} 的歷史 metrics（最新在最下面）：\n\n{history_text}\n\n"
                    f"快速檢查發現：{threshold_hit['msg']}\n請詳細分析是否有異常及建議。"
                )
                result, _ = await run_claude(
                    prompt, config.get("project_path", WORK_ROOT),
                    system_prompt=monitor_system_prompt(config),
                )

                # Parse Claude response
                summary = threshold_hit["msg"]
                details = result
                try:
                    analysis = json.loads(result)
                    summary = analysis.get("summary", summary)
                    details = analysis.get("details", details)
                except (json.JSONDecodeError, TypeError):
                    pass

                if open_incident:
                    # Update existing incident thread
                    open_incident["consecutive_ok"] = 0
                    thread = client.get_channel(open_incident["thread_id"])
                    if thread:
                        update_text = f"📊 **持續異常** ({time.strftime('%H:%M')})\n{summary}\n\n{details}"
                        for chunk in split_message(update_text):
                            await thread.send(chunk)
                else:
                    # New incident
                    try:
                        await create_incident(monitor_id, channel, summary, details)
                    except Exception as e:
                        print(f"[MONITOR] Failed to create incident: {e}", flush=True)

                consecutive_ok_for_summary = 0

            else:
                # Normal
                consecutive_ok_for_summary += 1

                if open_incident:
                    open_incident["consecutive_ok"] = open_incident.get("consecutive_ok", 0) + 1
                    if open_incident["consecutive_ok"] >= 3:
                        await resolve_incident(open_incident)
                    else:
                        thread = client.get_channel(open_incident["thread_id"])
                        if thread:
                            await thread.send(
                                f"📊 檢查正常 ({open_incident['consecutive_ok']}/3)，"
                                f"連續 3 次正常就自動關閉。"
                            )
                    _save_monitors()

            # Periodic summary
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
                await channel.send(summary_text)

            print(
                f"[MONITOR] {config['name']} {time.strftime('%H:%M:%S')} - "
                f"{'ANOMALY' if threshold_hit else 'OK'}",
                flush=True,
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[MONITOR] {config['name']} Error: {e}", flush=True)


def parse_monitor_config(text: str, channel_id: int) -> dict | None:
    """Parse natural language into a monitor config."""
    project = detect_project(text)
    if not project:
        return None

    project_path = os.path.join(WORK_ROOT, project)

    # Detect checks from text
    checks = []
    text_lower = text.lower()
    if any(w in text_lower for w in ("cpu", "效能", "performance")):
        checks.append("cpu")
    if any(w in text_lower for w in ("memory", "mem", "記憶體", "內存")):
        checks.append("memory")
    if any(w in text_lower for w in ("log", "日誌", "錯誤")):
        checks.append("log")

    # Default: cpu + memory
    if not checks:
        checks = ["cpu", "memory"]

    # Detect intervals
    check_interval = 300  # default 5 min
    summary_interval = 10800  # default 3 hours

    # Parse "每X分鐘"
    interval_match = re.search(r"每\s*(\d+)\s*分鐘", text)
    if interval_match:
        check_interval = int(interval_match.group(1)) * 60

    # Detect log path
    log_path = None
    log_match = re.search(r"log[^\s]*\s+(\S+\.log)", text, re.IGNORECASE)
    if log_match:
        log_path = log_match.group(1)

    # Detect prometheus URL
    prom_url = None
    prom_match = re.search(r"(https?://\S+)", text)
    if prom_match:
        prom_url = prom_match.group(1)

    # Detect nickname: "叫他阿鏈" / "叫做阿鏈" / "nickname 阿鏈"
    nickname = None
    nick_match = re.search(r"(?:叫[他她它]?|叫做|暱稱|nickname)\s*(\S+)", text)
    if nick_match:
        nickname = nick_match.group(1).strip("，。、")

    mid = f"m_{uuid.uuid4().hex[:8]}"
    return {
        "id": mid,
        "name": project,
        "nickname": nickname or project,
        "channel_id": channel_id,
        "check_interval": check_interval,
        "summary_interval": summary_interval,
        "checks": checks,
        "service_name": project,
        "log_path": log_path,
        "prometheus_url": prom_url,
        "project_path": project_path,
        "knowledge": [],
        "enabled": True,
    }


def adjust_monitor_config(config: dict, text: str) -> dict:
    """Adjust a pending monitor config based on user feedback."""
    text_lower = text.lower()

    # Add/remove checks
    if any(w in text_lower for w in ("加 log", "加log", "加上 log", "log", "日誌")):
        if "log" not in config["checks"]:
            config["checks"].append("log")
    if any(w in text_lower for w in ("加 cpu", "加cpu")):
        if "cpu" not in config["checks"]:
            config["checks"].append("cpu")
    if any(w in text_lower for w in ("加 memory", "加mem", "加記憶體")):
        if "memory" not in config["checks"]:
            config["checks"].append("memory")
    if any(w in text_lower for w in ("不用 log", "不要 log", "拿掉 log", "移除 log")):
        config["checks"] = [c for c in config["checks"] if c != "log"]
    if any(w in text_lower for w in ("不用 cpu", "不要 cpu", "拿掉 cpu")):
        config["checks"] = [c for c in config["checks"] if c != "cpu"]

    # Adjust interval
    interval_match = re.search(r"(?:每|改成?\s*每?)\s*(\d+)\s*分鐘", text)
    if interval_match:
        config["check_interval"] = int(interval_match.group(1)) * 60

    # Adjust summary interval
    summary_match = re.search(r"(?:每|改成?\s*每?)\s*(\d+)\s*小時.*(?:回報|報告|summary)", text)
    if summary_match:
        config["summary_interval"] = int(summary_match.group(1)) * 3600

    # Detect log path
    log_match = re.search(r"(\S+\.log)\b", text)
    if log_match:
        config["log_path"] = log_match.group(1)

    # Detect prometheus URL
    prom_match = re.search(r"(https?://\S+)", text)
    if prom_match:
        config["prometheus_url"] = prom_match.group(1)

    # Adjust nickname
    nick_match = re.search(r"(?:叫[他她它]?|叫做|改名|暱稱|nickname)\s*(\S+)", text)
    if nick_match:
        config["nickname"] = nick_match.group(1).strip("，。、")

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
`@bot 監控 erigon 叫他阿鏈` — 開始監控，給暱稱
`@bot 監控 erigon 的 CPU/memory 和 log` — 指定檢查項目
`列出監控` — 查看所有監控
`停止監控 erigon` — 停止指定監控

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

**Super Manager：**
`!manager` — 查看所有 thread 的狀態總覽
`!manager <問題>` — 跨 thread 提問（例如：「erigon 修好了嗎？」「目前什麼最緊急？」）

**手動指令：**
`!repo` / `!repo <名稱>` — 查看/切換專案
`!projects` — 列出所有專案
`!reset` — 重置對話上下文
"""


# ── Events ───────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
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


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    content = message.content.strip()
    print(f"[MSG] {message.author}: {content}", flush=True)

    # ── Static commands ──

    if content == "!help":
        await message.reply(HELP_TEXT)
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
            result, _ = await run_claude(ctx, WORK_ROOT, system_prompt=(
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

    if content == "!reset":
        channel_session[message.channel.id] = None
        _save_state()
        await message.reply("✅ 對話已重置")
        return

    if content.startswith("!thread"):
        topic = content[7:].strip() or "Claude 對話"
        try:
            thread = await message.create_thread(name=topic)
            bot_threads.add(thread.id)
            channel_workdir[thread.id] = channel_workdir.get(message.channel.id, WORK_ROOT)
            await thread.send(f"🧵 Thread 已建立！直接在這裡打字就能跟我對話。\n工作目錄：`{channel_workdir[thread.id]}`")
            _save_state()
        except Exception as e:
            await message.reply(f"❌ 無法建立 thread: {e}")
        return

    if content == "!done":
        content = "!task done"

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
            _save_state()
            await status.edit(content=f"✅ Task 已建立！\n📂 Worktree: `{worktree_path}`\n🌿 Branch: `{branch_name}`")
            await thread.send(
                f"🔧 **Task: {description}**\n📂 工作目錄：`{worktree_path}`\n🌿 Branch：`{branch_name}`\n\n"
                f"直接在這裡打字開始工作。完成後說「做完了」或 `!done`。"
            )
        except Exception as e:
            await remove_worktree(repo_path, worktree_path)
            await status.edit(content=f"❌ 建立 thread 失敗：{e}")
        return

    # ── NLP: classify intent via Claude ──

    prompt = parse_prompt(content, client.user.id)
    if prompt is None:
        if message.reference and message.reference.message_id:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                if ref_msg.author == client.user:
                    prompt = content
            except Exception:
                pass
    if prompt is None and isinstance(message.channel, discord.Thread) and message.channel.id in bot_threads:
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
    if monitor_configs:
        nicknames = [f"{c.get('nickname', c['name'])}({c['name']})" for c in monitor_configs.values() if c.get("enabled")]
        if nicknames:
            ctx_parts.append(f"Active monitors: {', '.join(nicknames)}")
    context = "; ".join(ctx_parts)

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
        target = detect_project(prompt)
        removed = False
        for mid, config in list(monitor_configs.items()):
            if target and target.lower() not in config["name"].lower():
                continue
            task = active_monitor_tasks.pop(mid, None)
            if task:
                task.cancel()
            config["enabled"] = False
            del monitor_configs[mid]
            removed = True
            await message.reply(f"✅ 已停止監控 **{config['name']}**")
            break
        if not removed:
            await message.reply("❌ 找不到對應的監控。用「列出監控」查看。")
        _save_monitors()
        return

    if intent == "monitor_list":
        if not monitor_configs:
            await message.reply("目前沒有設定任何監控。")
        else:
            lines = ["**監控列表：**"]
            for mid, config in monitor_configs.items():
                running = mid in active_monitor_tasks and not active_monitor_tasks[mid].done()
                status = "🟢 運行中" if running else "🔴 已停止"
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
        config = parse_monitor_config(prompt, message.channel.id)
        if not config:
            await message.reply("❌ 找不到專案名稱。例如：「監控 erigon」")
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
        config = adjust_monitor_config(config, prompt)
        pending_monitors[message.channel.id] = config
        await message.reply(format_monitor_proposal(config))
        return

    if intent == "monitor_confirm" and message.channel.id in pending_monitors:
        config = pending_monitors.pop(message.channel.id)
        monitor_configs[config["id"]] = config
        _save_monitors()
        active_monitor_tasks[config["id"]] = asyncio.create_task(
            monitor_loop(config["id"], message.channel)
        )
        nickname = config.get("nickname", config["name"])
        checks_str = "、".join(config["checks"])
        display = f"{nickname} ({config['name']})" if nickname != config["name"] else config["name"]
        await message.reply(
            f"✅ **{display}** 已上線！\n"
            f"檢查項目：{checks_str}\n"
            f"每 {config['check_interval'] // 60} 分鐘檢查，每 {config['summary_interval'] // 3600} 小時回報"
        )
        return

    if intent == "monitor_teach":
        target_config = _find_monitor_by_text(prompt)
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
        target_config = _find_monitor_by_text(prompt)
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
            _save_state()
            await status.edit(content=f"✅ Task 已建立！\n📂 Worktree: `{worktree_path}`\n🌿 Branch: `{branch}`")
            await thread.send(
                f"🔧 **Task: {description}**\n📂 工作目錄：`{worktree_path}`\n🌿 Branch：`{branch}`\n\n"
                f"直接在這裡打字開始工作。完成後說「做完了」或 `!done`。"
            )
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
            await thread.send(f"🧵 Thread 已建立！直接在這裡打字就能跟我對話。\n工作目錄：`{channel_workdir[thread.id]}`")
            _save_state()
        except Exception as e:
            await message.reply(f"❌ 無法建立 thread: {e}")
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

        # Inject cross-thread context into prompt
        cross_ctx = build_cross_thread_context(message.channel.id)
        enriched_prompt = f"{cross_ctx}\n\n---\n{prompt}" if cross_ctx else prompt

        thinking = await message.reply("⏳ 處理中...")

        try:
            result, new_session_id = await run_claude(
                enriched_prompt, cwd, session_id, status_msg=thinking, channel=message.channel
            )
            print(f"[REPLY] result length={len(result)}, session={new_session_id}", flush=True)
            if new_session_id:
                channel_session[message.channel.id] = new_session_id
                _save_state()

            if detected:
                result = f"📂 `{cwd}`\n\n{result}"

            chunks = split_message(result)
            print(f"[REPLY] sending {len(chunks)} chunks, first={len(chunks[0])} chars", flush=True)
            try:
                await thinking.edit(content=chunks[0])
            except Exception as edit_err:
                print(f"[REPLY] edit failed: {edit_err}, sending as new message", flush=True)
                await message.channel.send(chunks[0])
            for i, chunk in enumerate(chunks[1:], 2):
                await message.channel.send(chunk)
            print(f"[REPLY] all chunks sent", flush=True)

            # Update thread summary in background (non-blocking)
            thread_topic = ""
            if isinstance(message.channel, discord.Thread):
                thread_topic = message.channel.name
            asyncio.create_task(
                update_thread_summary(message.channel.id, prompt, result, thread_topic)
            )
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
