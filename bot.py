import discord
import asyncio
import os
import json
import re
import subprocess
import time
import shutil

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
WORK_ROOT = "/root/work"

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

PREFIX = "!claude "
MAX_MSG_LEN = 2000

SYSTEM_PROMPT = (
    "You are a helpful assistant. Always respond in Traditional Chinese (繁體中文) "
    "unless the user writes in English, in which case respond in English."
)

MONITOR_SYSTEM_PROMPT = (
    "You are a performance monitoring assistant for an Erigon Ethereum full node. "
    "You will receive periodic system metrics (CPU, memory, etc.). "
    "Analyze the data and determine if there are anomalies or optimization opportunities. "
    "Respond in JSON format: {\"anomaly\": true/false, \"summary\": \"brief description\", \"details\": \"detailed analysis\"}. "
    "Consider: memory leaks (steady growth), CPU spikes, unusual resource consumption patterns. "
    "Compare with previous readings when available. Be concise. "
    "Always write your analysis in Traditional Chinese (繁體中文)."
)

# Per-channel working directory and session
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")


def _load_state():
    """Load persisted channel state from disk."""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        # JSON keys are strings, convert back to int
        workdir = {int(k): v for k, v in data.get("workdir", {}).items()}
        session = {int(k): v for k, v in data.get("session", {}).items()}
        threads = set(int(t) for t in data.get("threads", []))
        worktrees = {int(k): v for k, v in data.get("worktrees", {}).items()}
        return workdir, session, threads, worktrees
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return {}, {}, set(), {}


def _save_state():
    """Persist channel state to disk."""
    data = {
        "workdir": {str(k): v for k, v in channel_workdir.items()},
        "session": {str(k): v for k, v in channel_session.items()},
        "threads": list(bot_threads),
        "worktrees": {str(k): v for k, v in channel_worktrees.items()},
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)


channel_workdir, channel_session, _loaded_threads, channel_worktrees = _load_state()

# Monitor state
monitor_task: asyncio.Task | None = None
monitor_channel_id: int | None = None
monitor_history: list[dict] = []
MONITOR_INTERVAL = 300  # 5 minutes
METRICS_ENDPOINT = "http://localhost:6261/debug/metrics/prometheus"


def list_projects() -> list[str]:
    """List project directories under WORK_ROOT."""
    if not os.path.isdir(WORK_ROOT):
        return []
    return sorted(
        d for d in os.listdir(WORK_ROOT)
        if os.path.isdir(os.path.join(WORK_ROOT, d))
    )


THREAD_PATTERN = re.compile(
    r"(開|開個|建|建立|create|open|start)\s*.{0,10}(thread|討論串|對話串|串)",
    re.IGNORECASE,
)

# Pattern: detect branch/task creation requests
# e.g. "幫我在 erigon 開 branch fix/issue-42 修復 memory leak"
#      "開個 branch feat/api 在 erigon 做新 API"
#      "create branch fix/bug on erigon"
BRANCH_PATTERN = re.compile(
    r"(開|開個|建|建立|create|open|start)\s*.{0,5}branch",
    re.IGNORECASE,
)
# Extract branch name: word containing "/"
BRANCH_NAME_RE = re.compile(r"\b([a-zA-Z0-9_-]+/[a-zA-Z0-9_.#-]+)\b")

# Pattern: task done
DONE_PATTERN = re.compile(
    r"(做完了|完成了|done|結束|收工|finished|close\s*task|關閉\s*task|任務完成|task\s*done)",
    re.IGNORECASE,
)

# Pattern: list tasks
TASK_LIST_PATTERN = re.compile(
    r"(哪些|列出|list|查看|看看).{0,10}(task|任務|worktree|工作)",
    re.IGNORECASE,
)


def wants_thread(text: str) -> bool:
    """Detect if user wants to start a thread via natural language."""
    return bool(THREAD_PATTERN.search(text)) and not wants_branch_task(text)


def wants_branch_task(text: str) -> bool:
    """Detect if user wants to create a branch/worktree task."""
    return bool(BRANCH_PATTERN.search(text)) and bool(BRANCH_NAME_RE.search(text))


