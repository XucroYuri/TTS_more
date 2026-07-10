# Parser Provider Capabilities And Directory Redesign

Date: 2026-07-10
Status: Approved design

## Goal

Replace the duplicated parser activation UI with one always-open provider
directory that can configure current and future model providers without
hard-coded model-name conditionals scattered through the frontend and backend.

The redesign must:

- remove the top-level parser quick-activation panel;
- make the provider directory the only place to configure, test, and save a
  parser provider;
- fix the current editor overflow and alignment defects;
- support provider-specific reasoning modes and effort levels;
- prefer provider model-list APIs when available while preserving custom model
  IDs;
- remove the built-in Lingyiwanwu provider;
- add StepFun and MiniMax presets using their current recommended models;
- update the OpenAI preset to GPT-5.6 Terra with high reasoning effort; and
- preserve user-customized providers and secrets during configuration
  migration.

## Scope

This change covers the parser provider registry, parser-provider persistence,
model discovery, request construction, tests, and the parser provider directory
inside the existing service modal.

It does not redesign the surrounding workstation, add new routes, change the
parser verification contract, or expose raw arbitrary request JSON in the UI.

## Current Problems

### Duplicate ownership

The quick-activation panel and provider directory edit the same parser provider
state through separate controls. This creates two save/test paths and makes the
provider directory look optional even though it owns the complete provider
configuration.

### Layout overflow

The provider editor combines a two-column directory, a three-column form,
intrinsically sized selects, long endpoints, status content, and footer actions
inside a fixed-width modal. In particular, selects in `.llm-form-grid` do not
share the `width: 100%; min-width: 0` contract used by inputs. Long provider and
model values therefore force grid tracks wider than their container.

### Protocol-specific reasoning

The current persisted provider schema has no reasoning fields and the backend
always emits one generic Chat Completions payload. Providers use different
request shapes:

- OpenAI-style APIs use `reasoning_effort`;
- Claude Messages uses `output_config.effort` and may use adaptive thinking;
- DeepSeek combines `thinking.type` with `reasoning_effort`;
- GLM exposes `thinking.type` but does not document granular effort levels for
  the current API;
- StepFun supports `reasoning_effort` values `low` and `high` on
  `step-3.5-flash-2603`; and
- MiniMax M2.7 reasons by default but does not currently document a configurable
  effort enum.

Treating these as one unconditional parameter would either send invalid fields
or present controls that do not affect the selected model.

## Chosen Approach

Use a capability-driven provider registry. Keep the existing protocol adapter
as the transport boundary, and add a stable provider identity plus a reasoning
strategy. The registry supplies defaults and capabilities; the persisted record
stores the user's selected values.

This is preferred over URL/name inference because endpoints and names are
editable. It is preferred over a raw JSON editor because registry-backed fields
can be validated, migrated, and tested without allowing malformed request
payloads.

## Provider Registry

Each built-in provider profile defines:

```text
provider_id
display_name
adapter
auth_scheme
default_base_url
api_key_env
default_model
curated_models
model_catalog_strategy
reasoning_strategy
supported_thinking_modes
supported_efforts
default_thinking_mode
default_reasoning_effort
omit_sampling_when_reasoning
```

`provider_id` is stable and machine-readable. Display names remain editable.
Custom providers use `provider_id: custom` and choose a protocol adapter and
reasoning strategy explicitly under advanced settings.

The registry is backend-owned so request construction and validation use one
source of truth. The frontend receives public capability metadata with the
provider configuration response and never duplicates transport rules.

## Built-In Provider Changes

The built-in catalog will make these changes:

| Provider | Provider ID | Default model | Reasoning default | Model source |
| --- | --- | --- | --- | --- |
| OpenAI | `openai` | `gpt-5.6-terra` | high | remote, curated fallback |
| Anthropic | `anthropic` | `claude-fable-5` | high | remote, curated fallback |
| DeepSeek | `deepseek` | `deepseek-v4-pro` | thinking enabled, high | remote, curated fallback |
| Zhipu GLM | `zhipu` | `glm-5.1` | thinking enabled | curated, remote when supported |
| StepFun | `stepfun` | `step-3.5-flash-2603` | high | curated, remote when supported |
| MiniMax | `minimax` | `MiniMax-M2.7` | automatic | remote, curated fallback |

