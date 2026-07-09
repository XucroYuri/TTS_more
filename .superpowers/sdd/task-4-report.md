# Task 4 Report: Anthropic Adapter And Parser Factory

## Status

- Completed

## Commit

- `f5e3732c292b6c9a0a18af1b528c80a89e596117` (`feat(parser): add anthropic parser adapter`)

## Files Changed

- `backend/app/parser.py`
- `backend/app/main.py`
- `backend/tests/test_parser.py`

## TDD Evidence

### RED

Added the two failing tests required by the brief:

- `test_build_parser_provider_uses_anthropic_adapter`
- `test_anthropic_provider_posts_messages_tool_contract`

Command:

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_parser.py::test_build_parser_provider_uses_anthropic_adapter tests/test_parser.py::test_anthropic_provider_posts_messages_tool_contract -q
```

Observed failure:

```text
=================================== ERRORS ====================================
____________________ ERROR collecting tests/test_parser.py ____________________
ImportError while importing test module 'F:\Code\Gitee\TTS_more\.worktrees\codex-llm-parser-agent-reliability\backend\tests\test_parser.py'.
...
E   ImportError: cannot import name 'AnthropicProvider' from 'app.parser'
...
ERROR: found no collectors for ...test_build_parser_provider_uses_anthropic_adapter
ERROR: found no collectors for ...test_anthropic_provider_posts_messages_tool_contract
```

This was the expected RED signal: the new Anthropic adapter/factory symbols did not exist yet.

### GREEN

Implemented:

- `ParserProbeResult`
- `OpenAICompatibleProvider.probe(api_key)`
- `anthropic_messages_url(base_url)`
- `_ANTHROPIC_TOOL`
- `AnthropicProvider`
- `build_parser_provider(config, verifier=None)`
- `_decode_anthropic_tool_input(payload)`
- `_build_parser()` routing through `build_parser_provider(...)`

Re-ran the targeted RED tests:

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_parser.py::test_build_parser_provider_uses_anthropic_adapter tests/test_parser.py::test_anthropic_provider_posts_messages_tool_contract -q
```

Output:

```text
..                                                                       [100%]
2 passed in 0.12s
```

## Tests Run

### 1. Targeted TDD RED

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_parser.py::test_build_parser_provider_uses_anthropic_adapter tests/test_parser.py::test_anthropic_provider_posts_messages_tool_contract -q
```

```text
FAILED during collection with ImportError: cannot import name 'AnthropicProvider' from 'app.parser'
```

### 2. Targeted TDD GREEN

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_parser.py::test_build_parser_provider_uses_anthropic_adapter tests/test_parser.py::test_anthropic_provider_posts_messages_tool_contract -q
```

```text
..                                                                       [100%]
2 passed in 0.12s
```

### 3. Full parser regression suite

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_parser.py -q
```

```text
..........................                                               [100%]
26 passed in 0.15s
```

### 4. Parser-related API verification after `main.py` change

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_api.py -k parser -q
```

```text
.........                                                                [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\fastapi\testclient.py:1
  ... StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
9 passed, 57 deselected, 1 warning in 0.89s
```

## Summary Of Changes

- Added a parser-provider factory that chooses the adapter from `ParserProviderConfig.adapter`.
- Added an Anthropic parser adapter that calls `/v1/messages` with the `emit_tts_parse` tool contract and reuses existing verification/repair behavior.
- Added shared probe result support for provider contract testing.
- Routed app parser construction through the new provider factory without changing `/api/parser/providers/test` endpoint routing logic.
- Preserved backward-compatible source evidence handling: missing `source_text` is still tolerated unless present and inconsistent.

## Concerns

- Parser-related API tests still emit an existing `StarletteDeprecationWarning` from FastAPI's `TestClient` dependency chain. This did not block the task and is unrelated to the new adapter/factory logic.

## Task 4 Fix Worker Follow-Up

### Status

- Completed reviewer follow-up for Anthropic repair/probe behavior.

### Files Changed

- `backend/app/parser.py`
- `backend/tests/test_parser.py`

### What Changed

- Changed `AnthropicProvider` repair flow to keep the existing system prompt and tool contract while sending explicit repair instructions, prior JSON, quality errors, and the original script as the repair user message.
- Changed `AnthropicProvider.probe()` to build `content_preview` from the decoded raw Anthropic tool input payload instead of the verified draft model dump.
- Added focused regression tests covering Anthropic repair request content and probe preview semantics.

### Tests Run

1. Focused RED/GREEN tests:

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_parser.py::test_anthropic_provider_repairs_with_explicit_repair_message tests/test_parser.py::test_anthropic_provider_probe_preview_keeps_raw_payload_evidence -q
```

Output:

```text
..                                                                       [100%]
2 passed in 0.15s
```

2. Required covering command:

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_parser.py::test_anthropic_provider_posts_messages_tool_contract tests/test_parser.py -q
```

Output:

```text
............................                                             [100%]
28 passed in 0.14s
```

### Commit

- `d75e39acb4ae19769b6aae949bc8ac35e7c8230d` (`fix(parser): align anthropic repair and probe preview`)
