"""Offline unit checks of the core's non-routing paths with a fake kubectl. Run: python tests/core_unit.py"""
import importlib.util, io, json, os, stat, sys, tempfile
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("core", os.path.join(here, "..", "src", "agent_sandbox", "core.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d = tempfile.mkdtemp()
log = os.path.join(d, "calls.log")
fake = os.path.join(d, "kubectl")
# fake kubectl: logs argv; "get sandboxclaims -l" lists two claims; "exec" runs the bash script locally; others fail
open(fake, "w").write(f"""#!/usr/bin/env python3
import json, os, subprocess, sys
a = sys.argv[1:]
open({log!r}, "a").write(json.dumps(a) + "\\n")
if "get" in a and "sandboxclaims" in a:
    print(json.dumps({{"items": [{{"metadata": {{"name": "c1"}}}}, {{"metadata": {{"name": "c2"}}}}]}})); sys.exit(0)
if "delete" in a:
    if "c2" in a and os.environ.get("FAKE_DELETE_FAIL"):
        sys.stderr.write("forbidden"); sys.exit(1)
    sys.exit(0)
if "exec" in a:
    i = a.index("--"); sys.exit(subprocess.run(a[i + 1:]).returncode)
sys.stderr.write("boom"); sys.exit(1)
""")
os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
cfg = {"agent_sandbox_kubectl": fake, "agent_sandbox_kubeconfig": "/dev/null"}

# 1. release deletes existing claims and never creates one
assert m.Sandbox.release("my session", config=cfg) == 2
calls = [json.loads(l) for l in open(log)]
assert not any("apply" in c for c in calls), calls
assert sum("delete" in c for c in calls) == 2
assert any("agent-sandbox/session=my-session" in c for c in calls)
print("release ok")

# 2. infrastructure errors raise RuntimeError (not NameError)
sb = m.Sandbox.__new__(m.Sandbox)
sb._kubectl, sb._kubeconfig, sb._ns, sb._pod, sb._container = fake, "/dev/null", "sbx", "p", "main"
try:
    sb._exec_simple("exit 3")
    raise SystemExit("expected RuntimeError")
except RuntimeError as e:
    assert "rc=3" in str(e)
print("errors ok")

# 3. stream accepts any binary file-like object; exit codes propagate
sb._file_sync, sb._env_digest, sb._env_skip, sb._env_skip_globs, sb._host_cmds = None, None, m._ENV_SKIP, (), ()
sb._env_forward, sb._env_forward_globs = frozenset(), ()
sb._exec_simple = lambda *a, **k: ""  # skip the env push
buf = io.BytesIO()
r = sb._default_run("printf 'a\\nb'; exit 5", d, 30, stream=buf)
assert r["returncode"] == 5 and buf.getvalue() == b"a\nb", (r, buf.getvalue())
r = sb._default_run("echo out; echo err >&2", d, 30)
assert r["returncode"] == 0 and "out" in r["output"] and "err" in r["output"], r
print("stream ok")

# 4. release reports delete failures
os.environ["FAKE_DELETE_FAIL"] = "1"
try:
    m.Sandbox.release("my session", config=cfg)
    raise SystemExit("expected RuntimeError")
except RuntimeError as e:
    assert "c2" in str(e) and "forbidden" in str(e), e
del os.environ["FAKE_DELETE_FAIL"]
print("release failure ok")

# 5. timeout holds while output keeps coming; short writes are completed
import time
t0 = time.monotonic()
try:
    sb._default_run("printf first; sleep 3; printf last", d, 0.5, stream=io.BytesIO())
    raise SystemExit("expected TimeoutExpired")
except m.subprocess.TimeoutExpired:
    assert time.monotonic() - t0 < 2, time.monotonic() - t0
class OneByte(io.RawIOBase):
    def __init__(self): self.data = bytearray()
    def writable(self): return True
    def write(self, b): self.data += bytes(b[:1]); return 1
ob = OneByte()
sb._default_run("printf abcdef", d, 30, stream=ob)
assert bytes(ob.data) == b"abcdef", ob.data
print("timeout and short writes ok")

# 6. slow writer: nothing lost, return waits for it; writer errors reach the caller
class Slow(io.RawIOBase):
    def __init__(self): self.data = bytearray()
    def writable(self): return True
    def write(self, b): time.sleep(1.5); self.data += bytes(b); return len(b)
sw = Slow()
r = sb._default_run("printf payload", d, 1, stream=sw)
assert r["returncode"] == 0 and bytes(sw.data) == b"payload", (r, sw.data)
class Full(io.RawIOBase):
    def writable(self): return True
    def write(self, b): raise OSError("disk full")
try:
    sb._default_run("printf x", d, 30, stream=Full())
    raise SystemExit("expected OSError")
except OSError as e:
    assert "disk full" in str(e), e
class NoneWriter(io.RawIOBase):
    def writable(self): return True
    def write(self, b): return None
t0 = time.monotonic()
try:
    sb._default_run("printf x; sleep 20", d, 30, stream=NoneWriter())
    raise SystemExit("expected OSError")
except OSError as e:
    assert "no bytes" in str(e) and time.monotonic() - t0 < 5, (e, time.monotonic() - t0)
print("slow and failing writers ok")

# 7. env.sh carries only allowlisted host variables; default allowlist is empty
os.environ.update({"OPENAI_API_KEY": "sk-host-secret", "MY_BUILD_FLAG": "on", "NPM_CONFIG_REGISTRY": "https://r.example/"})
def payload(fwd):
    s7 = m.Sandbox.__new__(m.Sandbox)
    c = {"agent_sandbox_forward_env": fwd} if fwd is not None else {}
    v = m._as_list(m._cfg(c, "agent_sandbox_forward_env", "AGENT_SANDBOX_FORWARD_ENV", ""))
    s7._env_forward = frozenset(x for x in v if not m._GLOB_CHARS & set(x))
    s7._env_forward_globs = tuple(x for x in v if m._GLOB_CHARS & set(x))
    s7._env_skip, s7._env_skip_globs, s7._host_cmds = m._ENV_SKIP, (), ()
    return s7._env_payload()
p0 = payload(None)
assert "export " not in p0 and "sk-host-secret" not in p0, p0
p1 = payload("MY_BUILD_FLAG,NPM_CONFIG_*")
assert "export MY_BUILD_FLAG=on" in p1 and "NPM_CONFIG_REGISTRY" in p1 and "sk-host-secret" not in p1, p1
os.environ["AGENT_SANDBOX_FORWARD_ENV"] = "MY_BUILD_FLAG"
p2 = payload(None)
assert "export MY_BUILD_FLAG=on" in p2 and "NPM_CONFIG_REGISTRY" not in p2 and "sk-host-secret" not in p2, p2
del os.environ["AGENT_SANDBOX_FORWARD_ENV"]
print("env allowlist ok")

# 8. gitconfig pushed to the sandbox keeps identity, drops credential-bearing settings
gh = tempfile.mkdtemp(); gc = os.path.join(gh, ".gitconfig")
open(gc, "w").write("""[user]\n\tname = Dev\n\temail = dev@example.com\n\tsigningkey = ABCDEF\n
[credential]\n\thelper = store\n[http]\n\textraheader = AUTHORIZATION: bearer ghp_header\n\tproxy = http://u:pw@proxy:3128\n
[url "https://u:ghp_url@git.example/"]\n\tinsteadOf = https://git.example/\n[github]\n\ttoken = ghp_section\n
[core]\n\tautocrlf = input\n\taskpass = /bin/echo\n[alias]\n\tco = checkout\n\tgettoken = !echo ghp_alias\n
[remote "origin"]\n\turl = https://u:ghp_remote@git.example/r.git\n""")
sent = []
s8 = m.Sandbox.__new__(m.Sandbox); s8._exec_simple = lambda cmd, **k: sent.append(cmd)
old_home = os.environ["HOME"]; os.environ["HOME"] = gh
try:
    s8._push_gitconfig()
finally:
    os.environ["HOME"] = old_home
script = "".join(sent)
assert "user.name Dev" in script and "core.autocrlf input" in script and "alias.co checkout" in script, script
for bad in ("ghp_", "pw@", "signingkey", "credential", "extraheader", "proxy", "askpass", "insteadof", "remote."):
    assert bad not in script.lower(), (bad, script)
print("gitconfig allowlist ok")
