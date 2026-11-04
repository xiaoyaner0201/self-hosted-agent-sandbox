"""``sbx``: run commands for an agent session inside a sandbox claimed from your warm pool.

  sbx exec   --session ID [--cwd DIR] [--keep] -- COMMAND...   run one command (host-routing applies)
  sbx shell  --session ID [--cwd DIR]                          read commands from stdin, one per line
  sbx release --session ID                                     delete the session's claim

Configuration comes from AGENT_SANDBOX_* environment variables (KUBECONFIG, POOL, NAMESPACE,
CODE_DIRS, HOST_COMMANDS, ENV_SKIP, SYNC_PATHS, IDLE_TTL, ...); see the README.
``--keep`` keeps the claim for the next ``sbx exec --session ID`` (persistent mode), so a
sequence of calls shares one sandbox until ``sbx release``.
"""
from __future__ import annotations

import argparse
import os
import shlex
import sys

from .core import Sandbox


def _open(a, persistent: bool) -> Sandbox:
    cfg = {"agent_sandbox_persistent": "true"} if persistent else {}
    return Sandbox(cwd=os.path.abspath(a.cwd), timeout=a.timeout, session=a.session, config=cfg, app=a.app)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sbx", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("exec", "shell", "release"):
        p = sub.add_parser(name)
        p.add_argument("--session", required=True)
        p.add_argument("--cwd", default=os.getcwd())
        p.add_argument("--timeout", type=int, default=600)
        p.add_argument("--app", default="sbx")
        if name == "exec":
            p.add_argument("--keep", action="store_true", help="keep the claim for later calls")
            p.add_argument("command", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    if a.cmd == "release":
        try:
            n = Sandbox.release(a.session)
        except RuntimeError as e:
            print(e, file=sys.stderr)
            return 1
        print(f"released {n} claim(s)", file=sys.stderr)
        return 0
    if a.cmd == "exec":
        cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
        if not cmd:
            ap.error("exec needs a command")
        command = cmd[0] if len(cmd) == 1 else shlex.join(cmd)
        sb = _open(a, persistent=a.keep)
        try:
            r = sb.execute(command, os.path.abspath(a.cwd), timeout=a.timeout, stream=sys.stdout.buffer)
        finally:
            sys.stdout.flush()
            sb.close()
        return r["returncode"]
    sb = _open(a, persistent=False)
    rc = 0
    try:
        for line in sys.stdin:
            line = line.rstrip("\n")
            if line.strip():
                rc = sb.execute(line, sb.cwd, timeout=a.timeout, stream=sys.stdout.buffer)["returncode"]
                sys.stdout.flush()
    finally:
        sb.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
