# Semantic Script Analysis Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Build a source-grounded, human-confirmed semantic script analysis stage that recognizes speakers, emotion evidence, and quoted or unquoted dialogue before projecting accepted utterances into the existing TTS workbench.

**Architecture:** Keep ScriptRevision as the immutable source of truth. Add sidecar AnalysisRun, SemanticAnalysisDraft, and SemanticRevision records, an independent semantic provider/analyzer, and a controlled confirmation projection into a compatibility ParseRevision. Add a feature-scoped React analysis workspace whose local draft state never enters the existing broad project autosave.

**Tech Stack:** Python 3.11, FastAPI, Pydantic 2, file-backed JSON storage, httpx, pytest, React 19, TypeScript 5.9, Vite 7, Vitest, jsdom, pnpm.

**Spec:** docs/superpowers/specs/2026-08-31-semantic-script-analysis-workbench-design.md

## Global Constraints

- Preserve all existing dirty-worktree changes; never replace or reset unrelated hunks.
- Keep backend local-first and bound to 127.0.0.1.
- Use Python >=3.11,<3.12 and the repository .venv.
- Use pnpm exclusively for frontend commands.
- Persist project data as JSON files; add no database or external task queue.
- Support every new user-visible string in Chinese and English, with Chinese fallback.
- Do not log API keys, Authorization, complete scripts, complete prompts, provider response bodies, or raw exception stacks.
- Do not call the legacy parser verifier or legacy provider parse methods from the semantic workflow.
- Accepted dialogue must be an exact contiguous source substring and remain in source order.
- Run tests with fake providers; live model calls are manual smoke tests only.

---

### Task 1: Immutable Source Metadata and UTF-16 Domain Primitives

**Files:**
- Create: backend/app/semantic_source.py
- Create: backend/app/semantic_models.py
- Modify: backend/app/models.py
- Create: backend/tests/test_semantic_models.py
- Modify: backend/tests/test_models.py

**Interfaces:**
- Produces: sha256_source(text: str) -> str
- Produces: py_index_to_utf16(text: str, index: int) -> int
- Produces: utf16_to_py_index(text: str, offset: int) -> int
- Produces: validate_source_span(span: SourceSpan, source: ScriptRevision) -> None
- Produces: SourceSpan, SemanticAnnotation, CharacterCandidate, SemanticUtterance, AnalysisWarning, UnresolvedCandidate, AnalysisError, AnalysisRun, SemanticAnalysisDraft, SemanticRevision
- Modifies: ScriptRevision with source_filename, source_media_type, source_sha256
- Modifies: ScriptLine with semantic_revision_id, utterance_id

- [ ] **Step 1: Write failing UTF-16 and legacy compatibility tests**

    def test_utf16_round_trip_handles_chinese_emoji_and_crlf():
        text = "甲😀\r\n台词"
        assert py_index_to_utf16(text, 0) == 0
        assert py_index_to_utf16(text, 2) == 3
        assert utf16_to_py_index(text, 3) == 2
        with pytest.raises(ValueError, match="surrogate"):
            utf16_to_py_index(text, 2)

    def test_legacy_script_models_load_without_semantic_fields():
        revision = ScriptRevision(revision_id="script-r001", source_markdown="甲：你好")
        line = ScriptLine(id="line-001", character_id="char-1", text="你好")
        assert revision.source_sha256 is None
        assert line.semantic_revision_id is None
        assert line.utterance_id is None

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

    .\.venv\Scripts\python.exe -m pytest backend/tests/test_semantic_models.py backend/tests/test_models.py -q

Expected: collection or import failure because semantic_source.py and semantic models do not exist.

- [ ] **Step 3: Implement source helpers and compatibility fields**

