"""Real-container regressions for type conflicts and interrupted command receipt.
Run separately from the original 11-case artifact gate: python3 tests/artifact_integrity.py
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from artifact_pull import ArtifactPull, m


class ArtifactIntegrity(ArtifactPull):
    # Only run the cases declared below, not the inherited artifact gate a second time.
    def assert_received(self, path, expected, *, pending=False):
        self.assertEqual(path.read_text(), expected)
        self.assertEqual(self.remote(f"cat {path}"), expected)
        self.assertEqual(self.sb._pull_pending, pending)
        print(json.dumps({"artifact": path.name,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "remote_matches": True, "pending": pending}), flush=True)

    def test_file_to_directory_conflict_retains_receipt(self):
        for strict in (True, False):
            with self.subTest(strict=strict):
                self.sb._strict_pull = strict
                entry = self.host / f"entry-{strict}"
                entry.write_text("previous-file")
                self.sb._file_sync.sync()
                before = self.mark()
                command = f"rm {entry}; mkdir {entry}; printf new-child > {entry}/child"
                if strict:
                    with self.assertRaises(RuntimeError) as caught:
                        self.sb.execute(command)
                    self.assertIsInstance(caught.exception.__cause__, NotADirectoryError)
                else:
                    self.assertEqual(self.sb.execute(command)["returncode"], 0)
                self.assertTrue(self.sb._pull_pending)
                self.assertEqual(self.mark(), before)
                self.assertEqual(entry.read_text(), "previous-file")
                with self.assertRaises(NotADirectoryError):
                    self.sb.execute("printf must-not-run > side-effect")
                self.assertEqual(self.remote(f"test ! -e {self.host}/side-effect; printf %s $?"), "0")
                # Explicit caller resolution: no automatic deletion of host type conflicts.
                entry.unlink()
                self.assertEqual(self.sb.execute("true")["returncode"], 0)
                self.assert_received(entry / "child", "new-child")

    def test_empty_directory_conflict_is_not_silently_acknowledged(self):
        entry = self.host / "empty-entry"
        entry.write_text("previous-file")
        self.sb._file_sync.sync()
        before = self.mark()
        self.remote(f"rm {entry}; mkdir {entry}")
        with self.assertRaises(NotADirectoryError):
            self.sb.pull()
        self.assertEqual(self.mark(), before)
        self.assertTrue(self.sb._pull_pending)
        entry.unlink()
        self.sb.pull()
        self.assertTrue(entry.is_dir())
        self.assertFalse(self.sb._pull_pending)

    def test_directory_to_file_conflict_retains_receipt(self):
        entry = self.host / "entry"
        entry.mkdir()
        child = entry / "child"
        child.write_text("previous-child")
        self.sb._file_sync.sync()
        before = self.mark()
        self.remote(f"rm -rf {entry}; printf new-file > {entry}")
        with self.assertRaises(OSError):
            self.sb.pull()
        self.assertEqual(self.mark(), before)
        self.assertTrue(self.sb._pull_pending)
        self.assertEqual(child.read_text(), "previous-child")
        child.unlink()
        entry.rmdir()
        self.sb.execute("true")
        self.assert_received(entry, "new-file")

    def test_stream_writer_failure_recovers_without_rerunning_command(self):
        class BrokenWriter:
            def write(self, data):
                raise OSError("caller output sink closed")

        for strict in (True, False):
            with self.subTest(strict=strict):
                self.sb._strict_pull = strict
                entry = self.host / "existing"
                entry.write_text("old-host")
                self.sb._file_sync.sync()
                before = self.mark()
                with self.assertRaisesRegex(OSError, "caller output sink closed"):
                    self.sb.execute("printf new-remote > existing; printf run >> runs; printf output",
                                    stream=BrokenWriter())
                self.assertEqual(self.remote(f"cat {entry}"), "new-remote")
                self.assertTrue(self.sb._pull_pending)
                self.assertEqual(self.mark(), before)
                self.fault.write_text("truncate")
                with self.assertRaises(Exception):
                    self.sb.execute("printf must-not-run > side-effect")
                self.assertTrue(self.sb._pull_pending)
                self.assertEqual(self.remote(f"test ! -e {self.host}/side-effect; printf %s $?"), "0")
                self.fault.unlink()
                self.sb.execute("true")
                self.assert_received(entry, "new-remote")
                self.assert_received(self.host / "runs", "run" if strict else "runrun")

    def test_real_command_timeout_retains_receipt(self):
        entry = self.host / "existing"
        entry.write_text("old-host")
        self.sb._file_sync.sync()
        before = self.mark()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.sb.execute("printf new-remote > existing; printf run > runs; sleep 2", timeout=1)
        self.assertTrue(self.sb._pull_pending)
        self.assertEqual(self.mark(), before)
        # Wait for the original remote command to finish; never execute it again.
        self.remote("sleep 2")
        self.sb.execute("true")
        self.assert_received(entry, "new-remote")
        self.assert_received(self.host / "runs", "run")

    def test_adapter_callback_error_preserves_original_exception_and_receipt(self):
        class CallbackError(Exception):
            pass

        for use_hook in (False, True):
            with self.subTest(use_hook=use_hook):
                entry = self.host / "existing"
                entry.write_text("old-host")
                self.sb._file_sync.sync()
                error = CallbackError("adapter output callback failed")
                calls = []

                def adapter(command, cwd, timeout):
                    calls.append(command)
                    if use_hook:
                        self.sb.before_command()
                    self.remote(f"cd {cwd}; {command}")
                    raise error

                with self.assertRaises(CallbackError) as caught:
                    self.sb.execute("printf new-remote > existing; printf run >> callback-runs",
                                    run_in_sandbox=adapter)
                self.assertIs(caught.exception, error)
                self.assertTrue(self.sb._pull_pending)
                self.sb.execute("true")
                self.assertEqual(len(calls), 1)
                self.assert_received(entry, "new-remote")
                self.assert_received(self.host / "callback-runs", "run" if not use_hook else "runrun")

    def test_direct_adapter_hook_marks_pending_before_command(self):
        entry = self.host / "existing"
        entry.write_text("old-host")
        self.sb.before_command()
        self.assertTrue(self.sb._pull_pending)
        # An external adapter starts a real command, then its output callback fails.
        self.remote(f"printf new-remote > {entry}")
        self.sb.before_command()
        self.assert_received(entry, "new-remote", pending=True)
        # The hook reserves receipt for the command about to run.
        self.assertTrue(self.sb._pull_pending)

    def test_symlink_parent_is_still_protected(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "child").write_text("safe")
        (self.host / "link").symlink_to(outside, target_is_directory=True)
        self.remote(f"mkdir -p {self.host}/link; printf hostile > {self.host}/link/child")
        self.sb.pull()
        self.assertEqual((outside / "child").read_text(), "safe")
        self.assertTrue((self.host / "link").is_symlink())
        self.assertFalse(self.sb._pull_pending)

    def test_multifile_partial_write_retry_before_push(self):
        for name in ("a", "b"):
            (self.host / name).write_text("old-" + name)
        self.sb._file_sync.sync()
        self.remote(f"printf new-a > {self.host}/a; printf new-b > {self.host}/b")
        before = self.mark()
        original, applied = os.replace, []

        def fail_second(src, dst, *args, **kw):
            if str(dst).startswith(str(self.host)):
                if len(applied) == 1:
                    raise OSError("second replace disk failure")
                applied.append(str(dst))
            return original(src, dst, *args, **kw)

        with patch.object(m.os, "replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.sb.pull()
        self.assertEqual(len(applied), 1)
        self.assertEqual(before, self.mark())
        self.assertTrue(self.sb._pull_pending)
        self.sb.execute("true")
        for name in ("a", "b"):
            self.assert_received(self.host / name, "new-" + name)

    def test_directory_deletion_and_multifile_delete_retry(self):
        for name in ("obsolete/a", "obsolete/nested/b", "keep"):
            path = self.host / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        self.sb._file_sync.sync()
        self.remote(f"rm -rf {self.host}/obsolete")
        before = self.mark()
        original, deleted = os.unlink, []

        def fail_second(path, *args, **kw):
            if str(path).startswith(str(self.host / "obsolete")):
                if len(deleted) == 1:
                    raise PermissionError("second delete denied")
                deleted.append(str(path))
            return original(path, *args, **kw)

        with patch.object(m.os, "unlink", side_effect=fail_second):
            with self.assertRaises(PermissionError):
                self.sb.pull()
        self.assertEqual(len(deleted), 1)
        self.assertEqual(before, self.mark())
        self.assertTrue(self.sb._pull_pending)
        self.sb.execute("true")
        self.assertFalse((self.host / "obsolete/a").exists())
        self.assertFalse((self.host / "obsolete/nested/b").exists())
        self.assertEqual(self.remote(f"test ! -d {self.host}/obsolete; printf %s $?"), "0")
        self.assert_received(self.host / "keep", "keep")


if __name__ == "__main__":
    names = sorted(n for n in ArtifactIntegrity.__dict__ if n.startswith("test_"))
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestSuite(ArtifactIntegrity(n) for n in names))
    sys.exit(not result.wasSuccessful())
