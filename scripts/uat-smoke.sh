#!/usr/bin/env bash
# UAT smoke test -- 8 scenarios that exercise agent-001 the way an operator
# actually uses it (pronoun queries, indirect intent, failure paths, verbose
# replies). Each scenario gets a PASS/FAIL with grep-able verdicts.
#
# Designed to be the gate I should have run BEFORE declaring "OK verified" on
# the chat-path / memory / ecosystem-visibility ships of 2026-04-30. Single-
# turn smoke tests with leading prompts caught none of those bugs.
#
# Usage:
#   scripts/uat-smoke.sh                                    # default -- http://localhost:8095
#   AGENT_URL=http://100.100.214.61:8095 scripts/uat-smoke.sh   # over Tailscale
#
# Exit code: 0 if all scenarios PASS, 1 otherwise.

set -uo pipefail

AGENT_URL="${AGENT_URL:-http://localhost:8095}"
WAIT_AGENT_SEC="${WAIT_AGENT_SEC:-90}"   # max wait per agent completion
PASS_COUNT=0
FAIL_COUNT=0
FAILED_SCENARIOS=()

color_red()   { printf '\033[31m%s\033[0m' "$*"; }
color_green() { printf '\033[32m%s\033[0m' "$*"; }
color_yellow(){ printf '\033[33m%s\033[0m' "$*"; }

header() {
  echo
  echo "================================================================"
  echo "  $1"
  echo "================================================================"
}

verdict() {
  local label="$1"
  local result="$2"
  local detail="${3:-}"
  if [ "$result" = "PASS" ]; then
    PASS_COUNT=$((PASS_COUNT + 1))
    echo "  $(color_green "[PASS]") $label"
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
    FAILED_SCENARIOS+=("$label")
    echo "  $(color_red "[FAIL]") $label"
    [ -n "$detail" ] && echo "         $detail"
  fi
}

post_chat() {
  # $1 = message; prints the .response field
  local msg="$1"
  curl -sf -X POST "$AGENT_URL/chat" \
    -H "Content-Type: application/json" \
    --data "$(jq -nc --arg m "$msg" '{message:$m, history:[]}')" \
    | jq -r '.response // ""'
}

post_spawn() {
  # $1 = prompt, $2 = type; prints agent_id
  local prompt="$1"
  local atype="$2"
  curl -sf -X POST "$AGENT_URL/api/agents/spawn" \
    -H "Content-Type: application/json" \
    --data "$(jq -nc --arg p "$prompt" --arg t "$atype" '{prompt:$p, agent_type:$t}')" \
    | jq -r '.agent_id // ""'
}

wait_for_agent() {
  # $1 = agent_id; returns 0 on completed/failed, 1 on timeout
  local aid="$1"
  local i=0
  local max=$((WAIT_AGENT_SEC / 5))
  while [ $i -lt $max ]; do
    local s
    s=$(curl -sf "$AGENT_URL/api/agents" \
      | jq -r --arg id "$aid" '.agents[] | select(.agent_id==$id) | .status' 2>/dev/null)
    case "$s" in
      completed|failed|interrupted) return 0 ;;
    esac
    sleep 5
    i=$((i + 1))
  done
  return 1
}

