#!/usr/bin/env python3
"""Attach a headless client to a zellij session so its panes have a real size.

A session created with `zellij attach -b` has no client, so zellij gives it a
50x50 default and splits that between panes. Agent TUIs reflow to ~25 columns
and become unreadable and unscrapable. Attaching a pty of a chosen size fixes
it without putting anything on the user's screen.

    zj_headless.py orchestration 200 50 &

Runs until killed. The pty is drained continuously; a full pty would block the
zellij client and freeze every pane in the session.
"""

import fcntl
import os
import pty
import struct
import sys
import termios


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: zj_headless.py <session> [cols] [rows]")
    session = sys.argv[1]
    cols = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    rows = int(sys.argv[3]) if len(sys.argv) > 3 else 50

    pid, fd = pty.fork()
    if pid == 0:
        os.environ["ZELLIJ_AUTO_ATTACH"] = "false"
        os.execvp("zellij", ["zellij", "attach", "--create", session])

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    print(pid, flush=True)
    while True:
        try:
            if not os.read(fd, 65536):
                break
        except OSError:
            break


if __name__ == "__main__":
    main()
