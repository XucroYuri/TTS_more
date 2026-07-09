# LLM Parser Agent Reliability Design

## Goal

Upgrade script parsing from a lightly repaired OpenAI-compatible JSON call into a provider-adapter parser agent with strong semantic extraction, strict source-fidelity validation, and high-performance model defaults for complex script, prose, article, and mixed-format inputs.

The highest-priority invariant is: extracted dialogue text must be exactly faithful to the source. A line whose spoken text cannot be traced back to an unchanged source excerpt must not be accepted.

## Current Context

The backend parser currently lives mainly in `backend/app/parser.py`. It uses OpenAI-compatible Chat Completions, a screenplay-oriented prompt, JSON response parsing, one repair attempt, and a verifier that checks character references, empty lines, duplicate ids, non-dialogue roles, rough expected-line counts, and ordered source traceability.

Provider presets live in `backend/app/parser_config.py`. The current default list is OpenAI-compatible only and includes providers the new requirement excludes: Baidu Qianfan and Mistral. It does not include Anthropic, Gemini, OpenRouter, or Aihubmix.

The frontend parser provider editor uses `frontend/src/types.ts`, `frontend/src/lib/parserConfig.ts`, and the parser provider section in `frontend/src/App.tsx`. It assumes one provider shape, but it can be extended without changing the user's basic workflow.

## Scope

- Keep the existing ability to configure one OpenAI-compatible model manually.
- Add an adapter field so providers can use native or OpenAI-compatible request shapes.
- Add Anthropic native adapter support.
- Add Gemini, OpenRouter, and Aihubmix presets through OpenAI-compatible endpoints.
- Remove Baidu Qianfan and Mistral from default presets.
- Strengthen parser prompts around complex formats, semantic extraction, and exact source fidelity.
- Strengthen verification so line text must match source text exactly after only safe wrapping/markup normalization.
- Require LLM output to carry source evidence for every accepted line.
- Preserve fail-closed behavior: unavailable providers produce availability errors; poor extraction quality produces quality errors.

## Non-Goals

- Do not add a deterministic rule parser as the primary parser.
- Do not silently synthesize missing dialogue from heuristics.
- Do not change TTS generation routing, voice binding, queue behavior, or project storage schema beyond parser output fields that are already accepted by the current `ScriptLine` model.
- Do not require multiple providers. A single configured model remains valid.

## Provider Adapter Design

### Configuration Model

Extend parser provider records with:

```python
adapter: Literal["openai-compatible", "anthropic"] = "openai-compatible"
```

Old saved provider JSON without `adapter` loads as `openai-compatible`.

The public API includes `adapter`, and the frontend editor exposes it as a compact select. The default add-provider template remains OpenAI-compatible, because that is the broadest manual integration path.

### Adapter Registry

Parser construction chooses an adapter from `ParserProviderConfig.adapter`:

- `openai-compatible`: existing Chat Completions flow, upgraded prompt/schema, used for OpenAI, Gemini compatibility endpoint, OpenRouter, Aihubmix, and other compatible providers.
- `anthropic`: Claude Messages API with a required structured tool call. The adapter extracts the tool input as the parser payload and passes it through the same normalizer and verifier.

The rest of the parser operates on a shared `ParsedScriptDraft` and shared quality verifier, so adapter choice does not fork downstream behavior.

## Agent Prompt Contract

The parser prompt becomes format-agnostic instead of screenplay-only. It should explicitly cover:

- Standard screenplay blocks: `角色（情绪）: 台词`, uppercase speaker blocks, Markdown speaker headings.
- Prose and novel dialogue: quoted speech with speaker attribution before or after the quote.
- Interview and news formats: speaker labels, attribution clauses, narrated quotes, and named quotes.
- Mixed Chinese/English punctuation and nested parentheticals.
- Narrator, voice-over, host, announcer, and quoted speaker lines that should be synthesized by TTS.

The prompt requires internal reasoning but forbids revealing chain-of-thought. The visible output is only structured data.

Each output line must include source evidence in the raw provider payload:

```json
{
  "speaker": "角色显示名",
  "text": "台词原文",
  "note": "情绪或括注，不带括号",
  "language": "zh",
  "source_excerpt": "原文中包含该台词的最小片段",
  "source_text": "与 text 完全一致的原文台词片段"
}
```

`source_text` is the exact source substring used for `text`. `source_excerpt` may include speaker label, quote marks, or parenthetical context. If a provider omits `source_text`, the backend falls back to `text` for evidence matching; this keeps old providers usable but gives native adapters a stronger contract.

## Verification Design

The verifier remains the source of truth. It does not decide what a dialogue line is by itself; it checks whether the LLM's claim is acceptable.

Required checks:

- The draft contains at least one TTS line.
- Every line has non-empty `text`, valid `character_id`, and unique normalized line id.
- Every character referenced by a line exists in `characters`.
- Non-TTS cue roles such as SFX, MUSIC, scene headings, camera directions, transitions, and title cards are rejected.
- Every line's `text` is traceable in source order.
- Every line's `text` must match a source substring exactly after only safe normalization:
  - Strip Markdown emphasis/backticks around the text.
  - Remove wrapping quote marks around extracted prose dialogue.
  - Move leading parentheticals into `note`.
  - Do not change words, characters, punctuation inside the spoken text, or line-internal whitespace except for source line wrapping that joins adjacent physical lines from the same dialogue block.
- `note` must not duplicate or absorb spoken text.
- If `source_text` is provided, its normalized spoken content must equal the normalized `text`.
- If `source_excerpt` is provided, it must occur in the source or contain a source-text span that occurs in the source.

Repair is allowed once per provider. Repair instructions include the concrete verifier errors, previous JSON, and original source. If repair still fails, the parser raises `ParserQualityError`.