Implement semantic_source.py with exact UTF-16 conversion. Reject offsets inside a surrogate pair and indexes outside the string.

    def sha256_source(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def py_index_to_utf16(text: str, index: int) -> int:
        if index < 0 or index > len(text):
            raise ValueError("python index out of range")
        return len(text[:index].encode("utf-16-le")) // 2

    def utf16_to_py_index(text: str, offset: int) -> int:
        if offset < 0:
            raise ValueError("UTF-16 offset out of range")
        units = 0
        for index, char in enumerate(text):
            if units == offset:
                return index
            width = len(char.encode("utf-16-le")) // 2
            if units < offset < units + width:
                raise ValueError("UTF-16 offset splits a surrogate pair")
            units += width
        if units == offset:
            return len(text)
        raise ValueError("UTF-16 offset out of range")

Add the optional fields to backend/app/models.py. Do not change existing line_uid behavior.

- [ ] **Step 4: Implement semantic Pydantic models and invariants**

Use string enums with the exact spec values. SourceSpan validation must verify non-negative half-open offsets and non-empty text. SemanticUtterance must include dialogue_annotation_id, optional speaker_annotation_id, character_candidate_id, evidence ids, emotion fields, language, confidence, uncertainty codes, and review status.

Add:

    def validate_source_span(span: SourceSpan, source: ScriptRevision) -> None:
        if span.source_revision_id != source.revision_id:
            raise ValueError("source revision mismatch")
        expected_hash = source.source_sha256 or sha256_source(source.source_markdown)
        if span.source_sha256 != expected_hash:
            raise ValueError("source hash mismatch")
        start = utf16_to_py_index(source.source_markdown, span.start_utf16)
        end = utf16_to_py_index(source.source_markdown, span.end_utf16)
        if source.source_markdown[start:end] != span.text:
            raise ValueError("source span text mismatch")

- [ ] **Step 5: Run tests and commit**

Run:

    .\.venv\Scripts\python.exe -m pytest backend/tests/test_semantic_models.py backend/tests/test_models.py -q

Expected: PASS.

Commit only this task’s files:

    git add backend/app/semantic_source.py backend/app/semantic_models.py backend/app/models.py backend/tests/test_semantic_models.py backend/tests/test_models.py
    git commit -m "feat: add semantic analysis domain primitives"

---

### Task 2: Versioned Semantic Sidecar Storage and Controlled Draft Commands

**Files:**
- Create: backend/app/semantic_storage.py
- Modify: backend/app/storage.py
- Create: backend/tests/test_semantic_storage.py

**Interfaces:**
- Consumes: semantic models and source validation from Task 1
- Produces: ProjectStore.project_lock(project_id)
- Produces: ProjectStore.project_semantic_dir(project_id)
- Produces: ProjectStore.update_project(project_id, mutation)
- Produces: SemanticStore.create_run_and_draft, load_run, load_draft, save_run, replace_analysis_result, patch_draft, load_revision, interrupt_incomplete_runs
- Produces: DraftPatchRequest with expected_version and a discriminated operations list

- [ ] **Step 1: Write failing storage layout, version, cascade, and recovery tests**

    def test_create_run_and_draft_persists_sidecars(project_store):
        store = SemanticStore(project_store)
        run, draft = store.create_run_and_draft("demo", "script-r001", trace_id="trace-1")
        assert store.load_run(run.id).draft_id == draft.id
        assert store.load_draft(draft.id).version == 1

    def test_patch_is_atomic_and_rejects_stale_versions(project_store):
        store = seeded_semantic_store(project_store)
        draft = store.patch_draft("draft-1", 1, [create_dialogue_operation()])
        assert draft.version == 2
        with pytest.raises(SemanticConflictError, match="draft_version_conflict"):
            store.patch_draft("draft-1", 1, [])

    def test_delete_dialogue_cascades_utterance(project_store):
        store = seeded_semantic_store(project_store)
        patched = store.patch_draft("draft-1", 1, [delete_annotation_operation("dialogue-1")])
        assert patched.utterances == []

    def test_interrupt_incomplete_runs_preserves_terminal_runs(project_store):
        store = SemanticStore(project_store)
        running = seed_run(store, status="running")
        completed = seed_run(store, status="completed")
        assert store.interrupt_incomplete_runs() == 1
        assert store.load_run(running.id).status == "interrupted"
        assert store.load_run(completed.id).status == "completed"

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

    .\.venv\Scripts\python.exe -m pytest backend/tests/test_semantic_storage.py -q

Expected: FAIL because SemanticStore and ProjectStore transaction helpers do not exist.

- [ ] **Step 3: Add project-scoped locking and update_project**

Add a weak project lock registry alongside the existing manifest locks. update_project must load, apply the supplied mutation while locked, save through the existing atomic materializer, and return the saved project plus mutation result.

    @contextmanager
    def project_lock(self, project_id: str) -> Iterator[None]:
        lock = self._project_lock_for(self._safe_project_id(project_id))
        with lock:
            yield

    def project_semantic_dir(self, project_id: str) -> Path:
        return self.project_script_dir(project_id) / "semantic"

    def update_project(self, project_id: str, mutation: Callable[[ScriptProject], R]) -> tuple[ScriptProject, R]:
        with self.project_lock(project_id):
            project = self.load_project(project_id)
            result = mutation(project)
            self.save_project(project_id, project)
            return project, result

- [ ] **Step 4: Implement sidecar indexes and command application**

Store:

    script/semantic/runs/<run_id>.json
    script/semantic/drafts/<draft_id>.json
    script/semantic/revisions/<semantic_revision_id>.json

Maintain a small semantic/index.json mapping run and draft ids to project ids, written atomically. Apply each operation to a deep copy, cascade references exactly as the spec truth table requires, validate the complete draft, then increment version once and replace the file.

Rejected requirements:

- PATCH while run is queued or running raises semantic_run_not_terminal.
- PATCH after confirmation raises draft_confirmed.
- Any dangling reference or accepted utterance without accepted dialogue and character raises semantic_draft_invalid.

- [ ] **Step 5: Run tests and commit**

Run:

    .\.venv\Scripts\python.exe -m pytest backend/tests/test_semantic_storage.py backend/tests/test_storage_security.py -q

Expected: PASS.

Commit only this task’s files:

    git add backend/app/semantic_storage.py backend/app/storage.py backend/tests/test_semantic_storage.py
    git commit -m "feat: persist versioned semantic analysis drafts"

---

### Task 3: Idempotent Confirmation and Existing Workbench Projection

**Files:**
- Create: backend/app/semantic_projection.py
- Modify: backend/app/semantic_storage.py
- Modify: backend/app/models.py
- Create: backend/tests/test_semantic_projection.py
- Modify: backend/tests/test_semantic_storage.py

**Interfaces:**
- Consumes: SemanticStore, SemanticAnalysisDraft, ProjectStore.update_project
- Produces: project_confirmed_draft(project, draft, semantic_revision_id) -> ParseRevision
- Produces: SemanticStore.confirm_draft(draft_id, expected_version, idempotency_key) -> SemanticConfirmResult

- [ ] **Step 1: Write failing projection and idempotency tests**

    def test_confirmation_projects_only_accepted_assigned_utterances(project_store):
        store = seeded_confirmable_store(project_store)
        result = store.confirm_draft("draft-1", 3, "confirm-key-1")
        assert result.project.active_parse_revision_id == "semantic-" + result.semantic_revision.id
        assert [line.text for line in result.project.lines] == ["真的有加速效果！"]
        assert result.project.lines[0].semantic_revision_id == result.semantic_revision.id
        assert result.project.lines[0].utterance_id == "utterance-1"

    def test_same_confirmation_key_returns_same_revision_without_duplicates(project_store):
        store = seeded_confirmable_store(project_store)
        first = store.confirm_draft("draft-1", 3, "confirm-key-1")
        second = store.confirm_draft("draft-1", 3, "confirm-key-1")
        assert second.semantic_revision.id == first.semantic_revision.id
        assert len(second.project.parse_revisions) == len(first.project.parse_revisions)

    def test_different_key_on_confirmed_draft_conflicts(project_store):
        store = seeded_confirmable_store(project_store)
        store.confirm_draft("draft-1", 3, "confirm-key-1")
        with pytest.raises(SemanticConflictError, match="draft_confirmed"):
            store.confirm_draft("draft-1", 3, "confirm-key-2")

- [ ] **Step 2: Run tests and verify failure**

Run:

    .\.venv\Scripts\python.exe -m pytest backend/tests/test_semantic_projection.py backend/tests/test_semantic_storage.py -q

Expected: FAIL because confirmation projection is absent.

- [ ] **Step 3: Implement deterministic role and line projection**

Resolve a character in this order:

    def resolve_project_character(project: ScriptProject, candidate: CharacterCandidate) -> ProjectCharacter:
        if candidate.project_character_id:
            return require_project_character(project, candidate.project_character_id)
        matches = exact_project_character_matches(project, candidate.canonical_name)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise SemanticProjectionError("ambiguous_project_character")
        return new_project_character_from_candidate(candidate)

Build one compatibility ParseRevision with provider semantic-confirmed, line.id equal to utterance id, note equal to normalized emotion or empty string, and language equal to utterance language or project default.

- [ ] **Step 4: Implement crash-safe idempotent confirmation**

Under the project lock:

1. Reload draft and handle an existing confirmation key before version validation.
2. Validate expected_version and every accepted utterance.
3. Write immutable SemanticRevision.
4. Add or reuse the deterministic compatibility ParseRevision and activate it.
5. Save project.
6. Mark draft confirmed with both revision ids and the key.

If a semantic revision exists after a process interruption but project materialization is missing, retry with the same key must finalize the same deterministic ids rather than create new ones.

- [ ] **Step 5: Run tests and commit**

Run:

    .\.venv\Scripts\python.exe -m pytest backend/tests/test_semantic_projection.py backend/tests/test_semantic_storage.py backend/tests/test_models.py -q

Expected: PASS.

Commit only this task’s files:

    git add backend/app/semantic_projection.py backend/app/semantic_storage.py backend/app/models.py backend/tests/test_semantic_projection.py backend/tests/test_semantic_storage.py
    git commit -m "feat: confirm semantic drafts into TTS revisions"

---

### Task 4: Independent Semantic Provider, Prompt Contract, and Grounding

**Files:**
- Create: backend/app/semantic_provider.py
- Create: backend/app/semantic_analysis.py
- Create: backend/tests/fixtures/mixed_semantic_script.txt
- Create: backend/tests/test_semantic_provider.py
- Create: backend/tests/test_semantic_analysis.py

**Interfaces:**
- Consumes: ParserProviderConfig and parser provider records, validate_egress_url, scrub_error
- Produces: AnalysisChunk, UtteranceCandidate, CharacterCandidatePayload, SemanticProviderResponse
- Produces: SemanticProvider.analyze_chunk(chunk) -> SemanticProviderResponse
- Produces: locate_candidate(chunk, candidate) -> LocatedCandidate | UnresolvedCandidate
- Produces: SemanticAnalysisService.analyze(project_id, source_revision, run, draft) -> SemanticAnalysisDraft

- [ ] **Step 1: Write failing provider contract and security tests**

    def test_openai_semantic_provider_uses_semantic_contract(monkeypatch):
        response = fake_openai_response(characters=[], utterances=[])
        provider = build_semantic_provider(config(), client=fake_client(response))
        result = provider.analyze_chunk(AnalysisChunk.single("只有叙述"))
        assert result.utterance_candidates == []
        assert "source-grounded semantic annotations" in fake_client.last_json["messages"][0]["content"]

    def test_runtime_endpoint_revalidates_dns_and_scrubs_errors(monkeypatch):
        provider = build_semantic_provider(private_dns_config(), client=fake_client())
        with pytest.raises(SemanticProviderUnavailable) as error:
            provider.analyze_chunk(AnalysisChunk.single("文本"))
        assert "169.254.169.254" not in str(error.value)

    def test_empty_utterance_response_is_valid():
        assert decode_semantic_payload({"character_candidates": [], "utterance_candidates": []}).utterance_candidates == []

- [ ] **Step 2: Write failing grounding, alias, and partial-success tests**

The mixed fixture must include:

    胶布（惊喜，大喊）：真的！真的有加速效果！跑得好快！！这下大笨蛙追不上了
    诸葛九九：胶布，踩蓝格！
    九九继续向前，地面蓝光闪烁。

Tests must assert exact unquoted dialogue spans, a quoted variant excluding wrapper quotes, duplicate text disambiguation with anchors and occurrence_index, one-character mutation rejection, source_excerpt speaker mismatch as a warning, 诸葛九九/九九 as a controlled alias candidate, 王/老王 not auto-merged, and a failed chunk yielding a partial completed draft.

- [ ] **Step 3: Run tests and verify failure**

Run:

    .\.venv\Scripts\python.exe -m pytest backend/tests/test_semantic_provider.py backend/tests/test_semantic_analysis.py backend/tests/test_net_guard.py -q

Expected: FAIL because semantic provider and analysis service are absent.

- [ ] **Step 4: Implement adapter-specific semantic requests**

Create a new system prompt and response schema. Reuse configuration fields and low-level URL helpers, but do not invoke legacy parse, repair, draft normalization, or ScriptParseVerifier.

Before every actual HTTP request:

    validate_egress_url(endpoint, allow_loopback=True, resolve_dns=True)

Resolve the API key from config.api_key_env. Use config.timeout_seconds. Map missing key, egress, transport, timeout, malformed JSON, and schema failures to typed semantic exceptions without carrying the original response body.

- [ ] **Step 5: Implement chunking, exact location, aliases, and draft construction**

Chunk on paragraph boundaries with global UTF-16 metadata and overlap. For each candidate:

1. Exact-search dialogue_excerpt.
2. If duplicated, filter by exact anchor suffix/prefix and zero-based occurrence_index.
3. If still ambiguous, emit unresolved source_anchor_ambiguous and create no dialogue annotation.
4. Create separate speaker and emotion evidence annotations only when their evidence uniquely resolves.
5. Preserve valid chunks when another chunk fails.
6. Sort utterances by dialogue start_utf16 and deduplicate overlap spans.

Coverage heuristics may add missing_quoted_dialogue warnings but never fail the run.

- [ ] **Step 6: Run tests and commit**

Run:

    .\.venv\Scripts\python.exe -m pytest backend/tests/test_semantic_provider.py backend/tests/test_semantic_analysis.py backend/tests/test_parser.py backend/tests/test_net_guard.py -q

Expected: PASS, including all legacy parser tests.

Commit only this task’s files:

    git add backend/app/semantic_provider.py backend/app/semantic_analysis.py backend/tests/fixtures/mixed_semantic_script.txt backend/tests/test_semantic_provider.py backend/tests/test_semantic_analysis.py
    git commit -m "feat: analyze messy scripts into grounded semantic drafts"

---

### Task 5: Persistent Executor, Semantic Logs, and FastAPI Routes

**Files:**
- Create: backend/app/semantic_logging.py
- Create: backend/app/semantic_executor.py
- Create: backend/app/semantic_routes.py
- Modify: backend/app/main.py
- Create: backend/tests/test_semantic_routes.py
- Create: backend/tests/test_semantic_logging.py
- Modify: backend/tests/test_api.py

**Interfaces:**
- Consumes: SemanticStore, SemanticAnalysisService, SemanticConfirmResult
- Produces: SemanticAnalysisExecutor.submit(run_id), recover_interrupted(), shutdown()
- Produces: build_semantic_router(project_store, semantic_store, executor) -> APIRouter
- Produces: JSONL events keyed by run_id and trace_id

- [ ] **Step 1: Write failing API lifecycle and error persistence tests**

    def test_analysis_run_completes_and_is_readable_after_new_app(tmp_path, fake_semantic_service):
        app = create_app(data_root=tmp_path, semantic_service=fake_semantic_service)
        with TestClient(app) as client:
            created = client.post("/api/projects/demo/analysis-runs", json={"source_revision_id": "script-r001"})
            assert created.status_code == 202
            terminal = wait_for_terminal(client, created.json()["run_id"])
            assert terminal["status"] == "completed"
        with TestClient(create_app(data_root=tmp_path, semantic_service=fake_semantic_service)) as client:
            assert client.get("/api/analysis-runs/" + terminal["id"]).json()["status"] == "completed"

    def test_async_contract_failure_is_persisted_as_422(tmp_path, failing_semantic_service):
        with TestClient(create_app(data_root=tmp_path, semantic_service=failing_semantic_service)) as client:
            created = create_run(client)
            failed = wait_for_terminal(client, created["run_id"])
            assert failed["status"] == "failed"
            assert failed["error"]["http_status"] == 422
            assert failed["error"]["trace_id"]

    def test_patch_and_confirm_routes_enforce_version_and_idempotency(client):
        assert client.patch("/api/analysis-drafts/draft-1", json={"expected_version": 0, "operations": []}).status_code == 409
        first = confirm(client, "key-1")
        second = confirm(client, "key-1")
        assert second.json()["semantic_revision"]["id"] == first.json()["semantic_revision"]["id"]

- [ ] **Step 2: Write failing log redaction and rotation tests**

    def test_semantic_log_never_writes_source_or_secret(tmp_path):
        logger = semantic_event_logger(tmp_path, max_bytes=512, backup_count=2)
        logger.error(run_id="run-1", source_text="SECRET SCRIPT", api_key="sk-secret", exception=RuntimeError("sk-secret"))
        payload = read_all_semantic_logs(tmp_path)
        assert "SECRET SCRIPT" not in payload
        assert "sk-secret" not in payload
        assert '"run_id": "run-1"' in payload

- [ ] **Step 3: Run tests and verify failure**

Run:

    .\.venv\Scripts\python.exe -m pytest backend/tests/test_semantic_routes.py backend/tests/test_semantic_logging.py -q

Expected: FAIL because executor, router, and logger do not exist.

- [ ] **Step 4: Implement executor and recovery**

Use ThreadPoolExecutor(max_workers=int(os.getenv("TTS_MORE_SEMANTIC_WORKERS", "2"))). Before analysis mark run running. On success replace the draft and mark completed complete or partial. Map typed failures to persisted AnalysisError with 422, 502, or 504. On startup mark prior queued/running records interrupted. On shutdown stop accepting submissions and shut down the pool.

- [ ] **Step 5: Implement safe semantic logging**

Write semantic-analysis.jsonl with 10 MB and five backups. Accept only an allowlisted metadata dictionary; never accept source_text, prompt, api_key, Authorization, response body, or traceback fields. Store exception class and scrub_error(exception).

- [ ] **Step 6: Implement routes and create_app integration**

Add an optional semantic_service factory argument to create_app for deterministic tests. Instantiate SemanticStore, SemanticAnalysisExecutor, and router without changing legacy parse routes. Use FastAPI lifespan or equivalent registered startup/shutdown handlers to recover and close the executor.

Map:

    SemanticNotFoundError -> 404
    SemanticConflictError -> 409
    SemanticValidationError -> 422

Return 200 for GET of a failed asynchronous run and include error.http_status in its body.

- [ ] **Step 7: Run backend targeted and broad regression tests**

Run:

    .\.venv\Scripts\python.exe -m pytest backend/tests/test_semantic_models.py backend/tests/test_semantic_storage.py backend/tests/test_semantic_projection.py backend/tests/test_semantic_provider.py backend/tests/test_semantic_analysis.py backend/tests/test_semantic_routes.py backend/tests/test_semantic_logging.py backend/tests/test_models.py backend/tests/test_parser.py backend/tests/test_api.py -q

Expected: PASS.

- [ ] **Step 8: Commit**

    git add backend/app/semantic_logging.py backend/app/semantic_executor.py backend/app/semantic_routes.py backend/app/main.py backend/tests/test_semantic_routes.py backend/tests/test_semantic_logging.py backend/tests/test_api.py
    git commit -m "feat: expose persistent semantic analysis API"

---

### Task 6: Frontend Semantic Types, API Client, and File Input Utilities

**Files:**
- Modify: frontend/src/types.ts
- Modify: frontend/src/api.ts
- Modify: frontend/src/api.test.ts
- Create: frontend/src/features/script-analysis/fileInput.ts
- Create: frontend/src/features/script-analysis/fileInput.test.ts

**Interfaces:**
- Consumes: backend API contracts from Tasks 1 through 5
- Produces: TypeScript SourceSpan, SemanticAnnotation, CharacterCandidate, SemanticUtterance, SemanticAnalysisDraft, SemanticRevision, AnalysisRun, DraftOperation, SemanticConfirmResponse
- Produces: createAnalysisRun, fetchAnalysisRun, fetchAnalysisDraft, patchAnalysisDraft, confirmAnalysisDraft
- Produces: readScriptFile(file: File) -> Promise<{ text: string; filename: string; mediaType: string }>

- [ ] **Step 1: Write failing API request-shape tests**

    it("creates and confirms semantic analysis drafts", async () => {
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ run_id: "run-1", draft_id: "draft-1" }))
        .mockResolvedValueOnce(jsonResponse({ semantic_revision: { id: "semantic-1" }, project: projectFixture }));
      await createAnalysisRun("demo", "script-r001");
      await confirmAnalysisDraft("draft-1", 3, "key-1");
      expect(fetchMock.mock.calls[0][0]).toBe("/api/projects/demo/analysis-runs");
      expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
        expected_version: 3,
        idempotency_key: "key-1"
      });
    });

