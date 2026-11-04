#!/bin/bash
# Install gVisor (runsc) on a k3s node and register the "runsc" containerd runtime.
#   ./gvisor-node.sh [path/to/gvisor.tar.bz2]   (tarball: runsc + containerd-shim-runsc-v1)
# Without a tarball the binaries are downloaded from storage.googleapis.com.
set -euo pipefail
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
if [ -n "${1:-}" ]; then
  tar xjf "$1" -C "$work"
else
  url=https://storage.googleapis.com/gvisor/releases/release/latest/$(uname -m)
  for f in runsc runsc.sha512 containerd-shim-runsc-v1 containerd-shim-runsc-v1.sha512; do
    curl -fsSL -o "$work/$f" "$url/$f"
  done
  (cd "$work" && sha512sum -c runsc.sha512 containerd-shim-runsc-v1.sha512)
fi
install -m 0755 "$(find "$work" -name runsc -type f | head -1)" "$(find "$work" -name containerd-shim-runsc-v1 -type f | head -1)" /usr/local/bin/
runsc --version | head -1
d=/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d
mkdir -p "$d"
cat > "$d/runsc.toml" <<'TOML'
version = 3
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
TOML
svc=k3s; systemctl is-active -q k3s-agent && svc=k3s-agent
systemctl restart "$svc"
sleep 10
k3s crictl info | python3 -c 'import json,sys; r=json.load(sys.stdin)["config"]["containerd"]["runtimes"]; print("runtimes:", sorted(r)); sys.exit("runsc" not in r)'
