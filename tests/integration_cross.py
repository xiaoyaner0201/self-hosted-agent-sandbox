"""Integrated contracts: REAL Docker receipt + POSIX PGs, FAKE lifecycle only.
Run: PYTHONPATH=src python3 tests/integration_cross.py
Inherits only the fixture, not the original 11 artifact test cases in its run count.
"""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from artifact_pull import ArtifactPull, m

FAKE = Path(__file__).with_name('fake_lifecycle_kubectl.py').resolve()


class IntegratedContracts(ArtifactPull):
    def setUp(self):
        super().setUp()
        self.state = Path(self.tmp.name) / 'lifecycle.json'
        def obj(name, uid, **extra):
            return dict(metadata=dict(namespace='test-ns', name=name, uid=uid,
                                      resourceVersion='17'), **extra)
        self.lifecycle: dict = {'objects': {
            'claim': obj('claim-a', 'claim-uid', status={'sandbox': {'name': 'sandbox-a'}}),
            'sandbox': obj('sandbox-a', 'sandbox-uid'),
            'pod': obj('sandbox-a', 'pod-uid'),
        }}
        self.state.write_text(json.dumps(self.lifecycle))
        self.sb._allocation = m.Allocation('test-ns', 'claim-a', 'claim-uid', 'sandbox-a')
        self.sb._on_allocation = None
        self.sb._claim = 'claim-a'
        self.sb._kubectl, self.sb._kubeconfig, self.sb._ns = str(FAKE), str(self.state), 'test-ns'
        # Only control-plane subprocesses use the fake. Data path stays real Docker.
        self.sb._run_k = m.Sandbox._run_k.__get__(self.sb)

    def lifecycle_calls(self):
        log = Path(str(self.state) + '.log')
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    def test_cross_strict_failure_retains_target_then_receipt_and_release(self):
        target = self.sb.allocation
        self.fault.write_text('truncate')
        with self.assertRaisesRegex(RuntimeError, 'command may have completed'):
            self.sb.execute('printf run >> runs; printf remote > result')
        self.assertTrue(self.sb._pull_pending)
        self.assertEqual(self.sb.allocation, target)
        with self.assertRaises(Exception):
            self.sb.close(force_remove=True)
        self.assertEqual(self.sb._claim, 'claim-a')
        self.assertEqual(self.sb.allocation, target)
        self.assertEqual(self.lifecycle_calls(), [])
        self.fault.unlink()
        self.sb.pull()
        self.assertFalse(self.sb._pull_pending)
        self.assertEqual((self.host / 'result').read_text(), 'remote')
        self.assertEqual((self.host / 'runs').read_text(), 'run')
        saved = []
        self.sb._on_allocation = saved.append
        released = self.sb.release_verified(timeout=5, poll_interval=.02)
        self.assertTrue(released.inventoried)
        self.assertIn(released, saved)
        self.assertEqual(self.sb.allocation, released)
        self.assertIsNone(self.sb._claim)
        self.assertEqual(json.loads(self.state.read_text())['objects'], {})

    def test_cross_verified_release_is_not_artifact_receipt(self):
        self.fault.write_text('truncate')
        with self.assertRaises(RuntimeError):
            self.sb.execute('printf valuable > unreceived')
        self.assertFalse((self.host / 'unreceived').exists())
        original_pull = self.sb._pull_code
        with patch.object(self.sb, '_pull_code', wraps=original_pull) as pull:
            released = self.sb.release_verified(timeout=5, poll_interval=.02)
            pull.assert_not_called()
        self.assertTrue(released.inventoried)
        self.assertEqual(self.sb.allocation, released)
        self.assertIsNone(self.sb._pod)
        self.assertTrue(self.sb._pull_pending)  # cleanup verdict is not an ACK
        self.assertFalse((self.host / 'unreceived').exists())
        self.assertEqual(json.loads(self.state.read_text())['objects'], {})
        print('FAKE verified API absence; unreceived artifact stays absent, pending remains true', flush=True)

    def test_cross_legacy_close_defaults_persistence_and_nonzero_delete(self):
        for persistent, force, fail in ((False, False, True), (True, False, False), (True, True, False)):
            with self.subTest(persistent=persistent, force=force, delete_fail=fail):
                self.lifecycle['delete_fail'] = fail
                self.state.write_text(json.dumps(self.lifecycle))
                before = len(self.lifecycle_calls())
                target = self.sb.allocation
                self.sb._claim, self.sb._persistent, self.sb._strict_pull = 'claim-a', persistent, False
                self.fault.write_text('truncate')
                self.sb.close(force_remove=force)
                self.assertIsNone(self.sb._claim)
                self.assertEqual(self.sb.allocation, target)
                calls = self.lifecycle_calls()[before:]
                self.assertEqual(sum(x['args'][0] == 'delete' for x in calls), int(force or not persistent))
                self.assertTrue(self.sb._pull_pending)

    def stream_failure_with_group(self, *, timeout):
        existing = self.host / 'existing'
        existing.write_text('old-host')
        self.sb._file_sync.sync()
        before = self.mark()
        marker = Path(self.tmp.name) / 'local-marker'
        ids = Path(self.tmp.name) / 'local-ids'
        child = Path(self.tmp.name) / 'child.py'
        child.write_text("import sys,time\nf=open(sys.argv[1],'ab',buffering=0)\nwhile True:\n f.write(b'x'); time.sleep(.02)\n")
        launcher = Path(self.tmp.name) / 'launcher.py'
        launcher.write_text('''import json, os, pathlib, subprocess, sys, time
# Complete the real remote mutation before the local stream failure, avoiding any
# claim that killing a local transport process proves remote shell termination.
r = subprocess.run(sys.argv[4:], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
if r.returncode: sys.exit(r.returncode)
p = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)
pathlib.Path(sys.argv[3]).write_text(json.dumps([os.getpid(), p.pid, os.getpgrp()]))
print('ready', flush=True)
p.wait()
''')
        original_argv = self.sb._exec_argv
        processes = []
        real_popen = m.subprocess.Popen
        error = OSError('cross sink failed')
        class FailedWriter:
            def write(self, data):
                raise error
        def spawn(*a, **kw):
            p = real_popen(*a, **kw)
            if kw.get('start_new_session'):
                processes.append(p)
            return p
        def argv(*a, **kw):
            if len(a) >= 3 and 'CROSS_COMMAND' in a[2]:
                return [sys.executable, str(launcher), str(child), str(marker), str(ids), *original_argv(*a, **kw)]
            return original_argv(*a, **kw)
        def cleanup():
            for p in processes:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.wait(timeout=5)
        self.addCleanup(cleanup)
        with patch.object(self.sb, '_exec_argv', side_effect=argv), patch.object(m.subprocess, 'Popen', side_effect=spawn):
            expected_error = subprocess.TimeoutExpired if timeout else OSError
            with self.assertRaises(expected_error) as raised:
                self.sb.execute('printf new-remote > existing; printf run >> runs # CROSS_COMMAND',
                                timeout=2 if timeout else 10,
                                stream=io.BytesIO() if timeout else FailedWriter())
        if not timeout:
            self.assertIs(raised.exception, error)
        self.assertTrue(self.sb._pull_pending)
        self.assertEqual(self.mark(), before)
        self.assertEqual(existing.read_text(), 'old-host')
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        leader, pid, pg = json.loads(ids.read_text())
        self.assertEqual(leader, pg)
        self.assertNotEqual(pg, os.getpgrp())
        size = marker.stat().st_size
        time.sleep(.15)
        self.assertEqual(marker.stat().st_size, size)
        for process in (leader, pid):
            result = subprocess.run(['ps', '-o', 'stat=', '-p', str(process)], capture_output=True, text=True)
            self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith('Z'), result.stdout)
        self.sb.pull()
        self.assertFalse(self.sb._pull_pending)
        self.assertEqual(existing.read_text(), 'new-remote')
        self.assertEqual((self.host / 'runs').read_text(), 'run')
        self.assertEqual(self.sb.execute('true')['returncode'], 0)
        self.assertEqual(existing.read_text(), 'new-remote')
        self.assertEqual((self.host / 'runs').read_text(), 'run')
        print(json.dumps(dict(local_pg=pg, leader_reaped=True, child_stopped=True,
                              pending_then_received=True, command_runs=1, timeout=timeout)), flush=True)

    def test_cross_stream_writer_pending_and_process_group_reap(self):
        self.stream_failure_with_group(timeout=False)

    def test_cross_stream_timeout_pending_and_process_group_reap(self):
        self.stream_failure_with_group(timeout=True)


if __name__ == '__main__':
    names = sorted(n for n in IntegratedContracts.__dict__ if n.startswith('test_cross_'))
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(IntegratedContracts(n) for n in names))
    sys.exit(not result.wasSuccessful())
