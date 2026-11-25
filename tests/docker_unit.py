"""Offline checks of the docker driver with a fake docker CLI (tests/fake_docker.py).
Never contacts a Docker daemon. Run: PYTHONPATH=src python3 tests/docker_unit.py

The fake runs ``docker exec`` locally, so the "sandbox" shares this filesystem; real transport
and isolation are covered by tests/e2e_docker.sh against a local daemon.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

from self_hosted_agent_sandbox import Allocation, ReleaseError, Sandbox
from self_hosted_agent_sandbox import core

FAKE = Path(__file__).with_name('fake_docker.py').resolve()


class DockerDriverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(os.path.realpath(self.tmp.name))  # host routing refuses symlinked cwd
        self.state = root / 'docker.json'
        (root / 'home').mkdir()
        self.code = root / 'code'
        self.code.mkdir()
        env = {'FAKE_DOCKER_STATE': str(self.state), 'FAKE_DOCKER_HOME': str(root / 'home')}
        self.env_patch = unittest.mock.patch.dict(os.environ, env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for k in [k for k in os.environ if k.startswith('AGENT_SANDBOX_')]:
            self.addCleanup(os.environ.__setitem__, k, os.environ.pop(k))
        self.cfg = {'agent_sandbox_driver': 'docker', 'agent_sandbox_docker': str(FAKE),
                    'agent_sandbox_docker_image': 'local/test:1'}

    def data(self):
        return json.loads(self.state.read_text()) if self.state.exists() else {'containers': {}}

    def put(self, **knobs):
        d = self.data()
        d.update(knobs)
        self.state.write_text(json.dumps(d))

    def calls(self):
        p = Path(str(self.state) + '.log')
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def open(self, **cfg):
        sb = Sandbox(cwd=str(self.code), session='Unit Session', app='unit',
                     config=dict(self.cfg, **cfg))
        self.addCleanup(lambda: sb.close(force_remove=True))
        return sb

    def run_argv(self):
        return next(c for c in self.calls() if c[0] == 'run')

    # ---- claim ------------------------------------------------------------
    def test_run_is_non_root_labelled_limited_and_mounts_nothing(self):
        sb = self.open()
        a = self.run_argv()
        self.assertEqual(a[:2], ['run', '-d'])
        self.assertEqual(a[a.index('--user') + 1], '1000:1000')
        self.assertIn('agent-sandbox/session=unit-session', a)
        self.assertIn('agent-sandbox/app=unit', a)
        self.assertEqual(a[a.index('--memory') + 1], '4g')
        self.assertEqual(a[a.index('--cpus') + 1], '2')
        self.assertIn('--cap-drop', a)
        self.assertNotIn('--runtime', a)
        self.assertNotIn('--privileged', a)
        self.assertFalse(any(x in ('-v', '--volume', '--mount') for x in a), a)
        self.assertIn('local/test:1', a)
        alloc = sb.allocation
        self.assertEqual(alloc.driver, 'docker')
        self.assertEqual(alloc.claim, sb._claim)
        self.assertTrue(alloc.claim_uid and alloc.inventoried)
        self.assertIn(alloc.claim, self.data()['containers'])

    def test_runtime_memory_cpu_and_user_are_configurable(self):
        self.open(agent_sandbox_docker_runtime='runsc', agent_sandbox_docker_memory='512m',
                  agent_sandbox_docker_cpus='0.5', agent_sandbox_docker_user='2000:2000')
        a = self.run_argv()
        self.assertEqual(a[a.index('--runtime') + 1], 'runsc')
        self.assertEqual(a[a.index('--memory') + 1], '512m')
        self.assertEqual(a[a.index('--cpus') + 1], '0.5')
        self.assertEqual(a[a.index('--user') + 1], '2000:2000')

    def test_env_selects_driver(self):
        os.environ.update(AGENT_SANDBOX_DRIVER='docker', AGENT_SANDBOX_DOCKER=str(FAKE),
                          AGENT_SANDBOX_DOCKER_IMAGE='local/env:2')
        sb = Sandbox(cwd=str(self.code), session='envsel')
        self.addCleanup(lambda: sb.close(force_remove=True))
        self.assertIn('local/env:2', self.run_argv())

    def test_root_user_and_missing_image_refused_before_any_docker_call(self):
        for user in ('0', 'root', '0:0', 'root:root'):
            with self.subTest(user=user), self.assertRaisesRegex(ValueError, 'non-root'):
                self.open(agent_sandbox_docker_user=user)
        with self.assertRaisesRegex(ValueError, 'AGENT_SANDBOX_DOCKER_IMAGE'):
            Sandbox(cwd=str(self.code), config={'agent_sandbox_driver': 'docker',
                                                'agent_sandbox_docker': str(FAKE)})
        with self.assertRaisesRegex(ValueError, 'unknown sandbox driver'):
            Sandbox(cwd=str(self.code), config={'agent_sandbox_driver': 'podman'})
        self.assertEqual(self.calls(), [])

    def test_run_failure_keeps_reserved_name_and_release_verifies_absence(self):
        self.put(run_fail=True)
        seen = []
        with self.assertRaisesRegex(RuntimeError, 'docker run failed') as raised:
            Sandbox(cwd=str(self.code), session='s', config=self.cfg, on_allocation=seen.append)
        target = raised.exception.allocation
        self.assertEqual(seen[0], target)
        self.assertEqual(target.driver, 'docker')
        self.assertFalse(target.claim_uid)
        released = Sandbox.release_allocation(target, config=self.cfg, timeout=5, poll_interval=.02)
        self.assertEqual(released.claim, target.claim)

    # ---- exec: shared upper layer -----------------------------------------
    def test_exec_returncode_output_stream_and_docker_exec(self):
        sb = self.open()
        r = sb.execute('echo out; echo err >&2; exit 3')
        self.assertEqual(r['returncode'], 3)
        self.assertIn('out', r['output'])
        self.assertIn('err', r['output'])
        buf = io.BytesIO()
        r = sb.execute("printf 'a\\nb'", stream=buf)
        self.assertEqual((r['returncode'], buf.getvalue()), (0, b'a\nb'))
        execs = [c for c in self.calls() if c[0] == 'exec']
        self.assertTrue(execs)
        self.assertTrue(all(sb._pod in c for c in execs), execs)
        self.assertEqual(sb.exec_argv('true')[:3], [str(FAKE), 'exec', sb._pod])
        self.assertEqual(sb.exec_argv('cat', stdin=True)[:4], [str(FAKE), 'exec', '-i', sb._pod])

    def test_env_allowlist_is_the_shared_one(self):
        os.environ.update(SBX_PROBE='hello', SBX_PROBE_SECRET_TOKEN='must-not-leak')
        self.addCleanup(os.environ.pop, 'SBX_PROBE')
        self.addCleanup(os.environ.pop, 'SBX_PROBE_SECRET_TOKEN')
        sb = self.open(agent_sandbox_forward_env='SBX_PROBE')
        out = sb.execute('echo $SBX_PROBE; echo tok=${SBX_PROBE_SECRET_TOKEN:-none}')['output']
        self.assertIn('hello', out)
        self.assertIn('tok=none', out)
        self.assertNotIn('must-not-leak', out)

    def test_host_command_routing_is_the_shared_one(self):
        sb = self.open(agent_sandbox_code_dirs=str(self.code), agent_sandbox_host_commands='git version')
        before = len(self.calls())
        r = sb.execute('git version', str(self.code))
        self.assertEqual(r['returncode'], 0)
        self.assertIn('git version', r['output'])
        ran = [c for c in self.calls()[before:] if c[0] == 'exec' and any('git version' in x for x in c)]
        self.assertEqual(ran, [])  # never sent to the container

    # ---- lifecycle ----------------------------------------------------------
    def test_close_removes_container(self):
        sb = Sandbox(cwd=str(self.code), session='closeme', config=self.cfg)
        name, cid = sb._claim, sb.allocation.claim_uid
        sb.close()
        self.assertIsNone(sb._claim)
        self.assertNotIn(name, self.data()['containers'])
        self.assertIn(['rm', '-f', cid], self.calls())  # by id: never a reused name

    def test_persistent_reuses_by_session_label_then_release(self):
        cfg = dict(self.cfg, agent_sandbox_persistent='true')
        a = Sandbox(cwd=str(self.code), session='keep', config=cfg)
        a.close()
        self.assertEqual(len(self.data()['containers']), 1)
        b = Sandbox(cwd=str(self.code), session='keep', config=cfg)
        self.assertEqual(a.allocation.claim, b.allocation.claim)
        self.assertEqual(a.allocation.claim_uid, b.allocation.claim_uid)
        b.close()
        self.assertEqual(Sandbox.release('keep', config=self.cfg), 1)
        self.assertEqual(self.data()['containers'], {})
        self.assertEqual(Sandbox.release('never-claimed', config=self.cfg), 0)

    def test_release_verified_reads_back_absence(self):
        sb = Sandbox(cwd=str(self.code), session='verify', config=self.cfg)
        saved = []
        sb._on_allocation = saved.append
        target = sb.release_verified(timeout=5, poll_interval=.02)
        self.assertEqual(target.driver, 'docker')
        self.assertTrue(target.inventoried)
        self.assertIn(target, saved)
        self.assertIsNone(sb._claim)
        self.assertEqual(self.data()['containers'], {})
        inspects = [c for c in self.calls() if c[:2] == ['container', 'inspect']]
        self.assertTrue(inspects)
        # ledger round-trip recovery of an already released target is also verified
        again = Sandbox.release_allocation(Allocation(**json.loads(json.dumps(target._asdict()))),
                                           config=self.cfg, timeout=5, poll_interval=.02)
        self.assertEqual(again, target)

    def test_release_unverified_outcomes_raise_with_target(self):
        sb = Sandbox(cwd=str(self.code), session='stuck', config=self.cfg)
        self.addCleanup(lambda: Sandbox.release('stuck', config=self.cfg))
        target = sb.allocation
        self.put(rm_keeps=True)
        with self.assertRaisesRegex(ReleaseError, 'not verified') as raised:
            sb.release_verified(timeout=.5, poll_interval=.05)
        self.assertEqual(raised.exception.allocation, target)
        self.assertEqual(sb._claim, target.claim)  # retained for retry
        self.put(rm_keeps=False, inspect_error=True)
        with self.assertRaisesRegex(ReleaseError, 'Cannot connect'):
            sb.release_verified(timeout=2, poll_interval=.05)
        self.put(inspect_error=False)

    def test_name_reuse_is_refused(self):
        sb = Sandbox(cwd=str(self.code), session='reuse', config=self.cfg)
        self.addCleanup(lambda: Sandbox.release('reuse', config=self.cfg))
        stale = sb.allocation._replace(claim_uid='0' * 64)
        with self.assertRaisesRegex(ReleaseError, 'refusing name reuse'):
            Sandbox.release_allocation(stale, config=self.cfg, timeout=2, poll_interval=.02)
        self.assertIn(sb._claim, self.data()['containers'])

    def test_kubernetes_stays_default(self):
        self.assertEqual(Allocation('ns', 'claim').driver, 'kubernetes')
        self.assertEqual(Allocation(**json.loads(json.dumps(Allocation('ns', 'c')._asdict()))),
                         Allocation('ns', 'c'))
        # Pre-0.5 ledgers have no driver field and still load as kubernetes.
        old = {k: v for k, v in Allocation('ns', 'c')._asdict().items() if k != 'driver'}
        self.assertEqual(Allocation(**old).driver, 'kubernetes')
        self.assertIn('AGENT_SANDBOX_DOCKER', core._ENV_SKIP)
        self.assertIn('DOCKER_HOST', core._ENV_SKIP)


if __name__ == '__main__':
    unittest.main(verbosity=2)
