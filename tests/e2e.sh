set -u
# End-to-end test of the sbx CLI against a real cluster (no agent framework).
# Mount: /in/src (this repo's src), /in/kubectl, /in/kc (kubeconfig from make-kubeconfig.sh); pool name below.
export PYTHONPATH=/in/src AGENT_SANDBOX_KUBECTL=/in/kubectl AGENT_SANDBOX_KUBECONFIG=/in/kc AGENT_SANDBOX_POOL=${SBX_TEST_POOL:?set SBX_TEST_POOL}
W=/workspaces/sbx-e2e/w1/workdir; rm -rf /workspaces/sbx-e2e; mkdir -p $W; cd $W
git init -q -b main . && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
export AGENT_SANDBOX_CODE_DIRS=$W SBX_PROBE=hello SBX_PROBE_SECRET_TOKEN=must-not-leak AGENT_SANDBOX_FORWARD_ENV=SBX_PROBE
SBX="python3 -m self_hosted_agent_sandbox.cli"
pass=0; fail=0; ck(){ if eval "$2"; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1"; fi; }
out=$($SBX exec --session e2e1 -- 'cat /proc/version; hostname; echo $SBX_PROBE; echo tok=${SBX_PROBE_SECRET_TOKEN:-none}' 2>&1); ck runs_in_gvisor '[[ "$out" == *gVisor* || "$out" == *gvisor* ]] && [[ "$out" != *"$(hostname)"* ]]'
ck env_forwarded '[[ "$out" == *hello* ]]'
ck env_not_listed_stays_on_host '[[ "$out" == *tok=none* && "$out" != *must-not-leak* ]]'
$SBX exec --session e2e2 -- 'echo made-in-sandbox > new.txt && git add new.txt && git -c user.email=s@s -c user.name=s commit -qm sbx' >/dev/null 2>&1
ck file_pulled_back '[ "$(cat $W/new.txt 2>/dev/null)" = made-in-sandbox ]'
ck commit_visible_on_host '[ "$(git -C $W log -1 --format=%s)" = sbx ]'
out=$($SBX exec --session e2e3 -- 'uname -n' 2>&1); ck default_runs_in_sandbox '[[ "$out" != "$(hostname)" ]]'
# a listed host command (harmless "git version") plus glue runs on the host; glue alone never does
out=$(AGENT_SANDBOX_HOST_COMMANDS="git version" $SBX exec --session e2e4 -- 'git version; uname -n' 2>&1); ck host_command_on_host '[[ "$out" == *"$(hostname)"* ]]'
out=$(AGENT_SANDBOX_HOST_COMMANDS="git version" $SBX exec --session e2e5 -- 'python3 -c "print(1)"; git version; uname -n' 2>&1); ck mixed_stays_in_sandbox '[[ "$out" != *"$(hostname)"* ]]'
$SBX exec --session e2e6 --keep -- 'echo 1 > /tmp/keep-mark' >/dev/null 2>&1
out=$($SBX exec --session e2e6 --keep -- 'cat /tmp/keep-mark' 2>&1); ck keep_reuses_claim '[[ "$out" == 1 ]]'
$SBX release --session e2e6 >/dev/null 2>&1
t0=$(date +%s); $SBX release --session never-claimed >/dev/null 2>&1; t1=$(date +%s)
ck release_unknown_is_quick '[ $((t1 - t0)) -lt 10 ]'
n=$(/in/kubectl --kubeconfig /in/kc -n sbx get sandboxclaims -l agent-sandbox/app=sbx -o name 2>/dev/null | wc -l); ck no_leftover_claims '[ "$n" = 0 ]'
$SBX exec --session e2e7 -- 'exit 7' >/dev/null 2>&1; ck exit_code_propagates '[ $? = 7 ]'
printf 'echo a\necho b\n' | $SBX shell --session e2e8 > /tmp/sh.out 2>&1; ck shell_mode '[ "$(tr -d "\n" < /tmp/sh.out)" = ab ]'
echo "passed=$pass/$((pass+fail))"
rm -rf /workspaces/sbx-e2e
