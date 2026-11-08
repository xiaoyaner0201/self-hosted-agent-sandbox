"""Router table: which commands may run on the host. Offline, no cluster needed.
Run: python tests/route_table.py (no cluster, no dependencies)"""
import importlib.util, os, sys, tempfile, types
spec = importlib.util.spec_from_file_location("e", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "self_hosted_agent_sandbox", "core.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
E = m.Sandbox.__new__(m.Sandbox)
E._host_cmds = m._DEFAULT_HOST_CMDS + ("mycli", "gh pr view", "gh pr create")
_w = os.path.realpath(tempfile.mkdtemp()); os.makedirs(f"{_w}/w/x/sub")
E._code_dirs = [f"{_w}/w"]; E.cwd = f"{_w}/w/x"
cases = {
 'git push origin feat': True,
 'cd sub && git push -u origin HEAD 2>&1 | tail -3': True,
 'git push origin HEAD >/dev/null 2>&1; echo rc=$?': True,
 'GIT_TRACE=1 git ls-remote https://x/y.git 2>&1 | head -2': True,
 'mycli issue get X | jq .status': True,
 'mycli issue get X --output json; mycli issue comment list X': True,
 "mycli issue comment add X --content 'see /docs/a.md'": True,
 'python3 -c "print(1)"; git push origin main': False,   # reviewer counterexample 1
 'echo $(id); git push origin main': False,               # reviewer counterexample 2
 'echo `id`; git push': False,
 'git push > /tmp/out': False,
 'git push; bash -c "id"': False,
 'git push & sleep 1': False,
 '(git push)': False,
 'git push <<EOF\nx\nEOF': False,
 'git push\nid': False,
 'env FOO=1 git push': False,
 'xargs git push < list': False,
 'sed -n 1p f; git push': False,
 'cd . && mvn -B test && git push': False,
 'mvn test': False,
 'git status': False,
 "echo 'git push'": False,
 'git pushx': False,
 'git push -u origin HEAD': True,
 'gh pr view 12 --json state | jq .state': True,
 "gh alias set --shell pwn 'id'; gh pwn": False,       # round-2 counterexample
 'gh extension install .; gh pwn': False,              # round-2 counterexample
 'gh api /user': False,
 'GIT_SSH_COMMAND=id git push': False,
 'LD_PRELOAD=/w/x.so git fetch': False,
 'git fetch --upload-pack=id origin': False,
 'git fetch --upload-pack id origin': False,
 'git clone -u id https://x/y': False,
 'git clone -cuser.x=1 https://x/y': False,
 'git clone --template=/w/t https://x/y': False,
 'git push --receive-pack=id origin': False,
 'git push --exec=id origin': False,
 'git -c core.sshCommand=id push': False,
 'git -C /tmp push': False,
 'GIT_TRACE=/tmp/x git ls-remote origin': False,      # round-3 counterexample
 'GIT_TRACE_PACKET=/etc/x git fetch': False,
 'GIT_TRACE=1 git push': True,
 'LANG=C.UTF-8 git push': True,
 'LANG=$(id) git push': False,
 'git clone /w/src /tmp/outside/clone': False,          # round-4 counterexample
 'git clone https://x/y.git /tmp/out': False,
 'git clone https://x/y.git sub': True,
 'git clone git@gitlab.example:g/r.git': True,
 'git clone file:///etc/x y': False,
 'git push origin HEAD:refs/heads/x': True,
 'echo $GITLAB_TOKEN; git push': False,
 'cat /etc/passwd; git push': False,
 'cat a.txt; git push': False,
 'cat ../../etc/passwd; git push': False,
 'cat ~/.git-credentials; git push': False,
 'cd /tmp && git push': False,
 'cd .. && git push': True,
 'cd ../.. && git push': False,
 'cd && git push': False,
 'git remote add up /tmp/evil.git': False,
 'git remote set-url origin https://x/y.git': True,
 'cat * ; git push': False,
 'cat {a,b}; git push': False,
 'cat /w/x/../../etc/passwd; git push': False,
 'sort -o/tmp/x in | git ls-remote origin': False,      # round-5 partial findings
 'git ls-remote origin | sort -o/tmp/x': False,
 'git ls-remote origin | sort --output=/tmp/x': False,
 'git ls-remote origin | uniq - /tmp/x': False,
 'git ls-remote origin | grep -f/tmp/pat': False,
 'git ls-remote origin | jq --rawfile a /etc/passwd .': False,
 'git ls-remote origin | date -f /etc/passwd': False,
 'cd --; git ls-remote origin': False,
 'cd -; git ls-remote origin': False,
 'cd -P; git ls-remote origin': False,
 'cd; git ls-remote origin': False,
 'git ls-remote origin | sort -u | head -5': True,
 'git ls-remote origin | grep -c refs/heads': True,
 "git ls-remote origin | grep -E 'v[0-9]+'": True,
 'git ls-remote origin | grep -E v[0-9]+': False,
 'git ls-remote origin | cut -f2 | sort': True,
 'git ls-remote origin | jq -r .x': True,
 'git ls-remote origin | tr -d x': True,
 'echo -n hi; git push': True,
 'grep x; git push': False,
 'git ls-remote x#y > push': False,                     # fuzzer finding
 'git push # comment': False,
 'cd .. | git clone https://x/y.git ../escape': False,  # round-6 finding
 'git ls-remote origin | cd ..': False,
 'cd .. && git clone https://x/y.git z': True,
 'cd sub/missing || git clone https://x/y.git ../outside': False,  # round-7 finding
 'cd .. ; git push': False,
 'git push && cd ..': False,
 'cd .. && cd .. && git push': False,
}
bad = 0
for c, want in cases.items():
    got = E._is_host_command(c)
    bad += got != want
    if got != want: print("BAD", want, got, repr(c))
print("routing bad =", bad, "of", len(cases))
assert bad == 0
# git protection
for p, want in {"/w/r/.git/hooks/pre-push": True, "/w/r/.git/config": True, "/w/r/.git/modules/x/config": True,
                "/w/r/.git/refs/heads/main": False, "/w/r/.git/objects/ab/cd": False, "/w/r/src/config": False,
                "/w/r/.git/worktrees/a/HEAD": True, "/w/r/.git/config.worktree": True,
                "/w/r/.git/commondir": True, "/w/r/.git/gitdir": True, "/w/r/.git/info/attributes": True,
                "/w/r/.git/HEAD": False, "/w/r/.git/index": False, "/w/r/.git/logs/HEAD": False,
                "/w/r/.git/packed-refs": False, "/w/r/.git/refs/x/.git/config": True,
                "/w/r/.git": True, "/w/r/sub/.git": True}.items():   # gitfile (not a dir)  — round-3 counterexample
    assert m._git_protected(p) == want, p
assert m._git_protected("/w/r/.git", True) is False
assert m._git_protected("/w/r/.git/refs/tags", True) is False
print("git_protected ok")
# symlink containment
d = tempfile.mkdtemp(); out = tempfile.mkdtemp()
os.makedirs(f"{d}/code"); os.symlink(out, f"{d}/code/link")
assert m._inside(f"{d}/code/sub", [f"{d}/code"]) and not m._inside(f"{d}/code/link", [f"{d}/code"])
print("inside ok")
# env: allowlist only (empty forwards nothing), skip list and host-identity names still win
E._env_forward, E._env_forward_globs = frozenset(), ()
E._env_skip, E._env_skip_globs = m._ENV_SKIP, ()
assert not any(E._env_forwarded(k) for k in ("OPENAI_API_KEY", "GITLAB_TOKEN", "BAR", "LANG"))
E._env_forward = frozenset({"BAR", "PATH", "FOO", "bad-name"}); E._env_forward_globs = ("NPM_CONFIG_*",)
E._env_skip = m._ENV_SKIP | {"FOO"}; E._env_skip_globs = ("*_TOKEN",)
assert E._env_forwarded("BAR") and E._env_forwarded("NPM_CONFIG_REGISTRY")
assert not E._env_forwarded("NPM_CONFIG_TOKEN") and not E._env_forwarded("FOO") and not E._env_forwarded("PATH")
assert not E._env_forwarded("bad-name") and not E._env_forwarded("OPENAI_API_KEY")
print("env allowlist ok")
# gitconfig: exact key allowlist; repository configs also keep what git needs to work
for k in ("user.name", "user.email", "core.autocrlf", "init.defaultbranch", "pull.rebase", "push.default"):
    assert m._gitconfig_key_forwarded(k) and m._gitconfig_key_forwarded(k, repo=True), k
for k in ("credential.helper", "http.extraheader", "http.proxy", "url.https://x/.insteadof", "include.path",
          "includeif.gitdir:~/w/.path", "user.signingkey", "user.token", "core.askpass", "core.sshcommand",
          "core.gitproxy", "core.hookspath", "core.fsmonitor", "core.pager", "core.editor", "alias.co",
          "alias.release", "sendemail.smtppass", "github.token", "gpg.program", "credential.https://x.helper",
          "remote.origin.url", "branch.main.remote"):
    assert not m._gitconfig_key_forwarded(k), k
for k in ("core.repositoryformatversion", "core.bare", "extensions.objectformat", "extensions.worktreeconfig", "remote.origin.url",
          "remote.origin.fetch", "remote.a.b.url", "branch.main.merge", "branch.feat/x.remote", "submodule.lib.url"):
    assert m._gitconfig_key_forwarded(k, repo=True), k
for k in ("remote.origin.uploadpack", "remote.origin.receivepack", "remote.origin.proxy", "remote.origin.vcs",
          "http.https://x.extraheader", "core.sshcommand", "core.hookspath", "core.fsmonitor", "alias.x",
          "credential.helper", "filter.lfs.process", "diff.x.textconv", "branch.main.description",
          "extensions.anything", "extensions.partialclone"):
    assert not m._gitconfig_key_forwarded(k, repo=True), k
assert m._is_repo_gitconfig("/w/.git/config") and m._is_repo_gitconfig("/w/.git/modules/lib/config")
assert m._is_repo_gitconfig("/w/.git/config.worktree") and m._is_repo_gitconfig("/w/.git/worktrees/x/config.worktree")
assert not m._is_repo_gitconfig("/w/config") and not m._is_repo_gitconfig("/w/.git/hooks/x")
print("gitconfig allowlist ok")
# safe_makedirs + symlink parent
d = os.path.realpath(tempfile.mkdtemp()); out = os.path.realpath(tempfile.mkdtemp())
root = f"{d}/code"; os.makedirs(root); os.symlink(out, f"{root}/lnk")
assert m._safe_makedirs(f"{root}/a/b", [root]) and os.path.isdir(f"{root}/a/b")
assert not m._safe_makedirs(f"{root}/lnk/new/deep", [root]) and not os.path.exists(f"{out}/new")
print("safe_makedirs ok")
# symlink in the tree pointing out: lexically inside, really outside
E2 = m.Sandbox.__new__(m.Sandbox)
E2._host_cmds = m._DEFAULT_HOST_CMDS; E2._code_dirs = [root]; E2.cwd = root
assert E2._is_host_command("git push") and not E2._is_host_command("cat lnk/x; git push") and not E2._is_host_command("cd lnk && git push")
os.makedirs(f"{root}/sub/deep"); os.symlink(f"{root}/sub/deep", f"{root}/dl")
assert not E2._is_host_command("cd dl && cd .. && git clone https://x/y.git ../outside")  # round-7 finding
assert not E2._is_host_command("cd dl && git push")
assert not E2._is_host_command("git push", f"{root}/not-there")  # round-8 finding: missing cwd
print("symlink confinement ok")