def parse_branch_task(text: str) -> tuple[str | None, str | None, str]:
    """Extract (project, branch, description) from natural language."""
    branch_match = BRANCH_NAME_RE.search(text)
    branch = branch_match.group(1) if branch_match else None
    project = detect_project(text)
    # Description: remove the branch name and project from text, use the rest
    desc = text
    if branch:
        desc = desc.replace(branch, "").strip()
    if project:
        desc = desc.replace(project, "").strip()
    # Clean up common filler words
    desc = re.sub(r"(開個?|建立?|create|open|start|幫我|在|的|上|做|個|on)\s*branch\s*", "", desc, flags=re.IGNORECASE).strip()
    desc = re.sub(r"\b(on|in)\b", "", desc, flags=re.IGNORECASE).strip()
    desc = re.sub(r"^[\s,，、在的上幫我開做個]+", "", desc).strip()
    desc = re.sub(r"[\s,，、在的上]+$", "", desc).strip()
    return project, branch, desc or (branch or "task")


def wants_done(text: str) -> bool:
    """Detect if user says the task is done."""
    return bool(DONE_PATTERN.search(text))


def wants_task_list(text: str) -> bool:
    """Detect if user wants to see active tasks."""
    return bool(TASK_LIST_PATTERN.search(text))


def detect_project(text: str) -> str | None:
    """Try to detect a project name from the message text."""
    projects = list_projects()
    text_lower = text.lower()
    for proj in projects:
        if proj.lower() in text_lower:
            return proj
    return None


async def create_worktree(repo_path: str, branch_name: str) -> str:
    """Create a git worktree for parallel work. Returns the worktree path."""
    # Sanitize branch name for use as directory name
    dir_suffix = branch_name.replace("/", "-")
    repo_basename = os.path.basename(repo_path)
    worktree_path = os.path.join(WORK_ROOT, f"{repo_basename}-wt-{dir_suffix}")

    # Remove stale directory if it exists but isn't a valid worktree
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
        # Branch might already exist, try without -b
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
    """Remove a git worktree."""
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "remove", "--force", worktree_path,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.wait()
    # Clean up directory if still present
    if os.path.exists(worktree_path):
        shutil.rmtree(worktree_path)


def _format_tool_desc(name: str, input_data: dict) -> str:
    """Format a tool use into a short human-readable description."""
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


