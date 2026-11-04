#!/usr/bin/env python3
"""Render a SandboxTemplate + SandboxWarmPool for one tenant (team / group).

The sandbox should run the SAME image as the agent host so paths and toolchains match.
Package caches live on each node under <cache-root>/<tenant> and are mounted at the path
the host image's cache env vars already point at, so nothing needs reconfiguring.

usage: render-pool.py --tenant NAME --image IMAGE [--replicas 1] [--uid 10001]
                      [--cache-root /var/lib/sbx-cache] [--cache-mount /var/cache/pkg]
                      [--host-alias IP=host[,host]] ... [--env K=V] ... [--memory 3584Mi]
"""
import argparse
import json

ap = argparse.ArgumentParser()
ap.add_argument("--tenant", required=True)
ap.add_argument("--image", required=True)
ap.add_argument("--replicas", type=int, default=1)
ap.add_argument("--namespace", default="sbx")
ap.add_argument("--uid", type=int, default=10001)
ap.add_argument("--cache-root", default="/var/lib/sbx-cache")
ap.add_argument("--cache-mount", default="/var/cache/pkg")
ap.add_argument("--workdir", default="/workspaces")
ap.add_argument("--memory", default="3584Mi", help="keep below the node's pod budget so OOM kills the sandbox, not the node")
ap.add_argument("--cpu", default="2")
ap.add_argument("--host-alias", action="append", default=[], metavar="IP=host[,host]")
ap.add_argument("--env", action="append", default=[], metavar="K=V")
a = ap.parse_args()

name = f"sbx-{a.tenant}"
uid = a.uid
env = [{"name": "JAVA_TOOL_OPTIONS", "value": "-XX:MaxRAMPercentage=50.0 -XX:MaxMetaspaceSize=384m "
        "-XX:ReservedCodeCacheSize=128m -XX:MaxDirectMemorySize=256m -Xss512k -XX:+ExitOnOutOfMemoryError"}]
env += [{"name": k, "value": v} for k, v in (e.split("=", 1) for e in a.env)]
aliases = [{"ip": ip, "hostnames": hosts.split(",")} for ip, hosts in (h.split("=", 1) for h in a.host_alias)]
pod = {
    "runtimeClassName": "gvisor",
    "automountServiceAccountToken": False,
    "enableServiceLinks": False,
    "securityContext": {"runAsUser": uid, "runAsGroup": uid, "fsGroup": uid},
    "initContainers": [{
        "name": "cache-perm", "image": a.image, "imagePullPolicy": "IfNotPresent",
        "securityContext": {"runAsUser": 0},
        "command": ["sh", "-c", f"chown {uid}:{uid} /c"],
        "volumeMounts": [{"name": "cache", "mountPath": "/c"}],
        "resources": {"limits": {"cpu": "500m", "memory": "256Mi"}},
    }],
    "containers": [{
        "name": "main", "image": a.image, "imagePullPolicy": "IfNotPresent",
        "command": ["sleep", "infinity"], "workingDir": a.workdir, "env": env,
        "resources": {"requests": {"cpu": "250m", "memory": "512Mi"},
                      "limits": {"cpu": a.cpu, "memory": a.memory}},
        "volumeMounts": [{"name": "ws", "mountPath": a.workdir},
                         {"name": "home", "mountPath": "/home/agent"},
                         {"name": "cache", "mountPath": a.cache_mount}],
    }],
    "volumes": [{"name": "ws", "emptyDir": {"sizeLimit": "10Gi"}},
                {"name": "home", "emptyDir": {"sizeLimit": "1Gi"}},
                {"name": "cache", "hostPath": {"path": f"{a.cache_root}/{a.tenant}", "type": "DirectoryOrCreate"}}],
}
if aliases:
    pod["hostAliases"] = aliases
labels = {"sandbox-tenant": a.tenant}
docs = [
    {"apiVersion": "extensions.agents.x-k8s.io/v1beta1", "kind": "SandboxTemplate",
     "metadata": {"name": name, "namespace": a.namespace, "labels": labels},
     "spec": {"networkPolicyManagement": "Unmanaged",
              "podTemplate": {"metadata": {"labels": labels}, "spec": pod}}},
    {"apiVersion": "extensions.agents.x-k8s.io/v1beta1", "kind": "SandboxWarmPool",
     "metadata": {"name": name, "namespace": a.namespace, "labels": labels},
     "spec": {"replicas": a.replicas, "sandboxTemplateRef": {"name": name}}},
]
print("\n---\n".join(json.dumps(d, indent=2) for d in docs))  # JSON is valid YAML for kubectl
