#!/usr/bin/env python3
"""Fixed kubectl subprocess interface for offline lifecycle checks; never contacts a cluster."""
import json
import sys
import time
from pathlib import Path

a = sys.argv[1:]
path = Path(a[a.index('--kubeconfig') + 1])
ns = a[a.index('-n') + 1]
a = a[a.index('-n') + 2:]
state = json.loads(path.read_text())
body = sys.stdin.read() if a[0] in ('apply', 'delete') and '-f' in a else ''
with Path(str(path) + '.log').open('a') as log:
    log.write(json.dumps({'args': a, 'namespace': ns, 'body': body}) + '\n')

def save():
    path.write_text(json.dumps(state))

def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)

if a[0] == 'apply':
    obj = json.loads(body)
    claim = state['objects']['claim']
    claim['metadata']['name'] = obj['metadata']['name']
    save()
    if state.get('apply_fail'):
        fail('ambiguous apply failure after create')
    sys.exit(0)
if a[0] == 'get':
    if a[1] in ('sandboxclaim', 'sandboxclaims.extensions.agents.x-k8s.io'):
        kind = 'claim'
    elif a[1] == 'sandboxes.agents.x-k8s.io':
        kind = 'sandbox'
    elif a[1] == 'pods':
        kind = 'pod'
    else:
        fail('unexpected resource')
    if state.get('forbidden') == kind:
        fail('Forbidden: observer permission denied')
    if state.get('stall') == kind:
        time.sleep(10)
    if state.get('malformed') == kind:
        print('{}')
        sys.exit(0)
    obj = state['objects'].get(kind)
    if obj and obj['metadata']['name'] == a[2] and obj['metadata']['namespace'] == ns:
        print(json.dumps(obj))
    sys.exit(0)
if a[0] == 'delete':
    if state.get('delete_fail'):
        fail('Forbidden: delete denied')
    if a[1].startswith('--raw='):
        claim = state['objects'].get('claim')
        opts = json.loads(body)
        if opts['apiVersion'] != 'v1' or opts['kind'] != 'DeleteOptions' or opts['propagationPolicy'] != 'Foreground':
            fail('invalid DeleteOptions')
        if not claim:
            fail('NotFound')
        if opts['preconditions'] != {'uid': claim['metadata']['uid'], 'resourceVersion': claim['metadata']['resourceVersion']}:
            fail('Conflict: preconditions failed')
        expected = f"--raw=/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/{ns}/sandboxclaims/{claim['metadata']['name']}"
        if a[1] != expected:
            fail('wrong raw URI')
    mode = state.get('mode', 'cascade')
    if mode == 'cascade':
        state['objects'] = {}
    elif mode == 'claim_only':
        state['objects'].pop('claim', None)
    elif mode == 'replace_pod':
        state['objects']['pod']['metadata']['uid'] = 'new-pod-uid'
    save()
    print('{}')
    sys.exit(0)
fail('unexpected operation')
