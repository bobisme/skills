#!/usr/bin/env python3
"""Drive another coding agent's TUI in a zellij pane.

Completion detection comes from agentbus, which normalises every agent's
transcript into one state machine. Screen scraping is only a fallback.

    zagent.py spawn codex --cwd /path            -> prints pane id
    zagent.py ask 6 "fix the failing test"       -> blocks, prints result JSON
    zagent.py send 6 "..." ; zagent.py wait 6    -> same, split apart
    zagent.py read 6                             -> current screen
    zagent.py status                             -> every agent agentbus can see
    zagent.py kill 6                             -> graceful quit, then close
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


def find_session(zsession, pane):
    """Map a pane to the agentbus session living in it, or None."""
    for s in load_snapshot().get("sessions", []):
        p = s.get("pane", {})
        if p.get("pane_id") == bare(pane) and (
            not zsession or p.get("zellij_session") == zsession
        ):
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
    inner = args.cmd or spec["cmd"]
    if args.args:
        inner += " " + " ".join(f"'{a}'" for a in args.args)
    scrub = " ".join(f"-u {v}" for v in SCRUB)
    # A login shell so the guest gets the same PATH/env a human would, and env -u
    # so it does not inherit the orchestrator's agent identity.
    launch = ["/bin/bash", "-lc", f"exec env {scrub} {inner}"]

    cmd = ["new-pane", "--name", args.name or f"guest-{args.agent}"]
    if args.cwd:
        cmd += ["--cwd", os.path.abspath(args.cwd)]
    if args.floating:
        cmd.append("--floating")
    cmd += ["--", *launch]
    pane = zj(args.session, *cmd, check=True).splitlines()[-1].strip()
    if not pane.startswith("terminal_"):
        raise SystemExit(f"unexpected new-pane output: {pane!r}")

    ready = wait_ready(args.session, pane, args.ready_timeout)
    print(json.dumps({"pane": pane, "bare": bare(pane), "ready": ready}))


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
    r = wait_turn(args.session, term(args.pane), 0, args.timeout, uptake=1e9)
    print(json.dumps(r, indent=2))


def cmd_ask(args):
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
    print(tail_screen(args.session, args.pane, args.lines))


def cmd_status(args):
    rows = []
    for s in load_snapshot().get("sessions", []):
        p = s.get("pane", {})
        rows.append(
            {
                "session": s["session"][:8],
                "source": s.get("source"),
                "state": s.get("state"),
                "pane": f"{p.get('zellij_session','')}/{p.get('pane_id','')}",
                "cwd": s.get("cwd", ""),
                "label": s.get("label", "")[:60],
            }
        )
    print(json.dumps(rows, indent=2))


def cmd_kill(args):
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
    ap.add_argument("--session", "-s", default=os.environ.get("ZAGENT_SESSION"),
                    help="zellij session (default: current)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("spawn")
    p.add_argument("agent")
    p.add_argument("--cmd", help="override the launch command")
    p.add_argument("--cwd")
    p.add_argument("--name")
    p.add_argument("--floating", action="store_true")
    p.add_argument("--ready-timeout", type=float, default=45.0)
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
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("kill")
    p.add_argument("pane")
    p.add_argument("--agent", default="codex")
    p.set_defaults(func=cmd_kill)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