The existing Lingyiwanwu built-in profile is removed. The remaining provider
presets keep their existing defaults unless separately listed above.

StepFun uses `https://api.stepfun.com/v1` and `STEP_API_KEY`. MiniMax uses the
OpenAI-compatible endpoint `https://api.minimaxi.com/v1` and
`MINIMAX_API_KEY`.

GPT-5.6 Terra is a limited-preview model at the time of this design. The OpenAI
preset remains disabled by default, so users without preview access can select
another remotely listed or custom model before enabling it.

## Persisted Provider Schema

Extend the provider record with normalized optional fields:

```text
provider_id: string
reasoning_strategy: auto | openai-effort | thinking-effort |
  anthropic-output-config | thinking-toggle | implicit | none
thinking_mode: auto | enabled | disabled
reasoning_effort: auto | minimal | low | medium | high | xhigh | max
```

The API validates a selected effort against the resolved provider/model
capabilities. `auto` means the backend omits an explicit effort value and lets
the provider use its default. Unsupported combinations return a clear 400
response during save or test rather than silently changing behavior.

`key_configured` remains public read-only state. Plaintext API keys continue to
be accepted only on save/test requests, written to `.env.local`, omitted from
the provider JSON file, and never returned.

## Reasoning Request Mapping

Request construction is centralized in adapter helpers rather than assembled
inline in each provider class.

### OpenAI effort

For supported OpenAI-compatible models, emit top-level
`reasoning_effort`. Omit it for `auto`.

### DeepSeek thinking and effort

Emit `thinking: {"type": "enabled" | "disabled"}` when the mode is explicit.
When thinking is enabled, emit supported `reasoning_effort` values. Current
DeepSeek presets expose `high` and `max` and map no unsupported UI values.

### Claude effort

Emit `output_config: {"effort": value}`. For models whose model metadata
advertises adaptive thinking, emit `thinking: {"type": "adaptive"}` when the
user enables thinking. Omit `output_config` for automatic effort.

### GLM thinking

Emit only `thinking: {"type": "enabled" | "disabled"}`. Do not present or send
invented granular effort levels unless later provider documentation or model
metadata adds them.

### StepFun effort

For `step-3.5-flash-2603`, expose and emit `low` or `high`. Other StepFun models
fall back to automatic effort unless their capability metadata is updated.

### MiniMax implicit reasoning

Do not emit an effort field for MiniMax M2.7 while no supported effort enum is
documented. The UI shows automatic reasoning and remains registry-driven so a
future capability update does not require redesigning the form.

### Sampling compatibility

The request builder omits sampling fields such as `temperature` when the
resolved provider profile declares them incompatible with reasoning mode. This
prevents the new reasoning controls from producing invalid provider requests.

## Model Discovery

Add a read-only backend endpoint:

```text
POST /api/parser/providers/models
```

The request contains one provider draft and an optional unsaved API key. If no
key is supplied, the backend resolves the configured environment variable. The
endpoint performs existing egress validation before contacting a provider.

The response is:

```text
models: [{ id, display_name?, capabilities? }]
source: remote | curated
warning?: string
```

Discovery strategies:

- OpenAI-compatible: call the profile-specific models endpoint, commonly
  `/v1/models` or `/models`, using Bearer authentication;
- Anthropic: call `/v1/models` with Anthropic headers and consume the returned
  effort/thinking capability metadata;
- curated: return the provider registry list when no documented endpoint
  exists; and
- remote-with-fallback: return curated values plus a non-blocking warning when
  the remote request fails.

Model discovery never saves configuration and never returns request headers,
keys, or raw provider errors containing sensitive endpoint data.

## Model Picker UX

The model field is a native select when catalog entries are available. It shows
the recommended model first, followed by discovered models. The final option is
"Custom model ID"; choosing it reveals an editable text field. A refresh icon
re-runs discovery without saving.

If discovery cannot run because no key is configured, the curated catalog is
shown immediately. If no curated catalog exists, the field starts in custom
mode. Discovery failure never blocks editing, testing, or saving.

## Directory UI

Remove the entire quick-activation panel, including its provider metadata,
standalone API-key input, test button, and save/check button. The provider
directory renders whenever the parser section is active; there is no collapsed
default state.

The directory contains:

1. A compact header with provider count, configured-key count, and directory
   status.
2. A left provider list with name, selected model, enabled state, key state, and
   latest test state.