async def _run_claude_stream(cmd: list[str], cwd: str, status_msg=None) -> tuple[str, str | None]:
    """Run claude with stream-json, parse events for progress, return (result, session_id)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # stream-json: structured events go to stdout
    result_text = ""
    session_id = None
    tools_used: list[str] = []
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
                # Show last 3 tools
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
                        # Claude is generating text
                        await update_status("💬 撰寫回覆中...")

            elif etype == "result":
                result_text = event.get("result", "")
                session_id = event.get("session_id")
                cost = event.get("total_cost_usd", 0)
                turns = event.get("num_turns", 0)
                print(f"[CLAUDE] done: {turns} turns, ${cost:.4f}", flush=True)

    # Read events from stdout; drain stderr
    events_task = asyncio.create_task(read_events())
    await proc.stderr.read()  # drain stderr
    await events_task
    await proc.wait()

    return result_text, session_id


async def fetch_thread_history(channel, limit: int = 20) -> str:
    """Fetch recent messages from a Discord channel/thread as context."""
    messages = []
    async for msg in channel.history(limit=limit, oldest_first=True):
        role = "assistant" if msg.author == client.user else "user"
        messages.append(f"[{role}] {msg.content}")
    return "\n".join(messages)


async def run_claude(prompt: str, cwd: str, session_id: str | None = None, status_msg=None, channel=None) -> tuple[str, str | None]:
    """Run claude CLI with the given prompt in the specified directory."""
    cmd = [
        "claude",
        "-p", prompt,
        "--system-prompt", SYSTEM_PROMPT,
        "--output-format", "stream-json",
        "--verbose",
    ]
    if session_id:
        cmd.extend(["--resume", session_id])

    result, new_session_id = await _run_claude_stream(cmd, cwd, status_msg)

    # If resume failed (empty result), rebuild context from thread history
    if session_id and not result:
        context = ""
        if channel:
            try:
                context = await fetch_thread_history(channel)
            except Exception:
                pass

        if context:
            rebuilt_prompt = f"以下是之前的對話紀錄，請根據這些上下文繼續回答：\n\n{context}\n\n---\n用戶最新的訊息：\n{prompt}"
        else:
            rebuilt_prompt = prompt

        cmd_retry = [
            "claude",
            "-p", rebuilt_prompt,
            "--system-prompt", SYSTEM_PROMPT,
            "--output-format", "stream-json",
            "--verbose",
        ]
        result, new_session_id = await _run_claude_stream(cmd_retry, cwd, status_msg)

    return result or "(no output)", new_session_id


def collect_metrics() -> str:
    """Collect erigon process metrics and system info."""
    lines = []
    lines.append(f"=== Erigon Metrics @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    # Process stats via ps
    try:
        ps_out = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=10
        ).stdout
        for line in ps_out.splitlines():
            if "erigon" in line.lower() and "grep" not in line:
                lines.append(f"[process] {line}")
    except Exception as e:
        lines.append(f"[process] error: {e}")

    # Memory info from /proc/meminfo
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if any(k in line for k in ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached")):
                    lines.append(f"[system] {line.strip()}")
    except Exception as e:
        lines.append(f"[system] error: {e}")

    # Prometheus metrics (subset)
    try:
        import urllib.request
        with urllib.request.urlopen(METRICS_ENDPOINT, timeout=5) as resp:
            prom_text = resp.read().decode()
        # Extract key metrics
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


async def monitor_loop(channel: discord.TextChannel):
    """Background loop: collect metrics, ask Claude to analyze, report anomalies."""
    global monitor_history
    await channel.send("📊 監控已啟動，每 5 分鐘檢查一次 Erigon 狀態。")
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        try:
            metrics = collect_metrics()
            # Keep last 6 readings (~30 min history) for trend analysis
            monitor_history.append({"time": time.strftime('%H:%M:%S'), "data": metrics})
            if len(monitor_history) > 6:
                monitor_history = monitor_history[-6:]

            history_text = "\n\n".join(
                f"--- {h['time']} ---\n{h['data']}" for h in monitor_history
            )
            prompt = f"以下是 Erigon node 的歷史 metrics（最新在最下面）：\n\n{history_text}\n\n請分析是否有異常或可以優化的地方。"

            cmd = [
                "claude", "-p", prompt,
                "--system-prompt", MONITOR_SYSTEM_PROMPT,
                "--output-format", "json",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=WORK_ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            raw = stdout.decode("utf-8", errors="replace").strip()

            # Parse Claude's response
            result_text = raw
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    for entry in data:
                        if isinstance(entry, dict) and entry.get("type") == "result":
                            result_text = entry.get("result", raw)
                elif isinstance(data, dict):
                    result_text = data.get("result", raw)
            except (json.JSONDecodeError, TypeError):
                pass

            # Try to parse the analysis JSON from Claude
            is_anomaly = False
            summary = result_text
            try:
                analysis = json.loads(result_text)
                is_anomaly = analysis.get("anomaly", False)
                summary = analysis.get("summary", "")
                details = analysis.get("details", "")
                if is_anomaly:
                    summary = f"**{summary}**\n\n{details}"
            except (json.JSONDecodeError, TypeError):
                # Claude didn't return JSON, check for anomaly keywords
                lower = result_text.lower()
                if any(w in lower for w in ("異常", "anomaly", "spike", "leak", "問題", "warning")):
                    is_anomaly = True
                    summary = result_text

            if is_anomaly:
                # Create a thread to discuss the anomaly
                try:
                    alert_msg = await channel.send(f"⚠️ **Erigon 監控偵測到異常**")
                    thread = await alert_msg.create_thread(name=f"Erigon 異常 - {time.strftime('%m/%d %H:%M')}")
                    bot_threads.add(thread.id)
                    channel_workdir[thread.id] = WORK_ROOT
                    _save_state()
                    chunks = split_message(summary)
                    for chunk in chunks:
                        await thread.send(chunk)
                    await thread.send("💬 你可以在這裡繼續討論，我會幫你分析和建議修復方案。")
                except Exception as e:
                    await channel.send(f"❌ 無法建立異常 thread: {e}")
            else:
                print(f"[MONITOR] {time.strftime('%H:%M:%S')} - Normal: {summary[:100]}", flush=True)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[MONITOR] Error: {e}", flush=True)


# Natural language patterns for monitor start/stop
MONITOR_START_PATTERN = re.compile(
    r"(開始|啟動|start|begin|run)\s*.{0,10}(監控|monitor|watch|觀察)",
    re.IGNORECASE,
)
MONITOR_STOP_PATTERN = re.compile(
    r"(停止|停|關閉|stop|end|cancel|取消)\s*.{0,10}(監控|monitor|watch|觀察)",
    re.IGNORECASE,
)
MONITOR_STATUS_PATTERN = re.compile(
    r"(監控|monitor).{0,5}(狀態|status|怎樣|如何)",
    re.IGNORECASE,
)


def wants_monitor_start(text: str) -> bool:
    return bool(MONITOR_START_PATTERN.search(text))


def wants_monitor_stop(text: str) -> bool:
    return bool(MONITOR_STOP_PATTERN.search(text))


def wants_monitor_status(text: str) -> bool:
    return bool(MONITOR_STATUS_PATTERN.search(text))


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


# Track threads created by the bot (thread_id -> True)
bot_threads: set[int] = _loaded_threads

HELP_TEXT = """**Claude Bot 指令說明**

