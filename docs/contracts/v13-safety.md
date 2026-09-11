# V13 SAFETY Contract

Status: implemented, awaiting exact-candidate stamped acceptance. No
IF-0-SAFETY-1 is emitted until the phase receipt accepts all four ECs.

## Trust And Capabilities

Archive enforcement is unconditional before extraction and direct upload.
Only trusted operator mode configuration can choose warn/skip; unknown modes
fail closed. The verifier pins the repository, exact certificate workflow/ref
identity, custom predicate and optional source digest. No token-display probe,
local signing emulation or automatic hosted signing dispatch remains.

Prepared bytes and metadata can be signed and uploaded without recompression.
Failed publication preserves local/remote evidence. The approved seven-second
synthetic signing-only job and independently rejected negative controls are
recorded in `docs/validation/v13/SAFETY-signing.json`. The predicate explicitly
states local build and operator-supplied digest, not hosted build provenance.
The pre-merge test alone trusts its exact feature-branch signer; normal runtime
trust remains main. No archive or repository source was uploaded.

Filesystem wrappers cover builtins/io/pathlib/os opens and supported descriptor
forms. SQLite read roots, readonly file URIs, dbapi2 aliases and ATTACH denial
are tested. These are cooperative guards, not hostile-code containment. The
limits and actual capability fields are in `docs/security/sandbox.md`.

## Lifetime And Exposure

POSIX STDIO uses cancellable pipe adapters with the existing MCP SDK parser and
server. EOF and signals cancel one serve scope and await one cleanup task;
repeated signals do not abandon cleanup. Watchers, background tasks and active
workers drain before storage. Worker pipe writes, partial reads and envelope
sizes are bounded. Close can interrupt an in-flight call and reaps the owned
worker process group.

Python threads are not cancellable. Deadline failure retains writer ownership
until the mutating thread settles; it never advertises timeout completion while
that worker is still running. An uncooperative STDIO operation causes the whole
owning service to exit unsuccessfully after a 15-second shutdown deadline
(plus at most one second of child reaping). The durable pending-generation
fence remains. Restart/reindex is required; this is not successful indexing.
The installed Linux proof covers both real plugin children and an admitted
long-running reindex. Other OS lifecycle behavior is not certified by this
Linux receipt. No support-matrix expansion is implied.

Normal logs omit query/symbol/tool-argument content and raw exception payloads.
Metrics are disabled unless explicitly configured; enabled standalone metrics
bind loopback, own their socket and use the process counter registry. The HTTP
metrics route retains its separate authentication boundary.

HTTP admin remains local and ephemeral. Restart invalidates access/refresh
sessions. Only explicitly trusted proxy peers can supply forwarded identity;
the entrypoint disables implicit Uvicorn rewriting. No shared authentication,
durable identity or fleet promotion is claimed. See `docs/security/auth-boundary.md`.

## Acceptance Evidence

The phase suite, broad offline suite, local gate, installed wheel/container
workflows and rechecked live-signing receipt must all pass on the candidate.
Development checks so far are not an accepted phase receipt. DATA remains
responsible for vector/artifact staging, table retention and full query policy;
PILOT owns inference/browser/PMCP acceptance, PREP the fresh four-model code
panel and version bump, and SHIP publication.
