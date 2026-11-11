"""Run unchanged gates against a non-editable wheel installation.

Usage: python tests/check_installed_wheel.py /path/to/wheel-venv/bin/python
Tests that prepend ../src are copied into a harness whose src points only at
installed site-packages. No editable checkout is available through that path.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    python = str(Path(sys.argv[1]).absolute())
    probe = """import json, inspect
from self_hosted_agent_sandbox import Sandbox, Allocation, ReleaseError, __version__, core
assert __version__ == '0.4.0'
assert 'on_allocation' in inspect.signature(Sandbox).parameters
assert all(hasattr(Sandbox, n) for n in ('pull', 'release_verified', 'release_allocation'))
a = Allocation('namespace', 'claim')
assert Allocation(**json.loads(json.dumps(a._asdict()))) == a
assert ReleaseError('unverified', a).allocation == a
print(json.dumps({'core': core.__file__, 'version': __version__}))
"""
    env = {k: v for k, v in os.environ.items()
           if k != 'PYTHONPATH' and not k.startswith('AGENT_SANDBOX_')}
    env['KUBECONFIG'] = '/dev/null'
    info = json.loads(subprocess.check_output([python, '-I', '-c', probe], env=env, text=True))
    core = Path(info['core']).resolve()
    assert 'site-packages' in core.parts, f'not an installed wheel: {core}'
    env['SHAS_TEST_CORE'] = str(core)
    print(json.dumps(info), flush=True)
    with tempfile.TemporaryDirectory(prefix='shas-wheel-gates-') as tmp:
        root = Path(tmp)
        shutil.copytree(Path(__file__).parent, root / 'tests')
        (root / 'src').symlink_to(core.parents[1], target_is_directory=True)
        assert (root / 'src/self_hosted_agent_sandbox/core.py').resolve() == core
        for script in ('route_table.py', 'route_fuzz.py', 'core_unit.py',
                       'lifecycle_unit.py', 'artifact_pull.py',
                       'artifact_integrity.py', 'integration_cross.py'):
            print(f'installed-wheel gate: {script}', flush=True)
            subprocess.run([python, str(root / 'tests' / script)], cwd=root,
                           env=env, check=True, timeout=600)
        subprocess.run([python, '-I', '-m', 'self_hosted_agent_sandbox.cli', '--help'],
                       cwd=root, env=env, check=True, timeout=30)


if __name__ == '__main__':
    main()
