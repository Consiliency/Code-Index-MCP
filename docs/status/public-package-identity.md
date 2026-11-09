# Public Package Identity

Check date: 2026-07-06 UTC; 1.5.0 publication recheck 2026-11-08; 1.5.1 recheck 2026-11-09; 1.6.0 preparation check 2026-11-09

## Scope

This note freezes the repo-owned public identity contract for PUBNAME.
It is metadata-only evidence for the prepared `1.3.0` surface. It does not
claim a new package or container publication.

## 1.6.0 Preparation Check (2026-11-09)

`https://pypi.org/pypi/index-it-mcp/json` reports `1.5.1` as the latest version.
`1.3.0`, `1.3.1`, `1.4.0`, `1.4.1` and `1.5.0` are yanked with the reason
"Unbounded mcp requirement: plain installs resolve mcp 2.x and the STDIO server
fails". `1.2.0` and its release candidates still declare an unbounded `mcp`
requirement and are not yanked; the abandoned `2.14.x` and `2.15.0a1` line is
yanked. The prepared `1.6.0` declares `mcp<2.4,>=2.2`. Public install docs
recommend `pip install index-it-mcp==1.5.1` until `1.6.0` is published and
accepted, and tell `1.6.0` users to drop any `mcp<2` pin.

## 1.5.1 Publication Recheck (2026-11-09)

`https://pypi.org/pypi/index-it-mcp/json` now reports `1.5.1` as the latest
version, published from Release Automation run 42550570039 at tag `v1.5.1`
(`13470d0`). The wheel and sdist digests match the GitHub release assets, and
the metadata declares `mcp<2,>=1.27`. A plain `pip install index-it-mcp==1.5.1`
resolves `mcp` 1.30.0; see "1.5.1 Delivered Acceptance" in
`docs/operations/v13-release.md`. Public install docs recommend
`pip install index-it-mcp==1.5.1`, and they keep the warning not to install
`1.5.0` or `1.4.1` without constraining `mcp` below 2. The project URLs still
point to `https://github.com/ViperJuice/Code-Index-MCP`.

## 1.5.0 Publication Recheck (2026-11-08)

`https://pypi.org/pypi/index-it-mcp/json` now reports `1.5.0` as the latest
version, published from Release Automation run 42539192667 at tag `v1.5.0`
(`81980d0`). The wheel and sdist digests match the GitHub release assets; see
the delivered acceptance record in `docs/operations/v13-release.md`. Version
parity for `index-it-mcp` is now proven for `1.5.0`. The project URLs still point
to `https://github.com/ViperJuice/Code-Index-MCP`. `code-index-mcp` is still an
external package (`2.17.1`, `johnhuang316/code-index-mcp`).

The published `1.5.0` metadata declares `mcp>=1.0.0` with no upper bound, so an
unconstrained `pip install index-it-mcp==1.5.0` resolves `mcp` 2.x, and STDIO
fails to start. The `1.5.1` corrective release caps `mcp>=1.27,<2`,
so public install docs do not add a separate pin; they say not to install
`1.5.0` or `1.4.1` without constraining `mcp` below 2.
The sections below are the original 2026-07-06 PUBNAME evidence.

## Metadata Probes

PyPI JSON probe used on 2026-07-06 UTC:

```bash
python - <<'PY'
import json, urllib.request
for name in ("index-it-mcp", "code-index-mcp"):
    url = f"https://pypi.org/pypi/{name}/json"
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = json.load(response)
    info = payload["info"]
    print(json.dumps({
        "name": name,
        "url": url,
        "normalized_name": info.get("name"),
        "version": info.get("version"),
        "project_urls": info.get("project_urls"),
    }, sort_keys=True))
PY
```

Observed source URLs:

- `https://pypi.org/pypi/index-it-mcp/json`
- `https://pypi.org/project/index-it-mcp/`
- `https://pypi.org/pypi/code-index-mcp/json`
- `https://pypi.org/project/code-index-mcp/`

Observed live facts:

| Package | Normalized name | Latest visible version on 2026-07-06 | Project/source URLs | Decision input |
| --- | --- | --- | --- | --- |
| `index-it-mcp` | `index-it-mcp` | `2.14.9` | `Homepage`, `Repository`, and `Documentation` point to `https://github.com/ViperJuice/Code-Index-MCP` | The canonical distribution name matches this repo, but live PyPI parity for the repo-owned `1.3.0` surface is not proven. |
| `code-index-mcp` | `code-index-mcp` | `2.17.0` | `Homepage` points to `https://github.com/johnhuang316/code-index-mcp` | Treat as an external or colliding package name, not this repo's install target. |

## Proof Status

- Owner/source URL proof for `index-it-mcp`: partial. The live project URLs
  point at this repository and the author metadata is `Jenner Torrence`, but
  the latest visible live version is `2.14.9` rather than this repo's prepared
  `1.3.0` surface.
- Version parity proof for `index-it-mcp`: not proven. PUBNAME therefore keeps
  public install docs on source and local-wheel proof.
- Owner/source proof for `code-index-mcp`: negative for this repo. The live
  homepage points to a different repository, so PUBNAME treats the name as
  external and avoids it in user-facing install instructions.

## Local Wheel Smoke

Repo-owned smoke command:

```bash
uv run --extra dev python scripts/release_smoke.py --wheel --stdio
```

Expected canonical entrypoints from the locally built wheel:

- `mcp-index`
- `index-it-mcp`

Removed legacy alias:

- `code-index-mcp`

## Identity Inventory

| Surface | Current surface | Decision | Rationale |
| --- | --- | --- | --- |
| Python distribution name | `index-it-mcp` | keep | Canonical install identity for this repo. |
| Canonical CLI entrypoints | `mcp-index`, `index-it-mcp` | keep | These are the wheel entrypoints PUBNAME verifies locally. |
| Legacy console-script alias | `code-index-mcp` | drop | It collides with an external package name and is not needed as the canonical install surface. |
| Click program name | `code-index-mcp` in `mcp_server/cli/__init__.py` | defer | Runtime label is outside PUBNAME ownership and needs a later runtime-facing phase if it should change. |
| MCP client server IDs / labels | `code-index`, `code-index-mcp`, `code-index-native`, similar example IDs | keep | These are client-local labels, not Python package names. |
| Container image name | `ghcr.io/consiliency/code-index-mcp` | keep | Container naming remains a separate public surface from the Python distribution name. |
| Profile/config filename | `code-index-mcp.profiles.yaml` | keep | Repo-shipped configuration filename, not an install target. |
| `.mcp.json` example labels | `code-index-mcp` and related server keys | keep | Example keys identify local client config entries only. |
| Repository name | `Code-Index-MCP` | keep | Repo rename is outside PUBNAME scope. |
| npm helper kit | `mcp-index-kit` | keep | Separate helper-kit identity; does not conflict with the Python distribution decision. |

## Public Docs Posture

Until a later release-evidence phase re-proves live parity for the prepared
`1.3.0` surface, public docs should:

- use `index-it-mcp` as the canonical Python distribution name;
- avoid `pip install code-index-mcp` or any other `code-index-mcp` package
  install guidance;
- prefer `uv sync --locked` or a locally built wheel for repo-owned proof; and
- classify `code-index-mcp` mentions as container/image naming, profile/config
  filenames, MCP client labels, or an explicit collision warning.
