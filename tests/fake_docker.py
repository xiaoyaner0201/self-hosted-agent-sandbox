#!/usr/bin/env python3
"""Fixed docker CLI interface for offline driver checks; never contacts a Docker daemon.

State: JSON file named by FAKE_DOCKER_STATE ({"containers": {name: {...}}, knobs...}).
``exec`` runs the command locally with HOME pointed at a scratch dir (never the real one);
``exec -u 0`` (root preparation) is only logged. Knobs: run_fail, rm_keeps, inspect_error.
"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

path = Path(os.environ['FAKE_DOCKER_STATE'])
a = sys.argv[1:]
state = json.loads(path.read_text()) if path.exists() else {}
state.setdefault('containers', {})
with Path(str(path) + '.log').open('a') as log:
    log.write(json.dumps(a) + '\n')


def save():
    path.write_text(json.dumps(state))


def fail(message, rc=1):
    print(message, file=sys.stderr)
    sys.exit(rc)


def lookup(ref):
    for name, c in state['containers'].items():
        if ref in (name, c['id']):
            return name, c
    return None, None


if a[0] == 'run':
    if state.get('run_fail'):
        fail('docker: Error response from daemon: unknown runtime specified')
    name = a[a.index('--name') + 1]
    labels = dict(a[i + 1].split('=', 1) for i, x in enumerate(a) if x == '--label')
    cid = uuid.uuid4().hex + uuid.uuid4().hex
    state['containers'][name] = {'id': cid, 'labels': labels, 'running': True}
    save()
    print(cid)
    sys.exit(0)
if a[:2] == ['container', 'inspect']:
    if state.get('inspect_error'):
        fail('Cannot connect to the Docker daemon')
    name, c = lookup(a[-1])
    if c is None:
        fail(f'Error response from daemon: No such container: {a[-1]}')
    print(json.dumps([{'Id': c['id'], 'Name': '/' + name, 'State': {'Running': c['running']},
                       'Config': {'Labels': c['labels']}}]))
    sys.exit(0)
if a[0] == 'ps':
    want = [x.split('=', 1)[1] for i, x in enumerate(a) if i and a[i - 1] == '--filter' and x.startswith('label=')]
    for name, c in state['containers'].items():
        if all(c['labels'].get(k) == v for k, v in (w.split('=', 1) for w in want)):
            print(name)
    sys.exit(0)
if a[:2] == ['rm', '-f']:
    for ref in a[2:]:
        name, c = lookup(ref)
        if c is not None and not state.get('rm_keeps'):
            del state['containers'][name]
    save()
    sys.exit(0)
if a[0] == 'exec':
    rest = a[1:]
    root = False
    while rest and rest[0].startswith('-'):
        if rest[0] in ('-u', '--user', '-e', '--env', '-w', '--workdir'):
            root = root or (rest[0] in ('-u', '--user') and rest[1] == '0')
            rest = rest[2:]
        else:
            rest = rest[1:]
    name, c = lookup(rest[0])
    if c is None or not c['running']:
        fail(f'Error response from daemon: No such container: {rest[0]}')
    if root:
        sys.exit(0)
    home = os.environ['FAKE_DOCKER_HOME']
    env = {'HOME': home, 'PATH': os.environ.get('PATH', '/usr/bin:/bin')}  # a container sees none of the host env
    sys.exit(subprocess.run(rest[1:], env=env).returncode)
fail('unexpected docker operation: ' + ' '.join(a))
