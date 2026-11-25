"""Offline checks of opt-in git checkpoints and fork. No cluster, no Docker.
Run: PYTHONPATH=src python3 tests/checkpoint_unit.py

The sandbox side is replaced by running each command locally in the code dir (the
``run_in_sandbox`` adapter hook), so pull-back is a no-op and the host repo is real git.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from self_hosted_agent_sandbox import Sandbox, cli
from self_hosted_agent_sandbox import core


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                           check=True).stdout.strip()


def local(command, cwd, timeout):
    r = subprocess.run(["bash", "-c", command], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return {"output": r.stdout + r.stderr, "returncode": r.returncode}


def offline_init(self):  # stands in for claim + push; the "sandbox" is the host dir itself
    self._pod = None
    self._file_sync = None


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sbx-ckpt-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "Dev")
        git(self.repo, "config", "user.email", "dev@example.com")
        (self.repo / "a.txt").write_text("one\n")
        (self.repo / ".gitignore").write_text("*.log\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        (self.repo / "staged.txt").write_text("staged\n")
        git(self.repo, "add", "staged.txt")  # user has a staged change: index must survive
        p = patch.object(core.Sandbox, "_initialize", offline_init)
        p.start()
        self.addCleanup(p.stop)
        self.env = patch.dict(os.environ, {"AGENT_SANDBOX_CHECKPOINT": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def sandbox(self, on, session="ck-a", cwd=None):
        cfg = {"agent_sandbox_code_dirs": [str(cwd or self.repo)]}
        if on is not None:
            cfg["agent_sandbox_checkpoint"] = on
        return Sandbox(cwd=str(cwd or self.repo), session=session, config=cfg)

    def run_cmd(self, sb, command, cwd=None):
        return sb.execute(command, str(cwd or self.repo), run_in_sandbox=local)

    def user_state(self):
        gitdir = self.repo / ".git"
        return (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "symbolic-ref", "HEAD"),
                (gitdir / "index").read_bytes(), git(self.repo, "for-each-ref", "refs/heads", "refs/tags"),
                git(self.repo, "--no-optional-locks", "status", "--porcelain"))  # read-only probe

    def sbx_refs(self):
        out = git(self.repo, "for-each-ref", "--format=%(refname)", "refs/sbx")
        return out.splitlines() if out else []

    def test_off_by_default_result_and_refs_unchanged(self):
        for on in (None, False, "false"):
            r = self.run_cmd(self.sandbox(on), "echo hi > new.txt")
            self.assertEqual(set(r), {"output", "returncode"}, r)
            self.assertEqual(r["returncode"], 0)
        self.assertEqual(self.sbx_refs(), [])

    def test_two_executes_make_two_chained_refs_without_touching_user_state(self):
        head = git(self.repo, "rev-parse", "HEAD")
        sb = self.sandbox(True)
        before = self.user_state()
        r1 = self.run_cmd(sb, "echo two > a.txt; echo u > untracked.txt; mkdir -p node_modules/x; echo dep > node_modules/x/i.js; echo l > x.log")
        state1 = self.user_state()
        r2 = self.run_cmd(sb, "rm untracked.txt; echo three > a.txt")
        self.assertEqual(r1["returncode"], 0)
        for r in (r1, r2):
            self.assertRegex(r["checkpoint"], r"^[0-9a-f]{40}$")
        self.assertEqual(self.sbx_refs(), ["refs/sbx/ck-a/000001", "refs/sbx/ck-a/000002"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/sbx/ck-a/000001"), r1["checkpoint"])
        self.assertEqual(git(self.repo, "rev-parse", f"{r1['checkpoint']}^"), head)
        self.assertEqual(git(self.repo, "rev-parse", f"{r2['checkpoint']}^"), r1["checkpoint"])
        # HEAD, branch, index bytes and tags: untouched by recording
        self.assertEqual(state1[:4], before[:4])
        self.assertEqual(self.user_state()[:4], before[:4])
        files1 = git(self.repo, "ls-tree", "-r", "--name-only", r1["checkpoint"]).splitlines()
        self.assertIn("untracked.txt", files1)   # untracked new file is in the checkpoint
        self.assertIn("staged.txt", files1)
        self.assertNotIn("x.log", files1)        # .gitignore'd
        self.assertFalse(any(f.startswith("node_modules/") for f in files1))  # sync skip dir
        self.assertEqual(git(self.repo, "show", f"{r1['checkpoint']}:a.txt"), "two")
        files2 = git(self.repo, "ls-tree", "-r", "--name-only", r2["checkpoint"]).splitlines()
        self.assertNotIn("untracked.txt", files2)
        self.assertEqual(git(self.repo, "show", f"{r2['checkpoint']}:a.txt"), "three")
        self.assertEqual([c["sha"] for c in sb.checkpoints()], [r1["checkpoint"], r2["checkpoint"]])

    def test_failed_command_is_still_recorded(self):
        r = self.run_cmd(self.sandbox("true"), "echo partial > a.txt; exit 3")
        self.assertEqual(r["returncode"], 3)
        self.assertEqual(git(self.repo, "show", f"{r['checkpoint']}:a.txt"), "partial")

    def test_code_dir_below_repo_top_checkpoints_whole_repo(self):
        sub = self.repo / "pkg"
        sub.mkdir()
        sb = self.sandbox(True, cwd=sub)
        r = self.run_cmd(sb, "echo s > s.txt", cwd=sub)
        self.assertIn("pkg/s.txt", git(self.repo, "ls-tree", "-r", "--name-only", r["checkpoint"]).splitlines())

    def test_not_a_repo_reports_none(self):
        plain = self.root / "plain"
        plain.mkdir()
        r = self.run_cmd(self.sandbox(True, cwd=plain), "echo x > f", cwd=plain)
        self.assertIn("checkpoint", r)
        self.assertIsNone(r["checkpoint"])

    def test_strict_pull_failure_raises_and_best_effort_skips_checkpoint(self):
        sb = self.sandbox(True)
        sb._pull_code = lambda: (_ for _ in ()).throw(RuntimeError("cut"))
        r = self.run_cmd(sb, "echo x > a.txt")
        self.assertNotIn("checkpoint", r)  # a tree that may miss the command's edits is not recorded
        self.assertEqual(self.sbx_refs(), [])

    def test_fork_checks_out_exact_checkpoint_as_new_sandbox(self):
        sb = self.sandbox(True)
        r1 = self.run_cmd(sb, "echo two > a.txt; mkdir d; echo deep > d/n.txt")
        self.run_cmd(sb, "echo three > a.txt; rm -r d")
        dest = self.root / "fork1"
        before = self.user_state()
        child = sb.fork(r1["checkpoint"], str(dest), session="ck-b")
        self.assertEqual(self.user_state()[:4], before[:4])
        self.assertEqual(child.cwd, str(dest))
        self.assertEqual(child._code_dirs, [str(dest)])
        self.assertEqual(child._task_label, "ck-b")
        self.assertTrue(child._checkpoint)  # inherited config
        self.assertEqual(git(dest, "rev-parse", "HEAD"), r1["checkpoint"])
        self.assertEqual(git(dest, "status", "--porcelain"), "")  # dir content == checkpoint tree
        self.assertEqual((dest / "a.txt").read_text(), "two\n")
        self.assertEqual((dest / "d" / "n.txt").read_text(), "deep\n")
        # the fork's own checkpoints chain from the forked commit under its own session
        r3 = self.run_cmd(child, "echo four > a.txt", cwd=dest)
        self.assertEqual(git(self.repo, "rev-parse", f"{r3['checkpoint']}^"), r1["checkpoint"])
        self.assertIn("refs/sbx/ck-b/000001", self.sbx_refs())
        with self.assertRaises(RuntimeError):
            sb.fork(r1["checkpoint"], str(dest), session="ck-c")  # non-empty target refused

    def test_prune_removes_only_that_session(self):
        a, b = self.sandbox(True), self.sandbox(True, session="ck-other")
        self.run_cmd(a, "echo 1 > a.txt")
        self.run_cmd(a, "echo 2 > a.txt")
        self.run_cmd(b, "echo 3 > a.txt")
        from self_hosted_agent_sandbox import checkpoint as ck
        self.assertEqual(ck.prune_checkpoints(str(self.repo), "ck-a"), 2)
        self.assertEqual(self.sbx_refs(), ["refs/sbx/ck-other/000001"])

    def test_pull_back_never_writes_checkpoint_refs(self):
        self.assertTrue(core._git_protected(str(self.repo / ".git/refs/sbx/ck-a/000001")))
        self.assertFalse(core._git_protected(str(self.repo / ".git/refs/heads/main")))
        # a sandbox that deletes a checkpoint ref file must not delete it on the host
        sb = self.sandbox(True)
        self.run_cmd(sb, "echo 1 > a.txt")
        ref = str(self.repo / ".git/refs/sbx/ck-a/000001")
        sb._code_dirs = [str(self.repo)]
        sb._file_sync = core._FileSync(sb._all_files, lambda *a: None, lambda *a: None)
        sb._file_sync.sync(force=True)
        self.assertIn(ref, sb._file_sync.synced)
        hooks = self.repo / ".git" / "hooks" / "reference-transaction"
        hooks.write_text("#!/bin/sh\nexit 1\n"); hooks.chmod(0o755)  # recording runs no hook
        self.run_cmd(sb, "echo 2 > a.txt")
        self.assertEqual(len(self.sbx_refs()), 2)

    def test_cli_list_fork_prune(self):
        sb = self.sandbox(True, session="cli-s")
        r1 = self.run_cmd(sb, "echo c1 > a.txt")
        self.run_cmd(sb, "echo c2 > a.txt")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["checkpoints", "--session", "cli-s", "--cwd", str(self.repo)])
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("1 " + r1["checkpoint"]), lines)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["checkpoints", "--session", "cli-s", "--cwd", str(self.repo), "--json"])
        self.assertEqual([c["sha"] for c in json.loads(out.getvalue())][0], r1["checkpoint"])
        dest = self.root / "cli-fork"
        with contextlib.redirect_stdout(io.StringIO()) as o:
            rc = cli.main(["fork", r1["checkpoint"][:12], str(dest), "--cwd", str(self.repo)])
        self.assertEqual(rc, 0)
        self.assertEqual((dest / "a.txt").read_text(), "c1\n")
        self.assertIn(str(dest), o.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["checkpoints", "--session", "cli-s", "--cwd", str(self.repo), "--prune"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.sbx_refs(), [])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["fork", "deadbeef", str(self.root / "x"), "--cwd", str(self.repo)]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
