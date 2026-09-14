# Read-only Host Toolchains

LLVM, PPL, backend sources and FlagGems are kept in versioned directories owned
by the CI account. Environment probes and task containers mount these directories
read-only at fixed paths under `/opt/local-ci/runtime/deps`. All profiles use the
same immutable runtime image; frozen frontend sources and writable build output
remain in task storage. Management recovery mounts only task/session
volumes so missing toolchains do not prevent evidence export or cleanup.

Configure a dedicated absolute `dependency_root` outside control, state and
credential directories. Each mount source must be a directory strictly beneath
that root. Directories/files must belong to the CI account, be readable by mapped
container UIDs, and have no group/other write permission. Links must be relative,
resolve inside the same version directory, and have existing targets.

Example profile fragment (replace paths, commits and digests with actual values):

```json
{
  "llvm": {"mode": "mount", "commit": "<exact LLVM commit>"},
  "mounts": [
    {
      "source": "/home/jiwang_ci/local_ci/workspace/dependencies/llvm-<commit>",
      "target": "/opt/local-ci/runtime/deps/llvm-<commit>",
      "read_only": true,
      "sha256": "<tree_digest of source>"
    },
    {
      "source": "/home/jiwang_ci/local_ci/workspace/dependencies/ppl-<version>",
      "target": "/opt/local-ci/runtime/deps/ppl",
      "read_only": true,
      "sha256": "<tree_digest of source>"
    }
  ]
}
```

Calculate each checksum using
`prepare.artifacts.tree_digest(Path(source))` from the trusted control code,
after finalizing permissions and internal links. This is a directory-content
digest, not an archive checksum. Environment preparation and deployment preflight
verify the full contents; task startup verifies the pinned image and read-only
mount identity. Record vendor package provenance
separately. Container read-only mounting does not prevent the host owner from
changing files; never edit a version directory used by a validated release.
Create a new version directory, update its mount configuration and repeat runtime preflight
instead. Old version directories must remain available to their active tasks.

Profile `archives`, `repositories` and `prepare_commands` are no longer used.
Install common Python/system packages when building the shared image; their ABI
must match all configured toolchains. Environment probes check imports and mounted
LLVM paths without building or installing frontend/backend Wheels. Formal tasks
build the selected source in their own writable directories and venvs.

Only fixed read-only directory binds are accepted; recursive nested mounts are
disabled. This is not a general host-mount mechanism and does not expose host
credentials, Docker sockets, task roots or the whole home directory.