- [ ] **Step 2: Write failing file input tests**

    it("accepts txt and md, removes one UTF-8 BOM, and preserves CRLF", async () => {
      const file = new File(["\uFEFF甲\r\n台词"], "script.md", { type: "text/markdown" });
      await expect(readScriptFile(file)).resolves.toMatchObject({ text: "甲\r\n台词", filename: "script.md" });
    });

    it("rejects unsupported files", async () => {
      await expect(readScriptFile(new File(["x"], "script.docx"))).rejects.toThrow("unsupported_script_file");
    });

- [ ] **Step 3: Run tests and verify failure**

Run:

    pnpm --dir frontend exec vitest run src/api.test.ts src/features/script-analysis/fileInput.test.ts

Expected: FAIL because semantic API functions and fileInput do not exist.

- [ ] **Step 4: Implement exact types, API calls, and file reader**

Keep new semantic types outside ScriptProject. Extend only ScriptRevision and ScriptLine with optional compatibility fields. Use the existing request helper and jsonHeaders.

    export async function patchAnalysisDraft(
      draftId: string,
      expectedVersion: number,
      operations: DraftOperation[]
    ): Promise<SemanticAnalysisDraft> {
      return request("/api/analysis-drafts/" + encodeURIComponent(draftId), {
        method: "PATCH",
        headers: jsonHeaders,
        body: JSON.stringify({ expected_version: expectedVersion, operations })
      });
    }

