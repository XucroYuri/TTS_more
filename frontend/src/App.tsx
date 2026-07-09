import {
  AlertCircle,
  Bot,
  CheckCircle2,
  Cpu,
  FileText,
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
  Trash2,
  Upload,
  Wand2,
  X
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  fetchCharacters,
  fetchProjectCharacters,
  fetchManifest,
  fetchGptSovitsModelCatalog,
  fetchGptSovitsModelSamples,
  fetchLogsReferenceAudio,
  fetchOpenSourceTTSCatalog,
  fetchParserProviders,
  fetchProject,
  fetchProjects,
  fetchRuntimeMode,
  fetchServiceSettings,
  fetchServiceLoadState,
  saveServiceSettings,
  configureOpenSourceTTS,
  detectOpenSourceTTS,
  fetchServiceLogs,
  fetchServices,
  fetchServicesStatus,
  fetchGenerationJob,
  fetchQueueStatus,
  generationPreflight,
  fetchVoiceCandidates,
  fetchLogsCandidates,
  freezeProjectCharacter,
  createGenerationJob,
  cancelGenerationJob,
  createParseRevision,
  createScriptRevision,
  deleteProject,
  deleteGenerationVersion,
  importRoleLibraryCandidate,
  reloadServiceSettings,
  runRealValidation,
  saveCharacters,
  saveParserProviders,
  scanCharacterLibrary,
  saveProject,
  startAndWaitService,
  startService,
  stopService,
  testParserProvider,
  testService,
  unfreezeProjectCharacter,
  deleteCharacterLibraryItem,
  uploadCharacterAvatar,
  uploadCharacterReferenceAudio,
  uploadProjectReferenceAudio
} from "./api";
import { defaultLanguage, languageOptions, nextLanguage, normalizeLanguage } from "./i18n";
import { ReferenceAudioInput } from "./components/ReferenceAudioInput";
import { RoleAvatar } from "./components/RoleAvatar";
import { ScriptManagerModal } from "./components/ScriptManagerModal";
import { WaveformPlayer } from "./components/WaveformPlayer";
import { TokenGate } from "./components/TokenGate";
import { generationFailureView, generationVersionTags, groupGenerationVersions, newestPlayableVersion, versionToInspectorDraft, type InspectorVersionDraft } from "./lib/generationHistory";
import { applyLogsReferenceSampleToConfig, selectedLogsReferenceSample } from "./lib/gptSovitsReference";
import { formatScriptNote } from "./lib/lineNote";
import { firstReferenceSampleFromModel, gptSovitsProjectBindingFromModel } from "./lib/modelCatalog";
import { ensureProjectCharacters, freezeProjectCharacterLocally, projectCharacterRows, resolveProjectCharacters } from "./lib/projectCharacters";
import { bindingCompleteness, catalogServiceOptions, roleLibraryBindingRows, roleLibraryServiceOptions, selectedCatalogServiceId } from "./lib/roleLibraryView";
import { buildGenerationTask, lineBinding, lineEngine, lineProfile, lineServiceId } from "./lib/routing";
import { createDefaultParserProviderDraft, KWJM_API_KEY_ENV, KWJM_BASE_URL, KWJM_BASE_URL_PLACEHOLDER, KWJM_MODEL, KWJM_PROVIDER_NAME, normalizeParserProviderDrafts, parserProviderKeyState, toParserProviderSavePayload, upsertKwjmParserProvider } from "./lib/parserConfig";
import { createEmptyManifest, createEmptyProject, createProjectId, readStoredProjectId, selectStartupProjectId, writeStoredProjectId } from "./lib/projectStartup";
import { filterAndSortProjectSummaries, nextProjectAfterDelete } from "./lib/scriptManagement";
import { projectToScriptSourceText } from "./lib/scriptSource";
import { summarizeLineHistory } from "./lib/status";
import { coreLocalProviders, coreProviderCoverage, filterScriptLines, isServiceOperational, lineHistoryForLine, routableProviderServices, serviceTopbarHealthItems, serviceTopbarSummary, standardProjectName, toggleLineSelection, validationRunState, type LineStatusFilter } from "./lib/workstation";
import { buildGradioEndpointRequest, gradioContractForProvider } from "./lib/ttsAccess";
import { createToast, inferToastLevel, toastDuration, type Toast, type ToastLevel, type ToastOptions } from "./lib/toast";
import { generationMethodForProvider, generationMethodOptions, generationMethodRouteLabels, historyPlayerSummary, inspectorBackupReferenceVisible, inspectorDiagnosticsState, inspectorPanelMode, inspectorSections, inspectorVersionContextVisible, lineCardSecondaryBadges, lineFilterToolbarState, lineFocusTransition, preflightFallbackAction, preflightLineLabelKey, preflightLineTone, preflightLoadLabelKey, preflightLoadTone, roleAccentClass, shouldRequestRevisionConfirmation, trustedBackupReferenceGroups, type GenerationMethodId, type LineCardSecondaryBadge } from "./lib/workbenchView";
import type {
  Character,
  CharacterReferenceAudioGroup,
  GenerationManifest,
  ParserProviderDraft,
  ParserProviderTestResponse,
  ProjectCharacter,
  ProjectSummary,
  RoleLibraryCandidate,
  RuntimeMode,
  ScriptLine,
  ScriptProject,
  VoiceBinding,
  VoiceCandidates,
  VoiceProfile,
  WorkerHealth,
  GenerationJob,
  GenerationVersion,
  GenerationTask,
  GenerationPreflightResponse,
  LogsReferenceAudioResponse,
  LogsReferenceAudioSample,
  CatalogProvider,
  OpenSourceTTSCatalogItem,
  OpenSourceTTSDetectResponse,
  ProviderType,
  QueueStatus,
  ReferenceAudioSample,
  ServiceLoadState
} from "./types";

type Translate = (key: string, options?: Record<string, unknown>) => string;
type SaveState = "idle" | "saving" | "saved" | "error";
type ServicePanelSection = "overview" | "open-source" | "tts" | "llm" | "resources" | "roles";
type ConfirmationTone = "warning" | "danger" | "info";
const KWJM_TESTING_INDEX = -1;
const LINE_LOAD_BATCH_SIZE = 40;
const COSY_VOICE_MODE_OPTIONS = [
  { id: "sft", labelKey: "inspector.cosyModeSft" },
  { id: "zero_shot", labelKey: "inspector.cosyModeZeroShot" },
  { id: "cross_lingual", labelKey: "inspector.cosyModeCrossLingual" },
  { id: "instruct", labelKey: "inspector.cosyModeInstruct" }
] as const;
const INDEX_EMOTION_MODE_OPTIONS = [
  { id: "same_as_voice", labelKey: "inspector.emotionSameAsVoice" },
  { id: "emotion_text", labelKey: "inspector.emotionText" },
  { id: "emotion_audio", labelKey: "inspector.emotionAudio" },
  { id: "emotion_vector", labelKey: "inspector.emotionVector" }
] as const;

type CosyVoiceMode = (typeof COSY_VOICE_MODE_OPTIONS)[number]["id"];
type IndexEmotionMode = (typeof INDEX_EMOTION_MODE_OPTIONS)[number]["id"];

interface ConfirmationDialogState {
  title: string;
  body: string;
  detail?: string;
  confirmLabel: string;
  cancelLabel: string;
  tone: ConfirmationTone;
}

function characterName(characters: Character[], id: string): string {
  return characters.find((character) => character.id === id)?.name ?? id;
}

function avatarFallback(name: string): string {
  return name.trim().slice(0, 1).toLocaleUpperCase() || "?";
}