## Multi-Provider Behavior

Keep the existing provider order semantics:

- Disabled providers are skipped.
- Missing keys or network failures are availability errors.
- Quality errors are collected and should remain visible instead of silently accepting worse output.
- A single configured provider is valid.

If a provider returns a structurally valid response that fails source fidelity, the system should not fall back to heuristic parsing. It can try the next enabled LLM provider if the current multi-provider policy allows it for quality failures; the final result must still pass verification.

## Default Provider Presets

The default provider list should favor high-capability, agent-stable, long-context models for extraction fidelity.

Recommended initial order:

1. OpenAI: `gpt-5.5`, adapter `openai-compatible`, base `https://api.openai.com/v1`, key `OPENAI_API_KEY`.
2. Anthropic: `claude-fable-5`, adapter `anthropic`, base `https://api.anthropic.com`, key `ANTHROPIC_API_KEY`.
3. Gemini: `gemini-3.1-pro-preview`, adapter `openai-compatible`, base `https://generativelanguage.googleapis.com/v1beta/openai`, key `GEMINI_API_KEY`. If preview-model stability is not acceptable for a deployment, use the stable `gemini-3.5-flash` preset instead.
4. OpenRouter: `~openai/gpt-latest`, adapter `openai-compatible`, base `https://openrouter.ai/api/v1`, key `OPENROUTER_API_KEY`.
5. Aihubmix: `gpt-5.5`, adapter `openai-compatible`, base `https://aihubmix.com/v1`, key `AIHUBMIX_API_KEY`.
6. Other existing compatible providers that do not conflict with the requirement, ordered below the flagship defaults.
7. KWJM remains a project-specific fallback last if it is still desired by the existing product flow.

Remove Baidu Qianfan and Mistral from defaults and `.env.example`.

The exact model strings are intentionally configurable. Presets should be treated as recommended starting points, not hard-coded capabilities.

## Frontend Design

The parser provider editor adds one field:

- Adapter: `OpenAI-compatible` or `Anthropic`

The add-provider button still creates an OpenAI-compatible draft. Existing provider cards show model, adapter, endpoint, enabled state, and key state.

The KWJM quick activation flow can remain unchanged unless it shares constants with the generic add-provider template. It should continue to produce an OpenAI-compatible provider.

## API Test Endpoint

`/api/parser/providers/test` should instantiate the correct adapter and use the same contract probe as runtime parsing.

The probe must validate:

- Structured output can be decoded.
- One expected line is returned.
- The expected line text is unchanged.
- The note is separated from text.
- The character reference is valid.

For Anthropic, the endpoint must parse the required tool input instead of Chat Completions `choices[0].message.content`.

## Error Handling

User-facing errors should distinguish:

- `disabled`: provider is disabled.
- `needs_key`: API key is missing.
- `blocked`: URL or config is invalid, including SSRF guard failures.
- `partial`: provider responded but failed the parser contract.
- `ready`: contract probe succeeded.

Secrets and endpoint-sensitive error details continue to pass through `scrub_error`.

## Testing Strategy

Backend tests:

- Old provider records without `adapter` load as OpenAI-compatible.
- Default providers exclude Baidu Qianfan and Mistral.
- Default providers include Anthropic, Gemini, OpenRouter, and Aihubmix.
- Anthropic adapter sends Messages API-shaped requests and extracts structured tool input.
- OpenAI-compatible adapter keeps working with existing mocked responses.
- Verifier rejects a one-character dialogue mutation.
- Verifier rejects punctuation changes inside dialogue.
- Verifier accepts leading parenthetical moved into `note`.
- Verifier accepts quoted prose dialogue when wrapping quotes are removed but inner text is unchanged.
- Verifier rejects `note` that absorbs spoken text.
- Repair receives verifier errors and source evidence requirements.
- Provider test endpoint uses the selected adapter.

Frontend tests:

- Parser provider payload preserves `adapter`.
- Add-provider draft defaults to OpenAI-compatible.
- Existing provider payloads without `adapter` render and save safely.
- KWJM activation still creates an OpenAI-compatible provider.

## Rollout

1. Add tests for config and verifier behavior.
2. Add `adapter` to backend config models with backwards-compatible default.
3. Implement shared parser schema/evidence normalization.
4. Upgrade the OpenAI-compatible prompt and response normalizer.
5. Add Anthropic adapter.
6. Update provider presets and `.env.example`.
7. Update frontend types/helpers/editor.
8. Run backend and frontend targeted tests, then broader suites if targeted tests pass.

## Source Notes

- OpenAI docs currently identify `gpt-5.5` as the flagship model for complex reasoning and coding.
- Anthropic docs recommend Claude Opus 4.8 for complex agentic coding and enterprise work, and Claude Fable 5 for the highest available capability; the model id shown for Fable 5 is `claude-fable-5`.
- Gemini docs list Gemini 3.1 Pro Preview for advanced intelligence and agentic workflows, and Gemini 3.5 Flash as a stable model for sustained frontier performance. The design uses Gemini 3.1 Pro Preview for the highest-capability preset and names Gemini 3.5 Flash as the stable alternative.
- Gemini's OpenAI compatibility docs show the compatible base URL `https://generativelanguage.googleapis.com/v1beta/openai/`.
- OpenRouter docs show the Chat Completions endpoint `https://openrouter.ai/api/v1/chat/completions` and the `~openai/gpt-latest` alias.
- Aihubmix docs describe OpenAI-compatible use with base URL `https://aihubmix.com/v1`.

## Approval State

The user approved the recommended Provider Adapter + LLM Agent verification approach on 2026-07-09, including multi-adapter support while preserving single-model OpenAI-compatible access.
