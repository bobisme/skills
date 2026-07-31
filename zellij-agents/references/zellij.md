# zellij pane mechanics for driving TUIs

Verified against zellij 0.44.3. Check `zellij --version`; the `--pane-id` flags
below are the load-bearing ones and are not present in older releases.

## Contents

- [Targeting a pane without stealing focus](#targeting-a-pane-without-stealing-focus)
- [Spawning](#spawning)
- [Sending input](#sending-input)
- [Reading the screen](#reading-the-screen)
- [Per-agent launch and exit](#per-agent-launch-and-exit)
- [Background sessions](#background-sessions)
- [Verified gotchas](#verified-gotchas)

## Targeting a pane without stealing focus

`write`, `write-chars` and `dump-screen` all take `-p/--pane-id`. Use it
always. Without it they act on the *focused* pane, which means driving a guest
would yank the user's cursor around and race with whatever they are typing.

```bash
zellij -s SESSION action write-chars -p terminal_3 "text"
zellij -s SESSION action dump-screen  -p terminal_3
```

`-s SESSION` targets a session other than the current one; omit it inside the
session being driven.

Pane ids are `terminal_N` / `plugin_N`, and the two are **separate id spaces** —
`terminal_1` and `plugin_1` both exist. agentbus stores the bare `N`.

## Spawning

```bash
zellij action new-pane --name guest --cwd /path -- /bin/bash -lc 'exec codex'
# prints: terminal_3
```

`new-pane` prints the created pane id on stdout — that is the handle for
everything after.

Useful flags: `--floating` (does not disturb the tiled layout), `--close-on-exit`,
`--in-place`. `new-tab` also accepts a command but returns a *tab* id, not a pane
id, so it needs a `list-panes` lookup afterwards.

Wrapping in `/bin/bash -lc 'exec …'` gets the guest a login shell's environment.
`exec` matters: it keeps the pane's process *being* the agent, so
`list-panes -c` shows the real command and the pane dies when the agent does.

**Scrub inherited identity.** The new pane inherits the environment of whoever
ran `zellij action`, which when the caller is an agent includes `CLAUDECODE=1`,
`CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_PID`, `AI_AGENT`.

```bash
-- /bin/bash -lc 'exec env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID … codex'
```

## Sending input

Two commands: `write-chars` for text, `write` for raw decimal bytes.

```bash
zellij action write -p terminal_3 13          # Enter (CR)
zellij action write -p terminal_3 3           # Ctrl-C
zellij action write -p terminal_3 27          # Escape
```

**Multi-line prompts require bracketed paste**, or the first newline submits a
truncated prompt and the rest lands as separate turns:

```bash
zellij action write -p terminal_3 27 91 50 48 48 126   # ESC[200~  paste start
zellij action write-chars -p terminal_3 "$(cat prompt.txt)"
zellij action write -p terminal_3 27 91 50 48 49 126   # ESC[201~  paste end
sleep 0.4
zellij action write -p terminal_3 13                   # submit
```

Verified: a 3-line prompt lands in the composer as three lines, unsubmitted,
then submits as one turn. Leave a short pause before Enter — the TUI processes
the paste asynchronously.

## Reading the screen

```bash
zellij action dump-screen -p terminal_3          # viewport
zellij action dump-screen -p terminal_3 --full   # + scrollback
zellij action dump-screen -p terminal_3 --ansi   # keep styling
```

Works on alternate-screen TUIs (verified with vim and codex), so full-screen
agents are readable. It is still a *rendered* view: wrapped, truncated with `…`,
and decorated. Prefer transcripts and events for anything that must be parsed.

An empty dump usually means the TUI has not drawn yet, not that it is broken.

Other useful reads:

```bash
zellij action list-panes -c    # + running command (real argv)
zellij action list-panes -s    # + focused / floating / EXITED
zellij action list-panes -j    # JSON
```

`EXITED=true` confirms a guest actually quit. Note `PaneInfo.terminal_command` is
`None` in plugin APIs; the CLI's `-c` is the reliable path.

## Per-agent launch and exit

| | launch | auto-approval | exit |
|---|---|---|---|
| codex | `codex` (optional prompt argv) | `--full-auto`, or `--dangerously-bypass-approvals-and-sandbox` | **Ctrl-C twice** |
| claude | `claude` | `--permission-mode acceptEdits`, or `--dangerously-skip-permissions` | `/exit` + Enter |
| opencode | `opencode` | — | Ctrl-C twice |

Verified: codex **ignores `/quit`** — it sits in the composer unsubmitted. Two
Ctrl-C presses exit it cleanly. Claude exits on `/exit`.

Always follow a graceful exit with `close-pane -p terminal_N`, and confirm via
`list-panes -s`. Closing the pane alone kills the process without letting the
agent flush state.

Auto-approval flags trade safety for not blocking. Only use them with the user's
agreement, and never in a directory whose contents are untrusted — the guest is
reading files that can contain instructions aimed at it.

## Background sessions

```bash
zellij attach -b NAME                     # create detached
zellij -s NAME action …                   # drive it
zellij delete-session NAME --force        # tear down
```

A detached session has **no client, so zellij gives it a 50x50 default**. Split
between two panes that is ~25 columns, at which agent TUIs reflow into
unreadable soup — verified. Attach a correctly sized pty client first:

```bash
python3 scripts/zj_headless.py NAME 200 50 &
```

The pty must be drained continuously or it fills and the zellij client blocks,
freezing every pane in the session. `zj_headless.py` does this.

Deleting the session kills every guest in it; quit them first if their state
matters.

## Verified gotchas

**First-run interstitials swallow prompts.** codex and claude both open a
directory-trust prompt in an unfamiliar directory. Nothing about the pane, the
process, or agentbus indicates this — the guest simply never registers a turn.
Dump the screen after spawn and clear modals before prompting. This is the single
most likely cause of a silent hang.

**`zellij pipe` delivers each message twice** — once with the payload, once with
`payload: None` to signal close, carrying the same pipe name. Relevant when
talking to plugins rather than panes; handle both or every command runs twice.

**A failed plugin instance is retained for the session.** After fixing a load
error, the same URL can keep serving the broken instance; copy the wasm to a new
path or restart the session when testing.

**`/proc/<pid>/environ` needs ptrace access; `/proc/<pid>/stat` does not.** Under
`kernel.yama.ptrace_scope=1` only a descendant can read another process's
environment, so reading `ZELLIJ_PANE_ID` back out of a running agent works when
tested by hand from inside its pane and fails as a service. Identify processes by
pid + start time from `stat` instead.