export default function App() {
  const { t, i18n } = useTranslation();
  const [currentProjectId, setCurrentProjectId] = useState<string | null>(() => readStoredProjectId());
  const [projectSummaries, setProjectSummaries] = useState<ProjectSummary[]>([]);
  const [characters, setCharacters] = useState<Character[]>([]);
  const [project, setProject] = useState<ScriptProject>(() => createEmptyProject());
  const [manifest, setManifest] = useState<GenerationManifest>(() => createEmptyManifest(null));
  const [services, setServices] = useState<WorkerHealth[]>([]);
  const [runtime, setRuntime] = useState<RuntimeMode | null>(null);
  const [voiceCandidates, setVoiceCandidates] = useState<VoiceCandidates | null>(null);
  const [activeLineId, setActiveLineId] = useState("");
  const [expandedLineId, setExpandedLineId] = useState<string | null>(null);
  const [selectedHistoryVersions, setSelectedHistoryVersions] = useState<Record<string, string>>({});
  const [versionDrafts, setVersionDrafts] = useState<Record<string, InspectorVersionDraft & { version_id: string }>>({});
  const [diagnosticsExpanded, setDiagnosticsExpanded] = useState(false);
  const [routeSettingsOpen, setRouteSettingsOpen] = useState(false);
  const [selectedLineIds, setSelectedLineIds] = useState<string[]>([]);
  const [lineTextDrafts, setLineTextDrafts] = useState<Record<string, string>>({});
  const [parserProviders, setParserProviders] = useState<ParserProviderDraft[]>([]);
  const [roleLibraryCandidates, setRoleLibraryCandidates] = useState<RoleLibraryCandidate[]>([]);
  const [gptModelCatalog, setGptModelCatalog] = useState<RoleLibraryCandidate[]>([]);
  const [isScanningModelCatalog, setIsScanningModelCatalog] = useState(false);
  const [activeModelCatalogId, setActiveModelCatalogId] = useState<string | null>(null);
  const [activeModelSampleId, setActiveModelSampleId] = useState<string | null>(null);
  const [activeProjectRoleId, setActiveProjectRoleId] = useState<string | null>(null);
  const [modelCatalogSamples, setModelCatalogSamples] = useState<Record<string, LogsReferenceAudioResponse>>({});
  const [loadingModelCatalogSamplesKey, setLoadingModelCatalogSamplesKey] = useState<string | null>(null);
  const [isSavingParserConfig, setIsSavingParserConfig] = useState(false);
  const [testingParserProviderIndex, setTestingParserProviderIndex] = useState<number | null>(null);
  const [parserProviderTestResults, setParserProviderTestResults] = useState<Record<number, ParserProviderTestResponse>>({});
  const [kwjmApiKeyInput, setKwjmApiKeyInput] = useState("");
  const [kwjmParserTestResult, setKwjmParserTestResult] = useState<ParserProviderTestResponse | null>(null);
  const [isLlmAdvancedOpen, setIsLlmAdvancedOpen] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isRefreshingTopology, setIsRefreshingTopology] = useState(false);
  const [isValidating, setIsValidating] = useState(false);
  const [isSavingServiceConfig, setIsSavingServiceConfig] = useState(false);
  const [testingServiceId, setTestingServiceId] = useState<string | null>(null);
  const [isScanningRoleLibrary, setIsScanningRoleLibrary] = useState(false);
  const [isTopologyMenuOpen, setIsTopologyMenuOpen] = useState(false);
  const [servicePanelSection, setServicePanelSection] = useState<ServicePanelSection>("open-source");
  const [openSourceCatalog, setOpenSourceCatalog] = useState<OpenSourceTTSCatalogItem[]>([]);
  const [selectedOpenSourceProvider, setSelectedOpenSourceProvider] = useState<CatalogProvider>("gpt-sovits");
  const [openSourceBaseUrl, setOpenSourceBaseUrl] = useState("");
  const [openSourceResourceGroup, setOpenSourceResourceGroup] = useState("gradio-gpu-0");
  const [openSourceCapacity, setOpenSourceCapacity] = useState(1);
  const [openSourceDisplayName, setOpenSourceDisplayName] = useState("");
  const [openSourceDetectResult, setOpenSourceDetectResult] = useState<OpenSourceTTSDetectResponse | null>(null);
  const [isDetectingOpenSource, setIsDetectingOpenSource] = useState(false);
  const [isConfiguringOpenSource, setIsConfiguringOpenSource] = useState(false);
  const [newScriptTitle, setNewScriptTitle] = useState("");
  const [newScriptSource, setNewScriptSource] = useState("");
  const [isCreatingScript, setIsCreatingScript] = useState(false);
  const [managerSearchText, setManagerSearchText] = useState("");
  const [managedProjectId, setManagedProjectId] = useState<string | null>(null);
  const [managedProject, setManagedProject] = useState<ScriptProject | null>(null);
  const [isManagedProjectLoading, setIsManagedProjectLoading] = useState(false);
  const [managerTitleDraft, setManagerTitleDraft] = useState("");
  const [managerSourceDraft, setManagerSourceDraft] = useState("");
  const [isManagerSaving, setIsManagerSaving] = useState(false);
  const [isManagerParsing, setIsManagerParsing] = useState(false);
  const [deletingProjectId, setDeletingProjectId] = useState<string | null>(null);
  const [isProjectLoaded, setIsProjectLoaded] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [lastSavedAt, setLastSavedAt] = useState<string | null>(null);
  const [expandedServiceId, setExpandedServiceId] = useState<string | null>(null);
  const [expandedServiceConfigId, setExpandedServiceConfigId] = useState<string | null>(null);
  const [selectedParserProviderIndex, setSelectedParserProviderIndex] = useState(0);
  const [serviceLogs, setServiceLogs] = useState<Record<string, string[]>>({});
  const [serviceLoadStates, setServiceLoadStates] = useState<Record<string, ServiceLoadState>>({});
  const [serviceSecrets, setServiceSecrets] = useState<Record<string, Record<string, string>>>({});
  const [logsReferenceAudio, setLogsReferenceAudio] = useState<Record<string, LogsReferenceAudioResponse>>({});
  const [loadingLogsReferenceKey, setLoadingLogsReferenceKey] = useState<string | null>(null);
  const [confirmationDialog, setConfirmationDialog] = useState<ConfirmationDialogState | null>(null);
  const confirmationResolverRef = useRef<((confirmed: boolean) => void) | null>(null);
  const [selectedLogsServiceId, setSelectedLogsServiceId] = useState<string>("");
  const [activeLibraryCharacterId, setActiveLibraryCharacterId] = useState<string | null>(null);
  const [activeRoleCandidateId, setActiveRoleCandidateId] = useState<string | null>(null);
  const [activeJob, setActiveJob] = useState<GenerationJob | null>(null);
  const generationAbortRef = useRef(false);
  const [queueStatus, setQueueStatus] = useState<QueueStatus | null>(null);
  const [preflightResult, setPreflightResult] = useState<GenerationPreflightResponse | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const toastTimers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());

  const removeToast = useCallback((toastId: number) => {
    setToasts((current) => current.filter((item) => item.id !== toastId));
    const timer = toastTimers.current.get(toastId);
    if (timer) {
      clearTimeout(timer);
      toastTimers.current.delete(toastId);
    }
  }, []);

  const pushToast = useCallback((message: string, options: ToastOptions = {}) => {
    if (!message) return;
    const toast = createToast(message, options);
    setToasts((current) => [...current.slice(-3), toast]);
    const duration = toastDuration(options);
    if (duration > 0) {
      const timer = setTimeout(() => removeToast(toast.id), duration);
      toastTimers.current.set(toast.id, timer);
    }
    return toast.id;
  }, [removeToast]);

  /**
   * Compatibility wrapper: accepts a message (usually an i18n string) and pushes
   * it as a toast. The level is inferred from the message/keys unless overridden.
   * All former setNotice(...) call sites continue to work through this wrapper.
   */
  const setNotice = useCallback((message: string, options?: { level?: ToastLevel }) => {
    if (!message) return;
    pushToast(message, { level: options?.level ?? inferToastLevel(message) });
  }, [pushToast]);

  const notice = toasts.length > 0 ? toasts[toasts.length - 1].message : "";
  const [searchText, setSearchText] = useState("");
  const [roleLibrarySearch, setRoleLibrarySearch] = useState("");
  const [characterFilter, setCharacterFilter] = useState("all");
  const [providerFilter, setProviderFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState<LineStatusFilter>("all");
  const [visibleLineCount, setVisibleLineCount] = useState(LINE_LOAD_BATCH_SIZE);
  const lineLoadMoreRef = useRef<HTMLDivElement | null>(null);

  function requestConfirmation(dialog: ConfirmationDialogState): Promise<boolean> {
    confirmationResolverRef.current?.(false);
    return new Promise((resolve) => {
      confirmationResolverRef.current = resolve;
      setConfirmationDialog(dialog);
    });
  }

  function resolveConfirmation(confirmed: boolean) {
    confirmationResolverRef.current?.(confirmed);
    confirmationResolverRef.current = null;
    setConfirmationDialog(null);
  }

  useEffect(() => () => {
    confirmationResolverRef.current?.(false);
  }, []);

  useEffect(() => () => {
    toastTimers.current.forEach((timer) => clearTimeout(timer));
    toastTimers.current.clear();
  }, []);

  useEffect(() => {
    setNotice(t("app.ready"));
    void refreshTopology();
    void refreshOpenSourceCatalog();
    void refreshProjects();
    void refreshParserProviders();
    fetchCharacters()
      .then(setCharacters)
      .catch(() => setCharacters([]));
  }, [t]);

  useEffect(() => {
    if (!currentProjectId) {
      setProject(createEmptyProject());
      setManifest(createEmptyManifest(null));
      setActiveLineId("");
      setExpandedLineId(null);
      setSelectedHistoryVersions({});
      setVersionDrafts({});
      setSelectedLineIds([]);
      setLineTextDrafts({});
      setIsProjectLoaded(true);
      setSaveState("idle");
      return;
    }
    setIsProjectLoaded(false);
    fetchProject(currentProjectId)
      .then((payload) => {
        setProject(payload);
        setActiveLineId(payload.lines[0]?.id ?? "");
        setExpandedLineId(null);
        setSelectedHistoryVersions({});
        setVersionDrafts({});
        setLineTextDrafts({});
        setIsProjectLoaded(true);
        return fetchProjectCharacters(currentProjectId)
          .then((projectCharactersPayload) => {
            setProject((current) => ({ ...current, project_characters: projectCharactersPayload.project_characters }));
          })
          .catch(() => undefined);
      })
      .catch(() => {
        setProject(createEmptyProject());
        setActiveLineId("");
        setExpandedLineId(null);
        setSelectedHistoryVersions({});
        setVersionDrafts({});
        setSelectedLineIds([]);
        setLineTextDrafts({});
        setIsProjectLoaded(true);
        setNotice(t("empty.projectLoadFailed"));
      });
    fetchManifest(currentProjectId)
      .then(setManifest)
      .catch(() => setManifest(createEmptyManifest(currentProjectId)));
  }, [currentProjectId, t]);

  useEffect(() => {
    if (!isProjectLoaded || !currentProjectId) return;
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

  const filteredLibraryCharacters = useMemo(() => {
    const query = roleLibrarySearch.trim().toLocaleLowerCase();
    if (!query) return characters;
    return characters.filter((character) => characterMatchValues(character).join(" ").toLocaleLowerCase().includes(query));
  }, [characters, roleLibrarySearch]);
  const filteredRoleCandidates = useMemo(() => {
    const query = roleLibrarySearch.trim().toLocaleLowerCase();
    if (!query) return roleLibraryCandidates;
    return roleLibraryCandidates.filter((candidate) =>
      `${candidate.name} ${candidate.id} ${candidate.logs_name ?? ""} ${(candidate.aliases ?? []).join(" ")}`.toLocaleLowerCase().includes(query)
    );
  }, [roleLibraryCandidates, roleLibrarySearch]);
  const activeLibraryCharacter = useMemo(
    () => filteredLibraryCharacters.find((character) => character.id === activeLibraryCharacterId) ?? filteredLibraryCharacters[0] ?? null,
    [activeLibraryCharacterId, filteredLibraryCharacters]
  );
  const activeRoleCandidate = useMemo(
    () => roleLibraryCandidates.find((candidate) => candidate.id === activeRoleCandidateId) ?? null,
    [activeRoleCandidateId, roleLibraryCandidates]
  );
  const filteredGptModelCatalog = useMemo(() => {
    const query = roleLibrarySearch.trim().toLocaleLowerCase();
    if (!query) return gptModelCatalog;
    return gptModelCatalog.filter((model) =>
      `${model.name} ${model.id} ${model.logs_name ?? ""} ${(model.aliases ?? []).join(" ")}`.toLocaleLowerCase().includes(query)
    );
  }, [gptModelCatalog, roleLibrarySearch]);
  const activeProjectCharacter = useMemo(
    () => {
      const currentLineCharacterId = project.lines.find((line) => line.id === activeLineId)?.character_id ?? project.lines[0]?.character_id;
      return projectCharacters.find((item) => item.project_character_id === activeProjectRoleId)
        ?? projectCharacters.find((item) => item.project_character_id === currentLineCharacterId)
        ?? projectCharacters[0]
        ?? null;
    },
    [activeLineId, activeProjectRoleId, project.lines, projectCharacters]
  );
  const activeModelCatalogItem = useMemo(
    () => activeModelCatalogId ? filteredGptModelCatalog.find((model) => model.id === activeModelCatalogId) ?? null : null,
    [activeModelCatalogId, filteredGptModelCatalog]
  );
  const activeModelSamplesKey = activeModelCatalogItem
    ? [activeModelCatalogItem.service_id ?? selectedLogsServiceId ?? "", activeModelCatalogItem.logs_name ?? activeModelCatalogItem.name].join("|")
    : "";
  const activeModelSamplesPayload = activeModelSamplesKey ? modelCatalogSamples[activeModelSamplesKey] : undefined;
  const activeModelSamples = activeModelSamplesPayload?.samples ?? [];
  const activeModelSelectedSample = activeModelSamples.find((sample) => sample.sample_id === activeModelSampleId) ?? activeModelSamples[0] ?? (activeModelCatalogItem ? firstReferenceSampleFromModel(activeModelCatalogItem) : null);
  const preflightByLine = useMemo(() => new Map((preflightResult?.items ?? []).map((item) => [item.line_uid ?? item.line_id, item])), [preflightResult]);
  const activeLine = useMemo(() => project.lines.find((line) => line.id === activeLineId) ?? project.lines[0], [activeLineId, project.lines]);
  const activeRoleRow = useMemo(
    () => activeLine ? projectRoleRows.find((role) => role.id === activeLine.character_id) : undefined,
    [activeLine, projectRoleRows]
  );
  const activeVersions = useMemo(() => (activeLine ? lineHistoryForLine(manifest, activeLine)?.versions ?? [] : []), [activeLine, manifest]);
  const selectedHistoryVersion = useMemo(
    () => activeVersions.find((version) => version.version_id === selectedHistoryVersions[activeLine?.id ?? ""]),
    [activeLine?.id, activeVersions, selectedHistoryVersions]
  );
  const selectedHistoryVersionTags = useMemo(
    () => {
      if (!selectedHistoryVersion) return null;
      const service = selectedHistoryVersion.service_id ? services.find((item) => item.service_id === selectedHistoryVersion.service_id) : undefined;
      return generationVersionTags(
        selectedHistoryVersion,
        selectedHistoryVersion.service_id ? serviceDisplayName(service ?? ({ engine: selectedHistoryVersion.engine, display_name: selectedHistoryVersion.service_id, ready: false } as WorkerHealth)) : undefined
      );
    },
    [selectedHistoryVersion, services]
  );
  const activeVersionDraft = activeLine ? versionDrafts[activeLine.id] : undefined;
  const activeInspectorMode = inspectorPanelMode(selectedHistoryVersion?.version_id);
  const activeSummary = useMemo(() => summarizeLineHistory(activeLine ? lineHistoryForLine(manifest, activeLine) : undefined), [activeLine, manifest]);
  const activePlayableVersion = useMemo(() => newestPlayableVersion(activeVersions), [activeVersions]);
  const activeLineTextDraft = activeLine ? lineTextDrafts[activeLine.id] ?? activeLine.text : "";
  const activeBindings = useMemo(() => (activeLine ? bindingsForLine(activeLine, resolvedCharacters) : []), [activeLine, resolvedCharacters]);
  const activeBinding = useMemo(() => (activeLine ? lineBinding(activeLine, resolvedCharacters) : undefined), [activeLine, resolvedCharacters]);
  const activeProfiles = useMemo(() => (activeLine ? profilesForLine(activeLine, resolvedCharacters) : []), [activeLine, resolvedCharacters]);
  const activeProvider: ProviderType = activeLine ? activeVersionDraft?.provider_type ?? activeBinding?.provider_type ?? providerFromEngine(activeLine.engine_override) ?? "indextts" : "gpt-sovits";
  const generationMethods = useMemo(() => generationMethodOptions(), []);
  const activeGenerationMethod = generationMethodForProvider(activeProvider);
  const activeGenerationRouteLabels = useMemo(() => generationMethodRouteLabels(activeGenerationMethod), [activeGenerationMethod]);
  const activeServiceId = activeLine ? activeVersionDraft?.service_id ?? lineServiceId(activeLine, resolvedCharacters) ?? "" : "";
  const activeServiceLoadState = activeServiceId ? serviceLoadStates[activeServiceId] : undefined;
  const activePreflightItem = activeLine ? preflightByLine.get(activeLine.line_uid ?? activeLine.id) : undefined;
  const activeExpectedLoadSignature = activePreflightItem?.load_signature ?? selectedHistoryVersion?.verified_load_signature ?? selectedHistoryVersion?.requested_load_signature ?? null;
  const activeInspectorSections = useMemo(() => inspectorSections(activeInspectorMode), [activeInspectorMode]);
  const activeInspectorDiagnostics = useMemo(
    () => inspectorDiagnosticsState({
      loaded: activeServiceLoadState?.loaded,
      loadedSignature: activeServiceLoadState?.loaded_signature,
      expectedSignature: activeExpectedLoadSignature,
      lastError: activeServiceLoadState?.last_error,
      expanded: diagnosticsExpanded
    }),
    [activeExpectedLoadSignature, activeServiceLoadState?.last_error, activeServiceLoadState?.loaded, activeServiceLoadState?.loaded_signature, diagnosticsExpanded]
  );
  const activeRawBindingConfig = useMemo(() => activeVersionDraft?.parameters ?? activeBinding?.config ?? {}, [activeBinding, activeVersionDraft]);
  const activeBindingConfig = useMemo(
    () => (!activeVersionDraft && activeLine?.service_override ? clearServiceScopedBindingConfig(activeProvider, activeRawBindingConfig) : activeRawBindingConfig),
    [activeLine?.service_override, activeProvider, activeRawBindingConfig, activeVersionDraft]
  );
  const cosyVoiceMode = cosyVoiceModeFromConfig(activeBindingConfig.mode);
  const indexEmotionMode = indexEmotionModeFromConfig(activeBindingConfig.emotion_mode);
  const cosyVoiceNeedsSpeaker = cosyVoiceMode === "sft" || cosyVoiceMode === "instruct";
  const cosyVoiceNeedsPrompt = cosyVoiceMode === "zero_shot" || cosyVoiceMode === "cross_lingual";
  const cosyVoiceNeedsInstruction = cosyVoiceMode === "instruct";

  useEffect(() => {
    setDiagnosticsExpanded(false);
  }, [activeLine?.id, activeServiceId]);

  useEffect(() => {
    setRouteSettingsOpen(false);
  }, [activeLine?.id, activeGenerationMethod]);

  const activeLogsReferenceRequest = useMemo(
    () => logsReferenceRequest(activeProvider, activeServiceId, activeBindingConfig),
    [activeBindingConfig, activeProvider, activeServiceId]
  );
  const activeLogsReferencePayload = activeLogsReferenceRequest ? logsReferenceAudio[activeLogsReferenceRequest.key] : undefined;
  const activeLogsReferenceSamples = activeLogsReferencePayload?.samples ?? [];
  const activeLogsReferenceSample = selectedLogsReferenceSample(activeLogsReferenceSamples, activeBindingConfig, { serviceId: activeServiceId });
  const activeReferenceAudioPath = activeProvider === "gpt-sovits" ? activeLogsReferenceSample?.path ?? stringConfig(activeBindingConfig.ref_audio_path) : "";
  const activeReferenceAudioLabel = activeLogsReferenceSample?.display_label || shortPath(activeReferenceAudioPath) || t("inspector.referenceAudio");
  const staleLogsReferenceServiceId = stringConfig(activeBindingConfig.logs_reference_service_id);
  const isLogsReferenceFromOtherService = Boolean(activeProvider === "gpt-sovits" && staleLogsReferenceServiceId && activeServiceId && staleLogsReferenceServiceId !== activeServiceId);
  const candidateReferenceGroups = useMemo(
    () => trustedBackupReferenceGroups(activeLine, resolvedCharacters),
    [activeLine, resolvedCharacters]
  );
  const showBackupReferenceSource = inspectorBackupReferenceVisible(activeProvider, candidateReferenceGroups.length);
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
  const displayedLines = useMemo(() => filteredLines.slice(0, visibleLineCount), [filteredLines, visibleLineCount]);
  const hasMoreFilteredLines = displayedLines.length < filteredLines.length;
  const selectedLines = useMemo(() => project.lines.filter((line) => selectedLineIds.includes(line.id)), [project.lines, selectedLineIds]);
  const providerOptions = useMemo(() => Array.from(new Set(project.lines.map((line) => lineBinding(line, resolvedCharacters)?.provider_type ?? "unassigned"))), [project.lines, resolvedCharacters]);
  const lineToolbarState = lineFilterToolbarState({
    providerFilter,
    statusFilter,
    selectedLineCount: selectedLineIds.length,
    filteredLineCount: filteredLines.length,
    labels: {
      filtersMore: t("filters.more"),
      selectedLines: (count) => t("table.selectedLines", { count }),
      visibleLines: (count) => t("table.visibleLines", { count }),
      status: (status) => statusText(status, t)
    }
  });
  const lineFilterTitle = lineToolbarState.title;
  const visibleLineLabel = lineToolbarState.countLabel;
  const selectedLanguage = normalizeLanguage(i18n.resolvedLanguage ?? i18n.language ?? defaultLanguage);
  const selectedLanguageLabel = languageOptions.find((option) => option.value === selectedLanguage)?.label ?? selectedLanguage;
  const projectRows = useMemo<ProjectSummary[]>(() => projectSummaries, [projectSummaries]);

  useEffect(() => {
    setManagedProjectId((current) => {
      if (current && projectRows.some((item) => item.project_id === current)) return current;
      return currentProjectId ?? projectRows[0]?.project_id ?? null;
    });
  }, [currentProjectId, projectRows]);

  useEffect(() => {
    if (!managedProjectId) {
      setManagedProject(null);
      setManagerTitleDraft("");
      setManagerSourceDraft("");
      setIsManagedProjectLoading(false);
      return;
    }
    let cancelled = false;
    setIsManagedProjectLoading(true);
    const projectPromise = managedProjectId === currentProjectId && isProjectLoaded
      ? Promise.resolve(projectWithCharacters)
      : fetchProject(managedProjectId);
    projectPromise
      .then((payload) => {
        if (cancelled) return;
        setManagedProject(payload);
        setManagerTitleDraft(payload.title);
        setManagerSourceDraft(projectToScriptSourceText(payload, characters));
      })
      .catch(() => {
        if (cancelled) return;
        setManagedProject(null);
        setManagerTitleDraft("");
        setManagerSourceDraft("");
        setNotice(t("empty.projectLoadFailed"));
      })
      .finally(() => {
        if (!cancelled) setIsManagedProjectLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [characters, currentProjectId, isProjectLoaded, managedProjectId, projectWithCharacters, setNotice, t]);

  useEffect(() => {
    setVisibleLineCount(LINE_LOAD_BATCH_SIZE);
  }, [characterFilter, currentProjectId, providerFilter, searchText, statusFilter]);

  useEffect(() => {
    if (!hasMoreFilteredLines) return;
    if (typeof IntersectionObserver === "undefined") {
      setVisibleLineCount(filteredLines.length);
      return;
    }
    const target = lineLoadMoreRef.current;
    if (!target) return;
    const root = target.closest(".line-table");
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      setVisibleLineCount((current) => Math.min(filteredLines.length, current + LINE_LOAD_BATCH_SIZE));
    }, {
      root,
      rootMargin: "160px 0px"
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [filteredLines.length, hasMoreFilteredLines]);

  const visibleServices = useMemo(() => services.filter((service) => !isUnsupportedLocalVibeVoice(service)), [services]);
  const ttsServices = useMemo(() => visibleServices.filter((service) => service.service_kind !== "llm-parser"), [visibleServices]);
  const roleLibraryTtsServices = useMemo(() => roleLibraryServiceOptions(ttsServices), [ttsServices]);
  const roleLibraryCatalogServices = useMemo(() => catalogServiceOptions(ttsServices), [ttsServices]);
  const selectedModelCatalogServiceId = useMemo(
    () => selectedCatalogServiceId(selectedLogsServiceId, roleLibraryCatalogServices),
    [roleLibraryCatalogServices, selectedLogsServiceId]
  );
  const gptSovitsBindingServiceOptions = useMemo(
    () => roleLibraryTtsServices.filter((service) => service.providerType === "gpt-sovits"),
    [roleLibraryTtsServices]
  );
  const serviceById = useMemo(() => new Map(visibleServices.map((service) => [service.service_id ?? "", service])), [visibleServices]);
  const activeService = activeServiceId ? serviceById.get(activeServiceId) : undefined;
  const activeProfileValue = activeLine ? activeVersionDraft?.profile ?? lineProfile(activeLine, resolvedCharacters) : "";
  const activeProfileLabel = activeLine?.temporary_binding
    ? t("inspector.temporaryBinding")
    : activeProfiles.find((profile) => profile.id === activeProfileValue)?.name || activeProfileValue || t("inspector.noProfile");
  const activeBindingLabel = activeVersionDraft?.binding_id
    ?? activeBinding?.binding_id
    ?? (activeLine?.temporary_binding ? `${t("inspector.temporaryBinding")} · ${activeLine.temporary_binding.provider_type}` : t("inspector.profileDefault"));
  const activeServiceLabel = activeServiceId
    ? serviceDisplayName(activeService ?? ({ engine: activeProvider, display_name: activeServiceId, ready: false } as WorkerHealth))
    : t("inspector.autoRoute");
  const activeServiceContract = activeService?.api_contract ?? activeProvider;
  const localServiceCount = useMemo(() => visibleServices.filter((service) => ["gpt-sovits", "indextts"].includes(service.provider_type ?? service.engine)), [visibleServices]);
  const paidServiceCount = useMemo(() => visibleServices.filter((service) => service.capabilities?.includes("paid_provider")), [visibleServices]);
  const serviceSummary = useMemo(() => serviceTopbarSummary(visibleServices, voiceCandidates, parserProviders), [parserProviders, visibleServices, voiceCandidates]);
  const serviceHealthItems = useMemo(() => serviceTopbarHealthItems(serviceSummary), [serviceSummary]);
  const selectedConfigService = useMemo(
    () => ttsServices.find((service) => service.service_id === expandedServiceConfigId) ?? ttsServices[0],
    [expandedServiceConfigId, ttsServices]
  );
  const runningServiceIds = useMemo(() => {
    const ids = new Set<string>();
    for (const item of activeJob?.items ?? []) {
      if (item.service_id && ["loading", "running", "finalizing"].includes(item.status)) ids.add(item.service_id);
    }
    return ids;
  }, [activeJob]);
  const activeRouteServices = useMemo(() => routableProviderServices(visibleServices, activeProvider), [activeProvider, visibleServices]);
  const activeSelectedServiceUnavailable = Boolean(activeServiceId && !activeRouteServices.some((service) => service.service_id === activeServiceId));
  const selectedOpenSourceCatalog = useMemo(
    () => openSourceCatalog.find((item) => item.provider_type === selectedOpenSourceProvider) ?? openSourceCatalog[0],
    [openSourceCatalog, selectedOpenSourceProvider]
  );
  const ttsHealthItems = useMemo(
    () => serviceHealthItems.filter((item) => item.id === "local" || item.id === "paid"),
    [serviceHealthItems]
  );
  const llmHealthItem = useMemo(
    () => serviceHealthItems.find((item) => item.id === "parser"),
    [serviceHealthItems]
  );
  const queueJobs = useMemo(() => queueStatus?.jobs ?? [], [queueStatus]);
  const queueItems = useMemo(() => queueJobs.flatMap((job) => job.items), [queueJobs]);
  const queueRunningItems = queueStatus?.running ?? queueItems.filter((item) => ["loading", "running", "finalizing"].includes(item.status)).length;
  const queueQueuedItems = queueStatus?.queued ?? queueItems.filter((item) => item.status === "queued").length;
  const queueCompletedItems = queueItems.filter((item) => item.status === "completed").length;
  const queueFailedItems = queueItems.filter((item) => item.status === "failed" || item.status === "cancelled").length;
  const queueProcessedItems = queueCompletedItems + queueFailedItems;
  const queueTotalItems = Math.max(queueItems.length, queueProcessedItems + queueRunningItems + queueQueuedItems);
  const queueActiveJob = activeJob && !["completed", "failed", "cancelled"].includes(activeJob.status)
    ? activeJob
    : queueJobs.find((job) => !["completed", "failed", "cancelled"].includes(job.status)) ?? null;
  const queueProgressRatio = queueActiveJob
    ? queueActiveJob.progress
    : queueTotalItems > 0
      ? queueProcessedItems / queueTotalItems
      : 0;
  const queueProgressPercent = Math.round(Math.max(0, Math.min(1, queueProgressRatio)) * 100);
  const queueHasWork = queueTotalItems > 0 || queueJobs.length > 0;
  const queueTopbarEntryVisible = queueHasWork || Boolean(queueActiveJob);
  const queueSyncLabel = isRefreshingTopology ? t("queue.polling") : t(queueStatus ? "queue.synced" : "queue.notSynced");
  const queueVisibleStatusLabel = queueActiveJob ? statusText(queueActiveJob.status, t) : queueSyncLabel;
  const queueVisibleTone = queueActiveJob ? queueStatusTone(queueActiveJob.status) : isRefreshingTopology ? "running" : "idle";
  const topologyModalTitle =
    servicePanelSection === "roles"
      ? t("characters.libraryManager")
      : servicePanelSection === "resources"
        ? t("services.resourceQueueTitle")
        : servicePanelSection === "llm"
          ? t("services.llmApiTitle")
          : t("services.ttsAccessTitle");
  const topologyModalDescription =
    servicePanelSection === "roles"
      ? t("characters.libraryHint")
      : servicePanelSection === "resources"
        ? t("services.resourceQueueDescription")
        : servicePanelSection === "llm"
          ? t("services.llmApiDescription")
          : t("services.ttsAccessDescription");
  const topologyModalClass =
    servicePanelSection === "roles"
      ? "role-library-modal"
      : servicePanelSection === "resources"
        ? "resource-queue-modal"
        : servicePanelSection === "llm"
          ? "llm-api-modal"
          : "service-access-modal";

  const configuredOpenSourceServices = useMemo(
    () => ttsServices.filter((service) => (service.catalog_provider ?? service.provider_type) === selectedOpenSourceProvider),
    [selectedOpenSourceProvider, ttsServices]
  );

  useEffect(() => {
    if (!selectedOpenSourceCatalog) return;
    setSelectedOpenSourceProvider(selectedOpenSourceCatalog.provider_type);
    setOpenSourceBaseUrl(selectedOpenSourceCatalog.default_base_url);
    setOpenSourceResourceGroup(selectedOpenSourceCatalog.resource_group);
    setOpenSourceCapacity(1);
    setOpenSourceDisplayName(selectedOpenSourceCatalog.display_name);
    setOpenSourceDetectResult(null);
  }, [selectedOpenSourceCatalog?.provider_type]);

  useEffect(() => {
    if (!activeServiceId) return;
    fetchServiceLoadState(activeServiceId)
      .then((state) => setServiceLoadStates((current) => ({ ...current, [activeServiceId]: state })))
      .catch(() => undefined);
  }, [activeServiceId, activeJob?.updated_at, activeVersions.length]);

  useEffect(() => {
    if (!activeLogsReferenceRequest) return;
    if (logsReferenceAudio[activeLogsReferenceRequest.key]) return;
    setLoadingLogsReferenceKey(activeLogsReferenceRequest.key);
    const referenceService = (activeLogsReferenceRequest.serviceId ? serviceById.get(activeLogsReferenceRequest.serviceId) : undefined)
      ?? activeRouteServices.find(isGptSovitsApiV2Service);
    const referenceServiceId = activeLogsReferenceRequest.serviceId || referenceService?.service_id || null;
    const referenceRequest = isGptSovitsApiV2Service(referenceService)
      ? fetchGptSovitsModelSamples({
        serviceId: referenceServiceId,
        logsName: activeLogsReferenceRequest.logsName,
        limit: 120
      })
      : fetchLogsReferenceAudio({
        serviceId: referenceServiceId,
        logsName: activeLogsReferenceRequest.logsName,
        gptWeightsPath: activeLogsReferenceRequest.gptWeightsPath,
        sovitsWeightsPath: activeLogsReferenceRequest.sovitsWeightsPath,
      });
    referenceRequest
      .then((payload) => setLogsReferenceAudio((current) => ({ ...current, [activeLogsReferenceRequest.key]: payload })))
      .catch(() => setLogsReferenceAudio((current) => ({
        ...current,
        [activeLogsReferenceRequest.key]: {
          service_id: activeLogsReferenceRequest.serviceId,
          logs_name: activeLogsReferenceRequest.logsName,
          samples: [],
          diagnostics: [{ status: "unreachable", detail: t("inspector.logsReferenceLoadFailed") }],
        }
      })))
      .finally(() => setLoadingLogsReferenceKey((current) => (current === activeLogsReferenceRequest.key ? null : current)));
  }, [activeLogsReferenceRequest, activeRouteServices, logsReferenceAudio, serviceById, t]);

  useEffect(() => {
    if (!activeModelCatalogItem || !activeModelSamplesKey) return;
    if (modelCatalogSamples[activeModelSamplesKey]) return;
    setLoadingModelCatalogSamplesKey(activeModelSamplesKey);
    fetchGptSovitsModelSamples({
      serviceId: (activeModelCatalogItem.service_id ?? selectedModelCatalogServiceId) || null,
      logsName: activeModelCatalogItem.logs_name ?? activeModelCatalogItem.name,
      limit: 40
    })
      .then((payload) => setModelCatalogSamples((current) => ({ ...current, [activeModelSamplesKey]: payload })))
      .catch(() => setModelCatalogSamples((current) => ({
        ...current,
        [activeModelSamplesKey]: {
          service_id: (activeModelCatalogItem.service_id ?? selectedModelCatalogServiceId) || null,
          logs_name: activeModelCatalogItem.logs_name ?? activeModelCatalogItem.name,
          samples: [],
          diagnostics: [{ status: "unreachable", detail: t("inspector.logsReferenceLoadFailed") }],
        }
      })))
      .finally(() => setLoadingModelCatalogSamplesKey((current) => (current === activeModelSamplesKey ? null : current)));
  }, [activeModelCatalogItem, activeModelSamplesKey, modelCatalogSamples, selectedModelCatalogServiceId, t]);

  const selectedParserProvider = parserProviders[selectedParserProviderIndex];
  const kwjmParserProviderIndex = useMemo(() => parserProviders.findIndex(isKwjmParserProvider), [parserProviders]);
  const kwjmParserProvider = kwjmParserProviderIndex >= 0 ? parserProviders[kwjmParserProviderIndex] : createDefaultParserProviderDraft();
  const kwjmHasUsableKey = parserProviderHasUsableKey(kwjmParserProvider);
  const kwjmActivationState: ParserProviderState = kwjmHasUsableKey ? (kwjmParserProvider.enabled ? "ready" : "disabled") : "partial";
  const kwjmCanActivate = Boolean(kwjmApiKeyInput.trim() || kwjmHasUsableKey);
  const kwjmDisplayTestResult = kwjmParserTestResult ?? (kwjmParserProviderIndex >= 0 ? parserProviderTestResults[kwjmParserProviderIndex] ?? null : null);
  useEffect(() => {
    if (!selectedLogsServiceId) return;
    if (!roleLibraryCatalogServices.some((option) => option.serviceId === selectedLogsServiceId)) {
      setSelectedLogsServiceId("");
    }
  }, [roleLibraryCatalogServices, selectedLogsServiceId]);

  async function refreshTopology(reloadConfig = false) {
    setIsRefreshingTopology(true);
    try {
      if (reloadConfig) {
        await reloadServiceSettings().catch(() => null);
      }
      const [servicePayload, settingsPayload, runtimePayload, candidatePayload, queuePayload] = await Promise.all([
        fetchServicesStatus().catch(() => fetchServices().catch(() => ({ services: [] }))),
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

  async function refreshOpenSourceCatalog() {
    try {
      const payload = await fetchOpenSourceTTSCatalog();
      setOpenSourceCatalog(payload.providers);
    } catch {
      setOpenSourceCatalog([]);
    }
  }

  async function runOpenSourceDetect() {
    setIsDetectingOpenSource(true);
    try {
      const payload = await detectOpenSourceTTS({
        provider_type: selectedOpenSourceProvider,
        repo_path: null,
        base_url: openSourceBaseUrl || null,
        api_contract: gradioContractForProvider(selectedOpenSourceProvider)
      });
      setOpenSourceDetectResult(payload);
      setNotice(t("services.openSourceDetectDone", { state: setupStateLabel(payload.setup_state, t) }));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("services.openSourceDetectFailed"));
    } finally {
      setIsDetectingOpenSource(false);
    }
  }

  async function saveOpenSourceService() {
    setIsConfiguringOpenSource(true);
    try {
      const payload = await configureOpenSourceTTS({
        ...buildGradioEndpointRequest({
          provider_type: selectedOpenSourceProvider,
          display_name: openSourceDisplayName || null,
          base_url: openSourceBaseUrl,
          resource_group: openSourceResourceGroup,
          capacity: openSourceCapacity,
          enabled: openSourceDetectResult ? ["partial", "ready"].includes(openSourceDetectResult.setup_state) : false,
        })
      });
      setOpenSourceDetectResult(payload.detect);
      setNotice(t("services.openSourceSaved"));
      await refreshTopology(true);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("services.openSourceSaveFailed"));
    } finally {
      setIsConfiguringOpenSource(false);
    }
  }

  async function detectAndSaveOpenSourceService() {
    setIsDetectingOpenSource(true);
    setIsConfiguringOpenSource(true);
    try {
      const detectPayload = await detectOpenSourceTTS({
        provider_type: selectedOpenSourceProvider,
        repo_path: null,
        base_url: openSourceBaseUrl || null,
        api_contract: gradioContractForProvider(selectedOpenSourceProvider)
      });
      setOpenSourceDetectResult(detectPayload);
      if (!["partial", "ready"].includes(detectPayload.setup_state)) {
        setNotice(t("services.openSourceDetectNotSaved", { state: setupStateLabel(detectPayload.setup_state, t) }));
        return;
      }
      const payload = await configureOpenSourceTTS({
        ...buildGradioEndpointRequest({
          provider_type: selectedOpenSourceProvider,
          display_name: openSourceDisplayName || null,
          base_url: openSourceBaseUrl,
          resource_group: openSourceResourceGroup,
          capacity: openSourceCapacity,
          enabled: true,
        })
      });
      setOpenSourceDetectResult(payload.detect);
      setNotice(t("services.openSourceDetectAndSaveDone", { state: setupStateLabel(payload.detect.setup_state, t) }));
      await refreshTopology(true);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("services.openSourceSaveFailed"));
    } finally {
      setIsDetectingOpenSource(false);
      setIsConfiguringOpenSource(false);
    }
  }

  async function refreshProjects(preferredProjectId?: string | null) {
    try {
      const payload = await fetchProjects();
      setProjectSummaries(payload.projects);
      setCurrentProjectId((current) => {
        const preferred = preferredProjectId !== undefined ? preferredProjectId : current ?? readStoredProjectId();
        const next = selectStartupProjectId(payload.projects, preferred);
        writeStoredProjectId(next);
        return next;
      });
    } catch {
      setProjectSummaries([]);
      setCurrentProjectId(null);
      writeStoredProjectId(null);
    }
  }

  async function createNewScriptProject() {
    const title = newScriptTitle.trim();
    const source = newScriptSource.trim();
    if (!title) {
      setNotice(t("script.newScriptTitleRequired"));
      return;
    }
    const projectId = createProjectId(title);
    const nextProject: ScriptProject = { ...createEmptyProject(), title };
    setIsCreatingScript(true);
    setSaveState("saving");
    try {
      await saveProject(projectId, nextProject);
      const savedProject = source
        ? (await createScriptRevision(projectId, source, t("script.initialScriptRevision"))).project
        : nextProject;
      setCurrentProjectId(projectId);
      writeStoredProjectId(projectId);
      setProject(savedProject);
      setManifest(createEmptyManifest(projectId));
      setActiveLineId("");
      setExpandedLineId(null);
      setSelectedLineIds([]);
      setSelectedHistoryVersions({});
      setVersionDrafts({});
      setManagedProjectId(projectId);
      setManagedProject(savedProject);
      setManagerTitleDraft(savedProject.title);
      setManagerSourceDraft(source || projectToScriptSourceText(savedProject, characters));
      setNewScriptTitle("");
      setNewScriptSource("");
      setSaveState("saved");
      setLastSavedAt(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
      setNotice(t("script.newScriptCreated"));
      await refreshProjects(projectId);
    } catch (error) {
      setSaveState("error");
      setNotice(error instanceof Error ? error.message : t("script.newScriptCreateFailed"));
    } finally {
      setIsCreatingScript(false);
    }
  }

  async function refreshParserProviders() {
    try {
      const payload = await fetchParserProviders();
      setParserProviders(normalizeParserProviderDrafts(payload.providers).map((provider) => ({ ...provider, api_key: "" })));
    } catch {
      setParserProviders([]);
    }
  }

  async function confirmRevisionRisk(targetProject: ScriptProject = project): Promise<boolean> {
    if (!shouldRequestRevisionConfirmation(targetProject.script_revisions?.length ?? 0, targetProject.parse_revisions?.length ?? 0)) return true;
    return requestConfirmation({
      title: t("confirm.revision.title"),
      body: t("script.revisionRisk"),
      detail: t("confirm.revision.detail"),
      confirmLabel: t("confirm.revision.confirm"),
      cancelLabel: t("actions.cancel"),
      tone: "warning"
    });
  }

  async function activateProjectRevision(parseRevisionId: string) {
    const revision = project.parse_revisions?.find((item) => item.revision_id === parseRevisionId);
    if (!revision) return;
    if (!(await confirmRevisionRisk())) return;
    setProject((current) => ({
      ...current,
      active_script_revision_id: revision.script_revision_id,
      active_parse_revision_id: revision.revision_id,
      project_characters: revision.project_characters,
      lines: revision.lines
    }));
    setActiveLineId(revision.lines[0]?.id ?? "");
    setExpandedLineId(null);
    setSelectedLineIds([]);
  }

  function updateParserProvider(index: number, patch: Partial<ParserProviderDraft>) {
    setParserProviders((current) => current.map((provider, itemIndex) => (itemIndex === index ? { ...provider, ...patch } : provider)));
  }

  function addParserProvider() {
    const next = parserProviders.length + 1;
    setParserProviders((current) => [...current, createDefaultParserProviderDraft(next)]);
    setSelectedParserProviderIndex(parserProviders.length);
  }

  async function saveParserProviderSettings() {
    setIsSavingParserConfig(true);
    try {
      const payload = await saveParserProviders(toParserProviderSavePayload(parserProviders));
      setParserProviders(normalizeParserProviderDrafts(payload.providers).map((provider) => ({ ...provider, api_key: "" })));
      setNotice(t("notice.parserConfigSaved"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.parserConfigFailed"));
    } finally {
      setIsSavingParserConfig(false);
    }
  }

  async function activateKwjmParserProvider() {
    const nextProviders = upsertKwjmParserProvider(parserProviders, kwjmApiKeyInput);
    setIsSavingParserConfig(true);
    try {
      const payload = await saveParserProviders(toParserProviderSavePayload(nextProviders));
      setParserProviders(normalizeParserProviderDrafts(payload.providers).map((provider) => ({ ...provider, api_key: "" })));
      setKwjmApiKeyInput("");
      setNotice(t("notice.parserConfigSaved"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.parserConfigFailed"));
    } finally {
      setIsSavingParserConfig(false);
    }
  }

  async function testKwjmParserProviderSettings() {
    const provider = upsertKwjmParserProvider(parserProviders, kwjmApiKeyInput).find(isKwjmParserProvider);
    if (!provider) return;
    setTestingParserProviderIndex(KWJM_TESTING_INDEX);
    try {
      const result = await testParserProvider(toParserProviderSavePayload([provider]).providers[0]);
      setKwjmParserTestResult(result);
      if (kwjmParserProviderIndex >= 0) {
        setParserProviderTestResults((current) => ({ ...current, [kwjmParserProviderIndex]: result }));
      }
      setNotice(result.ok ? t("notice.parserProviderTestReady", { provider: provider.name }) : t("notice.parserProviderTestFailed", { provider: provider.name }));
    } catch (error) {
      const result: ParserProviderTestResponse = {
        ok: false,
        state: "blocked",
        message: error instanceof Error ? error.message : t("notice.parserProviderTestFailed", { provider: provider.name }),
        provider: provider.name,
      };
      setKwjmParserTestResult(result);
      setNotice(result.message);
    } finally {
      setTestingParserProviderIndex(null);
    }
  }

  async function testParserProviderSettings(index: number) {
    const provider = parserProviders[index];
    if (!provider) return;
    setTestingParserProviderIndex(index);
    try {
      const result = await testParserProvider(toParserProviderSavePayload([provider]).providers[0]);
      setParserProviderTestResults((current) => ({ ...current, [index]: result }));
      setNotice(result.ok ? t("notice.parserProviderTestReady", { provider: provider.name }) : t("notice.parserProviderTestFailed", { provider: provider.name }));
    } catch (error) {
      const result: ParserProviderTestResponse = {
        ok: false,
        state: "blocked",
        message: error instanceof Error ? error.message : t("notice.parserProviderTestFailed", { provider: provider.name }),
        provider: provider.name,
      };
      setParserProviderTestResults((current) => ({ ...current, [index]: result }));
      setNotice(result.message);
    } finally {
      setTestingParserProviderIndex(null);
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
    if (!currentProjectId) return;
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
    if (!currentProjectId) {
      setNotice(t("empty.noProjectAction"));
      return;
    }
    setIsGenerating(true);
    generationAbortRef.current = false;
    setNotice(t("notice.generating"));
    try {
      const { tasks, blocked } = buildRunnableTasks(lines, resolvedCharacters);
      if (blocked.length > 0) {
        setNotice(t("notice.linesNeedBinding", { count: blocked.length }), { level: "warning" });
      }
      if (tasks.length === 0) return;
      const preflight = await ensureGenerationPreflight(tasks);
      if (preflight.status !== "ready") return;
      const job = await createGenerationJob(currentProjectId, tasks);
      setActiveJob(job);
      setNotice(t("notice.jobQueued", { job: job.job_id }));
      const finalJob = await pollGenerationJob(job.job_id);
      setActiveJob(finalJob);
      const nextManifest = await fetchManifest(currentProjectId);
      setManifest(nextManifest);
      if (generationAbortRef.current || finalJob.status === "cancelled") {
        setNotice(t("notice.generationCancelled"), { level: "warning" });
      } else {
        setNotice(finalJob.status === "completed" ? t("notice.generated") : t("notice.generationFailed"));
      }
      await refreshTopology();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.generationFailed"));
    } finally {
      setIsGenerating(false);
      generationAbortRef.current = false;
    }
  }

  async function ensureGenerationPreflight(tasks: GenerationTask[]): Promise<GenerationPreflightResponse> {
    if (!currentProjectId) {
      throw new Error(t("empty.noProjectAction"));
    }
    let preflight = await generationPreflight(currentProjectId, tasks);
    setPreflightResult(preflight);
    await refreshLoadStatesForPreflight(preflight);
    if (preflight.status === "ready") return preflight;
    const fallbackActions = Array.from(
      new Map(
        preflight.items
          .map((item) => preflightFallbackAction(item, visibleServices))
          .filter((item): item is NonNullable<typeof item> => Boolean(item))
          .map((item) => [item.serviceId, item])
      ).values()
    );
    if (preflight.status === "needs_user_action" && fallbackActions.length > 0) {
      const first = fallbackActions[0];
      const confirmed = await requestConfirmation({
        title: t("confirm.fallback.title"),
        body: t("notice.preflightNeedsFallback", { service: first.serviceName }),
        detail: t("confirm.fallback.detail"),
        confirmLabel: t("confirm.fallback.confirm"),
        cancelLabel: t("actions.cancel"),
        tone: "warning"
      });
      if (!confirmed) {
        setNotice(t("notice.preflightBlocked", { reason: preflight.items.find((item) => item.reason)?.reason ?? first.serviceName }));
        return preflight;
      }
      try {
        for (const action of fallbackActions) {
          setNotice(t("actions.starting", { service: action.serviceName }));
          await startAndWaitService(action.serviceId);
        }
        setNotice(t("notice.fallbackStarted"));
        await refreshTopology();
        preflight = await generationPreflight(currentProjectId, tasks);
        setPreflightResult(preflight);
        await refreshLoadStatesForPreflight(preflight);
      } catch (error) {
        setNotice(error instanceof Error ? error.message : t("notice.fallbackStartFailed"));
        return preflight;
      }
    }
    if (preflight.status !== "ready") {
      const blockedReason = preflight.items.find((item) => item.reason)?.reason ?? t("status.needsSetup");
      setNotice(t("notice.preflightBlocked", { reason: blockedReason }));
    }
    return preflight;
  }

  async function refreshLoadStatesForPreflight(preflight: GenerationPreflightResponse) {
    const serviceIds = Array.from(new Set(preflight.items.map((item) => item.selected_service_id).filter((item): item is string => Boolean(item))));
    if (serviceIds.length === 0) return;
    const states = await Promise.all(
      serviceIds.map((serviceId) => fetchServiceLoadState(serviceId).then((state) => [serviceId, state] as const).catch(() => null))
    );
    setServiceLoadStates((current) => {
      const next = { ...current };
      for (const entry of states) {
        if (entry) next[entry[0]] = entry[1];
      }
      return next;
    });
  }

  async function runSelectedQueue() {
    await runQueue(selectedLines.length > 0 ? selectedLines : filteredLines);
  }

  async function pollGenerationJob(jobId: string): Promise<GenerationJob> {
    for (let attempt = 0; attempt < 240; attempt += 1) {
      if (generationAbortRef.current) {
        const cancelled = await fetchGenerationJob(jobId);
        setActiveJob(cancelled);
        return cancelled;
      }
      const job = await fetchGenerationJob(jobId);
      setActiveJob(job);
      if (["completed", "failed", "cancelled"].includes(job.status)) return job;
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    throw new Error(t("notice.jobTimeout"));
  }

  async function cancelGeneration() {
    const jobId = activeJob?.job_id;
    generationAbortRef.current = true;
    if (!jobId) return;
    try {
      const cancelled = await cancelGenerationJob(jobId);
      setActiveJob(cancelled);
      setNotice(t("notice.generationCancelling"), { level: "warning" });
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.generationFailed"));
    }
  }

  function switchProject(projectId: string) {
    if (projectId === currentProjectId) return;
    setCurrentProjectId(projectId);
    writeStoredProjectId(projectId);
    setSelectedLineIds([]);
    setExpandedLineId(null);
    setSelectedHistoryVersions({});
    setVersionDrafts({});
    setNotice(t("app.ready"));
  }

  function applyManagedProjectToWorkspace(projectId: string, nextProject: ScriptProject, resetLineState = false) {
    if (projectId !== currentProjectId) return;
    setProject(nextProject);
    if (resetLineState) {
      setActiveLineId(nextProject.lines[0]?.id ?? "");
      setExpandedLineId(null);
      setSelectedLineIds([]);
      setSelectedHistoryVersions({});
      setVersionDrafts({});
    }
  }

  async function renameManagedProject() {
    if (!managedProjectId || !managedProject) return;
    const title = managerTitleDraft.trim();
    if (!title) {
      setNotice(t("script.newScriptTitleRequired"));
      return;
    }
    const nextProject = { ...managedProject, title };
    setIsManagerSaving(true);
    try {
      await saveProject(managedProjectId, nextProject);
      setManagedProject(nextProject);
      applyManagedProjectToWorkspace(managedProjectId, nextProject);
      setSaveState("saved");
      setNotice(t("notice.projectRenamed"));
      await refreshProjects(currentProjectId);
    } catch (error) {
      setSaveState("error");
      setNotice(error instanceof Error ? error.message : t("notice.autoSaveFailed"));
    } finally {
      setIsManagerSaving(false);
    }
  }

  async function saveManagedScriptRevision() {
    if (!managedProjectId || !managedProject) return;
    const title = managerTitleDraft.trim();
    const source = managerSourceDraft.trim();
    if (!title) {
      setNotice(t("script.newScriptTitleRequired"));
      return;
    }
    if (!source) {
      setNotice(t("script.sourceRequired"));
      return;
    }
    if (!(await confirmRevisionRisk(managedProject))) return;
    setIsManagerSaving(true);
    try {
      if (title !== managedProject.title) {
        await saveProject(managedProjectId, { ...managedProject, title });
      }
      const payload = await createScriptRevision(managedProjectId, source, t("script.currentSource"));
      setManagedProject(payload.project);
      setManagerTitleDraft(payload.project.title);
      setManagerSourceDraft(projectToScriptSourceText(payload.project, characters));
      applyManagedProjectToWorkspace(managedProjectId, payload.project);
      setSaveState("saved");
      setNotice(t("notice.projectSaved"));
      await refreshProjects(currentProjectId);
    } catch (error) {
      setSaveState("error");
      setNotice(error instanceof Error ? error.message : t("notice.autoSaveFailed"));
    } finally {
      setIsManagerSaving(false);
    }
  }

  async function parseManagedScriptRevision() {
    if (!managedProjectId || !managedProject) return;
    const title = managerTitleDraft.trim();
    const source = managerSourceDraft.trim();
    if (!title) {
      setNotice(t("script.newScriptTitleRequired"));
      return;
    }
    if (!source) {
      setNotice(t("script.sourceRequired"));
      return;
    }
    if (!(await confirmRevisionRisk(managedProject))) return;
    setIsManagerParsing(true);
    setNotice(t("parser.parsing"));
    try {
      if (title !== managedProject.title) {
        await saveProject(managedProjectId, { ...managedProject, title });
      }
      const scriptPayload = await createScriptRevision(managedProjectId, source, t("script.parseRevision"));
      const parsePayload = await createParseRevision(managedProjectId, scriptPayload.script_revision.revision_id);
      setManagedProject(parsePayload.project);
      setManagerTitleDraft(parsePayload.project.title);
      setManagerSourceDraft(projectToScriptSourceText(parsePayload.project, characters));
      applyManagedProjectToWorkspace(managedProjectId, parsePayload.project, true);
      setNotice(t("script.parseApplied"));
      await refreshProjects(currentProjectId);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("parser.parseFailed"));
    } finally {
      setIsManagerParsing(false);
    }
  }

  async function deleteManagedProject() {
    if (!managedProjectId) return;
    const title = managerTitleDraft.trim() || managedProject?.title || managedProjectId;
    const confirmed = await requestConfirmation({
      title: t("script.deleteScriptTitle"),
      body: t("script.deleteScriptBody", { title }),
      detail: t("script.deleteScriptDetail"),
      confirmLabel: t("script.deleteScript"),
      cancelLabel: t("actions.cancel"),
      tone: "danger"
    });
    if (!confirmed) return;
    const allRows = filterAndSortProjectSummaries(projectRows, "");
    const visibleRows = filterAndSortProjectSummaries(projectRows, managerSearchText);
    const nextCurrentProjectId = nextProjectAfterDelete(allRows, managedProjectId, currentProjectId);
    const nextManagedProjectId = nextProjectAfterDelete(visibleRows, managedProjectId, managedProjectId) ?? nextCurrentProjectId;
    const deletedCurrentProject = managedProjectId === currentProjectId;
    setDeletingProjectId(managedProjectId);
    try {
      await deleteProject(managedProjectId);
      setManagedProjectId(nextManagedProjectId);
      setManagedProject(null);
      if (deletedCurrentProject) {
        setCurrentProjectId(nextCurrentProjectId);
        writeStoredProjectId(nextCurrentProjectId);
        if (!nextCurrentProjectId) {
          setProject(createEmptyProject());
          setManifest(createEmptyManifest(null));
          setActiveLineId("");
          setExpandedLineId(null);
          setSelectedLineIds([]);
          setSelectedHistoryVersions({});
          setVersionDrafts({});
        }
      }
      setNotice(t("notice.projectDeleted"));
      await refreshProjects(deletedCurrentProject ? nextCurrentProjectId : currentProjectId);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.projectDeleteFailed"));
    } finally {
      setDeletingProjectId(null);
    }
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

  function playLine(line: ScriptLine) {
    const latest = newestPlayableVersion(lineHistoryForLine(manifest, line)?.versions ?? []);
    if (!latest?.audio_path) {
      setNotice(t("empty.noPlayableVersion"));
      return;
    }
    const audio = new Audio(`/api/audio?path=${encodeURIComponent(latest.audio_path)}`);
    void audio.play();
  }

  function focusLine(lineId: string) {
    const next = lineFocusTransition({ activeLineId, expandedLineId }, lineId, "card");
    setActiveLineId(next.activeLineId ?? "");
    setExpandedLineId(next.expandedLineId);
    setSelectedHistoryVersions((current) => {
      if (!current[lineId]) return current;
      const nextVersions = { ...current };
      delete nextVersions[lineId];
      return nextVersions;
    });
    setVersionDrafts((current) => {
      if (!current[lineId]) return current;
      const nextDrafts = { ...current };
      delete nextDrafts[lineId];
      return nextDrafts;
    });
  }

  function selectGenerationProvider(provider: ProviderType) {
    if (!activeLine) return;
    if (activeVersionDraft) {
      updateActiveVersionDraft({
        provider_type: provider,
        parameters: { ...defaultTemporaryConfig(provider, activeLine), ...activeVersionDraft.parameters }
      });
      return;
    }
    setTemporaryBindingProvider(activeLine.id, provider);
  }

  function selectGenerationMethod(methodId: GenerationMethodId) {
    if (methodId === "gpt-sovits") {
      selectGenerationProvider("gpt-sovits");
      return;
    }
    if (methodId === "indextts") {
      selectGenerationProvider("indextts");
      return;
    }
    if (methodId === "cosyvoice") {
      selectGenerationProvider("cosyvoice");
      return;
    }
    if (!["openai", "gemini", "xai", "volcengine"].includes(activeProvider)) {
      selectGenerationProvider("openai");
    }
  }

  function selectHistoryVersion(lineId: string, version: GenerationVersion) {
    setActiveLineId(lineId);
    setExpandedLineId(lineId);
    setSelectedHistoryVersions((current) => ({ ...current, [lineId]: version.version_id }));
    setVersionDrafts((current) => ({
      ...current,
      [lineId]: { ...versionToInspectorDraft(version), version_id: version.version_id }
    }));
  }

  async function removeHistoryVersion(line: ScriptLine, version: GenerationVersion) {
    if (!currentProjectId) return;
    const confirmed = await requestConfirmation({
      title: t("history.deleteTitle"),
      body: t("history.deleteBody", { version: version.version_id }),
      detail: version.audio_path ? shortPath(version.audio_path) : undefined,
      confirmLabel: t("history.deleteConfirm"),
      cancelLabel: t("actions.cancel"),
      tone: "danger",
    });
    if (!confirmed) return;
    try {
      const lineKey = line.line_uid ?? line.id;
      const payload = await deleteGenerationVersion(currentProjectId, lineKey, version.version_id);
      const nextManifest = await fetchManifest(currentProjectId);
      setManifest(nextManifest);
      if (selectedHistoryVersions[line.id] === version.version_id) {
        clearSelectedHistoryVersion(line.id);
      }
      setNotice(payload.warning ? t("notice.generationVersionDeletedWithWarning", { warning: payload.warning }) : t("notice.generationVersionDeleted"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.generationVersionDeleteFailed"));
    }
  }

  function clearSelectedHistoryVersion(lineId: string) {
    setSelectedHistoryVersions((current) => {
      const next = { ...current };
      delete next[lineId];
      return next;
    });
    setVersionDrafts((current) => {
      const next = { ...current };
      delete next[lineId];
      return next;
    });
  }

  function updateActiveVersionDraft(patch: Partial<InspectorVersionDraft>) {
    if (!activeLine || !activeVersionDraft) return;
    setVersionDrafts((current) => ({
      ...current,
      [activeLine.id]: { ...activeVersionDraft, ...patch }
    }));
  }

  function updateLineTextDraft(lineId: string, text: string) {
    setLineTextDrafts((current) => ({ ...current, [lineId]: text }));
  }

  function resetLineTextDraft(lineId: string) {
    setLineTextDrafts((current) => {
      const next = { ...current };
      delete next[lineId];
      return next;
    });
  }

  function lineWithGenerationText(line: ScriptLine): ScriptLine {
    const draft = lineTextDrafts[line.id];
    if (draft === undefined || draft === line.text) return line;
    return { ...line, text: draft };
  }

  async function runInspectorGeneration() {
    if (!activeLine) return;
    const lineForGeneration = lineWithGenerationText(activeLine);
    if (!activeVersionDraft) {
      await runQueue([lineForGeneration]);
      return;
    }
    const provider = activeVersionDraft.provider_type ?? activeProvider;
    const lineFromDraft: ScriptLine = {
      ...lineForGeneration,
      engine_override: engineFromProvider(provider),
      profile_override: activeVersionDraft.profile,
      binding_override: null,
      service_override: activeVersionDraft.service_id ?? null,
      temporary_binding: {
        binding_id: activeVersionDraft.binding_id ?? `${activeLine.id}-${provider}-history-draft`,
        provider_type: provider,
        service_id: activeVersionDraft.service_id,
        fallback_services: [],
        capabilities: defaultCapabilitiesForProvider(provider),
        config: activeVersionDraft.parameters
      }
    };
    await runQueue([lineFromDraft]);
  }

  function toggleVisibleSelection() {
    const visibleIds = displayedLines.map((line) => line.id);
    const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedLineIds.includes(id));
    setSelectedLineIds(allVisibleSelected ? selectedLineIds.filter((id) => !visibleIds.includes(id)) : Array.from(new Set([...selectedLineIds, ...visibleIds])));
  }

  const scriptManagerPane = (
    <ScriptManagerModal
      open
      variant="inline"
      projects={projectRows}
      currentProjectId={currentProjectId}
      selectedProjectId={managedProjectId}
      selectedProject={managedProject}
      isSelectedProjectLoading={isManagedProjectLoading}
      searchText={managerSearchText}
      titleDraft={managerTitleDraft}
      sourceDraft={managerSourceDraft}
      newScriptTitle={newScriptTitle}
      newScriptSource={newScriptSource}
      isCreatingScript={isCreatingScript}
      isSavingScript={isManagerSaving}
      isParsingScript={isManagerParsing}
      deletingProjectId={deletingProjectId}
      onClose={() => undefined}
      onSearchTextChange={setManagerSearchText}
      onSelectProject={setManagedProjectId}
      onOpenProject={(projectId) => {
        switchProject(projectId);
        setManagedProjectId(projectId);
      }}
      onTitleDraftChange={setManagerTitleDraft}
      onSourceDraftChange={setManagerSourceDraft}
      onNewScriptTitleChange={setNewScriptTitle}
      onNewScriptSourceChange={setNewScriptSource}
      onCreateScript={() => void createNewScriptProject()}
      onRenameScript={() => void renameManagedProject()}
      onSaveRevision={() => void saveManagedScriptRevision()}
      onParseRevision={() => void parseManagedScriptRevision()}
      onDeleteScript={() => void deleteManagedProject()}
    />
  );

  return (
    <div className="app-shell">
      <TokenGate />
      <aside className="sidebar">
        <div className="brand-row">
          <div className="brand-mark"><Mic2 size={17} /></div>
          <div>
            <h1>{t("app.title")}</h1>
            <span>{t("app.subtitle")}</span>
          </div>
        </div>

        <section className="panel compact parser-panel script-workspace-panel">
          {scriptManagerPane}
        </section>

      </aside>

      <main className="workspace">
        <header className="topbar">
          <div className="toolbar topbar-toolbar">
            <span className={`notice ${toasts.length > 0 ? `notice-${toasts[toasts.length - 1].level}` : ""}`} title={notice}>{notice || t("app.ready")}</span>
            <button
              className={`topbar-action-button menu-trigger ${servicePanelSection === "roles" && isTopologyMenuOpen ? "active" : ""}`}
              onClick={() => {
                setServicePanelSection("roles");
                setIsTopologyMenuOpen(true);
              }}
              title={t("characters.libraryManager")}
            >
              <Library size={15} />
              <span className="menu-trigger-label">{t("topbar.roleLibrary")}</span>
            </button>
            {queueTopbarEntryVisible && (
              <button
                className={`topbar-action-button menu-trigger ${servicePanelSection === "resources" && isTopologyMenuOpen ? "active" : ""}`}
                onClick={() => {
                  setServicePanelSection("resources");
                  setIsTopologyMenuOpen(true);
                }}
                title={t("services.resourceQueueTitle")}
              >
                <History size={15} />
                <span className="menu-trigger-label">{t("topbar.resourceQueue")}</span>
              </button>
            )}
            <div className="topbar-menu-wrap topbar-config-actions">
              <button
                className={`topbar-action-button menu-trigger service-status-trigger tone-${serviceSummary.parser.tone} ${servicePanelSection === "llm" && isTopologyMenuOpen ? "active" : ""}`}
                onClick={() => {
                  const shouldOpen = servicePanelSection !== "llm" || !isTopologyMenuOpen;
                  setServicePanelSection("llm");
                  setIsTopologyMenuOpen(shouldOpen);
                  if (shouldOpen) setIsLlmAdvancedOpen(false);
                }}
                title={llmTopbarTitle(serviceSummary, t)}
              >
                <Bot size={15} />
                <span className="menu-trigger-label">{t("topbar.llmConfig")}</span>
                {llmHealthItem && (
                  <span className="service-health-strip" aria-hidden="true">
                    <span className={`service-health-dot tone-${llmHealthItem.tone}`} title={`${t(llmHealthItem.labelKey)} ${llmHealthItem.value}`.trim()} />
                  </span>
                )}
              </button>
              <button
                className={`topbar-action-button menu-trigger service-status-trigger tone-${ttsTopbarTone(serviceSummary)} ${servicePanelSection === "open-source" && isTopologyMenuOpen ? "active" : ""}`}
                onClick={() => {
                  setServicePanelSection("open-source");
                  setIsTopologyMenuOpen((open) => servicePanelSection === "open-source" ? !open : true);
                }}
                title={ttsTopbarTitle(serviceSummary, t)}
              >
                <Cpu size={15} />
                <span className="menu-trigger-label">{t("topbar.ttsConfig")}</span>
                <span className="service-health-strip" aria-hidden="true">
                  {ttsHealthItems.map((item) => (
                    <span className={`service-health-dot tone-${item.tone}`} key={item.id} title={`${t(item.labelKey)} ${item.value}`.trim()} />
                  ))}
                </span>
              </button>
              {isTopologyMenuOpen && (
                <div className="service-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setIsTopologyMenuOpen(false); }}>
                  <div className={`service-modal ${topologyModalClass}`} role="dialog" aria-modal="true" aria-label={topologyModalTitle}>
                    <header className="service-modal-head">
                      <div>
                        <strong>{topologyModalTitle}</strong>
                        <span>{topologyModalDescription}</span>
                      </div>
                      <div className="service-modal-actions">
                        <button className="icon-button small" onClick={() => void refreshTopology(true)} title={t("actions.refresh")}>
                          {isRefreshingTopology ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />}
                        </button>
                        <button className="icon-button small" onClick={() => setIsTopologyMenuOpen(false)} title={t("actions.close")}><X size={14} /></button>
                      </div>
                    </header>

                    <div className="service-modal-body">
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

                        {servicePanelSection === "open-source" && (
                          <div className="tts-access-panel">
                            <section className="tts-access-card tts-access-primary">
                              <div className="tts-provider-segment" aria-label={t("services.openSourceChooseEngine")}>
                                {openSourceCatalog.map((item) => (
                                  <button
                                    className={`open-source-mode-card ${selectedOpenSourceProvider === item.provider_type ? "active" : ""}`}
                                    key={item.provider_type}
                                    onClick={() => setSelectedOpenSourceProvider(item.provider_type)}
                                    type="button"
                                  >
                                    <strong>{item.display_name}</strong>
                                  </button>
                                ))}
                                {openSourceCatalog.length === 0 && <div className="empty-row">{t("services.openSourceNoCatalog")}</div>}
                              </div>
                              <div className="open-source-form-grid tts-access-form">
                                <label className="wide">
                                  <span>{t("services.openSourceBaseUrl")}</span>
                                  <input value={openSourceBaseUrl} onChange={(event) => setOpenSourceBaseUrl(event.target.value)} placeholder={selectedOpenSourceCatalog?.default_base_url} />
                                </label>
                              </div>
                              <div className="open-source-actions">
                                <button className="primary-button compact-button" onClick={() => void detectAndSaveOpenSourceService()} disabled={isDetectingOpenSource || isConfiguringOpenSource || !openSourceBaseUrl}>
                                  {isConfiguringOpenSource ? <Loader2 className="spin" size={14} /> : <CheckCircle2 size={14} />} {t("services.openSourceDetectAndSave")}
                                </button>
                              </div>

                              <details className="tts-access-maintenance">
                                <summary>
                                  <span>{t("services.openSourceAdvanced")}</span>
                                  <small>{openSourceDetectResult ? setupStateLabel(openSourceDetectResult.setup_state, t) : t("services.openSourceAdvancedHint", { count: configuredOpenSourceServices.length })}</small>
                                </summary>
                                <div className="tts-access-maintenance-body">
                                  <div className="open-source-existing">
                                    <div className="open-source-existing-list">
                                      {configuredOpenSourceServices.map((service) => {
                                        const state = ttsServiceState(service, runningServiceIds.has(service.service_id ?? ""), runtime?.service_mode);
                                        return (
                                          <article className={`open-source-existing-card state-${state}`} key={service.service_id ?? service.engine}>
                                            <span className={`tts-state-dot ${state}`} />
                                            <span>
                                              <strong>{serviceDisplayName(service)}</strong>
                                              <small>{service.base_url || t("services.endpointMissing")}</small>
                                            </span>
                                            <span className={`tracker-chip ${ttsStateToneClass(state)}`}>{ttsServiceStateLabel(service, state, t, runtime?.service_mode)}</span>
                                          </article>
                                        );
                                      })}
                                      {configuredOpenSourceServices.length === 0 && <div className="empty-row compact">{t("services.noService")}</div>}
                                    </div>
                                  </div>
                                  {openSourceDetectResult && (
                                    <div className={`open-source-detect-card compact state-${setupStateTone(openSourceDetectResult.setup_state)}`}>
                                      <div>
                                        <span>{t("services.openSourceSetupState")}</span>
                                        <strong>{setupStateLabel(openSourceDetectResult.setup_state, t)}</strong>
                                      </div>
                                      <div>
                                        <span>{t("services.openSourceEndpointReachable")}</span>
                                        <strong>{booleanLabel(openSourceDetectResult.endpoint_reachable, t)}</strong>
                                      </div>
                                      <p>{openSourceDetectResult.env_hint}</p>
                                    </div>
                                  )}
                                  <div className="tts-maintenance-tools">
                                    <label className="library-field compact">
                                      <span>{t("services.openSourceDisplayName")}</span>
                                      <input value={openSourceDisplayName} onChange={(event) => setOpenSourceDisplayName(event.target.value)} placeholder={selectedOpenSourceCatalog?.display_name} />
                                    </label>
                                    <div className="open-source-actions compact">
                                      <button className="secondary-button compact-button" onClick={() => void refreshOpenSourceCatalog()}>
                                        <RefreshCw size={13} /> {t("services.openSourceRefreshCatalog")}
                                      </button>
                                      <button className="secondary-button compact-button" onClick={() => void runOpenSourceDetect()} disabled={isDetectingOpenSource || isConfiguringOpenSource || !openSourceBaseUrl}>
                                        {isDetectingOpenSource ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />} {t("services.openSourceDetect")}
                                      </button>
                                    </div>
                                  </div>
                                </div>
                              </details>
                            </section>
                          </div>
                        )}

                        {servicePanelSection === "tts" && (
                          <div className="tts-ops-workbench">
                            <section className="tts-ops-rail">
                              <div className="tts-title-block">
                                <strong><Bot size={15} /> {t("services.panelTTS")}</strong>
                                <span>{t("services.ttsHint")}</span>
                              </div>
                              <div className="tts-metric-grid">
                                <div className="tts-meter ready"><span>{t("services.routableServices")}</span><strong>{ttsServices.filter((service) => ["ready", "running"].includes(ttsServiceState(service, runningServiceIds.has(service.service_id ?? ""), runtime?.service_mode))).length}/{ttsServices.length}</strong></div>
                                <div className="tts-meter warn"><span>{t("services.needsAction")}</span><strong>{ttsServices.filter((service) => ttsServiceState(service, runningServiceIds.has(service.service_id ?? ""), runtime?.service_mode) === "partial").length}</strong></div>
                                <div className="tts-meter danger"><span>{t("services.blocked")}</span><strong>{ttsServices.filter((service) => ttsServiceState(service, runningServiceIds.has(service.service_id ?? ""), runtime?.service_mode) === "blocked").length}</strong></div>
                                <div className="tts-meter neutral"><span>{t("services.disabled")}</span><strong>{ttsServices.filter((service) => ttsServiceState(service, runningServiceIds.has(service.service_id ?? ""), runtime?.service_mode) === "disabled").length}</strong></div>
                              </div>
                              <div className="tts-policy-card">
                                <strong>{t("services.endpointStrategy")}</strong>
                                <span>{t("services.endpointStrategyHint")}</span>
                                <div className="tts-policy-pills">
                                  <span>{t("services.scopeLocalhost")}</span>
                                  <span>{t("services.scopeLan")}</span>
                                  <span>{t("services.scopePublic")}</span>
                                  <span>{t("services.scopeCommercial")}</span>
                                </div>
                              </div>
                              <div className="tts-policy-card">
                                <strong>{t("services.routeSafety")}</strong>
                                <span>{t("services.routeSafetyHint")}</span>
                              </div>
                              <div className="tts-rail-actions">
                                <button className="secondary-button compact-button" onClick={() => selectedConfigService?.service_id && void testSelectedService(selectedConfigService.service_id)} disabled={!selectedConfigService?.service_id || testingServiceId === selectedConfigService.service_id}>
                                  {testingServiceId === selectedConfigService?.service_id ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />} {t("services.testEndpoint")}
                                </button>
                                <button className="primary-button compact-button" onClick={() => void saveServiceDirectorySettings()} disabled={isSavingServiceConfig || ttsServices.length === 0}>
                                  {isSavingServiceConfig ? <Loader2 className="spin" size={14} /> : <CheckCircle2 size={14} />} {t("services.saveDirectory")}
                                </button>
                              </div>
                            </section>

                            <section className="tts-service-directory">
                              <div className="tts-section-head">
                                <strong><SlidersHorizontal size={15} /> {t("services.serviceDirectory")}</strong>
                                <span>{ttsServices.length}</span>
                              </div>
                              <div className="tts-service-list">
                                {ttsServices.map((worker) => {
                                  const state = ttsServiceState(worker, runningServiceIds.has(worker.service_id ?? ""), runtime?.service_mode);
                                  const selected = selectedConfigService?.service_id === worker.service_id;
                                  return (
                                    <article className={`tts-service-card ${selected ? "selected" : ""} state-${state}`} key={worker.service_id ?? worker.engine}>
                                      <button
                                        className="tts-service-select"
                                        onClick={() => setExpandedServiceConfigId(worker.service_id ?? null)}
                                        type="button"
                                      >
                                        <span className={`tts-state-dot ${state}`} />
                                        <span className="tts-service-main">
                                          <strong title={worker.service_id ?? worker.engine}>{serviceDisplayName(worker)}</strong>
                                          <small>{worker.service_id ?? worker.engine}</small>
                                        </span>
                                        <span className={`tts-state-badge ${state}`}>{ttsServiceStateLabel(worker, state, t, runtime?.service_mode)}</span>
                                        <span className="tts-service-endpoint">{worker.base_url || t("services.endpointMissing")}</span>
                                        <span className="tts-chip-row">
                                          <span className={`tracker-chip ${ttsStateToneClass(state)}`}>{serviceLifecycleText(worker, t)}</span>
                                          <span className="tracker-chip">{serviceEndpointMode(worker, t)}</span>
                                          <span className="tracker-chip">{worker.resource_group ?? t("status.resource")}</span>
                                        </span>
                                      </button>
                                      <div className="tts-card-actions">
                                        <button className="icon-button tiny" disabled={!worker.service_id || !worker.supervisor?.manageable || worker.supervisor.running} onClick={() => worker.service_id && void serviceAction(worker.service_id, "start")} title={t("actions.startService")}><Power size={13} /></button>
                                        <button className="icon-button tiny" disabled={!worker.service_id || !worker.supervisor?.running} onClick={() => worker.service_id && void serviceAction(worker.service_id, "stop")} title={t("actions.stopService")}><Square size={12} /></button>
                                        <button className="icon-button tiny" disabled={!worker.service_id} onClick={() => worker.service_id && void toggleLogs(worker.service_id)} title={t("actions.showLogs")}><FileText size={13} /></button>
                                        <button className="icon-button tiny" disabled={!worker.service_id} onClick={() => worker.service_id && void testSelectedService(worker.service_id)} title={t("services.testEndpoint")}><RefreshCw size={13} /></button>
                                      </div>
                                      {expandedServiceId === worker.service_id && <pre className="service-log tts-service-log">{(serviceLogs[worker.service_id ?? ""] ?? [t("empty.noLogs")]).join("\n")}</pre>}
                                    </article>
                                  );
                                })}
                                {ttsServices.length === 0 && <div className="empty-row">{t("services.noService")}</div>}
                              </div>
                            </section>

                            <section className="tts-service-detail">
                              {selectedConfigService ? (
                                <>
                                  <div className="tts-detail-hero">
                                    <div>
                                      <strong>{serviceDisplayName(selectedConfigService)}</strong>
                                      <span>{selectedConfigService.service_id ?? selectedConfigService.engine} · {selectedConfigService.base_url || t("services.endpointMissing")}</span>
                                    </div>
                                    <span className={`tts-detail-state ${ttsServiceState(selectedConfigService, runningServiceIds.has(selectedConfigService.service_id ?? ""), runtime?.service_mode)}`}>
                                      <span className={`tts-state-dot ${ttsServiceState(selectedConfigService, runningServiceIds.has(selectedConfigService.service_id ?? ""), runtime?.service_mode)}`} />
                                      {ttsServiceStateLabel(selectedConfigService, ttsServiceState(selectedConfigService, runningServiceIds.has(selectedConfigService.service_id ?? ""), runtime?.service_mode), t, runtime?.service_mode)}
                                    </span>
                                  </div>
                                  <div className="tts-detail-metrics">
                                    <div><span>{t("services.lifecycle")}</span><strong>{serviceLifecycleText(selectedConfigService, t)}</strong></div>
                                    <div><span>{t("services.health")}</span><strong>{serviceHealthText(selectedConfigService, t, runtime?.service_mode)}</strong></div>
                                    <div><span>{t("services.networkScope")}</span><strong>{serviceEndpointMode(selectedConfigService, t)}</strong></div>
                                    <div><span>{t("services.resourceGroup")}</span><strong>{selectedConfigService.resource_group ?? t("status.unassigned")}</strong></div>
                                  </div>
                                  <div className="tts-form-grid">
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
                                      <input value={selectedConfigService.base_url ?? ""} onChange={(event) => updateServiceDraft(selectedConfigService.service_id, { base_url: event.target.value })} placeholder="http://127.0.0.1:9872" />
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
                                  </div>
                                  <div className="tts-contract-grid">
                                    <div><span>{t("services.provider")}</span><strong>{providerLabel(selectedConfigService.provider_type ?? selectedConfigService.engine)}</strong></div>
                                    <div><span>{t("services.apiContract")}</span><strong>{selectedConfigService.api_contract ?? selectedConfigService.engine}</strong></div>
                                    <div><span>{t("services.authProfile")}</span><strong>{serviceAuthText(selectedConfigService, t)}</strong></div>
                                    <div><span>{t("services.costPolicy")}</span><strong>{summarizeConfigValue(selectedConfigService.cost_policy)}</strong></div>
                                  </div>
                                  <div className="tts-capability-card">
                                    <span>{t("services.capabilities")}</span>
                                    <strong>{selectedConfigService.capabilities?.join(" / ") || "-"}</strong>
                                    <small>{t("services.defaultParams")}: {summarizeConfigValue(selectedConfigService.default_params)}</small>
                                  </div>
                                  <div className="tts-detail-actions">
                                    <span>{t("services.configHint")}</span>
                                    <div>
                                      <button className="secondary-button compact-button" disabled={!selectedConfigService.service_id || !selectedConfigService.supervisor?.manageable || selectedConfigService.supervisor.running} onClick={() => selectedConfigService.service_id && void serviceAction(selectedConfigService.service_id, "start")}><Power size={14} /> {t("actions.startService")}</button>
                                      <button className="secondary-button compact-button" disabled={!selectedConfigService.service_id || !selectedConfigService.supervisor?.running} onClick={() => selectedConfigService.service_id && void serviceAction(selectedConfigService.service_id, "stop")}><Square size={13} /> {t("actions.stopService")}</button>
                                      <button className="secondary-button compact-button" onClick={() => void testSelectedService(selectedConfigService.service_id)} disabled={testingServiceId === selectedConfigService.service_id}>
                                        {testingServiceId === selectedConfigService.service_id ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />} {t("services.testEndpoint")}
                                      </button>
                                      <button className="primary-button compact-button" onClick={() => void saveServiceDirectorySettings()} disabled={isSavingServiceConfig}>
                                        {isSavingServiceConfig ? <Loader2 className="spin" size={14} /> : <CheckCircle2 size={14} />} {t("services.saveDirectory")}
                                      </button>
                                    </div>
                                  </div>
                                </>
                              ) : (
                                <div className="empty-row">{t("services.noService")}</div>
                              )}
                            </section>
                          </div>
                        )}

                        {servicePanelSection === "llm" && (
                          <div className="llm-activation-workbench">
                            <section className="llm-activation-panel">
                              <div className="llm-activation-head">
                                <div className="llm-title-block">
                                  <strong><Bot size={15} /> {t("parser.kwjmActivationTitle")}</strong>
                                  <span>{t("parser.kwjmActivationHint")}</span>
                                </div>
                                <div className="llm-head-actions">
                                  <span className={`llm-detail-state state-${kwjmActivationState}`}>
                                    <span className={`llm-state-dot ${kwjmActivationState}`} />
                                    {kwjmActivationStateLabel(kwjmActivationState, t)}
                                  </span>
                                  <button className="secondary-button compact-button" onClick={() => setIsLlmAdvancedOpen((open) => !open)} aria-expanded={isLlmAdvancedOpen}>
                                    <SlidersHorizontal size={14} /> {t(isLlmAdvancedOpen ? "parser.hideAdvancedConfig" : "parser.advancedConfig")}
                                  </button>
                                </div>
                              </div>
                              <p className="llm-preset-line">{t("parser.kwjmPresetMeta", { model: KWJM_MODEL, baseUrl: KWJM_BASE_URL })}</p>
                              <div className="llm-api-key-row">
                                <label className="llm-api-key-field">
                                  <span>{t("parser.apiKey")}</span>
                                  <input
                                    type="password"
                                    value={kwjmApiKeyInput}
                                    onChange={(event) => setKwjmApiKeyInput(event.target.value)}
                                    placeholder={t(kwjmHasUsableKey ? "parser.apiKeyPlaceholderConfigured" : "parser.apiKeyPlaceholderMissing")}
                                  />
                                </label>
                                <button className="secondary-button compact-button" onClick={() => void testKwjmParserProviderSettings()} disabled={testingParserProviderIndex !== null || !kwjmCanActivate}>
                                  {testingParserProviderIndex === KWJM_TESTING_INDEX ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />} {t("parser.testProvider")}
                                </button>
                                <button className="primary-button compact-button" onClick={() => void activateKwjmParserProvider()} disabled={isSavingParserConfig || !kwjmCanActivate}>
                                  {isSavingParserConfig ? <Loader2 className="spin" size={14} /> : <CheckCircle2 size={14} />} {t("parser.activateKwjm")}
                                </button>
                              </div>
                              {kwjmDisplayTestResult && (
                                <section className={`llm-test-result ${kwjmDisplayTestResult.ok ? "ok" : "danger"}`}>
                                  <div>
                                    <strong>{kwjmHasUsableKey ? t("parser.kwjmConfigured") : t("parser.kwjmMissingKey")}</strong>
                                    <span>{kwjmDisplayTestResult ? kwjmDisplayTestResult.message : t("parser.noTestYet")}</span>
                                  </div>
                                  {kwjmDisplayTestResult?.latency_ms != null && <small>{kwjmDisplayTestResult.latency_ms}ms</small>}
                                </section>
                              )}
                            </section>

                            {isLlmAdvancedOpen && (
                              <section className="llm-advanced-panel">
                                <div className="llm-section-head">
                                  <div className="llm-section-title">
                                    <strong><SlidersHorizontal size={15} /> {t("parser.providerDirectory")}</strong>
                                    <span>{t("parser.providerHint")}</span>
                                  </div>
                                  <span>{t("parser.advancedSummary", {
                                    enabled: parserProviders.filter((provider) => provider.enabled).length,
                                    total: parserProviders.length,
                                    keys: parserProviders.filter((provider) => parserProviderHasUsableKey(provider)).length
                                  })}</span>
                                </div>
                              <div className="llm-advanced-layout">
                                <div className="llm-provider-list compact">
                                {parserProviders.map((provider, index) => {
                                  const state = parserProviderState(provider);
                                  const selected = selectedParserProviderIndex === index;
                                  return (
                                    <button
                                      className={`llm-provider-card ${selected ? "selected" : ""} state-${state}`}
                                      key={`${provider.name}-${index}`}
                                      onClick={() => setSelectedParserProviderIndex(index)}
                                      type="button"
                                    >
                                      <span className={`llm-state-dot ${state}`} />
                                      <span className="llm-provider-main">
                                        <strong>{provider.name || t("parser.providerName")}</strong>
                                        <small>{provider.model || t("status.unset")}</small>
                                      </span>
                                      <span className="llm-chip-row">
                                        <span className={`tracker-chip ${state === "ready" ? "ok" : state === "blocked" ? "danger" : "warn"}`}>{parserProviderStateLabel(provider, t)}</span>
                                        <span className={`tracker-chip ${parserProviderHasUsableKey(provider) ? "ok" : "warn"}`}>{t(parserProviderHasUsableKey(provider) ? "parser.keyConfigured" : "parser.keyMissing")}</span>
                                        {parserProviderTestResults[index] && (
                                          <span className={`tracker-chip ${parserProviderTestResults[index].ok ? "ok" : "danger"}`}>{parserProviderTestResults[index].ok ? t("parser.testPassed") : t("parser.testFailed")}</span>
                                        )}
                                      </span>
                                    </button>
                                  );
                                })}
                                {parserProviders.length === 0 && <div className="empty-row">{t("empty.noParserProviders")}</div>}
                                </div>

                                <div className="llm-provider-editor">
                                  {selectedParserProvider ? (() => {
                                const selectedState = parserProviderState(selectedParserProvider);
                                const selectedTestResult = parserProviderTestResults[selectedParserProviderIndex];
                                return (
                                  <>
                                      <div className="llm-detail-hero compact">
                                    <div>
                                      <strong>{selectedParserProvider.name || t("parser.providerName")}</strong>
                                      <span>{selectedParserProvider.model || t("status.unset")} · {t(`parser.adapter.${selectedParserProvider.adapter}`)} · {selectedParserProvider.base_url || t("services.endpointMissing")}</span>
                                    </div>
                                    <span className={`llm-detail-state state-${selectedState}`}>
                                      <span className={`llm-state-dot ${selectedState}`} />
                                      {parserProviderStateLabel(selectedParserProvider, t)}
                                    </span>
                                  </div>
                                      <div className="llm-form-grid llm-simple-form">
                                        <label className="llm-switch llm-switch-field">
                                          <input type="checkbox" checked={selectedParserProvider.enabled} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { enabled: event.target.checked })} />
                                          <span>{t("parser.enabled")}</span>
                                        </label>
                                        <label>
                                          <span>{t("parser.providerName")}</span>
                                          <input value={selectedParserProvider.name} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { name: event.target.value })} />
                                        </label>
                                        <label>
                                          <span>{t("parser.adapterLabel")}</span>
                                          <select value={selectedParserProvider.adapter} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { adapter: event.target.value as ParserProviderDraft["adapter"] })}>
                                            <option value="openai-compatible">{t("parser.adapter.openai-compatible")}</option>
                                            <option value="anthropic">{t("parser.adapter.anthropic")}</option>
                                          </select>
                                        </label>
                                        <label className="wide">
                                          <span>{t("parser.baseUrl")}</span>
                                          <input value={selectedParserProvider.base_url} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { base_url: event.target.value })} placeholder={KWJM_BASE_URL_PLACEHOLDER} />
                                        </label>
                                        <label>
                                          <span>{t("parser.model")}</span>
                                          <input value={selectedParserProvider.model} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { model: event.target.value })} placeholder={KWJM_MODEL} />
                                        </label>
                                        <label>
                                          <span>{t("parser.apiKey")}</span>
                                          <input
                                            type="password"
                                            value={selectedParserProvider.api_key ?? ""}
                                            onChange={(event) => updateParserProvider(selectedParserProviderIndex, { api_key: event.target.value })}
                                            placeholder={t(parserProviderKeyState(selectedParserProvider) === "configured" ? "parser.apiKeyPlaceholderConfigured" : "parser.apiKeyPlaceholderMissing")}
                                          />
                                        </label>
                                      </div>
                                      <details className="llm-extra-settings">
                                        <summary>{t("parser.advancedParameters")}</summary>
                                        <div className="llm-form-grid llm-extra-form">
                                          <label>
                                            <span>{t("parser.priority")}</span>
                                            <input type="number" min={1} value={selectedParserProvider.priority} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { priority: Number(event.target.value) || 100 })} />
                                          </label>
                                          <label>
                                            <span>{t("parser.apiKeyEnv")}</span>
                                            <input value={selectedParserProvider.api_key_env} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { api_key_env: event.target.value })} placeholder={KWJM_API_KEY_ENV} />
                                          </label>
                                          <label>
                                            <span>{t("parser.timeout")}</span>
                                            <input type="number" min={5} max={300} value={selectedParserProvider.timeout_seconds} onChange={(event) => updateParserProvider(selectedParserProviderIndex, { timeout_seconds: Number(event.target.value) || 45 })} />
                                          </label>
                                        </div>
                                      </details>
                                      {selectedTestResult && (
                                        <section className={`llm-test-result ${selectedTestResult.ok ? "ok" : "danger"}`}>
                                      <div>
                                        <strong>{t("parser.lastTest")}</strong>
                                            <span>{selectedTestResult.message}</span>
                                      </div>
                                      {selectedTestResult?.latency_ms != null && <small>{selectedTestResult.latency_ms}ms</small>}
                                    </section>
                                      )}
                                  </>
                                );
                              })() : (
                                <div className="empty-row">{t("empty.noParserProviders")}</div>
                              )}
                                </div>
                              </div>
                                <div className="llm-detail-actions">
                                  <span>{t("parser.providerDetailHint")}</span>
                                  <button className="secondary-button compact-button" onClick={addParserProvider}><Plus size={13} /> {t("parser.addProvider")}</button>
                                  <button className="secondary-button compact-button" onClick={() => void testParserProviderSettings(selectedParserProviderIndex)} disabled={!selectedParserProvider || testingParserProviderIndex !== null}>
                                    {testingParserProviderIndex === selectedParserProviderIndex ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />} {t("parser.testProvider")}
                                  </button>
                                  <button className="primary-button compact-button" onClick={() => void saveParserProviderSettings()} disabled={isSavingParserConfig || parserProviders.length === 0}>
                                    {isSavingParserConfig ? <Loader2 className="spin" size={14} /> : <CheckCircle2 size={14} />} {t("parser.saveProviders")}
                                  </button>
                                </div>
                              </section>
                            )}
                          </div>
                        )}

                        {servicePanelSection === "resources" && (
                          <div className="queue-workbench">
                            <section className={`queue-status-card ${queueHasWork ? "has-work" : "is-empty"}`}>
                              <div className="queue-status-head">
                                <div>
                                  <strong><History size={15} /> {t("queue.title")}</strong>
                                  <span>{queueHasWork ? t("queue.processedRatio", { processed: queueProcessedItems, total: queueTotalItems }) : t("queue.noJobs")}</span>
                                </div>
                                <StatusPill tone={queueVisibleTone} label={queueVisibleStatusLabel} />
                              </div>

                              {queueHasWork && (
                                <>
                                  <div className="queue-progress-row">
                                    <strong>{queueProgressPercent}%</strong>
                                    <div className="queue-dispatch-bar" aria-label={t("queue.progressLabel", { percent: queueProgressPercent })}>
                                      <span style={{ width: `${queueProgressPercent}%` }} />
                                    </div>
                                  </div>
                                  <div className="queue-count-strip" aria-label={t("queue.countSummary")}>
                                    <span><strong>{queueQueuedItems}</strong>{t("filters.queued")}</span>
                                    <span><strong>{queueRunningItems}</strong>{t("filters.running")}</span>
                                    <span><strong>{queueCompletedItems}</strong>{t("status.completed")}</span>
                                    <span><strong>{queueFailedItems}</strong>{t("status.failed")}</span>
                                  </div>
                                </>
                              )}

                              {queueJobs.length > 0 && (
                                <details className="queue-job-details" open={Boolean(queueActiveJob)}>
                                  <summary>
                                    <span>{t("queue.recentJobs")}</span>
                                    <small>{t("queue.itemCount", { count: queueJobs.length })}</small>
                                  </summary>
                                  <div className="queue-job-list">
                                    {queueJobs.slice(0, 5).map((job) => {
                                      const jobPercent = Math.round(Math.max(0, Math.min(1, job.progress)) * 100);
                                      return (
                                        <article className={`queue-job-card state-${queueStatusTone(job.status)}`} key={job.job_id}>
                                          <div>
                                            <strong>{job.job_id}</strong>
                                            <span>{t("queue.itemCount", { count: job.items.length })}</span>
                                          </div>
                                          <div className="queue-job-meta">
                                            <StatusPill tone={queueStatusTone(job.status)} label={statusText(job.status, t)} />
                                            <span>{jobPercent}%</span>
                                          </div>
                                        </article>
                                      );
                                    })}
                                  </div>
                                </details>
                              )}
                            </section>
                          </div>
                        )}

                        {servicePanelSection === "roles" && (
                          <div className="role-library-workbench">
                            <section className="role-library-rail">
                              <div className="role-library-title-block">
                                <strong>{t("characters.currentScriptRoles")}</strong>
                                <span>{t("characters.currentScriptRolesHint")}</span>
                              </div>
                              <div className="project-role-compact-list">
                                {projectRoleRows.map((role, index) => {
                                  const mapping = projectCharacters.find((item) => item.project_character_id === role.id);
                                  const linkedCharacter = mapping?.library_character_id
                                    ? characters.find((character) => character.id === mapping.library_character_id)
                                    : null;
                                  const selected = activeProjectCharacter?.project_character_id === role.id;
                                  const profileLabel = role.profile === "unassigned" ? t("status.unassigned") : role.profile;
                                  const bindingLabel = mapping?.project_binding
                                    ? t("characters.projectTemporaryVoice")
                                    : linkedCharacter?.name ?? profileLabel;
                                  return (
                                    <button
                                      className={`project-role-compact ${selected ? "selected" : ""} ${mapping?.project_binding || linkedCharacter ? "matched" : "unmatched"} ${roleAccentClass(index)}`}
                                      key={role.id}
                                      onClick={() => {
                                        setActiveProjectRoleId(role.id);
                                        setActiveRoleCandidateId(null);
                                        setActiveModelCatalogId(null);
                                      }}
                                    >
                                      <RoleAvatar avatarPath={role.avatarPath} fallback={role.avatarFallback} size="sm" />
                                      <span>
                                        <strong>{role.name}</strong>
                                        <small>{t("characters.lines", { count: role.lineCount })} · {bindingLabel}</small>
                                      </span>
                                    </button>
                                  );
                                })}
                                {projectRoleRows.length === 0 && <div className="empty-row compact">{t("characters.noProjectRoles")}</div>}
                              </div>

                              <div className="role-library-active-summary">
                                <span>{t("characters.selectedProjectRole")}</span>
                                <strong>{activeProjectCharacter?.name ?? t("status.unassigned")}</strong>
                                <small>
                                  {activeProjectCharacter?.project_binding
                                    ? `${t("characters.projectTemporaryVoice")} · ${activeProjectCharacter.project_binding.service_id ?? t("services.noService")}`
                                    : t("characters.noProjectBinding")}
                                </small>
                                {activeProjectCharacter?.project_binding && (
                                  <button className="secondary-button compact-button" onClick={clearActiveProjectRoleBinding}>{t("characters.clearProjectBinding")}</button>
                                )}
                              </div>

                              <details className="role-maintenance-panel role-library-collapsible">
                                <summary>
                                  <span><Library size={13} /> {t("characters.commonVoices")}</span>
                                  <small>{t("characters.commonVoicesHint")}</small>
                                </summary>
                                <div className="role-library-collapsible-body">
                                  <div className="role-library-drawer-actions">
                                    <label className="search-field library-search">
                                      <Search size={14} />
                                      <input value={roleLibrarySearch} onChange={(event) => setRoleLibrarySearch(event.target.value)} placeholder={t("characters.searchLibrary")} />
                                    </label>
                                    <button className="secondary-button compact-button" onClick={addEmptyLibraryCharacter}><Plus size={13} /> {t("characters.addRole")}</button>
                                  </div>
                                  <div className="role-directory-list">
                                    {filteredLibraryCharacters.map((character, index) => {
                                      const summary = characterBindingSummary(character);
                                      const selected = activeModelCatalogId === null && activeRoleCandidateId === null && activeLibraryCharacter?.id === character.id;
                                      return (
                                        <button
                                          className={`role-directory-card ${selected ? "selected" : ""} ${roleAccentClass(index)}`}
                                          key={character.id}
                                          onClick={() => {
                                            setActiveLibraryCharacterId(character.id);
                                            setActiveRoleCandidateId(null);
                                            setActiveModelCatalogId(null);
                                          }}
                                        >
                                          <RoleAvatar avatarPath={character.avatar_path} fallback={avatarFallback(character.name)} size="lg" />
                                          <span className="role-directory-main">
                                            <strong>{character.name}</strong>
                                            <small>{summary.providerLabel} · {summary.bindingCount} {t("characters.bindings")}</small>
                                          </span>
                                          <span className={`role-state-dot ${characterStatusTone(character)}`} />
                                          <span className="role-directory-meta">
                                            <strong>{summary.completeCount}/{summary.bindingCount || 1}</strong>
                                            <small>{t("characters.completeBindings")}</small>
                                          </span>
                                        </button>
                                      );
                                    })}
                                    {filteredLibraryCharacters.length === 0 && <div className="empty-row compact">{t("characters.noCommonVoices")}</div>}
                                  </div>
                                </div>
                              </details>

                              <details className="role-maintenance-panel">
                                <summary>
                                  <span>{t("characters.roleMaintenance")}</span>
                                  <small>{t("characters.roleMaintenanceHint")}</small>
                                </summary>
                                <div className="role-maintenance-body">
                                  <div className="role-maintenance-actions">
                                    <button className="secondary-button compact-button" onClick={() => void scanRoles()} disabled={isScanningRoleLibrary}>
                                      {isScanningRoleLibrary ? <Loader2 className="spin" size={13} /> : <RefreshCw size={13} />} {t("characters.scanCandidates")}
                                    </button>
                                    <button className="secondary-button compact-button" onClick={() => void refreshModelCatalog()} disabled={isScanningModelCatalog}>
                                      {isScanningModelCatalog ? <Loader2 className="spin" size={13} /> : <RefreshCw size={13} />} {t("characters.refreshModelCatalog")}
                                    </button>
                                  </div>
                                  <label className="library-field compact">
                                    <span>{t("characters.logsService")}</span>
                                    <select value={selectedLogsServiceId} onChange={(event) => setSelectedLogsServiceId(event.target.value)}>
                                      <option value="">{t("characters.allGptServices")}</option>
                                      {roleLibraryCatalogServices.map((service) => (
                                        <option value={service.serviceId} key={service.serviceId}>{service.label} · {providerLabel(service.providerType)}</option>
                                      ))}
                                    </select>
                                  </label>
                                  <div className="role-library-status-row">
                                    <div><span>{t("characters.confirmedLibrary")}</span><strong>{characters.filter((character) => character.library_status === "confirmed").length}</strong></div>
                                    <div><span>{t("characters.scanDrafts")}</span><strong>{filteredRoleCandidates.length + filteredGptModelCatalog.length}</strong></div>
                                    <div><span>{t("characters.projectMatch")}</span><strong>{projectCharacters.filter((item) => item.match_status === "matched" || item.library_character_id).length}/{projectRoleRows.length}</strong></div>
                                  </div>
                                  <div className="role-maintenance-list">
                                    {filteredRoleCandidates.length > 0 && (
                                      <div className="role-list-subhead">
                                        <span><RefreshCw size={13} /> {t("characters.scanDrafts")}</span>
                                        <strong>{filteredRoleCandidates.length}</strong>
                                      </div>
                                    )}
                                    {filteredRoleCandidates.map((candidate) => {
                                      const selected = activeRoleCandidate?.id === candidate.id;
                                      return (
                                        <button
                                          className={`candidate-strip-card ${selected ? "selected" : ""}`}
                                          key={candidate.id}
                                          onClick={() => {
                                            setActiveRoleCandidateId(candidate.id);
                                            setActiveModelCatalogId(null);
                                          }}
                                        >
                                          <span className="candidate-strip-title">
                                            <strong>{candidate.name}</strong>
                                            <small>{candidate.logs_name ?? candidate.id}</small>
                                          </span>
                                          <span className="candidate-strip-counts">
                                            <b>GPT {candidate.gpt_weights?.length ?? 0}</b>
                                            <b>SoVITS {candidate.sovits_weights?.length ?? 0}</b>
                                            <b>Ref {candidate.reference_audio_groups?.reduce((sum, group) => sum + (group.samples?.length ?? 0), 0) ?? 0}</b>
                                          </span>
                                        </button>
                                      );
                                    })}
                                    {filteredGptModelCatalog.length > 0 && (
                                      <div className="role-list-subhead">
                                        <span><Cpu size={13} /> {t("characters.modelCatalog")}</span>
                                        <strong>{filteredGptModelCatalog.length}</strong>
                                      </div>
                                    )}
                                    {filteredGptModelCatalog.map((model) => {
                                      const selected = activeModelCatalogItem?.id === model.id;
                                      const sourceService = roleLibraryTtsServices.find((service) => service.serviceId === model.service_id);
                                      return (
                                        <button
                                          className={`candidate-strip-card model-catalog-card ${selected ? "selected" : ""}`}
                                          key={model.id}
                                          onClick={() => {
                                            setActiveModelCatalogId(model.id);
                                            setActiveRoleCandidateId(null);
                                          }}
                                        >
                                          <span className="candidate-strip-title">
                                            <strong>{model.name}</strong>
                                            <small>{model.logs_name ?? model.id}</small>
                                          </span>
                                          <span className="candidate-strip-counts">
                                            <b>{sourceService?.label ?? model.service_id ?? t("services.noService")}</b>
                                            <b>Ref {model.reference_audio_groups?.reduce((sum, group) => sum + (group.samples?.length ?? 0), 0) ?? 0}</b>
                                          </span>
                                        </button>
                                      );
                                    })}
                                    {filteredRoleCandidates.length === 0 && filteredGptModelCatalog.length === 0 && <div className="empty-row compact">{t("characters.noMaintenanceItems")}</div>}
                                  </div>
                                </div>
                              </details>
                            </section>

                            <section className="role-library-detail-pane">
                              {activeModelCatalogItem ? (
                                <div className="role-detail-stack">
                                  <div className="role-detail-hero">
                                    <RoleAvatar fallback={avatarFallback(activeModelCatalogItem.name)} size="lg" />
                                    <div>
                                      <strong>{activeModelCatalogItem.name}</strong>
                                      <span>{providerLabel("gpt-sovits")} · {activeModelCatalogItem.logs_name ?? activeModelCatalogItem.id}</span>
                                    </div>
                                    <button className="secondary-button compact-button" onClick={() => void refreshModelCatalog()} disabled={isScanningModelCatalog}>
                                      {isScanningModelCatalog ? <Loader2 className="spin" size={13} /> : <RefreshCw size={13} />} {t("characters.modelCatalog")}
                                    </button>
                                  </div>
                                  <div className="role-detail-metrics">
                                    <div><span>GPT</span><strong>{activeModelCatalogItem.gpt_weights?.length ?? 0}</strong></div>
                                    <div><span>SoVITS</span><strong>{activeModelCatalogItem.sovits_weights?.length ?? 0}</strong></div>
                                    <div><span>Ref</span><strong>{activeModelCatalogItem.reference_audio_groups?.reduce((sum, group) => sum + (group.samples?.length ?? 0), 0) ?? activeModelSamples.length}</strong></div>
                                  </div>
                                  <section className="role-config-card">
                                    <div className="role-config-head">
                                      <strong>{t("characters.projectRoleBinding")}</strong>
                                      <select value={activeProjectCharacter?.project_character_id ?? ""} onChange={(event) => setActiveProjectRoleId(event.target.value || null)}>
                                        {projectRoleRows.map((role) => (
                                          <option value={role.id} key={role.id}>{role.name}</option>
                                        ))}
                                      </select>
                                    </div>
                                    <div className="role-model-actions">
                                      <button className="primary-button compact-button" onClick={bindActiveModelToProjectRole} disabled={!activeProjectCharacter}>{t("characters.bindToProjectRole")}</button>
                                      <button className="secondary-button compact-button" onClick={writeActiveModelToLibrary} disabled={!activeProjectCharacter}>{t("characters.writeToLibrary")}</button>
                                      <button className="secondary-button compact-button" onClick={clearActiveProjectRoleBinding} disabled={!activeProjectCharacter?.project_binding}>{t("characters.clearProjectBinding")}</button>
                                    </div>
                                    <div className="role-detail-card">
                                      <span>{t("characters.selectedProjectRole")}</span>
                                      <strong>{activeProjectCharacter?.name ?? t("status.unassigned")}</strong>
                                      <small>{activeProjectCharacter?.project_binding ? `${activeProjectCharacter.project_binding.provider_type} · ${activeProjectCharacter.project_binding.service_id ?? t("services.noService")}` : t("characters.noProjectBinding")}</small>
                                    </div>
                                  </section>
                                  <div className="role-detail-card">
                                    <span>{t("characters.sourceService")}</span>
                                    <strong>{serviceDisplayName(serviceById.get(activeModelCatalogItem.service_id ?? "") ?? ({ engine: "gpt-sovits", display_name: activeModelCatalogItem.service_id ?? t("services.noService"), ready: false } as WorkerHealth))}</strong>
                                    <small>{activeModelCatalogItem.source ?? "model_catalog"}</small>
                                  </div>
                                  <div className="role-detail-card">
                                    <span>{t("characters.selectedReference")}</span>
                                    <strong>{activeModelSelectedSample ? shortPath(activeModelSelectedSample.path) : t("status.unset")}</strong>
                                    <small>{activeModelSelectedSample ? referenceSampleDisplayLabel(activeModelSelectedSample) : t("status.unset")}</small>
                                  </div>
                                  <div className="role-detail-card">
                                    <span>{t("characters.recommendedAssets")}</span>
                                    <strong>{activeModelCatalogItem.recommended_gpt_weights_path ? shortPath(activeModelCatalogItem.recommended_gpt_weights_path) : t("status.unset")}</strong>
                                    <small>{activeModelCatalogItem.recommended_sovits_weights_path ? shortPath(activeModelCatalogItem.recommended_sovits_weights_path) : t("status.unset")}</small>
                                  </div>
                                  <ReferencePreview groups={activeModelCatalogItem.reference_audio_groups ?? []} t={t} />
                                </div>
                              ) : activeRoleCandidate ? (
                                <div className="role-detail-stack">
                                  <div className="role-detail-hero">
                                    <RoleAvatar fallback={avatarFallback(activeRoleCandidate.name)} size="lg" />
                                    <div>
                                      <strong>{activeRoleCandidate.name}</strong>
                                      <span>{activeRoleCandidate.logs_name ?? activeRoleCandidate.id}</span>
                                    </div>
                                    <button className="primary-button compact-button" onClick={() => void importCandidate(activeRoleCandidate)}>{t("characters.importCandidate")}</button>
                                  </div>
                                  <div className="role-detail-metrics">
                                    <div><span>GPT</span><strong>{activeRoleCandidate.gpt_weights?.length ?? 0}</strong></div>
                                    <div><span>SoVITS</span><strong>{activeRoleCandidate.sovits_weights?.length ?? 0}</strong></div>
                                    <div><span>Ref</span><strong>{activeRoleCandidate.reference_audio_groups?.reduce((sum, group) => sum + (group.samples?.length ?? 0), 0) ?? 0}</strong></div>
                                  </div>
                                  <div className="role-detail-card">
                                    <span>{t("characters.sourceService")}</span>
                                    <strong>{serviceDisplayName(serviceById.get(activeRoleCandidate.service_id ?? "") ?? ({ engine: "gpt-sovits", display_name: activeRoleCandidate.service_id ?? t("services.noService"), ready: false } as WorkerHealth))}</strong>
                                    <small>{activeRoleCandidate.source ?? "filesystem"}</small>
                                  </div>
                                  <div className="role-detail-card">
                                    <span>{t("characters.recommendedAssets")}</span>
                                    <strong>{activeRoleCandidate.recommended_gpt_weights_path ? shortPath(activeRoleCandidate.recommended_gpt_weights_path) : t("status.unset")}</strong>
                                    <small>{activeRoleCandidate.recommended_sovits_weights_path ? shortPath(activeRoleCandidate.recommended_sovits_weights_path) : t("status.unset")}</small>
                                  </div>
                                  <ReferencePreview groups={activeRoleCandidate.reference_audio_groups ?? []} t={t} />
                                </div>
                              ) : activeLibraryCharacter ? (() => {
                                const bindingRows = roleLibraryBindingRows(activeLibraryCharacter, ttsServices);
                                const gptBinding = bindingRows.find((row) => row.providerType === "gpt-sovits")?.binding;
                                const gptConfig = gptBinding?.config ?? {};
                                const gptComplete = gptBinding ? bindingCompleteness(gptBinding) : null;
                                const referenceSamples = (activeLibraryCharacter.reference_audio_groups ?? []).flatMap((group) =>
                                  (group.samples ?? []).map((sample) => ({ ...sample, group: group.name }))
                                );
                                const linkedProjectRoles = projectCharacters.filter((item) => item.library_character_id === activeLibraryCharacter.id);
                                const summary = characterBindingSummary(activeLibraryCharacter);
                                return (
                                  <div className="role-detail-stack">
                                    <div className="role-detail-hero role-detail-hero-editable">
                                      <RoleAvatar avatarPath={activeLibraryCharacter.avatar_path} fallback={avatarFallback(activeLibraryCharacter.name)} size="lg" />
                                      <div>
                                        <strong>{activeLibraryCharacter.name}</strong>
                                        <span>{characterMatchValues(activeLibraryCharacter).slice(0, 4).join(" · ") || activeLibraryCharacter.id}</span>
                                      </div>
                                      <div className="role-detail-hero-actions">
                                        <button className="primary-button compact-button" onClick={() => applyLibraryCharacterToProjectRole(activeLibraryCharacter)} disabled={!activeProjectCharacter}>{t("characters.bindToProjectRole")}</button>
                                        <label className="secondary-button compact-button avatar-upload-button">
                                          <Upload size={13} /> {t("characters.uploadAvatar")}
                                          <input
                                            type="file"
                                            accept="image/png,image/jpeg,image/webp"
                                            onChange={(event) => {
                                              void uploadAvatar(activeLibraryCharacter.id, event.currentTarget.files?.[0]);
                                              event.currentTarget.value = "";
                                            }}
                                          />
                                        </label>
                                        <button className="icon-button danger" onClick={() => void removeLibraryCharacter(activeLibraryCharacter.id)} title={t("characters.deleteRole")}><X size={14} /></button>
                                      </div>
                                    </div>
                                    <div className="role-detail-mini-strip">
                                      <div><span>{t("characters.status")}</span><strong>{t(`characters.status_${activeLibraryCharacter.library_status ?? "draft"}`)}</strong></div>
                                      <div><span>{t("characters.bindings")}</span><strong>{summary.completeCount}/{summary.bindingCount || 1}</strong></div>
                                      <div><span>{t("characters.referenceAudio")}</span><strong>{referenceSampleCount(activeLibraryCharacter.reference_audio_groups)}</strong></div>
                                      <div><span>{t("characters.projectMatch")}</span><strong>{linkedProjectRoles.length}</strong></div>
                                    </div>

                                    <section className="role-config-card">
                                      <div className="role-config-head">
                                        <strong>{t("characters.identity")}</strong>
                                        <select value={activeLibraryCharacter.library_status ?? "draft"} onChange={(event) => updateLibraryCharacter(activeLibraryCharacter.id, { library_status: event.target.value as Character["library_status"] })}>
                                          <option value="draft">{t("characters.status_draft")}</option>
                                          <option value="partial">{t("characters.status_partial")}</option>
                                          <option value="confirmed">{t("characters.status_confirmed")}</option>
                                          <option value="archived">{t("characters.status_archived")}</option>
                                        </select>
                                      </div>
                                      <div className="role-config-form">
                                        <label>
                                          <span>{t("characters.roleName")}</span>
                                          <input value={activeLibraryCharacter.name} onChange={(event) => updateLibraryCharacter(activeLibraryCharacter.id, { name: event.target.value })} />
                                        </label>
                                        <label>
                                          <span>{t("characters.tags")}</span>
                                          <input value={(activeLibraryCharacter.tags ?? []).join("，")} onChange={(event) => updateLibraryCharacterListField(activeLibraryCharacter.id, "tags", event.target.value)} />
                                        </label>
                                        <label className="wide">
                                          <span>{t("characters.aliases")}</span>
                                          <textarea rows={2} value={(activeLibraryCharacter.aliases ?? []).join("，")} onChange={(event) => updateLibraryCharacterListField(activeLibraryCharacter.id, "aliases", event.target.value)} />
                                        </label>
                                        <label className="wide">
                                          <span>{t("characters.notes")}</span>
                                          <textarea rows={2} value={activeLibraryCharacter.notes ?? ""} onChange={(event) => updateLibraryCharacter(activeLibraryCharacter.id, { notes: event.target.value })} />
                                        </label>
                                      </div>
                                    </section>

                                    <section className="role-config-card">
                                      <div className="role-config-head">
                                        <strong>{t("characters.ttsBindings")}</strong>
                                        {gptBinding ? (
                                          <span className={`tracker-chip ${gptComplete?.complete ? "ok" : "warn"}`}>{gptComplete?.complete ? t("characters.readyToGenerate") : `${t("characters.missingFields")}: ${gptComplete?.missing.join(", ")}`}</span>
                                        ) : (
                                          <button className="secondary-button compact-button" onClick={() => addGptBindingForCharacter(activeLibraryCharacter.id)}><Plus size={13} /> {t("characters.createGptBinding")}</button>
                                        )}
                                      </div>
                                      {bindingRows.length > 0 ? (
                                        <div className="role-binding-table">
                                          {bindingRows.map((row) => (
                                            <div className="role-binding-row" key={row.bindingId}>
                                              <div>
                                                <strong>{providerLabel(row.providerType)}</strong>
                                                <small>{row.profileName} · {row.serviceLabel || t("services.noService")}</small>
                                              </div>
                                              <div className="role-binding-fields">
                                                <span>{row.bindingId}</span>
                                                {row.complete ? <span>{t("characters.readyToGenerate")}</span> : row.missing.map((field) => <span className="missing" key={`${row.bindingId}-${field}`}>{field}</span>)}
                                                {row.binding.capabilities.slice(0, 2).map((capability) => <span key={`${row.bindingId}-${capability}`}>{capability}</span>)}
                                              </div>
                                            </div>
                                          ))}
                                        </div>
                                      ) : (
                                        <div className="role-empty-config">{t("characters.noTtsBindingsHint")}</div>
                                      )}
                                      {gptBinding ? (
                                        <>
                                          <div className="role-config-subhead">
                                            <strong>{t("characters.gptBinding")}</strong>
                                            <span>{t("characters.modelCatalogService")}</span>
                                          </div>
                                          <div className="role-config-form">
                                            <label>
                                              <span>{t("characters.logsName")}</span>
                                              <input value={stringConfigValue(gptConfig.logs_name)} onChange={(event) => updateLibraryBindingConfig(activeLibraryCharacter.id, gptBinding.binding_id, { logs_name: event.target.value })} />
                                            </label>
                                            <label>
                                              <span>{t("services.selectedService")}</span>
                                              <select value={gptBinding.service_id ?? ""} onChange={(event) => updateLibraryBindingConfig(activeLibraryCharacter.id, gptBinding.binding_id, {}, { service_id: event.target.value || null })}>
                                                <option value="">{t("services.noService")}</option>
                                                {gptSovitsBindingServiceOptions.map((service) => (
                                                  <option value={service.serviceId} key={service.serviceId}>{service.label}</option>
                                                ))}
                                              </select>
                                            </label>
                                            <label>
                                              <span>{t("characters.defaultGpt")}</span>
                                              <select value={stringConfigValue(gptConfig.gpt_weights_path)} onChange={(event) => updateLibraryBindingConfig(activeLibraryCharacter.id, gptBinding.binding_id, { gpt_weights_path: event.target.value })}>
                                                <option value="">{t("status.auto")}</option>
                                                {(voiceCandidates?.gpt_sovits.gpt_weights ?? []).map((item) => (
                                                  <option value={item.path} key={item.path}>{item.name}</option>
                                                ))}
                                              </select>
                                            </label>
                                            <label>
                                              <span>{t("characters.defaultSovits")}</span>
                                              <select value={stringConfigValue(gptConfig.sovits_weights_path)} onChange={(event) => updateLibraryBindingConfig(activeLibraryCharacter.id, gptBinding.binding_id, { sovits_weights_path: event.target.value })}>
                                                <option value="">{t("status.auto")}</option>
                                                {(voiceCandidates?.gpt_sovits.sovits_weights ?? []).map((item) => (
                                                  <option value={item.path} key={item.path}>{item.name}</option>
                                                ))}
                                              </select>
                                            </label>
                                            <label>
                                              <span>{t("characters.referenceAudio")}</span>
                                              <select value={stringConfigValue(gptConfig.ref_audio_path)} onChange={(event) => updateLibraryBindingConfig(activeLibraryCharacter.id, gptBinding.binding_id, { ref_audio_path: event.target.value })}>
                                                <option value="">{t("status.unset")}</option>
                                                {referenceSamples.map((sample) => (
                                                  <option value={sample.path} key={sample.path}>{shortPath(sample.path)}</option>
                                                ))}
                                              </select>
                                            </label>
                                            <div className="wide role-audio-uploader">
                                              <ReferenceAudioInput
                                                label={t("characters.addReferenceAudio")}
                                                value={stringConfigValue(gptConfig.ref_audio_path)}
                                                onUpload={(file) => uploadCharacterReference(activeLibraryCharacter.id, gptBinding.binding_id, file)}
                                              />
                                              <small>{t("characters.referenceUploadHint")}</small>
                                            </div>
                                            <label className="wide">
                                              <span>{t("characters.promptText")}</span>
                                              <textarea rows={2} value={stringConfigValue(gptConfig.prompt_text)} onChange={(event) => updateLibraryBindingConfig(activeLibraryCharacter.id, gptBinding.binding_id, { prompt_text: event.target.value })} />
                                            </label>
                                          </div>
                                        </>
                                      ) : (
                                        <div className="role-empty-config">{t("characters.noGptBindingHint")}</div>
                                      )}
                                    </section>
                                  </div>
                                );
                              })() : (
                                <div className="role-empty-config role-detail-empty-state">
                                  <strong>{t("characters.roleDetailEmptyTitle")}</strong>
                                  <span>{t("characters.roleDetailEmptyHint")}</span>
                                </div>
                              )}
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
            <div className="filters-row">
              <label className="search-field">
                <Search size={15} />
                <input value={searchText} onChange={(event) => setSearchText(event.target.value)} placeholder={t("filters.search")} />
              </label>
              <span className="line-toolbar-count">{visibleLineLabel}</span>
              <details className="line-filter-menu">
                <summary title={lineFilterTitle}>
                  <SlidersHorizontal size={14} />
                  <span>{t("filters.more")}</span>
                  {lineToolbarState.activeBadgeVisible && <b>{t("filters.active")}</b>}
                </summary>
                <div className="line-filter-popover">
                  <label>
                    <span>{t("filters.provider")}</span>
                    <select value={providerFilter} onChange={(event) => setProviderFilter(event.target.value)} aria-label={t("filters.provider")}>
                      <option value="all">{t("filters.all")}</option>
                      {providerOptions.map((provider) => <option value={provider} key={provider}>{provider}</option>)}
                    </select>
                  </label>
                  <label>
                    <span>{t("filters.status")}</span>
                    <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as LineStatusFilter)} aria-label={t("filters.status")}>
                      <option value="all">{t("filters.all")}</option>
                      <option value="not-generated">{t("filters.notGenerated")}</option>
                      <option value="queued">{t("filters.queued")}</option>
                      <option value="running">{t("filters.running")}</option>
                      <option value="completed">{t("filters.completed")}</option>
                      <option value="failed">{t("filters.failed")}</option>
                    </select>
                  </label>
                  {lineToolbarState.clearButtonVisible && (
                    <button
                      className="secondary-button compact-button"
                      onClick={() => {
                        setProviderFilter("all");
                        setStatusFilter("all");
                      }}
                      type="button"
                    >
                      {t("filters.clear")}
                    </button>
                  )}
                </div>
              </details>
              <div className="task-actions">
                {isGenerating && activeJob && (
                  <span className="generation-progress-inline" aria-label={t("queue.progressLabel", { percent: Math.round((activeJob.progress ?? 0) * 100) })}>
                    {t("queue.progressLabel", { percent: Math.round((activeJob.progress ?? 0) * 100) })}
                  </span>
                )}
                {isGenerating && activeJob ? (
                  <button className="secondary-button cancel-button" onClick={() => void cancelGeneration()} title={t("actions.cancel")}>
                    <X size={15} /> {t("actions.cancel")}
                  </button>
                ) : (
                  <button className="primary-button" onClick={() => void runSelectedQueue()} disabled={isGenerating || filteredLines.length === 0}>
                    <RefreshCw size={15} /> {selectedLineIds.length > 0 ? t("app.queueSelected") : t("app.queueFiltered")}
                  </button>
                )}
              </div>
            </div>
            <div className="role-strip">
              <div className="role-pill-row">
                <button
                  aria-pressed={characterFilter === "all"}
                  aria-label={`${t("filters.all")} · ${project.lines.length}`}
                  className={`role-pill role-pill-all ${characterFilter === "all" ? "active" : ""}`}
                  onClick={() => {
                    setCharacterFilter("all");
                    setExpandedLineId(null);
                  }}
                  title={t("filters.all")}
                >
                  <span className="role-pill-label">{t("filters.all")}</span>
                  <span className="role-pill-count">{project.lines.length}</span>
                </button>
                {projectRoleRows.map((role, index) => {
                  const isActive = characterFilter === role.id;
                  return (
                    <button
                      aria-pressed={isActive}
                      aria-label={`${role.name} · ${t("characters.lines", { count: role.lineCount })}`}
                      className={`role-pill ${isActive ? "active" : ""} ${roleAccentClass(index)}`}
                      key={role.id}
                      onClick={() => focusRoleChip(role.id)}
                      title={`${role.name} · ${t("characters.lines", { count: role.lineCount })}`}
                    >
                      <span className="role-pill-label">{role.name}</span>
                      <span className="role-pill-count">{role.lineCount}</span>
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="line-table line-card-list">
              {displayedLines.map((line) => {
                const summary = summarizeLineHistory(lineHistoryForLine(manifest, line));
                const queueItem = activeJob?.items.find((item) => item.line_uid ? item.line_uid === (line.line_uid ?? line.id) : item.line_id === line.id);
                const visibleTone = queueItem ? queueStatusTone(queueItem.status) : summary.tone;
                const visibleLabel = queueItem ? t(`status.${queueItem.status}`) : summaryLabel(summary, t);
                const selected = selectedLineIds.includes(line.id);
                const rowBinding = lineBinding(line, resolvedCharacters);
                const canGenerateLine = Boolean(rowBinding);
                const historyVersions = lineHistoryForLine(manifest, line)?.versions ?? [];
                const roleIndex = Math.max(0, projectRoleRows.findIndex((role) => role.id === line.character_id));
                const roleRow = projectRoleRows[roleIndex];
                const secondaryBadges = lineCardSecondaryBadges(historyVersions.at(-1), historyVersions.length);
                const preflightItem = preflightByLine.get(line.line_uid ?? line.id);
                const preflightTone = preflightLineTone(preflightItem);
                const preflightLabelKey = preflightLineLabelKey(preflightItem);
                const preflightLoadState = preflightItem?.selected_service_id ? serviceLoadStates[preflightItem.selected_service_id] : undefined;
                const loadTone = preflightLoadTone(preflightItem, preflightLoadState?.loaded_signature);
                const loadLabelKey = preflightLoadLabelKey(preflightItem, preflightLoadState?.loaded_signature);
                const expanded = expandedLineId === line.id;
                return (
                  <article
                    className={`line-row line-card ${activeLineId === line.id ? "active" : ""} ${expanded ? "expanded" : ""} ${roleAccentClass(roleIndex)}`}
                    data-queue-state={queueItem?.status ?? summary.tone}
                    key={line.id}
                    onClick={() => focusLine(line.id)}
                  >
                    <div className="line-primary-row">
                      <label className="line-check" onClick={(event) => event.stopPropagation()}>
                        <input type="checkbox" checked={selected} onChange={() => setSelectedLineIds((current) => toggleLineSelection(current, line.id))} />
                      </label>
                      <div className="line-speaker">
                        <RoleAvatar avatarPath={roleRow?.avatarPath} fallback={roleRow?.avatarFallback ?? avatarFallback(characterName(resolvedCharacters, line.character_id))} size="md" />
                        <strong>{characterName(resolvedCharacters, line.character_id)}</strong>
                      </div>
                      <div className="line-copy">
                        {formatScriptNote(line.note) && <span className="line-note" title={formatScriptNote(line.note)}>{formatScriptNote(line.note)}</span>}
                        <p className="line-dialogue">{line.text}</p>
                      </div>
                      <StatusPill tone={visibleTone} label={visibleLabel} />
                    </div>
                    <div className="line-secondary-row">
                      {secondaryBadges.map((badge) => <span className="line-meta-chip" key={lineCardBadgeKey(badge)}>{lineCardBadgeLabel(badge, t)}</span>)}
                      {preflightTone && preflightLabelKey && (
                        <span className={`line-meta-chip ${preflightTone}`} title={preflightItem?.reason ?? preflightItem?.load_signature ?? ""}>
                          {t(preflightLabelKey)}
                        </span>
                      )}
                      {loadTone && loadLabelKey && (
                        <span className={`line-meta-chip ${loadTone}`} title={preflightItem?.load_signature ?? ""}>
                          {t(loadLabelKey)}
                        </span>
                      )}
                      {queueItem?.queue_position && <span className="line-meta-chip neutral">{t("queue.position", { position: queueItem.queue_position })}</span>}
                      {queueItem?.cluster_size && queueItem.cluster_size > 1 && (
                        <span className="line-meta-chip ok" title={queueItem.cluster_key}>
                          {t("queue.cluster", { current: queueItem.cluster_position ?? 1, total: queueItem.cluster_size })}
                        </span>
                      )}
                      {queueItem?.error && <span className="line-meta-chip danger" title={queueItem.error}>{t("queue.routeError")}</span>}
                      {!canGenerateLine && <span className="line-meta-chip attention">{t("status.needsSetup")}</span>}
                      <span className="row-actions">
                        <button className="icon-button tiny" onClick={(event) => { event.stopPropagation(); playLine(line); }} title={t("actions.playLatest")}><Play size={14} /></button>
                        <button className="icon-button tiny" disabled={!canGenerateLine} onClick={(event) => { event.stopPropagation(); void runQueue([line]); }} title={canGenerateLine ? t("actions.regenerate") : t("inspector.needsTemporaryBinding")}><RefreshCw size={14} /></button>
                      </span>
                    </div>
                    {queueItem && <div className="line-progress"><span style={{ width: `${Math.round(queueItem.progress * 100)}%` }} /></div>}
                    {expanded && (
                      <LineHistoryPanel
                        versions={historyVersions}
                        services={visibleServices}
                        selectedVersionId={selectedHistoryVersions[line.id]}
                        onSelect={(version) => selectHistoryVersion(line.id, version)}
                        onDelete={(version) => void removeHistoryVersion(line, version)}
                        t={t}
                      />
                    )}
                  </article>
                );
              })}
              {hasMoreFilteredLines && (
                <div className="line-scroll-sentinel" ref={lineLoadMoreRef}>
                  {t("table.loadingMore", { visible: displayedLines.length, total: filteredLines.length })}
                </div>
              )}
              {filteredLines.length === 0 && (
                <div className="empty-row table-empty line-empty-state">
                  <strong>{t("empty.noLines")}</strong>
                  <button
                    className="secondary-button compact-button"
                    type="button"
                    onClick={() => {
                      setSearchText("");
                      setCharacterFilter("all");
                      setProviderFilter("all");
                      setStatusFilter("all");
                    }}
                  >
                    {t("filters.clear")}
                  </button>
                </div>
              )}
            </div>
          </div>

          <aside className={`inspector inspector-${activeInspectorMode}`}>
            {activeLine && (
              <div className="inspector-stack">
                {inspectorVersionContextVisible(activeInspectorMode, selectedHistoryVersion?.version_id) && selectedHistoryVersion && activeVersionDraft ? (
                  <section className="inspector-card selected-version-card">
                    <div className="selected-version-head">
                      <div>
                        <span>{t("inspector.selectedVersion")}</span>
                        <strong>{selectedHistoryVersion.version_id} · {providerLabel(selectedHistoryVersion.provider_type ?? selectedHistoryVersion.engine)}</strong>
                      </div>
                      <button className="secondary-button compact-button" onClick={() => clearSelectedHistoryVersion(activeLine.id)}>{t("inspector.returnToCurrentBinding")}</button>
                    </div>

                    <div className="version-param-grid">
                      <div>
                        <span>{t("inspector.service")}</span>
                        <strong>{selectedHistoryVersionTags?.service ?? selectedHistoryVersion.service_id ?? t("inspector.autoRoute")}</strong>
                      </div>
                      <div>
                        <span>{t("inspector.configScheme")}</span>
                        <strong>{selectedHistoryVersionTags?.config ?? selectedHistoryVersion.binding_id ?? selectedHistoryVersion.profile}</strong>
                      </div>
                      <div>
                        <span>{t("inspector.verificationLevel")}</span>
                        <strong>{selectedHistoryVersionTags ? t(`history.verification.${selectedHistoryVersionTags.verification}`) : String(selectedHistoryVersion.metadata?.load_verification_level ?? selectedHistoryVersion.metadata?.verification_level ?? t("status.unset"))}</strong>
                      </div>
                    </div>

                    <details className="version-param-details">
                      <summary>{t("inspector.parameterSnapshot")}</summary>
                      <pre>{formatVersionParameters(activeVersionDraft.parameters ?? selectedHistoryVersion.parameters ?? {})}</pre>
                    </details>

                    {selectedHistoryVersion.error && <p className="selected-version-error">{selectedHistoryVersion.error}</p>}
                  </section>
                ) : null}

                {activeInspectorSections.includes("config") && (
                  <section className="inspector-card inspector-config-card">
                    <div className="inspector-section-head compact generation-method-head">
                      <div>
                        <strong>{t("inspector.voiceSetup")}</strong>
                      </div>
                      <button
                        className={`generation-method-state-pill tone-${activeInspectorDiagnostics.tone}`}
                        onClick={() => setDiagnosticsExpanded((current) => !current)}
                        type="button"
                        title={diagnosticsExpanded || activeInspectorDiagnostics.expanded ? t("inspector.hideDiagnosticsShort") : t("inspector.showDiagnosticsShort")}
                      >
                        <span className="state-dot" />
                        {activeInspectorDiagnostics.visible && <span>{t(`inspector.diagnosticsReason.${activeInspectorDiagnostics.reason}`)}</span>}
                        {activeInspectorDiagnostics.visible && (
                          <span className="state-action">
                            {diagnosticsExpanded || activeInspectorDiagnostics.expanded ? t("inspector.hideDiagnosticsShort") : t("inspector.showDiagnosticsShort")}
                          </span>
                        )}
                      </button>
                    </div>
                    <div className={`generation-method-panel method-${activeGenerationMethod}`}>
                      <div className="voice-route-summary compact-route-summary" aria-label={t("inspector.routeAndVoice")}>
                        <div>
                          <span>{t("inspector.currentVoice")}</span>
                          <strong title={activeProfileLabel}>{activeProfileLabel}</strong>
                          <small title={activeBindingLabel}>{activeBindingLabel}</small>
                        </div>
                        <div>
                          <span>{t("inspector.routeService")}</span>
                          <strong title={activeServiceLabel}>{activeServiceLabel}</strong>
                          <small title={activeServiceContract}>{activeServiceContract}</small>
                        </div>
                      </div>
                      <details
                        className="inspector-more-settings route-settings"
                        key={`${activeLine?.id ?? "none"}-${activeGenerationMethod}`}
                        onToggle={(event) => setRouteSettingsOpen(event.currentTarget.open)}
                        open={routeSettingsOpen}
                      >
                        <summary>{t("inspector.routeSettings")}</summary>
                        <div className="inspector-more-body">
                          <div className="generation-method-tabs" role="tablist" aria-label={t("inspector.generationMethod")}>
                            {generationMethods.map((method) => (
                              <button
                                className={`generation-method-tab ${activeGenerationMethod === method.id ? "active" : ""}`}
                                key={method.id}
                                onClick={() => selectGenerationMethod(method.id)}
                                role="tab"
                                type="button"
                                aria-selected={activeGenerationMethod === method.id}
                                aria-label={`${t(method.labelKey)} · ${t(method.hintKey)}`}
                                title={t(method.hintKey)}
                              >
                                <strong>{t(method.labelKey)}</strong>
                              </button>
                            ))}
                          </div>
                          {activeGenerationMethod === "commercial" && (
                            <label className="resource-field">
                              <span>{t("inspector.commercialProvider")}</span>
                              <select value={activeProvider} onChange={(event) => selectGenerationProvider(event.target.value as ProviderType)}>
                                <option value="openai">OpenAI</option>
                                <option value="gemini">Gemini</option>
                                <option value="xai">xAI</option>
                                <option value="volcengine">Volcengine</option>
                                <option value="generic-http">{t("inspector.genericHttp")}</option>
                                {activeProvider === "vibevoice" && <option value="vibevoice">VibeVoice Legacy</option>}
                              </select>
                            </label>
                          )}
                          <div className="field-grid compact-field-grid voice-route-grid">
                            <label>
                              <span>{t(activeGenerationRouteLabels.profileLabelKey)}</span>
                              <select value={activeVersionDraft?.profile ?? lineProfile(activeLine, resolvedCharacters)} onChange={(event) => {
                                if (activeVersionDraft) {
                                  updateActiveVersionDraft({ profile: event.target.value });
                                } else {
                                  updateLine(activeLine.id, { profile_override: event.target.value, binding_override: null, service_override: null, engine_override: null });
                                }
                              }}>
                                {activeLine.temporary_binding && <option value={activeLine.temporary_binding.binding_id}>{t("inspector.temporaryBinding")}</option>}
                                {activeProfiles.map((profile) => <option value={profile.id} key={profile.id}>{profile.name}</option>)}
                                {activeProfiles.length === 0 && <option value={lineProfile(activeLine, resolvedCharacters)}>{lineProfile(activeLine, resolvedCharacters) || t("inspector.noProfile")}</option>}
                              </select>
                            </label>
                            <label>
                              <span>{t(activeGenerationRouteLabels.bindingLabelKey)}</span>
                              <select value={activeVersionDraft?.binding_id ?? activeLine.binding_override ?? ""} onChange={(event) => {
                                if (activeVersionDraft) {
                                  updateActiveVersionDraft({ binding_id: event.target.value || null });
                                } else {
                                  updateLine(activeLine.id, { binding_override: event.target.value || null, service_override: null });
                                }
                              }}>
                                {activeVersionDraft && <option value={activeVersionDraft.binding_id ?? ""}>{t("inspector.versionDraft")} · {activeVersionDraft.binding_id ?? selectedHistoryVersion?.version_id}</option>}
                                {activeLine.temporary_binding && <option value="">{t("inspector.temporaryBinding")} · {activeLine.temporary_binding.provider_type}</option>}
                                {!activeLine.temporary_binding && <option value="">{t("inspector.profileDefault")}{activeBinding ? ` · ${activeBinding.provider_type}` : ""}</option>}
                                {activeBindings.map((binding) => <option value={binding.binding_id} key={binding.binding_id}>{binding.provider_type} · {binding.binding_id}</option>)}
                              </select>
                            </label>
                            <label>
                              <span>{t(activeGenerationRouteLabels.serviceLabelKey)}</span>
                              <select value={activeSelectedServiceUnavailable ? "" : (activeVersionDraft?.service_id ?? lineServiceId(activeLine, resolvedCharacters) ?? "")} onChange={(event) => {
                                const nextServiceId = event.target.value || null;
                                if (activeVersionDraft) {
                                  updateActiveVersionDraft({
                                    service_id: nextServiceId,
                                    parameters: clearServiceScopedBindingConfig(activeProvider, activeVersionDraft.parameters)
                                  });
                                } else {
                                  updateLineService(activeLine.id, nextServiceId);
                                }
                              }}>
                                <option value="">{t("inspector.autoRoute")}</option>
                                {activeRouteServices.length === 0 && <option value="" disabled>{t("inspector.noRoutableService")}</option>}
                                {activeRouteServices.map((service) => <option value={service.service_id} key={service.service_id ?? service.engine}>{service.display_name ?? service.service_id} · {service.resource_group ?? t("status.resource")}</option>)}
                              </select>
                            </label>
                          </div>
                          {activeLine.temporary_binding && !activeVersionDraft && (
                            <button
                              className="secondary-button compact-button route-clear-temporary"
                              type="button"
                              onClick={() => clearTemporaryBinding(activeLine.id)}
                            >
                              {t("inspector.clearTemporaryBinding")}
                            </button>
                          )}
                        </div>
                      </details>
                    </div>
                    {(diagnosticsExpanded || activeInspectorDiagnostics.expanded) && (
                      <div className={`load-signature-panel inspector-inline-diagnostics tone-${activeInspectorDiagnostics.tone} ${activeServiceLoadState?.last_error ? "attention" : ""}`}>
                        <div>
                          <span>{t("inspector.currentLoadState")}</span>
                          <strong>{activeServiceLoadState?.loaded ? t("inspector.loadStateLoaded") : t("inspector.loadStateEmpty")}</strong>
                        </div>
                        <code title={activeServiceLoadState?.loaded_signature ?? ""}>
                          {activeServiceLoadState?.loaded_signature ? compactSignature(activeServiceLoadState.loaded_signature) : t("inspector.loadStateUnknown")}
                        </code>
                        {activeExpectedLoadSignature && (
                          <small>{t("inspector.expectedLoadSignature")}: {compactSignature(activeExpectedLoadSignature)}</small>
                        )}
                        {activeServiceLoadState?.verification_level && <small>{t("inspector.loadVerificationLevel")}: {activeServiceLoadState.verification_level}</small>}
                        {activeServiceLoadState?.last_error && <small className="load-state-error">{t("inspector.lastLoadError")}: {activeServiceLoadState.last_error}</small>}
                      </div>
                    )}
                  </section>
                )}

                {activeInspectorSections.includes("reference") && (
                  <section className="inspector-card reference-panel inspector-reference-card">
                    <div className="inspector-section-head compact reference-section-head">
                      <div>
                        <strong><Library size={15} /> {activeGenerationMethod === "commercial" ? t("inspector.apiVoiceReference") : t("inspector.voiceReference")}</strong>
                      </div>
                    </div>

                    {!activeLine.temporary_binding && !activeBinding ? (
                      <div className="reference-setup-callout attention" aria-live="polite">
                        <span>{t("inspector.needsTemporaryBindingShort")}</span>
                        <button className="secondary-button compact-button" type="button" onClick={() => setTemporaryBindingProvider(activeLine.id, "indextts")}>{t("inspector.createIndexTemporary")}</button>
                      </div>
                    ) : null}

                    {activeProvider === "gpt-sovits" && (
                      <>
                        <div className="gpt-reference-compact">
                          <div className="gpt-resource-summary-grid">
                            <div>
                              <span>{t("characters.logsName")}</span>
                              <strong>{stringConfig(activeBindingConfig.logs_name) || t("status.unset")}</strong>
                            </div>
                            <div>
                              <span>{t("inspector.service")}</span>
                              <strong>{activeServiceLabel}</strong>
                              <small title={activeServiceContract}>{activeServiceContract}</small>
                            </div>
                            <div>
                              <span>{t("inspector.currentReference")}</span>
                              <strong>{activeLogsReferenceSample?.display_label ?? shortPath(stringConfig(activeBindingConfig.ref_audio_path)) ?? t("status.unset")}</strong>
                            </div>
                          </div>
                          {activeReferenceAudioPath && isLocalAudioAsset(activeReferenceAudioPath) && (
                            <div className="active-reference-player">
                              <div>
                                <span>{t("inspector.selectedReferenceAudio")}</span>
                                <strong title={activeReferenceAudioLabel}>{activeReferenceAudioLabel}</strong>
                              </div>
                              <WaveformPlayer audioPath={activeReferenceAudioPath} label={activeReferenceAudioLabel} compact />
                            </div>
                          )}

                          <details className="inspector-more-settings reference-settings">
                            <summary>{t("inspector.weightsAndReference")}</summary>
                            <div className="inspector-more-body">
                              <div className="gpt-resource-control-grid">
                                <div className="gpt-resource-column">
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
                                </div>

                                <div className="gpt-resource-column">
                                  <div className="logs-reference-picker">
                                    <label className="resource-field">
                                      <span>{t("inspector.logsReferenceAudio")}</span>
                                      <select
                                        value={activeLogsReferenceSample?.sample_id ?? ""}
                                        disabled={!activeLogsReferenceRequest || loadingLogsReferenceKey === activeLogsReferenceRequest?.key}
                                        onChange={(event) => {
                                          const sample = activeLogsReferenceSamples.find((item) => item.sample_id === event.target.value);
                                          if (sample) applyLogsReferenceSample(sample);
                                        }}
                                      >
                                        <option value="">{activeLogsReferenceRequest ? t("status.unset") : t("inspector.logsReferenceNeedsLogs")}</option>
                                        {activeLogsReferenceSamples.map((sample) => (
                                          <option value={sample.sample_id} key={sample.sample_id}>{sample.display_label}</option>
                                        ))}
                                      </select>
                                    </label>
                                    <button
                                      className="icon-button"
                                      disabled={!activeLogsReferenceRequest}
                                      onClick={() => {
                                        if (!activeLogsReferenceRequest) return;
                                        setLogsReferenceAudio((current) => {
                                          const next = { ...current };
                                          delete next[activeLogsReferenceRequest.key];
                                          return next;
                                        });
                                      }}
                                      title={t("inspector.refreshLogsReference")}
                                    >
                                      <RefreshCw size={14} />
                                    </button>
                                  </div>
                                  {isLogsReferenceFromOtherService && (
                                    <div className="empty-row compact attention">
                                      {t("inspector.logsReferenceServiceMismatch")}
                                    </div>
                                  )}
                                  {activeLogsReferenceRequest && activeLogsReferenceSamples.length === 0 && (
                                    <div className="empty-row compact">
                                      {loadingLogsReferenceKey === activeLogsReferenceRequest.key ? t("inspector.loadingLogsReference") : (activeLogsReferencePayload?.diagnostics?.[0]?.detail ?? t("inspector.noLogsReferenceAudio"))}
                                    </div>
                                  )}

                                  {activeLogsReferenceSample && (
                                    <div className="logs-reference-preview compact-reference-preview">
                                      <div>
                                        <span>{t("inspector.textSource")}: {activeLogsReferenceSample.text_source || t("status.unset")}</span>
                                        <strong>{activeLogsReferenceSample.text || t("inspector.emptyPromptText")}</strong>
                                      </div>
                                      {isLocalAudioAsset(activeLogsReferenceSample.path) && <WaveformPlayer audioPath={activeLogsReferenceSample.path} label={activeLogsReferenceSample.display_label} />}
                                    </div>
                                  )}
                                </div>
                              </div>

                              <div className="gpt-manual-reference-card">
                                <label className="resource-field manual-reference-text-field">
                                  <span>{t("inspector.promptText")}</span>
                                  <textarea value={stringConfig(activeBindingConfig.prompt_text)} onChange={(event) => updateActiveBindingConfig({ prompt_text: event.target.value })} placeholder={t("inspector.promptPlaceholder")} rows={3} />
                                </label>
                                <div className="manual-reference-audio-field">
                                  <ReferenceAudioInput
                                    label={t("inspector.referenceAudio")}
                                    value={stringConfig(activeBindingConfig.ref_audio_path)}
                                    onUpload={(file) => uploadLineReference(file, "ref_audio_path")}
                                  />
                                </div>
                              </div>
                            </div>
                          </details>
                        </div>
                      </>
                    )}

                    {activeProvider === "indextts" && (
                      <div className="index-temporary-panel">
                        <ReferenceAudioInput
                          label={t("inspector.uploadVoiceReference")}
                          value={stringConfig(activeBindingConfig.voice)}
                          onUpload={(file) => uploadLineReference(file, "voice")}
                        />
                        <details className="inspector-more-settings reference-settings">
                          <summary>{t("inspector.emotionAndParams")}</summary>
                          <div className="inspector-more-body">
                            <label className="resource-field index-emotion-mode-field">
                              <span>{t("inspector.emotionMode")}</span>
                              <select value={indexEmotionMode} onChange={(event) => updateActiveBindingConfig({ emotion_mode: event.target.value })}>
                                {INDEX_EMOTION_MODE_OPTIONS.map((mode) => (
                                  <option value={mode.id} key={mode.id}>{t(mode.labelKey)}</option>
                                ))}
                              </select>
                            </label>
                            {indexEmotionMode === "emotion_text" && (
                              <label className="resource-field">
                                <span>{t("inspector.emotionText")}</span>
                                <input value={stringConfig(activeBindingConfig.emotion_text)} onChange={(event) => updateActiveBindingConfig({ emotion_text: event.target.value })} placeholder={activeLine.note || t("inspector.emotionTextPlaceholder")} />
                              </label>
                            )}
                            {indexEmotionMode === "emotion_audio" && (
                              <ReferenceAudioInput
                                label={t("inspector.uploadEmotionReference")}
                                value={stringConfig(activeBindingConfig.emotion_audio)}
                                onUpload={(file) => uploadLineReference(file, "emotion_audio")}
                              />
                            )}
                            {indexEmotionMode === "emotion_vector" && (
                              <label className="resource-field">
                                <span>{t("inspector.emotionVector")}</span>
                                <input value={vectorConfig(activeBindingConfig.emotion_vector)} onChange={(event) => updateActiveBindingConfig({ emotion_vector: parseVectorConfig(event.target.value) })} placeholder="0,0,0,0,0,0,0,0" />
                              </label>
                            )}
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
                          </div>
                        </details>
                      </div>
                    )}

                    {activeProvider === "cosyvoice" && (
                      <div className="cosyvoice-temporary-panel">
                        <label className="resource-field cosyvoice-mode-field">
                          <span>{t("inspector.cosyVoiceMode")}</span>
                          <select value={cosyVoiceMode} onChange={(event) => updateActiveBindingConfig({ mode: event.target.value })}>
                            {COSY_VOICE_MODE_OPTIONS.map((mode) => (
                              <option value={mode.id} key={mode.id}>{t(mode.labelKey)}</option>
                            ))}
                          </select>
                        </label>
                        {cosyVoiceNeedsSpeaker ? (
                          <label className="resource-field">
                            <span>{t("inspector.cosySpeaker")}</span>
                            <input value={stringConfig(activeBindingConfig.speaker_id)} onChange={(event) => updateActiveBindingConfig({ speaker_id: event.target.value })} placeholder={t("inspector.cosySpeakerPlaceholder")} />
                          </label>
                        ) : null}
                        {cosyVoiceNeedsPrompt ? (
                          <>
                            <ReferenceAudioInput
                              label={t("inspector.cosyReferenceAudio")}
                              value={stringConfig(activeBindingConfig.prompt_audio_path)}
                              onUpload={(file) => uploadLineReference(file, "prompt_audio_path")}
                            />
                            <label className="resource-field">
                              <span>{t("inspector.cosyPromptText")}</span>
                              <textarea value={stringConfig(activeBindingConfig.prompt_text)} onChange={(event) => updateActiveBindingConfig({ prompt_text: event.target.value })} placeholder={t("inspector.cosyPromptTextPlaceholder")} rows={3} />
                            </label>
                          </>
                        ) : null}
                        {cosyVoiceNeedsInstruction && (
                          <label className="resource-field">
                            <span>{t("inspector.cosyInstruction")}</span>
                            <textarea value={stringConfig(activeBindingConfig.instruct_text)} onChange={(event) => updateActiveBindingConfig({ instruct_text: event.target.value })} placeholder={t("inspector.cosyInstructionPlaceholder")} rows={3} />
                          </label>
                        )}
                        <details className="inspector-more-settings reference-settings">
                          <summary>{t("inspector.advancedParams")}</summary>
                          <div className="inspector-more-body">
                            <div className="advanced-grid compact-cosyvoice-grid">
                              <label>
                                <span>{t("inspector.cosySpeed")}</span>
                                <input value={String(activeBindingConfig.speed ?? 1)} onChange={(event) => updateActiveBindingConfig({ speed: Number(event.target.value) })} />
                              </label>
                              <label>
                                <span>{t("inspector.cosySeed")}</span>
                                <input value={String(activeBindingConfig.seed ?? -1)} onChange={(event) => updateActiveBindingConfig({ seed: Number(event.target.value) })} />
                              </label>
                            </div>
                          </div>
                        </details>
                      </div>
                    )}

                    {activeProvider === "vibevoice" ? (
                      <div className="voice-source-summary">
                        <div className="empty-row">{t("inspector.legacyVibeVoice")}</div>
                      </div>
                    ) : showBackupReferenceSource ? (
                      <div className="voice-source-summary fallback-reference-source">
                        <label className="resource-field">
                          <span>{t("inspector.backupReferenceAudio")}</span>
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
                      </div>
                    ) : activeProvider === "gpt-sovits" || activeProvider === "indextts" || activeProvider === "cosyvoice" ? (
                      null
                    ) : (
                      <div className="commercial-reference-note">{t("inspector.commercialResourceHint")}</div>
                    )}
                  </section>
                )}

                <section className={`inspector-generate-dock inspector-speech-workbench tone-${activeInspectorDiagnostics.tone}`}>
                  <div className="speech-workbench-line">
                    <div className="speech-workbench-copy">
                      <div className="speech-workbench-title">
                        <strong>{characterName(resolvedCharacters, activeLine.character_id)}</strong>
                        <span className={`generate-status-light tone-${activeInspectorDiagnostics.tone} summary-${activeSummary.tone}`} title={summaryLabel(activeSummary, t)} />
                      </div>
                      {formatScriptNote(activeLine.note) && <p className="speech-workbench-note">{formatScriptNote(activeLine.note)}</p>}
                      <label className="speech-workbench-editor">
                        <span>{t("inspector.lineTextForGeneration")}</span>
                        <textarea
                          value={activeLineTextDraft}
                          onChange={(event) => updateLineTextDraft(activeLine.id, event.target.value)}
                          placeholder={activeLine.text}
                          rows={3}
                        />
                      </label>
                      {activeLineTextDraft !== activeLine.text && (
                        <button className="secondary-button compact-button speech-workbench-reset" type="button" onClick={() => resetLineTextDraft(activeLine.id)}>
                          {t("inspector.resetLineText")}
                        </button>
                      )}
                    </div>
                  </div>
                  {activePlayableVersion?.audio_path && (
                    <div className="generated-result-player">
                      <div>
                        <span>{t("inspector.generatedResult")}</span>
                        <strong>{activePlayableVersion.version_id}</strong>
                      </div>
                      <WaveformPlayer audioPath={activePlayableVersion.audio_path} label={activePlayableVersion.version_id} compact />
                    </div>
                  )}
                  <div className="generate-dock-actions">
                    <button className="primary-button inspector-generate-button" onClick={() => void runInspectorGeneration()} disabled={isGenerating || (!activeVersionDraft && !activeBinding)}>
                      {isGenerating ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
                      {activeVersionDraft ? t("inspector.generateFromVersion") : activeSummary.tone === "completed" ? t("actions.regenerate") : t("inspector.generateLine")}
                    </button>
                  </div>
                </section>
              </div>
            )}
            {!activeLine && (
              <div className="empty-row inspector-empty-state">
                <strong>{t("empty.noActiveLine")}</strong>
                <span>{t("empty.noActiveLineHint")}</span>
              </div>
            )}
          </aside>
        </section>
      </main>
      {confirmationDialog && (
        <div className="confirm-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) resolveConfirmation(false); }}>
          <section className={`confirm-modal tone-${confirmationDialog.tone}`} role="dialog" aria-modal="true" aria-labelledby="confirm-modal-title">
            <div className="confirm-modal-icon">
              <AlertCircle size={18} />
            </div>
            <div className="confirm-modal-copy">
              <h2 id="confirm-modal-title">{confirmationDialog.title}</h2>
              <p>{confirmationDialog.body}</p>
              {confirmationDialog.detail && <small>{confirmationDialog.detail}</small>}
            </div>
            <div className="confirm-modal-actions">
              <button className="secondary-button" type="button" onClick={() => resolveConfirmation(false)}>{confirmationDialog.cancelLabel}</button>
              <button className="primary-button" type="button" onClick={() => resolveConfirmation(true)}>{confirmationDialog.confirmLabel}</button>
            </div>
          </section>
        </div>
      )}
      {toasts.length > 0 && (
        <div className="toast-stack" role="region" aria-label="通知" aria-live="polite">
          {toasts.map((toast) => (
            <div key={toast.id} className={`toast toast-${toast.level}`} role="status">
              <span className="toast-message">{toast.message}</span>
              <button className="toast-close" type="button" aria-label="关闭通知" onClick={() => removeToast(toast.id)}>×</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );

  function focusFirstLineForCharacter(characterId: string) {
    const next = project.lines.find((line) => line.character_id === characterId);
    if (!next) return;
    const transition = lineFocusTransition({ activeLineId, expandedLineId }, next.id, "role");
    setActiveLineId(transition.activeLineId ?? "");
    setExpandedLineId(transition.expandedLineId);
  }

  function focusRoleChip(characterId: string) {
    focusFirstLineForCharacter(characterId);
    setCharacterFilter((current) => (current === characterId ? "all" : characterId));
    setExpandedLineId(null);
  }

  function updateLine(lineId: string, patch: Partial<ScriptLine>) {
    setProject((current) => ({
      ...current,
      lines: current.lines.map((line) => (line.id === lineId ? { ...line, ...patch } : line))
    }));
  }

  function updateActiveBindingConfig(patch: Record<string, unknown>) {
    if (!activeLine) return;
    if (activeVersionDraft) {
      updateActiveVersionDraft({ parameters: { ...activeVersionDraft.parameters, ...patch } });
      return;
    }
    upsertTemporaryBinding(activeLine.id, activeProvider, {
      configPatch: patch,
      serviceId: activeServiceId || activeBinding?.service_id || null,
      baseConfig: activeBindingConfig,
      sourceBindingId: activeBinding?.binding_id,
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
    if (!line) return;
    const binding = lineBinding(line, resolvedCharacters);
    const provider = (line.temporary_binding?.provider_type ?? binding?.provider_type ?? providerFromEngine(line.engine_override) ?? "indextts") as ProviderType;
    upsertTemporaryBinding(lineId, provider, {
      serviceId,
      baseConfig: clearServiceScopedBindingConfig(provider, line.temporary_binding?.config ?? binding?.config ?? defaultTemporaryConfig(provider, line)),
      sourceBindingId: binding?.binding_id,
    });
  }

  function upsertTemporaryBinding(
    lineId: string,
    provider: ProviderType,
    options: { configPatch?: Record<string, unknown>; serviceId?: string | null; replaceProvider?: boolean; baseConfig?: Record<string, unknown>; sourceBindingId?: string | null } = {}
  ) {
    setProject((current) => ({
      ...current,
      lines: current.lines.map((line) => {
        if (line.id !== lineId) return line;
        const existing = options.replaceProvider ? null : line.temporary_binding;
        const serviceId = options.serviceId !== undefined ? options.serviceId : existing?.service_id ?? defaultServiceForProvider(visibleServices, provider);
        const baseConfig = options.baseConfig ?? (existing?.provider_type === provider ? existing.config : defaultTemporaryConfig(provider, line));
        return {
          ...line,
          engine_override: null,
          profile_override: null,
          binding_override: null,
          service_override: null,
          temporary_binding: {
            binding_id: existing?.binding_id && existing.provider_type === provider ? existing.binding_id : options.sourceBindingId ?? `line-temp-${provider}`,
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
      setProject((current) => projectWithProjectCharacters(current, ensureProjectCharacters(current, characters).map((item) => {
          if (item.project_character_id !== projectCharacterId || !item.character_snapshot) return item;
          return { ...item, character_snapshot: updater(item.character_snapshot) };
        })
      ));
      return;
    }
    const libraryId = mapping?.library_character_id ?? projectCharacterId;
    setCharacters((current) => current.map((character) => (character.id === libraryId ? updater(character) : character)));
  }

  function updateProjectCharacter(nextProjectCharacter: ProjectCharacter) {
    setProject((current) => projectWithProjectCharacters(current, ensureProjectCharacters(current, characters).map((item) =>
        item.project_character_id === nextProjectCharacter.project_character_id ? nextProjectCharacter : item
      ))
    );
  }

  async function freezeRole(projectCharacterId: string) {
    if (!currentProjectId) {
      setNotice(t("empty.noProjectAction"));
      return;
    }
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
    if (!currentProjectId) {
      setNotice(t("empty.noProjectAction"));
      return;
    }
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
      const [payload, modelPayload] = await Promise.all([
        fetchLogsCandidates(selectedModelCatalogServiceId, true, 80).catch(() => scanCharacterLibrary(80)),
        fetchGptSovitsModelCatalog(selectedModelCatalogServiceId, 120).catch(() => ({ models: [], diagnostics: [] }))
      ]);
      setRoleLibraryCandidates(payload.candidates);
      setGptModelCatalog(modelPayload.models);
      setActiveModelCatalogId((current) => current && modelPayload.models.some((model) => model.id === current) ? current : modelPayload.models[0]?.id ?? null);
      const diagnosticCount = (payload.diagnostics?.length ?? 0) + (modelPayload.diagnostics?.length ?? 0);
      const diagnostics = diagnosticCount ? ` · ${diagnosticCount} ${t("characters.diagnostics")}` : "";
      setNotice(`${t("notice.roleScanDone", { count: payload.candidates.length + modelPayload.models.length })}${diagnostics}`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.roleScanFailed"));
    } finally {
      setIsScanningRoleLibrary(false);
    }
  }

  async function refreshModelCatalog() {
    setIsScanningModelCatalog(true);
    setNotice(t("notice.scanningRoles"));
    try {
      const payload = await fetchGptSovitsModelCatalog(selectedModelCatalogServiceId, 120);
      setGptModelCatalog(payload.models);
      setActiveModelCatalogId((current) => current && payload.models.some((model) => model.id === current) ? current : payload.models[0]?.id ?? null);
      const diagnostics = payload.diagnostics?.length ? ` · ${payload.diagnostics.length} ${t("characters.diagnostics")}` : "";
      setNotice(`${t("notice.roleScanDone", { count: payload.models.length })}${diagnostics}`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.roleScanFailed"));
    } finally {
      setIsScanningModelCatalog(false);
    }
  }

  function bindActiveModelToProjectRole() {
    if (!activeProjectCharacter || !activeModelCatalogItem) return;
    const binding = gptSovitsProjectBindingFromModel(activeProjectCharacter.project_character_id, activeModelCatalogItem, activeModelSelectedSample);
    setProject((current) => {
      const nextProjectCharacters = ensureProjectCharacters(current, characters).map((item) =>
        item.project_character_id === activeProjectCharacter.project_character_id
          ? { ...item, project_binding: binding, match_status: item.match_status ?? "manual" }
          : item
      );
      return projectWithProjectCharacters(current, nextProjectCharacters);
    });
    setNotice(t("notice.roleSaved"));
  }

  function clearActiveProjectRoleBinding() {
    if (!activeProjectCharacter) return;
    setProject((current) => {
      const nextProjectCharacters = ensureProjectCharacters(current, characters).map((item) =>
        item.project_character_id === activeProjectCharacter.project_character_id
          ? { ...item, project_binding: null }
          : item
      );
      return projectWithProjectCharacters(current, nextProjectCharacters);
    });
    setNotice(t("notice.roleSaved"));
  }

  function applyLibraryCharacterToProjectRole(character: Character | null) {
    if (!activeProjectCharacter || !character) return;
    setProject((current) => {
      const nextProjectCharacters = ensureProjectCharacters(current, characters).map((item) =>
        item.project_character_id === activeProjectCharacter.project_character_id
          ? {
              ...item,
              library_character_id: character.id,
              mode: "reference" as const,
              character_snapshot: null,
              project_binding: null,
              match_status: "matched" as const
            }
          : item
      );
      return projectWithProjectCharacters(current, nextProjectCharacters);
    });
    setActiveLibraryCharacterId(character.id);
    setActiveRoleCandidateId(null);
    setActiveModelCatalogId(null);
    setNotice(t("notice.roleSaved"));
  }

  function writeActiveModelToLibrary() {
    if (!activeProjectCharacter || !activeModelCatalogItem) return;
    const libraryId = activeProjectCharacter.library_character_id ?? stableLibraryCharacterId(activeProjectCharacter.name, activeProjectCharacter.project_character_id);
    const binding = gptSovitsProjectBindingFromModel(libraryId, activeModelCatalogItem, activeModelSelectedSample);
    const profileId = `${libraryId}-gpt-sovits`;
    const bindingId = `${libraryId}-gpt-sovits-binding`;
    const libraryBinding: VoiceBinding = {
      ...binding,
      binding_id: bindingId,
      service_id: binding.service_id,
      config: {
        ...binding.config,
        path_service_id: binding.service_id ?? undefined
      }
    };
    setCharacters((current) => {
      const existing = current.find((character) => character.id === libraryId);
      const baseCharacter: Character = existing ?? {
        id: libraryId,
        name: activeProjectCharacter.name,
        aliases: [activeProjectCharacter.name],
        nicknames: [],
        match_names: [activeModelCatalogItem.logs_name ?? activeModelCatalogItem.name],
        notes: "",
        tags: ["model-mapping"],
        library_status: "confirmed",
        reference_audio_groups: activeModelCatalogItem.reference_audio_groups ?? [],
        profiles: [],
        default_engine: "gpt-sovits",
        default_profile: profileId,
        fallback_profiles: []
      };
      const nextProfile: VoiceProfile = {
        id: profileId,
        name: `${baseCharacter.name} GPT-SoVITS`,
        engine: "gpt-sovits",
        service_id: libraryBinding.service_id,
        fallback_services: [],
        config: {},
        bindings: [libraryBinding]
      };
      const nextCharacter: Character = {
        ...baseCharacter,
        aliases: Array.from(new Set([...(baseCharacter.aliases ?? []), activeProjectCharacter.name])),
        match_names: Array.from(new Set([...(baseCharacter.match_names ?? []), activeModelCatalogItem.logs_name ?? activeModelCatalogItem.name])),
        tags: Array.from(new Set([...(baseCharacter.tags ?? []), "model-mapping"])),
        library_status: "confirmed",
        reference_audio_groups: activeModelCatalogItem.reference_audio_groups?.length ? activeModelCatalogItem.reference_audio_groups : baseCharacter.reference_audio_groups,
        default_engine: "gpt-sovits",
        default_profile: profileId,
        profiles: [nextProfile, ...(baseCharacter.profiles ?? []).filter((profile) => profile.id !== profileId)],
        updated_at: new Date().toISOString()
      };
      return [...current.filter((character) => character.id !== libraryId), nextCharacter];
    });
    setProject((current) => {
      const nextProjectCharacters = ensureProjectCharacters(current, characters).map((item) =>
        item.project_character_id === activeProjectCharacter.project_character_id
          ? { ...item, library_character_id: libraryId, mode: "reference" as const, character_snapshot: null }
          : item
      );
      return projectWithProjectCharacters(current, nextProjectCharacters);
    });
    setActiveLibraryCharacterId(libraryId);
    setNotice(t("notice.roleSaved"));
  }

  async function importCandidate(candidate: RoleLibraryCandidate) {
    setNotice(t("notice.importingRole"));
    try {
      const payload = await importRoleLibraryCandidate(candidate);
      setCharacters((current) => [...current.filter((character) => character.id !== payload.character.id), payload.character]);
      setRoleLibraryCandidates((current) => current.filter((item) => item.id !== candidate.id));
      setActiveRoleCandidateId(null);
      setActiveLibraryCharacterId(payload.character.id);
      setProject((current) => projectWithProjectCharacters(current, ensureProjectCharacters(current, characters).map((item) =>
          normalizeRoleToken(item.name) === normalizeRoleToken(payload.character.name)
            ? { ...item, library_character_id: payload.character.id, mode: "reference", character_snapshot: null }
            : item
        ))
      );
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
    setActiveLibraryCharacterId(id);
    setActiveRoleCandidateId(null);
    setNotice(t("notice.roleAdded"));
  }

  function updateLibraryCharacter(characterId: string, patch: Partial<Character>) {
    setCharacters((current) =>
      current.map((character) =>
        character.id === characterId
          ? {
              ...character,
              ...patch,
              updated_at: new Date().toISOString(),
            }
          : character
      )
    );
  }

  function updateLibraryCharacterListField(characterId: string, field: "aliases" | "nicknames" | "match_names" | "tags", value: string) {
    updateLibraryCharacter(characterId, { [field]: splitEditableList(value) } as Partial<Character>);
  }

  function updateLibraryBindingConfig(characterId: string, bindingId: string, configPatch: Record<string, unknown>, bindingPatch: Partial<VoiceBinding> = {}) {
    setCharacters((current) =>
      current.map((character) => {
        if (character.id !== characterId) return character;
        return {
          ...character,
          updated_at: new Date().toISOString(),
          profiles: (character.profiles ?? []).map((profile) => {
            const hasTargetBinding = (profile.bindings ?? []).some((binding) => binding.binding_id === bindingId);
            return {
              ...profile,
              service_id: hasTargetBinding && bindingPatch.service_id !== undefined ? bindingPatch.service_id : profile.service_id,
              bindings: (profile.bindings ?? []).map((binding) =>
                binding.binding_id === bindingId
                  ? {
                      ...binding,
                      ...bindingPatch,
                      config: {
                        ...(binding.config ?? {}),
                        ...configPatch,
                      },
                    }
                  : binding
              ),
            };
          }),
        };
      })
    );
  }

  function addGptBindingForCharacter(characterId: string) {
    const serviceId = selectedModelCatalogServiceId || gptSovitsBindingServiceOptions[0]?.serviceId || null;
    setCharacters((current) =>
      current.map((character) => {
        if (character.id !== characterId) return character;
        const profileId = `${character.id}-gpt-sovits`;
        const bindingId = `${character.id}-gpt-sovits-binding`;
        const existingProfiles = character.profiles ?? [];
        const nextProfile: VoiceProfile = {
          id: profileId,
          name: `${character.name} GPT-SoVITS`,
          engine: "gpt-sovits",
          service_id: serviceId,
          fallback_services: [],
          config: {},
          bindings: [
            {
              binding_id: bindingId,
              provider_type: "gpt-sovits",
              service_id: serviceId,
              fallback_services: [],
              capabilities: ["trained_weights_voice", "reference_audio_voice", "wav_output"],
              config: {
                logs_name: character.name,
              },
            },
          ],
        };
        return {
          ...character,
          default_engine: "gpt-sovits",
          default_profile: profileId,
          library_status: "partial",
          updated_at: new Date().toISOString(),
          profiles: [...existingProfiles, nextProfile],
        };
      })
    );
  }

  async function removeLibraryCharacter(characterId: string) {
    try {
      await deleteCharacterLibraryItem(characterId);
      setCharacters((current) => current.filter((character) => character.id !== characterId));
      if (activeLibraryCharacterId === characterId) setActiveLibraryCharacterId(null);
      setNotice(t("notice.roleDeleted"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.roleDeleteFailed"));
    }
  }

  async function uploadAvatar(characterId: string, file: File | undefined) {
    if (!file) return;
    setNotice(t("notice.avatarUploading"));
    try {
      const payload = await uploadCharacterAvatar(characterId, file);
      setCharacters((current) => current.map((character) => (character.id === characterId ? payload.character : character)));
      setNotice(t("notice.avatarUploaded"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.avatarUploadFailed"));
    }
  }

  async function uploadCharacterReference(characterId: string, bindingId: string | undefined, file: File | undefined) {
    if (!file) return;
    setNotice(t("notice.uploadingReference"));
    try {
      const payload = await uploadCharacterReferenceAudio(characterId, file);
      setCharacters((current) => current.map((character) => {
        if (character.id !== characterId) return character;
        if (!bindingId) return payload.character;
        return {
          ...payload.character,
          profiles: (payload.character.profiles ?? []).map((profile) => ({
            ...profile,
            bindings: (profile.bindings ?? []).map((binding) => (
              binding.binding_id === bindingId
                ? { ...binding, config: { ...(binding.config ?? {}), ref_audio_path: payload.sample.path } }
                : binding
            ))
          }))
        };
      }));
      setNotice(t("notice.referenceUploaded"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.referenceUploadFailed"));
      throw error;
    }
  }

  function applyReferenceCandidate(path: string | undefined) {
    const provider = activeProvider;
    if (provider === "indextts") {
      updateActiveBindingConfig({ voice: path || undefined });
    } else if (provider === "gpt-sovits") {
      updateActiveBindingConfig({ ref_audio_path: path || undefined });
    } else if (provider === "cosyvoice") {
      updateActiveBindingConfig({ prompt_audio_path: path || undefined });
    } else {
      updateActiveBindingConfig({ ref_audio_path: path || undefined });
    }
    setNotice(t("notice.referenceApplied"));
  }

  function applyLogsReferenceSample(sample: LogsReferenceAudioSample) {
    updateActiveBindingConfig(applyLogsReferenceSampleToConfig(activeBindingConfig, sample, { serviceId: activeServiceId }));
    setNotice(t("notice.logsReferenceApplied"));
  }

  async function uploadLineReference(file: File | undefined, target: "voice" | "emotion_audio" | "ref_audio_path" | "prompt_audio_path") {
    if (!file || !activeLine) return;
    if (!currentProjectId) {
      setNotice(t("empty.noProjectAction"));
      return;
    }
    setNotice(t("notice.uploadingReference"));
    try {
      const payload = await uploadProjectReferenceAudio(currentProjectId, file);
      updateActiveBindingConfig({ [target]: payload.sample.path });
      setNotice(t("notice.referenceUploaded"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.referenceUploadFailed"));
      throw error;
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

function projectWithProjectCharacters(project: ScriptProject, projectCharacters: ProjectCharacter[]): ScriptProject {
  return {
    ...project,
    project_characters: projectCharacters,
    parse_revisions: project.parse_revisions?.map((revision) =>
      revision.revision_id === project.active_parse_revision_id
        ? { ...revision, project_characters: projectCharacters }
        : revision
    )
  };
}

function stableLibraryCharacterId(name: string, fallback: string): string {
  const ascii = name
    .trim()
    .toLocaleLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return ascii || fallback || `role-${Date.now().toString(36)}`;
}

function referenceSampleSourceLabel(sample: LogsReferenceAudioSample | ReferenceAudioSample): string {
  return "text_source" in sample ? sample.text_source ?? "sample" : sample.text_source ?? "sample";
}

function referenceSampleDisplayLabel(sample: LogsReferenceAudioSample | ReferenceAudioSample): string {
  return "display_label" in sample ? sample.display_label : shortPath(sample.path);
}

function providerFromEngine(engine: ScriptLine["engine_override"]): ProviderType | null {
  if (engine === "gpt-sovits" || engine === "indextts" || engine === "cosyvoice" || engine === "vibevoice") return engine;
  return null;
}

function engineFromProvider(provider: ProviderType): ScriptLine["engine_override"] {
  if (provider === "gpt-sovits" || provider === "indextts" || provider === "cosyvoice" || provider === "vibevoice") return provider;
  return "commercial";
}

function defaultServiceForProvider(services: WorkerHealth[], provider: ProviderType): string | null {
  return routableProviderServices(services, provider)[0]?.service_id ?? null;
}

function sourceProfileLabel(sourceProfile: string, t: Translate): string {
  return t(`services.openSourceMode_${sourceProfile}`);
}

function setupStateLabel(setupState: string | null | undefined, t: Translate): string {
  if (setupState === "ready") return t("services.setup_ready");
  if (setupState === "partial") return t("services.setup_partial");
  if (setupState === "repo_found") return t("services.setup_repo_found");
  if (setupState === "repo_missing") return t("services.setup_repo_missing");
  if (setupState === "env_missing") return t("services.setup_env_missing");
  if (setupState === "endpoint_unreachable") return t("services.setup_endpoint_unreachable");
  return t("services.setup_not_configured");
}

function setupStateTone(setupState: string | null | undefined): "ready" | "partial" | "blocked" | "neutral" {
  if (setupState === "ready") return "ready";
  if (setupState === "partial" || setupState === "repo_found") return "partial";
  if (setupState === "repo_missing" || setupState === "env_missing" || setupState === "endpoint_unreachable") return "blocked";
  return "neutral";
}

function booleanLabel(value: boolean | null | undefined, t: Translate): string {
  return value ? t("status.yes") : t("status.no");
}

function defaultCapabilitiesForProvider(provider: ProviderType): string[] {
  if (provider === "gpt-sovits") return ["trained_weights_voice", "reference_audio_voice"];
  if (provider === "indextts") return ["reference_audio_voice", "emotion_text"];
  if (provider === "cosyvoice") return ["tts", "reference_audio_voice", "zero_shot_voice", "cross_lingual_voice", "style_instruction", "wav_output"];
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
  if (provider === "cosyvoice") {
    return {
      mode: "zero_shot",
      prompt_text: line.note || undefined,
      speed: 1,
      seed: -1
    };
  }
  return {};
}

function bindingsForLine(line: ScriptLine, characters: Character[]): VoiceBinding[] {
  const character = characters.find((item) => item.id === line.character_id);
  const profileId = line.profile_override ?? character?.default_profile;
  return character?.profiles?.find((profile) => profile.id === profileId)?.bindings ?? [];
}

function profilesForLine(line: ScriptLine, characters: Character[]): VoiceProfile[] {
  return characters.find((item) => item.id === line.character_id)?.profiles ?? [];
}

function stringConfig(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function cosyVoiceModeFromConfig(value: unknown): CosyVoiceMode {
  const mode = stringConfig(value);
  return COSY_VOICE_MODE_OPTIONS.some((item) => item.id === mode) ? (mode as CosyVoiceMode) : "zero_shot";
}

function indexEmotionModeFromConfig(value: unknown): IndexEmotionMode {
  const mode = stringConfig(value);
  return INDEX_EMOTION_MODE_OPTIONS.some((item) => item.id === mode) ? (mode as IndexEmotionMode) : "same_as_voice";
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
  if (provider === "cosyvoice") return stringConfig(config.prompt_audio_path) || stringConfig(config.reference_audio);
  return stringConfig(config.ref_audio_path);
}

function logsReferenceRequest(provider: ProviderType, serviceId: string | null | undefined, config: Record<string, unknown>) {
  if (provider !== "gpt-sovits") return null;
  const logsName = stringConfig(config.logs_name);
  if (!logsName) return null;
  const gptWeightsPath = stringConfig(config.gpt_weights_path);
  const sovitsWeightsPath = stringConfig(config.sovits_weights_path);
  const key = [serviceId ?? "", logsName, gptWeightsPath, sovitsWeightsPath].join("|");
  return { key, serviceId, logsName, gptWeightsPath, sovitsWeightsPath };
}

function clearServiceScopedBindingConfig(provider: ProviderType, config: Record<string, unknown>): Record<string, unknown> {
  if (provider !== "gpt-sovits") return config;
  const next = { ...config };
  for (const key of [
    "ref_audio_path",
    "reference_audio",
    "prompt_text",
    "prompt_lang",
    "logs_reference_sample_id",
    "logs_reference_label",
    "logs_reference_service_id",
    "logs_reference_logs_name",
  ]) {
    delete next[key];
  }
  return next;
}

function compactConfig(config: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(config).filter(([, value]) => value !== undefined && value !== ""));
}

function ttsTopbarTone(summary: ReturnType<typeof serviceTopbarSummary>): "ready" | "attention" | "offline" {
  if (summary.local.tone === "offline") return "offline";
  if (summary.local.tone === "attention") return "attention";
  if (summary.paid.total > 0 && summary.paid.tone !== "ready") return "attention";
  return "ready";
}

function ttsTopbarTitle(summary: ReturnType<typeof serviceTopbarSummary>, t: Translate): string {
  return [
    `${t("services.localReady")}: ${summary.local.ready}/${summary.local.total}`,
    `${t("services.paidReady")}: ${summary.paid.ready}/${summary.paid.total}`,
  ].join(" · ");
}

function llmTopbarTitle(summary: ReturnType<typeof serviceTopbarSummary>, t: Translate): string {
  return [
    `${t("services.parserReady")}: ${summary.parser.ready}/${summary.parser.total}`,
    `${t("parser.keyReady")}: ${summary.parser.ready}/${summary.parser.total}`,
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
    "local-gpt-sovits": "http://127.0.0.1:9872",
    "local-indextts": "http://127.0.0.1:7860",
    "local-cosyvoice": "http://127.0.0.1:50000",
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
  if (provider === "cosyvoice") return defaults["local-cosyvoice"];
  return undefined;
}

function serviceDisplayName(service: WorkerHealth): string {
  if (service.display_name) return service.display_name;
  const provider = service.provider_type ?? service.engine;
  const nameMap: Record<string, string> = {
    "gpt-sovits": "GPT-SoVITS",
    indextts: "IndexTTS",
    cosyvoice: "CosyVoice",
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

function isGptSovitsApiV2Service(service: WorkerHealth | undefined): boolean {
  if (!service) return false;
  return service.api_contract === "gpt-sovits-api-v2"
    || Boolean(service.capabilities?.some((capability) => capability === "gpt-sovits-api-v2" || capability === "model_catalog"));
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
  if (service.source_profile) return sourceProfileLabel(service.source_profile, t);
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

function formatVersionParameters(parameters: Record<string, unknown>): string {
  if (Object.keys(parameters).length === 0) return "{}";
  return JSON.stringify(parameters, null, 2);
}

function compactSignature(signature: string): string {
  const parts = signature.split("|").filter(Boolean);
  if (parts.length <= 2) return signature;
  const service = parts.find((part) => part.startsWith("service_id=")) ?? parts[0];
  const logs = parts.find((part) => part.startsWith("logs_name="));
  const ref = parts.find((part) => part.startsWith("ref_audio_path="));
  return [service, logs, ref].filter(Boolean).join(" · ");
}

function shortPath(value: string): string {
  const parts = value.split(/[\\/]/).filter(Boolean);
  if (parts.length <= 2) return value;
  return `${parts.at(-2)} / ${parts.at(-1)}`;
}

function shortRevisionId(value: string): string {
  if (value.length <= 10) return value;
  return `${value.slice(0, 4)}…${value.slice(-4)}`;
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
  const localCoverage = coreProviderCoverage(services);
  const localReady = localCoverage.filter((item) => item.operational).length;
  const completed = Object.values(manifest.lines)
    .flatMap((history) => history.versions)
    .filter((version) => version.status === "completed" && coreLocalProviders.has(version.provider_type ?? version.engine)).length;
  return [
    { id: "mode", label: statusText(runtime?.service_mode ?? "real", t), state: runtime?.service_mode === "real" ? "done" : "attention" },
    { id: "services", label: `${localReady}/${localCoverage.length}`, state: localReady === localCoverage.length ? "done" : "attention" },
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
  return statusText(summary.label, t);
}

function lineCardBadgeKey(badge: LineCardSecondaryBadge): string {
  return badge.kind === "version_count" ? `${badge.kind}-${badge.count}` : badge.kind;
}

function lineCardBadgeLabel(badge: LineCardSecondaryBadge, t: Translate): string {
  if (badge.kind === "latest_playable") return t("history.latestPlayable");
  if (badge.kind === "latest_failed") return t("history.latestFailed");
  if (badge.kind === "version_count") return t("history.versionCount", { count: badge.count });
  return t("history.noVersions");
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
type TTSServiceState = "ready" | "partial" | "blocked" | "disabled" | "running";

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

function ttsServiceState(service: WorkerHealth, isRunning: boolean, runtimeMode?: string): TTSServiceState {
  const healthStatus = String(service.health?.status ?? "").toLowerCase();
  if (isRunning) return "running";
  if (service.enabled === false) return "disabled";
  if (isMockEndpoint(service, runtimeMode)) return "blocked";
  if (!service.base_url) return "blocked";
  if (isStoppedManagedService(service)) return service.can_start === false ? "blocked" : "partial";
  if (healthStatus === "bridge required") return "partial";
  if (healthStatus === "unsupported gradio app") return "blocked";
  if (service.key_configured === false) return "partial";
  if (service.ready) return "ready";
  return service.health?.status ? "partial" : "blocked";
}

function ttsServiceStateLabel(service: WorkerHealth, state: TTSServiceState, t: Translate, runtimeMode?: string): string {
  if (state === "running") return t("status.running");
  if (state === "disabled") return t("status.disabled");
  if (state === "ready") return t("status.ready");
  if (isMockEndpoint(service, runtimeMode)) return t("services.realEndpointRequired");
  if (!service.base_url) return t("services.endpointMissing");
  if (isStoppedManagedService(service)) return t("services.notStarted");
  if (service.key_configured === false) return t("status.needsKey");
  if (state === "partial") return serviceOperationalLabel(service, "warn", t, runtimeMode);
  return t("services.blocked");
}

function ttsStateToneClass(state: TTSServiceState): "ok" | "warn" | "danger" | "running" | "neutral" {
  if (state === "ready") return "ok";
  if (state === "running") return "running";
  if (state === "partial") return "warn";
  if (state === "disabled") return "neutral";
  return "danger";
}

type ParserProviderState = "ready" | "partial" | "blocked" | "disabled";

function isKwjmParserProvider(provider: Pick<ParserProviderDraft, "name" | "api_key_env">): boolean {
  return provider.name.trim() === KWJM_PROVIDER_NAME || provider.api_key_env.trim() === KWJM_API_KEY_ENV;
}

function parserProviderState(provider: ParserProviderDraft): ParserProviderState {
  if (!provider.enabled) return "disabled";
  if (!provider.base_url || !provider.model || !provider.api_key_env) return "blocked";
  if (parserProviderHasUsableKey(provider)) return "ready";
  return "partial";
}

function parserProviderHasUsableKey(provider: ParserProviderDraft): boolean {
  return Boolean(provider.key_configured || provider.api_key?.trim());
}

function parserProviderStateLabel(provider: ParserProviderDraft, t: Translate): string {
  const state = parserProviderState(provider);
  if (state === "ready") return t("status.ready");
  if (state === "partial") return t("status.needsKey");
  if (state === "disabled") return t("status.disabled");
  return t("services.blocked");
}

function kwjmActivationStateLabel(state: ParserProviderState, t: Translate): string {
  if (state === "ready") return t("status.ready");
  if (state === "disabled") return t("parser.readyToActivate");
  if (state === "partial") return t("status.needsKey");
  return t("services.blocked");
}

function queueStatusTone(status: string): "idle" | "queued" | "running" | "completed" | "failed" {
  if (status === "completed") return "completed";
  if (status === "failed" || status === "cancelled") return "failed";
  if (status === "queued") return "queued";
  if (status === "loading" || status === "finalizing" || status === "running") return "running";
  return "idle";
}

function LineHistoryPanel({
  versions,
  services,
  selectedVersionId,
  onSelect,
  onDelete,
  t
}: {
  versions: GenerationVersion[];
  services: WorkerHealth[];
  selectedVersionId?: string;
  onSelect: (version: GenerationVersion) => void;
  onDelete: (version: GenerationVersion) => void;
  t: Translate;
}) {
  const groups = groupGenerationVersions(versions);
  const serviceById = new Map(services.map((service) => [service.service_id ?? "", service]));
  if (groups.length === 0) {
    return <div className="line-history-panel empty">{t("inspector.noVersions")}</div>;
  }
  return (
    <div className="line-history-panel" onClick={(event) => event.stopPropagation()}>
      {groups.map((group) => (
        <section className="history-batch" key={group.groupId}>
          <div className="history-batch-head">
            <strong>{t("history.batch")} {group.label}</strong>
            <StatusPill tone={queueStatusTone(group.latestStatus)} label={statusText(group.latestStatus, t)} />
          </div>
          {group.versions.map((version) => {
            const player = historyPlayerSummary(version);
            const tags = generationVersionTags(version, version.service_id ? serviceDisplayName(serviceById.get(version.service_id) ?? ({ engine: version.engine, display_name: version.service_id, ready: false } as WorkerHealth)) : undefined);
            return (
              <div className={`history-version ${selectedVersionId === version.version_id ? "active" : ""}`} key={version.version_id} onClick={() => onSelect(version)}>
                <div className="history-version-head">
                  <button className="history-version-select" type="button" onClick={(event) => { event.stopPropagation(); onSelect(version); }}>
                    {player.playable ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
                    <strong>{player.versionId}</strong>
                    <small>{new Date(version.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</small>
                    <span>{statusText(player.status, t)}</span>
                  </button>
                  <div className="history-version-tags">
                    <span>{tags.service}</span>
                    <span>{tags.config}</span>
                    <span>{t(`history.verification.${tags.verification}`)}</span>
                  </div>
                  <button className="icon-button tiny danger" type="button" onClick={(event) => { event.stopPropagation(); onDelete(version); }} title={t("history.deleteVersion")}>
                    <Trash2 size={13} />
                  </button>
                </div>
                {player.playable && player.audioPath ? (
                  <WaveformPlayer audioPath={player.audioPath} label={`${t("history.waveformLabel")} ${player.versionId}`} />
                ) : version.error ? (
                  <FailureHistoryMessage version={version} t={t} />
                ) : (
                  <p className="history-version-empty">{statusText(player.status, t)}</p>
                )}
              </div>
            );
          })}
        </section>
      ))}
    </div>
  );
}

function FailureHistoryMessage({ version, t }: { version: GenerationVersion; t: Translate }) {
  const failure = generationFailureView(version);
  return (
    <p className="history-version-error">
      <strong>{t(failure.labelKey)}</strong>
      {failure.detail && <span>{failure.detail}</span>}
    </p>
  );
}

function ReferencePreview({ groups, t }: { groups: CharacterReferenceAudioGroup[]; t: Translate }) {
  const samples = groups.flatMap((group) => (group.samples ?? []).map((sample) => ({ ...sample, group: group.name }))).slice(0, 6);
  return (
    <div className="role-detail-card reference-preview-card">
      <span>{t("characters.referenceAudio")}</span>
      {samples.length > 0 ? (
        <div className="reference-preview-list">
          {samples.map((sample) => (
            <div className="reference-preview-row" key={`${sample.path}-${sample.group}`}>
              <div>
                <strong>{shortPath(sample.path)}</strong>
                <small>{sample.text || sample.group}</small>
              </div>
              {isLocalAudioAsset(sample.path) && <WaveformPlayer audioPath={sample.path} label={shortPath(sample.path)} />}
            </div>
          ))}
        </div>
      ) : (
        <small>{t("characters.noReferenceAudio")}</small>
      )}
    </div>
  );
}

function isLocalAudioAsset(path: string): boolean {
  const normalized = path.replaceAll("\\", "/").toLowerCase();
  return /\.(aac|flac|m4a|mp3|ogg|opus|wav|webm)$/i.test(normalized);
}

function providerLabel(provider: string | null | undefined): string {
  const labels: Record<string, string> = {
    "gpt-sovits": "GPT-SoVITS",
    indextts: "IndexTTS",
    cosyvoice: "CosyVoice",
    openai: "OpenAI",
    gemini: "Gemini",
    xai: "xAI",
    volcengine: "Volcengine",
    "generic-http": "Generic HTTP"
  };
  return labels[provider ?? ""] ?? provider ?? "-";
}

function characterStatusTone(character: Character): "ready" | "warn" | "danger" | "neutral" {
  if (character.library_status === "confirmed") return "ready";
  if (character.library_status === "partial") return "warn";
  if (character.library_status === "archived") return "neutral";
  return "danger";
}

function referenceSampleCount(groups: CharacterReferenceAudioGroup[] | undefined): number {
  return (groups ?? []).reduce((sum, group) => sum + (group.samples?.length ?? 0), 0);
}

function characterBindingSummary(character: Character): { bindingCount: number; completeCount: number; providerLabel: string } {
  const bindings = (character.profiles ?? []).flatMap((profile) => profile.bindings ?? []);
  const completeCount = bindings.filter((binding) => bindingCompleteness(binding).complete).length;
  const provider = bindings[0]?.provider_type ?? character.default_engine ?? character.profiles?.[0]?.engine ?? null;
  return {
    bindingCount: bindings.length,
    completeCount,
    providerLabel: providerLabel(provider)
  };
}

function stringConfigValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function referenceSampleDisplayText(sample: unknown): string {
  if (!sample || typeof sample !== "object") return "";
  const record = sample as Record<string, unknown>;
  return stringConfigValue(record.display_label) || stringConfigValue(record.text) || stringConfigValue(record.path);
}

function splitEditableList(value: string): string[] {
  return value
    .split(/[\n,，、]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function StatusPill({ tone, label }: { tone: "idle" | "queued" | "running" | "completed" | "failed"; label: string }) {
  return <span className={`status-pill ${tone}`}>{label}</span>;
}
