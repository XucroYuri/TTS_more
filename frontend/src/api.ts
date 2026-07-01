import type { Character, GenerationJob, GenerationManifest, GenerationTask, ParsedDraft, ParserProvidersResponse, ParserProvidersSavePayload, ProjectCharactersResponse, ProjectCharacter, ProjectSummary, QueueStatus, ReferenceAudioGroup, RoleLibraryCandidate, RoleLibraryScanResponse, RuntimeMode, ScriptProject, ServiceActionResult, ServiceLogResponse, ServiceSettingsPayload, ServiceSettingsResponse, VoiceCandidates, WorkerHealth } from "./types";

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

export async function testService(serviceId: string): Promise<{ service_id: string; ready: boolean; health: Record<string, unknown> }> {
  return request(`/api/services/${encodeURIComponent(serviceId)}/test`, { method: "POST" });
}

export async function fetchServicesStatus(): Promise<{ services: WorkerHealth[]; hardware: Record<string, unknown> }> {
  return request("/api/services/status");
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

export async function stopService(serviceId: string): Promise<ServiceActionResult> {
  return request(`/api/services/${encodeURIComponent(serviceId)}/stop`, { method: "POST" });
}

export async function fetchServiceLogs(serviceId: string): Promise<ServiceLogResponse> {
  return request(`/api/services/${encodeURIComponent(serviceId)}/logs?lines=80`);
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

export async function fetchProjects(): Promise<{ projects: ProjectSummary[] }> {
  return request("/api/projects");
}

export async function fetchManifest(projectId: string): Promise<GenerationManifest> {
  return request(`/api/projects/${projectId}/manifest`);
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

export async function freezeProjectCharacter(projectId: string, projectCharacterId: string): Promise<{ project_character: ProjectCharacter }> {
  return request(`/api/projects/${projectId}/characters/${encodeURIComponent(projectCharacterId)}/freeze`, { method: "POST" });
}

export async function unfreezeProjectCharacter(projectId: string, projectCharacterId: string): Promise<{ project_character: ProjectCharacter }> {
  return request(`/api/projects/${projectId}/characters/${encodeURIComponent(projectCharacterId)}/unfreeze`, { method: "POST" });
}
