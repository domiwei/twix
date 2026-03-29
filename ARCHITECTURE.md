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

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_MODEL` | `claude-opus-4-6` | Model for collection and chat |
| `JUDGE_MODEL` | `claude-haiku-4-5-20251001` | Model for Tier 1 judgment |
| `INTENT_MODEL` | `claude-haiku-4-5-20251001` | Model for intent classification |
