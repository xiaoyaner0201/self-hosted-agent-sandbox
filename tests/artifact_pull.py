"""Real binary/tar fault injection, isolated Docker (not a cluster test).
Run: python3 tests/artifact_pull.py [--baseline]
Requires the already-local node image below; never pulls an image.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get("SHAS_TEST_CORE", ROOT / "src/self_hosted_agent_sandbox/core.py"))
spec = importlib.util.spec_from_file_location("core", CORE)
assert spec is not None and spec.loader is not None
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
IMAGE = os.environ.get("SHAS_TEST_IMAGE", "sha256:7702d8aa4d5910871692af4480eb3ba86f23047a6417a44d857d06c95d060d3f")
BASELINE = "--baseline" in sys.argv
if BASELINE:
    sys.argv.remove("--baseline")


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, check=check, timeout=60)


class ArtifactPull(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        docker("image", "inspect", IMAGE)  # an unavailable prerequisite is an error, not a skip
        cls.name = "shas-artifact-test-" + uuid.uuid4().hex[:12]
        docker("run", "--pull=never", "-d", "--name", cls.name,
               "--user", "10001:10001", "--read-only", "--cap-drop=ALL",
               "--security-opt", "no-new-privileges", "--network", "none",
               "--memory", "512m", "--pids-limit", "128",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,uid=10001,gid=10001",
               "--tmpfs", "/Volumes:rw,nosuid,nodev,size=128m,uid=10001,gid=10001",
               "--tmpfs", "/Users:rw,nosuid,nodev,size=128m,uid=10001,gid=10001",
               IMAGE, "sleep", "infinity")
        cls.info = json.loads(docker("inspect", cls.name).stdout)[0]
        print(json.dumps({"scope": "REAL Docker exec/tar, FAKE lifecycle; NOT Kubernetes/gVisor",
                          "core_source": str(CORE),
                          "image": cls.info["Image"], "container": cls.name,
                          "runtime": cls.info["HostConfig"]["Runtime"]}), flush=True)

    @classmethod
    def tearDownClass(cls):
        docker("rm", "-f", cls.name)
        absent = docker("inspect", cls.name, check=False).returncode != 0
        print(json.dumps({"cleanup_container": cls.name, "absent": absent}), flush=True)
        assert absent

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shas-artifact-")
        self.addCleanup(self.tmp.cleanup)
        self.host = Path(self.tmp.name).resolve() / "task"
        self.host.mkdir()
        self.fault = Path(self.tmp.name) / "fault"
        shim = Path(self.tmp.name) / "transport"
        shim.write_text('''#!/usr/bin/env python3
import pathlib, subprocess, sys
fault = pathlib.Path(FAULT)
a = sys.argv[1:]
mode = fault.read_text() if fault.exists() else ''
pull = len(a) >= 3 and 'changed.tar' in a[2]
ack = len(a) >= 3 and 'mv -f' in a[2] and 'changed.tar' not in a[2]
if mode == 'rc42' and pull or mode == 'ack42' and ack:
    sys.exit(42)
if mode == 'tar-fail' and pull:
    a[2] = 'tar() { return 24; }; ' + a[2]
r = subprocess.run(['docker', 'exec', *(['-i'] if sys.stdin.isatty() is False and a[0] == 'tar' else []), CONTAINER, *a], input=sys.stdin.buffer.read() if a[0] == 'tar' else None, capture_output=True)
out = r.stdout
if pull and mode == 'truncate': out = out[:4096]
if pull and mode == 'cut-checksums':
    import io, tarfile
    with tarfile.open(fileobj=io.BytesIO(out), mode='r:') as t:
        out = out[:t.getmember('checksums').offset]
if pull and mode == 'corrupt':
    out = bytearray(out)
    # Corrupt the inner tar's payload, not its header; outer tar remains parseable.
    import io, tarfile
    with tarfile.open(fileobj=io.BytesIO(out), mode='r:') as t:
        member = t.getmember('changed.tar')
        out[member.offset_data + 1024] ^= 1
if ack and mode == 'ack-lost':
    sys.exit(42)  # real commit executed; response lost
sys.stdout.buffer.write(out)
sys.stderr.buffer.write(r.stderr)
sys.exit(r.returncode)
'''.replace("FAULT", repr(str(self.fault))).replace("CONTAINER", repr(self.name)))
        shim.chmod(0o755)
        sb = self.sb = m.Sandbox.__new__(m.Sandbox)
        sb._code_dirs, sb._pod = [str(self.host)], self.name
        sb._exec_argv = lambda *a, **kw: [str(shim), *a]
        sb._file_sync = m._FileSync(sb._all_files, sb._tar_push, sb._rm_remote)
        sb._sync_paths = []
        sb._strict_pull = True
        sb._pull_pending = False
        sb._auto_code_dirs = False
        sb._host_cmds = ()
        sb.cwd, sb.timeout = str(self.host), 30
        sb._renew_ttl = lambda: None
        sb._push_env = lambda **kw: None
        sb._claim, sb._persistent, sb._lock = "synthetic-claim", False, threading.Lock()
        self.deletes = []
        sb._run_k = lambda *a, **kw: (self.deletes.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
        sb._exec_simple(f"mkdir -p {self.host} {m._STATE_DIR}; touch {m._MARK_FILE}")

    def remote(self, script):
        return self.sb._exec_simple(script)

    def mark(self):
        return self.remote(f"stat -c %y {m._MARK_FILE}").strip()

    def payload(self, name="out/data.bin"):
        # Random binary is generated INSIDE the container, never copied from host.
        self.remote(f"mkdir -p {self.host}/out; head -c 1048576 /dev/urandom > {self.host}/{name}")
        return self.remote(f"sha256sum {self.host}/{name}").split()[0]

    def assert_hash(self, relative, expected):
        actual = hashlib.sha256((self.host / relative).read_bytes()).hexdigest()
        self.assertEqual(actual, expected)
        print(json.dumps({"artifact": relative, "bytes": (self.host / relative).stat().st_size,
                          "sha256": actual}), flush=True)

    def test_truncated_transfer_keeps_mark_and_retries(self):
        expected = self.payload()
        before = self.mark()
        self.fault.write_text("truncate")
        with self.assertRaises(Exception):
            self.sb._pull_code()
        after_failure = self.mark()
        self.fault.unlink()
        self.sb._pull_code()
        print(json.dumps({"mark_before": before, "mark_after_truncation": after_failure,
                          "file_present_after_retry": (self.host / "out/data.bin").exists()}), flush=True)
        self.assertEqual(after_failure, before, "unacknowledged export advanced the cursor")
        self.assert_hash("out/data.bin", expected)

    def test_nonzero_transfer_keeps_mark(self):
        expected = self.payload()
        before = self.mark()
        self.fault.write_text("rc42")
        with self.assertRaises(RuntimeError):
            self.sb._pull_code()
        self.assertEqual(self.mark(), before)
        self.fault.unlink()
        self.sb._pull_code()
        self.assert_hash("out/data.bin", expected)

    def test_corruption_rejected_before_writes_or_deletions(self):
        expected = self.payload()
        local = self.host / "old.txt"
        local.write_text("must survive invalid manifest")
        self.sb._file_sync.sync()
        self.remote(f"rm {self.host}/old.txt")
        before = self.mark()
        self.fault.write_text("corrupt")
        with self.assertRaises(Exception):
            self.sb._pull_code()
        self.assertEqual(self.mark(), before)
        self.assertTrue(local.exists())
        self.assertFalse((self.host / "out/data.bin").exists())
        self.fault.unlink()
        self.sb._pull_code()
        self.assertFalse(local.exists())
        self.assert_hash("out/data.bin", expected)

    def test_execute_strict_and_legacy_and_retry_before_push(self):
        artifact = self.host / "existing.bin"
        artifact.write_bytes(b"old host copy")
        self.sb._file_sync.sync()
        self.fault.write_text("truncate")
        command = "head -c 1048576 /dev/urandom > existing.bin; sha256sum existing.bin"
        with self.assertRaises(RuntimeError):
            self.sb.execute(command)
        expected = self.remote(f"sha256sum {artifact}").split()[0]
        with self.assertRaises(Exception):
            self.sb.execute("printf must-not-run > side-effect")
        self.assertEqual(self.remote(f"test ! -e {self.host}/side-effect; printf %s $?"), "0")
        self.fault.unlink()
        self.assertEqual(self.sb.execute("true")["returncode"], 0)
        self.assert_hash("existing.bin", expected)  # force push must not restore the old host copy
        self.sb._strict_pull = False
        for code in (0, 23):
            name = f"out-legacy-{code}.bin"
            self.fault.write_text("truncate")
            result = self.sb.execute(f"head -c 1048576 /dev/urandom > {name}; exit {code}")
            self.assertEqual(result["returncode"], code)
            self.assertEqual(set(result), {"output", "returncode"})
            expected = self.remote(f"sha256sum {self.host}/{name}").split()[0]
            self.fault.unlink()
            self.assertEqual(self.sb.execute("true")["returncode"], 0)
            self.assert_hash(name, expected)

    def test_ack_failure_and_lost_response_recover(self):
        for mode in ("ack42", "ack-lost"):
            with self.subTest(mode=mode):
                expected = self.payload()
                before = self.mark()
                self.fault.write_text(mode)
                with self.assertRaises(RuntimeError):
                    self.sb._pull_code()
                self.assert_hash("out/data.bin", expected)
                if mode == "ack42":
                    self.assertEqual(self.mark(), before)
                self.fault.unlink()
                self.sb._pull_code()
                self.assert_hash("out/data.bin", expected)

    def test_write_failure_does_not_ack(self):
        expected = self.payload()
        before = self.mark()
        from unittest.mock import patch
        with patch.object(m.os, "replace", side_effect=OSError("injected disk full")):
            with self.assertRaises(OSError):
                self.sb._pull_code()
        self.assertEqual(self.mark(), before)
        self.sb._pull_code()
        self.assert_hash("out/data.bin", expected)

    def test_missing_final_member_and_remote_tar_failure(self):
        expected = self.payload()
        for mode in ("cut-checksums", "tar-fail"):
            with self.subTest(mode=mode):
                before = self.mark()
                self.fault.write_text(mode)
                with self.assertRaises(Exception):
                    self.sb.pull()
                self.assertEqual(self.mark(), before)
                self.assertFalse((self.host / "out/data.bin").exists())
                self.fault.unlink()
        self.sb.pull()
        self.assert_hash("out/data.bin", expected)

    def test_delete_failure_does_not_ack(self):
        local = self.host / "old.txt"
        local.write_text("old")
        self.sb._file_sync.sync()
        self.remote(f"rm {local}")
        before = self.mark()
        from unittest.mock import patch
        original = os.unlink

        def fail_target(path, *args, **kw):
            if str(path) == str(local):
                raise PermissionError("injected delete denial")
            return original(path, *args, **kw)

        with patch.object(m.os, "unlink", side_effect=fail_target):
            with self.assertRaises(PermissionError):
                self.sb.pull()
        self.assertEqual(self.mark(), before)
        self.assertTrue(local.exists())
        self.sb.pull()
        self.assertFalse(local.exists())

    def test_configuration_opt_in(self):
        # Real constructor config/env parsing, without acquiring a claim or reading git config.
        class ConfigOnly(m.Sandbox):
            def _acquire(self):
                pass

            def _exec_simple(self, *a, **kw):
                return "/synthetic-home"

            def _push_gitconfig(self):
                pass

            def _push_env(self, **kw):
                pass

        from unittest.mock import patch
        with patch.dict(os.environ, {"AGENT_SANDBOX_STRICT_PULL": "true"}):
            self.assertTrue(ConfigOnly(config={"agent_sandbox_code_dirs": []})._strict_pull)
            self.assertFalse(ConfigOnly(config={"agent_sandbox_strict_pull": False})._strict_pull)
        with patch.dict(os.environ, {"AGENT_SANDBOX_STRICT_PULL": ""}):
            self.assertFalse(ConfigOnly()._strict_pull)
            self.assertTrue(ConfigOnly(config={"agent_sandbox_strict_pull": True})._strict_pull)

    def test_exclusions_explicit_copy_and_git_symlink_guards(self):
        self.remote(f"cd {self.host}; mkdir -p dist node_modules out .git/hooks; "
                    "printf artifact > dist/result; printf dependency > node_modules/index.js; "
                    "printf hostile > .git/config; printf hostile > .git/hooks/pre-push; "
                    "cp dist/result out/result; printf outside > out/link")
        outside = Path(self.tmp.name) / "outside"
        outside.write_text("safe")
        (self.host / "out").mkdir()
        (self.host / "out/link").symlink_to(outside)
        self.sb._pull_code()
        self.assertFalse((self.host / "dist/result").exists())
        self.assertFalse((self.host / "node_modules/index.js").exists())
        self.assertEqual((self.host / "out/result").read_bytes(), b"artifact")
        self.assertFalse((self.host / ".git/config").exists())
        self.assertFalse((self.host / ".git/hooks/pre-push").exists())
        self.assertEqual(outside.read_text(), "safe")

    def test_strict_close_retains_claim_on_pull_failure(self):
        self.payload()
        self.fault.write_text("truncate")
        with self.assertRaises(Exception):
            self.sb.close()
        self.assertEqual(self.sb._claim, "synthetic-claim")
        self.assertFalse(self.deletes)
        self.fault.unlink()
        self.sb.close()
        self.assertTrue(self.deletes)
        self.assertIsNone(self.sb._claim)


if __name__ == "__main__":
    if BASELINE:
        suite = unittest.TestSuite([ArtifactPull("test_truncated_transfer_keeps_mark_and_retries")])
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        sys.exit(not result.wasSuccessful())
    unittest.main(verbosity=2)
