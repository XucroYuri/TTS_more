import type { Character, DemoValidationPlan, GenerationJob, GenerationManifest, GenerationPreflightResponse, GenerationTask, GPTSoVITSModelCatalogResponse, LogsReferenceAudioResponse, OpenSourceTTSCatalogItem, OpenSourceTTSConfigureRequest, OpenSourceTTSDetectRequest, OpenSourceTTSDetectResponse, ParseRevision, ParsedDraft, ParserProviderDraft, ParserProviderTestResponse, ParserProvidersResponse, ParserProvidersSavePayload, ProjectCharactersResponse, ProjectCharacter, ProjectSummary, QueueStatus, ReferenceAudioGroup, RoleLibraryCandidate, RoleLibraryScanResponse, RuntimeMode, ScriptProject, ScriptRevision, ServiceActionResult, ServiceLoadState, ServiceLogResponse, ServiceSettingsPayload, ServiceSettingsResponse, VoiceCandidates, WorkerHealth } from "./types";

const jsonHeaders = { "Content-Type": "application/json" };

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || response.statusText);
  }
  return response.json() as Promise<T>;
}

export async function fetchHealth(): Promise<{ status: string; workers: WorkerHealth[] }> {
  return request("/api/health");
}

export async function fetchServices(): Promise<{ services: WorkerHealth[] }> {
  return request("/api/services");
}

export async function fetchServiceSettings(): Promise<ServiceSettingsResponse> {
  return request("/api/settings/services");
}

export async function saveServiceSettings(payload: ServiceSettingsPayload): Promise<ServiceSettingsResponse> {
  return request("/api/settings/services", {
    method: "PUT",
    headers: jsonHeaders,
    body: JSON.stringify(payload)
  });
}

export async function reloadServiceSettings(): Promise<ServiceSettingsResponse> {
  return request("/api/settings/services/reload", { method: "POST" });
}

export async function testService(serviceId: string): Promise<{ service_id: string; ready: boolean; health: Record<string, unknown> }> {
  return request(`/api/services/${encodeURIComponent(serviceId)}/test`, { method: "POST" });
}

export async function fetchServicesStatus(): Promise<{ services: WorkerHealth[]; hardware: Record<string, unknown> }> {
  return request("/api/services/status");
}

export async function fetchOpenSourceTTSCatalog(): Promise<{ providers: OpenSourceTTSCatalogItem[] }> {
  return request("/api/open-source-tts/catalog");
}

export async function detectOpenSourceTTS(payload: OpenSourceTTSDetectRequest): Promise<OpenSourceTTSDetectResponse> {
  return request("/api/open-source-tts/detect", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(payload)
  });
}

export async function configureOpenSourceTTS(payload: OpenSourceTTSConfigureRequest): Promise<{ service: WorkerHealth; detect: OpenSourceTTSDetectResponse; settings: ServiceSettingsResponse }> {
  return request("/api/open-source-tts/configure", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(payload)
  });
}

export async function fetchStartupChecks(): Promise<Record<string, unknown>> {
  return request("/api/startup/checks");
}

export async function fetchRuntimeMode(): Promise<RuntimeMode> {
  return request("/api/runtime/mode");
}

export async function startService(serviceId: string): Promise<ServiceActionResult> {
  return request(`/api/services/${encodeURIComponent(serviceId)}/start`, { method: "POST" });
}

export async function startAndWaitService(serviceId: string, timeoutSeconds = 45): Promise<ServiceActionResult & { health?: Record<string, unknown> }> {
  return request(`/api/services/${encodeURIComponent(serviceId)}/start-and-wait?timeout_seconds=${encodeURIComponent(String(timeoutSeconds))}`, { method: "POST" });
}

export async function stopService(serviceId: string): Promise<ServiceActionResult> {
  return request(`/api/services/${encodeURIComponent(serviceId)}/stop`, { method: "POST" });
}

export async function fetchServiceLogs(serviceId: string): Promise<ServiceLogResponse> {
  return request(`/api/services/${encodeURIComponent(serviceId)}/logs?lines=80`);
}

export async function fetchServiceLoadState(serviceId: string): Promise<ServiceLoadState> {
  return request(`/api/services/${encodeURIComponent(serviceId)}/load-state`);
}

export async function fetchVoiceCandidates(): Promise<VoiceCandidates> {
  return request("/api/resources/voice-candidates?limit=80");
}

export async function parseScript(text: string): Promise<ParsedDraft> {
  return request("/api/parse-script", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ text })
  });
}

export async function fetchParserProviders(): Promise<ParserProvidersResponse> {
  return request("/api/parser/providers");
}

