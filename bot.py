import discord
import asyncio
import os
import json
import re
import subprocess
import time

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
channel_workdir: dict[int, str] = {}
channel_session: dict[int, str | None] = {}

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


def wants_thread(text: str) -> bool:
    """Detect if user wants to start a thread via natural language."""
    return bool(THREAD_PATTERN.search(text))


def detect_project(text: str) -> str | None:
    """Try to detect a project name from the message text."""
    projects = list_projects()
    text_lower = text.lower()
    for proj in projects:
        if proj.lower() in text_lower:
            return proj
    return None


async def run_claude(prompt: str, cwd: str, session_id: str | None = None) -> tuple[str, str | None]:
    """Run claude CLI with the given prompt in the specified directory."""
    cmd = [
        "claude",
        "-p", prompt,
        "--system-prompt", SYSTEM_PROMPT,
        "--output-format", "json",
        "--verbose",
    ]
    if session_id:
        cmd.extend(["--resume", session_id])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    raw = stdout.decode("utf-8", errors="replace").strip()

    # If resume failed (session not found), retry without --resume
    if session_id and proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace")
        if "No conversation found" in stderr_text:
            cmd_retry = [
                "claude",
                "-p", prompt,
                "--system-prompt", SYSTEM_PROMPT,
                "--output-format", "json",
                "--verbose",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd_retry,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            raw = stdout.decode("utf-8", errors="replace").strip()

    new_session_id = None
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            # Find the result entry
            result = ""
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "result":
                    result = entry.get("result", "")
                    new_session_id = entry.get("session_id")
            # If result is empty, collect tool outputs and assistant text
            if not result:
                parts = []
                for entry in data:
                    if not isinstance(entry, dict):
                        continue
                    # Assistant text messages
                    if entry.get("type") == "assistant" and entry.get("message"):
                        msg = entry["message"]
                        for block in msg.get("content", []):
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text" and block.get("text"):
                                parts.append(block["text"])
                    # Tool results
                    if entry.get("type") == "tool_result":
                        content = entry.get("content", "")
                        if isinstance(content, str) and content:
                            parts.append(content)
                        elif isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                                    parts.append(item["text"])
                result = "\n".join(parts) if parts else raw
        elif isinstance(data, dict):
            result = data.get("result", raw)
            new_session_id = data.get("session_id")
        else:
            result = raw
    except (json.JSONDecodeError, TypeError):
        result = raw

    if not result:
        if stderr:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            if err_text:
                result = f"(stderr: {err_text[:500]})"

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
bot_threads: set[int] = set()

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
        await message.reply(f"✅ 工作目錄：`{path}`")
        return

    # !reset
    if content == "!reset":
        channel_session[message.channel.id] = None
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
        except Exception as e:
            await message.reply(f"❌ 無法建立 thread: {e}")
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

    cwd = channel_workdir.get(message.channel.id, WORK_ROOT)
    session_id = channel_session.get(message.channel.id)

    thinking = await message.reply("⏳ 處理中...")

    try:
        result, new_session_id = await run_claude(prompt, cwd, session_id)
        if new_session_id:
            channel_session[message.channel.id] = new_session_id

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
