# Acknowledged code pull-back / 制品回传合同

## Scope / 范围

This 0.4.0rc1 SDK prerelease combines acknowledged receipt with the separately documented [lifecycle API](lifecycle.md). The receipt component itself changes neither routing nor claim acquisition/expiry/release design; it is not an artifact service. A `Sandbox` is used by one serialized command stream; concurrent processes sharing the same claim are unsupported. This candidate does not provide durable recovery after process restart, claim expiry/deletion, or a new `Sandbox` constructor (which adopts the host tree as authoritative).

本候选只修 SDK 回传完整性与确认时机，不接制品仓库、构建调度、CI/CD、平台 API，也不改变目录排除规则或现有路由。生命周期能力已一并整合，见 [生命周期合同](lifecycle.md)；`release_verified()` 不回传文件，回收成功不等于制品已收。使用同一个仍存活的实例重试，不能以重新构造实例代替重试。

## Compatibility and strict mode / 兼容与严格模式

Default behavior remains interactive/best-effort: an initial post-command pull failure logs a warning, and `execute()` returns the original `{output, returncode}`. The command exit code alone does **not** guarantee artifacts were received. Cursor acknowledgement is fixed in both modes.

For build callers opt in explicitly:

```python
from self_hosted_agent_sandbox import Sandbox

sb = Sandbox(cwd="/workspaces/project", config={
    "agent_sandbox_code_dirs": ["/workspaces/project"],
    "agent_sandbox_strict_pull": True,
})
try:
    result = sb.execute("make && mkdir -p out && cp dist/package.tar.gz out/package.tar.gz")
except RuntimeError:
    # The command may already have completed. Repair transport/local disk first.
    # Retry RECEIPT on this same instance; do not repeat non-idempotent commands.
    written, deleted = sb.pull()  # always propagates receipt failure
    # Command result is not returned by this failed execute; investigate before continuing.
else:
    if result["returncode"] != 0:
        raise RuntimeError("build failed")
# Check expected artifacts, lengths and SHA-256 here, then:
sb.close()
```

Equivalent environment setting: `AGENT_SANDBOX_STRICT_PULL=true` (default `false`). An explicit config `False` overrides an environment `true`.

- Strict post-command receipt failure raises `RuntimeError` chained to the original error. It does not fabricate or replace the command's exit code with a transport code.
- `pull()` receives without executing another command or pushing host files. It returns `(written, deleted)` and raises on error in either mode.
- After a failed receipt, the next `execute()` / `before_command()` must first complete receipt. If transport remains broken that next command is **blocked even in default mode**: otherwise the existing force-push would overwrite remote edits with stale host bytes. This is an intentional integrity safeguard, not a change to the initial best-effort result contract.
- A command timeout, stream writer error or adapter callback exception also leaves receipt pending. The original exception propagates unchanged; the next command receives remote edits before pushing. Recovery never reruns the interrupted command. `before_command()` reserves receipt after its push, including for external adapter-managed shells.
- Ordinary host file/directory conflicts are application errors, not protected symlink skips: receipt does not ACK them. File-to-directory (including empty directories) and directory-to-file changes require the caller to move/remove the conflicting host path, then retry `pull()` on the same instance. The SDK does not recursively delete conflicting host trees. Strict mode raises; default mode retains its warning/result behavior but blocks the next push until receipt succeeds.
- Strict `close()` propagates receipt failure and retains the claim reference instead of deleting it; retry `pull()` or `close()` before its idle TTL expires. `force_remove=True` does not bypass strict receipt. To deliberately abandon results, disable strict mode in the caller's policy / release separately. Default `close()` retains its legacy warning-and-cleanup behavior and can discard unreceived files; use strict mode for artifacts.
- Host-command pre-pull already propagated failure and still does. Adapter-managed shells must call the existing `before_command()` hook when used outside `execute()`.

## Receipt protocol / 接收确认