readScriptFile must validate the lower-cased extension, use File.text(), remove only a leading BOM, preserve the rest exactly, and enforce the same 200,000 code-point early warning without replacing the backend boundary.

- [ ] **Step 5: Run tests and commit**

Run:

    pnpm --dir frontend exec vitest run src/api.test.ts src/features/script-analysis/fileInput.test.ts

Expected: PASS.

Commit only this task’s files:

    git add frontend/src/types.ts frontend/src/api.ts frontend/src/api.test.ts frontend/src/features/script-analysis/fileInput.ts frontend/src/features/script-analysis/fileInput.test.ts
    git commit -m "feat: add semantic analysis frontend contracts"

---

### Task 7: Source Range Selection and Layered Annotation Rendering

**Files:**
- Create: frontend/src/features/script-analysis/selectionOffsets.ts
- Create: frontend/src/features/script-analysis/selectionOffsets.test.ts
- Create: frontend/src/features/script-analysis/annotationView.ts
- Create: frontend/src/features/script-analysis/annotationView.test.ts
- Create: frontend/src/features/script-analysis/SourceAnnotationPane.tsx
- Create: frontend/src/features/script-analysis/SelectionAnnotationMenu.tsx
- Create: frontend/src/features/script-analysis/SourceAnnotationPane.test.tsx
- Create: frontend/src/features/script-analysis/script-analysis.css