**基本用法：**
`!claude <問題>` 或 `@claudeBot <問題>`

**開 Thread 聊天（獨立上下文）：**
`!thread <主題>` — 開一個 thread，裡面直接打字就能對話

**專案操作：**
直接在訊息中提到專案名稱，Bot 會自動切換到對應目錄：
`@claudeBot 看一下 my-project 的結構`
`@claudeBot 修 my-app 裡的 login bug`

**Erigon 監控：**
`@claudeBot 開始監控 erigon` — 每 5 分鐘檢查 CPU/Memory，異常自動開 thread
`@claudeBot 停止監控` — 停止背景監控
`@claudeBot 監控狀態` — 查看監控是否在跑

**平行任務（Worktree）：**
自然語言：`@bot 在 erigon 開 branch fix/issue-42 修復 memory leak`
或指令：`!task erigon fix/issue-42 修復 memory leak`
完成後說「做完了」或 `!done` — 清理 worktree
`有哪些 task` 或 `!task list` — 列出進行中的 tasks

**手動指令：**
`!repo` — 查看目前工作目錄
`!repo <名稱或路徑>` — 手動切換專案
`!projects` — 列出所有專案
`!reset` — 重置對話上下文
"""


@client.event
async def on_ready():
    print(f"Bot is online as {client.user}", flush=True)
    for guild in client.guilds:
        print(f"  Connected to server: {guild.name} (id: {guild.id})", flush=True)
    projects = list_projects()
    print(f"  Projects in {WORK_ROOT}: {projects or '(empty)'}", flush=True)


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    content = message.content.strip()
    print(f"[MSG] {message.author}: {content}", flush=True)

    # !help
    if content == "!help":
        await message.reply(HELP_TEXT)
        return

    # !projects - list available projects
    if content == "!projects":
        projects = list_projects()
        if projects:
            proj_list = "\n".join(f"• `{p}`" for p in projects)
            await message.reply(f"**{WORK_ROOT} 底下的專案：**\n{proj_list}")
        else:
            await message.reply(f"`{WORK_ROOT}` 底下還沒有專案目錄")
        return

    # !repo - set or show working directory
    if content.startswith("!repo"):
        args = content[5:].strip()
        if not args:
            cwd = channel_workdir.get(message.channel.id, WORK_ROOT)
            await message.reply(f"目前工作目錄：`{cwd}`")
            return
        # Support both full path and project name
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

    # !reset
    if content == "!reset":
        channel_session[message.channel.id] = None
        _save_state()
        await message.reply("✅ 對話已重置")
        return

    # !thread - create a thread for conversation
    if content.startswith("!thread"):
        topic = content[7:].strip() or "Claude 對話"
        try:
            thread = await message.create_thread(name=topic)
            bot_threads.add(thread.id)
            channel_workdir[thread.id] = channel_workdir.get(message.channel.id, WORK_ROOT)
            parent_session = channel_session.get(message.channel.id)
            if parent_session:
                channel_session[thread.id] = parent_session
                await thread.send(f"🧵 Thread 已建立，已帶入之前的對話上下文！直接打字繼續聊。\n工作目錄：`{channel_workdir[thread.id]}`")
            else:
                await thread.send(f"🧵 Thread 已建立！直接在這裡打字就能跟我對話。\n工作目錄：`{channel_workdir[thread.id]}`")
            _save_state()
        except Exception as e:
            await message.reply(f"❌ 無法建立 thread: {e}")
        return

    # !done - shortcut for !task done
    if content == "!done":
        content = "!task done"

    # !task - create a worktree-backed task thread
    if content.startswith("!task"):
        args = content[5:].strip()

        # !task done — clean up the worktree for this thread
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

        # !task list — show active tasks
        if args in ("list", "ls", ""):
            if not channel_worktrees:
                await message.reply("目前沒有進行中的 task。")
                return
            lines = ["**進行中的 Tasks：**"]
            for tid, info in channel_worktrees.items():
                lines.append(f"• <#{tid}> — `{info['branch']}` (`{info['worktree']}`)")
            await message.reply("\n".join(lines))
            return

        # !task <project> <branch> <description>
        parts = args.split(None, 2)
        if len(parts) < 2:
            await message.reply("用法：`!task <專案> <branch名稱> [描述]`\n例如：`!task erigon fix/issue-42 修復 memory leak`")
            return

        project_name = parts[0]
        branch_name = parts[1]
        description = parts[2] if len(parts) > 2 else branch_name

        # Resolve project path
        if os.path.isdir(project_name):
            repo_path = project_name
        elif os.path.isdir(os.path.join(WORK_ROOT, project_name)):
            repo_path = os.path.join(WORK_ROOT, project_name)
        else:
            await message.reply(f"❌ 找不到專案：`{project_name}`")
            return

        # Create worktree
        status = await message.reply("⏳ 正在建立 worktree...")
        try:
            worktree_path = await create_worktree(repo_path, branch_name)
        except Exception as e:
            await status.edit(content=f"❌ 建立 worktree 失敗：{e}")
            return

        # Create thread
        try:
            thread_name = f"🔧 {description[:90]}"
            thread = await message.create_thread(name=thread_name)
            bot_threads.add(thread.id)
            channel_workdir[thread.id] = worktree_path
            channel_worktrees[thread.id] = {
                "repo": repo_path,
                "worktree": worktree_path,
                "branch": branch_name,
            }
            _save_state()
            await status.edit(content=f"✅ Task 已建立！\n📂 Worktree: `{worktree_path}`\n🌿 Branch: `{branch_name}`")
            await thread.send(
                f"🔧 **Task: {description}**\n"
                f"📂 工作目錄：`{worktree_path}`\n"
                f"🌿 Branch：`{branch_name}`\n\n"
                f"直接在這裡打字開始工作。完成後用 `!task done` 清理 worktree。"
            )
        except Exception as e:
            # Clean up worktree if thread creation failed
            await remove_worktree(repo_path, worktree_path)
            await status.edit(content=f"❌ 建立 thread 失敗：{e}")
        return

    # Monitor commands via natural language
    global monitor_task, monitor_channel_id
    prompt_check = parse_prompt(content, client.user.id)
    check_text = prompt_check or content

    if wants_monitor_start(check_text):
        if monitor_task and not monitor_task.done():
            await message.reply("📊 監控已經在跑了！")
        else:
            monitor_channel_id = message.channel.id
            monitor_history.clear()
            monitor_task = asyncio.create_task(monitor_loop(message.channel))
            await message.reply("✅ 開始監控 Erigon，每 5 分鐘檢查 CPU/Memory，發現異常會開 thread 通知你。")
        return

    if wants_monitor_stop(check_text):
        if monitor_task and not monitor_task.done():
            monitor_task.cancel()
            monitor_task = None
            monitor_channel_id = None
            monitor_history.clear()
            await message.reply("✅ 已停止監控。")
        else:
            await message.reply("目前沒有在跑監控。")
        return

    if wants_monitor_status(check_text):
        if monitor_task and not monitor_task.done():
            readings = len(monitor_history)
            await message.reply(f"📊 監控運行中，已收集 {readings} 筆數據，每 {MONITOR_INTERVAL//60} 分鐘檢查一次。")
        else:
            await message.reply("目前沒有在跑監控。")
        return

    # Natural language: task done (in worktree thread)
    if wants_done(check_text) and message.channel.id in channel_worktrees:
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

    # Natural language: list tasks
    if wants_task_list(check_text):
        if not channel_worktrees:
            await message.reply("目前沒有進行中的 task。")
        else:
            lines = ["**進行中的 Tasks：**"]
            for tid, info in channel_worktrees.items():
                lines.append(f"• <#{tid}> — `{info['branch']}` (`{info['worktree']}`)")
            await message.reply("\n".join(lines))
        return

    # Natural language: create branch/worktree task
    if wants_branch_task(check_text) and not isinstance(message.channel, discord.Thread):
        project, branch, description = parse_branch_task(check_text)
        if not project:
            await message.reply("❌ 我找不到專案名稱，請提到專案名稱，例如：「在 erigon 開 branch fix/issue-42」")
            return
        if not branch:
            await message.reply("❌ 我找不到 branch 名稱，請用 `xxx/yyy` 格式，例如：`fix/issue-42`")
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
                "repo": repo_path,
                "worktree": worktree_path,
                "branch": branch,
            }
            _save_state()
            await status.edit(content=f"✅ Task 已建立！\n📂 Worktree: `{worktree_path}`\n🌿 Branch: `{branch}`")
            await thread.send(
                f"🔧 **Task: {description}**\n"
                f"📂 工作目錄：`{worktree_path}`\n"
                f"🌿 Branch：`{branch}`\n\n"
                f"直接在這裡打字開始工作。完成後說「做完了」或 `!done` 清理 worktree。"
            )
        except Exception as e:
            await remove_worktree(repo_path, worktree_path)
            await status.edit(content=f"❌ 建立 thread 失敗：{e}")
        return

    # Claude prompt — support !claude, @mention, or replying to bot
    prompt = parse_prompt(content, client.user.id)
    if prompt is None:
        # Check if this is a reply to the bot's message
        if message.reference and message.reference.message_id:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                if ref_msg.author == client.user:
                    prompt = content
            except Exception:
                pass
    # In bot-created threads, treat all messages as prompts
    if prompt is None and isinstance(message.channel, discord.Thread) and message.channel.id in bot_threads:
        prompt = content
    if prompt is None:
        return
    if not prompt:
        await message.reply("請在指令後輸入內容，例如：`!claude 你好`")
        return

    # Auto-detect thread request from natural language
    if wants_thread(prompt) and not isinstance(message.channel, discord.Thread):
        topic = prompt[:50] if len(prompt) > 5 else "Claude 對話"
        try:
            thread = await message.create_thread(name=topic)
            bot_threads.add(thread.id)
            channel_workdir[thread.id] = channel_workdir.get(message.channel.id, WORK_ROOT)
            # Carry over session from parent channel so thread continues the conversation
            parent_session = channel_session.get(message.channel.id)
            if parent_session:
                channel_session[thread.id] = parent_session
                await thread.send(f"🧵 Thread 已建立，已帶入之前的對話上下文！直接打字繼續聊。\n工作目錄：`{channel_workdir[thread.id]}`")
            else:
                await thread.send(f"🧵 Thread 已建立！直接在這裡打字就能跟我對話。\n工作目錄：`{channel_workdir[thread.id]}`")
            _save_state()
        except Exception as e:
            await message.reply(f"❌ 無法建立 thread: {e}")
        return

    # Auto-detect project from message
    detected = detect_project(prompt)
    if detected:
        new_cwd = os.path.join(WORK_ROOT, detected)
        old_cwd = channel_workdir.get(message.channel.id)
        if old_cwd != new_cwd:
            channel_workdir[message.channel.id] = new_cwd
            # Only reset session when actually switching to a different project
            if old_cwd is not None:
                channel_session[message.channel.id] = None
            _save_state()

    cwd = channel_workdir.get(message.channel.id, WORK_ROOT)
    session_id = channel_session.get(message.channel.id)

    thinking = await message.reply("⏳ 處理中...")

    try:
        result, new_session_id = await run_claude(prompt, cwd, session_id, status_msg=thinking, channel=message.channel)
        if new_session_id:
            channel_session[message.channel.id] = new_session_id
            _save_state()

        # Prepend project info if auto-detected
        if detected:
            result = f"📂 `{cwd}`\n\n{result}"

        chunks = split_message(result)
        await thinking.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await message.channel.send(chunk)
    except Exception as e:
        await thinking.edit(content=f"❌ Error: {e}")


if __name__ == "__main__":
    if not TOKEN:
        print("Error: Set DISCORD_BOT_TOKEN environment variable")
        exit(1)
    client.run(TOKEN)
