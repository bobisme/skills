#!/usr/bin/env python3
"""Drive another coding agent's TUI in a zellij pane.

Guests live in a dedicated zellij session, "agents" by default, which is created
on demand. They never land in whatever session the caller happens to be in:
spawning panes into the user's working session rearranges their layout without
being asked.

Completion detection comes from agentbus, which normalises every agent's
transcript into one state machine. Screen scraping is only a fallback.

    zagent.py spawn codex --cwd /path            -> prints pane id
    zagent.py ask 6 "fix the failing test"       -> blocks, prints result JSON
    zagent.py send 6 "..." ; zagent.py wait 6    -> same, split apart
    zagent.py read 6                             -> current screen
    zagent.py status                             -> agents in the session
    zagent.py kill 6                             -> graceful quit, then close

Override the session with --session/-s or $ZAGENT_SESSION. Passing -s '' targets
the caller's current session, which is rarely what is wanted.
"""

import argparse
import json
import os
import subprocess
import sys
import time

SNAPSHOT = None  # resolved lazily; zellij's runtime dir is uid-scoped
STATE = os.path.expanduser(
    os.environ.get("AGENTBUS_STATE", "~/.local/state/agentbus")
)

# Guests get their own session so they never disturb the user's layout.
DEFAULT_SESSION = os.environ.get("ZAGENT_SESSION", "agents")
# A session with no attached client is 50x50, which reflows agent TUIs into
# unreadable ~25-column panes, so a headless client is attached to give it a
# size. Zellij sizes a session to its *smallest* attached client, so this is
# deliberately larger than any real terminal: whenever a human attaches, their
# terminal is the smaller one and the session fits itself to them, the way tmux
# behaves. Shrinking this would cap what the human sees.
DEFAULT_COLS, DEFAULT_ROWS = 500, 150
EVENTS = os.path.join(STATE, "events.jsonl")
REGISTER = os.path.join(STATE, "register.jsonl")

# How long to wait for the guest to visibly pick up a prompt before deciding it
# never landed. A prompt that vanishes into an interstitial fails here rather
# than hanging until the turn timeout.
UPTAKE_TIMEOUT = 25.0

AGENTS = {
    # exit: keystrokes that make the TUI quit on its own, tried before the pane
    # is closed out from under it.
    "codex": {"cmd": "codex", "exit": [[3], [3]]},
    "claude": {"cmd": "claude", "exit": [["/exit"], [13]]},
    "opencode": {"cmd": "opencode", "exit": [[3], [3]]},
}

# Inherited from the orchestrator, and actively misleading inside a guest: a
# nested agent that sees these believes it is a child of the caller.
SCRUB = [
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_EXECPATH",
    "AI_AGENT",
]


def snapshot_path():
    global SNAPSHOT
    if SNAPSHOT is None:
        d = f"/tmp/zellij-{os.getuid()}"
        SNAPSHOT = (
            os.path.join(d, "agentbus.json")
            if os.path.isdir(d)
            else os.path.join(STATE, "snapshot.json")
        )
    return SNAPSHOT


def zj(session, *args, check=False):
    cmd = ["zellij"]
    if session:
        cmd += ["-s", session]
    cmd += ["action", *[str(a) for a in args]]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"zellij {' '.join(args[:2])} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def list_sessions():
    """Map session name -> "live" | "exited"."""
    r = subprocess.run(
        ["zellij", "list-sessions", "-n"], capture_output=True, text=True
    )
    out = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        name = line.split()[0]
        out[name] = "exited" if "EXITED" in line else "live"
    return out


def has_client(session):
    """True when a client is attached, so the session has a usable size."""
    r = subprocess.run(
        ["zellij", "-s", session, "action", "list-clients"],
        capture_output=True,
        text=True,
    )
    rows = [l for l in r.stdout.splitlines()[1:] if l.strip()]
    return bool(rows)


