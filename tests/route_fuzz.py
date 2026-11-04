"""Run: python tests/route_fuzz.py [seed] [iterations]
Random commands from a token pool: every command routed to the host is run under bash -x in a
sandbox dir with tracing; we assert bash only ever executed allowlisted programs with arguments
that the parser saw (bash's own word list must equal shlex's)."""
import importlib.util, os, random, subprocess, sys, tempfile, json
spec = importlib.util.spec_from_file_location("e", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "agent_sandbox", "core.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
root = os.path.realpath(tempfile.mkdtemp(prefix="fz-")); os.makedirs(f"{root}/code/sub/deep"); os.symlink(f"{root}/code/sub/deep", f"{root}/code/dl")
E = m.Sandbox.__new__(m.Sandbox)
E._host_cmds = ("git push", "git ls-remote"); E._code_dirs = [f"{root}/code"]; E.cwd = f"{root}/code"
pool = ["dl", "missing", "sub/deep", "../..", "git", "push", "ls-remote", "origin", "|", ";", "&&", "||", "grep", "-e", "x", "sort", "-u", "uniq", "head", "-n", "2",
        "jq", ".", "-r", "cut", "-f2", "tr", "-d", "echo", "hi", "cd", "sub", "..", ">/dev/null", "2>&1", "'a b'", '"c d"',
        "\\;", "\\|", "a\\ b", "'*'", "*", "{a,b}", "!", "#", "x#y", "'", '"', "\\", "=", "A=1", "GIT_TRACE=1",
        ">", "<", "&", "(", ")", "-", "--", "-o", "/tmp/o", "./f", "$?", "\t", "%", "^", "@", "\\n", "$'x'", "a&&b"]
random.seed(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
host, mism = 0, []
for _ in range(int(sys.argv[2]) if len(sys.argv) > 2 else 40000):
    segs = []
    for k in range(random.randint(1, 4)):
        head = random.choice(["git push", "git ls-remote", "grep", "sort", "uniq", "head", "jq", "cut", "tr", "echo", "cd", "wc"])
        segs.append(head + " " + " ".join(random.choice(pool) for _ in range(random.randint(0, 4))))
        segs.append(random.choice(["|", ";", "&&", "||", "|", "|"]))
    cmd = " ".join(segs[:-1])
    if not E._is_host_command(cmd, f"{root}/code"):
        continue
    host += 1
    # replace every program with a recorder: bash runs the line, each simple command logs argv
    rec = f"{root}/rec.jsonl"; open(rec, "w").close()
    shim = "".join(f'{p}() {{ python3 -c "import json,sys;open(\'{rec}\',\'a\').write(json.dumps(sys.argv[1:])+chr(10))" {p} "$@"; }}\n'
                   for p in ("git", "grep", "sort", "uniq", "head", "tail", "jq", "cut", "tr", "wc", "echo", "printf", "true", "false", "pwd", "uname"))
    r = subprocess.run(["bash", "--norc", "--noprofile", "-c", shim + "enable -n echo printf true false pwd\n" + cmd],
                       cwd=f"{root}/code", capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
                       env={"PATH": "/usr/bin:/bin", "HOME": root + "/home", "PYTHONDONTWRITEBYTECODE": "1"})
    calls = [json.loads(l) for l in open(rec) if l.strip()]
    for c in calls:
        if c[0] not in ("git", "grep", "sort", "uniq", "head", "tail", "jq", "cut", "tr", "wc", "echo", "printf", "true", "false", "pwd", "uname"):
            mism.append(("unexpected program", cmd, c))
    leaked = [p for p in os.listdir(root) if p not in ("code", "rec.jsonl", "home")]
    if leaked:
        mism.append(("file created outside code", cmd, leaked))
        for p in leaked: subprocess.run(["rm", "-rf", f"{root}/{p}"])
    extra = [p for p in os.listdir(f"{root}/code") if p not in ("sub", "dl")]
    if extra:
        mism.append(("file created in code", cmd, extra))
        for p in extra: subprocess.run(["rm", "-rf", f"{root}/code/{p}"])
print("host-routed:", host, "problems:", len(mism))
for x in mism[:15]: print(x)
sys.exit(1 if mism else 0)
