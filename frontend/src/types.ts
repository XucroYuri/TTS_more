export type EngineName = "gpt-sovits" | "indextts" | "vibevoice" | "commercial";
export type ProviderType = "gpt-sovits" | "indextts" | "vibevoice" | "openai" | "gemini" | "xai" | "volcengine" | "generic-http";
export type GenerationStatus = "queued" | "loading" | "running" | "finalizing" | "completed" | "failed" | "cancelled";

export interface WorkerHealth {
  service_id?: string;
  service_kind?: "tts" | "llm-parser";
  display_name?: string;
  engine: EngineName;
  provider_type?: ProviderType;
  api_contract?: string;
  ready: boolean;
  mode?: string;
  network_scope?: "localhost" | "lan" | "public" | "commercial";
  managed?: boolean;
  enabled?: boolean;
  poll_interval_seconds?: number;
  base_url?: string | null;
  resource_group?: string;
  priority?: number;
  capabilities?: string[];
  auth_profile?: Record<string, string>;
  default_params?: Record<string, unknown>;
  cost_policy?: Record<string, unknown>;
  health?: Record<string, unknown>;
  supervisor?: ServiceSupervisorStatus;
  cli?: string;
  script?: string;
  key_configured?: boolean;
}

export interface ServiceSupervisorStatus {
  service_id: string;
  manageable: boolean;
  running: boolean;
  record?: Record<string, unknown> | null;
}

export interface RuntimeMode {
  service_mode: string;
  data_root: string;
  runtime_root: string;
  services: ServiceSupervisorStatus[];
}

export interface ProjectSummary {
  project_id: string;
  title: string;
  default_language: string;
  line_count: number;
}

export interface ServiceActionResult {
  status: string;
  service_id?: string;
  pid?: number;
  reason?: string;
  log_path?: string;
}

export interface ServiceLogResponse {
  status: string;
  service_id: string;
  log_path?: string;
  lines: string[];
}

export interface VoiceBinding {
  binding_id: string;
  provider_type: ProviderType;
  service_id?: string | null;
  fallback_services: string[];
  capabilities: string[];
  config: Record<string, unknown>;
}

export interface VoiceProfile {
  id: string;
  name: string;
  engine: EngineName;
  service_id?: string | null;
  fallback_services: string[];
  bindings?: VoiceBinding[];
  config: Record<string, unknown>;
}

export interface ReferenceAudioSample {
  path: string;
  text?: string;
  text_source?: "sidecar" | "manual" | "none";
  duration_seconds?: number | null;
}

export interface Character {
  id: string;
  name: string;
  aliases: string[];
  nicknames?: string[];
  match_names?: string[];
  notes: string;
  tags?: string[];
  library_status?: "draft" | "partial" | "confirmed" | "archived";
  source_assets?: Record<string, unknown>;
  updated_at?: string;
  reference_audio_groups?: CharacterReferenceAudioGroup[];
  profiles?: VoiceProfile[];
  default_engine?: EngineName | null;
  default_profile?: string | null;
  fallback_profiles: string[];
}

export interface CharacterReferenceAudioGroup {
  id: string;
  name: string;
  paths: string[];
  copied_paths?: string[];
  samples?: ReferenceAudioSample[];
}

export type ProjectCharacterMode = "reference" | "snapshot";

export interface ProjectCharacter {
  project_character_id: string;
  name: string;
  library_character_id?: string | null;
  mode: ProjectCharacterMode;
  character_snapshot?: Character | null;
  match_confidence?: number | null;
  match_status?: "matched" | "unmatched" | "ambiguous" | "manual" | null;
}

export interface ScriptLine {
  id: string;
  character_id: string;
  text: string;
  note: string;
  language?: string | null;
  engine_override?: EngineName | null;
  profile_override?: string | null;
  binding_override?: string | null;
  service_override?: string | null;
  temporary_binding?: VoiceBinding | null;
}

export interface ScriptProject {
  title: string;
  default_language: string;
  project_characters?: ProjectCharacter[];
  lines: ScriptLine[];
}

