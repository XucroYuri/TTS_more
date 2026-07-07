export type EngineName = "gpt-sovits" | "indextts" | "cosyvoice" | "vibevoice" | "commercial";
export type ProviderType = "gpt-sovits" | "indextts" | "cosyvoice" | "vibevoice" | "openai" | "gemini" | "xai" | "volcengine" | "generic-http";
export type SourceProfile = "local_repo" | "local_endpoint" | "lan_endpoint" | "cloud_endpoint" | "api_placeholder";
export type CatalogProvider = "gpt-sovits" | "indextts" | "cosyvoice";
export type SetupState = "not_configured" | "repo_missing" | "repo_found" | "env_missing" | "endpoint_unreachable" | "partial" | "ready";
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
  state?: "disabled" | "blocked" | "partial" | "ready" | "running" | string;
  severity?: "neutral" | "attention" | "danger" | "ready" | string;
  port_reachable?: boolean;
  config_ok?: boolean;
  required_api_ok?: boolean;
  auth_ok?: boolean;
  can_start?: boolean;
  supervisor_state?: string;
  cli?: string;
  script?: string;
  key_configured?: boolean;
  source_profile?: SourceProfile | null;
  catalog_provider?: CatalogProvider | null;
  setup_state?: SetupState | null;
  repo_path?: string | null;
  repo_found?: boolean | null;
  repo_path_exists?: boolean | null;
  endpoint_reachable?: boolean;
  api_contract_ok?: boolean;
  loaded_signature?: string | null;
  verification_level?: string | null;
  last_load_error?: string | null;
}

export interface OpenSourceTTSCatalogItem {
  provider_type: CatalogProvider;
  display_name: string;
  clone_url: string;
  default_repo_path: string;
  resolved_default_repo_path?: string;
  default_base_url: string;
  default_ports: number[];
  api_contracts: string[];
  capabilities: string[];
  priority: number;
  resource_group: string;
  recommended_clone_command: string;
  start_hint: string;
}

export interface OpenSourceTTSDetectRequest {
  provider_type: CatalogProvider;
  repo_path?: string | null;
  base_url?: string | null;
  api_contract?: string | null;
}

export interface OpenSourceTTSDetectResponse {
  provider_type: CatalogProvider;
  repo_path?: string | null;
  repo_found: boolean;
  base_url?: string | null;
  endpoint_reachable: boolean;
  api_contract_ok: boolean;
  health: Record<string, unknown>;
  setup_state: SetupState;
  env_hint: string;
}

export interface OpenSourceTTSConfigureRequest {
  provider_type: CatalogProvider;
  service_id?: string | null;
  display_name?: string | null;
  source_profile: SourceProfile;
  repo_path?: string | null;
  base_url: string;
  api_contract?: string | null;
  network_scope?: "localhost" | "lan" | "public" | "commercial" | null;
  managed?: boolean;
  enabled?: boolean;
  resource_group?: string;
  capacity?: number;
  start_command?: string[];
  start_cwd?: string | null;
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
  updated_at?: string;
  script_revision_count?: number;
  parse_revision_count?: number;
  character_count?: number;
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
  avatar_path?: string | null;
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
  project_binding?: VoiceBinding | null;
  match_confidence?: number | null;
  match_status?: "matched" | "unmatched" | "ambiguous" | "manual" | null;
}

export interface ScriptLine {
  id: string;
  line_uid?: string;
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

export interface ScriptRevision {
  revision_id: string;
  source_markdown: string;
  parent_revision_id?: string | null;
  summary?: string;
  created_at: string;
}

export interface ParseRevision {
  revision_id: string;
  script_revision_id: string;
  parent_parse_revision_id?: string | null;
  provider: string;
  warnings: string[];
  project_characters: ProjectCharacter[];
  lines: ScriptLine[];
  created_at: string;
}

export interface ScriptProject {
  title: string;
  default_language: string;
  project_characters?: ProjectCharacter[];
  active_script_revision_id?: string | null;
  active_parse_revision_id?: string | null;
  script_revisions?: ScriptRevision[];
  parse_revisions?: ParseRevision[];
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
  line_uid?: string | null;
  script_revision_id?: string | null;
  parse_revision_id?: string | null;
  binding_snapshot?: Record<string, unknown> | null;
  requested_load_signature?: string | null;
  verified_load_signature?: string | null;
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

export interface ParserProviderTestResponse {
  ok: boolean;
  state: "ready" | "partial" | "blocked" | "disabled" | "needs_key";
  message: string;
  provider: string;
  model?: string;
  latency_ms?: number;
  content_preview?: string;
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
  sample_count?: number;
  has_training_data?: boolean;
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

export interface LogsReferenceAudioSample {
  sample_id: string;
  display_label: string;
  path: string;
  text: string;
  text_source: "sidecar" | "manual" | "none" | "name2text" | "audio_metadata" | string;
  character?: string;
  emotion?: string;
  remark?: string;
  prompt_lang?: string;
  source: "logs" | "refdir" | string;
  logs_name?: string;
}

export interface LogsReferenceAudioResponse {
  service_id?: string | null;
  logs_name: string;
  samples: LogsReferenceAudioSample[];
  diagnostics?: Array<{ status: string; path?: string; detail: string }>;
}

export interface GPTSoVITSModelCatalogResponse {
  models: RoleLibraryCandidate[];
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
  line_uid?: string | null;
  status: QueueItemStatus;
  progress: number;
  queue_position?: number | null;
  cluster_key: string;
  cluster_size?: number | null;
  cluster_position?: number | null;
  load_signature?: string | null;
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

export interface ServiceLoadState {
  service_id: string;
  loaded: boolean;
  loaded_signature?: string | null;
  verification_level?: string | null;
  updated_at?: string | null;
  last_error?: string | null;
  last_error_at?: string | null;
}

export interface GenerationPreflightItem {
  line_id: string;
  line_uid?: string | null;
  status: "ready" | "needs_user_action" | "blocked";
  selected_service_id?: string | null;
  load_signature?: string | null;
  current_loaded_signature?: string | null;
  load_state?: "loaded" | "switch_required" | "not_loaded" | "unresolved" | string | null;
  load_match?: boolean;
  verification_level?: string | null;
  last_load_error?: string | null;
  fallback_action?: { type: "start_service"; service_id: string } | null;
  reason?: string | null;
}

export interface GenerationPreflightResponse {
  status: "ready" | "needs_user_action" | "blocked";
  items: GenerationPreflightItem[];
}

export interface DemoValidationPlan {
  project_id: string;
  title: string;
  summary: {
    line_count: number;
    considered_line_count: number;
    runnable_line_count: number;
    blocked_line_count: number;
    task_count: number;
    repeats: number;
  };
  blocked_lines: Array<{ line_id: string; line_uid?: string | null; character_id: string; reason: string }>;
  tasks: GenerationTask[];
  preflight: GenerationPreflightResponse;
  clusters: Array<{ cluster_key: string; count: number; line_ids: string[] }>;
}