1. Capture `mark.new` **before** scanning. Keep the committed `mark` unchanged.
2. Generate the current NUL-delimited full file list and incremental tar. A failed `find` or `tar` is fatal; do not hide tar errors with `|| true`.
3. Export an outer tar containing exactly `all`, `changed.tar`, and a final `checksums` file (`sha256sum -b all changed.tar`). The final member and SHA-256 checks detect record-aligned truncation and bit corruption that Python's permissive tar reader alone can miss.
4. Validate the entire transport envelope and both payload checksums **before** any host write or deletion. Existing `.git`, path containment and symlink rules remain in force.
5. Apply eligible files with existing per-file atomic replacement and mirror eligible deletions. Local write/delete errors leave the mark unchanged. This is not a multi-file transaction: a disk error may leave a partially updated host tree; rerunning `pull()` converges without pushing that partial state back first.
6. Only after successful local application, issue a separate exec to atomically rename `mark.new` to `mark`. A missing/failed acknowledgement remains a retryable failure. If the rename succeeded but its response was lost, local data was already applied; retry remains safe.

Remote requirements remain Linux bash/find/GNU tar, with **`sha256sum` (coreutils) now also required**. Missing tools fail receipt rather than advancing the cursor. Only one pending `mark.new` is reused; failures do not create unbounded marker files. No background transfer/retry daemon or remote durable snapshot is introduced.

Watermark-based sync tracks mtime, not a content-addressed filesystem: preserved/backdated mtimes, concurrently running background writers, malicious sandbox processes and arbitrary host edits during receipt are outside this contract. Checksums detect transport damage; they are not signatures and do not make sandbox outputs trusted.

## Directory exclusions and explicit export / 目录排除与显式导出

Unchanged exclusions by directory name:

`node_modules`, `.venv`, `venv`, `target`, `build`, `dist`, `.gradle`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.next`, `.turbo`.

Strict receipt does **not** export these directories, override `.git` protections, follow symlinks, validate a caller's expected output set, or guarantee a successful build. Copy only named outputs into a non-excluded directory such as `out/` **inside a declared code dir**, e.g. `cp dist/package.tar.gz out/package.tar.gz`. Do not make all of `dist`/`node_modules` a code dir or disable exclusions to obtain artifacts. Caller checks must explicitly reject missing files and compare expected lengths/hashes. `output` is decoded text; even binary `stream` merges stderr, so neither is a generic clean artifact channel.

中文简述：显式导出是“把需要的文件复制到已声明代码目录里的 `out/`”，不是默认全目录同步。严格模式只保证允许回传的传输/本地应用失败不被当作已接收；预期制品集合、缺失/大小/hash 检查仍由调用方负责。受 `.git` 与宿主 symlink 保护而跳过的文件不因严格模式变得可写。

## Verification / 验证入口

```bash
python3 tests/route_table.py
python3 tests/core_unit.py
python3 tests/route_fuzz.py
python3 tests/artifact_pull.py
python3 tests/artifact_integrity.py
```

The artifact suite requires Docker and an **already-local** Node Debian image (default pinned image ID in the test; `SHAS_TEST_IMAGE` can select a compatible local Linux image). It never pulls an image or contacts Kubernetes. It creates only a uniquely named non-root, read-only-root, drop-all-capabilities, no-network Docker container with tmpfs workspaces; no source bind mounts or production resources. Container removal is read back. On macOS set `TMPDIR` to a writable path under `/Users` or `/Volumes` (not `/private/var`) so the same absolute task path can exist in the isolated container.

The separate integrity suite reuses that real-container transport for file/directory conflict recovery, interrupted stream/timeout/adapter receipt without command replay, partial multi-file application retry, and symlink-parent protection. It does not duplicate the original artifact cases in its reported test count.

REAL: Linux tar, binary generation, subprocess transport, hash comparisons, writes, deletion and acknowledgement. FAKE: claim reference and deletion stub. These tests are not gVisor/Kubernetes/production acceptance. The baseline regression (`--baseline`) must fail against public main before the fix because a 4096-byte rc=0 truncated response advances `mark`. To run that exact case against an unchanged baseline core, set `SHAS_TEST_CORE=/absolute/path/to/baseline/core.py` and pass `--baseline`; the log records the actually imported source.
