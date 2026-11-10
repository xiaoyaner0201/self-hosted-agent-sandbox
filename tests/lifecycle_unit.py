"""Offline public lifecycle API tests + real local process groups. No cluster access.
Run: PYTHONPATH=src python tests/lifecycle_unit.py
"""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from self_hosted_agent_sandbox import Allocation, ReleaseError, Sandbox
from self_hosted_agent_sandbox import core

FAKE = Path(__file__).with_name('fake_lifecycle_kubectl.py').resolve()


def obj(name, uid, **extra):
    return dict(metadata={'namespace': 'test-ns', 'name': name, 'uid': uid,
                          'resourceVersion': '17'}, **extra)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state.json'
        self.data = {'objects': {
            'claim': obj('claim-a', 'claim-uid', status={
                'sandbox': {'name': 'sandbox-a'}, 'conditions': [{'type': 'Ready', 'status': 'True'}]}),
            'sandbox': obj('sandbox-a', 'sandbox-uid'),
            'pod': obj('sandbox-a', 'pod-uid'),
        }}
        self.write()
        self.cfg = {'agent_sandbox_kubectl': str(FAKE),
                    'agent_sandbox_kubeconfig': str(self.state),
                    'agent_sandbox_namespace': 'ignored-namespace'}
        self.target = Allocation('test-ns', 'claim-a', 'claim-uid', 'sandbox-a')

    def write(self):
        self.state.write_text(json.dumps(self.data))

    def calls(self):
        p = Path(str(self.state) + '.log')
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def release(self, target=None, **kw):
        return Sandbox.release_allocation(target or self.target, config=self.cfg,
                                          timeout=kw.pop('timeout', 3), poll_interval=.02, **kw)

    def instance(self):
        sb = Sandbox.__new__(Sandbox)
        sb._allocation, sb._on_allocation = self.target, None
        sb._kubectl, sb._kubeconfig, sb._ns = str(FAKE), str(self.state), 'test-ns'
        sb._claim, sb._pod = 'claim-a', 'sandbox-a'
        sb._lock, sb._persistent = threading.Lock(), False
        sb._pull_code = lambda: (0, 0)
        return sb

    def test_exact_verified_cascade_and_namespace(self):
        ledger = []
        def persist(target):
            self.assertFalse(any(x['args'][0] == 'delete' for x in self.calls()))
            ledger.append(target)
        result = self.release(on_allocation=persist)
        self.assertTrue(result.inventoried)
        self.assertEqual(result.pod_uid, 'pod-uid')
        self.assertEqual(ledger[-1], result)
        calls = self.calls()
        self.assertTrue(all(x['namespace'] == 'test-ns' for x in calls))
        deletes = [x for x in calls if x['args'][0] == 'delete']
        self.assertEqual(len(deletes), 1)
        self.assertEqual(json.loads(deletes[0]['body'])['preconditions'],
                         {'uid': 'claim-uid', 'resourceVersion': '17'})
        self.assertFalse(any(x['args'][0] == 'apply' for x in calls))
        self.assertEqual([x['args'][1] for x in calls[-3:]], [
            'sandboxclaims.extensions.agents.x-k8s.io', 'sandboxes.agents.x-k8s.io', 'pods'])

    def test_delete_rc_zero_is_not_verification(self):
        self.data['mode'] = 'pending'; self.write()
        start = time.monotonic()
        with self.assertRaises(ReleaseError) as raised:
            self.release(timeout=1.5)
        self.assertLess(time.monotonic() - start, 2.3)
        self.assertTrue(raised.exception.allocation.inventoried)
        self.assertIsInstance(raised.exception.__cause__, (TimeoutError, subprocess.TimeoutExpired))
        self.assertTrue(any(x['args'][0] == 'delete' for x in self.calls()))

    def test_absent_claim_but_live_sandbox_pod_not_success(self):
        self.data['mode'] = 'claim_only'; self.write()
        with self.assertRaises(ReleaseError) as raised:
            self.release(timeout=1.5)
        retry = raised.exception.allocation
        self.assertTrue(retry.inventoried)
        self.data['objects'] = {}; self.write()
        self.assertEqual(self.release(retry), retry)

    def test_delete_failure_retains_instance_target_and_retry(self):
        sb = self.instance()
        self.data['delete_fail'] = True; self.write()
        with self.assertRaises(ReleaseError):
            sb.release_verified(timeout=3)
        self.assertEqual(sb._claim, 'claim-a')
        self.assertTrue(sb.allocation.inventoried)
        self.data['delete_fail'] = False; self.write()
        target = sb.release_verified(timeout=3)
        self.assertIsNone(sb._claim)
        self.assertEqual(sb.allocation, target)
        self.assertEqual(sb.release_verified(timeout=3), target)  # idempotent exact read-back

    def test_legacy_close_failure_and_persistence_preserve_public_handle(self):
        sb = self.instance()
        self.data['delete_fail'] = True; self.write()
        sb.close()
        self.assertIsNone(sb._claim)  # intentionally compatible legacy private state
        self.assertEqual(sb.allocation, self.target)
        self.data['delete_fail'] = False; self.write()
        self.assertTrue(sb.release_verified(timeout=3).inventoried)
        sb = self.instance(); sb._persistent = True
        count = len(self.calls()); sb.close()
        self.assertEqual(len(self.calls()), count)
        self.assertEqual(sb.allocation, self.target)

    def test_observer_rbac_fails_before_delete(self):
        self.data['forbidden'] = 'sandbox'; self.write()
        with self.assertRaisesRegex(ReleaseError, 'Forbidden'):
            self.release()
        self.assertFalse(any(x['args'][0] == 'delete' for x in self.calls()))

    def test_malformed_read_is_not_absence(self):
        self.data['malformed'] = 'pod'; self.write()
        with self.assertRaisesRegex(ReleaseError, 'invalid identity'):
            self.release()
        self.assertFalse(any(x['args'][0] == 'delete' for x in self.calls()))

    def test_uid_reuse_before_delete_is_not_touched(self):
        self.data['objects']['claim']['metadata']['uid'] = 'someone-else'; self.write()
        with self.assertRaisesRegex(ReleaseError, 'UID changed'):
            self.release()
        self.assertFalse(any(x['args'][0] == 'delete' for x in self.calls()))

    def test_uid_reuse_during_poll_is_not_success(self):
        self.data['mode'] = 'replace_pod'; self.write()
        with self.assertRaisesRegex(ReleaseError, 'UID changed') as raised:
            self.release()
        self.assertEqual(raised.exception.allocation.pod_uid, 'pod-uid')

    def test_legacy_adopted_pod_name_is_observed_not_guessed(self):
        self.data['objects']['sandbox']['metadata']['annotations'] = {'agents.x-k8s.io/pod-name': 'old-pod'}
        self.data['objects']['pod']['metadata']['name'] = 'old-pod'; self.write()
        target = self.release()
        self.assertEqual(target.pod, 'old-pod')
        self.assertTrue(any(x['args'][:3] == ['get', 'pods', 'old-pod'] for x in self.calls()))

    def test_incomplete_inventory_fails_closed(self):
        self.data['objects'].pop('sandbox'); self.write()
        with self.assertRaisesRegex(ReleaseError, 'before inventory'):
            self.release()
        self.assertFalse(any(x['args'][0] == 'delete' for x in self.calls()))

    def test_unbound_claim_is_reaper_responsibility(self):
        self.data['objects']['claim']['status'] = {}; self.write()
        with self.assertRaisesRegex(ReleaseError, 'binding unknown'):
            self.release(Allocation('test-ns', 'claim-a'))
        self.assertFalse(any(x['args'][0] == 'delete' for x in self.calls()))

    def test_inventory_callback_failure_prevents_delete_and_keeps_snapshot(self):
        def fail(target):
            if target.inventoried:
                raise OSError('ledger unavailable')
        with self.assertRaisesRegex(ReleaseError, 'ledger unavailable') as raised:
            self.release(on_allocation=fail)
        self.assertTrue(raised.exception.allocation.inventoried)
        self.assertFalse(any(x['args'][0] == 'delete' for x in self.calls()))

    def assert_inventory_callback_retry(self, *, instance, succeeds):
        sb = self.instance() if instance else None
        attempts = []
        def fail(target):
            if target.inventoried:
                attempts.append(target)
                raise OSError('ledger unavailable')
        def release(target, callback):
            if sb is not None:
                sb._on_allocation = callback
                return sb.release_verified(timeout=3)
            return self.release(target, on_allocation=callback)

        with self.assertRaisesRegex(ReleaseError, 'ledger unavailable') as raised:
            release(self.target, fail)
        retry = raised.exception.allocation
        self.assertTrue(retry.inventoried)
        self.assertEqual(attempts, [retry])
        self.assertFalse(any(x['args'][0] == 'delete' for x in self.calls()))
        if sb is not None:
            self.assertEqual(sb.allocation, retry)
            self.assertEqual((sb._claim, sb._pod), ('claim-a', 'sandbox-a'))

        saved = []
        def persist(target):
            self.assertFalse(any(x['args'][0] == 'delete' for x in self.calls()))
            saved.append(target)
        if succeeds:
            self.assertEqual(release(retry, persist), retry)
            self.assertIn(retry, saved)
            self.assertEqual(sum(x['args'][0] == 'delete' for x in self.calls()), 1)
            self.assertEqual(json.loads(self.state.read_text())['objects'], {})
            if sb is not None:
                self.assertEqual(sb.allocation, retry)
                self.assertIsNone(sb._claim)
                self.assertIsNone(sb._pod)
        else:
            with self.assertRaisesRegex(ReleaseError, 'ledger unavailable') as retried:
                release(retry, fail)
            self.assertEqual(retried.exception.allocation, retry)
            self.assertEqual(attempts, [retry, retry])
            self.assertFalse(any(x['args'][0] == 'delete' for x in self.calls()))
            if sb is not None:
                self.assertEqual(sb.allocation, retry)
                self.assertEqual((sb._claim, sb._pod), ('claim-a', 'sandbox-a'))

    def test_direct_inventory_callback_retry_failure_prevents_delete(self):
        self.assert_inventory_callback_retry(instance=False, succeeds=False)

    def test_direct_inventory_callback_retry_success_allows_delete(self):
        self.assert_inventory_callback_retry(instance=False, succeeds=True)

    def test_instance_inventory_callback_retry_failure_prevents_delete(self):
        self.assert_inventory_callback_retry(instance=True, succeeds=False)

    def test_instance_inventory_callback_retry_success_allows_delete(self):
        self.assert_inventory_callback_retry(instance=True, succeeds=True)

    def test_total_budget_includes_inventory_subprocess(self):
        self.data['stall'] = 'claim'; self.write()
        start = time.monotonic()
        with self.assertRaises(ReleaseError):
            self.release(timeout=.25)
        self.assertLess(time.monotonic() - start, .9)
        self.assertFalse(any(x['args'][0] == 'delete' for x in self.calls()))

    def test_uid_precondition_blocks_claim_replacement_after_inventory(self):
        def replace_claim(target):
            if target.inventoried:
                self.data['objects']['claim']['metadata']['uid'] = 'replacement'
                self.write()
        with self.assertRaisesRegex(ReleaseError, 'Conflict') as raised:
            self.release(on_allocation=replace_claim)
        self.assertEqual(raised.exception.allocation.claim_uid, 'claim-uid')
        self.assertEqual(json.loads(self.state.read_text())['objects']['claim']['metadata']['uid'], 'replacement')

    def test_release_cancellation_preserves_original_error_and_latest_target(self):
        def cancel(target):
            if target.inventoried:
                raise KeyboardInterrupt('caller cancelled')
        with self.assertRaises(KeyboardInterrupt) as raised:
            self.release(on_allocation=cancel)
        self.assertTrue(getattr(raised.exception, 'allocation').inventoried)
        self.assertFalse(any(x['args'][0] == 'delete' for x in self.calls()))

    def test_legacy_async_close_before_inventory_cannot_claim_verified_success(self):
        sb = self.instance(); sb.close()
        self.assertEqual(sb.allocation, self.target)
        with self.assertRaisesRegex(ReleaseError, 'before inventory'):
            sb.release_verified(timeout=3)

    def test_instance_lock_wait_is_inside_budget(self):
        sb = self.instance()
        sb._lock.acquire()
        self.addCleanup(sb._lock.release)
        start = time.monotonic()
        with self.assertRaisesRegex(ReleaseError, 'lock deadline'):
            sb.release_verified(timeout=.1)
        self.assertLess(time.monotonic() - start, .6)
        self.assertEqual(self.calls(), [])

    def test_bad_budgets_start_no_process(self):
        for value in (0, -1, float('inf'), float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.release(timeout=value)
        self.assertEqual(self.calls(), [])

    def test_snapshot_roundtrip(self):
        self.assertEqual(Allocation(**json.loads(json.dumps(self.target._asdict()))), self.target)
        with self.assertRaises(AttributeError):
            self.target.claim = 'other'

    def constructor(self, callback, **kw):
        cfg = dict(self.cfg, agent_sandbox_namespace='test-ns')
        with patch.object(Sandbox, '_exec_simple', side_effect=OSError('setup failed')):
            try:
                Sandbox(config=cfg, on_allocation=callback, **kw)
            except OSError as error:
                return error
        self.fail('constructor should have failed')

    def test_constructor_failure_exposes_binding_without_extra_rbac(self):
        snapshots = []
        error = self.constructor(snapshots.append)
        self.assertTrue(snapshots[0].claim)
        self.assertFalse(snapshots[0].claim_uid)
        self.assertEqual(error.allocation, snapshots[-1])
        self.assertEqual(error.allocation.sandbox, 'sandbox-a')
        self.assertEqual(error.allocation.claim_uid, 'claim-uid')
        self.assertEqual([c['args'][0] for c in self.calls()], ['apply', 'get'])
        self.assertTrue(self.release(error.allocation).inventoried)

    def test_constructor_notifies_before_ambiguous_apply(self):
        snapshots = []
        self.data['apply_fail'] = True; self.write()
        def notify(target):
            self.assertEqual(self.calls(), [])
            snapshots.append(target)
        with self.assertRaisesRegex(RuntimeError, 'claim create failed') as raised:
            Sandbox(config=dict(self.cfg, agent_sandbox_namespace='test-ns'), on_allocation=notify)
        self.assertEqual(raised.exception.allocation, snapshots[0])
        self.assertTrue(self.release(snapshots[0]).inventoried)

    def test_constructor_callback_failure_stops_apply(self):
        def fail(target):
            raise OSError('ledger write failed')
        with self.assertRaisesRegex(OSError, 'ledger write failed') as raised:
            Sandbox(config=self.cfg, on_allocation=fail)
        self.assertTrue(raised.exception.allocation.claim)
        self.assertEqual(self.calls(), [])


@unittest.skipUnless(os.name == 'posix', 'process groups require POSIX')
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.marker = self.path / 'marker'
        self.ids = self.path / 'ids'
        self.launcher = self.path / 'launcher.py'
        self.writer = self.path / 'writer.py'
        self.writer.write_text("import sys,time\nf=open(sys.argv[1],'ab',buffering=0)\nwhile True:\n f.write(b'x'); time.sleep(.02)\n")
        self.launcher.write_text('''import json, os, subprocess, sys, time
from pathlib import Path
p = subprocess.Popen([sys.executable, str(Path(__file__).with_name('writer.py')), sys.argv[1]])
Path(sys.argv[2]).write_text(json.dumps([os.getpid(), p.pid, os.getpgrp()]))
print('ready', flush=True)
if sys.argv[3] == 'exit': sys.exit(0)
p.wait()
''')
        self.processes = []
        self.sb = Sandbox.__new__(Sandbox)
        self.sb.before_command = lambda: None
        self.sb._exec_argv = lambda *a: [sys.executable, str(self.launcher),
                                         str(self.marker), str(self.ids), 'wait']
        self.real_popen = subprocess.Popen
        def spawn(*a, **kw):
            p = self.real_popen(*a, **kw)
            self.processes.append(p)
            self.addCleanup(self.cleanup_process, p)
            return p
        self.patcher = patch.object(core.subprocess, 'Popen', spawn)
        self.patcher.start(); self.addCleanup(self.patcher.stop)

    def cleanup_process(self, p):
        try: os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        p.wait(timeout=3)

    def assert_stopped(self):
        self.assertTrue(self.ids.exists(), 'launcher must actually run')
        leader, child, pg = json.loads(self.ids.read_text())
        self.assertEqual(leader, pg)
        self.assertNotEqual(pg, os.getpgrp())
        self.assertIsNotNone(self.processes[0].returncode, 'direct child reaped')
        size = self.marker.stat().st_size if self.marker.exists() else 0
        time.sleep(.12)
        self.assertEqual(self.marker.stat().st_size if self.marker.exists() else 0, size)
        for pid in (leader, child):
            r = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
            self.assertTrue(not r.stdout.strip() or r.stdout.strip().startswith('Z'), (pid, r.stdout))

    def test_stream_timeout_reaps_real_group(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.sb._default_run('unused', self.tmp.name, .3, stream=io.BytesIO())
        self.assertGreater(self.marker.stat().st_size, 0)
        self.assert_stopped()

    def test_stream_keyboard_interrupt_reaps_real_group(self):
        done = threading.Event()
        def interrupt():
            end = time.monotonic() + 3
            while not done.wait(.01) and time.monotonic() < end:
                if self.ids.exists() and self.marker.exists() and self.marker.stat().st_size:
                    os.kill(os.getpid(), signal.SIGINT)
                    return
        t = threading.Thread(target=interrupt, daemon=True); t.start()
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.sb._default_run('unused', self.tmp.name, 5, stream=io.BytesIO())
        finally:
            done.set(); t.join()
        self.assert_stopped()

    def test_exited_leader_does_not_leave_pipe_holding_child(self):
        self.sb._exec_argv = lambda *a: [sys.executable, str(self.launcher),
                                         str(self.marker), str(self.ids), 'exit']
        start = time.monotonic()
        result = self.sb._default_run('unused', self.tmp.name, 2, stream=io.BytesIO())
        self.assertEqual(result['returncode'], 0)
        self.assertLess(time.monotonic() - start, 1)
        self.assert_stopped()

    def test_writer_failure_reaps_real_group(self):
        class FailWriter:
            def write(self, data):
                raise OSError('sink failed')
        with self.assertRaisesRegex(OSError, 'sink failed'):
            self.sb._default_run('unused', self.tmp.name, 2, stream=FailWriter())
        self.assert_stopped()


if __name__ == '__main__':
    unittest.main(verbosity=2)