**Interfaces:**
- Consumes: SourceSpan and SemanticAnnotation
- Produces: domRangeToSourceSpan(root, range, sourceRevision) -> SourceSpan
- Produces: splitAnnotatedText(source, annotations) -> AnnotationTextSegment[]
- Produces: SourceAnnotationPane callbacks onCreateAnnotation, onSelectAnnotation

- [ ] **Step 1: Write failing pure offset and segmentation tests**

    it("converts a DOM range across text nodes to UTF-16 source offsets", () => {
      const root = renderSource("甲😀台词");
      const range = selectText(root, "😀台");
      expect(domRangeToOffsets(root, range)).toEqual({ startUtf16: 1, endUtf16: 4 });
    });

    it("splits overlapping annotations without losing identity", () => {
      const segments = splitAnnotatedText("角色（惊喜）：台词", [speaker, emotion, dialogue]);
      expect(segments.map((item) => item.text).join("")).toBe("角色（惊喜）：台词");
      expect(segments.some((item) => item.annotationIds.length > 1)).toBe(true);
    });

- [ ] **Step 2: Run pure tests and verify failure**

Run:

    pnpm --dir frontend exec vitest run src/features/script-analysis/selectionOffsets.test.ts src/features/script-analysis/annotationView.test.ts

Expected: FAIL because helpers do not exist.

