import {
  AlertCircle,
  Bot,
  CheckCircle2,
  ChevronDown,
  Cpu,
  FileText,
  FolderKanban,
  History,
  Languages,
  Library,
  Loader2,
  Mic2,
  Play,
  Plus,
  Power,
  RefreshCw,
  Search,
  Settings,
  SlidersHorizontal,
  Square,
  UserRound,
  Upload,
  Wand2,
  X
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  fetchCharacters,
  fetchProjectCharacters,
  fetchManifest,
  fetchParserProviders,
  fetchProject,
  fetchProjects,
  fetchReferenceAudio,
  fetchRuntimeMode,
  fetchServiceSettings,
  saveServiceSettings,
  fetchServiceLogs,
  fetchServices,
  fetchGenerationJob,
  fetchQueueStatus,
  fetchVoiceCandidates,
  fetchLogsCandidates,
  freezeProjectCharacter,
  createGenerationJob,
  importRoleLibraryCandidate,
  parseScript,
  runRealValidation,
  saveCharacters,
  saveParserProviders,
  scanCharacterLibrary,
  saveProject,
  startService,
  stopService,
  testService,
  unfreezeProjectCharacter,
  deleteCharacterLibraryItem,
  uploadProjectReferenceAudio
} from "./api";
import { defaultLanguage, languageOptions, nextLanguage, normalizeLanguage } from "./i18n";
import { ensureProjectCharacters, freezeProjectCharacterLocally, projectCharacterRows, resolveProjectCharacters } from "./lib/projectCharacters";
import { buildGenerationTask, lineBinding, lineEngine, lineProfile, lineServiceId } from "./lib/routing";
import { parserProviderKeyState, toParserProviderSavePayload } from "./lib/parserConfig";
import { projectToScriptSourceText } from "./lib/scriptSource";
import { summarizeLineHistory } from "./lib/status";
import { coreLocalProviders, filterScriptLines, isServiceOperational, lineStatus, serviceTopbarSummary, standardProjectName, toggleLineSelection, validationRunState, type LineStatusFilter } from "./lib/workstation";
import { initialCharacters, initialManifest, initialProject } from "./mockData";
import type {
  Character,
  GenerationManifest,
  ParsedDraft,
  ParserProviderDraft,
  ProjectCharacter,
  ProjectSummary,
  ReferenceAudioGroup,
  RoleLibraryCandidate,
  RuntimeMode,
  ScriptLine,
  ScriptProject,
  VoiceBinding,
  VoiceCandidates,
  VoiceProfile,
  WorkerHealth,
  GenerationJob,
  GenerationTask,
  ProviderType,
  QueueStatus
} from "./types";

const defaultProjectId = "demo";
type Translate = (key: string, options?: Record<string, unknown>) => string;
type SaveState = "idle" | "saving" | "saved" | "error";
type ServicePanelSection = "overview" | "tts" | "llm" | "resources" | "roles";
type ScriptSourceMode = "project" | "manual" | "draft";

function characterName(characters: Character[], id: string): string {
  return characters.find((character) => character.id === id)?.name ?? id;
}

