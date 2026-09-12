# Plugin Worker Capability Guards

Plugin workers run in separate processes over versioned JSON-line IPC.
Capability guards are **cooperative defense in depth, not an OS-enforced
hostile-plugin sandbox**. Only trusted, reviewed plugin code is supported.

## Capability Model

`CapabilitySet` defines `fs_read` and `fs_write` path roots, `env_allow`
names, a boolean `network` flag, `sqlite` (`none` or `readonly`), CPU seconds
and memory MiB. SQLite defaults to none; memory defaults to 2048 MiB.
Worker startup imports and constructs the trusted plugin before enabling the
filesystem wrappers.

Supported Python filesystem forms include builtins/io/pathlib opens, string,
Path and byte paths, and resolvable descriptors, including `os.open(dir_fd=...)`.
Canonical paths must remain within their declared read/write roots. Only that
worker's own scratch directory is implicitly writable/readable; other files
under the system temporary directory are not exempt. Standard streams remain
available for IPC. Descriptor guards require resolvable OS descriptor paths
and deny unsupported descriptors.

Readonly SQLite connections handle string, Path, byte and file-URI inputs,
enforce declared read roots and override writable URI modes. Both
`sqlite3.connect` and `sqlite3.dbapi2.connect` are guarded. ATTACH is denied,
so a readonly connection cannot attach a writable database. Unknown SQLite
capabilities and non-boolean network values in capability JSON are rejected.

## Limits

These Python wrappers do not contain deliberately malicious plugins.
Native extensions, existing/pre-imported handles, alternate filesystem APIs,
subprocesses, monkey-patching and filesystem races can bypass cooperative
wrappers. The network patch is also cooperative, not a network namespace or
firewall. Environment filtering is not protection against a malicious child
or native code. Do not rely on these guards to execute untrusted code or to
protect secrets from an adversarial plugin. OS/container isolation is a
separate operator responsibility.

Resource limits are platform-dependent. Worker IPC enforces a 16 MiB envelope
limit and request deadlines; lifecycle acceptance is recorded in the SAFETY
receipt. A Python timeout alone does not prove a worker or indexing thread
has stopped.

## Default-On Operation

Workers remain default-on. `MCP_PLUGIN_SANDBOX_DISABLE=1` opts into in-process
execution and removes this defense in depth. No opt-out is needed for supported
plugins. See [the support matrix](../SUPPORT_MATRIX.md) for actual language
support; registry membership does not establish sandbox availability.

Availability states are `enabled`, `unsupported`, `missing_extra`,
`disabled` and `load_error`. Registry-only languages without a supported
worker module remain unsupported in default mode. Known dependency failures
include language, required extras and remediation metadata. For example,
Java analysis requires `uv sync --locked --extra java`; C# accepts
`c_sharp` and `csharp`.

See the [P18 upgrade notes](../operations/p18-upgrade.md) for historical
default-on migration steps; the current capability limits above are authoritative.