- [ ] **Step 3: Implement pure UTF-16 selection and segmentation**

Walk only text nodes under the source root. Reject ranges outside the root, collapsed ranges, and selections that cannot map exactly. Build visual segments from the sorted unique set of zero, source length, and every annotation start/end. Each segment carries all covering annotation ids and kinds.

- [ ] **Step 4: Write failing component interaction tests**

Using the existing jsdom plus react-dom/client pattern:

    it("creates a dialogue annotation from the selected source range", async () => {
      const onCreate = vi.fn();
      const view = renderPane({ source: "胶布：快跑！", onCreateAnnotation: onCreate });
      selectVisibleText(view, "快跑！");
      click(view, "标记为台词");
      expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
        kind: "dialogue",
        span: expect.objectContaining({ text: "快跑！" })
      }));
    });

    it("lists all annotations when a layered segment is clicked", async () => {
      const view = renderPane({ source, annotations: overlappingAnnotations });
      clickLayeredSegment(view);
      expect(view.textContent).toContain("台词");
      expect(view.textContent).toContain("情感证据");
    });

- [ ] **Step 5: Implement source pane, selection menu, and accessible styles**

Render exact source whitespace with white-space: pre-wrap. Use a blue background for dialogue, purple underline for speaker, and amber wavy underline for emotion evidence. Pair colors with visible legend labels. Keep selection creation keyboard accessible and expose layered annotation choices as real buttons.

- [ ] **Step 6: Run tests and commit**

Run:

    pnpm --dir frontend exec vitest run src/features/script-analysis/selectionOffsets.test.ts src/features/script-analysis/annotationView.test.ts src/features/script-analysis/SourceAnnotationPane.test.tsx

Expected: PASS.

Commit only this task’s files:

    git add frontend/src/features/script-analysis/selectionOffsets.ts frontend/src/features/script-analysis/selectionOffsets.test.ts frontend/src/features/script-analysis/annotationView.ts frontend/src/features/script-analysis/annotationView.test.ts frontend/src/features/script-analysis/SourceAnnotationPane.tsx frontend/src/features/script-analysis/SelectionAnnotationMenu.tsx frontend/src/features/script-analysis/SourceAnnotationPane.test.tsx frontend/src/features/script-analysis/script-analysis.css
    git commit -m "feat: add source-grounded annotation pane"

---

### Task 8: Draft Controller, Results Pane, Aliases, and Persistent Errors

**Files:**
- Create: frontend/src/features/script-analysis/useAnalysisDraft.ts
- Create: frontend/src/features/script-analysis/useAnalysisDraft.test.ts
- Create: frontend/src/features/script-analysis/AnalysisResultsPane.tsx
- Create: frontend/src/features/script-analysis/CharacterAliasEditor.tsx
- Create: frontend/src/features/script-analysis/ScriptAnalysisWorkspace.tsx
- Create: frontend/src/features/script-analysis/ScriptAnalysisWorkspace.test.tsx
- Modify: frontend/src/features/script-analysis/script-analysis.css
- Modify: frontend/src/i18n.ts
- Modify: frontend/src/i18n.test.ts