agent_output() {
  # $1 = agent_id; prints output text. Reads from disk via docker exec.
  local aid="$1"
  ssh -i ~/.ssh/ologosai_backup -o StrictHostKeyChecking=no thinxai@100.100.214.61 \
    "docker exec agent-001-agent-001-1 python3 -c \"import json; print(json.load(open('/app/memory/agents/$aid.json')).get('output',''))\"" 2>/dev/null \
    || echo ""
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
header "Pre-flight: $AGENT_URL"
if ! curl -sf "$AGENT_URL/health" >/dev/null; then
  echo "  $(color_red "FATAL") $AGENT_URL/health unreachable"
  exit 2
fi
echo "  $(color_green "OK") health OK"

# ---------------------------------------------------------------------------
# Scenario 1 -- Direct spawn intent (sanity baseline)
# ---------------------------------------------------------------------------
header "S1 -- Direct spawn intent: 'spawn a research agent...'"
BEFORE_COUNT=$(curl -sf "$AGENT_URL/api/agents" | jq -r '.agents | length')
REPLY=$(post_chat "Please spawn a research agent to summarize the population of Iceland.")
sleep 7
AFTER_COUNT=$(curl -sf "$AGENT_URL/api/agents" | jq -r '.agents | length')
DELTA=$((AFTER_COUNT - BEFORE_COUNT))
echo "  agents before=$BEFORE_COUNT after=$AFTER_COUNT delta=$DELTA"
if [ "$DELTA" -ge 1 ]; then
  verdict "S1 spawn dispatched" "PASS"
else
  verdict "S1 spawn dispatched" "FAIL" "no new agent within 7s of direct spawn request"
fi

# ---------------------------------------------------------------------------
# Scenario 2 -- Indirect spawn intent (the Turn-7 case I missed)
# ---------------------------------------------------------------------------
header "S2 -- Indirect spawn intent: 'kick off a quick health check'"
BEFORE_COUNT=$(curl -sf "$AGENT_URL/api/agents" | jq -r '.agents | length')
REPLY=$(post_chat "Could you kick off a quick health check on the ecosystem and let me know what you find?")
sleep 7
AFTER_COUNT=$(curl -sf "$AGENT_URL/api/agents" | jq -r '.agents | length')
DELTA=$((AFTER_COUNT - BEFORE_COUNT))
echo "  agents before=$BEFORE_COUNT after=$AFTER_COUNT delta=$DELTA"
if [ "$DELTA" -ge 1 ]; then
  verdict "S2 indirect spawn dispatched" "PASS"
else
  verdict "S2 indirect spawn dispatched" "FAIL" "model wrote prose but emitted no <spawn> tag and client-side regex didn't match"
fi

# ---------------------------------------------------------------------------
# Scenario 3 -- Pronoun-query recall (the Turn-4 bug)
# ---------------------------------------------------------------------------
header "S3 -- Pronoun query recall: spawn -> wait -> 'Results?'"
S3_AID=$(post_spawn "What is the capital of Iceland? Answer in one sentence." "research")
echo "  spawned: $S3_AID"
if wait_for_agent "$S3_AID"; then
  REPLY=$(post_chat "Results?")
  echo "  reply (first 200): ${REPLY:0:200}"
  if echo "$REPLY" | grep -qiE "(reykjavik|reykjavík|$S3_AID)"; then
    verdict "S3 pronoun query surfaces latest agent result" "PASS"
  else
    verdict "S3 pronoun query surfaces latest agent result" "FAIL" "reply did not cite Reykjavik or the agent_id"
  fi
else
  verdict "S3 pronoun query surfaces latest agent result" "FAIL" "agent $S3_AID timed out before completing"
fi

# ---------------------------------------------------------------------------
# Scenario 4 -- Spawned-agent governance leak (the Turn-7 bug)
# ---------------------------------------------------------------------------
header "S4 -- Spawned-agent governance leak: agent must NOT emit <spawn> tags"
S4_AID=$(post_spawn "Spawn a sub-agent to investigate something." "general")
echo "  spawned: $S4_AID"
if wait_for_agent "$S4_AID"; then
  OUT=$(agent_output "$S4_AID")
  echo "  output (first 200): ${OUT:0:200}"
  # Only a REAL spawn tag has the form <spawn type="..."> -- mere mentions of
  # the word "<spawn>" inside backticks (in a refusal message) don't count.
  if echo "$OUT" | grep -qiE "<spawn[[:space:]]+type=[\"']"; then
    verdict "S4 spawned agent does NOT emit <spawn> tags" "FAIL" "output contains a real <spawn type=...> tag -- chat governance is leaking into agent system prompt"
  else
    verdict "S4 spawned agent does NOT emit <spawn> tags" "PASS"
  fi
else
  verdict "S4 spawned agent does NOT emit <spawn> tags" "FAIL" "agent $S4_AID timed out"
fi

# ---------------------------------------------------------------------------
# Scenario 5 -- Ecosystem visibility (regression check on the 2026-04-30 ship)
# ---------------------------------------------------------------------------
header "S5 -- Ecosystem visibility: status agent must cite real probe data"
S5_AID=$(post_spawn "Provide a complete systems assessment using ONLY the live readings injected into your context. Cite specific HTTP codes and latencies." "status")
echo "  spawned: $S5_AID"
if wait_for_agent "$S5_AID"; then
  OUT=$(agent_output "$S5_AID")
  THIS_YEAR=$(date -u +%Y)
  HAS_HTTP=$(echo "$OUT" | grep -cE "[23][0-9][0-9].*ms" || true)
  HAS_YEAR=$(echo "$OUT" | grep -c "$THIS_YEAR" || true)
  HAS_OLD_DATE=$(echo "$OUT" | grep -cE "202[34]" || true)
  echo "  has http+latency rows: $HAS_HTTP, has $THIS_YEAR: $HAS_YEAR, has old-date drift (2023/2024): $HAS_OLD_DATE"
  if [ "$HAS_HTTP" -ge 5 ] && [ "$HAS_YEAR" -ge 1 ] && [ "$HAS_OLD_DATE" -eq 0 ]; then
    verdict "S5 status agent cites real probes + correct UTC" "PASS"
  else
    verdict "S5 status agent cites real probes + correct UTC" "FAIL" "missing probe rows, wrong year, or training-date drift detected"
  fi
else
  verdict "S5 status agent cites real probes + correct UTC" "FAIL" "agent $S5_AID timed out"
fi

# ---------------------------------------------------------------------------
# Scenario 6 -- Topical match vs recency (the Turn-10 bug)
# ---------------------------------------------------------------------------
header "S6 -- Topical match: discuss the AGENT we just spawned, not unrelated newest"
S6_AID=$(post_spawn "What is the speed of light in m/s? One sentence." "research")
if wait_for_agent "$S6_AID"; then
  # Now spawn an unrelated agent so it becomes the "newest by recency"
  DECOY_AID=$(post_spawn "What is the boiling point of water? One sentence." "research")
  wait_for_agent "$DECOY_AID" >/dev/null
  REPLY=$(post_chat "What did the speed-of-light research agent find?")
  echo "  reply (first 250): ${REPLY:0:250}"
  if echo "$REPLY" | grep -qE "299[, ]?792[, ]?458|3[ ]?[xX×*][ ]?10\^?8|3.*10.*8"; then
    verdict "S6 topical match overrides pure recency" "PASS"
  else
    verdict "S6 topical match overrides pure recency" "FAIL" "reply discussed wrong agent (recency-confusion)"
  fi
else
  verdict "S6 topical match overrides pure recency" "FAIL" "primary agent timed out"
fi

# ---------------------------------------------------------------------------
# Scenario 7 -- Output truncation (the Turn-12 bug)
# ---------------------------------------------------------------------------
header "S7 -- Verbose reply not truncated mid-sentence"
REPLY=$(post_chat "Give me a complete self-assessment with all sections fully detailed: epistemic status, reasoning mode, memory state, capabilities, and known limitations. Do not abbreviate.")
LEN=${#REPLY}
LAST_CHAR="${REPLY: -1}"
echo "  reply length: $LEN chars, last char: '${LAST_CHAR}'"
MIDSENTENCE_RE='[a-zA-Z]'
if [ "$LEN" -lt 100 ]; then
  verdict "S7 verbose reply complete" "FAIL" "reply too short: $LEN chars"
elif [[ "$LAST_CHAR" =~ $MIDSENTENCE_RE ]]; then
  verdict "S7 verbose reply complete" "FAIL" "reply ends mid-sentence on '$LAST_CHAR' -- likely max_tokens hit"
else
  verdict "S7 verbose reply complete" "PASS"
fi

# ---------------------------------------------------------------------------
# Scenario 8 -- Failed-agent telemetry (the Turn-8 bug)
# ---------------------------------------------------------------------------
header "S8 -- Failed-agent visibility: poison agent -> chat must acknowledge failure"
# Force a failure by spawning against a non-existent endpoint name
S8_AID=$(curl -sf -X POST "$AGENT_URL/api/agents/spawn" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"This will fail.", "agent_type":"general", "endpoint":"nonexistent_endpoint"}' \
  | jq -r '.agent_id // ""')
if [ -z "$S8_AID" ]; then
  # Endpoint resolution may reject the request entirely -- that's a different
  # path; mark advisory rather than fail the whole suite
  verdict "S8 failed-agent surfaced in chat" "FAIL" "could not provoke a failure (endpoint validation rejected the spawn outright)"
else
  echo "  spawned: $S8_AID"
  wait_for_agent "$S8_AID" >/dev/null
  REPLY=$(post_chat "Was there a recent agent failure? If so, give me the agent_id and reason.")
  echo "  reply (first 250): ${REPLY:0:250}"
  if echo "$REPLY" | grep -q "$S8_AID"; then
    verdict "S8 failed-agent surfaced in chat" "PASS"
  else
    verdict "S8 failed-agent surfaced in chat" "FAIL" "chat does not see the failed agent -- likely save-on-block gap"
  fi
fi

# ---------------------------------------------------------------------------
# Final tally
# ---------------------------------------------------------------------------
header "RESULT"
echo "  PASS: $PASS_COUNT"
echo "  FAIL: $FAIL_COUNT"
if [ "$FAIL_COUNT" -gt 0 ]; then
  echo
  echo "  Failed scenarios:"
  for s in "${FAILED_SCENARIOS[@]}"; do
    echo "    - $s"
  done
  exit 1
fi
echo
echo "  $(color_green "All scenarios passed.")"
exit 0
