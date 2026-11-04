#!/bin/bash
# Install a k3s sandbox node whose pods are hard-capped at SBX_POD_BUDGET_MI (default 4096).
# The rest of the machine is kubelet system-reserved, so existing services keep their memory.
#
#   SBX_NODE_IP=10.0.0.11 ./node-install.sh server
#   SBX_NODE_IP=10.0.0.12 ./node-install.sh agent https://10.0.0.11:6443 /path/to/node-token
#
# Online hosts fetch the official installer from get.k3s.io. Air-gapped hosts: put k3s,
# install-k3s.sh (the get.k3s.io script) and k3s-airgap-images-amd64.tar.zst in SBX_DL first.
set -euo pipefail
ROLE=${1:?server|agent}
K3S_VERSION=${K3S_VERSION:-v1.37.1+k3s1}
SBX_DL=${SBX_DL:-/root/sbx-dl}
IP=${SBX_NODE_IP:?set SBX_NODE_IP to the address other nodes reach this one on}
BUDGET_MI=${SBX_POD_BUDGET_MI:-4096}
CAP_MI=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 ))
RES_MI=$(( CAP_MI - BUDGET_MI ))
[ "$RES_MI" -gt 1024 ] || { echo "machine too small for a ${BUDGET_MI}Mi pod budget" >&2; exit 1; }
echo "ip=$IP total=${CAP_MI}Mi pods=${BUDGET_MI}Mi reserved=${RES_MI}Mi"

mkdir -p /var/lib/rancher/k3s/agent/images /etc/rancher/k3s "$SBX_DL"
# The official installer script; air-gapped hosts copy it into SBX_DL beforehand.
[ -s "$SBX_DL/install-k3s.sh" ] || curl -fsSL -o "$SBX_DL/install-k3s.sh" https://get.k3s.io
if [ -s "$SBX_DL/k3s-airgap-images-amd64.tar.zst" ]; then
  cp "$SBX_DL/k3s-airgap-images-amd64.tar.zst" /var/lib/rancher/k3s/agent/images/
  install -m 0755 "$SBX_DL/k3s" /usr/local/bin/k3s
  export INSTALL_K3S_SKIP_DOWNLOAD=true
fi
cat > /etc/rancher/k3s/config.yaml <<CFG
node-ip: $IP
kubelet-arg:
  - "system-reserved=memory=${RES_MI}Mi"
  - "eviction-hard=memory.available<200Mi,nodefs.available<5%,imagefs.available<5%"
  - "max-pods=60"
node-label:
  - "agent-sandbox/pool=default"
CFG
if [ "$ROLE" = server ]; then
  cat >> /etc/rancher/k3s/config.yaml <<CFG
write-kubeconfig-mode: "0600"
disable: [traefik, servicelb, metrics-server]
tls-san: [$IP]
CFG
  INSTALL_K3S_VERSION=$K3S_VERSION sh "$SBX_DL/install-k3s.sh" server
else
  INSTALL_K3S_VERSION=$K3S_VERSION K3S_URL="${2:?server url}" K3S_TOKEN="$(cat "${3:?token file}")" \
    sh "$SBX_DL/install-k3s.sh" agent
fi

# gVisor sandboxes are not OOM-killable by the kernel (oom_score_adj -999) and would swap
# instead of dying; with swap off for pods the cgroup limit kills them promptly.
mkdir -p /etc/systemd/system/kubepods.slice.d
printf '[Slice]\nMemorySwapMax=0\n' > /etc/systemd/system/kubepods.slice.d/50-sbx-noswap.conf
systemctl daemon-reload
if [ -f /sys/fs/cgroup/kubepods.slice/memory.swap.max ]; then echo 0 > /sys/fs/cgroup/kubepods.slice/memory.swap.max; fi
# Cache root used by render-pool.py (uid 10001 by default); the marker lets cache-gc prune it.
mkdir -p /var/lib/sbx-cache && touch /var/lib/sbx-cache/.sbx-cache-root