3. A right editor divided into Connection, Model, Reasoning, and Routing
   sections.
4. A sticky action footer containing Add provider, Test connection, and Save
   configuration.

The Connection section owns enabled state, display name, adapter, base URL, API
key, and API-key environment name. The Model section owns discovery and model
selection. The Reasoning section is entirely capability-driven. Priority and
timeout remain in Routing so the common path stays compact.

## Responsive Layout

At wide modal sizes, the directory uses a bounded two-column layout with a
provider list of at least 240px and an editor that can shrink to zero without
overflow. The editor uses at most two form columns.

At medium widths, the provider list stacks above the editor. At mobile widths,
all form fields and footer actions become one column.

All `input`, `select`, buttons, header text containers, and grid children in the
directory receive an explicit `min-width: 0`; form controls receive
`width: 100%`. Long endpoints and model IDs truncate in summaries but remain
fully visible in their editable fields and title attributes. The modal body is
the only vertical scrolling container; nested horizontal scrolling is not
allowed.

## Migration

When loading a persisted provider file:

1. Infer `provider_id` for known legacy presets from their exact name, endpoint,
   and key environment fingerprint.
2. Remove the exact legacy Lingyiwanwu built-in preset. A modified/custom entry
   is preserved as `custom`.
3. Append disabled StepFun and MiniMax presets when they are missing.
4. Upgrade an old shipped default model only when the record still matches the
   old preset fingerprint and is disabled. Enabled or customized records are
   never overwritten.
5. Normalize missing reasoning fields to provider defaults without writing the
   file during a GET request. The normalized state is persisted only after the
   user clicks Save.

This migration makes the new catalog visible while avoiding silent changes to
active production routing.

## Error Handling

- Remote model-list failure returns curated models with a warning.
- Unsupported effort/model combinations fail validation with the provider,
  model, and accepted values named in the message.
- A provider test uses the same request builder as real parsing so a successful
  test proves the selected reasoning settings are accepted.
- Missing keys, unauthorized model access, and limited-preview model access are
  shown as provider test failures without clearing the draft.
- Provider errors continue through the existing scrubber before reaching the
  frontend.

## Testing

Backend tests will cover:

- registry defaults, including StepFun and MiniMax and absence of
  Lingyiwanwu;
- legacy provider normalization and conservative migration;
- request payload mapping for OpenAI, Claude, DeepSeek, GLM, StepFun, and
  MiniMax;
- omission of unsupported reasoning and sampling fields;
- remote model discovery for OpenAI-compatible and Anthropic response shapes;
- curated fallback after discovery failure;
- egress validation and secret masking; and
- provider test parity with real parse request construction.

Frontend tests will cover:

- capability normalization and valid effort choices;
- model picker remote, curated, empty, custom, loading, and failure states;
- save payloads for automatic and explicit reasoning settings; and
- preservation of drafts after model discovery or provider test failure.

Rendered browser verification will cover:

- directory visible by default with no duplicate activation panel;
- selecting a provider and choosing a discovered or custom model;
- capability-specific reasoning controls;
- test and save state transitions;
- no clipping, overlap, or horizontal overflow at the current desktop viewport,
  a medium viewport, and a mobile viewport; and
- console health and existing parser modal close/refresh behavior.

## Source Notes

- OpenAI GPT-5.6 preview and model IDs:
  https://help.openai.com/en/articles/20001325-a-preview-of-gpt-5-6-sol-terra-and-luna
- OpenAI model listing:
  https://platform.openai.com/docs/api-reference/models
- Claude model capabilities and listing:
  https://platform.claude.com/docs/en/api/models/list
- Claude effort:
  https://platform.claude.com/docs/en/build-with-claude/effort
- DeepSeek model listing and thinking mode:
  https://api-docs.deepseek.com/api/list-models
  https://api-docs.deepseek.com/guides/thinking_mode
- GLM model overview and thinking mode:
  https://docs.bigmodel.cn/cn/guide/start/model-overview
  https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode
- StepFun Chat Completion reasoning controls:
  https://platform.stepfun.com/docs/zh/api-reference/chat/chat-completion-create
- MiniMax text models and model listing:
  https://platform.minimaxi.com/docs/api-reference/api-overview
  https://platform.minimaxi.com/docs/api-reference/models/openai/list-models
