# Memory: Agent-001

## Persistence Contract

Memory serves Mind by providing priors for the current session and persisting state for future sessions. Memory has no authority; it provides context.

## What Persists

### Runtime State (ephemeral, in-process)
- Active task queue and dependency graph
- Agent/endpoint health model (latency, failure counts)
- In-flight subtask status

### Session State (persists across restarts)
- Endpoint registry (model URLs, capabilities, policies)
- Task history (last N completed tasks with outcomes)
- Agent performance metrics (aggregate success rates, avg latency)
- Configuration snapshots

## Storage

v0.1 uses file-based JSON storage in a `memory/` directory:
- `memory/endpoints.json` — registered model endpoints
- `memory/task_log.jsonl` — append-only task history
- `memory/metrics.json` — aggregate performance data
- `memory/config_snapshot.json` — last-known-good configuration

Future versions may migrate to SQLite or an external store. The storage interface is abstracted to support this.

## Constraints

- **O7 compliance**: Before any flush or compaction, all in-flight state must be written.
- **No authority**: Memory informs decisions but does not make them. Stale memory is overridden by current observations.
- **Bounded size**: Task log is rotated at 10,000 entries. Metrics are rolling averages, not unbounded accumulators.

## Cross-Module Interface

```
Mind ──belief revision──> Memory (persist updated endpoint health, task outcomes)
Memory ──priors──────────> Mind (load endpoint registry, historical performance)
Morals ──constrains──────> Memory (O7: flush before compaction)
```
