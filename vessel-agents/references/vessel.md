# vessel, for driving agent TUIs

Verified against vessel 0.17.4. `vessel <cmd> --help` is authoritative; this
file covers only what matters for hosting a coding agent, plus what cost time.

## Contents

- [The subcommands that matter](#the-subcommands-that-matter)
- [Spawning a guest](#spawning-a-guest)
- [Environment isolation](#environment-isolation)
- [Sending input](#sending-input)
- [Reading the screen](#reading-the-screen)
- [Watching, as a human](#watching-as-a-human)
- [Teardown](#teardown)
- [Verified gotchas](#verified-gotchas)

## The subcommands that matter

| command | use |
|---|---|
| `spawn --name N --cwd D --rows R --cols C -- CMD` | start a guest; prints the name |
| `list --format json` | id, pid, state, size, labels, exit code |
| `send ID TEXT --paste --enter` | a prompt: bracketed paste, then submit |
| `send-bytes ID HEX` | raw input; rarely needed now that `--paste` exists |
| `send-keys ID KEY…` | `enter`, `ctrl-c`, `up`, `escape`, … |
| `snapshot ID` | current screen (`--raw` keeps ANSI) |
| `wait ID --stable MS --contains S --pattern RE -t N` | screen-based wait, AND-combined |
| `wait ID --exited -t N` | block until the process is gone — the clean teardown check |
| `kill ID` / `kill --label L` / `kill --all` | teardown, SIGTERM then SIGKILL |
| `view` / `attach ID` | human viewers |
| `events` / `subscribe` | lifecycle JSON / raw output streams |
| `resize ID --rows R --cols C` | change size after spawn |

`events` reports *vessel* lifecycle (spawn, exit), not agent turn boundaries —
it will not tell you a guest finished thinking.

## Spawning a guest

```bash
vessel spawn --name reviewer --cwd ~/src/proj --rows 50 --cols 200 -- codex
```

**Set `--rows`/`--cols`.** The default is 24x80, which is cramped for an agent
TUI and truncates everything a screen read could tell you. 50x200 is a good
default; `vessel view` resizes agents to match tmux panes anyway (disable with
`--no-resize`).

Other flags worth knowing:

- `--label` — group guests, then `kill --label`, `subscribe --label`,
  `view --label`. The natural way to manage a fleet.
- `--timeout N` — auto-kill after N seconds (SIGTERM, then SIGKILL after 5s). A
  cheap backstop against a forgotten guest burning quota.
- `--memory-limit 4G` — cgroup-bounded; kills only that guest.
- `--max-output BYTES` — stop recording the transcript past a cap.

Names must be unique among live agents; reusing one fails rather than replacing.

## Environment isolation

Guests start from a **clean environment**. Only `PATH`, `HOME`, `USER`, `TERM`,
`SHELL`, `LANG` are inherited from the *server*, plus anything added with
`--env` / `--env-inherit`.

This solves for free the problem that a pane-based host has: an orchestrating
agent's identity variables do not reach the guest. Verified — `vessel exec -- env`
from inside a Claude Code session shows **zero** `CLAUDE*` variables, so a nested
agent does not mistake itself for a child of the caller.

Two consequences:

- The server's env is whatever was in scope when it first auto-started, so those
  six variables trace back to that process. It is a small, fixed set, but if
  `TERM` or `PATH` look wrong inside a guest, that is where they came from.
- Anything a guest genuinely needs (`ANTHROPIC_API_KEY`, proxy settings) must be
  passed explicitly with `--env` or `--env-inherit`.

## Sending input

One call does everything a prompt needs:

```bash
vessel send ID "text" --paste --enter     # bracketed paste, then submit
vessel send ID - --paste --enter          # payload from stdin
vessel send-keys ID enter ctrl-c up       # named keys
vessel send-bytes ID 1b5b3230307e…        # raw hex, rarely needed now
```

- `--paste` wraps the text in `ESC[200~` … `ESC[201~`. **Required for multi-line
  prompts**: without it the first newline submits a truncated prompt and each
  remaining line lands as its own turn — three billed turns for one prompt, none
  of them what was asked.
- `--enter` writes the submit key *separately, after a short pause*, so the TUI
  registers a keypress instead of absorbing a trailing CR into the paste as
  content. Tune with `--submit-delay-ms`.

Both behaviours are vessel's as of 0.17.5. Against an earlier version the markers
have to be hand-rolled through `send-bytes` and the Enter sent as a second
`send-keys` call after a `sleep` — on those versions `send --enter` and
`--newline` silently leave the prompt sitting in the composer.

## Reading the screen

`vessel snapshot ID` returns the rendered viewport — wrapped, truncated, and
decorated with box characters. Fine for spotting a modal, poor for parsing an
answer. Prefer `agentbus wait`, which returns the agent's own final message (see
`agentbus.md`).

`vessel dump ID` gives the recorded transcript, and `subscribe` streams output
live; both are raw PTY bytes, not structured agent output.

## Watching, as a human

vessel already has this covered — do not rebuild it:

```bash
vessel view                 # tmux, one pane per agent
vessel view --mode windows  # one tmux window per agent
vessel view --label crew    # only part of the fleet
vessel attach reviewer      # single agent, interactive
```

`view` resizes agent PTYs to match their tmux panes by default, so a guest
reflows to whatever the viewer shows. Tell the user the command; a guest they
can see and take over is far more useful than one they cannot.

## Teardown

Kill the agent process politely first, then remove it:

| agent | graceful exit |
|---|---|
| codex | `send-keys ctrl-c` twice |
| claude | `/exit` then `enter` |
| opencode | `send-keys ctrl-c` twice |

Verified: codex **ignores `/quit`** — it sits in the composer. Two Ctrl-C
presses exit it. Then `vessel kill ID` to reap the agent record.

`vessel kill --label` and `--all` exist for fleets; `--force` sends SIGKILL and
skips cleanup, so a guest loses any state it would have flushed.

## Verified gotchas

**`vessel doctor` hung once** in a non-interactive shell, producing no output
after two minutes, though it did start the server as a side effect. It has not
reproduced since — warm, cold, or with stdin closed it completes in under a
second and prints its checks. Treat it as usable but not guaranteed: to test
liveness on an automated path, `vessel list` is a plain request/response and
cannot wedge.

**The server auto-starts on first use** (`vessel server --daemon`). That means
the first command in a session may be slow, and the server outlives the caller.

**Guests keep running until killed.** Unlike a pane, nothing closes them when a
terminal goes away — that is the point, and also how quota gets burned by a
forgotten agent. `--timeout` at spawn, or an explicit kill, every time.
