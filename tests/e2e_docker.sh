#!/usr/bin/env bash
# End-to-end test of the sbx CLI on the docker driver against a LOCAL Docker daemon (no cluster).
# Same checks as tests/e2e.sh where they apply. Needs an already-local image with bash, tar, find,
# sha256sum and git (never pulled), e.g.  SBX_TEST_IMAGE=node:24-bookworm bash tests/e2e_docker.sh
# Runs from this repo checkout; creates and removes its own containers (label agent-sandbox/app=sbx-e2e).
set -u
here=$(cd "$(dirname "$0")/.." && pwd)
IMG=${SBX_TEST_IMAGE:?set SBX_TEST_IMAGE to an already-local image}
docker image inspect "$IMG" >/dev/null 2>&1 || { echo "image $IMG is not local; this test never pulls" >&2; exit 2; }
base=$(cd "${TMPDIR:-/tmp}" && pwd -P)/sbx-e2e-docker.$$
W=$base/workdir; rm -rf "$base"; mkdir -p "$W"; cd "$W" || exit 2
git init -q -b main . && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git init -q --bare "$base/remote.git" && git remote add origin "$base/remote.git"
export PYTHONPATH=$here/src AGENT_SANDBOX_DRIVER=docker AGENT_SANDBOX_DOCKER_IMAGE=$IMG
export AGENT_SANDBOX_CODE_DIRS=$W SBX_PROBE=hello SBX_PROBE_SECRET_TOKEN=must-not-leak AGENT_SANDBOX_FORWARD_ENV=SBX_PROBE
sbx(){ local c=$1; shift; python3 -m self_hosted_agent_sandbox.cli "$c" --app sbx-e2e "$@"; }
pass=0; fail=0; ck(){ if eval "$2"; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1"; fi; }
left(){ docker ps -aq --filter label=agent-sandbox/app=sbx-e2e | wc -l | tr -d ' '; }
echo "image=$IMG runtime=${AGENT_SANDBOX_DOCKER_RUNTIME:-default(runc)} host=$(hostname) workdir=$W"

out=$(sbx exec --session e2e1 -- 'id -u; hostname; echo $SBX_PROBE; echo tok=${SBX_PROBE_SECRET_TOKEN:-none}' 2>&1); echo "$out" | sed 's/^/  | /'
ck runs_in_container '[[ "$out" != *"$(hostname)"* ]]'
ck runs_as_non_root '[ "$(echo "$out" | head -1)" != 0 ]'
ck env_forwarded '[[ "$out" == *hello* ]]'
ck env_not_listed_stays_on_host '[[ "$out" == *tok=none* && "$out" != *must-not-leak* ]]'
sbx exec --session e2e2 -- 'echo made-in-sandbox > new.txt && git add new.txt && git -c user.email=s@s -c user.name=s commit -qm sbx' >/dev/null 2>&1
ck file_pulled_back '[ "$(cat "$W/new.txt" 2>/dev/null)" = made-in-sandbox ]'
ck commit_visible_on_host '[ "$(git -C "$W" log -1 --format=%s)" = sbx ]'
sbx exec --session e2e2b -- 'mkdir -p .git/hooks && echo "touch /tmp/pwned" > .git/hooks/pre-push && chmod +x .git/hooks/pre-push; git config core.hooksPath /tmp' >/dev/null 2>&1
ck git_hooks_config_not_pulled '[ ! -e "$W/.git/hooks/pre-push" ] && ! git -C "$W" config core.hooksPath >/dev/null'
out=$(sbx exec --session e2e3 -- 'uname -n' 2>&1); ck default_runs_in_sandbox '[[ "$out" != "$(hostname)" ]]'
# git network commands are host commands by default: push from the sandbox session lands on the host remote
out=$(sbx exec --session e2e3b -- 'git push -q origin main' 2>&1); echo "$out" | sed 's/^/  | /'
ck git_push_runs_on_host '[ "$(git --git-dir="$base/remote.git" log -1 --format=%s main 2>/dev/null)" = sbx ]'
out=$(sbx exec --session e2e3c -- 'cat /dev/null; git push origin main; echo rc=$?' 2>&1)
ck git_network_stub_in_sandbox '[[ "$out" == *"not available inside the code sandbox"* && "$out" == *rc=97* ]]'
out=$(AGENT_SANDBOX_HOST_COMMANDS="git version" sbx exec --session e2e4 -- 'git version; uname -n' 2>&1); ck host_command_on_host '[[ "$out" == *"$(hostname)"* ]]'
out=$(AGENT_SANDBOX_HOST_COMMANDS="git version" sbx exec --session e2e5 -- 'python3 -c "print(1)"; git version; uname -n' 2>&1); ck mixed_stays_in_sandbox '[[ "$out" != *"$(hostname)"* ]]'
sbx exec --session e2e6 --keep -- 'echo 1 > /tmp/keep-mark' >/dev/null 2>&1
out=$(sbx exec --session e2e6 --keep -- 'cat /tmp/keep-mark' 2>&1); ck keep_reuses_claim '[[ "$out" == 1 ]]'
ck keep_is_one_container '[ "$(docker ps -q --filter label=agent-sandbox/session=e2e6 | wc -l | tr -d " ")" = 1 ]'
sbx release --session e2e6 >/dev/null 2>&1
t0=$(date +%s); sbx release --session never-claimed >/dev/null 2>&1; t1=$(date +%s)
ck release_unknown_is_quick '[ $((t1 - t0)) -lt 10 ]'
sbx exec --session e2e7 -- 'exit 7' >/dev/null 2>&1; ck exit_code_propagates '[ $? = 7 ]'
printf 'echo a\necho b\n' | sbx shell --session e2e8 > "$base/sh.out" 2>&1; ck shell_mode '[ "$(tr -d "\n" < "$base/sh.out")" = ab ]'
# checkpoints (docs/checkpoint.md) are host-side and work unchanged on this driver
out=$(AGENT_SANDBOX_CHECKPOINT=true python3 - <<'PY' 2>&1
import os
from self_hosted_agent_sandbox import Sandbox
sb = Sandbox(cwd=os.getcwd(), session="e2e10", app="sbx-e2e")
r = sb.execute("echo ck > ck.txt")
print("checkpoint", bool(r.get("checkpoint")), len(sb.checkpoints()))
sb.close()
PY
); echo "  | $out"
ck checkpoint_on_docker '[[ "$out" == *"checkpoint True 1"* ]]'
# SDK: verified release reads back docker inspect absence
out=$(python3 - <<'PY' 2>&1
import json, subprocess
from self_hosted_agent_sandbox import Sandbox
sb = Sandbox(cwd="/", session="e2e9", app="sbx-e2e", config={"agent_sandbox_code_dirs": "/nonexistent"})
cid = sb.allocation.claim_uid
t = sb.release_verified(timeout=30)
r = subprocess.run(["docker", "container", "inspect", cid], capture_output=True, text=True)
print(json.dumps({"driver": t.driver, "inventoried": t.inventoried, "inspect_rc": r.returncode,
                  "absent": "no such" in r.stderr.lower()}))
PY
); echo "  | $out"
ck release_verified_reads_back_absence '[[ "$out" == *"\"absent\": true"* && "$out" == *"\"driver\": \"docker\""* ]]'
ck no_leftover_containers '[ "$(left)" = 0 ]'
echo "passed=$pass/$((pass+fail))"
rm -rf "$base"
[ "$fail" = 0 ]
