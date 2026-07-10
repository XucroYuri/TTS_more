# Audit Repair Batch C

## Outcome

- Status: fixed in the commit containing this report.
- Task start base: `4f62cd512190287ddfb5ef367778fddc96fac7ff`.
- Implementation patch SHA-256 (source and tests, excluding this report): `d5abdfd64d861dd956c3ead2b82dc98a54c6b33c9cff76abb6216171a044eecd`.
- Scope: service HTTP SSRF controls, Gradio download URL origin validation, worker artifact streaming limits, and post-download cleanup semantics.

## Security Invariants

1. A saved service endpoint and every `httpx` request in `services.py` pass the same network-scope-aware egress guard. `localhost` may add loopback and `lan` may add private ranges; link-local, cloud metadata, unspecified, reserved, multicast, unsafe schemes, and userinfo remain blocked.
2. Real HTTP transports fail closed when DNS returns no addresses. `httpx.MockTransport` skips DNS only, while still enforcing scheme, userinfo, and literal-address checks.
3. Absolute Gradio audio URLs must match the configured service scheme, host, and effective port. Cross-origin URLs, userinfo, HTTPS-to-HTTP downgrade, and non-HTTP schemes are rejected before transport invocation.
4. Worker artifacts are streamed into a temporary file while counting bytes and hashing. The default limit is 100 MiB, configurable by `default_params.artifact_download_max_bytes` or `TTS_MORE_MAX_ARTIFACT_DOWNLOAD_BYTES`; declared size/hash and actual size/hash must all agree before atomic replacement.
5. Remote DELETE runs only after verified atomic placement. Cleanup failure returns successful synthesis with `metadata.artifact_cleanup.status=deferred`, an artifact id, and a TTL cleanup warning.

## RED

- Environment bootstrap: initial `uv run pytest` could not find `pytest`; `uv sync --extra dev` installed the repository-declared dev dependencies.
- Command: `uv run pytest tests/test_net_guard.py tests/test_services.py -q`
- Result before implementation: `19 failed, 54 passed`. Failures proved missing userinfo/metadata/reserved-address checks, missing scope/same-origin helpers, transport invocation before validation, eager artifact materialization, and DELETE 500 propagation.
- Additional bypass test: `uv run pytest tests/test_net_guard.py::test_service_egress_can_fail_closed_when_dns_does_not_resolve -q`
- Result before fail-closed DNS implementation: `1 failed` because `allow_unresolved` was absent.

## GREEN

- `uv run pytest tests/test_net_guard.py tests/test_services.py -q`: `74 passed`.
- `uv run pytest tests/test_commercial_provider_clients.py -q`: `4 passed`.
- `uv run pytest tests/test_api.py -k 'service_settings_round_trip or service_settings_reload' -q`: `2 passed, 78 deselected`.
- `uv run pytest tests/test_worker_artifacts.py tests/test_workers.py -q`: `54 passed`.
- `uv run pytest --ignore=tests/test_api.py --ignore=tests/test_deploy_tool.py -q`: `363 passed, 2 skipped` on the earlier shared-tree snapshot.
- `PYTHONPATH=..:. uv run pytest -q`: `530 passed, 2 skipped` on the final shared-tree snapshot.
- `uv run python -m compileall -q app/services.py app/service_config.py app/net_guard.py`: passed.
- `git diff --check` for the batch files: passed.

## Shared-Tree Regression Note

The first full backend run reached `488 passed, 2 skipped, 1 failed`; a later run during concurrent edits reached `488 passed, 2 skipped, 5 failed`. The final standard run reached `529 passed, 2 skipped, 1 failed`; the sole failure was the out-of-scope `test_deploy_tool.py` importing the top-level `backend` package while pytest ran from the backend directory. Running that exact test with the repository parent on `PYTHONPATH` passed (`1 passed`). Batch C's focused, API settings, commercial provider, worker artifact, and remaining-suite checks passed, and no out-of-scope file was modified or staged by this batch.

## Self-Review And Concerns

- Audited all 24 `httpx.Client`/stream call sites in `services.py`; each URL argument now passes `_validated_url` immediately before request construction.
- Confirmed the worker artifact path no longer accesses `response.content`, stops at the first chunk crossing the configured limit, removes partial temp files, and preserves an existing output on size/hash failure.
- Confirmed existing localhost/LAN MockTransport synthesis, upload, queue, catalog, and commercial-provider tests remain green.
- Redirect following remains disabled by httpx defaults. DNS is validated before every real request, but connection-time IP pinning is not implemented; an attacker-controlled authoritative DNS server could still attempt a rebinding change between guard resolution and httpx resolution. Closing that narrower residual would require a transport-level resolver/IP-pinning design that preserves TLS SNI and is outside this patch's compatibility surface.
