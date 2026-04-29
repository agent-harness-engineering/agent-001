# Memory — Ologos Operator Temporal Continuity

This file defines *what the Ologos operator system should remember, why, and how it feeds back into reasoning*. It answers the 4M question: **"What persists across sessions, and how does it shape the next one?"**

Distinct from the other modules:
- **Mission** (`mission.md`) — stable operational context (infrastructure, team) that rarely changes
- **Mind** (`mind.md`) — consumes memory as priors for belief revision
- **Morals** (`morals.md`) — process obligations enforced by gates, not memory

---

## What Memory Is For

Memory bridges sessions and grounds reasoning. Within a session, context is live. Across sessions, only what is explicitly persisted survives.

Memory supplies the **prior formation** step in Mind's belief revision cycle. A session that reads no memory starts with training knowledge as its only prior — the lowest-weight source. A session that reads rich memory starts with high-weight, operationally grounded priors.

---

## Memory Types

Four types, each with a distinct purpose and decay rate:

### user
Who the operator is: role, goals, domain knowledge, preferences, collaboration style.
- **Decay:** slow — identity changes rarely
- **Good entry:** "JD is the CIO; deep infrastructure background, works primarily from iPhone; prefers direct answers over explanations"

### feedback
Guidance on approach — corrections and confirmations. What to avoid; what to keep doing.
- **Decay:** medium — feedback evolves as the system matures
- **Structure:** rule + **Why:** (reason given) + **How to apply:** (when it kicks in). Knowing *why* lets you judge edge cases
- **Write when:** operator corrects an approach, or confirms a non-obvious choice worked. Both matter.

### project
Ongoing work: active initiatives, key decisions, known problems, deferred items.
- **Decay:** fast — project state changes week to week
- **Structure:** fact/decision + **Why:** + **How to apply:**
- **Write when:** you learn who is doing what, why, or by when. Convert relative dates to absolute.

### reference
Pointers to where information lives in external systems.
- **Decay:** slow until the resource moves
- **Good entry:** "Pipeline bugs tracked in Linear project INGEST"
- **Verify before acting** — references can go stale

---

## What NOT to Persist

- Code patterns, architecture, file paths derivable from reading the codebase
- Git history — `git log` is authoritative
- Debugging solutions — the fix is in the code; the commit message has the context
- Anything already in `mission.md`, `mind.md`, `morals.md`
- Ephemeral task details: in-progress work, current conversation context, temporary state

If in doubt: ask what would be *non-obvious to a future session reading the system cold*. That is what memory is for.

---

## Agent-001 Memory Implementation

Agent-001 has two persistence layers:

**Session memory (in-browser, chat):** conversation history stored in browser localStorage per session. Survives page reload; does not persist across devices or browsers.

**Agent job memory (disk):** completed agent status files written to `memory/agents/{agent_id}.json`. Survives container restarts. Readable via `GET /api/agents`. This is operational state — job outcomes, outputs, errors — not operator memory.

There is no cross-session operator memory system in Agent-001. Each conversation starts fresh. Mission context (team, infrastructure, telos) is loaded from the 4M governance modules at startup and injected as system context for every model call.

---

## Retention Rules

**One entry per topic.** Update rather than duplicate.

**Entries must be actionable.** A memory that cannot change future behavior is not worth storing.

**Verify before acting.** A memory entry is a snapshot. Before acting on something a prior session recorded — a service state, a file path, a configuration — verify it still holds.

**Stale entries degrade reasoning.** When a direct observation contradicts a prior, trust the observation and note the contradiction.

---

## Handoff Contract with Mind

Memory supplies Mind's prior formation step. The source weight in Mind's belief revision:

> Session context (memory, agent results) → High weight, but provisional

When Mind's posterior (from direct observation) contradicts a prior:
1. Trust the posterior — direct observation outweighs stored belief
2. Flag the contradiction: "Earlier context said X; current observation says Y"
3. Update the prior for the remainder of the session
