"""Checkpoint after REAL Docker receipt (FAKE lifecycle). Not Kubernetes/gVisor.
Run: PYTHONPATH=src python3 tests/checkpoint_docker.py   (same image/TMPDIR rules as artifact_pull.py)
"""
import subprocess
import unittest

from artifact_pull import ArtifactPull, m


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


class CheckpointAfterReceipt(ArtifactPull):
    def setUp(self):
        super().setUp()
        git(self.host, "init", "-q", "-b", "main")
        (self.host / "a.txt").write_text("host\n")
        git(self.host, "add", "-A")
        git(self.host, "-c", "user.name=Dev", "-c", "user.email=dev@example.com", "commit", "-q", "-m", "init")
        self.sb._checkpoint, self.sb._task_label, self.sb._config = True, "dock", {}
        self.sb._env_digest = None
        self.sb.before_command()  # push the repo (incl. .git) like a real session start
        self.sb._pull_pending = False

    def test_checkpoint_holds_sandbox_edits_and_forged_ref_is_refused(self):
        head = git(self.host, "rev-parse", "HEAD")
        r = self.sb.execute("echo sandbox > a.txt; echo new > made-in-sandbox.txt; "
                            "mkdir -p .git/refs/sbx/dock && echo " + "f" * 40 + " > .git/refs/sbx/dock/000009")
        self.assertEqual(r["returncode"], 0, r)
        sha = r["checkpoint"]
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertEqual(git(self.host, "show", f"{sha}:a.txt"), "sandbox")
        self.assertEqual(git(self.host, "show", f"{sha}:made-in-sandbox.txt"), "new")
        self.assertEqual(git(self.host, "rev-parse", f"{sha}^"), head)
        self.assertEqual(git(self.host, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.host, "for-each-ref", "--format=%(refname)", "refs/sbx"), "refs/sbx/dock/000001")
        self.assertFalse((self.host / ".git/refs/sbx/dock/000009").exists())
        r2 = self.sb.execute("rm made-in-sandbox.txt")
        self.assertEqual(git(self.host, "rev-parse", f"{r2['checkpoint']}^"), sha)
        self.assertNotIn("made-in-sandbox.txt", git(self.host, "ls-tree", "--name-only", r2["checkpoint"]))


if __name__ == "__main__":  # only this file's cases, not the inherited fixture's
    import sys
    names = sorted(n for n in CheckpointAfterReceipt.__dict__ if n.startswith("test_"))
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestSuite(CheckpointAfterReceipt(n) for n in names))
    sys.exit(not result.wasSuccessful())
