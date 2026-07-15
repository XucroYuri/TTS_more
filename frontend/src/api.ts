import type { CatalogProvider, Character, DemoValidationPlan, GenerationJob, GenerationManifest, GenerationPreflightResponse, GenerationTask, GPTSoVITSModelCatalogResponse, LocalPortableServicesResponse, LogsReferenceAudioResponse, OpenSourceTTSCatalogItem, OpenSourceTTSConfigureRequest, OpenSourceTTSDetectRequest, OpenSourceTTSDetectResponse, ParseRevision, ParsedDraft, ParserProviderDraft, ParserProviderTestResponse, ParserProvidersResponse, ParserProvidersSavePayload, PortableActionResponse, PortableActionStatusResponse, PortableDiscoveryResponse, PortableFolderSelectionResponse, PortableImportApplyResponse, PortableImportPlanResult, PortableOperationLogsResponse, PortableOperationResponse, PortableRegistrationRequest, PortableRegistrationResponse, PortableServiceAction, ProjectCharactersResponse, ProjectCharacter, ProjectSummary, QueueStatus, ReferenceAudioGroup, RoleLibraryCandidate, RoleLibraryScanResponse, RuntimeMode, ScriptProject, ScriptRevision, ServiceActionResult, ServiceLoadState, ServiceLogResponse, ServiceSettingsPayload, ServiceSettingsResponse, VoiceCandidates, WorkerHealth } from "./types";
import { validatePortableProxyUrl } from "./lib/portableProxy";

const jsonHeaders = { "Content-Type": "application/json" };

const TOKEN_STORAGE_KEY = "tts_more_token";

/** Read the optional API token from localStorage (set when the backend has
 * TTS_MORE_API_TOKEN configured). Returns "" when unset. */