**Interfaces:**
- Consumes: frontend semantic API, SourceAnnotationPane
- Produces: useAnalysisDraft(projectId, sourceRevision)
- Produces: ScriptAnalysisWorkspace props projectId, sourceRevision, onConfirmed, onCancel

- [ ] **Step 1: Write failing hook state-machine tests**

Use fake timers and mocked API functions:

    it("stops polling on failed and preserves the 422 panel", async () => {
      mockRunSequence([{ status: "running" }, failedRun(422, "trace-1")]);
      const controller = renderAnalysisHook();
      await advanceUntilTerminal();
      expect(controller.current.isRunning).toBe(false);
      expect(controller.current.error?.http_status).toBe(422);
      controller.unmount();
      expect(renderAnalysisHook().current.error?.trace_id).toBe("trace-1");
    });

    it("serializes draft patches with the latest expected version", async () => {
      const controller = renderAnalysisHook(completedDraft(3));
      controller.current.queueOperations([acceptUtterance("utterance-1")]);
      controller.current.queueOperations([assignCharacter("utterance-1", "char-1")]);
      await flushAutosave();
      expect(patchMock.mock.calls[0][1]).toBe(3);
      expect(patchMock.mock.calls[1][1]).toBe(4);
    });

- [ ] **Step 2: Write failing workspace behavior tests**

    it("filters pending and low-confidence utterances", () => {
      const view = renderWorkspace(mixedDraft);
      click(view, "待确认");
      expect(visibleUtteranceIds(view)).toEqual(["utterance-pending"]);
      click(view, "低置信度");
      expect(visibleUtteranceIds(view)).toEqual(["utterance-low"]);
    });

    it("confirms only the server summary and forwards the updated project", async () => {
      const onConfirmed = vi.fn();
      confirmMock.mockResolvedValue(confirmResult);
      const view = renderWorkspace(confirmableDraft, onConfirmed);
      click(view, "确认分析并进入配音");
      expect(view.textContent).toContain("将导入 1 条");
      confirmDialog(view);
      await waitFor(() => expect(onConfirmed).toHaveBeenCalledWith(confirmResult.project));
    });

- [ ] **Step 3: Run tests and verify failure**

Run:

    pnpm --dir frontend exec vitest run src/features/script-analysis/useAnalysisDraft.test.ts src/features/script-analysis/ScriptAnalysisWorkspace.test.tsx src/i18n.test.ts

Expected: FAIL because the hook and workspace do not exist and translation keys are missing.

- [ ] **Step 4: Implement the isolated controller**

The hook owns all semantic state. It must:

- create or attach to one run;
- poll at a bounded interval until completed, failed, or interrupted;
- fetch the draft after terminal state;
- queue command batches and send them serially with the latest version;
- surface 409 without overwriting local edits;
- persist dismissed run ids under tts-more:analysis-dismissed-runs;
- restore undismissed errors from AnalysisRun after reload;
- generate one idempotency key and reuse it for confirm retries.

Do not place the draft in App project state and do not invoke saveProject from this hook.

- [ ] **Step 5: Implement results, alias editor, and workspace**

Render utterances in source order. The result card displays speaker, exact dialogue, evidence, normalized emotion, inference badge, confidence, uncertainty, and review status. CharacterAliasEditor emits controlled DraftOperation arrays for accepted, rejected, renamed, merged, split, or reassigned candidates.

Confirm opens a summary based on current accepted assigned utterances. The server response is authoritative.

- [ ] **Step 6: Add complete bilingual translations and responsive feature CSS**

Add matching zh/en keys for stage navigation, upload, selection types, filters, statuses, warnings, errors, copy diagnostics, aliases, confirmation summary, empty result, and retry. The two-pane layout stacks below 860 px and all primary actions remain reachable without hover.

- [ ] **Step 7: Run tests and commit**

Run:

    pnpm --dir frontend exec vitest run src/features/script-analysis/useAnalysisDraft.test.ts src/features/script-analysis/ScriptAnalysisWorkspace.test.tsx src/i18n.test.ts

Expected: PASS.

Commit only this task’s files:

    git add frontend/src/features/script-analysis/useAnalysisDraft.ts frontend/src/features/script-analysis/useAnalysisDraft.test.ts frontend/src/features/script-analysis/AnalysisResultsPane.tsx frontend/src/features/script-analysis/CharacterAliasEditor.tsx frontend/src/features/script-analysis/ScriptAnalysisWorkspace.tsx frontend/src/features/script-analysis/ScriptAnalysisWorkspace.test.tsx frontend/src/features/script-analysis/script-analysis.css frontend/src/i18n.ts frontend/src/i18n.test.ts
    git commit -m "feat: add semantic analysis review workspace"

---

### Task 9: App Stage Gate, Script Upload Entry, and End-to-End Compatibility

**Files:**
- Modify: frontend/src/components/ScriptManagerModal.tsx
- Modify: frontend/src/components/ScriptManagerModal.test.ts
- Modify: frontend/src/App.tsx
- Modify: frontend/src/App.css
- Create: frontend/src/features/script-analysis/analysisFlow.test.tsx

**Interfaces:**
- Consumes: ScriptAnalysisWorkspace and fileInput utilities
- Produces: explicit analysis versus TTS stage in App
- Produces: confirmed project handoff that refreshes current TTS lines