export async function saveParserProviders(payload: ParserProvidersSavePayload): Promise<ParserProvidersResponse> {
  return request("/api/parser/providers", {
    method: "PUT",
    headers: jsonHeaders,
    body: JSON.stringify(payload)
  });
}

export async function testParserProvider(provider: Omit<ParserProviderDraft, "key_configured">): Promise<ParserProviderTestResponse> {
  return request("/api/parser/providers/test", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ provider })
  });
}

export async function saveProject(projectId: string, project: ScriptProject): Promise<void> {
  await request(`/api/projects/${projectId}`, {
    method: "PUT",
    headers: jsonHeaders,
    body: JSON.stringify(project)
  });
}

export async function fetchProject(projectId: string): Promise<ScriptProject> {
  return request(`/api/projects/${projectId}`);
}

export async function fetchScriptRevisions(projectId: string): Promise<{ script_revisions: ScriptRevision[]; parse_revisions: ParseRevision[]; active_script_revision_id?: string | null; active_parse_revision_id?: string | null }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/script-revisions`);
}

export async function createScriptRevision(projectId: string, sourceMarkdown: string, summary = ""): Promise<{ project: ScriptProject; script_revision: ScriptRevision }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/script-revisions`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ source_markdown: sourceMarkdown, summary })
  });
}

export async function createParseRevision(projectId: string, scriptRevisionId?: string | null): Promise<{ project: ScriptProject; parse_revision: ParseRevision }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/parse-revisions`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ script_revision_id: scriptRevisionId })
  });
}

export async function activateRevision(projectId: string, scriptRevisionId?: string | null, parseRevisionId?: string | null): Promise<{ project: ScriptProject }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/activate-revision`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ script_revision_id: scriptRevisionId, parse_revision_id: parseRevisionId })
  });
}

export async function fetchProjects(): Promise<{ projects: ProjectSummary[] }> {
  return request("/api/projects");
}

export async function deleteProject(projectId: string): Promise<{ status: string; project_id: string; trashed_path: string }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
}

export async function fetchManifest(projectId: string): Promise<GenerationManifest> {
  return request(`/api/projects/${projectId}/manifest`);
}

