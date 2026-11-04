#!/bin/bash
# Write a kubeconfig for the agent host that can only claim sandboxes and exec into them.
#   cluster/make-kubeconfig.sh https://10.0.0.11:6443 > agent-sandbox.kubeconfig
# Run where kubectl already talks to the cluster as an admin, after applying cluster/rbac.yaml.
set -euo pipefail
SERVER=${1:?API server URL the agent host reaches, e.g. https://10.0.0.11:6443}
NS=${SBX_NAMESPACE:-sbx}
SECRET=agent-sandbox-token
for _ in $(seq 1 30); do
  TOKEN=$(kubectl -n "$NS" get secret "$SECRET" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d)
  [ -n "$TOKEN" ] && break
  sleep 1
done
[ -n "${TOKEN:-}" ] || { echo "no token in $NS/$SECRET; did you apply cluster/rbac.yaml?" >&2; exit 1; }
CA=$(kubectl -n "$NS" get secret "$SECRET" -o jsonpath='{.data.ca\.crt}')
cat <<KCFG
apiVersion: v1
kind: Config
clusters: [{name: sbx, cluster: {server: "$SERVER", certificate-authority-data: "$CA"}}]
users: [{name: agent-sandbox, user: {token: "$TOKEN"}}]
contexts: [{name: sbx, context: {cluster: sbx, user: agent-sandbox, namespace: $NS}}]
current-context: sbx
KCFG