- [ ] **Step 1: Write failing ScriptManager entry and flow tests**

    it("offers txt and md upload and starts semantic analysis instead of legacy parse", () => {
      const onAnalyze = vi.fn();
      const view = renderScriptManager({ onAnalyzeScript: onAnalyze });
      expect(fileInput(view).accept).toBe(".txt,.md,text/plain,text/markdown");
      click(view, "分析剧本");
      expect(onAnalyze).toHaveBeenCalledTimes(1);
    });

    it("moves from analysis to the existing TTS workbench only after confirm", async () => {
      const app = renderAppWithSemanticApi();
      startAnalysis(app, "胶布：快跑！");
      expect(app.querySelector(".script-analysis-workspace")).not.toBeNull();
      resolveAnalysisDraft(app);
      confirmAnalysis(app);
      await waitFor(() => expect(app.querySelector(".workbench-grid")).not.toBeNull());
      expect(app.textContent).toContain("快跑！");
    });

- [ ] **Step 2: Run tests and verify failure**

Run:

    pnpm --dir frontend exec vitest run src/components/ScriptManagerModal.test.ts src/features/script-analysis/analysisFlow.test.tsx

Expected: FAIL because the analysis entry and stage gate are absent.

- [ ] **Step 3: Add the script input and analysis entry**

Extend ScriptManagerModal with onAnalyzeScript and onScriptFileSelected controlled callbacks. Keep legacy parse code and endpoint available, but label the new primary action 分析剧本 / Analyze script and route it to the semantic stage. Reading a file replaces the source draft only after successful validation.

- [ ] **Step 4: Add the minimal App stage gate**

Store only:

    type WorkspaceStage = "tts" | "analysis";
    const [workspaceStage, setWorkspaceStage] = useState<WorkspaceStage>("tts");
    const [analysisSourceRevision, setAnalysisSourceRevision] = useState<ScriptRevision | null>(null);

Starting analysis saves an exact ScriptRevision without trim-based source mutation, sets the revision, and enters analysis. Confirmation applies the returned ScriptProject through the existing project-loading state path, selects its first line, clears selection/history state, refreshes summaries, and returns to tts. Cancelling returns to the script manager without changing active ParseRevision.

- [ ] **Step 5: Preserve old 422 behavior while switching to persistent run errors**

Keep the existing dirty-worktree parser error fixes intact for the legacy path. Semantic errors render from AnalysisRun inside the analysis workspace, stop its spinner, survive reload, and never get cleared by unrelated App renders.

- [ ] **Step 6: Run frontend feature tests and production build**

Run:

    pnpm --dir frontend test
    pnpm --dir frontend build

Expected: all tests pass and TypeScript/Vite build succeeds.

- [ ] **Step 7: Commit**

    git add frontend/src/components/ScriptManagerModal.tsx frontend/src/components/ScriptManagerModal.test.ts frontend/src/App.tsx frontend/src/App.css frontend/src/features/script-analysis/analysisFlow.test.tsx
    git commit -m "feat: gate TTS workbench behind semantic review"

---

### Task 10: Full Regression, Security Check, and Manual Service Handoff

**Files:**
- Modify only files required to fix failures discovered by the commands below
- Do not weaken or delete existing tests to make the suite pass

**Interfaces:**
- Consumes: complete backend and frontend feature
- Produces: verified local services at 127.0.0.1:8000 and 127.0.0.1:5173

- [ ] **Step 1: Run the complete backend suite**

Run:

    .\.venv\Scripts\python.exe -m pytest backend -q

Expected: PASS. If a pre-existing environment-only test is skipped by its existing marker, report the skip; do not hide a new failure.

- [ ] **Step 2: Run the complete frontend suite and build**

Run:

    pnpm --dir frontend test
    pnpm --dir frontend build

Expected: PASS.

- [ ] **Step 3: Run source and secret leakage checks**

Run:

    rg -n "Authorization|api_key|source_text|source_markdown|prompt|response_body|traceback" backend/app/semantic_logging.py backend/tests/test_semantic_logging.py

Inspect every hit. The logger API must reject or omit sensitive values; tests may contain marker strings only to prove they are absent from output.

- [ ] **Step 4: Verify the approved regression fixture through the API**

Use TestClient with a fake SemanticProvider response for the mixed fixture. Assert:

    accepted dialogue texts are exact source substrings
    utterance starts are monotonically increasing
    诸葛九九 and 九九 are one reviewable alias candidate
    a speaker evidence mismatch remains a warning
    confirmation imports only accepted assigned utterances

- [ ] **Step 5: Stop old dev processes and start fresh services**

Use the repository Stop-Dev.cmd and Start-Dev.cmd or their documented non-interactive equivalents. Verify:

    GET http://127.0.0.1:8000/api/health returns success
    http://127.0.0.1:5173 loads the workstation

Do not expose either service beyond localhost.

- [ ] **Step 6: Manual smoke test**

With the user’s configured provider:

1. Paste the provided messy script.
2. Start analysis and verify progress reaches a terminal state.
3. Verify speaker, emotion evidence, quoted dialogue, and unquoted dialogue highlights.
4. Add one missed annotation by free selection.
5. Confirm a partial result and verify the existing TTS line cards.
6. Trigger or replay a saved 422 and verify it remains visible with trace_id after reload.

- [ ] **Step 7: Final scoped commit if verification required fixes**

Stage only the verified fix hunks and their tests:

    git diff --check
    git status --short
    git commit -m "test: verify semantic analysis workbench"