def attach_headless(session, cols, rows, timeout=20.0):
    """Start a detached, correctly sized pty client and wait for it to land."""
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "zj_headless.py")
    if not os.path.exists(helper):
        raise SystemExit(f"missing helper: {helper}")
    subprocess.Popen(
        [sys.executable, helper, session, str(cols), str(rows)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # survives this process exiting
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if list_sessions().get(session) == "live" and has_client(session):
            return True
        time.sleep(0.4)
    return False


def ensure_session(session, cols=DEFAULT_COLS, rows=DEFAULT_ROWS):
    """Guarantee `session` exists and is usable. Returns what had to be done.

    An empty session name means "the caller's current session"; nothing to set
    up, and nothing that should be created behind the user's back.
    """
    if not session:
        return {"session": None, "action": "current"}

    state = list_sessions().get(session)
    if state == "exited":
        # Resurrectable, but its panes are dead placeholders and attaching
        # revives a layout nobody asked for. Start clean instead.
        subprocess.run(
            ["zellij", "delete-session", session, "--force"],
            capture_output=True,
            text=True,
        )
        state = None

    if state is None:
        if not attach_headless(session, cols, rows):
            raise SystemExit(f"could not create zellij session {session!r}")
        return {"session": session, "action": "created"}

    if not has_client(session):
        # Alive but unattached: 50x50, so guests would be unreadable.
        attach_headless(session, cols, rows)
        return {"session": session, "action": "resized"}

    return {"session": session, "action": "existing"}


def require_session(session):
    """Fail loudly rather than acting on the wrong session."""
    if not session:
        return
    if list_sessions().get(session) != "live":
        raise SystemExit(
            f"zellij session {session!r} is not running; "
            f"run `zagent.py spawn` first"
        )


def term(pane):
    """Accept 6 or terminal_6; zellij wants terminal_6, agentbus stores 6."""
    p = str(pane)
    return p if p.startswith("terminal_") else f"terminal_{p}"


def bare(pane):
    return str(pane).replace("terminal_", "")


def load_snapshot():
    try:
        with open(snapshot_path()) as f:
            return json.load(f)
    except Exception:
        return {"sessions": []}


def location_of(s):
    """Where a session lives, as (mux, mux_session, pane).

    Current agentbus publishes `location: {mux, session, pane}`. Older versions
    published `pane: {zellij_session, pane_id}` and assumed zellij. Both are read
    so the skill works either side of that change.
    """
    loc = s.get("location")
    if isinstance(loc, dict):
        return loc.get("mux", ""), loc.get("session", ""), loc.get("pane", "")
    old = s.get("pane") or {}
    return "zellij", old.get("zellij_session", ""), old.get("pane_id", "")


def find_session(zsession, pane):
    """Map a pane to the agentbus session living in it, or None."""
    for s in load_snapshot().get("sessions", []):
        mux, mux_session, pane_id = location_of(s)
        if mux and mux != "zellij":
            continue
        if pane_id == bare(pane) and (not zsession or mux_session == zsession):
            return s
    return None


def events_size():
    try:
        return os.path.getsize(EVENTS)
    except OSError:
        return 0


def events_since(offset, sid=None):
    out = []
    try:
        with open(EVENTS, "rb") as f:
            f.seek(offset)
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if sid is None or e.get("session") == sid:
                    out.append(e)
    except OSError:
        pass
    return out


def transcript_for(sid):
    """Latest registered transcript path for a session."""
    path = None
    try:
        with open(REGISTER) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("session_id") == sid and r.get("transcript"):
                    path = r["transcript"]
    except OSError:
        pass
    return path


def last_assistant_text(transcript):
    """Final assistant message in a Claude transcript.

    Claude's turn_end carries no result (it is derived from a turn_duration
    record, which has no text), so the answer has to come from here.
    """
    last = None
    try:
        with open(transcript) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") != "assistant":
                    continue
                for c in r.get("message", {}).get("content", []):
                    if c.get("type") == "text" and c.get("text", "").strip():
                        last = c["text"]
    except OSError:
        pass
    return last


# ---------------------------------------------------------------- spawn


def spawn(args):
    spec = AGENTS.get(args.agent)
    if not spec and not args.cmd:
        raise SystemExit(f"unknown agent {args.agent!r}; pass --cmd")
    setup = ensure_session(args.session, args.cols, args.rows)
    inner = args.cmd or spec["cmd"]
    if args.args:
        inner += " " + " ".join(f"'{a}'" for a in args.args)
    scrub = " ".join(f"-u {v}" for v in SCRUB)
    # A login shell so the guest gets the same PATH/env a human would, and env -u
    # so it does not inherit the orchestrator's agent identity.
    launch = ["/bin/bash", "-lc", f"exec env {scrub} {inner}"]

    name = args.name or f"guest-{args.agent}"
    cwd = [os.path.abspath(args.cwd)] if args.cwd else []

    if args.floating or args.split:
        # Splitting divides the session between guests: four of them get a
        # quarter of the width each, and TUIs reflow into soup. Deliberate
        # choice only.
        cmd = ["new-pane", "--name", name]
        if cwd:
            cmd += ["--cwd", cwd[0]]
        if args.floating:
            cmd.append("--floating")
        pane = zj(args.session, *cmd, "--", *launch, check=True)
        pane = pane.splitlines()[-1].strip()
    else:
        # One tab per guest: every guest keeps the full session geometry no
        # matter how many are running.
        cmd = ["new-tab", "--name", name]
        if cwd:
            cmd += ["--cwd", cwd[0]]
        tab = zj(args.session, *cmd, "--", *launch, check=True)
        tab = tab.splitlines()[-1].strip()
        pane = pane_in_tab(args.session, tab)

    if not pane or not pane.startswith("terminal_"):
        raise SystemExit(f"could not determine new pane id (got {pane!r})")

    ready = wait_ready(args.session, pane, args.ready_timeout)
    print(
        json.dumps(
            {
                "pane": pane,
                "bare": bare(pane),
                "ready": ready,
                "session": args.session or "(current)",
                "session_action": setup["action"],
            }
        )
    )


def pane_in_tab(session, tab_id, timeout=10.0):
    """The terminal pane of a freshly created tab.

    new-tab returns a tab id, not a pane id, so the pane has to be looked up.
    The tab's plugin panes (tab-bar, status-bar) are skipped.

    Parsed from JSON, not the table: tab names contain spaces ("Tab #1"), so
    splitting the table on whitespace misaligns every column after it.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            panes = json.loads(zj(session, "list-panes", "-j"))
        except Exception:
            panes = []
        for p in panes:
            if p.get("is_plugin") or p.get("is_floating"):
                continue
            if str(p.get("tab_id")) == str(tab_id):
                return f"terminal_{p['id']}"
        time.sleep(0.3)
    return None


TRUST_MARKERS = (
    "do you trust",
    "yes, i trust this folder",
    "trust the contents",
    "yes, continue",
)


def wait_ready(zsession, pane, timeout):
    """Wait until the TUI has drawn, clearing first-run interstitials.

    Both codex and claude open a directory-trust prompt in a directory they have
    not seen before. A prompt sent while that is up is swallowed silently.
    """
    deadline = time.time() + timeout
    cleared = 0
    while time.time() < deadline:
        screen = zj(zsession, "dump-screen", "-p", pane)
        low = screen.lower()
        if any(m in low for m in TRUST_MARKERS) and cleared < 3:
            zj(zsession, "write", "-p", pane, 13)
            cleared += 1
            time.sleep(2.0)
            continue
        if screen.strip():
            # Drawn and not on a known interstitial. Give the composer a beat.
            time.sleep(1.0)
            return True
        time.sleep(1.0)
    return False


# ---------------------------------------------------------------- send


def paste(zsession, pane, text):
    """Deliver text via bracketed paste, so newlines do not submit early."""
    zj(zsession, "write", "-p", pane, 27, 91, 50, 48, 48, 126)  # ESC[200~
    zj(zsession, "write-chars", "-p", pane, text)
    zj(zsession, "write", "-p", pane, 27, 91, 50, 48, 49, 126)  # ESC[201~
    time.sleep(0.4)


def submit(zsession, pane):
    zj(zsession, "write", "-p", pane, 13)


def send(args):
    require_session(args.session)
    text = args.text if args.text != "-" else sys.stdin.read()
    paste(args.session, term(args.pane), text)
    if not args.no_submit:
        submit(args.session, term(args.pane))
    print(json.dumps({"pane": term(args.pane), "sent": True}))


# ---------------------------------------------------------------- wait


BUSY = ("working", "blocked")


def wait_turn(zsession, pane, offset, timeout, uptake=UPTAKE_TIMEOUT):
    """Block until the guest finishes the turn that started after `offset`.

    Two phases on purpose. Waiting only for "idle" is a race: the guest is still
    idle for the moment between submitting and starting, so the wait returns
    instantly with the *previous* turn's answer. Uptake must be observed first.
    """
    sid = None
    t0 = time.time()
    saw_busy = False

    while time.time() - t0 < uptake:
        s = find_session(zsession, pane)
        if s:
            sid = s["session"]
            if s.get("state") in BUSY:
                saw_busy = True
                break
            # Some agents finish a trivial turn between polls; the event log
            # still records it, so treat fresh activity as uptake.
            if any(
                e.get("kind") in ("prompt", "tool", "turn_end")
                for e in events_since(offset, sid)
            ):
                saw_busy = True
                break
        time.sleep(0.3)

    if not saw_busy:
        return {
            "status": "no_uptake",
            "session": sid,
            "detail": "prompt never registered; check the screen for a modal",
        }

    while time.time() - t0 < timeout:
        s = find_session(zsession, pane)
        if s is None:
            time.sleep(0.4)
            continue
        sid = s["session"]
        state = s.get("state")
        if state == "blocked":
            return {
                "status": "blocked",
                "session": sid,
                "detail": s.get("detail", ""),
                "screen_tail": tail_screen(zsession, pane),
            }
        if state == "idle":
            return {
                "status": "done",
                "session": sid,
                "result": harvest(zsession, pane, sid, offset),
            }
        time.sleep(0.4)

    return {"status": "timeout", "session": sid}


def harvest(zsession, pane, sid, offset):
    """Best available text for the turn, in descending order of trust."""
    for e in reversed(events_since(offset, sid)):
        if e.get("kind") == "turn_end" and e.get("result"):
            return e["result"]  # codex publishes the answer directly
    t = transcript_for(sid)
    if t:
        text = last_assistant_text(t)
        if text:
            return text  # claude: the transcript is the only structured source
    return tail_screen(zsession, pane)


def tail_screen(zsession, pane, lines=40):
    screen = zj(zsession, "dump-screen", "-p", term(pane))
    keep = [l for l in screen.splitlines() if l.strip()]
    return "\n".join(keep[-lines:])


def cmd_wait(args):
    require_session(args.session)
    r = wait_turn(args.session, term(args.pane), 0, args.timeout, uptake=1e9)
    print(json.dumps(r, indent=2))


def cmd_ask(args):
    require_session(args.session)
    text = args.text if args.text != "-" else sys.stdin.read()
    pane = term(args.pane)
    paste(args.session, pane, text)
    offset = events_size()  # captured before submit: the log only grows
    submit(args.session, pane)
    r = wait_turn(args.session, pane, offset, args.timeout)
    print(json.dumps(r, indent=2))
    if r["status"] != "done":
        sys.exit(1)


# ---------------------------------------------------------------- misc


def cmd_read(args):
    require_session(args.session)
    print(tail_screen(args.session, args.pane, args.lines))


def cmd_status(args):
    rows = []
    for s in load_snapshot().get("sessions", []):
        mux, mux_session, pane_id = location_of(s)
        # Default to the session being orchestrated; the machine-wide view is
        # noisy and mostly other people's work.
        if not args.all and args.session and mux_session != args.session:
            continue
        rows.append(
            {
                # Full id, not a prefix: Codex session ids are UUIDv7, so their
                # leading characters are a shared timestamp and two concurrent
                # agents look identical when truncated.
                "session": s["session"],
                "source": s.get("source"),
                "state": s.get("state"),
                "pane": f"{mux_session}/{pane_id}",
                "cwd": s.get("cwd", ""),
                "label": s.get("label", "")[:60],
            }
        )
    print(json.dumps(rows, indent=2))


def cmd_kill(args):
    require_session(args.session)
    pane = term(args.pane)
    agent = args.agent
    spec = AGENTS.get(agent, {})
    for step in spec.get("exit", []):
        if isinstance(step[0], str):
            zj(args.session, "write-chars", "-p", pane, step[0])
        else:
            zj(args.session, "write", "-p", pane, *step)
        time.sleep(1.0)
    time.sleep(1.5)
    zj(args.session, "close-pane", "-p", pane)
    print(json.dumps({"pane": pane, "closed": True}))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--session", "-s", default=DEFAULT_SESSION,
        help=f"zellij session for guests, created on demand "
             f"(default: {DEFAULT_SESSION!r}; '' means the current session)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("spawn")
    p.add_argument("agent")
    p.add_argument("--cmd", help="override the launch command")
    p.add_argument("--cwd")
    p.add_argument("--name")
    p.add_argument("--floating", action="store_true",
                   help="floating pane; polite when targeting a session a "
                        "human is using")
    p.add_argument("--split", action="store_true",
                   help="split the current tab instead of opening a new one; "
                        "shrinks every guest as more are added")
    p.add_argument("--ready-timeout", type=float, default=45.0)
    p.add_argument("--cols", type=int, default=DEFAULT_COLS,
                   help="width for a newly created session")
    p.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    p.add_argument("args", nargs="*")
    p.set_defaults(func=spawn)

    p = sub.add_parser("send")
    p.add_argument("pane")
    p.add_argument("text")
    p.add_argument("--no-submit", action="store_true")
    p.set_defaults(func=send)

    p = sub.add_parser("ask")
    p.add_argument("pane")
    p.add_argument("text")
    p.add_argument("--timeout", type=float, default=900.0)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("wait")
    p.add_argument("pane")
    p.add_argument("--timeout", type=float, default=900.0)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("read")
    p.add_argument("pane")
    p.add_argument("--lines", type=int, default=40)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("status")
    p.add_argument("--all", action="store_true",
                   help="every agent on the machine, not just this session")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("kill")
    p.add_argument("pane")
    p.add_argument("--agent", default="codex")
    p.set_defaults(func=cmd_kill)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
