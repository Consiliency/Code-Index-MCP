# Dependency advisory triage: `uv.lock` (2026-10-31)

Tracking: Consiliency/Code-Index-MCP#106. This is a scoped triage. It is not a blanket
upgrade.

## Method

- Source: `uv.lock` at `85da6e1`, exported with
  `uv export --locked --all-extras --no-hashes --no-emit-project`: 306 pinned packages.
- Scanner: `uvx pip-audit` 2.10.1 (`-r <export> --no-deps --disable-pip`), using the
  PyPI/OSV advisory data available on 2026-10-31.
- Raw result: 397 rows in 36 packages. After deduplicating by GHSA alias per package,
  280 distinct advisories remain. The audit snapshot counted 844 rows and 419 IDs with a
  different scanner. That count includes alias and per-version duplicates.
- Classification: each package is sorted by the dependency sets that contain it (base
  install, `semantic`, `production`, `rerank`, `java`, `dev`) and by its path from
  `uv tree --invert`. The image built by `Dockerfile.production` installs base plus
  `production`.
- Reachability: `python -X importtime` over `mcp_server.cli.stdio_runner` and
  `mcp_server.gateway` shows which vulnerable modules load at startup:
  `anyio`, `certifi`, `cryptography`, `idna`, `jwt`, `pydantic_settings`,
  `python_multipart`, `starlette` and `urllib3`. Usage was then checked against each
  advisory's prerequisites. No exploit payloads were run.

## Upgraded: applicable base-install matches

| Package | From → To | Advisory | Why it applies |
|---|---|---|---|
| starlette | 1.0.0 → 1.0.1 | GHSA-86qp-5c8j-p5mr | `AuthenticationMiddleware` decides its auth exemptions from `request.url.path` (`mcp_server/security/security_middleware.py`). Starlette builds that URL from an unvalidated `Host` header, which is the advisory's authentication-bypass prerequisite. 1.0.1 is the smallest fixed release and is a patch release; FastAPI requires `starlette>=0.46.0`. |
| certifi | 2026.2.25 → 2026.10.18 | GHSA-6q48-6c7w-9gw2 | Every HTTPS client in the runtime (httpx and requests) trusts the certifi bundle, which still contains the distrusted `emSign ECC Root CA - G3`. The update changes only the bundled data, and 2026.10.18 is the only fixed release. |

The lockfile diff touches only these two packages. The `revision` header is still 3.
`uv lock --locked` and `uv sync --locked` pass. A re-scan shows certifi with 0
advisories. starlette still matches 4 advisories, all listed as not applicable below.

## Base-install matches deferred or not applicable

