"""Reproduce the v0.3.0 compatibility contract without a cluster."""
import subprocess
import sys
import threading
from unittest.mock import patch
from self_hosted_agent_sandbox import Sandbox

sb = Sandbox.__new__(Sandbox)
sb._lock, sb._claim, sb._persistent = threading.Lock(), 'claim-a', False
sb._pull_code = lambda: (0, 0)
sb._run_k = lambda *a, **k: subprocess.CompletedProcess(a, 1, '', 'Forbidden')
sb.close()
assert sb._claim is None
print('legacy close: failed delete still forgets _claim')
sb._claim, sb._persistent = 'claim-a', True
sb.close()
assert sb._claim is None
print('legacy persistent close: keeps resource, forgets _claim')

def acquire(self):
    self._claim, self._pod = 'claim-a', 'sandbox-a'
with patch.object(Sandbox, '_acquire', acquire), patch.object(Sandbox, '_exec_simple', side_effect=OSError('post-acquire failure')):
    try:
        Sandbox()
    except OSError as error:
        assert hasattr(error, 'allocation')
        print('constructor exception allocation attribute:', hasattr(error, 'allocation'))
    else:
        raise AssertionError('expected constructor failure')