export async function deleteGenerationVersion(projectId: string, lineKey: string, versionId: string): Promise<{ status: string; audio_deleted: boolean; warning?: string | null }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/manifest/lines/${encodeURIComponent(lineKey)}/versions/${encodeURIComponent(versionId)}`, {
    method: "DELETE"
  });
}

export async function generateTasks(projectId: string, tasks: GenerationTask[]): Promise<GenerationManifest> {
  return request("/api/generate", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ project_id: projectId, tasks })
  });
}

export async function createGenerationJob(projectId: string, tasks: GenerationTask[]): Promise<GenerationJob> {
  return request("/api/jobs/generation", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ project_id: projectId, tasks })
  });
}

export async function generationPreflight(projectId: string, tasks: GenerationTask[]): Promise<GenerationPreflightResponse> {
  return request("/api/generation/preflight", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ project_id: projectId, tasks })
  });
}

export async function fetchGenerationJob(jobId: string): Promise<GenerationJob> {
  return request(`/api/jobs/${encodeURIComponent(jobId)}`);
}

export async function cancelGenerationJob(jobId: string): Promise<GenerationJob> {
  return request(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
}

export async function fetchQueueStatus(): Promise<QueueStatus> {
  return request("/api/queue/status");
}

export async function runRealValidation(projectId: string, tasks: GenerationTask[]): Promise<{ summary: { completed: number; failed: number; total: number }; manifest: GenerationManifest }> {
  return request("/api/validation/real-tts/run", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ project_id: projectId, tasks })
  });
}

export async function fetchDemoValidationPlan(projectId: string, limit = 30, repeats = 1): Promise<DemoValidationPlan> {
  const params = new URLSearchParams({ project_id: projectId, limit: String(limit), repeats: String(repeats) });
  return request(`/api/validation/demo-plan?${params.toString()}`);
}

export async function fetchReferenceAudio(): Promise<{ groups: ReferenceAudioGroup[] }> {
  return request("/api/reference-audio/scan?limit=40");
}

export async function uploadProjectReferenceAudio(projectId: string, file: File): Promise<{ sample: { path: string; text: string; text_source: "manual" | "sidecar" | "none" } }> {
  const form = new FormData();
  form.append("file", file);
  return request(`/api/projects/${encodeURIComponent(projectId)}/reference-audio/upload`, {
    method: "POST",
    body: form
  });
}

export async function uploadCharacterReferenceAudio(characterId: string, file: File): Promise<{ character: Character; sample: { path: string; text: string; text_source: "manual" | "sidecar" | "none" } }> {
  const form = new FormData();
  form.append("file", file);
  return request(`/api/characters/${encodeURIComponent(characterId)}/reference-audio/upload`, {
    method: "POST",
    body: form
  });
}

export async function uploadCharacterAvatar(characterId: string, file: File): Promise<{ character: Character }> {
  const form = new FormData();
  form.append("file", file);
  return request(`/api/characters/${encodeURIComponent(characterId)}/avatar/upload`, {
    method: "POST",
    body: form
  });
}

export async function saveCharacters(characters: Character[]): Promise<void> {
  await request("/api/characters", {
    method: "PUT",
    headers: jsonHeaders,
    body: JSON.stringify(characters)
  });
}

export async function fetchCharacters(): Promise<Character[]> {
  return request("/api/characters");
}

export async function fetchCharacterLibrary(): Promise<{ characters: Character[] }> {
  return request("/api/character-library");
}

export async function scanCharacterLibrary(limit = 80): Promise<RoleLibraryScanResponse> {
  return request("/api/character-library/scan", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ limit })
  });
}

export async function fetchLogsCandidates(serviceId?: string | null, includeGradio = true, limit = 80): Promise<RoleLibraryScanResponse> {
  const params = new URLSearchParams({ include_gradio: String(includeGradio), limit: String(limit) });
  if (serviceId) params.set("service_id", serviceId);
  return request(`/api/character-library/logs-candidates?${params.toString()}`);
}

export async function fetchLogsReferenceAudio(options: { serviceId?: string | null; logsName: string; gptWeightsPath?: string | null; sovitsWeightsPath?: string | null; limit?: number }): Promise<LogsReferenceAudioResponse> {
  const params = new URLSearchParams({
    logs_name: options.logsName,
    limit: String(options.limit ?? 120)
  });
  if (options.serviceId) params.set("service_id", options.serviceId);
  if (options.gptWeightsPath) params.set("gpt_weights_path", options.gptWeightsPath);
  if (options.sovitsWeightsPath) params.set("sovits_weights_path", options.sovitsWeightsPath);
  return request(`/api/character-library/logs-reference-audio?${params.toString()}`);
}

export async function fetchGptSovitsModelCatalog(serviceId?: string | null, limit = 120): Promise<GPTSoVITSModelCatalogResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (serviceId) params.set("service_id", serviceId);
  return request(`/api/model-catalog/gpt-sovits?${params.toString()}`);
}

export async function fetchGptSovitsModelSamples(options: { serviceId?: string | null; logsName: string; limit?: number }): Promise<LogsReferenceAudioResponse> {
  const params = new URLSearchParams({
    logs_name: options.logsName,
    limit: String(options.limit ?? 120)
  });
  if (options.serviceId) params.set("service_id", options.serviceId);
  return request(`/api/model-catalog/gpt-sovits/samples?${params.toString()}`);
}

export async function importRoleLibraryCandidate(candidate: RoleLibraryCandidate): Promise<{ character: Character }> {
  return request("/api/character-library/import", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ candidate })
  });
}

export async function deleteCharacterLibraryItem(characterId: string): Promise<{ status: string }> {
  return request(`/api/character-library/${encodeURIComponent(characterId)}`, { method: "DELETE" });
}

export async function fetchProjectCharacters(projectId: string): Promise<ProjectCharactersResponse> {
  return request(`/api/projects/${projectId}/characters`);
}

export async function saveProjectCharacters(projectId: string, projectCharacters: ProjectCharacter[]): Promise<{ project_characters: ProjectCharacter[] }> {
  return request(`/api/projects/${projectId}/characters`, {
    method: "PUT",
    headers: jsonHeaders,
    body: JSON.stringify({ project_characters: projectCharacters })
  });
}

export async function rematchProjectCharacters(projectId: string): Promise<ProjectCharactersResponse> {
  return request(`/api/projects/${projectId}/characters/rematch`, { method: "POST" });
}

export async function freezeProjectCharacter(projectId: string, projectCharacterId: string): Promise<{ project_character: ProjectCharacter }> {
  return request(`/api/projects/${projectId}/characters/${encodeURIComponent(projectCharacterId)}/freeze`, { method: "POST" });
}

export async function unfreezeProjectCharacter(projectId: string, projectCharacterId: string): Promise<{ project_character: ProjectCharacter }> {
  return request(`/api/projects/${projectId}/characters/${encodeURIComponent(projectCharacterId)}/unfreeze`, { method: "POST" });
}
