# agentbus: the completion-detection contract

Everything here was verified against agentbus as of 2026-07-30. If behaviour
disagrees with this file, trust the running system and check
`~/src/zcode/crates/agentbus/src/event.rs`, which defines the vocabulary.

## Contents

- [The three files](#the-three-files)
- [Snapshot schema](#snapshot-schema)
- [Binding a pane to a session](#binding-a-pane-to-a-session)
- [Event schema](#event-schema)
- [Per-agent differences](#per-agent-differences-the-important-part)
- [Extracting a result](#extracting-a-result)
- [The race, precisely](#the-race-precisely)
- [Running without agentbus](#running-without-agentbus)

## The three files

| Path | Shape | Use |
|---|---|---|
| `/tmp/zellij-$UID/agentbus.json` | single JSON object, rewritten atomically | current state of every agent |
| `~/.local/state/agentbus/events.jsonl` | append-only JSONL | turn boundaries, results, history |
| `~/.local/state/agentbus/register.jsonl` | append-only JSONL | session to pane to transcript path |

The snapshot is written to zellij's uid-scoped tmp dir so the `herd` plugin can
read it from inside the WASI sandbox. If zellij is not running, agentbus falls
back to `~/.local/state/agentbus/snapshot.json`.

The snapshot is rewritten only when its rendered content *differs*, so an
unchanged mtime does not mean the observer is dead.

## Snapshot schema

```json
{"version": 1, "sessions": [{
  "session": "019fb57a-91cc-7e82-8210-64fefb9803df",
  "source": "codex",              // codex | claude | hook
  "state": "idle",                // working | idle | blocked
  "title": "", "label": "What word did you just say?",
  "cwd": "/home/bob/src/myproject",
  "model": "openai", "last_tool": "exec",
  "detail": "",                   // why blocked, when blocked
  "tokens": {"output": 2121, "context": 68080},
  "last_activity": "2026-07-30T23:21:49.636Z",
  "pane": {"zellij_session": "zatest", "pane_id": "1"},
  "subagents": [{"id": "...", "state": "done", "result": "...", "description": "..."}]
}]}
```

**The location field was renamed.** Current agentbus publishes

```json
"location": {"mux": "zellij", "session": "agents", "pane": "4"}
```

where older versions published `"pane": {"zellij_session": …, "pane_id": …}`.
`mux` exists because agentbus now recognises tmux, wezterm and kitty too, so a
subscriber must check it rather than assume zellij. `zagent.py location_of()`
reads both shapes; a consumer that reads only the old one silently binds nothing
and every wait reports `no_uptake`.

Sessions also now carry `state_since`, `tools` and `effort`.

Notes that matter:

- `pane` is the **bare number** (`"4"`), while zellij's CLI wants `terminal_4`.
  Convert in both directions.
- It is empty until a hook has reported the binding, and is cleared again as
  soon as the owning process dies — so empty means "not currently in a known
  pane", not "unknown forever".
- **There is deliberately no `done` state.** A finished agent is `idle`. The
  distinction between "finished your work" and "waiting for your next prompt"
  does not exist at this layer; it is recovered by watching the *transition*.
- `blocked` only ever arrives from a hook or the screen. No transcript records a
  permission prompt, because approval is UI state.
- Finished `subagents` expire from the snapshot 90s after completion.

## Binding a pane to a session

There is no way to ask an agent for its session id. Go the other way: find the
snapshot session whose `pane.zellij_session` and `pane.pane_id` match the pane
just spawned.

**A guest has no session at all until its first turn.** Codex writes no rollout
until prompted, and the register hook fires on prompt submit. So: spawn, send the
first prompt, *then* poll for the binding. Do not treat "no session for my pane"
before the first prompt as an error.

`register.jsonl` carries the same mapping plus the transcript path, and is
re-read wholesale — later lines win.

## Event schema

One JSON object per line: `ts`, `source`, `session`, `kind`, plus per-kind fields.

| kind | fields | meaning |
|---|---|---|
| `session` | `title`, `cwd`, `model` | identity, restated when it changes |
| `prompt` | `text` | user submitted; starts a turn (empty text = bare turn marker) |
| `label` | `text` | what the task is, with **no claim about the turn** |
| `turn_end` | `duration_ms`, `result` | turn finished |
| `tool` | `name` | tool call; implies working |
| `tokens` | `output`, `context` | `output` accumulates, `context` is a level |
| `reported` | `state`, `detail` | asserted by an agent's own hook |
| `subagent` | `id`, `state`, `agent_type`, `description`, `result` | nested agent |

`label` exists specifically because Claude restates its last prompt *after* the
turn-end record. Treating that restatement as a new turn pins a session to
"working" forever. **Never derive turn start from `label`.**

## Per-agent differences

Current agentbus gives both agents the same shape: `prompt` opens a turn,
`turn_end` closes it carrying the answer.

Against an **older agentbus** they diverged, and the divergence broke naive
consumers:

|  | codex | claude (old) |
|---|---|---|
| emits `prompt` | yes | **no** |
| `turn_end.result` | the answer text | **always null** |
| turn start visible as | `prompt` | `reported state=working` (hook) |

A wait loop gated on `prompt` **hung forever on a Claude guest** — observed in
testing, where Claude answered in ~1s and the loop still timed out at 120s.

Gate on the snapshot's normalised `state` regardless. It costs nothing, works on
both versions, and folds in the `reported` states from hooks — the only source
that ever says `blocked`.

**Never truncate a session id to compare or display it.** Codex uses UUIDv7, so
the leading characters are a creation timestamp: two guests started seconds apart
share a long prefix and look identical when shortened.

## Extracting a result

In descending order of trust:

1. **`turn_end.result` from the event log**, filtered to events appended after
   the prompt was submitted. Works for codex. Empty for Claude.
2. **The last assistant text message in the transcript.** Get the path from
   `register.jsonl` (`transcript` field), then read the JSONL and keep the last
   record with `type == "assistant"` whose `message.content[]` has a non-empty
   `{"type": "text"}` block. This is the only structured source for Claude.
3. **`zellij action dump-screen -p terminal_N`.** Always available, but it is a
   rendered viewport: wrapped, truncated, decorated with box characters, and
   missing anything scrolled away. Use `--full` for scrollback. Last resort.

`scripts/zagent.py:harvest()` implements exactly this ladder.

## The race, precisely

```
t0  submit prompt          snapshot.state == "idle"   (from the PREVIOUS turn)
t1  guest starts           snapshot.state == "working"
t2  guest finishes         snapshot.state == "idle"
```

Polling for `idle` between t0 and t1 returns immediately, with the previous
turn's answer. The window is small but real — a trivial codex turn was measured
completing in **1.1s**, so a 1s poll interval straddles the entire turn.

Two defences, use both:

1. **Two-phase wait.** Require an observed busy state (or fresh post-offset
   activity in the event log) before accepting `idle`.
2. **Offset the event log before submitting.** `os.path.getsize(events.jsonl)`
   is a cheap watermark; only read forward from it. The log is append-only, so
   this is stable.

## Running without agentbus

Degraded, and worth saying so to the user. Poll `dump-screen` and watch for the
agent's idle affordance — codex and claude both render a composer prompt line
when accepting input, and a spinner or status line while working.

Failure modes to expect, all of which agentbus exists to avoid:

- A pane that stops changing because the agent is **blocked on approval** reads
  identically to one that finished.
- An agent thinking silently between tool calls looks finished.
- An agent echoing prompt-like text into its output produces a false idle.
- Screen matching is inherently version-fragile; every TUI redesign breaks it.

Match only on frames actually observed. A guessed rule matches ordinary prose and
lies convincingly.