export default function App() {
  const { t, i18n } = useTranslation();
  const [currentProjectId, setCurrentProjectId] = useState(defaultProjectId);
  const [projectSummaries, setProjectSummaries] = useState<ProjectSummary[]>([]);
  const [characters, setCharacters] = useState<Character[]>(initialCharacters);
  const [project, setProject] = useState<ScriptProject>(initialProject);
  const [manifest, setManifest] = useState<GenerationManifest>(initialManifest);
  const [services, setServices] = useState<WorkerHealth[]>([]);
  const [runtime, setRuntime] = useState<RuntimeMode | null>(null);
  const [voiceCandidates, setVoiceCandidates] = useState<VoiceCandidates | null>(null);
  const [referenceGroups, setReferenceGroups] = useState<ReferenceAudioGroup[]>([]);
  const [activeLineId, setActiveLineId] = useState(project.lines[1]?.id ?? project.lines[0]?.id ?? "");
  const [selectedLineIds, setSelectedLineIds] = useState<string[]>([]);
  const [scriptInput, setScriptInput] = useState(() => projectToScriptSourceText(initialProject, initialCharacters));
  const [scriptSourceMode, setScriptSourceMode] = useState<ScriptSourceMode>("project");
  const [draft, setDraft] = useState<ParsedDraft | null>(null);
  const [parserProviders, setParserProviders] = useState<ParserProviderDraft[]>([]);
  const [roleLibraryCandidates, setRoleLibraryCandidates] = useState<RoleLibraryCandidate[]>([]);
  const [isParsing, setIsParsing] = useState(false);
  const [isSavingParserConfig, setIsSavingParserConfig] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isRefreshingTopology, setIsRefreshingTopology] = useState(false);
  const [isValidating, setIsValidating] = useState(false);
  const [isSavingServiceConfig, setIsSavingServiceConfig] = useState(false);
  const [testingServiceId, setTestingServiceId] = useState<string | null>(null);
  const [isScanningRoleLibrary, setIsScanningRoleLibrary] = useState(false);
  const [isProjectMenuOpen, setIsProjectMenuOpen] = useState(false);
  const [isTopologyMenuOpen, setIsTopologyMenuOpen] = useState(false);
  const [servicePanelSection, setServicePanelSection] = useState<ServicePanelSection>("overview");
  const [isProjectLoaded, setIsProjectLoaded] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [lastSavedAt, setLastSavedAt] = useState<string | null>(null);
  const [expandedCharacterId, setExpandedCharacterId] = useState<string | null>(null);
  const [expandedServiceId, setExpandedServiceId] = useState<string | null>(null);
  const [expandedServiceConfigId, setExpandedServiceConfigId] = useState<string | null>(null);
  const [selectedParserProviderIndex, setSelectedParserProviderIndex] = useState(0);
  const [serviceLogs, setServiceLogs] = useState<Record<string, string[]>>({});
  const [serviceSecrets, setServiceSecrets] = useState<Record<string, Record<string, string>>>({});
  const [selectedLogsServiceId, setSelectedLogsServiceId] = useState<string>("");
  const [activeJob, setActiveJob] = useState<GenerationJob | null>(null);
  const [queueStatus, setQueueStatus] = useState<QueueStatus | null>(null);
  const [notice, setNotice] = useState(t("app.ready"));
  const [searchText, setSearchText] = useState("");
  const [roleLibrarySearch, setRoleLibrarySearch] = useState("");
  const [characterFilter, setCharacterFilter] = useState("all");
  const [providerFilter, setProviderFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState<LineStatusFilter>("all");

  useEffect(() => {
    setNotice(t("app.ready"));
    void refreshTopology();
    void refreshProjects();
    void refreshParserProviders();
    fetchReferenceAudio()
      .then((payload) => setReferenceGroups(payload.groups))
      .catch(() => setReferenceGroups([]));
    fetchCharacters()
      .then((payload) => {
        if (payload.length > 0) setCharacters(payload);
      })
      .catch(() => undefined);
  }, [t]);

  useEffect(() => {
    setIsProjectLoaded(false);
    setScriptSourceMode("project");
    setDraft(null);
    fetchProject(currentProjectId)
      .then((payload) => {
        setProject(payload);
        setActiveLineId(payload.lines[0]?.id ?? "");
        setIsProjectLoaded(true);
        return fetchProjectCharacters(currentProjectId)
          .then((projectCharactersPayload) => {
            setProject((current) => ({ ...current, project_characters: projectCharactersPayload.project_characters }));
          })
          .catch(() => undefined);
      })
      .catch(() => {
        setProject(initialProject);
        setActiveLineId(initialProject.lines[0]?.id ?? "");
        setScriptSourceMode("project");
        setDraft(null);
        setIsProjectLoaded(true);
      });
    fetchManifest(currentProjectId)
      .then(setManifest)
      .catch(() => setManifest({ project_id: currentProjectId, lines: {} }));
  }, [currentProjectId]);

  useEffect(() => {
    if (!isProjectLoaded) return;
    setSaveState("saving");
    const handle = window.setTimeout(() => {
      void saveCurrentProject();
    }, 700);
    return () => window.clearTimeout(handle);
  }, [characters, currentProjectId, isProjectLoaded, project]);

  useEffect(() => {
    if (selectedParserProviderIndex >= parserProviders.length) {
      setSelectedParserProviderIndex(Math.max(parserProviders.length - 1, 0));
    }
  }, [parserProviders.length, selectedParserProviderIndex]);

  const projectWithCharacters = useMemo<ScriptProject>(
    () => ({ ...project, project_characters: ensureProjectCharacters(project, characters) }),
    [characters, project]
  );
  const projectCharacters = projectWithCharacters.project_characters ?? [];
  const resolvedCharacters = useMemo(() => resolveProjectCharacters(projectWithCharacters, characters), [characters, projectWithCharacters]);
  const projectRoleRows = useMemo(() => projectCharacterRows(projectWithCharacters, characters), [characters, projectWithCharacters]);

  useEffect(() => {
    if (!isProjectLoaded || scriptSourceMode !== "project") return;
    setScriptInput(projectToScriptSourceText(projectWithCharacters, characters));
  }, [characters, isProjectLoaded, projectWithCharacters, scriptSourceMode]);
  const filteredLibraryCharacters = useMemo(() => {
    const query = roleLibrarySearch.trim().toLocaleLowerCase();
    if (!query) return characters;
    return characters.filter((character) => characterMatchValues(character).join(" ").toLocaleLowerCase().includes(query));
  }, [characters, roleLibrarySearch]);
  const filteredRoleCandidates = useMemo(() => {
    const query = roleLibrarySearch.trim().toLocaleLowerCase();
    if (!query) return roleLibraryCandidates;
    return roleLibraryCandidates.filter((candidate) => `${candidate.name} ${candidate.id} ${(candidate.aliases ?? []).join(" ")}`.toLocaleLowerCase().includes(query));
  }, [roleLibraryCandidates, roleLibrarySearch]);
  const activeLine = useMemo(() => project.lines.find((line) => line.id === activeLineId) ?? project.lines[0], [activeLineId, project.lines]);
  const activeSummary = useMemo(() => summarizeLineHistory(activeLine ? manifest.lines[activeLine.id] : undefined), [activeLine, manifest]);
  const activeBindings = useMemo(() => (activeLine ? bindingsForLine(activeLine, resolvedCharacters) : []), [activeLine, resolvedCharacters]);
  const activeBinding = useMemo(() => (activeLine ? lineBinding(activeLine, resolvedCharacters) : undefined), [activeLine, resolvedCharacters]);
  const activeProfiles = useMemo(() => (activeLine ? profilesForLine(activeLine, resolvedCharacters) : []), [activeLine, resolvedCharacters]);
  const expandedProjectCharacter = useMemo(() => projectCharacters.find((character) => character.project_character_id === expandedCharacterId), [expandedCharacterId, projectCharacters]);
  const expandedCharacter = useMemo(() => resolvedCharacters.find((character) => character.id === expandedCharacterId), [resolvedCharacters, expandedCharacterId]);
  const activeProvider: ProviderType = activeLine ? activeBinding?.provider_type ?? providerFromEngine(activeLine.engine_override) ?? "indextts" : "gpt-sovits";
  const activeBindingConfig = useMemo(() => activeBinding?.config ?? {}, [activeBinding]);
  const candidateReferenceGroups = useMemo(
    () => prioritizedReferenceGroups(voiceCandidates?.reference_audio.groups?.length ? voiceCandidates.reference_audio.groups : referenceGroups, activeLine, resolvedCharacters),
    [activeLine, referenceGroups, resolvedCharacters, voiceCandidates]
  );
  const validationState = useMemo(
    () => validationRunState(runtime, services, voiceCandidates, manifest, isValidating, isGenerating),
    [runtime, services, voiceCandidates, manifest, isValidating, isGenerating]
  );
  const validationSteps = useMemo(() => buildValidationSteps(runtime, services, voiceCandidates, manifest, t), [runtime, services, voiceCandidates, manifest, t]);
  const filteredLines = useMemo(
    () =>
      filterScriptLines(project.lines, manifest, {
        characterId: characterFilter,
        provider: providerFilter,
        status: statusFilter,
        search: searchText,
        providerForLine: (line) => lineBinding(line, resolvedCharacters)?.provider_type ?? "unassigned"
      }),
    [characterFilter, manifest, project.lines, providerFilter, resolvedCharacters, searchText, statusFilter]
  );
  const selectedLines = useMemo(() => project.lines.filter((line) => selectedLineIds.includes(line.id)), [project.lines, selectedLineIds]);
  const providerOptions = useMemo(() => Array.from(new Set(project.lines.map((line) => lineBinding(line, resolvedCharacters)?.provider_type ?? "unassigned"))), [project.lines, resolvedCharacters]);
  const selectedLanguage = normalizeLanguage(i18n.resolvedLanguage ?? i18n.language ?? defaultLanguage);
  const selectedLanguageLabel = languageOptions.find((option) => option.value === selectedLanguage)?.label ?? selectedLanguage;
  const displayProjectTitle = standardProjectName(project.title || currentProjectId);
  const scriptSourceTone = scriptSourceMode === "project" ? "completed" : scriptSourceMode === "draft" ? "running" : "queued";
  const scriptSourceLabel = t(`parser.source.${scriptSourceMode}`);
  const scriptSourceHint = t(`parser.sourceHint.${scriptSourceMode}`, { projectLines: project.lines.length, draftLines: draft?.lines.length ?? 0 });
  const projectRows = useMemo<ProjectSummary[]>(
    () =>
      projectSummaries.length > 0
        ? projectSummaries
        : [{ project_id: currentProjectId, title: standardProjectName(project.title || currentProjectId), default_language: project.default_language, line_count: project.lines.length }],
    [currentProjectId, project.default_language, project.lines.length, project.title, projectSummaries]
  );
  const visibleServices = useMemo(() => services.filter((service) => !isUnsupportedLocalVibeVoice(service)), [services]);
  const localServiceCount = useMemo(() => visibleServices.filter((service) => ["gpt-sovits", "indextts"].includes(service.provider_type ?? service.engine)), [visibleServices]);
  const paidServiceCount = useMemo(() => visibleServices.filter((service) => service.capabilities?.includes("paid_provider")), [visibleServices]);
  const serviceSummary = useMemo(() => serviceTopbarSummary(visibleServices, voiceCandidates, parserProviders), [parserProviders, visibleServices, voiceCandidates]);
  const selectedConfigService = useMemo(
    () => visibleServices.find((service) => service.service_id === expandedServiceConfigId) ?? visibleServices[0],
    [expandedServiceConfigId, visibleServices]
  );
  const runningServiceIds = useMemo(() => {
    const ids = new Set<string>();
    for (const item of activeJob?.items ?? []) {
      if (item.service_id && ["loading", "running", "finalizing"].includes(item.status)) ids.add(item.service_id);
    }
    return ids;
  }, [activeJob]);
  const servicePanelItems = useMemo(
    () => [
      { id: "overview" as const, label: t("services.panelOverview"), meta: topbarToneText(serviceSummary.overallTone, t) },
      { id: "tts" as const, label: t("services.panelTTS"), meta: `${visibleServices.filter((service) => service.service_kind !== "llm-parser").length}` },
      { id: "llm" as const, label: t("services.panelLLM"), meta: `${serviceSummary.parser.ready}/${serviceSummary.parser.total}` },
      { id: "resources" as const, label: t("services.panelResources"), meta: queueStatus ? `${queueStatus.running}/${queueStatus.queued}` : "-" },
      { id: "roles" as const, label: t("services.panelRoles"), meta: `${characters.length}` }
    ],
    [characters.length, queueStatus, serviceSummary, t, visibleServices]
  );
  const selectedParserProvider = parserProviders[selectedParserProviderIndex];
  const logsServiceOptions = useMemo(
    () => visibleServices.filter((service) => service.enabled !== false && service.api_contract === "gradio-gpt-sovits-webui" && service.service_id),
    [visibleServices]
  );

  async function refreshTopology() {
    setIsRefreshingTopology(true);
    try {
      const [servicePayload, settingsPayload, runtimePayload, candidatePayload, queuePayload] = await Promise.all([
        fetchServices().catch(() => ({ services: [] })),
        fetchServiceSettings().catch(() => ({ services: [] })),
        fetchRuntimeMode().catch(() => null),
        fetchVoiceCandidates().catch(() => null),
        fetchQueueStatus().catch(() => null)
      ]);
      setServices(mergeServiceRecords(settingsPayload.services, servicePayload.services).filter((service) => !isUnsupportedLocalVibeVoice(service)));
      setRuntime(runtimePayload);
      setVoiceCandidates(candidatePayload);
      setQueueStatus(queuePayload);
    } finally {
      setIsRefreshingTopology(false);
    }
  }

  async function refreshProjects() {
    try {
      const payload = await fetchProjects();
      setProjectSummaries(payload.projects);
    } catch {
      setProjectSummaries([]);
    }
  }

  async function refreshParserProviders() {
    try {
      const payload = await fetchParserProviders();
      setParserProviders(payload.providers.map((provider) => ({ ...provider, api_key: "" })));
    } catch {
      setParserProviders([]);
    }
  }

  async function handleParse() {
    setIsParsing(true);
    setNotice(t("parser.parsing"));
    try {
      const parsed = await parseScript(scriptInput);
      setDraft(parsed);
      setScriptSourceMode("draft");
      setNotice(t("parser.parsedBy", { provider: parsed.provider }));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("parser.parseFailed"));
    } finally {
      setIsParsing(false);
    }
  }

  function acceptDraft() {
    if (!draft) return;
    const nextProject = {
      title: project.title,
      default_language: project.default_language,
      project_characters: projectCharactersFromDraft(draft.characters, characters),
      lines: draft.lines
    };
    setProject(nextProject);
    setActiveLineId(draft.lines[0]?.id ?? "");
    setSelectedLineIds([]);
    setDraft(null);
    setScriptSourceMode("project");
    setScriptInput(projectToScriptSourceText(nextProject, characters));
    setNotice(t("parser.draftApplied"));
  }

  function updateScriptInput(value: string) {
    setScriptInput(value);
    setScriptSourceMode("manual");
    setDraft(null);
  }

  function updateParserProvider(index: number, patch: Partial<ParserProviderDraft>) {
    setParserProviders((current) => current.map((provider, itemIndex) => (itemIndex === index ? { ...provider, ...patch } : provider)));
  }

  function addParserProvider() {
    const next = parserProviders.length + 1;
    setParserProviders((current) => [
      ...current,
      {
        name: `openai-compatible-${next}`,
        base_url: "https://api.openai.com/v1",
        api_key_env: `PARSER_PROVIDER_${next}_API_KEY`,
        model: "gpt-4o-mini",
        enabled: true,
        timeout_seconds: 45,
        priority: 100 + next,
        key_configured: false,
        api_key: "",
      },
    ]);
  }

  async function saveParserProviderSettings() {
    setIsSavingParserConfig(true);
    try {
      const payload = await saveParserProviders(toParserProviderSavePayload(parserProviders));
      setParserProviders(payload.providers.map((provider) => ({ ...provider, api_key: "" })));
      setNotice(t("notice.parserConfigSaved"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.parserConfigFailed"));
    } finally {
      setIsSavingParserConfig(false);
    }
  }

  function updateServiceDraft(serviceId: string | undefined, patch: Partial<WorkerHealth>) {
    if (!serviceId) return;
    setServices((current) => current.map((service) => (service.service_id === serviceId ? { ...service, ...patch } : service)));
  }

  function updateServiceSecret(serviceId: string | undefined, envName: string, value: string) {
    if (!serviceId || !envName) return;
    setServiceSecrets((current) => ({
      ...current,
      [serviceId]: {
        ...(current[serviceId] ?? {}),
        [envName]: value
      }
    }));
  }

  async function saveServiceDirectorySettings() {
    setIsSavingServiceConfig(true);
    try {
      const payload = await saveServiceSettings({
        services: visibleServices.map((service) => ({
          ...service,
          secrets: service.service_id ? serviceSecrets[service.service_id] ?? {} : {}
        }))
      });
      setServices(payload.services);
      setServiceSecrets({});
      setNotice(t("notice.serviceConfigSaved"));
      await refreshTopology();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.serviceConfigFailed"));
    } finally {
      setIsSavingServiceConfig(false);
    }
  }

  async function testSelectedService(serviceId: string | undefined) {
    if (!serviceId) return;
    setTestingServiceId(serviceId);
    try {
      const result = await testService(serviceId);
      setNotice(result.ready ? t("notice.serviceTestReady") : t("notice.serviceTestFailed"));
      await refreshTopology();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.serviceTestFailed"));
    } finally {
      setTestingServiceId(null);
    }
  }

  async function saveCurrentProject() {
    try {
      await Promise.all([saveProject(currentProjectId, project), saveCharacters(characters)]);
      setSaveState("saved");
      setLastSavedAt(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
      setNotice(t("notice.autoSaved"));
      await refreshProjects();
    } catch (error) {
      setSaveState("error");
      setNotice(error instanceof Error ? error.message : t("notice.autoSaveFailed"));
    }
  }

  async function runQueue(lines = project.lines) {
    setIsGenerating(true);
    setNotice(t("notice.generating"));
    try {
      const { tasks, blocked } = buildRunnableTasks(lines, resolvedCharacters);
      if (blocked.length > 0) {
        setNotice(t("notice.linesNeedBinding", { count: blocked.length }));
      }
      if (tasks.length === 0) return;
      const job = await createGenerationJob(currentProjectId, tasks);
      setActiveJob(job);
      setNotice(t("notice.jobQueued", { job: job.job_id }));
      const finalJob = await pollGenerationJob(job.job_id);
      setActiveJob(finalJob);
      const nextManifest = await fetchManifest(currentProjectId);
      setManifest(nextManifest);
      setNotice(finalJob.status === "completed" ? t("notice.generated") : t("notice.generationFailed"));
      await refreshTopology();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.generationFailed"));
    } finally {
      setIsGenerating(false);
    }
  }

  async function runSelectedQueue() {
    await runQueue(selectedLines.length > 0 ? selectedLines : filteredLines);
  }

  async function pollGenerationJob(jobId: string): Promise<GenerationJob> {
    for (let attempt = 0; attempt < 240; attempt += 1) {
      const job = await fetchGenerationJob(jobId);
      setActiveJob(job);
      if (["completed", "failed", "cancelled"].includes(job.status)) return job;
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    throw new Error(t("notice.jobTimeout"));
  }

  function switchProject(projectId: string) {
    if (projectId === currentProjectId) return;
    setCurrentProjectId(projectId);
    setSelectedLineIds([]);
    setDraft(null);
    setIsProjectMenuOpen(false);
    setNotice(t("app.ready"));
  }

  async function cycleLanguage() {
    await i18n.changeLanguage(nextLanguage(selectedLanguage));
  }

  async function runValidation() {
    if (validationState.disabled) return;
    setIsValidating(true);
    setNotice(t("notice.validating"));
    try {
      const { tasks, blocked } = buildRunnableTasks(project.lines, resolvedCharacters);
      if (blocked.length > 0) {
        setNotice(t("notice.linesNeedBinding", { count: blocked.length }));
      }
      if (tasks.length === 0) return;
      const result = await runRealValidation("validation", tasks);
      setManifest(result.manifest);
      setNotice(t("notice.validationSummary", { completed: result.summary.completed, total: result.summary.total }));
      await refreshTopology();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.validationFailed"));
    } finally {
      setIsValidating(false);
    }
  }

  function playLine(lineId: string) {
    const latest = manifest.lines[lineId]?.versions.filter((version) => version.status === "completed" && version.audio_path).at(-1);
    if (!latest?.audio_path) {
      setNotice(t("empty.noPlayableVersion"));
      return;
    }
    const audio = new Audio(`/api/audio?path=${encodeURIComponent(latest.audio_path)}`);
    void audio.play();
  }

  function toggleVisibleSelection() {
    const visibleIds = filteredLines.map((line) => line.id);
    const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedLineIds.includes(id));
    setSelectedLineIds(allVisibleSelected ? selectedLineIds.filter((id) => !visibleIds.includes(id)) : Array.from(new Set([...selectedLineIds, ...visibleIds])));
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-row">
          <div className="brand-mark"><Mic2 size={17} /></div>
          <div>
            <h1>{t("app.title")}</h1>
            <span>{t("app.subtitle")}</span>
          </div>
        </div>

        <section className="panel compact parser-panel">
          <div className="parser-project-flow">
            <button className={`project-trigger sidebar-project-trigger ${isProjectMenuOpen ? "active" : ""}`} onClick={() => setIsProjectMenuOpen((open) => !open)}>
              <FolderKanban size={14} />
              <span>{t("app.project")}</span>
              <strong>{displayProjectTitle}</strong>
              <ChevronDown size={13} />
            </button>
            {isProjectMenuOpen && (
              <div className="project-popover inline-project-popover" role="dialog" aria-label={t("app.projectManager")}>
                <div className="popover-head project-popover-head">
                  <div>
                    <strong>{t("app.projectManager")}</strong>
                    <span>{t("app.projectCount", { count: projectRows.length })} · {t("app.autoSave")} {lastSavedAt ?? saveStateLabel(saveState, t)}</span>
                  </div>
                  <StatusPill tone={saveStateTone(saveState)} label={saveStateLabel(saveState, t)} />
                </div>
                <div className="project-list compact-project-list">
                  {projectRows.map((item) => (
                    <button className={`project-row ${item.project_id === currentProjectId ? "active" : ""}`} key={item.project_id} onClick={() => switchProject(item.project_id)}>
                      <span className="project-row-title">
                        <strong>{standardProjectName(item.title || item.project_id)}</strong>
                        {item.project_id === currentProjectId && <small>{t("app.currentProject")}</small>}
                      </span>
                      <small>{item.project_id} · {item.default_language} · {t("table.visibleLines", { count: item.line_count })}</small>
                    </button>
                  ))}
                </div>
                <div className="project-actions">
                  <button className="secondary-button" disabled title={t("app.newProject")}><Plus size={14} /> {t("app.newProject")}</button>
                  <button className="secondary-button" disabled title={t("app.importProject")}><FileText size={14} /> {t("app.importProject")}</button>
                </div>
              </div>
            )}
          </div>
          <div className="panel-title split-title">
            <span><Wand2 size={15} /> {t("parser.title")}</span>
            <StatusPill tone={scriptSourceTone} label={scriptSourceLabel} />
          </div>
          <p className="parser-source-hint">{scriptSourceHint}</p>
          <textarea className="script-input" value={scriptInput} onChange={(event) => updateScriptInput(event.target.value)} aria-label={t("parser.title")} />
          <div className="script-actions">
            <button className="secondary-button" onClick={handleParse} disabled={isParsing}>
              {isParsing ? <Loader2 className="spin" size={15} /> : <Wand2 size={15} />} {t("parser.parse")}
            </button>
            <button className="secondary-button" onClick={acceptDraft} disabled={!draft}>{t("parser.accept")}</button>
          </div>
        </section>

      </aside>

      <main className="workspace">
        <header className="topbar">
          <div className="toolbar">
            <span className="notice" title={notice}>{notice}</span>
            <div className="topbar-menu-wrap">
              <button
                className={`secondary-button menu-trigger service-status-trigger tone-${serviceSummary.overallTone} ${isTopologyMenuOpen ? "active" : ""}`}
                onClick={() => { setIsTopologyMenuOpen((open) => !open); setIsProjectMenuOpen(false); }}
                title={serviceTopbarTitle(serviceSummary, t)}
              >
                <Cpu size={15} />
                <span className="menu-trigger-label">{t("nav.serviceResources")}</span>
                <span className="service-trigger-metrics" aria-hidden="true">
                  <span className={`service-trigger-metric tone-${serviceSummary.local.tone}`}>
                    <span className="service-led" />
                    <span>{t("services.localShort")} {serviceSummary.local.ready}/{serviceSummary.local.total}</span>
                  </span>
                  <span className={`service-trigger-metric tone-${serviceSummary.paid.tone}`}>
                    <span className="service-led" />
                    <span>{t("services.apiShort")} {serviceSummary.paid.ready}/{serviceSummary.paid.total}</span>
                  </span>
                  <span className={`service-trigger-metric tone-${serviceSummary.parser.tone}`}>
                    <span className="service-led" />
                    <span>{t("services.parserShort")} {serviceSummary.parser.ready}/{serviceSummary.parser.total}</span>
                  </span>
                  <span className={`service-trigger-metric tone-${serviceSummary.resources.tone}`}>
                    <span className="service-led" />
                    <span>{t("services.resourcesShort")}</span>
                  </span>
                </span>
                <ChevronDown size={14} />
              </button>
              {isTopologyMenuOpen && (
                <div className="service-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setIsTopologyMenuOpen(false); }}>
                  <div className="service-modal" role="dialog" aria-modal="true" aria-label={t("nav.serviceResources")}>
                    <header className="service-modal-head">
                      <div>
                        <strong>{t("nav.serviceResources")}</strong>
                        <span>{runtime?.service_mode ? `${t("validation.mode")} ${statusText(runtime.service_mode, t)} · ${t("services.description")}` : t("status.needsSetup")}</span>
                      </div>
                      <div className="service-modal-actions">
                        <button className="icon-button small" onClick={() => void refreshTopology()} title={t("actions.refresh")}>
                          {isRefreshingTopology ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />}
                        </button>
                        <button className="icon-button small" onClick={() => setIsTopologyMenuOpen(false)} title={t("actions.close")}><X size={14} /></button>
                      </div>
                    </header>

                    <div className="service-modal-status-strip">
                      {validationSteps.map((step) => (
                        <span className={`modal-status-chip ${step.state}`} key={step.id}>
                          <small>{t(`validation.${step.id}`)}</small>
                          <strong>{step.label}</strong>
                        </span>
                      ))}
                      <button className="secondary-button compact-button" onClick={() => void runValidation()} disabled={validationState.disabled} title={validationReasonText(validationState, t)}>
                        {isValidating ? <Loader2 className="spin" size={13} /> : <Play size={13} />} {t("validation.run")}
                      </button>
                    </div>

                    <div className="service-modal-body">
                      <aside className="service-modal-nav">
                        {servicePanelItems.map((item) => (
                          <button
                            className={`service-nav-item ${servicePanelSection === item.id ? "active" : ""}`}
                            key={item.id}
                            onClick={() => setServicePanelSection(item.id)}
                          >
                            <span>{item.label}</span>
                            <strong>{item.meta}</strong>
                          </button>
                        ))}
                      </aside>

                      <section className="service-modal-content">
                        {servicePanelSection === "overview" && (
                          <div className="service-section-stack">
                            <div className="service-overview-grid">
                              <div className={`overview-card state-${serviceSummary.local.tone}`}>
                                <span>{t("services.localReady")}</span>
                                <strong>{serviceSummary.local.ready}/{localServiceCount.length}</strong>
                              </div>
                              <div className={`overview-card state-${serviceSummary.paid.tone}`}>
                                <span>{t("services.paidReady")}</span>
                                <strong>{serviceSummary.paid.ready}/{paidServiceCount.length}</strong>
                              </div>
                              <div className={`overview-card state-${serviceSummary.parser.tone}`}>
                                <span>{t("services.parserReady")}</span>
                                <strong>{serviceSummary.parser.ready}/{serviceSummary.parser.total}</strong>
                              </div>
                              <div className={`overview-card state-${voiceCandidates?.ready ? "ready" : "attention"}`}>
                                <span>{t("services.resourceReady")}</span>
                                <strong>{voiceCandidates?.ready ? t("status.ready") : t("status.needsMapping")}</strong>
                              </div>
                              <div className={`overview-card state-${queueStatus?.running ? "running" : "ready"}`}>
                                <span>{t("queue.title")}</span>
                                <strong>{queueStatus ? `${queueStatus.running}/${queueStatus.queued}` : "-"}</strong>
                              </div>
                            </div>
                            <div className="service-modal-card">
                              <div className="panel-title"><Bot size={15} /> {t("nav.serviceStatus")}</div>
                              <p className="section-help">{t("services.statusHint")}</p>
                              <div className="service-status-legend">
                                <span className="legend-dot ok">{t("services.legendReady")}</span>
                                <span className="legend-dot warn">{t("services.legendPartial")}</span>
                                <span className="legend-dot danger">{t("services.legendBlocked")}</span>
                                <span className="legend-dot running">{t("services.legendRunning")}</span>
                              </div>
                            </div>
                          </div>
                        )}

                        {servicePanelSection === "tts" && (
                          <div className="service-work-grid">
                            <section className="service-modal-card service-list-card">
                              <div className="panel-title"><Bot size={15} /> {t("services.panelTTS")}</div>
                              <div className="service-list menu-list service-management-list">
                                {visibleServices.map((worker) => {
                                  const tone = serviceOperationalTone(worker, runningServiceIds.has(worker.service_id ?? ""), runtime?.service_mode);
                                  return (
                                    <div className={`service-row ${selectedConfigService?.service_id === worker.service_id ? "selected" : ""} service-state-${tone}`} key={worker.service_id ?? worker.engine}>
                                      <span className={`dot ${tone}`} />
                                      <div className="service-main">
                                        <strong title={worker.service_id ?? worker.engine}>{serviceDisplayName(worker)}</strong>
                                        <span>{worker.service_id ?? worker.engine}</span>
                                        <div className="service-tracker">
                                          <span className={`tracker-chip ${tone}`}>{serviceOperationalLabel(worker, tone, t, runtime?.service_mode)}</span>
                                          <span className="tracker-chip">{serviceLifecycleText(worker, t)}</span>
                                          <span className="tracker-chip">{worker.resource_group ?? t("status.resource")}</span>
                                        </div>
                                      </div>
                                      <div className="service-actions">
                                        <button className="icon-button tiny" disabled={!worker.service_id || !worker.supervisor?.manageable || worker.supervisor.running} onClick={() => worker.service_id && void serviceAction(worker.service_id, "start")} title={t("actions.startService")}><Power size={13} /></button>
                                        <button className="icon-button tiny" disabled={!worker.service_id || !worker.supervisor?.running} onClick={() => worker.service_id && void serviceAction(worker.service_id, "stop")} title={t("actions.stopService")}><Square size={12} /></button>
                                        <button className="icon-button tiny" disabled={!worker.service_id} onClick={() => worker.service_id && void toggleLogs(worker.service_id)} title={t("actions.showLogs")}><FileText size={13} /></button>
                                        <button className="icon-button tiny" disabled={!worker.service_id} onClick={() => setExpandedServiceConfigId(worker.service_id ?? null)} title={t("actions.configureService")}><SlidersHorizontal size={13} /></button>
                                      </div>
                                      {expandedServiceId === worker.service_id && <pre className="service-log">{(serviceLogs[worker.service_id ?? ""] ?? [t("empty.noLogs")]).join("\n")}</pre>}
                                    </div>
                                  );
                                })}
                              </div>
                            </section>
                            <section className="service-modal-card service-config-card">
                              <div className="panel-title"><SlidersHorizontal size={15} /> {t("nav.serviceConfig")}</div>
                              <p className="section-help">{t("services.configHint")}</p>
                              {selectedConfigService ? (
                                <div className="service-config selected-config">
                                  <div className="config-readonly"><span>{t("services.selectedService")}</span><strong>{selectedConfigService.service_id ?? "-"}</strong></div>
                                  <div className="config-readonly"><span>{t("services.health")}</span><strong>{serviceHealthText(selectedConfigService, t, runtime?.service_mode)} · {serviceLifecycleText(selectedConfigService, t)}</strong></div>
                                  <label>
                                    <span>{t("services.enabled")}</span>
                                    <select value={selectedConfigService.enabled === false ? "false" : "true"} onChange={(event) => updateServiceDraft(selectedConfigService.service_id, { enabled: event.target.value === "true" })}>
                                      <option value="true">{t("status.enabled")}</option>
                                      <option value="false">{t("status.disabled")}</option>
                                    </select>
                                  </label>
                                  <label>
                                    <span>{t("services.displayName")}</span>
                                    <input value={selectedConfigService.display_name ?? serviceDisplayName(selectedConfigService)} onChange={(event) => updateServiceDraft(selectedConfigService.service_id, { display_name: event.target.value })} />
                                  </label>
                                  <label className="wide">
                                    <span>{t("services.endpoint")}</span>
                                    <input value={selectedConfigService.base_url ?? ""} onChange={(event) => updateServiceDraft(selectedConfigService.service_id, { base_url: event.target.value })} placeholder="http://127.0.0.1:9880" />
                                  </label>
                                  <label>
                                    <span>{t("services.networkScope")}</span>
                                    <select value={selectedConfigService.network_scope ?? "localhost"} onChange={(event) => updateServiceDraft(selectedConfigService.service_id, { network_scope: event.target.value as WorkerHealth["network_scope"] })}>
                                      <option value="localhost">{t("services.scopeLocalhost")}</option>
                                      <option value="lan">{t("services.scopeLan")}</option>
                                      <option value="public">{t("services.scopePublic")}</option>
                                      <option value="commercial">{t("services.scopeCommercial")}</option>
                                    </select>
                                  </label>
                                  <label>
                                    <span>{t("services.resourceGroup")}</span>
                                    <input value={selectedConfigService.resource_group ?? ""} onChange={(event) => updateServiceDraft(selectedConfigService.service_id, { resource_group: event.target.value })} />
                                  </label>
                                  <label>
                                    <span>{t("services.priority")}</span>
                                    <input type="number" min={1} value={selectedConfigService.priority ?? 100} onChange={(event) => updateServiceDraft(selectedConfigService.service_id, { priority: Number(event.target.value) || 100 })} />
                                  </label>
                                  <label>
                                    <span>{t("services.pollInterval")}</span>
                                    <input type="number" min={1} max={300} value={selectedConfigService.poll_interval_seconds ?? 5} onChange={(event) => updateServiceDraft(selectedConfigService.service_id, { poll_interval_seconds: Number(event.target.value) || 5 })} />
                                  </label>
                                  <div className="config-readonly"><span>{t("services.provider")}</span><strong>{selectedConfigService.provider_type ?? selectedConfigService.engine}</strong></div>
                                  <div className="config-readonly"><span>{t("services.apiContract")}</span><strong>{selectedConfigService.api_contract ?? selectedConfigService.engine}</strong></div>
                                  <div className="config-readonly"><span>{t("services.authProfile")}</span><strong>{serviceAuthText(selectedConfigService, t)}</strong></div>
                                  <div className="config-readonly"><span>{t("services.costPolicy")}</span><strong>{summarizeConfigValue(selectedConfigService.cost_policy)}</strong></div>
                                  {serviceAuthEnvNames(selectedConfigService).map((envName) => (
                                    <label className="wide" key={envName}>
                                      <span>{envName} · {selectedConfigService.key_configured ? t("parser.keyConfigured") : t("parser.keyMissing")}</span>
                                      <input
                                        type="password"
                                        value={serviceSecrets[selectedConfigService.service_id ?? ""]?.[envName] ?? ""}
                                        onChange={(event) => updateServiceSecret(selectedConfigService.service_id, envName, event.target.value)}
                                        placeholder={selectedConfigService.key_configured ? t("parser.apiKeyPlaceholderConfigured") : t("parser.apiKeyPlaceholderMissing")}
                                      />
                                    </label>
                                  ))}
                                  <div className="wide config-readonly"><span>{t("services.capabilities")}</span><strong>{selectedConfigService.capabilities?.join(" / ") || "-"}</strong></div>
                                  <div className="wide config-readonly"><span>{t("services.defaultParams")}</span><strong>{summarizeConfigValue(selectedConfigService.default_params)}</strong></div>
                                  <div className="wide service-config-actions">
                                    <button className="secondary-button compact-button" onClick={() => void testSelectedService(selectedConfigService.service_id)} disabled={testingServiceId === selectedConfigService.service_id}>
                                      {testingServiceId === selectedConfigService.service_id ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />} {t("services.testEndpoint")}
                                    </button>
                                    <button className="primary-button compact-button" onClick={() => void saveServiceDirectorySettings()} disabled={isSavingServiceConfig}>
                                      {isSavingServiceConfig ? <Loader2 className="spin" size={14} /> : <CheckCircle2 size={14} />} {t("services.saveDirectory")}
                                    </button>
                                  </div>
                                </div>
                              ) : (
                                <div className="empty-row">{t("services.noService")}</div>
                              )}
                            </section>
                          </div>
                        )}

                        {servicePanelSection === "llm" && (
                          <div className="llm-work-grid">
                            <section className="service-modal-card llm-provider-card">
                              <div className="parser-config-head">
                                <div>
                                  <strong>{t("parser.llmProviders")}</strong>
                                  <span>{t("parser.providerHint")}</span>
                                </div>
                                <button className="secondary-button compact-button" onClick={addParserProvider}><Plus size={13} /> {t("parser.addProvider")}</button>
                              </div>
                              <div className="parser-provider-list llm-provider-list">
                                {parserProviders.map((provider, index) => {
                                  const keyState = parserProviderKeyState(provider);
                                  const tone = parserProviderTone(provider);
                                  return (
                                    <button
                                      className={`parser-provider-summary service-state-${tone} ${selectedParserProviderIndex === index ? "selected" : ""}`}
                                      key={`${provider.name}-${index}`}
                                      onClick={() => setSelectedParserProviderIndex(index)}
                                      type="button"
                                    >
                                      <span className={`dot ${tone}`} />
                                      <span className="parser-provider-main">
                                        <strong>{provider.name || t("parser.providerName")}</strong>
                                        <small>{provider.model || t("status.unset")} · {provider.base_url || t("services.endpointMissing")}</small>
                                        <span className="service-tracker">
                                          <span className={`tracker-chip ${tone}`}>{provider.enabled ? t("status.enabled") : t("status.disabled")}</span>
                                          <span className={`tracker-chip ${keyState === "configured" ? "ok" : "warn"}`}>{t(keyState === "configured" ? "parser.keyConfigured" : "parser.keyMissing")}</span>
                                        </span>
                                      </span>
                                      <strong className="priority-badge">{provider.priority}</strong>
                                    </button>
                                  );
                                })}
                                {parserProviders.length === 0 && <div className="empty-row">{t("empty.noParserProviders")}</div>}
                              </div>
                              <div className="parser-config-actions">
                                <span>{t("parser.providerCount", { count: parserProviders.length })} · {t("parser.openAIProtocol")}</span>
                              </div>
                            </section>

                            <section className="service-modal-card llm-detail-card">
                              <div className="panel-title"><SlidersHorizontal size={15} /> {t("parser.providerDetails")}</div>
                              <p className="section-help">{t("parser.providerDetailHint")}</p>
                              {selectedParserProvider ? (
                                <div className="service-config parser-detail-form">
                                  <label className="parser-toggle wide">
                                    <input type="checkbox" checked={selectedParserProvider.enabled} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { enabled: event.target.checked })} />
                                    <span>{t("parser.enabled")}</span>
                                  </label>
                                  <label>
                                    <span>{t("parser.providerName")}</span>
                                    <input value={selectedParserProvider.name} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { name: event.target.value })} />
                                  </label>
                                  <label>
                                    <span>{t("parser.model")}</span>
                                    <input value={selectedParserProvider.model} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { model: event.target.value })} placeholder="gpt-4o-mini" />
                                  </label>
                                  <label className="wide">
                                    <span>{t("parser.baseUrl")}</span>
                                    <input value={selectedParserProvider.base_url} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { base_url: event.target.value })} placeholder="https://api.openai.com/v1" />
                                  </label>
                                  <label>
                                    <span>{t("parser.apiKeyEnv")}</span>
                                    <input value={selectedParserProvider.api_key_env} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { api_key_env: event.target.value })} placeholder="OPENAI_API_KEY" />
                                  </label>
                                  <label>
                                    <span>{t("parser.apiKey")} · {t(parserProviderKeyState(selectedParserProvider) === "configured" ? "parser.keyConfigured" : "parser.keyMissing")}</span>
                                    <input
                                      type="password"
                                      value={selectedParserProvider.api_key ?? ""}
                                      onChange={(event) => updateParserProvider(selectedParserProviderIndex, { api_key: event.target.value })}
                                      placeholder={t(parserProviderKeyState(selectedParserProvider) === "configured" ? "parser.apiKeyPlaceholderConfigured" : "parser.apiKeyPlaceholderMissing")}
                                    />
                                  </label>
                                  <label>
                                    <span>{t("parser.timeout")}</span>
                                    <input type="number" min={5} max={300} value={selectedParserProvider.timeout_seconds} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { timeout_seconds: Number(event.target.value) || 45 })} />
                                  </label>
                                  <label>
                                    <span>{t("parser.priority")}</span>
                                    <input type="number" min={1} value={selectedParserProvider.priority} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { priority: Number(event.target.value) || 100 })} />
                                  </label>
                                  <div className="wide config-readonly">
                                    <span>{t("parser.openAIProtocol")}</span>
                                    <strong>Chat Completions · JSON draft parser · rule fallback</strong>
                                  </div>
                                  <div className="wide service-config-actions">
                                    <button className="primary-button" onClick={() => void saveParserProviderSettings()} disabled={isSavingParserConfig}>
                                      {isSavingParserConfig ? <Loader2 className="spin" size={14} /> : <CheckCircle2 size={14} />} {t("parser.saveProviders")}
                                    </button>
                                  </div>
                                </div>
                              ) : (
                                <div className="empty-row">{t("empty.noParserProviders")}</div>
                              )}
                            </section>
                          </div>
                        )}

                        {servicePanelSection === "resources" && (
                          <div className="resource-dashboard">
                            <section className="service-modal-card resource-dashboard-card">
                              <div className="panel-title"><Cpu size={15} /> {t("services.resourceGroups")}</div>
                              <div className="resource-card-grid">
                                {resourceGroups(visibleServices).map((group) => {
                                  const tone = resourceGroupTone(group);
                                  const percent = group.total > 0 ? Math.round((group.ready / group.total) * 100) : 0;
                                  return (
                                    <div className={`resource-status-card service-state-${tone}`} key={group.name}>
                                      <div>
                                        <strong>{group.name === "unassigned" ? t("status.unassigned") : group.name}</strong>
                                        <span>{group.ready}/{group.total}</span>
                                      </div>
                                      <div className="resource-meter"><span style={{ width: `${percent}%` }} /></div>
                                      <small>{topbarToneText(tone === "ok" ? "ready" : tone === "warn" ? "attention" : "offline", t)}</small>
                                    </div>
                                  );
                                })}
                              </div>
                            </section>

                            <section className="service-modal-card resource-dashboard-card">
                              <div className="panel-title"><Library size={15} /> {t("services.modelAssets")}</div>
                              <div className="resource-asset-grid">
                                <div className="overview-card state-ready"><span>GPT</span><strong>{voiceCandidates?.gpt_sovits.gpt_weights.length ?? 0}</strong></div>
                                <div className="overview-card state-ready"><span>SoVITS</span><strong>{voiceCandidates?.gpt_sovits.sovits_weights.length ?? 0}</strong></div>
                                <div className={`overview-card state-${voiceCandidates?.indextts.model.ready ? "ready" : "attention"}`}><span>IndexTTS</span><strong>{voiceCandidates?.indextts.model.ready ? t("status.ready") : `${voiceCandidates?.indextts.model.missing.length ?? 0} ${t("status.missing")}`}</strong></div>
                                <div className={`overview-card state-${voiceCandidates?.ready ? "ready" : "attention"}`}><span>{t("services.resourceReady")}</span><strong>{voiceCandidates?.ready ? t("status.ready") : t("status.needsMapping")}</strong></div>
                              </div>
                            </section>

                            <section className="service-modal-card resource-dashboard-card">
                              <div className="panel-title"><History size={15} /> {t("queue.title")}</div>
                              <div className="queue-resource-strip">
                                <div><span>{t("filters.queued")}</span><strong>{queueStatus?.queued ?? 0}</strong></div>
                                <div><span>{t("filters.running")}</span><strong>{queueStatus?.running ?? 0}</strong></div>
                                <div><span>{t("status.completed")}</span><strong>{queueStatus?.jobs.filter((job) => job.status === "completed").length ?? 0}</strong></div>
                                <div><span>{t("status.failed")}</span><strong>{queueStatus?.jobs.filter((job) => job.status === "failed").length ?? 0}</strong></div>
                              </div>
                            </section>
                          </div>
                        )}

                        {servicePanelSection === "roles" && (
                          <div className="role-library-modal-grid">
                            <section className="service-modal-card role-library-control-card">
                              <div className="parser-config-head">
                                <div>
                                  <strong>{t("characters.libraryManager")}</strong>
                                  <span>{t("characters.libraryHint")}</span>
                                </div>
                                <button className="secondary-button compact-button" onClick={addEmptyLibraryCharacter}><Plus size={13} /> {t("characters.addRole")}</button>
                              </div>
                              <label className="search-field library-search">
                                <Search size={14} />
                                <input value={roleLibrarySearch} onChange={(event) => setRoleLibrarySearch(event.target.value)} placeholder={t("characters.searchLibrary")} />
                              </label>
                              <label className="library-service-select">
                                <span>{t("characters.logsService")}</span>
                                <select value={selectedLogsServiceId} onChange={(event) => setSelectedLogsServiceId(event.target.value)}>
                                  <option value="">{t("characters.allGptServices")}</option>
                                  {logsServiceOptions.map((service) => (
                                    <option value={service.service_id ?? ""} key={service.service_id}>{serviceDisplayName(service)}</option>
                                  ))}
                                </select>
                              </label>
                              <button className="secondary-button compact-button" onClick={() => void scanRoles()} disabled={isScanningRoleLibrary}>
                                {isScanningRoleLibrary ? <Loader2 className="spin" size={13} /> : <RefreshCw size={13} />} {t("characters.scanCandidates")}
                              </button>
                              <div className="role-library-stats">
                                <div className="overview-card state-ready"><span>{t("characters.confirmedLibrary")}</span><strong>{filteredLibraryCharacters.length}</strong></div>
                                <div className="overview-card state-attention"><span>{t("characters.scanDrafts")}</span><strong>{filteredRoleCandidates.length}</strong></div>
                                <div className="overview-card state-ready"><span>{t("characters.projectRoles")}</span><strong>{projectRoleRows.length}</strong></div>
                              </div>
                              <div className="project-role-list modal-project-role-list">
                                {projectRoleRows.map((role) => (
                                  <button className="role-row" key={role.id} onClick={() => { focusFirstLineForCharacter(role.id); setIsTopologyMenuOpen(false); }}>
                                    <span className="role-avatar" aria-hidden="true"><UserRound size={13} /></span>
                                    <span className="role-main">
                                      <strong>{role.name}</strong>
                                      <small>{t("characters.lines", { count: role.lineCount })} · {t(`characters.${role.mode}`)}</small>
                                    </span>
                                    <span className="role-route">
                                      <strong>{role.provider}</strong>
                                      <small>{role.profile}</small>
                                    </span>
                                    <ChevronDown size={13} />
                                  </button>
                                ))}
                              </div>
                            </section>

                            <section className="service-modal-card role-library-list-card">
                              <div className="panel-title"><UserRound size={15} /> {t("characters.confirmedLibrary")} · {filteredLibraryCharacters.length}</div>
                              <div className="library-list modal-library-list">
                                {filteredLibraryCharacters.map((character) => (
                                  <div className="library-row" key={character.id}>
                                    <span className="role-avatar" aria-hidden="true"><UserRound size={13} /></span>
                                    <div>
                                      <strong>{character.name}</strong>
                                      <small>{character.default_engine ?? t("status.unset")} · {character.profiles?.length ?? 0} {t("characters.bindings")}</small>
                                    </div>
                                    <button className="icon-button tiny" onClick={() => void removeLibraryCharacter(character.id)} title={t("characters.deleteRole")}><X size={13} /></button>
                                  </div>
                                ))}
                                {filteredLibraryCharacters.length === 0 && <div className="empty-row">{t("characters.noProjectRoles")}</div>}
                              </div>
                            </section>

                            <section className="service-modal-card role-library-list-card">
                              <div className="panel-title"><RefreshCw size={15} /> {t("characters.scanDrafts")} · {filteredRoleCandidates.length}</div>
                              <div className="library-list modal-library-list">
                                {filteredRoleCandidates.map((candidate) => (
                                  <div className="library-row candidate" key={candidate.id}>
                                    <span className="role-avatar" aria-hidden="true"><UserRound size={13} /></span>
                                    <div>
                                      <strong>{candidate.logs_name ?? candidate.name}</strong>
                                      <small>{candidate.service_id ?? t("services.localManaged")} · {candidate.source ?? "filesystem"}</small>
                                      <small>GPT {candidate.gpt_weights?.length ?? 0} · SoVITS {candidate.sovits_weights?.length ?? 0} · Ref {candidate.reference_audio_groups?.length ?? 0}</small>
                                      {candidate.recommended_gpt_weights_path && <small>{t("characters.defaultGpt")}: {shortPath(candidate.recommended_gpt_weights_path)}</small>}
                                      {candidate.recommended_sovits_weights_path && <small>{t("characters.defaultSovits")}: {shortPath(candidate.recommended_sovits_weights_path)}</small>}
                                    </div>
                                    <button className="secondary-button compact-button" onClick={() => void importCandidate(candidate)}>{t("characters.importCandidate")}</button>
                                  </div>
                                ))}
                                {roleLibraryCandidates.length === 0 && <div className="empty-row">{t("characters.noScanDrafts")}</div>}
                              </div>
                              <div className="parser-config-actions">
                                <span>{t("characters.scanCandidates")} · {t("characters.importCandidate")}</span>
                              </div>
                            </section>
                          </div>
                        )}
                      </section>
                    </div>
                  </div>
                </div>
              )}
            </div>
            <button className="language-select language-toggle" onClick={() => void cycleLanguage()} title={selectedLanguageLabel}>
              <Languages size={15} />
              <span>{selectedLanguageLabel}</span>
            </button>
          </div>
        </header>

        <section className="workbench-grid">
          <div className="lines-panel">
            <div className="role-strip">
              <button
                className="role-library-trigger"
                onClick={() => { setServicePanelSection("roles"); setIsTopologyMenuOpen(true); setIsProjectMenuOpen(false); }}
                title={t("characters.libraryManager")}
              >
                <Library size={14} />
                <span>{t("characters.library")}</span>
                <strong>{projectRoleRows.length}</strong>
                <ChevronDown size={13} />
              </button>
              <div className="role-avatar-row">
                {projectRoleRows.map((role) => {
                  const active = activeLine?.character_id === role.id;
                  const expanded = expandedCharacterId === role.id;
                  return (
                    <button className={`role-chip ${active ? "active" : ""} ${expanded ? "expanded" : ""}`} key={role.id} onClick={() => { focusFirstLineForCharacter(role.id); setExpandedCharacterId(expanded ? null : role.id); }}>
                      <span className="role-avatar" aria-hidden="true"><UserRound size={13} /></span>
                      <span className="role-chip-text">
                        <strong>{role.name}</strong>
                        <small>{t("characters.lines", { count: role.lineCount })} · {t(`characters.${role.mode}`)}</small>
                      </span>
                    </button>
                  );
                })}
              </div>
              {expandedCharacter && (
                <div className="role-popover">
                  <div className="role-popover-head">
                    <span className="role-avatar large" aria-hidden="true"><UserRound size={16} /></span>
                    <div>
                      <strong>{expandedCharacter.name}</strong>
                      <span>{expandedCharacter.default_engine ?? t("status.unset")} / {expandedCharacter.default_profile ?? t("status.unset")} · {expandedProjectCharacter ? t(`characters.${expandedProjectCharacter.mode}`) : t("status.unset")}</span>
                    </div>
                    <button className="icon-button tiny" onClick={() => setExpandedCharacterId(null)} title={t("actions.close")}><X size={13} /></button>
                  </div>
                  <div className="role-popover-grid">
                    <label>
                      <span>{t("characters.defaultProfile")}</span>
                      <select value={expandedCharacter.default_profile ?? ""} onChange={(event) => updateCharacterDefaultProfile(expandedCharacter.id, event.target.value)}>
                        <option value="">{t("status.unset")}</option>
                        {expandedCharacter.profiles?.map((profile) => <option value={profile.id} key={profile.id}>{profile.name}</option>)}
                      </select>
                    </label>
                    <div className="role-setting-grid compact">
                      <div><span>{t("characters.engine")}</span><strong>{expandedCharacter.default_engine ?? t("status.unset")}</strong></div>
                      <div><span>{t("characters.bindings")}</span><strong>{expandedCharacter.profiles?.length ?? 0}</strong></div>
                    </div>
                  </div>
                  <div className="voice-binding-list">
                    <span>{t("characters.voiceLock")}</span>
                    {expandedCharacter.profiles?.map((profile) => (
                      <button className={`voice-binding-button ${expandedCharacter.default_profile === profile.id ? "active" : ""}`} key={profile.id} onClick={() => updateCharacterDefaultProfile(expandedCharacter.id, profile.id)}>
                        <strong>{profile.name}</strong>
                        <small>{profile.engine} · {profile.bindings?.map((binding) => binding.provider_type).join(" / ") || t("status.unset")}</small>
                      </button>
                    ))}
                  </div>
                  {expandedCharacter.notes && <p className="role-notes">{expandedCharacter.notes}</p>}
                  <div className="role-popover-actions">
                    <button className="secondary-button compact-button" onClick={() => focusFirstLineForCharacter(expandedCharacter.id)}>{t("characters.focusLines")}</button>
                    {expandedProjectCharacter?.mode === "snapshot" ? (
                      <button className="secondary-button compact-button" onClick={() => void unfreezeRole(expandedProjectCharacter.project_character_id)}>{t("characters.unfreeze")}</button>
                    ) : (
                      <button className="primary-button compact-button" onClick={() => void freezeRole(expandedCharacter.id)}>{t("characters.freeze")}</button>
                    )}
                  </div>
                </div>
              )}
            </div>
            <div className="filters-row">
              <label className="search-field">
                <Search size={15} />
                <input value={searchText} onChange={(event) => setSearchText(event.target.value)} placeholder={t("filters.search")} />
              </label>
              <select value={characterFilter} onChange={(event) => setCharacterFilter(event.target.value)} aria-label={t("filters.character")}>
                <option value="all">{t("filters.all")}</option>
                    {projectRoleRows.map((role) => <option value={role.id} key={role.id}>{role.name}</option>)}
              </select>
              <select value={providerFilter} onChange={(event) => setProviderFilter(event.target.value)} aria-label={t("filters.provider")}>
                <option value="all">{t("filters.all")}</option>
                {providerOptions.map((provider) => <option value={provider} key={provider}>{provider}</option>)}
              </select>
              <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as LineStatusFilter)} aria-label={t("filters.status")}>
                <option value="all">{t("filters.all")}</option>
                <option value="not-generated">{t("filters.notGenerated")}</option>
                <option value="queued">{t("filters.queued")}</option>
                <option value="running">{t("filters.running")}</option>
                <option value="completed">{t("filters.completed")}</option>
                <option value="failed">{t("filters.failed")}</option>
              </select>
              <div className="task-actions">
                <span>{selectedLineIds.length > 0 ? t("table.selectedLines", { count: selectedLineIds.length }) : t("table.visibleLines", { count: filteredLines.length })}</span>
                <button className="primary-button" onClick={() => void runSelectedQueue()} disabled={isGenerating || filteredLines.length === 0}>
                  {isGenerating ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />} {selectedLineIds.length > 0 ? t("app.queueSelected") : t("app.queueFiltered")}
                </button>
              </div>
            </div>

            <div className="line-table">
              <div className="table-head">
                <button className="select-all" onClick={toggleVisibleSelection}>{t("table.select")}</button>
                <span>{t("table.line")}</span>
                <span>{t("table.character")}</span>
                <span>{t("table.note")}</span>
                <span>{t("table.provider")}</span>
                <span>{t("table.binding")}</span>
                <span>{t("table.service")}</span>
                <span>{t("table.status")}</span>
                <span>{t("table.actions")}</span>
              </div>
              {filteredLines.map((line) => {
                const summary = summarizeLineHistory(manifest.lines[line.id]);
                const queueItem = activeJob?.items.find((item) => item.line_id === line.id);
                const visibleTone = queueItem ? queueStatusTone(queueItem.status) : summary.tone;
                const visibleLabel = queueItem ? t(`status.${queueItem.status}`) : summaryLabel(summary, t);
                const selected = selectedLineIds.includes(line.id);
                const rowBinding = lineBinding(line, resolvedCharacters);
                const canGenerateLine = Boolean(rowBinding);
                return (
                  <div
                    className={`line-row ${activeLineId === line.id ? "active" : ""}`}
                    data-queue-state={queueItem?.status ?? summary.tone}
                    key={line.id}
                    onClick={() => setActiveLineId(line.id)}
                  >
                    <label className="line-check" onClick={(event) => event.stopPropagation()}>
                      <input type="checkbox" checked={selected} onChange={() => setSelectedLineIds((current) => toggleLineSelection(current, line.id))} />
                    </label>
                    <span className="line-id">{line.id}</span>
                    <span>{characterName(resolvedCharacters, line.character_id)}</span>
                    <span className="muted" title={line.note}>{line.note || "-"}</span>
                    <span>{rowBinding?.provider_type ?? t("status.unassigned")}</span>
                    <span className="muted" title={rowBinding?.binding_id ?? t("status.unassigned")}>{rowBinding?.binding_id ?? t("status.unassigned")}</span>
                    <span className="muted">{rowBinding ? lineServiceId(line, resolvedCharacters) ?? t("status.auto") : t("status.needsSetup")}</span>
                    <span><StatusPill tone={visibleTone} label={visibleLabel} /></span>
                    <span className="row-actions">
                      <button className="icon-button tiny" onClick={(event) => { event.stopPropagation(); playLine(line.id); }} title={t("actions.playLatest")}><Play size={14} /></button>
                      <button className="icon-button tiny" disabled={!canGenerateLine} onClick={(event) => { event.stopPropagation(); void runQueue([line]); }} title={canGenerateLine ? t("actions.regenerate") : t("inspector.needsTemporaryBinding")}><RefreshCw size={14} /></button>
                      <History size={14} />
                    </span>
                    {queueItem && <div className="line-progress"><span style={{ width: `${Math.round(queueItem.progress * 100)}%` }} /></div>}
                    <p className="line-text">{line.text}</p>
                  </div>
                );
              })}
              {filteredLines.length === 0 && <div className="empty-row table-empty">{t("empty.noLines")}</div>}
            </div>
          </div>

          <aside className="inspector">
            {activeLine && (
              <>
                <div className="inspector-head">
                  <div>
                    <div className="eyebrow">{t("inspector.title")}</div>
                    <h3>{characterName(resolvedCharacters, activeLine.character_id)}</h3>
                  </div>
                  <StatusPill tone={activeSummary.tone} label={summaryLabel(activeSummary, t)} />
                </div>
                <p className="dialogue">{activeLine.text}</p>
                <div className="field-grid">
                  <label>
                    <span>{t("inspector.provider")}</span>
                    <select value={activeProvider} onChange={(event) => setTemporaryBindingProvider(activeLine.id, event.target.value as ProviderType)}>
                      <option value="gpt-sovits">GPT-SoVITS</option>
                      <option value="indextts">IndexTTS</option>
                      <option value="openai">OpenAI</option>
                      <option value="gemini">Gemini</option>
                      <option value="xai">xAI</option>
                      <option value="volcengine">Volcengine</option>
                    </select>
                  </label>
                  <label>
                    <span>{t("inspector.profile")}</span>
                    <select value={lineProfile(activeLine, resolvedCharacters)} onChange={(event) => updateLine(activeLine.id, { profile_override: event.target.value, binding_override: null, service_override: null, engine_override: null })}>
                      {activeLine.temporary_binding && <option value={activeLine.temporary_binding.binding_id}>{t("inspector.temporaryBinding")}</option>}
                      {activeProfiles.map((profile) => <option value={profile.id} key={profile.id}>{profile.name}</option>)}
                      {activeProfiles.length === 0 && <option value={lineProfile(activeLine, resolvedCharacters)}>{lineProfile(activeLine, resolvedCharacters) || t("inspector.noProfile")}</option>}
                    </select>
                  </label>
                  <label className="wide">
                    <span>{t("inspector.voiceBinding")}</span>
                    <select value={activeLine.binding_override ?? ""} onChange={(event) => updateLine(activeLine.id, { binding_override: event.target.value || null, service_override: null })}>
                      {activeLine.temporary_binding && <option value="">{t("inspector.temporaryBinding")} · {activeLine.temporary_binding.provider_type}</option>}
                      {!activeLine.temporary_binding && <option value="">{t("inspector.profileDefault")}{activeBinding ? ` · ${activeBinding.provider_type}` : ""}</option>}
                      {activeBindings.map((binding) => <option value={binding.binding_id} key={binding.binding_id}>{binding.provider_type} · {binding.binding_id}</option>)}
                    </select>
                  </label>
                  <label className="wide">
                    <span>{t("inspector.service")}</span>
                    <select value={lineServiceId(activeLine, resolvedCharacters) ?? ""} onChange={(event) => updateLineService(activeLine.id, event.target.value || null)}>
                      <option value="">{t("inspector.autoRoute")}</option>
                      {servicesForProvider(visibleServices, activeProvider).map((service) => <option value={service.service_id} key={service.service_id ?? service.engine}>{service.display_name ?? service.service_id} · {service.resource_group ?? t("status.resource")}</option>)}
                    </select>
                  </label>
                  <label className="wide">
                    <span>{t("inspector.note")}</span>
                    <input value={activeLine.note} onChange={(event) => updateLine(activeLine.id, { note: event.target.value })} />
                  </label>
                </div>

                <div className="reference-panel">
                  <div className="panel-title"><Library size={15} /> {t("inspector.referenceResources")}</div>
                  {activeLine.temporary_binding && (
                    <div className="temporary-binding-banner">
                      <span>{t("inspector.temporaryBindingHint")}</span>
                      <button className="secondary-button compact-button" onClick={() => clearTemporaryBinding(activeLine.id)}>{t("inspector.clearTemporaryBinding")}</button>
                    </div>
                  )}
                  {!activeBinding && (
                    <div className="temporary-binding-banner attention">
                      <span>{t("inspector.needsTemporaryBinding")}</span>
                      <button className="secondary-button compact-button" onClick={() => setTemporaryBindingProvider(activeLine.id, "indextts")}>{t("inspector.createIndexTemporary")}</button>
                    </div>
                  )}
                  <div className="resource-context">
                    <div>
                      <span>{t("inspector.bindingConfig")}</span>
                      <strong>{activeBinding?.binding_id ?? lineProfile(activeLine, resolvedCharacters)}</strong>
                    </div>
                    <div>
                      <span>{t("inspector.provider")}</span>
                      <strong>{activeProvider}</strong>
                    </div>
                  </div>

                  {activeProvider === "gpt-sovits" && (
                    <>
                      <label className="resource-field">
                        <span>{t("inspector.gptWeights")}</span>
                        <select value={stringConfig(activeBindingConfig.gpt_weights_path)} onChange={(event) => updateActiveBindingConfig({ gpt_weights_path: event.target.value || undefined })}>
                          <option value="">{t("inspector.autoDefault")}</option>
                          {voiceCandidates?.gpt_sovits.gpt_weights.map((item) => <option value={item.path} key={item.path}>{item.name}</option>)}
                        </select>
                      </label>
                      <label className="resource-field">
                        <span>{t("inspector.sovitsWeights")}</span>
                        <select value={stringConfig(activeBindingConfig.sovits_weights_path)} onChange={(event) => updateActiveBindingConfig({ sovits_weights_path: event.target.value || undefined })}>
                          <option value="">{t("inspector.autoDefault")}</option>
                          {voiceCandidates?.gpt_sovits.sovits_weights.map((item) => <option value={item.path} key={item.path}>{item.name}</option>)}
                        </select>
                      </label>
                      <label className="resource-field">
                        <span>{t("inspector.promptText")}</span>
                        <input value={stringConfig(activeBindingConfig.prompt_text)} onChange={(event) => updateActiveBindingConfig({ prompt_text: event.target.value })} placeholder={t("inspector.promptPlaceholder")} />
                      </label>
                    </>
                  )}

                  {activeProvider === "indextts" && (
                    <div className="index-temporary-panel">
                      <label className="upload-field">
                        <span><Upload size={13} /> {t("inspector.uploadVoiceReference")}</span>
                        <input type="file" accept="audio/*" onChange={(event) => void uploadLineReference(event.currentTarget.files?.[0], "voice")} />
                      </label>
                      <label className="resource-field">
                        <span>{t("inspector.emotionMode")}</span>
                        <select value={stringConfig(activeBindingConfig.emotion_mode) || "same_as_voice"} onChange={(event) => updateActiveBindingConfig({ emotion_mode: event.target.value })}>
                          <option value="same_as_voice">{t("inspector.emotionSameAsVoice")}</option>
                          <option value="emotion_audio">{t("inspector.emotionAudio")}</option>
                          <option value="emotion_vector">{t("inspector.emotionVector")}</option>
                          <option value="emotion_text">{t("inspector.emotionText")}</option>
                        </select>
                      </label>
                      {(stringConfig(activeBindingConfig.emotion_mode) || "same_as_voice") === "emotion_text" && (
                        <label className="resource-field">
                          <span>{t("inspector.emotionText")}</span>
                          <input value={stringConfig(activeBindingConfig.emotion_text)} onChange={(event) => updateActiveBindingConfig({ emotion_text: event.target.value })} placeholder={activeLine.note || t("inspector.emotionTextPlaceholder")} />
                        </label>
                      )}
                      {(stringConfig(activeBindingConfig.emotion_mode) || "same_as_voice") === "emotion_audio" && (
                        <label className="upload-field">
                          <span><Upload size={13} /> {t("inspector.uploadEmotionReference")}</span>
                          <input type="file" accept="audio/*" onChange={(event) => void uploadLineReference(event.currentTarget.files?.[0], "emotion_audio")} />
                        </label>
                      )}
                      {(stringConfig(activeBindingConfig.emotion_mode) || "same_as_voice") === "emotion_vector" && (
                        <label className="resource-field">
                          <span>{t("inspector.emotionVector")}</span>
                          <input value={vectorConfig(activeBindingConfig.emotion_vector)} onChange={(event) => updateActiveBindingConfig({ emotion_vector: parseVectorConfig(event.target.value) })} placeholder="0,0,0,0,0,0,0,0" />
                        </label>
                      )}
                      <details className="advanced-params">
                        <summary>{t("inspector.advancedParams")}</summary>
                        <div className="advanced-grid">
                          {[
                            ["top_p", 0.8],
                            ["top_k", 30],
                            ["temperature", 0.8],
                            ["num_beams", 3],
                            ["repetition_penalty", 10],
                            ["max_mel_tokens", 1500]
                          ].map(([key, fallback]) => (
                            <label key={String(key)}>
                              <span>{String(key)}</span>
                              <input value={String(activeBindingConfig[String(key)] ?? fallback)} onChange={(event) => updateActiveBindingConfig({ [String(key)]: Number(event.target.value) })} />
                            </label>
                          ))}
                        </div>
                      </details>
                    </div>
                  )}

                  {activeProvider === "vibevoice" ? (
                    <div className="voice-source-summary">
                      <div className="empty-row">{t("inspector.legacyVibeVoice")}</div>
                    </div>
                  ) : activeProvider === "gpt-sovits" || activeProvider === "indextts" ? (
                    <div className="voice-source-summary">
                      <label className="resource-field">
                        <span>{t("inspector.referenceAudio")}</span>
                        <select value={referencePathForProvider(activeProvider, activeBindingConfig)} onChange={(event) => applyReferenceCandidate(event.target.value)}>
                          <option value="">{t("status.unset")}</option>
                          {candidateReferenceGroups.map((group) => (
                            <option value={group.samples[0] ?? ""} key={group.id} disabled={group.samples.length === 0}>
                              {group.name} · {group.audio_count}
                            </option>
                          ))}
                        </select>
                      </label>
                      <p className="resource-help">{t("inspector.voiceSourceHelp")}</p>
                      {candidateReferenceGroups.length === 0 && <div className="empty-row">{t("inspector.noReferenceCandidates")}</div>}
                    </div>
                  ) : (
                    <div className="empty-row">{t("inspector.commercialResourceHint")}</div>
                  )}
                </div>

                <div className="version-list">
                  <div className="panel-title"><History size={15} /> {t("inspector.versions")}</div>
                  {(manifest.lines[activeLine.id]?.versions ?? []).map((version) => (
                    <div className="version-row" key={version.version_id}>
                      {version.status === "completed" ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
                      <strong>{version.version_id}</strong>
                      <span>{version.provider_type ?? version.engine}</span>
                      <span>{version.binding_id ?? version.service_id ?? version.profile}</span>
                      {version.error && <small>{version.error}</small>}
                    </div>
                  ))}
                  {!manifest.lines[activeLine.id] && <div className="empty-row">{t("inspector.noVersions")}</div>}
                </div>
              </>
            )}
          </aside>
        </section>
      </main>
    </div>
  );

  function focusFirstLineForCharacter(characterId: string) {
    const next = project.lines.find((line) => line.character_id === characterId);
    if (next) setActiveLineId(next.id);
  }

  function updateLine(lineId: string, patch: Partial<ScriptLine>) {
    setProject((current) => ({
      ...current,
      lines: current.lines.map((line) => (line.id === lineId ? { ...line, ...patch } : line))
    }));
  }

  function updateCharacterDefaultProfile(characterId: string, profileId: string) {
    updateSourceCharacterForRole(characterId, (character) => {
        const profile = character.profiles?.find((item) => item.id === profileId);
        return {
          ...character,
          default_profile: profileId || null,
          default_engine: profile?.engine ?? character.default_engine ?? null
        };
    });
  }

  function updateActiveBindingConfig(patch: Record<string, unknown>) {
    if (!activeLine) return;
    if (activeLine.temporary_binding || !activeBinding) {
      upsertTemporaryBinding(activeLine.id, activeProvider, { configPatch: patch });
      return;
    }
    const sourceCharacter = resolvedCharacters.find((character) => character.id === activeLine.character_id);
    const profileId = activeLine.profile_override ?? sourceCharacter?.default_profile;
    updateSourceCharacterForRole(activeLine.character_id, (character) => {
        return {
          ...character,
          profiles: character.profiles?.map((profile) => {
            if (profile.id !== profileId) return profile;
            return {
              ...profile,
              bindings: profile.bindings?.map((binding) =>
                binding.binding_id === activeBinding.binding_id
                  ? { ...binding, config: compactConfig({ ...binding.config, ...patch }) }
                  : binding
              )
            };
          })
        };
    });
  }

  function setTemporaryBindingProvider(lineId: string, provider: ProviderType) {
    upsertTemporaryBinding(lineId, provider, { replaceProvider: true });
  }

  function clearTemporaryBinding(lineId: string) {
    updateLine(lineId, { temporary_binding: null, engine_override: null, profile_override: null, binding_override: null, service_override: null });
  }

  function updateLineService(lineId: string, serviceId: string | null) {
    const line = project.lines.find((item) => item.id === lineId);
    if (line?.temporary_binding) {
      upsertTemporaryBinding(lineId, line.temporary_binding.provider_type, { serviceId });
      return;
    }
    updateLine(lineId, { service_override: serviceId });
  }

  function upsertTemporaryBinding(
    lineId: string,
    provider: ProviderType,
    options: { configPatch?: Record<string, unknown>; serviceId?: string | null; replaceProvider?: boolean } = {}
  ) {
    setProject((current) => ({
      ...current,
      lines: current.lines.map((line) => {
        if (line.id !== lineId) return line;
        const existing = options.replaceProvider ? null : line.temporary_binding;
        const serviceId = options.serviceId !== undefined ? options.serviceId : existing?.service_id ?? defaultServiceForProvider(visibleServices, provider);
        const baseConfig = existing?.provider_type === provider ? existing.config : defaultTemporaryConfig(provider, line);
        return {
          ...line,
          engine_override: null,
          profile_override: null,
          binding_override: null,
          service_override: null,
          temporary_binding: {
            binding_id: existing?.binding_id && existing.provider_type === provider ? existing.binding_id : `line-temp-${provider}`,
            provider_type: provider,
            service_id: serviceId,
            fallback_services: existing?.provider_type === provider ? existing.fallback_services ?? [] : [],
            capabilities: defaultCapabilitiesForProvider(provider),
            config: compactConfig({ ...baseConfig, ...(options.configPatch ?? {}) })
          }
        };
      })
    }));
  }

  function updateSourceCharacterForRole(projectCharacterId: string, updater: (character: Character) => Character) {
    const mapping = projectCharacters.find((item) => item.project_character_id === projectCharacterId);
    if (mapping?.mode === "snapshot") {
      setProject((current) => ({
        ...current,
        project_characters: ensureProjectCharacters(current, characters).map((item) => {
          if (item.project_character_id !== projectCharacterId || !item.character_snapshot) return item;
          return { ...item, character_snapshot: updater(item.character_snapshot) };
        })
      }));
      return;
    }
    const libraryId = mapping?.library_character_id ?? projectCharacterId;
    setCharacters((current) => current.map((character) => (character.id === libraryId ? updater(character) : character)));
  }

  function updateProjectCharacter(nextProjectCharacter: ProjectCharacter) {
    setProject((current) => ({
      ...current,
      project_characters: ensureProjectCharacters(current, characters).map((item) =>
        item.project_character_id === nextProjectCharacter.project_character_id ? nextProjectCharacter : item
      )
    }));
  }

  async function freezeRole(projectCharacterId: string) {
    setNotice(t("notice.freezingRole"));
    try {
      const payload = await freezeProjectCharacter(currentProjectId, projectCharacterId);
      updateProjectCharacter(payload.project_character);
      setNotice(t("notice.roleFrozen"));
    } catch (error) {
      const mapping = projectCharacters.find((item) => item.project_character_id === projectCharacterId);
      if (mapping) {
        updateProjectCharacter(freezeProjectCharacterLocally(mapping, characters));
      }
      setNotice(error instanceof Error ? error.message : t("notice.roleFreezeFailed"));
    }
  }

  async function unfreezeRole(projectCharacterId: string) {
    setNotice(t("notice.unfreezingRole"));
    try {
      const payload = await unfreezeProjectCharacter(currentProjectId, projectCharacterId);
      updateProjectCharacter(payload.project_character);
      setNotice(t("notice.roleUnfrozen"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.roleFreezeFailed"));
    }
  }

  async function scanRoles() {
    setIsScanningRoleLibrary(true);
    setNotice(t("notice.scanningRoles"));
    try {
      const payload = await fetchLogsCandidates(selectedLogsServiceId || null, true, 80).catch(() => scanCharacterLibrary(80));
      setRoleLibraryCandidates(payload.candidates);
      const diagnostics = payload.diagnostics?.length ? ` · ${payload.diagnostics.length} ${t("characters.diagnostics")}` : "";
      setNotice(`${t("notice.roleScanDone", { count: payload.candidates.length })}${diagnostics}`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.roleScanFailed"));
    } finally {
      setIsScanningRoleLibrary(false);
    }
  }

  async function importCandidate(candidate: RoleLibraryCandidate) {
    setNotice(t("notice.importingRole"));
    try {
      const payload = await importRoleLibraryCandidate(candidate);
      setCharacters((current) => [...current.filter((character) => character.id !== payload.character.id), payload.character]);
      setRoleLibraryCandidates((current) => current.filter((item) => item.id !== candidate.id));
      setProject((current) => ({
        ...current,
        project_characters: ensureProjectCharacters(current, characters).map((item) =>
          normalizeRoleToken(item.name) === normalizeRoleToken(payload.character.name)
            ? { ...item, library_character_id: payload.character.id, mode: "reference", character_snapshot: null }
            : item
        )
      }));
      setNotice(t("notice.roleImported"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.roleImportFailed"));
    }
  }

  function addEmptyLibraryCharacter() {
    const id = `role-${Date.now().toString(36)}`;
    const character: Character = {
      id,
      name: t("characters.newRoleName"),
      aliases: [],
      notes: "",
      tags: ["manual"],
      library_status: "draft",
      fallback_profiles: [],
      profiles: []
    };
    setCharacters((current) => [...current, character]);
    setNotice(t("notice.roleAdded"));
  }

  async function removeLibraryCharacter(characterId: string) {
    try {
      await deleteCharacterLibraryItem(characterId);
      setCharacters((current) => current.filter((character) => character.id !== characterId));
      setNotice(t("notice.roleDeleted"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.roleDeleteFailed"));
    }
  }

  function applyReferenceCandidate(path: string | undefined) {
    const provider = activeProvider;
    if (provider === "indextts") {
      updateActiveBindingConfig({ voice: path || undefined });
    } else if (provider === "gpt-sovits") {
      updateActiveBindingConfig({ ref_audio_path: path || undefined });
    } else {
      updateActiveBindingConfig({ ref_audio_path: path || undefined });
    }
    setNotice(t("notice.referenceApplied"));
  }

  async function uploadLineReference(file: File | undefined, target: "voice" | "emotion_audio") {
    if (!file || !activeLine) return;
    setNotice(t("notice.uploadingReference"));
    try {
      const payload = await uploadProjectReferenceAudio(currentProjectId, file);
      updateActiveBindingConfig({ [target]: payload.sample.path });
      setNotice(t("notice.referenceUploaded"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.referenceUploadFailed"));
    }
  }

  function applyVibePreset(key: string) {
    updateActiveBindingConfig({ speaker_name: key || undefined });
    setNotice(t("notice.presetApplied"));
  }

  async function serviceAction(serviceId: string, action: "start" | "stop") {
    setNotice(t(action === "start" ? "actions.starting" : "actions.stopping", { service: serviceId }));
    try {
      const result = action === "start" ? await startService(serviceId) : await stopService(serviceId);
      setNotice(t("actions.serviceStatus", { service: serviceId, status: result.status }));
      await refreshTopology();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("actions.actionFailed"));
    }
  }

  async function toggleLogs(serviceId: string) {
    if (expandedServiceId === serviceId) {
      setExpandedServiceId(null);
      return;
    }
    setExpandedServiceId(serviceId);
    try {
      const payload = await fetchServiceLogs(serviceId);
      setServiceLogs((current) => ({ ...current, [serviceId]: payload.lines }));
    } catch (error) {
      setServiceLogs((current) => ({ ...current, [serviceId]: [error instanceof Error ? error.message : t("notice.logUnavailable")] }));
    }
  }
}

function projectCharactersFromDraft(draftCharacters: Character[], library: Character[]): ProjectCharacter[] {
  return draftCharacters.map((character) => {
    const match = library.find((item) =>
      characterMatchValues(item).some((value) => normalizeRoleToken(value) === normalizeRoleToken(character.name) || normalizeRoleToken(value) === normalizeRoleToken(character.id))
    );
    if (match) {
      return {
        project_character_id: character.id,
        name: character.name,
        library_character_id: match.id,
        mode: "reference",
        character_snapshot: null,
        match_confidence: 1,
        match_status: "matched"
      };
    }
    return {
      project_character_id: character.id,
      name: character.name,
      library_character_id: null,
      mode: "reference",
      character_snapshot: null,
      match_confidence: null,
      match_status: "unmatched"
    };
  });
}

function characterMatchValues(character: Character): string[] {
  return Array.from(new Set([
    character.id,
    character.name,
    ...(character.aliases ?? []),
    ...(character.nicknames ?? []),
    ...(character.match_names ?? [])
  ]));
}

function normalizeRoleToken(value: string): string {
  return value.replace(/\s+/g, "").toLocaleLowerCase();
}

function buildRunnableTasks(lines: ScriptLine[], characters: Character[]): { tasks: GenerationTask[]; blocked: ScriptLine[] } {
  const tasks: GenerationTask[] = [];
  const blocked: ScriptLine[] = [];
  for (const line of lines) {
    try {
      tasks.push(buildGenerationTask(line, characters));
    } catch {
      blocked.push(line);
    }
  }
  return { tasks, blocked };
}

function providerFromEngine(engine: ScriptLine["engine_override"]): ProviderType | null {
  if (engine === "gpt-sovits" || engine === "indextts" || engine === "vibevoice") return engine;
  return null;
}

function defaultServiceForProvider(services: WorkerHealth[], provider: ProviderType): string | null {
  return services.find((service) => service.enabled !== false && service.provider_type === provider && service.service_id)?.service_id ?? null;
}

function defaultCapabilitiesForProvider(provider: ProviderType): string[] {
  if (provider === "gpt-sovits") return ["trained_weights_voice", "reference_audio_voice"];
  if (provider === "indextts") return ["reference_audio_voice", "emotion_text"];
  if (provider === "openai" || provider === "gemini" || provider === "xai") return ["commercial_voice", "style_instruction"];
  if (provider === "volcengine") return ["commercial_voice", "emotion_text"];
  return ["tts"];
}

function defaultTemporaryConfig(provider: ProviderType, line: ScriptLine): Record<string, unknown> {
  if (provider === "indextts") {
    return {
      emotion_mode: line.note ? "emotion_text" : "same_as_voice",
      emotion_text: line.note || undefined,
      top_p: 0.8,
      top_k: 30,
      temperature: 0.8,
      num_beams: 3,
      repetition_penalty: 10,
      max_mel_tokens: 1500
    };
  }
  if (provider === "gpt-sovits") {
    return { prompt_lang: "zh", text_lang: line.language ?? "zh", text_split_method: "cut5" };
  }
  return {};
}

function servicesForEngine(services: WorkerHealth[], engine: ScriptLine["engine_override"] | "gpt-sovits") {
  return services.filter((service) => service.engine === engine && service.service_id);
}

function servicesForLine(services: WorkerHealth[], line: ScriptLine, characters: Character[]) {
  const binding = lineBinding(line, characters);
  if (binding?.provider_type) return services.filter((service) => service.provider_type === binding.provider_type && service.service_id);
  return servicesForEngine(services, lineEngine(line, characters));
}

function servicesForProvider(services: WorkerHealth[], provider: ProviderType) {
  return services.filter((service) => service.provider_type === provider && service.service_id);
}

function bindingsForLine(line: ScriptLine, characters: Character[]): VoiceBinding[] {
  const character = characters.find((item) => item.id === line.character_id);
  const profileId = line.profile_override ?? character?.default_profile;
  return character?.profiles?.find((profile) => profile.id === profileId)?.bindings ?? [];
}

function profilesForLine(line: ScriptLine, characters: Character[]): VoiceProfile[] {
  return characters.find((item) => item.id === line.character_id)?.profiles ?? [];
}

function prioritizedReferenceGroups(groups: ReferenceAudioGroup[], line: ScriptLine | undefined, characters: Character[]): ReferenceAudioGroup[] {
  if (!line) return groups.slice(0, 8);
  const name = characterName(characters, line.character_id).toLocaleLowerCase();
  const matching = groups.filter((group) => {
    const groupName = group.name.toLocaleLowerCase();
    return groupName.includes(name) || name.includes(groupName);
  });
  const rest = groups.filter((group) => !matching.includes(group));
  return [...matching, ...rest].slice(0, 8);
}

function stringConfig(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function vectorConfig(value: unknown): string {
  return Array.isArray(value) ? value.join(",") : "";
}

function parseVectorConfig(value: string): number[] {
  const parsed = value
    .split(/[,，\s]+/)
    .map((item) => Number(item))
    .filter((item) => Number.isFinite(item));
  return parsed.length > 0 ? parsed : [0, 0, 0, 0, 0, 0, 0, 0];
}

function referencePathForProvider(provider: string, config: Record<string, unknown>): string {
  if (provider === "indextts") return stringConfig(config.voice);
  return stringConfig(config.ref_audio_path);
}

function compactConfig(config: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(config).filter(([, value]) => value !== undefined && value !== ""));
}

function serviceTopbarTitle(summary: ReturnType<typeof serviceTopbarSummary>, t: Translate): string {
  return [
    `${t("services.localReady")}: ${summary.local.ready}/${summary.local.total}`,
    `${t("services.paidReady")}: ${summary.paid.ready}/${summary.paid.total}`,
    `${t("services.parserReady")}: ${summary.parser.ready}/${summary.parser.total}`,
    `${t("services.resourceReady")}: ${summary.resources.ready ? t("status.ready") : t("status.needsMapping")}`,
  ].join(" · ");
}

function topbarToneText(tone: string, t: Translate): string {
  if (tone === "ready") return t("status.ready");
  if (tone === "offline") return t("services.legendBlocked");
  return t("services.legendPartial");
}

function isUnsupportedLocalVibeVoice(service: WorkerHealth): boolean {
  return service.service_id === "local-vibevoice" || (service.mode === "local" && (service.provider_type ?? service.engine) === "vibevoice");
}

function mergeServiceRecords(settings: WorkerHealth[], health: WorkerHealth[]): WorkerHealth[] {
  const settingsById = new Map(settings.map((service) => [service.service_id ?? service.engine, service]));
  const healthById = new Map(health.map((service) => [service.service_id ?? service.engine, service]));
  const ids = Array.from(new Set([...settingsById.keys(), ...healthById.keys()]));
  return ids.map((id) => {
    const config = settingsById.get(id);
    const runtime = healthById.get(id);
    const provider = config?.provider_type ?? runtime?.provider_type ?? runtime?.engine ?? config?.engine;
    return {
      ...(config ?? {}),
      ...(runtime ?? {}),
      service_kind: config?.service_kind ?? runtime?.service_kind,
      display_name: config?.display_name ?? runtime?.display_name,
      base_url: config?.base_url || runtime?.base_url || defaultServiceBaseUrl(id, provider),
      network_scope: config?.network_scope ?? runtime?.network_scope,
      managed: config?.managed ?? runtime?.managed,
      enabled: config?.enabled ?? runtime?.enabled,
      poll_interval_seconds: config?.poll_interval_seconds ?? runtime?.poll_interval_seconds,
      auth_profile: config?.auth_profile ?? runtime?.auth_profile,
      default_params: config?.default_params ?? runtime?.default_params,
      cost_policy: config?.cost_policy ?? runtime?.cost_policy,
      key_configured: config?.key_configured ?? runtime?.key_configured,
    } as WorkerHealth;
  });
}

function defaultServiceBaseUrl(id: string, provider?: string): string | undefined {
  const defaults: Record<string, string> = {
    "local-gpt-sovits": "http://127.0.0.1:9880",
    "local-indextts": "http://127.0.0.1:9881",
    "openai-tts": "https://api.openai.com/v1",
    "gemini-tts": "https://generativelanguage.googleapis.com/v1beta",
    "xai-tts": "https://api.x.ai/v1",
    "volcengine-tts": "https://openspeech.bytedance.com/api/v1/tts"
  };
  if (defaults[id]) return defaults[id];
  if (provider === "openai") return defaults["openai-tts"];
  if (provider === "gemini") return defaults["gemini-tts"];
  if (provider === "xai") return defaults["xai-tts"];
  if (provider === "volcengine") return defaults["volcengine-tts"];
  return undefined;
}

function serviceDisplayName(service: WorkerHealth): string {
  if (service.display_name) return service.display_name;
  const provider = service.provider_type ?? service.engine;
  const nameMap: Record<string, string> = {
    "gpt-sovits": "GPT-SoVITS",
    indextts: "IndexTTS",
    vibevoice: "VibeVoice",
    openai: "OpenAI TTS",
    gemini: "Gemini TTS",
    xai: "xAI TTS",
    volcengine: "Volcengine TTS",
    "generic-http": "Generic HTTP TTS"
  };
  const base = nameMap[provider] ?? standardProjectName(provider);
  if (service.mode === "external" || service.capabilities?.includes("paid_provider")) return base;
  if (service.service_id?.startsWith("local-")) return `${base} Local`;
  return base;
}

function serviceHealthText(service: WorkerHealth, t: Translate, runtimeMode?: string): string {
  const tone = serviceOperationalTone(service, false, runtimeMode);
  return serviceOperationalLabel(service, tone, t, runtimeMode);
}

function serviceLifecycleText(service: WorkerHealth, t: Translate): string {
  const state = service.supervisor?.running ? "running" : service.supervisor?.manageable ? "stopped" : service.mode ?? "external";
  return statusText(state, t);
}

function serviceEndpointMode(service: WorkerHealth, t: Translate): string {
  if (service.mode === "external") return t("services.remoteExternal");
  if (service.supervisor?.manageable || service.service_id?.startsWith("local-")) return t("services.localManaged");
  return service.mode ?? t("services.remoteExternal");
}

function serviceAuthText(service: WorkerHealth, t: Translate): string {
  if (!service.auth_profile || Object.keys(service.auth_profile).length === 0) {
    return service.capabilities?.includes("paid_provider") ? t("status.needsKey") : "-";
  }
  return Object.keys(service.auth_profile).join(", ");
}

function serviceAuthEnvNames(service: WorkerHealth): string[] {
  if (!service.auth_profile) return [];
  return Object.values(service.auth_profile).filter((value): value is string => Boolean(value));
}

function summarizeConfigValue(value: unknown): string {
  if (!value || (typeof value === "object" && Object.keys(value as Record<string, unknown>).length === 0)) return "-";
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `${key}: ${String(item)}`)
      .join(" · ");
  }
  return String(value);
}

function shortPath(value: string): string {
  const parts = value.split(/[\\/]/).filter(Boolean);
  if (parts.length <= 2) return value;
  return `${parts.at(-2)} / ${parts.at(-1)}`;
}

function resourceGroups(services: WorkerHealth[]): Array<{ name: string; ready: number; total: number }> {
  const groups = new Map<string, { name: string; ready: number; total: number }>();
  for (const service of services) {
    const name = service.resource_group ?? "unassigned";
    const group = groups.get(name) ?? { name, ready: 0, total: 0 };
    group.total += 1;
    if (isServiceOperational(service)) group.ready += 1;
    groups.set(name, group);
  }
  return Array.from(groups.values());
}

function resourceGroupTone(group: { ready: number; total: number }): "ok" | "warn" | "danger" {
  if (group.total === 0) return "danger";
  if (group.ready === group.total) return "ok";
  if (group.ready > 0) return "warn";
  return "danger";
}

function isMockEndpoint(service: WorkerHealth, runtimeMode?: string): boolean {
  return runtimeMode === "mock" || service.mode === "mock" || Boolean(service.base_url?.startsWith("mock://"));
}

function isStoppedManagedService(service: WorkerHealth): boolean {
  return Boolean(service.supervisor?.manageable && !service.supervisor.running);
}

function buildValidationSteps(
  runtime: RuntimeMode | null,
  services: WorkerHealth[],
  candidates: VoiceCandidates | null,
  manifest: GenerationManifest,
  t: Translate
): Array<{ id: "mode" | "services" | "resources" | "generation"; label: string; state: "ready" | "attention" | "done" }> {
  const localServices = services.filter((service) => coreLocalProviders.has(service.provider_type ?? service.engine));
  const completed = Object.values(manifest.lines)
    .flatMap((history) => history.versions)
    .filter((version) => version.status === "completed" && coreLocalProviders.has(version.provider_type ?? version.engine)).length;
  return [
    { id: "mode", label: statusText(runtime?.service_mode ?? "real", t), state: runtime?.service_mode === "real" ? "done" : "attention" },
    { id: "services", label: `${localServices.filter(isServiceOperational).length}/${localServices.length}`, state: localServices.length >= coreLocalProviders.size && localServices.every(isServiceOperational) ? "done" : "attention" },
    { id: "resources", label: candidates?.ready ? t("status.ready") : t("status.needsMapping"), state: candidates?.ready ? "done" : "attention" },
    { id: "generation", label: `${completed}/${coreLocalProviders.size}`, state: completed >= coreLocalProviders.size ? "done" : "ready" }
  ];
}

function validationReasonText(state: { reasonKey: string | null; serviceId?: string }, t: Translate): string {
  if (!state.reasonKey) return "";
  return t(state.reasonKey, { service: state.serviceId ?? "" });
}

function summaryLabel(summary: ReturnType<typeof summarizeLineHistory>, t: Translate): string {
  if (summary.tone === "idle") return t("status.notGenerated");
  const version = summary.label.split(" ")[0];
  return `${version} ${statusText(summary.tone, t)}`;
}

function saveStateLabel(state: SaveState, t: Translate): string {
  if (state === "saving") return t("status.saving");
  if (state === "saved") return t("status.saved");
  if (state === "error") return t("status.saveError");
  return t("app.autoSave");
}

function saveStateTone(state: SaveState): "idle" | "queued" | "running" | "completed" | "failed" {
  if (state === "saving") return "running";
  if (state === "saved") return "completed";
  if (state === "error") return "failed";
  return "idle";
}

function statusText(status: string, t: Translate): string {
  const normalized = status.trim().toLowerCase().replaceAll("_", " ");
  const keyMap: Record<string, string> = {
    saved: "status.saved",
    saving: "status.saving",
    "save error": "status.saveError",
    completed: "status.completed",
    failed: "status.failed",
    running: "status.running",
    loading: "status.loading",
    finalizing: "status.finalizing",
    cancelled: "status.cancelled",
    queued: "status.queued",
    ready: "status.ready",
    "not generated": "status.notGenerated",
    "needs key": "status.needsKey",
    "bridge required": "status.bridgeRequired",
    "unsupported gradio app": "status.unsupportedGradioApp",
    "needs setup": "status.needsSetup",
    stopped: "status.stopped",
    external: "status.external",
    mock: "status.mock",
    real: "status.real",
    missing: "status.missing",
    "needs mapping": "status.needsMapping",
    auto: "status.auto",
    resource: "status.resource",
    unassigned: "status.unassigned",
    unset: "status.unset"
  };
  return keyMap[normalized] ? t(keyMap[normalized]) : status;
}

type OperationalTone = "ok" | "warn" | "danger" | "running";

function serviceOperationalTone(service: WorkerHealth, isRunning: boolean, runtimeMode?: string): OperationalTone {
  const healthStatus = String(service.health?.status ?? "").toLowerCase();
  if (isRunning) return "running";
  if (service.enabled === false) return "danger";
  if (isMockEndpoint(service, runtimeMode)) return "danger";
  if (!service.base_url) return "danger";
  if (isStoppedManagedService(service)) return "danger";
  if (healthStatus === "bridge required") return "warn";
  if (healthStatus === "unsupported gradio app") return "danger";
  if (service.capabilities?.includes("paid_provider") || service.mode === "external") {
    if (service.key_configured === false) return "warn";
    return service.ready ? "ok" : "danger";
  }
  return service.ready ? "ok" : "danger";
}

function serviceOperationalLabel(service: WorkerHealth, tone: OperationalTone, t: Translate, runtimeMode?: string): string {
  const healthStatus = String(service.health?.status ?? "").toLowerCase();
  if (tone === "running") return t("status.running");
  if (service.enabled === false) return t("status.disabled");
  if (isMockEndpoint(service, runtimeMode)) return t("services.realEndpointRequired");
  if (!service.base_url) return t("services.endpointMissing");
  if (isStoppedManagedService(service)) return t("services.notStarted");
  if (service.key_configured === false) return t("status.needsKey");
  if (healthStatus) return statusText(healthStatus, t);
  if (tone === "ok") return t("status.ready");
  if (tone === "warn") return t("status.needsSetup");
  return t("services.blocked");
}

function parserProviderTone(provider: ParserProviderDraft): OperationalTone {
  if (!provider.enabled) return "danger";
  if (provider.key_configured) return "ok";
  if (provider.base_url && provider.model && provider.api_key_env) return "warn";
  return "danger";
}

function queueStatusTone(status: string): "idle" | "queued" | "running" | "completed" | "failed" {
  if (status === "completed") return "completed";
  if (status === "failed" || status === "cancelled") return "failed";
  if (status === "queued") return "queued";
  if (status === "loading" || status === "finalizing" || status === "running") return "running";
  return "idle";
}

function StatusPill({ tone, label }: { tone: "idle" | "queued" | "running" | "completed" | "failed"; label: string }) {
  return <span className={`status-pill ${tone}`}>{label}</span>;
}
