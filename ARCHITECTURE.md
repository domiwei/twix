# Discord Claude Bot - Monitor Architecture

## Design Philosophy

Based on [Harness Design for Long-Running Apps](https://www.anthropic.com/engineering/harness-design-long-running-apps):

- **Context Reset > Context Compaction**: Each step gets a fresh Claude session, avoiding coherence loss from accumulated tool-call noise
- **Structured Artifacts as Handoff**: Steps communicate via JSON, not raw text blobs
- **Separate Collection from Evaluation**: The agent that runs commands should not be the same one that judges the results (GAN-inspired pattern)

## Monitor Check Flow

```
┌─────────────────────────────────────────┐
│  Step 1: Collect (Opus, with tools)     │
│  - Runs SSH/curl/API commands           │
│  - Returns structured JSON:             │
│    {metrics, signals, raw_notes}        │
│  - signals: Opus-level observations     │
│    spotted during collection            │
└──────────────┬──────────────────────────┘
               │ structured artifact
               ▼
┌─────────────────────────────────────────┐
│  Step 2: Store in History               │
│  - Rolling window of 36 entries (~3hr)  │
│  - JSON, not raw text                   │
└──────────────┬──────────────────────────┘
               │ last 6 readings
               ▼
┌─────────────────────────────────────────┐
│  Step 3: Judge Tier 1 (Haiku, no tools) │
│  - Receives structured data only        │
│  - --max-turns 1 (no tool calls)        │
│  - Returns: {anomaly, summary, details, │
│              confidence, lesson}         │
└──────────────┬──────────────────────────┘
               │
               ▼ confidence == "low"?
        ┌──────┴──────┐
        │ yes         │ no
        ▼             ▼
┌───────────────┐   Done
│  Tier 2: Opus │
│  (no tools)   │
│  Deep analysis│
│  with Haiku's │
│  initial take │
└───────┬───────┘
        │
        ▼
      Done
```

## Why This Design

### Problem: Context Pollution
Previously, a single Opus session ran SSH commands (12+ tool calls), then judged the results in the same context. The accumulated tool-call noise degraded judgment quality.

### Problem: Self-Evaluation Bias
The same agent that collected data also decided if it was anomalous. Separating collector from evaluator produces more honest assessments.

### Solution: Three Clean Contexts

| Step | Model | Tools | Context |
|------|-------|-------|---------|
| Collect | Opus | Yes (SSH, curl) | Fresh session, only collection instructions |
| Judge T1 | Haiku | None | Fresh session, only structured metrics + knowledge |
| Judge T2 | Opus | None | Fresh session, only structured metrics + Haiku's take |

### Cost Optimization
- Most checks are "all normal" — Haiku handles these at ~1/10 the cost
- Only ambiguous cases escalate to Opus (confidence: low)
- Collection still uses Opus because it needs reasoning to figure out the right commands

## Intent Classification

A separate Haiku call classifies user messages into intents before routing. Key design decisions:

- One-time status checks ("check the status") route to `chat`, not `monitor_add`
- When user replies to a monitor's webhook message, context tells the classifier the target is the monitor itself (so "restart" = `monitor_resume`, not `restart` the service)
- `monitor_remove` checks `pending_monitors` first, so users can cancel setups that haven't been confirmed yet

## Multi-Agent Harness (GAN-inspired)

Based on [Harness Design for Long-Running Apps](https://www.anthropic.com/engineering/harness-design-long-running-apps), the bot now uses a Generator-Evaluator architecture for all code-related tasks.

### Evaluator (Auto Review)

```
┌─────────────────────────────────────────────────────┐
│  User sends message                                 │
└──────────────┬──────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────┐
│  Generator (Opus) produces response                 │
└──────────────┬──────────────────────────────────────┘
               │
               ▼ used Edit/Write/Bash?
        ┌──────┴──────┐
        │ yes         │ no
        ▼             ▼
┌───────────────┐  ┌──────────────────────┐
│  Auto Review  │  │  Show [🔍 Review]    │
│  (Sonnet)     │  │  button — user       │
│               │  │  decides             │
└───────┬───────┘  └──────────────────────┘
        │
        ▼ PASS?
    ┌───┴───┐
    │yes    │no
    ▼       ▼
  Done   ┌───────────────────────────────┐
         │  Generator resumes session    │
         │  with Evaluator's feedback    │
         │  → fixes → Evaluator re-     │
         │  reviews (up to 3 rounds)     │
         └───────────────────────────────┘
```

Key design:
- Generator **resumes the same session** (`--resume`) when fixing — retains full context, only addresses feedback
- Evaluator is always a **separate, fresh session** — avoids self-evaluation bias
- Max 3 rounds (`MAX_GEN_EVAL_ROUNDS`), then warns for manual check
- Evaluator uses Sonnet (cheaper, sufficient for review)

### Context Relay (Debug Sessions)

```
┌────────────────────┐
│  Turns 1-30        │  Normal session
│  (same session_id) │
└────────┬───────────┘
         │ cumulative turns >= 30
         ▼
┌────────────────────────────────────────┐
│  Auto-generate debug summary (Haiku)   │
│  - Problem description                 │
│  - What was tried + results            │
│  - What was ruled out                  │
│  - Current leads                       │
│  - Modified files                      │
└────────┬───────────────────────────────┘
         │ "🔄 Context 已接力"
         ▼
┌────────────────────────────────────────┐
│  New session with summary injected     │
│  Keeps last 5 round summaries          │
│  Turns reset to 0                      │
└────────────────────────────────────────┘
```

Solves **context anxiety** — instead of one session degrading as context fills, each round gets a clean slate with structured handoff.

- Threshold: `CONTEXT_RELAY_THRESHOLD = 30` turns
- `!reset` clears all debug context

### Planner (Sprint Contract)

For large tasks, `!plan <description>` activates a three-agent pipeline:

```
┌─────────────────────────────────────────────────────┐
│  !plan 為 ePBS 加上 builder bid 驗證機制             │
└──────────────┬──────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────┐
│  Planner (Opus) reads codebase → Sprint Contract    │
│  {steps, depends_on, acceptance criteria}           │
└──────────────┬──────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────┐
│  Display plan + [✅ 開始] [✏️ 修改] [❌ 取消]         │
│  User can iterate on the plan before executing      │
└──────────────┬──────────────────────────────────────┘
               ▼ ✅ confirmed
┌─────────────────────────────────────────────────────┐
│  Dependency resolver:                               │
│  - depends_on: [] → run in parallel (diff Pikmin)   │
│  - depends_on: [1,2] → wait, then run serial        │
│                                                     │
│  Each step:                                         │
│    Generator executes → Evaluator reviews           │
│    FAIL → dialogue loop (up to 3 rounds)            │
│    PASS → next step                                 │
└─────────────────────────────────────────────────────┘
               ▼
  🏁 Sprint Contract 完成！
```

Example parallel execution:
```
⚡ 並行執行:
  🔴 紅皮 → Step 1: users API (worktree A)
  🔵 藍皮 → Step 2: products API (worktree B)
  🟡 黃皮 → Step 3: auth middleware (worktree C)

串行（等 1,2,3 完成）:
  🟣 紫皮 → Step 4: integration tests
```

## Usage Examples

### Scenario 0: Monitoring (監控)

Automated health checks with tiered judgment.

```
（每 5 分鐘自動執行）

🐛 蟲皮 (erigon 監控)：⚠️ erigon 監控偵測到異常

  ┌─ Collect (Opus) ─────────────────────────┐
  │ SSH 進主機，跑 docker stats、看 log、     │
  │ 查 sync 進度 → 產出結構化 JSON            │
  └──────────────────────────────────────────┘
           ↓
  ┌─ Judge T1 (Haiku) ──────────────────────┐
  │ 「記憶體從 12GB 漲到 28GB，confidence:   │
  │  low — 不確定是正常 sync 還是 leak」      │
  └──────────────────────────────────────────┘
           ↓ confidence: low → 升級
  ┌─ Judge T2 (Opus) ───────────────────────┐
  │ 「對比歷史數據，sync 階段記憶體通常在     │
  │  15-20GB。28GB 偏高，而且持續增長，       │
  │  可能是 memory leak。建議檢查最近的       │
  │  commit 有沒有改到 cache 相關邏輯」       │
  └──────────────────────────────────────────┘

🐛 蟲皮：⚠️ erigon 監控偵測到異常
  （自動開 incident thread）

🐛 蟲皮：記憶體異常增長（28GB，正常約 15-20GB）
         疑似 memory leak。最近的 commit 改了
         cache 邏輯，建議檢查。
         如果要修，說「修吧」，我會開 branch 來處理。
```

After several normal checks, auto-resolve:

```
🐛 蟲皮：✅ 連續多次檢查正常，問題已解決。關閉此 thread。
```

Knowledge accumulation — the monitor learns from each check:

```
蟲皮 accumulated knowledge:
  1. "erigon sync 階段記憶體通常在 15-20GB"
  2. "peer disconnection log 是正常現象，不需告警"
  3. "CPU 在 stage execution 時會到 90%+，屬正常"
```

### Scenario 1: Discussion (討論)

Ask a question, optionally request a second opinion.

```
你：ePBS 的 builder bid 驗證流程是怎麼運作的？

🔴 紅皮：beacon node 收到 bid 後會經過三個階段...
         1. 驗證 builder 身份（檢查 pubkey 是否在 registry）
         2. 驗證 bid amount 是否合理（> 最低門檻）
         3. ...

         [🔍 召喚 Review]     ← 你覺得不太確定，按一下

🔵 藍皮：🔍 Review 中...
🔵 藍皮：紅皮的回答大致正確，但漏了一點 ——
         第 2 步不只看 amount，還要驗證 bid 的
         slot 跟 parent_hash 是否對應當前 head。
         可以看 cl/forkchoice/bid_validation.go:142
```

### Scenario 2: Debug (除錯)

Bug fix with auto-review and Generator-Evaluator dialogue.

```
你：sync 卡在 slot 8420311，log 顯示 "missing execution payload envelope"

🔴 紅皮：⚙️ 執行中...
🔴 紅皮：找到問題了。cl/stages/forward_sync.go:287
         在收到 beacon block 後直接呼叫 processPayload，
         但 envelope 可能還沒到。改成 defer + 加入
         pending queue，等 envelope 到了再處理。
         ✏️ 編輯 forward_sync.go
         ✏️ 編輯 pending_blocks.go

🔵 藍皮：🔍 Review 中...
🔵 藍皮：fix 方向正確，但有個問題 —— pending queue
         沒有設 TTL，如果 envelope 永遠不來，這些
         block 會一直堆在記憶體裡。→ FAIL

🔴 紅皮：🔧 根據回饋修正中... (第 2/3 輪)
🔴 紅皮：加了 TTL 機制，pending 超過 30 秒自動丟棄
         並 log warning。
         ✏️ 編輯 pending_blocks.go

🔵 藍皮：🔍 Re-review 中... (第 2/3 輪)
🔵 藍皮：✅ PASS — TTL 合理，丟棄時有 log 可追蹤。
```

Context relay kicks in after extended debugging:

```
（來回 debug 了好幾輪，累計 30 turns）

🔄 Context 已接力 — 開啟新 session，保留之前的 debug 紀錄。

你：剛才改完跑了還是有問題，新的 log 是 "parent hash mismatch"

🔴 紅皮：根據之前的紀錄，已經修了 missing envelope 的問題
         並加了 pending queue + TTL。這次的 parent hash
         mismatch 應該是另一個問題，讓我看看...
         （從乾淨的 context 繼續，但帶著之前的摘要）
```

### Scenario 3: Writing Code (大型開發任務)

Use `!plan` for multi-step feature development.

```
你：!plan 在 erigon 加上 ePBS builder bid 驗證機制

📋 Planner 分析中...

## 📋 Sprint Contract
**為 erigon 實作 ePBS builder bid 驗證機制**

1. 定義 BidValidation 介面與資料結構 ⚡ 可並行
   驗收：struct 與 interface 定義完成，編譯通過

2. 實作 bid 簽章驗證邏輯 ⚡ 可並行
   驗收：unit test 覆蓋 valid/invalid/expired 三種 case

3. 整合到 forkchoice 的 block processing 流程 (依賴 1, 2)
   驗收：收到 block 時自動驗證 bid，無效 bid 被拒絕

4. 加入 beacon API endpoint (依賴 1)
   驗收：GET /eth/v1/builder/bids 回傳正確 JSON

5. 寫整合測試 (依賴 3, 4)
   驗收：go test ./cl/... 全部通過

請確認計畫：
[✅ 開始執行] [✏️ 修改] [❌ 取消]

你：按 ✏️
你：步驟 2 加上 benchmark，我想知道驗證的 latency

📋 修改計畫中...
（Planner 重新產出包含 benchmark 的計畫）
[✅ 開始執行] [✏️ 修改] [❌ 取消]

你：按 ✅

🚀 開始執行 Sprint Contract — 共 5 個步驟
⚡ 並行執行 2 個步驟: 定義資料結構, 實作簽章驗證

🔴 紅皮：步驟 1 ⚙️ 定義 BidValidation 介面...
🟡 黃皮：步驟 2 ⚙️ 實作 bid 簽章驗證...

🔴 紅皮：（完成 struct 定義）
🔵 藍皮：步驟 1 🔍 驗收... ✅ PASS

🟡 黃皮：（完成驗證邏輯 + benchmark）
🔵 藍皮：步驟 2 🔍 驗收...
         「benchmark 只測了 happy path，要加上
          invalid signature 的 case」→ FAIL
🟡 黃皮：🔧 根據回饋修正中...
🟡 黃皮：（補上 invalid case benchmark）
🔵 藍皮：🔍 Re-review... ✅ PASS

🟣 紫皮：步驟 3 ⚙️ 整合到 forkchoice...
...

🏁 Sprint Contract 完成！ 5/5 步驟已完成。
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_MODEL` | `claude-opus-4-6` | Generator / main chat model |
| `EVALUATOR_MODEL` | `claude-sonnet-4-6` | Evaluator review model |
| `PLANNER_MODEL` | `claude-opus-4-6` | Planner model |
| `SUMMARY_MODEL` | `claude-haiku-4-5-20251001` | Thread summaries / debug relay summaries |
| `JUDGE_MODEL` | `claude-haiku-4-5-20251001` | Monitor Tier 1 judgment |
| `INTENT_MODEL` | `claude-haiku-4-5-20251001` | Intent classification |