| Package (version) | Path | Advisories | Decision and reason |
|---|---|---|---|
| mcp 1.27.0 | direct | GHSA-hvrp-rf83-w775 (fixed in 1.27.2) | **Deferred.** `enable_tasks()` is used, but the impact requires several clients on one server. The server runs only the STDIO transport, so each process has one session. The fix also stops listing tasks written directly through a `TaskStore`, and `MCPTaskRegistry` is a custom store. Upgrade in a separate change that tests task listing. |
| mcp 1.27.0 | direct | GHSA-jpw9-pfvf-9f58, -vj7q-gjh5-988w, -84m7-p3x7-pcfv, -5h93-6whr-6q8j, -w4fh-qvv9-3v23, -rwrf-2pqf-9j8j | Not applicable. The code uses no SSE, Streamable HTTP or WebSocket server transport, no SDK client and no SDK bearer auth. |
| starlette 1.0.1 | fastapi | GHSA-jp82-jpqv-5vv3, -82w8-qh3p-5jfq, -x746-7m8f-x49c, -wqp7-x3pw-xc5r | Not applicable. The code does not read `request.url.hostname` or `request.url.netloc`, has no form endpoints (login takes JSON), uses no `HTTPEndpoint` and serves no `StaticFiles`. |
| pyjwt 2.12.1 | mcp[crypto] and auth layer | 16 GHSA (for example -xgmm-8j9v-c9wx, -jq35-7prp-9v3f, -gvp8-978c-rx2q, -p4g4-x82p-q773) | Not applicable. `jwt.decode` uses a single configured algorithm (`HS256` by default) and a server-side secret, with no `PyJWK` or `PyJWKClient`, no detached or `b64:false` payloads, and a fresh `options` dict on every call. Fixing all 16 would need 2.15.0. |
| python-multipart 0.0.26 | mcp, starlette | 4 GHSA | Not applicable. No form or multipart parsing is reachable: no `Form` or `File` parameters, no `request.form()`, and the SDK HTTP auth routes are unused. |
| cryptography 46.0.7 | mcp[crypto], pyjwt | GHSA-537c-gmf6-5ccf (bundled OpenSSL), -m2h6-j472-rp4c, -jwv3-5hgf-82ww, -g6cj-pr64-35w5 | **Deferred, runtime unknown.** The code does no X.509 path validation and no PKCS7 decryption. The bundled OpenSSL in the wheel is used only through cryptography itself, not through Python's `ssl` module. The fix requires 48.0.1, two major versions up. Check the OpenSSL 2026-06-09 advisory against the HMAC-only JWT path before scheduling. |
| aiohttp 3.13.5, multidict 6.7.1 | voyageai | 22 + 1 GHSA | Not applicable. Only the synchronous `voyageai.Client` is used, which goes through requests. No aiohttp client or server runs. |
| pillow 12.2.0 | voyageai | 52 GHSA | Not applicable. No images are opened. Only text is embedded, and multimodal is unused. |
| gitpython 3.1.46 | treesitter-chunker | 34 GHSA (including GHSA-284h-m62q-gf8w) | Not applicable. Only `chunker/repo/processor.py` imports `git`, and Code-Index-MCP never imports that module (it does not load at startup). Repository git access goes through the `git` CLI. Re-check if chunker repository processing is adopted. |
| langchain-core 1.3.0, langsmith 0.7.32 | voyageai (langchain-text-splitters) | 1 + 2 GHSA | Not applicable. The code does no LangChain deserialization (`load`/`loads`), does not pull LangSmith prompts and uses no `TracingMiddleware`. |
| urllib3 2.6.3 | requests, qdrant-client | 5 GHSA | Not applicable to configured endpoints. requests handles redirects itself. The streaming decompression issues need a malicious server, and the endpoints are operator-configured (Voyage API, local inference). No HTTPS proxy TLS is configured. |
| anyio 4.13.0 | httpx and others | GHSA-82r6-8w77-94w6, -5p39-cfhj-2xmp | Not applicable. Endpoint hostnames are ASCII, so the IDN TLS issue cannot trigger, and `anyio.to_process` is unused. |
| idna 3.11 | httpx and others | GHSA-65pc-fj4g-8rjx | Not applicable. Hostnames come from operator configuration, not untrusted input. |
| h2 4.3.0, hpack 4.1.0 | httpx[http2] via qdrant-client | 1 + 1 GHSA | Not applicable. qdrant-client defaults to `http2=False` and Code-Index-MCP never enables it. The h2 issue is server-side. |
| filelock 3.28.0, fsspec 2026.3.0 | voyageai (HF hub) | 9 + 1 GHSA | Not applicable. Code-Index-MCP does not use `ReadWriteLock`, soft locks or Windows lock paths, and the HF cache is per user. `ReferenceFileSystem` is unused. |
| pydantic-settings 2.13.1 | mcp | GHSA-4xgf-cpjx-pc3j | Not applicable. `secrets_nested_subdir` is unused. |

## Extras and development

| Package (version) | Set | Distinct GHSA | Decision and reason |
|---|---|---|---|
| mako 1.3.11 | `production` (via alembic) | 1 | Not applicable. The issue is Windows-only backslash traversal, and alembic is not imported by `mcp_server`. |
| sentence-transformers 5.4.1 | `semantic` | 1 | Deferred. Exploitation needs an attacker-controlled model. Reranker and embedding models are configured by the operator. Not in the production image. |
| transformers 5.5.4, torch 2.11.0 | `semantic` | 2 + 1 | Deferred. The issues need `save_pretrained` or `load_custom_generate` with untrusted keys, or `torch.jit.script` on local input. Neither is used with untrusted input. Not in the production image. |
| setuptools 81.0.0 | `semantic`, `dev` | 2 | Not applicable at runtime. The issues are MANIFEST.in exclude handling and Windows legacy-wheel install. The build backend comes from `[build-system]` under build isolation, not from this lock entry. |
| authlib, black, bleach, jupyter-server, jupyterlab, mistune, nltk, notebook, pylint, soupsieve, tornado | `dev` only | 10, 1, 3, 8, 12, 17, 31, 3, 1, 4, 13 | Deferred: development tooling only, never installed by `uv sync --locked` without `--extra dev` and absent from the production image. Upgrade separately as tooling maintenance. |

## Residual risk

- Applicability was judged from the source at this commit. New uses of SDK HTTP
  transports, forms, `PyJWK`, chunker repository processing or async Voyage clients
  must reopen the matching rows.
- The installed and deployed versions on operator hosts were not inventoried. A host
  that installed before this change keeps starlette 1.0.0 and the old certifi until it
  runs `uv sync --locked` again or the image is rebuilt.