export function getApiToken(): string {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setApiToken(token: string): void {
  try {
    if (token) {
      localStorage.setItem(TOKEN_STORAGE_KEY, token);
    } else {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  } catch {
    /* ignore storage errors (private mode etc.) */
  }
}

export async function fetchAuthStatus(): Promise<{ auth_required: boolean }> {
  return request("/api/auth/status");
}

function withAuthHeader(init?: RequestInit): RequestInit {
  const token = getApiToken();
  if (!token) return init ?? {};
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${token}`);
  return { ...init, headers };
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, withAuthHeader(init));
  if (response.status === 401) {
    // Notify the UI to prompt for the API token. Listeners in App.tsx show
    // a token-entry dialog; the rejected promise still propagates the error.
    window.dispatchEvent(new CustomEvent("tts-more:auth-required"));
    const body = await response.text();
    throw new Error(body || "API token required");
  }
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || response.statusText);
  }
  return response.json() as Promise<T>;
}

export class PortableApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "PortableApiError";
    this.status = status;
    this.code = code;
  }
}

async function portableRequest<T>(url: string, init?: RequestInit, allowBlocked = false): Promise<T> {
  const response = await fetch(url, withAuthHeader(init));
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (response.status === 401) {
    window.dispatchEvent(new CustomEvent("tts-more:auth-required"));
  }
  if (!response.ok) {
    const detail = payload && typeof payload === "object" && "detail" in payload
      ? (payload as { detail?: unknown }).detail
      : null;
    const code = detail && typeof detail === "object" && "code" in detail && typeof detail.code === "string"
      ? detail.code
      : `HTTP_${response.status}`;
    const message = detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string"
      ? detail.message
      : response.statusText || "Portable service request failed";
    throw new PortableApiError(response.status, code, message);
  }
  if (!allowBlocked && payload && typeof payload === "object") {
    const semantic = payload as { status?: unknown; error_code?: unknown; reason?: unknown };
    if (semantic.status === "blocked" || typeof semantic.error_code === "string") {
      const code = typeof semantic.error_code === "string"
        ? semantic.error_code
        : "LOCAL_CONTROL_ACTION_FAILED";
      const message = typeof semantic.reason === "string"
        ? semantic.reason
        : "Portable service action failed";
      throw new PortableApiError(response.status, code, message);
    }
  }
  return payload as T;
}

function portableHeaders(token: string, includeJson = false): Headers {
  const headers = new Headers();
  headers.set("X-TTS-More-Control", token);
  if (includeJson) headers.set("Content-Type", "application/json");
  return headers;
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

export async function fetchServicesStatus(signal?: AbortSignal): Promise<{ services: WorkerHealth[]; hardware: Record<string, unknown> }> {
  return request("/api/services/status", { signal });
}

export async function fetchLocalControlToken(signal?: AbortSignal): Promise<string> {
  const payload = await portableRequest<{ token: string }>("/api/local-control/token", {
    cache: "no-store",
    referrerPolicy: "no-referrer",
    signal,
  });
  if (!payload || typeof payload.token !== "string" || !payload.token) {
    throw new PortableApiError(502, "LOCAL_CONTROL_INVALID_RESPONSE", "The local control token response is invalid");
  }
  return payload.token;
}

export async function fetchLocalPortableServices(token: string, signal?: AbortSignal): Promise<LocalPortableServicesResponse> {
  return portableRequest("/api/local-portable-services", {
    headers: portableHeaders(token),
    signal,
  });
}

export async function discoverLocalPortableServices(token: string, roots: string[] = [], signal?: AbortSignal): Promise<PortableDiscoveryResponse> {
  return portableRequest("/api/local-portable-services/discover", {
    method: "POST",
    headers: portableHeaders(token, true),
    body: JSON.stringify({ roots }),
    signal,
  });
}

export async function selectLocalPortableFolder(
  component: CatalogProvider,
  token: string,
  packageId?: string,
  signal?: AbortSignal,
): Promise<PortableFolderSelectionResponse> {
  return portableRequest("/api/local-portable-services/select-folder", {
    method: "POST",
    headers: portableHeaders(token, true),
    body: JSON.stringify(packageId ? { component, package_id: packageId } : { component }),
    signal,
  });
}

export async function registerLocalPortableService(
  payload: PortableRegistrationRequest,
  token: string,
  signal?: AbortSignal,
): Promise<PortableRegistrationResponse> {
  const body: PortableRegistrationRequest = {
    component: payload.component,
    package_id: payload.package_id,
    path: payload.path,
    ...(payload.port_override === undefined ? {} : { port_override: payload.port_override }),
  };
  return portableRequest("/api/local-portable-services/register", {
    method: "POST",
    headers: portableHeaders(token, true),
    body: JSON.stringify(body),
    signal,
  });
}

export async function planLocalPortableImport(
  component: CatalogProvider,
  token: string,
  signal?: AbortSignal,
): Promise<PortableImportPlanResult> {
  return portableRequest(`/api/local-portable-services/${encodeURIComponent(component)}/imports/plan`, {
    method: "POST",
    headers: portableHeaders(token, true),
    body: JSON.stringify({}),
    signal,
  });
}

export async function applyLocalPortableImport(
  component: CatalogProvider,
  planId: string,
  planDigest: string,
  token: string,
  signal?: AbortSignal,
): Promise<PortableImportApplyResponse> {
  return portableRequest(
    `/api/local-portable-services/${encodeURIComponent(component)}/imports/${encodeURIComponent(planId)}/apply`,
    {
      method: "POST",
      headers: portableHeaders(token, true),
      body: JSON.stringify({ confirmed: true, plan_digest: planDigest }),
      signal,
    },
  );
}

export async function portableServiceAction(
  component: CatalogProvider,
  action: PortableServiceAction,
  token: string,
  options: { port_override?: number; proxy_url?: string } = {},
  signal?: AbortSignal,
): Promise<PortableActionResponse> {
  if (options.port_override !== undefined && action !== "start") {
    throw new RangeError("port_override is supported only by start");
  }
  if (options.proxy_url !== undefined && action !== "repair") {
    throw new RangeError("proxy_url is supported only by repair");
  }
  if (options.proxy_url !== undefined && !validatePortableProxyUrl(options.proxy_url)) {
    throw new RangeError("proxy_url must be a valid HTTP(S) proxy URL");
  }
  const body = {
    ...(options.port_override === undefined ? {} : { port_override: options.port_override }),
    ...(options.proxy_url === undefined ? {} : { proxy_url: options.proxy_url }),
  };
  const hasBody = Object.keys(body).length > 0;
  return portableRequest(`/api/local-portable-services/${encodeURIComponent(component)}/${action}`, {
    method: "POST",
    headers: portableHeaders(token, hasBody),
    ...(hasBody ? { body: JSON.stringify(body) } : {}),
    signal,
  });
}

export async function fetchPortableActionStatus(
  component: CatalogProvider,
  actionId: string,
  token: string,
  signal?: AbortSignal,
): Promise<PortableActionStatusResponse> {
  return portableRequest(
    `/api/local-portable-services/${encodeURIComponent(component)}/actions/${encodeURIComponent(actionId)}`,
    { headers: portableHeaders(token), signal },
  );
}

export async function fetchPortableOperation(
  component: CatalogProvider,
  operationId: string,
  token: string,
  signal?: AbortSignal,
): Promise<PortableOperationResponse> {
  return portableRequest(
    `/api/local-portable-services/${encodeURIComponent(component)}/operations/${encodeURIComponent(operationId)}`,
    { headers: portableHeaders(token), signal },
    true,
  );
}

export async function fetchPortableOperationLogs(
  component: CatalogProvider,
  operationId: string,
  token: string,
  afterSeq = 0,
  limit = 100,
  signal?: AbortSignal,
): Promise<PortableOperationLogsResponse> {
  if (!Number.isInteger(afterSeq) || afterSeq < 0) throw new RangeError("afterSeq must be a non-negative integer");
  if (!Number.isInteger(limit) || limit < 1 || limit > 500) throw new RangeError("limit must be between 1 and 500");
  const query = new URLSearchParams({ after_seq: String(afterSeq), limit: String(limit) });
  return portableRequest(
    `/api/local-portable-services/${encodeURIComponent(component)}/operations/${encodeURIComponent(operationId)}/logs?${query.toString()}`,
    { headers: portableHeaders(token), signal },
    true,
  );
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
