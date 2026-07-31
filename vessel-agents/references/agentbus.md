# agentbus, as a consumer sees it

Verified against the agentbus that added `sessions` and `wait`. `agentbus --help`
is authoritative; trust the running system over this file.

## Contents

- [The two reading verbs](#the-two-reading-verbs)
- [What --pid resolves](#what---pid-resolves)
- [agy is the odd one](#agy-is-the-odd-one)
- [The --since window](#the---since-window)
- [When there is no pid](#when-there-is-no-pid)
- [Underneath: state, events, files](#underneath-state-events-files)
- [History worth knowing](#history-worth-knowing)

## The two reading verbs

```bash
agentbus sessions [--pid N] [--session S] [--cwd D] [--json]
agentbus wait (--session S | --pid N) [--timeout SECS] [--since EPOCH] [--json]
```

`sessions --json` prints a JSON **array**. Each entry carries `session`,
`source`, `state`, `cwd`, `label`, `model`, `effort`, `last_tool`, `tools`,
`detail`, `tokens`, `last_activity`, `state_since`, `subagents`, and — the two
that matter for a supervisor — **`pid`** and **`transcript`**.

`wait` blocks until the current-or-next turn ends and prints
`{status, session, result, duration_ms}`. **`result` is the full final message**,
newlines preserved. Exit codes: `0` done, `3` blocked, `4` timeout, `1` session
could not be resolved.

Prefer these over reading the snapshot file. Doing so also keeps the snapshot
schema out of your code, which is where breakage lands — when the session shape
changed, a file-reading consumer did not error, it silently bound nothing and
every wait timed out while the agent sat there having already answered.

## What `--pid` resolves

`--pid` matches the named process **or any descendant of it**, which is what
lets a supervisor pass the pid it spawned without knowing how the agent is
launched:

| agent | pid vessel spawned | pid agentbus registered |
|---|---|---|
| claude | 1014041 | 1014041 — the same process |
| agy | 2073922 | 2073922 — the same process (a single Go binary) |
| codex | 1016689 (`node …/codex` shim) | 1016952 (the real binary, its child) |

Verified: `agentbus sessions --pid <vessel's pid>` resolves the codex session
whose registered pid is one level below. Two guests in the *same* working
directory bind to distinct sessions, so a fleet may share a repo.

The registration is written on the guest's **first prompt**, so a freshly
spawned guest resolves to nothing until it has taken one. That is the normal
cause of exit code 1 on a first `wait`.

## The `--since` window

`wait` never reports a turn that ended before the wait began. That is the right
default — it is what stops a caller from collecting the *previous* turn's answer
— but it means a turn that starts and finishes in the gap between sending a
prompt and calling `wait` is missed.

The gap is real: a trivial codex turn was measured at 1.1s end to end.

So mark the moment before sending, and pass it:

```bash
t=$(date +%s)
vessel send guest "…" --paste --enter
agentbus wait --pid "$pid" --since "$t" --json
```

This is also what makes fan-out safe across separate processes: each guest's `t`
is just a shell variable, where a hand-rolled waiter would have to persist a
byte offset to a file to survive between `send` and `wait`.

## agy is the odd one

agy (Antigravity CLI, Gemini) is observed from a plain JSONL transcript at
`~/.gemini/antigravity-cli/brain/<conversationId>/…/transcript.jsonl`, written
for every conversation whether or not hooks exist, so it is *discovered* without
cooperation. Its session id is that conversation id.

What it never writes is a **turn boundary**, and one is not derivable — agentbus
found that the obvious rule (prose with no tool calls, agy's own `NO_TOOL_CALL`
stop reason) fires 13 times across a 7-turn conversation, because the model
narrates between tool batches. So the `Stop` hook carries it.

The consequence for a supervisor: **without the hooks, `agentbus wait` on an agy
guest can only time out.** Sessions appear, carry prompts and tool calls, and
stay `working` forever. Verified here by correlation — every agy session with
activity before the hooks were installed is still `working`; ones after reach
`idle`.

Hooks live at `~/.gemini/config/hooks.json`, installed by `just sync-agy`. Not
`.agents/` — that is a customization root for skills and rules and is never read
for hooks, so a file placed there looks right and silently never fires.

Two smaller differences: agy's `wait` result carries no `duration_ms`, since the
boundary comes from a hook rather than a timed record; and its cwd and model
reach the bus only through that hook payload, never from the transcript.

## When there is no pid

An agent that reports through an integration rather than `agentbus hook
register` — OpenCode — publishes no pid, so `--pid` cannot resolve it. Fall back
to `--cwd`, and accept its limits:

```bash
agentbus sessions --cwd /path --json
```

Two such guests in one directory are indistinguishable. Give each its own
working directory, or bind them one at a time.

## Underneath: state, events, files

Worth knowing when debugging, not for normal use.

| path | shape |
|---|---|
| `~/.local/state/agentbus/snapshot.json` | current state (also mirrored where a plugin can read it) |
| `~/.local/state/agentbus/events.jsonl` | append-only event log |
| `~/.local/state/agentbus/register.jsonl` | session → pid, transcript, mux/pane |

States are `working`, `idle`, `blocked`. **There is deliberately no `done`** — a
finished agent is one you have not given the next thing to yet, which is idle;
the distinction is recovered by watching the transition, which is exactly what
`wait` does for you.

`blocked` only ever arrives from a hook or the screen, because no transcript
records a permission prompt — approval is UI state.

Event kinds: `session`, `prompt`, `label`, `turn_end`, `tool`, `tokens`,
`reported`, `subagent`. `label` carries no claim about the turn: Claude restates
its last prompt *after* the turn ends, so deriving turn start from it pins a
session to "working" forever.

## History worth knowing

If a machine is running an older agentbus, these were true and shaped a lot of
consumer code:

- `sessions` and `wait` did not exist; consumers read the snapshot file and
  hand-rolled the state machine.
- `register_session()` returned early unless `ZELLIJ_PANE_ID` was set, so
  vessel-hosted guests never registered and identity had to be inferred from
  cwd and timing.
- Claude emitted no `prompt` event, so a wait gated on it hung forever —
  measured, a 1s answer against a 120s timeout.
- `turn_end.result` was a `one_line(text, 160)` preview meant for a status line,
  so any consumer preferring it silently truncated real answers; the full text
  had to come from the transcript, with per-agent record shapes to sniff.