export interface GenerationVersion {
  version_id: string;
  engine: EngineName;
  profile: string;
  service_id?: string | null;
  resource_group?: string | null;
  provider_type?: ProviderType | null;
  binding_id?: string | null;
  status: GenerationStatus;
  audio_path?: string | null;
  parameters?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  log_summary?: string;
  error?: string | null;
  created_at: string;
}

export interface LineHistory {
  line_id: string;
  versions: GenerationVersion[];
}

export interface GenerationManifest {
  project_id: string;
  lines: Record<string, LineHistory>;
}

export interface GenerationTask {
  line: ScriptLine;
  engine: EngineName;
  profile: string;
  service_id?: string | null;
  fallback_service_ids: string[];
  provider_type?: ProviderType | null;
  binding_id?: string | null;
  required_capabilities: string[];
  parameters: Record<string, unknown>;
}

export interface ParsedDraft {
  provider: string;
  characters: Character[];
  lines: ScriptLine[];
  warnings: string[];
}

export interface ParserProviderConfig {
  name: string;
  base_url: string;
  api_key_env: string;
  model: string;
  enabled: boolean;
  timeout_seconds: number;
  priority: number;
  key_configured: boolean;
}

export interface ParserProviderDraft extends ParserProviderConfig {
  api_key?: string;
}

export interface ParserProvidersResponse {
  providers: ParserProviderConfig[];
}

export interface ParserProvidersSavePayload {
  providers: Array<Omit<ParserProviderDraft, "key_configured">>;
}

export interface ReferenceAudioGroup {
  id: string;
  name: string;
  path: string;
  audio_count: number;
  samples: string[];
}

export interface RoleLibraryCandidate {
  id: string;
  name: string;
  aliases?: string[];
  logs_id?: string;
  logs_name?: string;
  service_id?: string | null;
  source?: "filesystem" | "gradio" | "merged" | string;
  recommended_gpt_weights_path?: string;
  recommended_sovits_weights_path?: string;
  recommended_ref_audio_path?: string;
  gpt_weights?: Array<{ name: string; path: string; score?: [number, number] }>;
  sovits_weights?: Array<{ name: string; path: string; score?: [number, number] }>;
  reference_audio_groups?: CharacterReferenceAudioGroup[];
}

export interface RoleLibraryScanResponse {
  candidates: RoleLibraryCandidate[];
  diagnostics?: Array<{ service_id?: string; status: string; detail: string }>;
}

export interface ProjectCharactersResponse {
  project_characters: ProjectCharacter[];
  characters: Character[];
}

export interface VoiceCandidates {
  ready: boolean;
  runtimes?: Record<string, { python: string; ready: boolean; missing_modules: string[]; error?: string | null }>;
  reference_audio: {
    path: string;
    exists: boolean;
    is_dir: boolean;
    groups: ReferenceAudioGroup[];
  };
  gpt_sovits: {
    gpt_weights: Array<{ name: string; path: string }>;
    sovits_weights: Array<{ name: string; path: string }>;
    diagnostics: Array<Record<string, string>>;
  };
  indextts: {
    reference_audio: ReferenceAudioGroup[];
    model: { path: string; ready: boolean; missing: string[] };
    diagnostics: Array<Record<string, string>>;
  };
}

export interface ServiceSettingsResponse {
  services: WorkerHealth[];
}

export interface ServiceSettingsPayload {
  services: Array<WorkerHealth & { secrets?: Record<string, string> }>;
}

export type QueueItemStatus = "queued" | "loading" | "running" | "finalizing" | "completed" | "failed" | "cancelled";

export interface GenerationQueueItem {
  task_id: string;
  line_id: string;
  status: QueueItemStatus;
  progress: number;
  queue_position?: number | null;
  cluster_key: string;
  service_id?: string | null;
  resource_group?: string | null;
  error?: string | null;
  version_id?: string | null;
}

export interface GenerationJob {
  job_id: string;
  project_id: string;
  status: QueueItemStatus;
  progress: number;
  items: GenerationQueueItem[];
  created_at: string;
  updated_at: string;
  error?: string | null;
}

export interface QueueStatus {
  jobs: GenerationJob[];
  queued: number;
  running: number;
}
