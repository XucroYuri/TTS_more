import {
  AlertCircle,
  ArrowLeft,
  Bot,
  CheckCircle2,
  ChevronDown,
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
  ApiRequestError,
  clearVoiceSelection,
  fetchCharacters,
  fetchAnalysisReviewSession,
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
  confirmVoiceCandidateIdentity,
  detectOpenSourceTTS,
  fetchServiceLogs,
  fetchServices,
  fetchServicesStatus,
  fetchQueueStatus,
  generationPreflight,
  fetchVoiceCandidates,
  fetchVoiceCatalog,
  fetchLogsCandidates,
  freezeProjectCharacter,
  createGenerationJob,
  createScriptRevision,
  deleteProject,
  deleteGenerationVersion,
  importRoleLibraryCandidate,
  reloadServiceSettings,
  recommendVoices,
  referenceAudioUrl,
  rematchProjectCharacters,
  runRealValidation,
  saveCharacters,
  saveParserProviders,
  scanCharacterLibrary,
  saveProject,
  selectVoiceCandidate,
  startAndWaitService,
  startService,
  stopService,
  testParserProvider,
  testService,
  syncVoiceCatalog,
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
import { VoiceAssetStatusPanel } from "./features/voice-matching/VoiceAssetStatusPanel";
import { VoiceCandidatePanel } from "./features/voice-matching/VoiceCandidatePanel";
import { voiceCatalogReady } from "./features/voice-matching/voiceReadiness";
import { QueueDropdown } from "./features/queue/QueueDropdown";
import { QueuePanel } from "./features/queue/QueuePanel";
import { RoleLibraryPanel } from "./features/roles/RoleLibraryPanel";
import { useRoleLibraryController } from "./features/roles/useRoleLibraryController";
import { workspaceSnapshotKey } from "./features/workbench/useWorkspacePersistence";
import { ServiceCenter } from "./features/services/ServiceCenter";
import { LineWorkspace } from "./features/line-workspace/LineWorkspace";
import { WorkbenchShell } from "./features/workbench/WorkbenchShell";
import { VoiceInspector } from "./features/voice-matching/VoiceInspector";
import { VoiceConfigurationDrawer } from "./features/voice-matching/VoiceConfigurationDrawer";
import {
  AnalysisStageGate,
  activeScriptSourceText,
  beginAnalysisSourceRevision,
  buildConfirmedAnalysisHandoff,
  readAnalysisScriptFile,
  reviewConfirmedAnalysis,
  shouldAutosaveWorkspace,
  type AnalysisSourceFileMetadata,
  type WorkspaceStage
} from "./features/script-analysis/analysisFlow";
import { scriptTitleFromFilename, type ScriptFileTarget } from "./features/script-analysis/fileInput";
import {
  activeAnalysisScopeForRevision,
  activeAnalysisScopeMatchesRevision,
  archiveRestorableAnalysisSession,
  clearActiveAnalysisScope,
  hasRestorableAnalysisSession,
  readActiveAnalysisScope,
  writeActiveAnalysisScope
} from "./features/script-analysis/useAnalysisDraft";
import {
  analysisScopeStorageId,
  defaultAnalysisStorage,
  writeAnalysisRunSession
} from "./features/script-analysis/analysisSessionStorage";
import { generationFailureView, generationVersionTags, groupGenerationVersions, newestPlayableVersion, versionToInspectorDraft, type InspectorVersionDraft } from "./lib/generationHistory";
import { generationStatusCounts, generationStatusKey, generationStatusTone, isTerminalGenerationStatus, type GenerationStatusTone } from "./lib/generationStatus";
import { generationLineKey, latestQueueItemForLine, lineHasActiveGeneration, upsertGenerationJob } from "./lib/generationQueue";
import {
  CATALOG_STAGED_REFERENCE_OPTION,
  applyLogsReferenceSampleToConfig,
  referenceAudioSamplesForCharacter,
  selectedDynamicWeightOption,
  selectedLogsReferenceOptionValue,
  selectedLogsReferenceSample,
} from "./lib/gptSovitsReference";
import { formatScriptNote } from "./lib/lineNote";
import { firstReferenceSampleFromModel, gptSovitsProjectBindingFromModel } from "./lib/modelCatalog";
import { ensureProjectCharacters, freezeProjectCharacterLocally, projectCharacterRows, resolveProjectCharacters } from "./lib/projectCharacters";
import { bindingCompleteness, catalogServiceOptions, roleLibraryBindingRows, roleLibraryDetailSelection, roleLibraryReferencePreview, roleLibraryServiceOptions, selectedCatalogServiceId } from "./lib/roleLibraryView";
import { buildGenerationTask, lineBinding, lineEngine, lineProfile, lineServiceId } from "./lib/routing";
import { createDefaultParserProviderDraft, KWJM_API_KEY_ENV, KWJM_BASE_URL, KWJM_BASE_URL_PLACEHOLDER, KWJM_MODEL, KWJM_PROVIDER_NAME, normalizeParserProviderDrafts, parserProviderKeyState, toParserProviderSavePayload, upsertKwjmParserProvider } from "./lib/parserConfig";
import { createEmptyManifest, createEmptyProject, createProjectId, readStoredProjectId, selectStartupProjectId, writeStoredProjectId } from "./lib/projectStartup";
import { filterAndSortProjectSummaries, nextProjectAfterDelete, projectPreviewStats } from "./lib/scriptManagement";
import { projectToScriptSourceText } from "./lib/scriptSource";
import { summarizeLineHistory } from "./lib/status";
import { coreLocalProviders, coreProviderCoverage, filterScriptLines, isServiceOperational, lineHistoryForLine, routableProviderServices, serviceTopbarHealthItems, serviceTopbarSummary, standardProjectName, validationRunState, type LineStatusFilter } from "./lib/workstation";
import { buildComfyUIEndpointRequest, ttsAudioSuiteContractForProvider } from "./lib/ttsAccess";
import { createToast, inferToastLevel, shouldToastNotice, toastDuration, type Toast, type ToastLevel, type ToastOptions } from "./lib/toast";
import { generationMethodForProvider, generationMethodOptions, generationMethodRouteLabels, historyPlayerSummary, inspectorBackupReferenceVisible, inspectorDiagnosticsState, inspectorPanelMode, inspectorSections, lineCardSecondaryBadges, lineFilterToolbarState, lineFocusTransition, lineWorkbenchControlsState, preflightFallbackAction, preflightLineLabelKey, preflightLineTone, preflightLoadLabelKey, preflightLoadTone, roleAccentClass, shouldRequestRevisionConfirmation, trustedBackupReferenceGroups, type GenerationMethodId, type LineCardSecondaryBadge } from "./lib/workbenchView";
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
  ScriptRevision,
  VoiceBinding,
  VoiceCatalogPublicView,
  VoiceCandidates,
  VoiceRecommendation,
  VoiceProfile,
  WorkerHealth,
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
type ProjectSaveOutcome = "saved" | "stale" | "failed";
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

interface PendingProjectAutosave {
  projectId: string;
  project: ScriptProject;
  characters: Character[];
  authorityEpoch: number;
  timerId: number | null;
}
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

function mergeVoiceSelectionAuthority(
  current: ScriptProject,
  authoritative: ScriptProject,
  lineId: string
): ScriptProject {
  const authoritativeLine = authoritative.lines.find((line) => line.id === lineId);
  if (!authoritativeLine) return current;
  const mergeLine = (line: ScriptLine): ScriptLine => line.id === lineId
    ? {
        ...line,
        voice_selection: authoritativeLine.voice_selection ?? null,
        temporary_binding: authoritativeLine.temporary_binding ?? null
      }
    : line;
  return {
    ...current,
    lines: current.lines.map(mergeLine),
    parse_revisions: current.parse_revisions?.map((revision) => revision.revision_id === current.active_parse_revision_id
      ? { ...revision, lines: revision.lines.map(mergeLine) }
      : revision)
  };
}

export default function App() {
  const { t, i18n } = useTranslation();
  const [currentProjectId, setCurrentProjectId] = useState<string | null>(() => readStoredProjectId());
  const [projectSummaries, setProjectSummaries] = useState<ProjectSummary[]>([]);
  const [characters, setCharacters] = useState<Character[]>([]);
  const [project, setProject] = useState<ScriptProject>(() => createEmptyProject());
  const [workspaceStage, setWorkspaceStage] = useState<WorkspaceStage>("tts");
  const [analysisSourceRevision, setAnalysisSourceRevision] = useState<ScriptRevision | null>(null);
  const [isReturningToAnalysis, setIsReturningToAnalysis] = useState(false);
  const [manifest, setManifest] = useState<GenerationManifest>(() => createEmptyManifest(null));
  const [services, setServices] = useState<WorkerHealth[]>([]);
  const [runtime, setRuntime] = useState<RuntimeMode | null>(null);
  const [voiceCandidates, setVoiceCandidates] = useState<VoiceCandidates | null>(null);
  const [voiceCatalog, setVoiceCatalog] = useState<VoiceCatalogPublicView | null>(null);
  const [voiceCatalogError, setVoiceCatalogError] = useState<string | null>(null);
  const [isSyncingVoiceCatalog, setIsSyncingVoiceCatalog] = useState(false);
  const [voiceRecommendations, setVoiceRecommendations] = useState<Record<string, VoiceRecommendation>>({});
  const [voiceRecommendationLoadingLineId, setVoiceRecommendationLoadingLineId] = useState<string | null>(null);
  const [voiceRecommendationError, setVoiceRecommendationError] = useState<string | null>(null);
  const [voiceRecommendationEpoch, setVoiceRecommendationEpoch] = useState(0);
  const [selectingVoiceCandidateId, setSelectingVoiceCandidateId] = useState<string | null>(null);
  const [activeLineId, setActiveLineId] = useState("");
  const [expandedLineId, setExpandedLineId] = useState<string | null>(null);
  const [selectedHistoryVersions, setSelectedHistoryVersions] = useState<Record<string, string>>({});
  const [versionDrafts, setVersionDrafts] = useState<Record<string, InspectorVersionDraft & { version_id: string }>>({});
  const [diagnosticsExpanded, setDiagnosticsExpanded] = useState(false);
  const [routeSettingsOpen, setRouteSettingsOpen] = useState(false);
  const [isVoiceConfigurationOpen, setIsVoiceConfigurationOpen] = useState(false);
  const [submittingGenerationKeys, setSubmittingGenerationKeys] = useState<string[]>([]);
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
  const [openSourceResourceGroup, setOpenSourceResourceGroup] = useState("comfyui-local-0");
  const [openSourceCapacity, setOpenSourceCapacity] = useState(3);
  const [openSourceResourceId, setOpenSourceResourceId] = useState("");
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
  const submittingGenerationKeysRef = useRef<Set<string>>(new Set());
  const managedProjectIdRef = useRef<string | null>(managedProjectId);
  const currentProjectIdRef = useRef<string | null>(currentProjectId);
  const analysisProjectIdRef = useRef<string | null>(null);
  const analysisConfirmedReviewRef = useRef(false);
  const analysisManagedProjectIdRef = useRef<string | null>(managedProjectId);
  const analysisCurrentProjectIdRef = useRef<string | null>(currentProjectId);
  const analysisSourceFileMetadataRef = useRef<AnalysisSourceFileMetadata | null>(null);
  const scriptFileOperationTokenRef = useRef(0);
  const analysisStartOperationTokenRef = useRef(0);
  const analysisManagerSavingTokenRef = useRef<number | null>(null);
  const analysisRestoreOperationTokenRef = useRef(0);
  const saveChainRef = useRef<Promise<void>>(Promise.resolve());
  const pendingProjectAutosaveRef = useRef<PendingProjectAutosave | null>(null);
  const workspaceBaselineByProjectRef = useRef<Map<string, string>>(new Map());
  const analysisAutosaveBlockedProjectIdRef = useRef<string | null>(null);
  const authorityUnknownProjectIdsRef = useRef<Set<string>>(new Set());
  const preserveManagerSourceDraftProjectIdRef = useRef<string | null>(null);
  const currentProjectTransitionOperationTokenRef = useRef(0);
  const currentProjectTransitionChainRef = useRef<Promise<void>>(Promise.resolve());
  const projectAuthorityEpochRef = useRef<Map<string, number>>(new Map());
  const projectRef = useRef(project);
  const charactersRef = useRef(characters);
  const isProjectLoadedRef = useRef(isProjectLoaded);
  projectRef.current = project;
  charactersRef.current = characters;
  isProjectLoadedRef.current = isProjectLoaded;
  const seededAuthoritativeProjectIdRef = useRef<string | null>(null);
  const voiceCatalogRequestTokenRef = useRef(0);
  const voiceRecommendationRequestTokenRef = useRef(0);
  const voiceRecommendationCacheRef = useRef<Set<string>>(new Set());
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
    if (!shouldToastNotice(message)) return;
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

  function markWorkspaceAuthoritative(
    projectId: string,
    projectSnapshot: ScriptProject,
    characterSnapshot: Character[]
  ): void {
    workspaceBaselineByProjectRef.current.set(
      projectId,
      workspaceSnapshotKey({
        projectId,
        project: projectSnapshot,
        characters: characterSnapshot
      })
    );
  }

  function workspaceMatchesAuthoritativeBaseline(
    projectId: string,
    projectSnapshot: ScriptProject,
    characterSnapshot: Character[]
  ): boolean {
    return workspaceBaselineByProjectRef.current.get(projectId) === workspaceSnapshotKey({
      projectId,
      project: projectSnapshot,
      characters: characterSnapshot
    });
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
    void refreshVoiceCatalog();
    void refreshOpenSourceCatalog();
    void refreshProjects();
    void refreshParserProviders();
    fetchCharacters()
      .then((payload) => {
        setCharacters(payload);
        const projectId = analysisCurrentProjectIdRef.current;
        if (projectId && isProjectLoadedRef.current) {
          markWorkspaceAuthoritative(projectId, projectRef.current, payload);
        }
      })
      .catch(() => setCharacters([]));
  }, [t]);

  useEffect(() => {
    const scope = readActiveAnalysisScope();
    if (!scope) return;
    const restoreToken = analysisRestoreOperationTokenRef.current + 1;
    analysisRestoreOperationTokenRef.current = restoreToken;
    let cancelled = false;
    const restoreCurrentProjectFallback = async () => {
      const fallbackProjectId = analysisCurrentProjectIdRef.current;
      if (!fallbackProjectId || readActiveAnalysisScope()) return;
      try {
        const fallbackProject = await fetchProject(fallbackProjectId);
        if (
          cancelled
          || analysisRestoreOperationTokenRef.current !== restoreToken
          || analysisCurrentProjectIdRef.current !== fallbackProjectId
          || readActiveAnalysisScope()
        ) return;
        const fallbackRevision = fallbackProject.script_revisions?.find(
          (revision) => revision.revision_id === fallbackProject.active_script_revision_id
        );
        if (fallbackRevision && hasRestorableAnalysisSession(fallbackProjectId, fallbackRevision)) {
          analysisProjectIdRef.current = fallbackProjectId;
          setAnalysisSourceRevision(fallbackRevision);
          setWorkspaceStage("analysis");
        }
      } catch {
        // The current-project loader owns its user-visible failure state.
      }
    };
    fetchProject(scope.projectId)
      .then((payload) => {
        if (cancelled || analysisRestoreOperationTokenRef.current !== restoreToken) return;
        const currentScope = readActiveAnalysisScope();
        if (
          !currentScope
          || currentScope.projectId !== scope.projectId
          || currentScope.revisionId !== scope.revisionId
          || currentScope.sourceSha256 !== scope.sourceSha256
        ) return;
        const sourceRevision = payload.script_revisions?.find((revision) =>
          activeAnalysisScopeMatchesRevision(scope, scope.projectId, revision)
        );
        if (!sourceRevision || !hasRestorableAnalysisSession(scope.projectId, sourceRevision)) {
          clearActiveAnalysisScope(scope);
          void restoreCurrentProjectFallback();
          return;
        }
        analysisProjectIdRef.current = scope.projectId;
        setAnalysisSourceRevision(sourceRevision);
        setWorkspaceStage("analysis");
      })
      .catch((error) => {
        if (
          cancelled
          || analysisRestoreOperationTokenRef.current !== restoreToken
          || !(error instanceof ApiRequestError)
          || error.status !== 404
        ) return;
        clearActiveAnalysisScope(scope);
        void restoreCurrentProjectFallback();
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!currentProjectId) {
      setProject(createEmptyProject());
      setManifest(createEmptyManifest(null));
      setActiveLineId("");
      setExpandedLineId(null);
      setSelectedHistoryVersions({});
      setVersionDrafts({});
      setLineTextDrafts({});
      setIsProjectLoaded(true);
      setSaveState("idle");
      return;
    }
    let cancelled = false;
    if (seededAuthoritativeProjectIdRef.current) {
      if (seededAuthoritativeProjectIdRef.current === currentProjectId) {
        seededAuthoritativeProjectIdRef.current = null;
        setIsProjectLoaded(true);
        fetchManifest(currentProjectId)
          .then((manifestPayload) => {
            if (!cancelled) setManifest(manifestPayload);
          })
          .catch(() => {
            if (!cancelled) setManifest(createEmptyManifest(currentProjectId));
          });
        return () => {
          cancelled = true;
        };
      }
      seededAuthoritativeProjectIdRef.current = null;
    }
    setIsProjectLoaded(false);
    fetchProject(currentProjectId)
      .then((payload) => {
        if (cancelled) return;
        const resumableRevision = payload.script_revisions?.find(
          (revision) => revision.revision_id === payload.active_script_revision_id
        );
        if (
          !readActiveAnalysisScope()
          && resumableRevision
          && hasRestorableAnalysisSession(currentProjectId, resumableRevision)
        ) {
          analysisProjectIdRef.current = currentProjectId;
          setAnalysisSourceRevision(resumableRevision);
          setWorkspaceStage("analysis");
        }
        setProject(payload);
        setActiveLineId(payload.lines[0]?.id ?? "");
        setExpandedLineId(null);
        setSelectedHistoryVersions({});
        setVersionDrafts({});
        setLineTextDrafts({});
        return fetchProjectCharacters(currentProjectId)
          .then((projectCharactersPayload) => {
            if (cancelled) return;
            const hydratedProject = {
              ...payload,
              project_characters: projectCharactersPayload.project_characters
            };
            markWorkspaceAuthoritative(
              currentProjectId,
              hydratedProject,
              charactersRef.current
            );
            setProject(hydratedProject);
            setIsProjectLoaded(true);
          })
          .catch(() => {
            if (cancelled) return;
            markWorkspaceAuthoritative(currentProjectId, payload, charactersRef.current);
            setIsProjectLoaded(true);
          });
      })
      .catch(() => {
        if (cancelled) return;
        setProject(createEmptyProject());
        setActiveLineId("");
        setExpandedLineId(null);
        setSelectedHistoryVersions({});
        setVersionDrafts({});
        setLineTextDrafts({});
        setIsProjectLoaded(true);
        setNotice(t("empty.projectLoadFailed"));
      });
    fetchManifest(currentProjectId)
      .then((manifestPayload) => {
        if (!cancelled) setManifest(manifestPayload);
      })
      .catch(() => {
        if (!cancelled) setManifest(createEmptyManifest(currentProjectId));
      });
    return () => {
      cancelled = true;
    };
  }, [currentProjectId, t]);

  useEffect(() => {
    if (!shouldAutosaveWorkspace(workspaceStage) || !isProjectLoaded || !currentProjectId) return;
    if (workspaceMatchesAuthoritativeBaseline(currentProjectId, project, characters)) return;
    setSaveState("saving");
    const autosaveProjectId = currentProjectId;
    const autosaveProject = project;
    const autosaveCharacters = characters;
    const autosaveAuthorityEpoch = projectAuthorityEpochRef.current.get(autosaveProjectId) ?? 0;
    const pendingAutosave: PendingProjectAutosave = {
      projectId: autosaveProjectId,
      project: autosaveProject,
      characters: autosaveCharacters,
      authorityEpoch: autosaveAuthorityEpoch,
      timerId: null
    };
    pendingProjectAutosaveRef.current = pendingAutosave;
    if (analysisAutosaveBlockedProjectIdRef.current === autosaveProjectId) {
      return () => {
        if (pendingProjectAutosaveRef.current === pendingAutosave) pendingProjectAutosaveRef.current = null;
      };
    }
    const handle = window.setTimeout(() => {
      if (pendingProjectAutosaveRef.current !== pendingAutosave) return;
      pendingAutosave.timerId = null;
      void flushPendingProjectAutosave(autosaveProjectId);
    }, 700);
    pendingAutosave.timerId = handle;
    return () => {
      window.clearTimeout(handle);
      if (pendingProjectAutosaveRef.current === pendingAutosave) pendingProjectAutosaveRef.current = null;
    };
  }, [characters, currentProjectId, isProjectLoaded, project, workspaceStage]);

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
  const currentSourceRevision = useMemo(
    () => project.script_revisions?.find((revision) => revision.revision_id === project.active_script_revision_id) ?? null,
    [project.active_script_revision_id, project.script_revisions]
  );
  const resolvedCharacters = useMemo(() => resolveProjectCharacters(projectWithCharacters, characters), [characters, projectWithCharacters]);
  const projectRoleRows = useMemo(() => projectCharacterRows(projectWithCharacters, characters), [characters, projectWithCharacters]);

  const roleLibraryController = useRoleLibraryController({
    characters,
    projectCharacters,
    search: roleLibrarySearch,
    onSaveCharacters: setCharacters,
    onSaveProjectCharacters: (nextProjectCharacters) => {
      setProject((current) => projectWithProjectCharacters(current, nextProjectCharacters));
    }
  });
  const filteredLibraryCharacters = roleLibraryController.filteredCharacters;
  const filteredRoleCandidates = useMemo(() => {
    const query = roleLibrarySearch.trim().toLocaleLowerCase();
    if (!query) return roleLibraryCandidates;
    return roleLibraryCandidates.filter((candidate) =>
      `${candidate.name} ${candidate.id} ${candidate.logs_name ?? ""} ${(candidate.aliases ?? []).join(" ")}`.toLocaleLowerCase().includes(query)
    );
  }, [roleLibraryCandidates, roleLibrarySearch]);
  const roleLibrarySelection = useMemo(
    () => roleLibraryDetailSelection({
      selectedCharacterId: activeLibraryCharacterId,
      filteredCharacters: filteredLibraryCharacters,
      selectedCandidateId: activeRoleCandidateId,
      selectedModelId: activeModelCatalogId
    }),
    [activeLibraryCharacterId, activeModelCatalogId, activeRoleCandidateId, filteredLibraryCharacters]
  );
  const activeLibraryCharacter = useMemo(
    () => roleLibrarySelection.kind === "library-character"
      ? filteredLibraryCharacters.find((character) => character.id === roleLibrarySelection.characterId) ?? null
      : null,
    [filteredLibraryCharacters, roleLibrarySelection]
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
  const activeLineCharacter = useMemo(
    () => activeLine ? resolvedCharacters.find((character) => character.id === activeLine.character_id) : undefined,
    [activeLine, resolvedCharacters]
  );
  const activeVoiceRecommendation = activeLine ? voiceRecommendations[activeLine.id] ?? null : null;
  const activeRoleRow = useMemo(
    () => activeLine ? projectRoleRows.find((role) => role.id === activeLine.character_id) : undefined,
    [activeLine, projectRoleRows]
  );
  const activeVersions = useMemo(() => (activeLine ? lineHistoryForLine(manifest, activeLine)?.versions ?? [] : []), [activeLine, manifest]);
  const selectedHistoryVersion = useMemo(
    () => activeVersions.find((version) => version.version_id === selectedHistoryVersions[activeLine?.id ?? ""]),
    [activeLine?.id, activeVersions, selectedHistoryVersions]
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

  useEffect(() => {
    setVoiceRecommendationError(null);
  }, [activeLine?.id]);

  useEffect(() => {
    const projectId = currentProjectId;
    const lineId = activeLine?.id;
    const catalogVersion = voiceCatalog?.catalog_version;
    if (!projectId || !lineId || !catalogVersion || !isProjectLoaded || workspaceStage !== "tts") return;
    const cacheKey = `${projectId}|${lineId}|${catalogVersion}`;
    if (voiceRecommendationCacheRef.current.has(cacheKey)) return;
    voiceRecommendationCacheRef.current.add(cacheKey);
    const requestToken = voiceRecommendationRequestTokenRef.current + 1;
    voiceRecommendationRequestTokenRef.current = requestToken;
    let cancelled = false;
    setVoiceRecommendationLoadingLineId(lineId);
    setVoiceRecommendationError(null);
    void (async () => {
      try {
        await flushPendingProjectAutosave(projectId);
        const payload = await recommendVoices(projectId, [lineId]);
        if (cancelled || voiceRecommendationRequestTokenRef.current !== requestToken) return;
        const recommendation = payload.recommendations.find((item) => item.line_id === lineId);
        if (recommendation) {
          setVoiceRecommendations((current) => ({ ...current, [lineId]: recommendation }));
        }
      } catch (error) {
        voiceRecommendationCacheRef.current.delete(cacheKey);
        if (!cancelled && voiceRecommendationRequestTokenRef.current === requestToken) {
          setVoiceRecommendationError(error instanceof Error ? error.message : t("voiceMatching.recommendationFailed"));
        }
      } finally {
        if (!cancelled && voiceRecommendationRequestTokenRef.current === requestToken) {
          setVoiceRecommendationLoadingLineId(null);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeLine?.id, currentProjectId, isProjectLoaded, voiceCatalog?.catalog_version, voiceRecommendationEpoch, workspaceStage]);

  const activeLogsReferenceServiceId = useMemo(() => {
    if (activeServiceId) return activeServiceId;
    const candidates = routableProviderServices(services, activeProvider);
    return candidates.length === 1 ? candidates[0]?.service_id ?? "" : "";
  }, [activeProvider, activeServiceId, services]);
  const activeLogsReferenceRequest = useMemo(
    () => activeLogsReferenceServiceId ? logsReferenceRequest(activeProvider, activeLogsReferenceServiceId, activeBindingConfig) : null,
    [activeBindingConfig, activeLogsReferenceServiceId, activeProvider]
  );
  const activeLogsReferencePayload = activeLogsReferenceRequest ? logsReferenceAudio[activeLogsReferenceRequest.key] : undefined;
  const fetchedLogsReferenceSamples = activeLogsReferencePayload?.samples ?? [];
  const activeLogsReferenceSamples = useMemo(
    () => referenceAudioSamplesForCharacter(
      activeLineCharacter,
      fetchedLogsReferenceSamples,
      activeLogsReferenceRequest?.logsName ?? ""
    ),
    [activeLineCharacter, activeLogsReferenceRequest?.logsName, fetchedLogsReferenceSamples]
  );
  const activeLogsReferenceSample = selectedLogsReferenceSample(activeLogsReferenceSamples, activeBindingConfig, { serviceId: activeLogsReferenceServiceId });
  const activeGptWeightOption = selectedDynamicWeightOption(activeBindingConfig, "gpt");
  const activeSovitsWeightOption = selectedDynamicWeightOption(activeBindingConfig, "sovits");
  const activeLogsReferenceOptionValue = selectedLogsReferenceOptionValue(activeLogsReferenceSample, activeBindingConfig);
  const staleLogsReferenceServiceId = stringConfig(activeBindingConfig.logs_reference_service_id);
  const isLogsReferenceFromOtherService = Boolean(activeProvider === "gpt-sovits" && staleLogsReferenceServiceId && staleLogsReferenceServiceId !== activeLogsReferenceServiceId);
  const activeReferenceAudioPath = activeProvider === "gpt-sovits"
    ? activeLogsReferenceSample?.path ?? (isLogsReferenceFromOtherService ? "" : stringConfig(activeBindingConfig.ref_audio_path))
    : "";
  const activeReferenceAudioLabel = activeLogsReferenceSample?.display_label || shortPath(activeReferenceAudioPath) || t("inspector.referenceAudio");
  const candidateReferenceGroups = useMemo(
    () => trustedBackupReferenceGroups(activeLine, resolvedCharacters),
    [activeLine, resolvedCharacters]
  );
  const showBackupReferenceSource = inspectorBackupReferenceVisible(activeProvider, candidateReferenceGroups.length);
  const validationState = useMemo(
    () => validationRunState(runtime, services, voiceCatalog, manifest, isValidating, submittingGenerationKeys.length > 0),
    [runtime, services, voiceCatalog, manifest, isValidating, submittingGenerationKeys.length]
  );
  const validationSteps = useMemo(() => buildValidationSteps(runtime, services, voiceCatalog, manifest, t), [runtime, services, voiceCatalog, manifest, t]);
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
  const providerOptions = useMemo(() => Array.from(new Set(project.lines.map((line) => lineBinding(line, resolvedCharacters)?.provider_type ?? "unassigned"))), [project.lines, resolvedCharacters]);
  const lineToolbarState = lineFilterToolbarState({
    providerFilter,
    statusFilter,
    labels: {
      filtersMore: t("filters.more"),
      status: (status) => statusText(status, t)
    }
  });
  const lineWorkbenchState = lineWorkbenchControlsState({
    hasProject: Boolean(currentProjectId),
    totalLineCount: project.lines.length,
    filteredLineCount: filteredLines.length
  });
  const lineFilterTitle = lineToolbarState.title;
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
    managedProjectIdRef.current = managedProjectId;
  }, [managedProjectId]);

  useEffect(() => {
    analysisManagedProjectIdRef.current = managedProjectId;
    scriptFileOperationTokenRef.current += 1;
    analysisStartOperationTokenRef.current += 1;
    analysisSourceFileMetadataRef.current = null;
  }, [managedProjectId]);

  useEffect(() => {
    currentProjectIdRef.current = currentProjectId;
  }, [currentProjectId]);

  useEffect(() => {
    analysisCurrentProjectIdRef.current = currentProjectId;
  }, [currentProjectId]);

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
        const preserveManagerSourceDraft = preserveManagerSourceDraftProjectIdRef.current === managedProjectId;
        if (preserveManagerSourceDraft) preserveManagerSourceDraftProjectIdRef.current = null;
        setManagedProject(payload);
        setManagerTitleDraft(payload.title);
        if (!preserveManagerSourceDraft) {
          setManagerSourceDraft(activeScriptSourceText(payload) ?? projectToScriptSourceText(payload, characters));
        }
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
  const serviceSummary = useMemo(() => serviceTopbarSummary(visibleServices, voiceCatalog, parserProviders), [parserProviders, visibleServices, voiceCatalog]);
  const serviceHealthItems = useMemo(() => serviceTopbarHealthItems(serviceSummary), [serviceSummary]);
  const queueJobs = useMemo(() => queueStatus?.jobs ?? [], [queueStatus]);
  const queueLineLabels = useMemo(() => {
    const labels: Record<string, string> = {};
    for (const line of project.lines) {
      const label = `${characterName(resolvedCharacters, line.character_id)} · ${line.text}`;
      labels[line.id] = label;
      if (line.line_uid) labels[line.line_uid] = label;
    }
    return labels;
  }, [project.lines, resolvedCharacters]);
  const selectedConfigService = useMemo(
    () => ttsServices.find((service) => service.service_id === expandedServiceConfigId) ?? ttsServices[0],
    [expandedServiceConfigId, ttsServices]
  );
  const runningServiceIds = useMemo(() => {
    const ids = new Set<string>();
    for (const job of queueJobs) {
      for (const item of job.items) {
        if (item.service_id && generationStatusTone(item.status) === "running") ids.add(item.service_id);
      }
    }
    return ids;
  }, [queueJobs]);
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
  const queueItems = useMemo(() => queueJobs.flatMap((job) => job.items), [queueJobs]);
  const queueCounts = useMemo(() => generationStatusCounts(queueItems.map((item) => item.status)), [queueItems]);
  const queueRunningItems = queueStatus?.running ?? queueCounts.running;
  const queueQueuedItems = queueStatus?.queued ?? queueCounts.queued;
  const queueCompletedItems = queueCounts.completed;
  const queueFailedItems = queueCounts.failed;
  const queueCancelledItems = queueCounts.cancelled;
  const queueProcessedItems = queueCounts.processed;
  const queueTotalItems = Math.max(queueCounts.total, queueProcessedItems + queueRunningItems + queueQueuedItems);
  const queueActiveJob = queueJobs.find((job) => !isTerminalGenerationStatus(job.status)) ?? null;
  const queueHasActiveWork = queueJobs.some((job) => !isTerminalGenerationStatus(job.status));
  const queueProgressRatio = queueActiveJob
    ? queueActiveJob.progress
    : queueTotalItems > 0
      ? queueProcessedItems / queueTotalItems
      : 0;
  const queueProgressPercent = Math.round(Math.max(0, Math.min(1, queueProgressRatio)) * 100);
  const activeLineQueueItem = activeLine ? latestQueueItemForLine(queueJobs, currentProjectId, activeLine) : undefined;
  const activeLineSubmitting = Boolean(
    currentProjectId
    && activeLine
    && submittingGenerationKeys.includes(generationLineKey(currentProjectId, activeLine))
  );
  const activeLineGenerationBusy = activeLineSubmitting || Boolean(activeLineQueueItem && !isTerminalGenerationStatus(activeLineQueueItem.status));
  const queueSyncLabel = isRefreshingTopology ? t("queue.polling") : t(queueStatus ? "queue.synced" : "queue.notSynced");
  const queueVisibleStatusLabel = queueActiveJob ? t(generationStatusKey(queueActiveJob.status)) : queueSyncLabel;
  const queueVisibleTone = queueActiveJob ? generationStatusTone(queueActiveJob.status) : isRefreshingTopology ? "running" : "idle";

  useEffect(() => {
    if (!queueHasActiveWork) return;
    let disposed = false;
    let timer: number | undefined;
    const pollQueue = async () => {
      let keepPolling = true;
      try {
        const nextQueue = await fetchQueueStatus();
        if (disposed) return;
        setQueueStatus(nextQueue);
        keepPolling = nextQueue.jobs.some((job) => !isTerminalGenerationStatus(job.status));
        if (currentProjectId && nextQueue.jobs.some((job) => job.project_id === currentProjectId)) {
          const nextManifest = await fetchManifest(currentProjectId).catch(() => null);
          if (!disposed && nextManifest) setManifest(nextManifest);
        }
      } catch {
        keepPolling = true;
      } finally {
        if (!disposed && keepPolling) timer = window.setTimeout(pollQueue, 1000);
      }
    };
    timer = window.setTimeout(pollQueue, 400);
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [currentProjectId, queueHasActiveWork]);

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
  }, [activeServiceId, queueActiveJob?.updated_at, activeVersions.length]);

  useEffect(() => {
    if (!activeLogsReferenceRequest) return;
    if (logsReferenceAudio[activeLogsReferenceRequest.key]) return;
    setLoadingLogsReferenceKey(activeLogsReferenceRequest.key);
    const referenceService = activeLogsReferenceRequest.serviceId ? serviceById.get(activeLogsReferenceRequest.serviceId) : undefined;
    const referenceServiceId = activeLogsReferenceRequest.serviceId || null;
    const referenceRequest = isGptSovitsApiV2Service(referenceService) && Boolean(activeLogsReferenceRequest.logsName)
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
  }, [activeLogsReferenceRequest, logsReferenceAudio, serviceById, t]);

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

  async function refreshVoiceCatalog() {
    const requestToken = voiceCatalogRequestTokenRef.current + 1;
    voiceCatalogRequestTokenRef.current = requestToken;
    try {
      const payload = await fetchVoiceCatalog();
      if (voiceCatalogRequestTokenRef.current !== requestToken) return;
      setVoiceCatalog(payload);
      setVoiceCatalogError(null);
    } catch (error) {
      if (voiceCatalogRequestTokenRef.current !== requestToken) return;
      setVoiceCatalogError(error instanceof Error ? error.message : t("voiceMatching.catalogUnavailable"));
    }
  }

  async function runVoiceCatalogSync() {
    setIsSyncingVoiceCatalog(true);
    setVoiceCatalogError(null);
    try {
      await syncVoiceCatalog();
      voiceRecommendationCacheRef.current.clear();
      voiceRecommendationRequestTokenRef.current += 1;
      setVoiceRecommendations({});
      await refreshVoiceCatalog();
      setVoiceRecommendationEpoch((current) => current + 1);
    } catch (error) {
      setVoiceCatalogError(error instanceof Error ? error.message : t("voiceMatching.syncFailed"));
    } finally {
      setIsSyncingVoiceCatalog(false);
    }
  }

  async function refreshActiveVoiceRecommendation() {
    const projectId = currentProjectId;
    const lineId = activeLine?.id;
    const catalogVersion = voiceCatalog?.catalog_version;
    if (!projectId || !lineId || !catalogVersion) return;
    const cacheKey = `${projectId}|${lineId}|${catalogVersion}`;
    setVoiceRecommendationLoadingLineId(lineId);
    setVoiceRecommendationError(null);
    try {
      await flushPendingProjectAutosave(projectId);
      await rematchProjectCharacters(projectId);
      const authoritativeProject = await fetchProject(projectId);
      if (currentProjectIdRef.current !== projectId) return;
      markWorkspaceAuthoritative(projectId, authoritativeProject, charactersRef.current);
      setProject(authoritativeProject);

      voiceRecommendationCacheRef.current.delete(cacheKey);
      const requestToken = voiceRecommendationRequestTokenRef.current + 1;
      voiceRecommendationRequestTokenRef.current = requestToken;
      const payload = await recommendVoices(projectId, [lineId]);
      if (
        currentProjectIdRef.current !== projectId
        || voiceRecommendationRequestTokenRef.current !== requestToken
      ) return;
      const recommendation = payload.recommendations.find((item) => item.line_id === lineId);
      if (recommendation) {
        setVoiceRecommendations((current) => ({ ...current, [lineId]: recommendation }));
      }
      voiceRecommendationCacheRef.current.add(cacheKey);
    } catch (error) {
      voiceRecommendationCacheRef.current.delete(cacheKey);
      if (currentProjectIdRef.current === projectId) {
        setVoiceRecommendationError(error instanceof Error ? error.message : t("voiceMatching.recommendationFailed"));
      }
    } finally {
      if (currentProjectIdRef.current === projectId) {
        setVoiceRecommendationLoadingLineId(null);
      }
    }
  }

  async function chooseVoiceCandidate(candidateId: string) {
    if (!currentProjectId || !activeLine) return;
    const projectId = currentProjectId;
    const lineId = activeLine.id;
    setSelectingVoiceCandidateId(candidateId);
    try {
      await flushPendingProjectAutosave(projectId);
      await selectVoiceCandidate(projectId, lineId, candidateId);
      const authoritativeProject = await fetchProject(projectId);
      const mergedProject = mergeVoiceSelectionAuthority(
        projectRef.current,
        authoritativeProject,
        lineId
      );
      markWorkspaceAuthoritative(projectId, mergedProject, charactersRef.current);
      setProject(mergedProject);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("voiceMatching.selectionFailed"));
    } finally {
      setSelectingVoiceCandidateId(null);
    }
  }

  async function confirmFuzzyVoiceIdentity(candidateId: string) {
    if (!currentProjectId || !activeLine) return;
    const projectId = currentProjectId;
    const lineId = activeLine.id;
    setSelectingVoiceCandidateId(candidateId);
    try {
      await flushPendingProjectAutosave(projectId);
      const payload = await confirmVoiceCandidateIdentity(projectId, lineId, candidateId);
      voiceRecommendationCacheRef.current.clear();
      setVoiceRecommendations((current) => ({
        ...current,
        [lineId]: payload.recommendation
      }));
      setCharacters(payload.characters);
      markWorkspaceAuthoritative(projectId, payload.project, payload.characters);
      setProject(payload.project);
      setNotice(t("voiceMatching.identityConfirmed"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("voiceMatching.identityConfirmationFailed"));
    } finally {
      setSelectingVoiceCandidateId(null);
    }
  }

  async function clearActiveVoiceSelection() {
    if (!currentProjectId || !activeLine) return;
    const projectId = currentProjectId;
    const lineId = activeLine.id;
    setSelectingVoiceCandidateId("__clear__");
    try {
      await flushPendingProjectAutosave(projectId);
      await clearVoiceSelection(projectId, lineId);
      const authoritativeProject = await fetchProject(projectId);
      const mergedProject = mergeVoiceSelectionAuthority(
        projectRef.current,
        authoritativeProject,
        lineId
      );
      markWorkspaceAuthoritative(projectId, mergedProject, charactersRef.current);
      setProject(mergedProject);
      setNotice(t("voiceMatching.selectionCleared"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("voiceMatching.selectionFailed"));
    } finally {
      setSelectingVoiceCandidateId(null);
    }
  }

  async function runOpenSourceDetect() {
    setIsDetectingOpenSource(true);
    try {
      const payload = await detectOpenSourceTTS({
        provider_type: selectedOpenSourceProvider,
        repo_path: null,
        base_url: openSourceBaseUrl || null,
        api_contract: ttsAudioSuiteContractForProvider(selectedOpenSourceProvider)
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
        ...buildComfyUIEndpointRequest({
          provider_type: selectedOpenSourceProvider,
          display_name: openSourceDisplayName || null,
          base_url: openSourceBaseUrl,
          resource_group: openSourceResourceGroup,
          capacity: openSourceCapacity,
          resource_id: openSourceResourceId || selectedOpenSourceCatalog?.default_resource_id || null,
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
        api_contract: ttsAudioSuiteContractForProvider(selectedOpenSourceProvider)
      });
      setOpenSourceDetectResult(detectPayload);
      if (!["partial", "ready"].includes(detectPayload.setup_state)) {
        setNotice(t("services.openSourceDetectNotSaved", { state: setupStateLabel(detectPayload.setup_state, t) }));
        return;
      }
      const payload = await configureOpenSourceTTS({
        ...buildComfyUIEndpointRequest({
          provider_type: selectedOpenSourceProvider,
          display_name: openSourceDisplayName || null,
          base_url: openSourceBaseUrl,
          resource_group: openSourceResourceGroup,
          capacity: openSourceCapacity,
          resource_id: openSourceResourceId || selectedOpenSourceCatalog?.default_resource_id || null,
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

  async function refreshProjects(preferredProjectId?: string | null, canApply: () => boolean = () => true) {
    try {
      const payload = await fetchProjects();
      if (!canApply()) return;
      setProjectSummaries(payload.projects);
      setCurrentProjectId((current) => {
        if (!canApply()) return current;
        const preferred = preferredProjectId !== undefined ? preferredProjectId : current ?? readStoredProjectId();
        const next = selectStartupProjectId(payload.projects, preferred);
        currentProjectIdRef.current = next;
        writeStoredProjectId(next);
        return next;
      });
    } catch {
      if (!canApply()) return;
      setProjectSummaries([]);
      currentProjectIdRef.current = null;
      setCurrentProjectId(null);
      writeStoredProjectId(null);
    }
  }

  async function createNewScriptProject() {
    const title = newScriptTitle.trim();
    const source = newScriptSource;
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
      const savedProject = source.trim()
        ? (await createScriptRevision(projectId, source, t("script.initialScriptRevision"))).project
        : nextProject;
      currentProjectIdRef.current = projectId;
      setCurrentProjectId(projectId);
      writeStoredProjectId(projectId);
      markWorkspaceAuthoritative(projectId, savedProject, charactersRef.current);
      setProject(savedProject);
      setManifest(createEmptyManifest(projectId));
      setActiveLineId("");
      setExpandedLineId(null);
      setSelectedHistoryVersions({});
      setVersionDrafts({});
      managedProjectIdRef.current = projectId;
      setManagedProjectId(projectId);
      setManagedProject(savedProject);
      setManagerTitleDraft(savedProject.title);
      setManagerSourceDraft(source.trim() ? source : projectToScriptSourceText(savedProject, characters));
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

  async function flushPendingProjectAutosave(
    projectId: string,
    authoritativeProject?: ScriptProject
  ): Promise<ScriptProject | null> {
    const pending = pendingProjectAutosaveRef.current?.projectId === projectId
      ? pendingProjectAutosaveRef.current
      : null;
    if (pending) {
      if (pending.timerId !== null) window.clearTimeout(pending.timerId);
      pendingProjectAutosaveRef.current = null;
    }
    let resolvedAuthority = authoritativeProject;
    const recoveringUnknownAuthority = !resolvedAuthority
      && authorityUnknownProjectIdsRef.current.has(projectId);
    if (recoveringUnknownAuthority) {
      await saveChainRef.current;
      try {
        resolvedAuthority = await fetchProject(projectId);
      } catch (error) {
        if (pending && !pendingProjectAutosaveRef.current) {
          pendingProjectAutosaveRef.current = { ...pending, timerId: null };
        }
        setSaveState("error");
        setNotice(error instanceof Error ? error.message : t("notice.autoSaveFailed"));
        return null;
      }
    }
    let flushedProject: ScriptProject | null = null;
    let saveOutcome: ProjectSaveOutcome | null = null;
    if (pending) {
      flushedProject = resolvedAuthority
        ? mergeAuthoritativeProjectStructure(pending.project, resolvedAuthority)
        : pending.project;
      saveChainRef.current = saveChainRef.current.then(async () => {
        saveOutcome = await saveCurrentProject(
          pending.projectId,
          flushedProject!,
          pending.characters,
          pending.authorityEpoch
        );
      });
    }
    await saveChainRef.current;
    if (saveOutcome === "failed") {
      if (!pendingProjectAutosaveRef.current && pending?.projectId === projectId) {
        pendingProjectAutosaveRef.current = { ...pending, timerId: null };
      }
      return null;
    }
    if (recoveringUnknownAuthority && resolvedAuthority) {
      flushedProject = await drainPendingProjectAutosaves(
        projectId,
        flushedProject ?? resolvedAuthority
      );
      if (pendingProjectAutosaveRef.current?.projectId === projectId) return null;
      authorityUnknownProjectIdsRef.current.delete(projectId);
      applyAuthoritativeProjectStructure(projectId, flushedProject);
    }
    return flushedProject;
  }

  async function drainPendingProjectAutosaves(
    projectId: string,
    authoritativeProject: ScriptProject
  ): Promise<ScriptProject> {
    let reconciledProject = authoritativeProject;
    do {
      const flushedProject = await flushPendingProjectAutosave(projectId, reconciledProject);
      if (!flushedProject) break;
      reconciledProject = flushedProject;
    } while (pendingProjectAutosaveRef.current?.projectId === projectId);
    return reconciledProject;
  }

  function applyAuthoritativeProjectStructure(
    projectId: string,
    authoritativeProject: ScriptProject
  ): void {
    if (analysisCurrentProjectIdRef.current === projectId) {
      if (analysisManagedProjectIdRef.current === projectId) {
        preserveManagerSourceDraftProjectIdRef.current = projectId;
      }
      const mergedProject = mergeAuthoritativeProjectStructure(
        projectRef.current,
        authoritativeProject
      );
      markWorkspaceAuthoritative(projectId, mergedProject, charactersRef.current);
      setProject(mergedProject);
    }
    if (analysisManagedProjectIdRef.current === projectId) {
      setManagedProject((current) => current
        ? mergeAuthoritativeProjectStructure(current, authoritativeProject)
        : current);
    }
  }

  function mergeAuthoritativeProjectStructure(
    current: ScriptProject,
    authoritativeProject: ScriptProject
  ): ScriptProject {
    return {
      ...current,
      title: authoritativeProject.title,
      active_script_revision_id: authoritativeProject.active_script_revision_id,
      active_parse_revision_id: authoritativeProject.active_parse_revision_id,
      script_revisions: authoritativeProject.script_revisions,
      parse_revisions: authoritativeProject.parse_revisions
    };
  }

  async function saveCurrentProject(
    targetProjectId: string,
    projectSnapshot: ScriptProject,
    characterSnapshot: Character[],
    authorityEpoch: number
  ): Promise<ProjectSaveOutcome> {
    if (authorityEpoch !== (projectAuthorityEpochRef.current.get(targetProjectId) ?? 0)) {
      setSaveState("saved");
      return "stale";
    }
    try {
      await Promise.all([saveProject(targetProjectId, projectSnapshot), saveCharacters(characterSnapshot)]);
      if (authorityEpoch !== (projectAuthorityEpochRef.current.get(targetProjectId) ?? 0)) {
        setSaveState("saved");
        setLastSavedAt(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
        return "stale";
      }
      markWorkspaceAuthoritative(
        targetProjectId,
        projectSnapshot,
        characterSnapshot
      );
      setSaveState("saved");
      setLastSavedAt(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
      setNotice(t("notice.autoSaved"));
      await refreshProjects();
      return "saved";
    } catch (error) {
      setSaveState("error");
      setNotice(error instanceof Error ? error.message : t("notice.autoSaveFailed"));
      return "failed";
    }
  }

  async function runQueue(lines: ScriptLine[]) {
    const projectId = currentProjectId;
    if (!projectId) {
      setNotice(t("empty.noProjectAction"));
      return;
    }
    const submissionKeys = lines.map((line) => generationLineKey(projectId, line));
    if (
      submissionKeys.some((key) => submittingGenerationKeysRef.current.has(key))
      || lines.some((line) => lineHasActiveGeneration(queueJobs, projectId, line))
    ) return;
    submissionKeys.forEach((key) => submittingGenerationKeysRef.current.add(key));
    setSubmittingGenerationKeys((current) => Array.from(new Set([...current, ...submissionKeys])));
    setNotice(t("notice.generating"));
    try {
      const { tasks, blocked } = buildRunnableTasks(lines, resolvedCharacters);
      if (blocked.length > 0) {
        setNotice(t("notice.linesNeedBinding", { count: blocked.length }), { level: "warning" });
      }
      if (tasks.length === 0) return;
      const preflight = await ensureGenerationPreflight(projectId, tasks);
      if (preflight.status !== "ready") return;
      const job = await createGenerationJob(projectId, tasks);
      setQueueStatus((current) => upsertGenerationJob(current, job));
      setNotice(t("notice.jobQueued", { job: job.job_id }));
      if (isTerminalGenerationStatus(job.status)) void refreshTopology();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("notice.generationFailed"));
    } finally {
      submissionKeys.forEach((key) => submittingGenerationKeysRef.current.delete(key));
      setSubmittingGenerationKeys((current) => current.filter((key) => !submissionKeys.includes(key)));
    }
  }

  async function ensureGenerationPreflight(projectId: string, tasks: GenerationTask[]): Promise<GenerationPreflightResponse> {
    let preflight = await generationPreflight(projectId, tasks);
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
        preflight = await generationPreflight(projectId, tasks);
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

  async function switchProject(projectId: string): Promise<boolean> {
    const operationToken = currentProjectTransitionOperationTokenRef.current + 1;
    currentProjectTransitionOperationTokenRef.current = operationToken;
    const transition = currentProjectTransitionChainRef.current.then(async () => {
      const previousProjectId = analysisCurrentProjectIdRef.current;
      if (projectId === previousProjectId) return true;
      if (previousProjectId) {
        await flushPendingProjectAutosave(previousProjectId);
        if (operationToken !== currentProjectTransitionOperationTokenRef.current) return false;
        if (
          authorityUnknownProjectIdsRef.current.has(previousProjectId)
          || pendingProjectAutosaveRef.current?.projectId === previousProjectId
        ) return false;
      }
      if (operationToken !== currentProjectTransitionOperationTokenRef.current) return false;
      currentProjectIdRef.current = projectId;
      analysisCurrentProjectIdRef.current = projectId;
      setCurrentProjectId(() => {
        writeStoredProjectId(projectId);
        return projectId;
      });
      setExpandedLineId(null);
      setSelectedHistoryVersions({});
      setVersionDrafts({});
      setNotice(t("app.ready"));
      return true;
    });
    currentProjectTransitionChainRef.current = transition.then(
      () => undefined,
      () => undefined
    );
    return transition;
  }

  function applyManagedProjectToWorkspace(projectId: string, nextProject: ScriptProject, resetLineState = false) {
    if (projectId !== currentProjectIdRef.current) return;
    markWorkspaceAuthoritative(projectId, nextProject, charactersRef.current);
    setProject(nextProject);
    if (resetLineState) {
      setActiveLineId(nextProject.lines[0]?.id ?? "");
      setExpandedLineId(null);
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

  function updateManagedSourceDraft(value: string) {
    scriptFileOperationTokenRef.current += 1;
    analysisStartOperationTokenRef.current += 1;
    analysisSourceFileMetadataRef.current = null;
    setManagerSourceDraft(value);
  }

  async function selectManagedScriptFile(file: File, target: ScriptFileTarget) {
    const operationToken = scriptFileOperationTokenRef.current + 1;
    scriptFileOperationTokenRef.current = operationToken;
    const targetProjectId = target === "existing" ? analysisManagedProjectIdRef.current : null;
    const currentSource = target === "existing" ? managerSourceDraft : newScriptSource;
    const persistedSource = target === "existing" ? projectPreviewStats(managedProject).activeSourceMarkdown : "";
    if (currentSource !== persistedSource && currentSource.trim()) {
      const confirmed = await requestConfirmation({
        title: t("script.replaceSourceTitle"),
        body: t("script.replaceSourceBody", { filename: file.name }),
        detail: t("script.replaceSourceDetail"),
        confirmLabel: t("script.replaceSourceConfirm"),
        cancelLabel: t("actions.cancel"),
        tone: "warning"
      });
      if (!confirmed) return;
    }
    const isCurrent = () => operationToken === scriptFileOperationTokenRef.current
      && (target === "new" || targetProjectId === analysisManagedProjectIdRef.current);
    const outcome = await readAnalysisScriptFile(file, currentSource, isCurrent);
    if (outcome.status === "stale") return;
    if (outcome.status === "error") {
      setNotice(t(outcome.errorCode === "unsupported_script_file"
        ? "analysis.input.unsupportedFile"
        : "analysis.input.readFailed"));
      return;
    }
    analysisStartOperationTokenRef.current += 1;
    analysisSourceFileMetadataRef.current = outcome.metadata;
    if (target === "existing") {
      setManagerSourceDraft(outcome.source);
    } else {
      setNewScriptSource(outcome.source);
      setNewScriptTitle((current) => current.trim() ? current : scriptTitleFromFilename(file.name));
    }
    if (outcome.metadata.warning) {
      setNotice(t("analysis.input.largeFileWarning", {
        count: outcome.metadata.warning.codePointCount,
        limit: outcome.metadata.warning.limit
      }));
    }
  }

  async function analyzeManagedScriptRevision() {
    if (!managedProjectId || !managedProject) return;
    const targetProjectId = managedProjectId;
    const title = managerTitleDraft.trim();
    const source = managerSourceDraft;
    if (!title) {
      setNotice(t("script.newScriptTitleRequired"));
      return;
    }
    if (!source.trim()) {
      setNotice(t("script.sourceRequired"));
      return;
    }
    const operationToken = analysisStartOperationTokenRef.current + 1;
    analysisStartOperationTokenRef.current = operationToken;
    const isCurrent = () => operationToken === analysisStartOperationTokenRef.current
      && targetProjectId === analysisManagedProjectIdRef.current;
    analysisManagerSavingTokenRef.current = operationToken;
    analysisAutosaveBlockedProjectIdRef.current = targetProjectId;
    setIsManagerSaving(true);
    let createdAuthoritativeProject: ScriptProject | null = null;
    let createAttempted = false;
    try {
      const currentAutosaveProjectId = analysisCurrentProjectIdRef.current;
      if (currentAutosaveProjectId) await flushPendingProjectAutosave(currentAutosaveProjectId);
      if (targetProjectId !== currentAutosaveProjectId) await flushPendingProjectAutosave(targetProjectId);
      if (!isCurrent()) return;
      const confirmed = await confirmRevisionRisk(managedProject);
      if (!confirmed || !isCurrent()) return;
      const latestCurrentAutosaveProjectId = analysisCurrentProjectIdRef.current;
      if (latestCurrentAutosaveProjectId) await flushPendingProjectAutosave(latestCurrentAutosaveProjectId);
      if (targetProjectId !== latestCurrentAutosaveProjectId) await flushPendingProjectAutosave(targetProjectId);
      if (!isCurrent()) return;
      projectAuthorityEpochRef.current.set(
        targetProjectId,
        (projectAuthorityEpochRef.current.get(targetProjectId) ?? 0) + 1
      );
      await saveChainRef.current;
      if (!isCurrent()) return;
      if (title !== managedProject.title) {
        await saveProject(targetProjectId, { ...managedProject, title });
      }
      if (!isCurrent()) return;
      createAttempted = true;
      authorityUnknownProjectIdsRef.current.add(targetProjectId);
      await beginAnalysisSourceRevision({
        projectId: targetProjectId,
        source,
        summary: t("analysis.input.analyze"),
        metadata: analysisSourceFileMetadataRef.current ?? undefined,
        isCurrent,
        onCreated: async (payload) => {
          authorityUnknownProjectIdsRef.current.delete(targetProjectId);
          createdAuthoritativeProject = payload.project;
          createdAuthoritativeProject = await drainPendingProjectAutosaves(
            targetProjectId,
            createdAuthoritativeProject
          );
          applyAuthoritativeProjectStructure(targetProjectId, createdAuthoritativeProject);
          return {
            ...payload,
            project: createdAuthoritativeProject
          };
        },
        onReady: async (payload) => {
          const latestCurrentProjectId = analysisCurrentProjectIdRef.current;
          if (latestCurrentProjectId && latestCurrentProjectId !== targetProjectId) {
            await flushPendingProjectAutosave(latestCurrentProjectId);
          }
          createdAuthoritativeProject = await drainPendingProjectAutosaves(
            targetProjectId,
            createdAuthoritativeProject ?? payload.project
          );
          applyAuthoritativeProjectStructure(targetProjectId, createdAuthoritativeProject);
          if (!isCurrent()) return;
          const readyProject = createdAuthoritativeProject;
          setManagedProject(readyProject);
          setManagerTitleDraft(readyProject.title);
          setManagerSourceDraft(payload.script_revision.source_markdown);
          if (targetProjectId === analysisCurrentProjectIdRef.current) {
            applyManagedProjectToWorkspace(targetProjectId, readyProject);
          }
          analysisProjectIdRef.current = targetProjectId;
          writeActiveAnalysisScope(targetProjectId, payload.script_revision);
          setAnalysisSourceRevision(payload.script_revision);
          setWorkspaceStage("analysis");
        }
      });
      if (isCurrent()) await refreshProjects();
    } catch (error) {
      if (createAttempted && !createdAuthoritativeProject) {
        try {
          createdAuthoritativeProject = await fetchProject(targetProjectId);
          authorityUnknownProjectIdsRef.current.delete(targetProjectId);
          createdAuthoritativeProject = await drainPendingProjectAutosaves(
            targetProjectId,
            createdAuthoritativeProject
          );
          applyAuthoritativeProjectStructure(targetProjectId, createdAuthoritativeProject);
        } catch {
          // An attempted create has an ambiguous server outcome. Keep stale writes blocked.
        }
      }
      if (isCurrent()) setNotice(error instanceof Error ? error.message : t("analysis.input.readFailed"));
    } finally {
      if (createdAuthoritativeProject) {
        createdAuthoritativeProject = await drainPendingProjectAutosaves(
          targetProjectId,
          createdAuthoritativeProject
        );
        applyAuthoritativeProjectStructure(targetProjectId, createdAuthoritativeProject);
      } else if (!createAttempted) {
        await flushPendingProjectAutosave(targetProjectId);
      }
      if (analysisAutosaveBlockedProjectIdRef.current === targetProjectId) {
        analysisAutosaveBlockedProjectIdRef.current = null;
      }
      if (analysisManagerSavingTokenRef.current === operationToken) {
        analysisManagerSavingTokenRef.current = null;
        setIsManagerSaving(false);
      }
    }
  }

  function applyConfirmedAnalysisProject(serverProject: ScriptProject) {
    const targetProjectId = analysisProjectIdRef.current;
    const sourceRevision = analysisSourceRevision;
    if (!targetProjectId || !sourceRevision) return;
    const handoff = buildConfirmedAnalysisHandoff(targetProjectId, serverProject, sourceRevision);
    const changesCurrentProject = analysisCurrentProjectIdRef.current !== handoff.currentProjectId;
    analysisCurrentProjectIdRef.current = handoff.currentProjectId;
    analysisManagedProjectIdRef.current = handoff.managedProjectId;
    seededAuthoritativeProjectIdRef.current = changesCurrentProject ? handoff.currentProjectId : null;
    writeStoredProjectId(handoff.currentProjectId);
    setCurrentProjectId(handoff.currentProjectId);
    markWorkspaceAuthoritative(
      handoff.currentProjectId,
      handoff.project,
      charactersRef.current
    );
    setProject(handoff.project);
    setActiveLineId(handoff.activeLineId);
    setExpandedLineId(handoff.expandedLineId);
    setSelectedHistoryVersions(handoff.selectedHistoryVersions);
    setVersionDrafts(handoff.versionDrafts);
    setLineTextDrafts(handoff.lineTextDrafts);
    setManagedProjectId(handoff.managedProjectId);
    setManagedProject(handoff.managedProject);
    setManagerTitleDraft(handoff.managerTitleDraft);
    setManagerSourceDraft(handoff.managerSourceDraft);
    setSaveState("saved");
    setLastSavedAt(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
    archiveRestorableAnalysisSession(targetProjectId, sourceRevision);
    clearActiveAnalysisScope(activeAnalysisScopeForRevision(targetProjectId, sourceRevision));
    analysisConfirmedReviewRef.current = false;
    const summaryRefreshEpoch = analysisStartOperationTokenRef.current;
    analysisProjectIdRef.current = null;
    analysisSourceFileMetadataRef.current = null;
    setAnalysisSourceRevision(null);
    setWorkspaceStage("tts");
    const confirmedLineIds = handoff.project.lines.map((line) => line.id);
    if (confirmedLineIds.length > 0) {
      void recommendVoices(handoff.currentProjectId, confirmedLineIds, { applyAutomatic: true })
        .then(async (payload) => {
          if (analysisCurrentProjectIdRef.current !== handoff.currentProjectId) return;
          setVoiceRecommendations(Object.fromEntries(
            payload.recommendations.map((recommendation) => [recommendation.line_id, recommendation])
          ));
          const authoritativeProject = await fetchProject(handoff.currentProjectId);
          if (analysisCurrentProjectIdRef.current !== handoff.currentProjectId) return;
          markWorkspaceAuthoritative(
            handoff.currentProjectId,
            authoritativeProject,
            charactersRef.current
          );
          setProject(authoritativeProject);
        })
        .catch((error) => {
          if (analysisCurrentProjectIdRef.current === handoff.currentProjectId) {
            setVoiceRecommendationError(
              error instanceof Error ? error.message : t("voiceMatching.recommendationFailed")
            );
          }
        });
    }
    void fetchProjects()
      .then((payload) => {
        if (
          analysisStartOperationTokenRef.current === summaryRefreshEpoch
          && analysisCurrentProjectIdRef.current === targetProjectId
        ) {
          setProjectSummaries(payload.projects);
        }
      })
      .catch(() => undefined);
  }

  function cancelScriptAnalysis() {
    analysisStartOperationTokenRef.current += 1;
    const targetProjectId = analysisProjectIdRef.current;
    if (targetProjectId && analysisSourceRevision) {
      if (analysisConfirmedReviewRef.current) {
        archiveRestorableAnalysisSession(targetProjectId, analysisSourceRevision);
      }
      clearActiveAnalysisScope(activeAnalysisScopeForRevision(targetProjectId, analysisSourceRevision));
    }
    analysisConfirmedReviewRef.current = false;
    analysisProjectIdRef.current = null;
    setAnalysisSourceRevision(null);
    setWorkspaceStage("tts");
  }

  async function reviewConfirmedAnnotations() {
    if (!currentProjectId || !currentSourceRevision || isReturningToAnalysis) return;
    const targetProjectId = currentProjectId;
    const expectedActiveRevisionId = currentSourceRevision.revision_id;
    let targetRevision = currentSourceRevision;
    const operationToken = analysisStartOperationTokenRef.current + 1;
    analysisStartOperationTokenRef.current = operationToken;
    setIsReturningToAnalysis(true);
    try {
      const session = await fetchAnalysisReviewSession(targetProjectId);
      if (
        analysisStartOperationTokenRef.current !== operationToken
        || analysisCurrentProjectIdRef.current !== targetProjectId
        || projectRef.current.active_script_revision_id !== expectedActiveRevisionId
      ) return;
      const recoveredRevision = projectRef.current.script_revisions?.find(
        (revision) => revision.revision_id === session.source_revision_id
      );
      if (!recoveredRevision) throw new Error(t("app.analysisResultUnavailable"));
      targetRevision = recoveredRevision;
      writeAnalysisRunSession(
        defaultAnalysisStorage(),
        analysisScopeStorageId(targetProjectId, targetRevision),
        { runId: session.run_id, draftId: session.draft_id }
      );
      if (
        analysisStartOperationTokenRef.current !== operationToken
        || analysisCurrentProjectIdRef.current !== targetProjectId
        || projectRef.current.active_script_revision_id !== expectedActiveRevisionId
      ) return;
      const scope = reviewConfirmedAnalysis(targetProjectId, targetRevision);
      analysisProjectIdRef.current = scope.projectId;
      analysisConfirmedReviewRef.current = true;
      setAnalysisSourceRevision(targetRevision);
      writeActiveAnalysisScope(targetProjectId, targetRevision);
      setWorkspaceStage("analysis");
    } catch (error) {
      setNotice(
        error instanceof ApiRequestError && error.status === 404
          ? t("app.analysisResultUnavailable")
          : error instanceof Error
            ? error.message
            : t("app.analysisResultUnavailable"),
        { level: "warning" }
      );
    } finally {
      if (analysisStartOperationTokenRef.current === operationToken) setIsReturningToAnalysis(false);
    }
  }

  async function saveManagedScriptRevision() {
    if (!managedProjectId || !managedProject) return;
    const title = managerTitleDraft.trim();
    const source = managerSourceDraft;
    if (!title) {
      setNotice(t("script.newScriptTitleRequired"));
      return;
    }
    if (!source.trim()) {
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

  function startCreatingScript() {
    scriptFileOperationTokenRef.current += 1;
    analysisStartOperationTokenRef.current += 1;
    analysisSourceFileMetadataRef.current = null;
    analysisManagedProjectIdRef.current = null;
    managedProjectIdRef.current = null;
    setManagedProjectId(null);
    setManagedProject(null);
  }

  async function deleteManagedProject(projectId: string) {
    const summary = projectRows.find((item) => item.project_id === projectId);
    const title = summary?.title || projectId;
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
    const nextCurrentProjectId = nextProjectAfterDelete(allRows, projectId, currentProjectId);
    const deletingSelectedProject = projectId === managedProjectId;
    const nextManagedProjectId = deletingSelectedProject
      ? nextProjectAfterDelete(visibleRows, projectId, managedProjectId) ?? nextCurrentProjectId
      : managedProjectId;
    const deletedCurrentProject = projectId === currentProjectId;
    setDeletingProjectId(projectId);
    try {
      await deleteProject(projectId);
      if (deletingSelectedProject) {
        managedProjectIdRef.current = nextManagedProjectId;
        analysisManagedProjectIdRef.current = nextManagedProjectId;
        setManagedProjectId(nextManagedProjectId);
        setManagedProject(null);
      }
      if (deletedCurrentProject) {
        currentProjectIdRef.current = nextCurrentProjectId;
        setCurrentProjectId(nextCurrentProjectId);
        writeStoredProjectId(nextCurrentProjectId);
        if (!nextCurrentProjectId) {
          setProject(createEmptyProject());
          setManifest(createEmptyManifest(null));
          setActiveLineId("");
          setExpandedLineId(null);
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
      deletingProjectId={deletingProjectId}
      onClose={() => undefined}
      onSearchTextChange={setManagerSearchText}
      onSelectProject={(projectId) => {
        if (projectId === analysisManagedProjectIdRef.current) return;
        scriptFileOperationTokenRef.current += 1;
        analysisStartOperationTokenRef.current += 1;
        analysisSourceFileMetadataRef.current = null;
        analysisManagedProjectIdRef.current = projectId;
        managedProjectIdRef.current = projectId;
        setManagedProjectId(projectId);
      }}
      onOpenProject={(projectId) => {
        void (async () => {
          if (!(await switchProject(projectId))) return;
          scriptFileOperationTokenRef.current += 1;
          analysisStartOperationTokenRef.current += 1;
          analysisSourceFileMetadataRef.current = null;
          analysisManagedProjectIdRef.current = projectId;
          managedProjectIdRef.current = projectId;
          setManagedProjectId(projectId);
        })();
      }}
      onTitleDraftChange={setManagerTitleDraft}
      onSourceDraftChange={updateManagedSourceDraft}
      onNewScriptTitleChange={setNewScriptTitle}
      onNewScriptSourceChange={setNewScriptSource}
      onStartCreateScript={startCreatingScript}
      onCreateScript={() => void createNewScriptProject()}
      onRenameScript={() => void renameManagedProject()}
      onSaveRevision={() => void saveManagedScriptRevision()}
      onAnalyzeScript={() => void analyzeManagedScriptRevision()}
      onScriptFileSelected={(file, target) => void selectManagedScriptFile(file, target)}
      onDeleteScript={(projectId) => void deleteManagedProject(projectId)}
    />
  );

  const appOverlays = (
    <>
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
    </>
  );

  return (
    <>
      <TokenGate />
      <WorkbenchShell
        stage={workspaceStage}
        overlays={appOverlays}
        sidebar={(
          <>
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
          </>
        )}
      >
        <AnalysisStageGate
          stage={workspaceStage}
          projectId={analysisProjectIdRef.current}
          sourceRevision={analysisSourceRevision}
          onConfirmed={applyConfirmedAnalysisProject}
          onCancel={cancelScriptAnalysis}
          controllerOptions={{
            mode: analysisConfirmedReviewRef.current ? "review" : "analyze"
          }}
          ttsWorkbench={(
            <>
        <header className="topbar">
          <div className="toolbar topbar-toolbar">
            <span className={`notice ${toasts.length > 0 ? `notice-${toasts[toasts.length - 1].level}` : ""}`} title={notice}>{notice || t("app.ready")}</span>
            <button
              className="topbar-action-button"
              data-action="review-confirmed-annotations"
              disabled={!currentProjectId || !currentSourceRevision || isReturningToAnalysis}
              onClick={() => void reviewConfirmedAnnotations()}
              title={t("app.reviewConfirmedAnnotationsHint")}
              type="button"
            >
              {isReturningToAnalysis ? <Loader2 className="spin" size={15} /> : <ArrowLeft size={15} />}
              <span className="menu-trigger-label">{t("app.reviewConfirmedAnnotations")}</span>
            </button>
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
                <ServiceCenter
                  title={topologyModalTitle}
                  description={topologyModalDescription}
                  className={topologyModalClass}
                  refreshing={isRefreshingTopology}
                  refreshLabel={t("actions.refresh")}
                  closeLabel={t("actions.close")}
                  onRefresh={() => void refreshTopology(true)}
                  onClose={() => setIsTopologyMenuOpen(false)}
                >
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
                              <div className={`overview-card state-${serviceSummary.resources.ready ? "ready" : "attention"}`}>
                                <span>{t("services.resourceReady")}</span>
                                <strong>{serviceSummary.resources.ready ? t("status.ready") : t("status.needsMapping")}</strong>
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
                                    onClick={() => {
                                      setSelectedOpenSourceProvider(item.provider_type);
                                      setOpenSourceResourceId("");
                                    }}
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
                                <label>
                                  <span>{t("services.openSourceResourceId")}</span>
                                  <input value={openSourceResourceId} onChange={(event) => setOpenSourceResourceId(event.target.value)} placeholder={selectedOpenSourceCatalog?.default_resource_id} />
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
                            <VoiceAssetStatusPanel
                              catalog={voiceCatalog}
                              syncing={isSyncingVoiceCatalog}
                              error={voiceCatalogError}
                              onSync={() => void runVoiceCatalogSync()}
                            />
                            <QueuePanel
                              jobs={queueJobs}
                              activeJob={queueActiveJob}
                              queued={queueQueuedItems}
                              running={queueRunningItems}
                              completed={queueCompletedItems}
                              failed={queueFailedItems}
                              cancelled={queueCancelledItems}
                              total={queueTotalItems}
                              processed={queueProcessedItems}
                              progressPercent={queueProgressPercent}
                              statusLabel={queueVisibleStatusLabel}
                              statusTone={queueVisibleTone}
                              externalStatusLabel={(status) => statusText(status, t)}
                            />
                          </div>
                        )}

                        {servicePanelSection === "roles" && (
                          <RoleLibraryPanel>
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
                                            setActiveProjectRoleId(
                                              suggestedProjectRoleId(candidate, projectCharacters)
                                              ?? activeProjectRoleId
                                            );
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
                                      <button className="primary-button compact-button" onClick={writeActiveModelToLibrary} disabled={!activeProjectCharacter}>{t("characters.bindToProjectRole")}</button>
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
                                  </div>
                                  <div className="role-detail-metrics">
                                    <div><span>GPT</span><strong>{activeRoleCandidate.gpt_weights?.length ?? 0}</strong></div>
                                    <div><span>SoVITS</span><strong>{activeRoleCandidate.sovits_weights?.length ?? 0}</strong></div>
                                    <div><span>Ref</span><strong>{activeRoleCandidate.reference_audio_groups?.reduce((sum, group) => sum + (group.samples?.length ?? 0), 0) ?? 0}</strong></div>
                                  </div>
                                  <section className="role-config-card">
                                    <div className="role-config-head">
                                      <strong>{t("characters.candidateProjectRole")}</strong>
                                      <select value={activeProjectCharacter?.project_character_id ?? ""} onChange={(event) => setActiveProjectRoleId(event.target.value || null)}>
                                        {projectRoleRows.map((role) => (
                                          <option value={role.id} key={role.id}>{role.name}</option>
                                        ))}
                                      </select>
                                    </div>
                                    <div className="role-detail-card">
                                      <span>{t("characters.candidateAssociation")}</span>
                                      <strong>
                                        {roleCandidateHasCompleteTrainingTask(activeRoleCandidate)
                                          ? t("characters.completeTrainingTask")
                                          : t("characters.incompleteTrainingTask")}
                                      </strong>
                                      <small>
                                        {roleCandidateHasCompleteTrainingTask(activeRoleCandidate)
                                          ? t("characters.completeTrainingTaskHint", { role: activeProjectCharacter?.name ?? t("status.unassigned") })
                                          : t("characters.incompleteTrainingTaskHint")}
                                      </small>
                                    </div>
                                    <div className="role-model-actions">
                                      <button className="primary-button compact-button" onClick={() => void importCandidate(activeRoleCandidate)}>
                                        {roleCandidateHasCompleteTrainingTask(activeRoleCandidate) && activeProjectCharacter
                                          ? t("characters.importAndLinkCandidate")
                                          : t("characters.importCandidate")}
                                      </button>
                                    </div>
                                  </section>
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
                          </RoleLibraryPanel>
                        )}
                      </section>
                </ServiceCenter>
              )}
            </div>
            <button className="language-select language-toggle" onClick={() => void cycleLanguage()} title={selectedLanguageLabel}>
              <Languages size={15} />
              <span>{selectedLanguageLabel}</span>
            </button>
          </div>
        </header>

        <LineWorkspace
          lineList={(
            <>
            {lineWorkbenchState.filtersVisible && (
            <div className="filters-row">
              <label className="search-field">
                <Search size={15} />
                <input value={searchText} onChange={(event) => setSearchText(event.target.value)} placeholder={t("filters.search")} />
              </label>
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
                      <option value="cancelling">{t("status.cancelling")}</option>
                      <option value="completed">{t("filters.completed")}</option>
                      <option value="failed">{t("filters.failed")}</option>
                      <option value="cancelled">{t("status.cancelled")}</option>
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
              <QueueDropdown jobs={queueJobs} currentProjectId={currentProjectId} lineLabels={queueLineLabels} />
            </div>
            )}
            {lineWorkbenchState.roleStripVisible && (
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
            )}

            <div className="line-table line-card-list">
              {displayedLines.map((line) => {
                const summary = summarizeLineHistory(lineHistoryForLine(manifest, line));
                const queueItem = latestQueueItemForLine(queueJobs, currentProjectId, line);
                const visibleTone = queueItem ? generationStatusTone(queueItem.status) : summary.tone;
                const visibleLabel = queueItem ? t(generationStatusKey(queueItem.status)) : summaryLabel(summary, t);
                const rowBinding = lineBinding(line, resolvedCharacters);
                const canGenerateLine = Boolean(rowBinding);
                const lineSubmitting = Boolean(currentProjectId && submittingGenerationKeys.includes(generationLineKey(currentProjectId, line)));
                const lineGenerationBusy = lineSubmitting || lineHasActiveGeneration(queueJobs, currentProjectId, line);
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
                      {queueItem?.external_status && (
                        <span className="line-meta-chip neutral" title={queueItem.external_job_id ?? ""}>
                          {t("queue.promptStatus", { status: statusText(queueItem.external_status, t) })}
                        </span>
                      )}
                      {queueItem?.cluster_size && queueItem.cluster_size > 1 && (
                        <span className="line-meta-chip ok" title={queueItem.cluster_key}>
                          {t("queue.cluster", { current: queueItem.cluster_position ?? 1, total: queueItem.cluster_size })}
                        </span>
                      )}
                      {queueItem?.error && <span className="line-meta-chip danger" title={queueItem.error}>{t("queue.routeError")}</span>}
                      {!canGenerateLine && <span className="line-meta-chip attention">{t("status.needsSetup")}</span>}
                      <span className="row-actions">
                        <button className="icon-button tiny" onClick={(event) => { event.stopPropagation(); playLine(line); }} title={t("actions.playLatest")}><Play size={14} /></button>
                        <button className="icon-button tiny" disabled={!canGenerateLine || lineGenerationBusy} onClick={(event) => { event.stopPropagation(); void runQueue([line]); }} title={canGenerateLine ? t("actions.regenerate") : t("inspector.needsTemporaryBinding")}>
                          {lineGenerationBusy ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />}
                        </button>
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
              {lineWorkbenchState.emptyState && (
                <div className="empty-row table-empty line-empty-state quiet">
                  <strong>{lineWorkbenchEmptyTitle(lineWorkbenchState.emptyState, t)}</strong>
                  <span>{lineWorkbenchEmptyHint(lineWorkbenchState.emptyState, t)}</span>
                  {lineWorkbenchState.clearFiltersVisible && (
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
                  )}
                </div>
              )}
            </div>
            </>
          )}
          inspector={(
            <VoiceInspector mode={activeInspectorMode}>
            {activeLine && (
              <div className="inspector-stack">
                <section className="inspector-card resident-voice-summary">
                  <div>
                    <span>{t("inspector.currentVoice")}</span>
                    <strong title={activeProfileLabel}>{activeProfileLabel}</strong>
                  </div>
                  <button className="secondary-button compact-button" type="button" onClick={() => setIsVoiceConfigurationOpen(true)}>
                    {t("inspector.voiceConfiguration")}
                  </button>
                </section>

                <VoiceConfigurationDrawer
                  open={isVoiceConfigurationOpen}
                  title={t("inspector.voiceConfiguration")}
                  closeLabel={t("actions.close")}
                  onClose={() => setIsVoiceConfigurationOpen(false)}
                >
                  <section className="voice-config-section">
                    <h3>{t("inspector.soundAndModels")}</h3>

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
                                    <select value={activeGptWeightOption.value} onChange={(event) => updateActiveBindingConfig({ gpt_weights_path: event.target.value || undefined })}>
                                      <option value="">{t("inspector.autoDefault")}</option>
                                      {activeGptWeightOption.relativePath && (
                                        <option value={activeGptWeightOption.value}>{shortPath(activeGptWeightOption.relativePath)}</option>
                                      )}
                                      {voiceCandidates?.gpt_sovits.gpt_weights.map((item) => <option value={item.path} key={item.path}>{item.name}</option>)}
                                    </select>
                                  </label>
                                  <label className="resource-field">
                                    <span>{t("inspector.sovitsWeights")}</span>
                                    <select value={activeSovitsWeightOption.value} onChange={(event) => updateActiveBindingConfig({ sovits_weights_path: event.target.value || undefined })}>
                                      <option value="">{t("inspector.autoDefault")}</option>
                                      {activeSovitsWeightOption.relativePath && (
                                        <option value={activeSovitsWeightOption.value}>{shortPath(activeSovitsWeightOption.relativePath)}</option>
                                      )}
                                      {voiceCandidates?.gpt_sovits.sovits_weights.map((item) => <option value={item.path} key={item.path}>{item.name}</option>)}
                                    </select>
                                  </label>
                                </div>

                                <div className="gpt-resource-column">
                                  <div className="logs-reference-picker">
                                    <label className="resource-field">
                                      <span>{t("inspector.logsReferenceAudio")}</span>
                                      <select
                                        value={activeLogsReferenceOptionValue}
                                        disabled={activeLogsReferenceSamples.length === 0 && (!activeLogsReferenceRequest || loadingLogsReferenceKey === activeLogsReferenceRequest?.key)}
                                        onChange={(event) => {
                                          const sample = activeLogsReferenceSamples.find((item) => item.sample_id === event.target.value);
                                          if (sample) applyLogsReferenceSample(sample);
                                        }}
                                      >
                                        <option value="">{activeLogsReferenceSamples.length > 0 || activeLogsReferenceRequest ? t("status.unset") : t("inspector.logsReferenceNeedsLogs")}</option>
                                        {!activeLogsReferenceSample && activeReferenceAudioPath && (
                                          <option value={CATALOG_STAGED_REFERENCE_OPTION}>{activeReferenceAudioLabel}</option>
                                        )}
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
                  </section>
                  <section className="voice-config-section">
                    <h3>{t("inspector.resourcesAndRuntime")}</h3>
                    <VoiceAssetStatusPanel
                      catalog={voiceCatalog}
                      syncing={isSyncingVoiceCatalog}
                      error={voiceCatalogError}
                      onSync={() => void runVoiceCatalogSync()}
                    />
                    <QueuePanel
                      jobs={queueJobs}
                      activeJob={queueActiveJob}
                      queued={queueQueuedItems}
                      running={queueRunningItems}
                      completed={queueCompletedItems}
                      failed={queueFailedItems}
                      cancelled={queueCancelledItems}
                      total={queueTotalItems}
                      processed={queueProcessedItems}
                      progressPercent={queueProgressPercent}
                      statusLabel={queueVisibleStatusLabel}
                      statusTone={queueVisibleTone}
                      externalStatusLabel={(status) => statusText(status, t)}
                    />
                  </section>
                </VoiceConfigurationDrawer>

                <VoiceCandidatePanel
                  recommendation={activeVoiceRecommendation}
                  selection={activeLine.voice_selection}
                  loading={voiceRecommendationLoadingLineId === activeLine.id}
                  error={voiceRecommendationError}
                  selectingCandidateId={selectingVoiceCandidateId}
                  referenceAudioUrl={referenceAudioUrl}
                  onSelect={(candidateId) => void chooseVoiceCandidate(candidateId)}
                  onConfirmIdentity={(candidateId) => void confirmFuzzyVoiceIdentity(candidateId)}
                  onClear={() => void clearActiveVoiceSelection()}
                  onRefresh={refreshActiveVoiceRecommendation}
                  onSyncAssets={() => void runVoiceCatalogSync()}
                  onOpenServices={() => setIsVoiceConfigurationOpen(true)}
                />

                {activeProvider === "gpt-sovits" && (
                  <details className="inspector-card inspector-manual-reference-card">
                    <summary className="manual-reference-summary">
                      <span className="manual-reference-summary-copy">
                        <strong><Mic2 size={15} /> {t("inspector.manualReferenceSetup")}</strong>
                        <span>{t("inspector.manualReferenceSetupHint")}</span>
                      </span>
                      <ChevronDown className="manual-reference-chevron" size={16} />
                    </summary>

                    <div className="manual-reference-body">
                      <div className="manual-reference-weight-grid">
                        <label className="resource-field">
                          <span>{t("inspector.gptWeights")}</span>
                          <select value={activeGptWeightOption.value} onChange={(event) => updateActiveBindingConfig({ gpt_weights_path: event.target.value || undefined })}>
                            <option value="">{t("inspector.autoDefault")}</option>
                            {activeGptWeightOption.relativePath && (
                              <option value={activeGptWeightOption.value}>{shortPath(activeGptWeightOption.relativePath)}</option>
                            )}
                            {voiceCandidates?.gpt_sovits.gpt_weights.map((item) => <option value={item.path} key={item.path}>{item.name}</option>)}
                          </select>
                        </label>
                        <label className="resource-field">
                          <span>{t("inspector.sovitsWeights")}</span>
                          <select value={activeSovitsWeightOption.value} onChange={(event) => updateActiveBindingConfig({ sovits_weights_path: event.target.value || undefined })}>
                            <option value="">{t("inspector.autoDefault")}</option>
                            {activeSovitsWeightOption.relativePath && (
                              <option value={activeSovitsWeightOption.value}>{shortPath(activeSovitsWeightOption.relativePath)}</option>
                            )}
                            {voiceCandidates?.gpt_sovits.sovits_weights.map((item) => <option value={item.path} key={item.path}>{item.name}</option>)}
                          </select>
                        </label>
                      </div>

                      <div className="logs-reference-picker">
                        <label className="resource-field">
                          <span>{t("inspector.roleReferenceAudio", { role: activeLineCharacter?.name ?? t("status.unassigned") })}</span>
                          <select
                            value={activeLogsReferenceOptionValue}
                            disabled={activeLogsReferenceSamples.length === 0 && (!activeLogsReferenceRequest || loadingLogsReferenceKey === activeLogsReferenceRequest?.key)}
                            onChange={(event) => {
                              const sample = activeLogsReferenceSamples.find((item) => item.sample_id === event.target.value);
                              if (sample) applyLogsReferenceSample(sample);
                            }}
                          >
                            <option value="">{activeLogsReferenceSamples.length > 0 || activeLogsReferenceRequest ? t("status.unset") : t("inspector.logsReferenceNeedsLogs")}</option>
                            {!activeLogsReferenceSample && activeReferenceAudioPath && (
                              <option value={CATALOG_STAGED_REFERENCE_OPTION}>{activeReferenceAudioLabel}</option>
                            )}
                            {activeLogsReferenceSamples.map((sample) => (
                              <option value={sample.sample_id} key={sample.sample_id}>{sample.display_label}</option>
                            ))}
                          </select>
                        </label>
                        <button
                          className="icon-button"
                          type="button"
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

                      {activeLogsReferenceSample && (
                        <div className="manual-reference-source">
                          <span>{t("inspector.textSource")}: {activeLogsReferenceSample.text_source || t("status.unset")}</span>
                          <strong>{activeLogsReferenceSample.text || t("inspector.emptyPromptText")}</strong>
                        </div>
                      )}

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
                      <WaveformPlayer
                        audioPath={activePlayableVersion.audio_path}
                        label={activePlayableVersion.version_id}
                        compact
                        downloadLabel={t("actions.downloadAudio")}
                        downloadName={fileNameFromPath(activePlayableVersion.audio_path, `${activePlayableVersion.version_id}.wav`)}
                      />
                    </div>
                  )}
                  <div className="generate-dock-actions">
                    <button className="primary-button inspector-generate-button" onClick={() => void runInspectorGeneration()} disabled={activeLineGenerationBusy || (!activeVersionDraft && !activeBinding)}>
                      {activeLineGenerationBusy ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
                      {activeVersionDraft ? t("inspector.generateFromVersion") : activeSummary.tone === "completed" ? t("actions.regenerate") : t("inspector.generateLine")}
                    </button>
                  </div>
                </section>
              </div>
            )}
            {!activeLine && (
              <div className={`empty-row inspector-empty-state ${lineWorkbenchState.emptyState ? "quiet" : ""}`}>
                <strong>{lineWorkbenchState.emptyState ? t("empty.inspectorIdle") : t("empty.noActiveLine")}</strong>
                {!lineWorkbenchState.emptyState && <span>{t("empty.noActiveLineHint")}</span>}
              </div>
            )}
            </VoiceInspector>
          )}
        />
            </>
          )}
        />
      </WorkbenchShell>
    </>
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
    roleLibraryController.mapProjectRole(activeProjectCharacter.project_character_id, character.id);
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
    const canLinkToProject = Boolean(
      currentProjectId
      && activeProjectCharacter
      && roleCandidateHasCompleteTrainingTask(candidate)
    );
    const confirmed = await requestConfirmation({
      title: t("characters.importCandidateTitle"),
      body: canLinkToProject
        ? t("characters.importCandidateAndLinkBody", {
            candidate: candidate.name,
            role: activeProjectCharacter?.name ?? ""
          })
        : t("characters.importCandidateOnlyBody", { candidate: candidate.name }),
      detail: canLinkToProject
        ? t("characters.importCandidateAndLinkDetail", {
            logs: candidate.logs_name ?? candidate.name,
            role: activeProjectCharacter?.name ?? ""
          })
        : t("characters.importCandidateOnlyDetail", {
            logs: candidate.logs_name ?? candidate.name
          }),
      confirmLabel: canLinkToProject ? t("characters.importAndLinkCandidate") : t("characters.importCandidate"),
      cancelLabel: t("actions.cancel"),
      tone: "info"
    });
    if (!confirmed) return;
    setNotice(t("notice.importingRole"));
    try {
      if (currentProjectId) await flushPendingProjectAutosave(currentProjectId);
      const projectTarget = canLinkToProject && currentProjectId && activeProjectCharacter
        ? { projectId: currentProjectId, projectCharacterId: activeProjectCharacter.project_character_id }
        : undefined;
      const payload = await importRoleLibraryCandidate(candidate, projectTarget);
      const nextCharacters = [
        ...charactersRef.current.filter((character) => character.id !== payload.character.id),
        payload.character
      ];
      setCharacters(nextCharacters);
      setRoleLibraryCandidates((current) => current.filter((item) => item.id !== candidate.id));
      setActiveRoleCandidateId(null);
      setActiveLibraryCharacterId(payload.character.id);
      if (payload.project_character && currentProjectId) {
        const targetLineIds = projectRef.current.lines
          .filter((line) => line.character_id === payload.project_character?.project_character_id)
          .map((line) => line.id);
        if (targetLineIds.length > 0) {
          const recommendationPayload = await recommendVoices(currentProjectId, targetLineIds, { applyAutomatic: true });
          setVoiceRecommendations((current) => ({
            ...current,
            ...Object.fromEntries(
              recommendationPayload.recommendations.map((recommendation) => [recommendation.line_id, recommendation])
            )
          }));
        }
        const authoritativeProject = await fetchProject(currentProjectId);
        markWorkspaceAuthoritative(currentProjectId, authoritativeProject, nextCharacters);
        setProject(authoritativeProject);
        setNotice(t("notice.roleImportedAndMatched", { role: payload.project_character.name }));
      } else {
        setNotice(t("notice.roleImported"));
      }
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
    updateActiveBindingConfig(applyLogsReferenceSampleToConfig(activeBindingConfig, sample, { serviceId: activeLogsReferenceServiceId }));
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

function roleCandidateHasCompleteTrainingTask(candidate: RoleLibraryCandidate): boolean {
  const hasReference = Boolean(
    candidate.recommended_ref_audio_path
    || candidate.reference_audio_groups?.some((group) => (group.samples?.length ?? 0) > 0)
  );
  return Boolean(
    candidate.logs_name
    && candidate.recommended_gpt_weights_path
    && candidate.recommended_sovits_weights_path
    && hasReference
  );
}

function suggestedProjectRoleId(
  candidate: RoleLibraryCandidate,
  projectCharacters: ProjectCharacter[]
): string | null {
  const candidateTokens = [candidate.name, ...(candidate.aliases ?? [])]
    .map(normalizeRoleToken)
    .filter(Boolean);
  let best: { id: string; score: number } | null = null;
  for (const projectCharacter of projectCharacters) {
    const roleToken = normalizeRoleToken(projectCharacter.name);
    if (!roleToken) continue;
    const score = candidateTokens.reduce((current, token) => {
      if (token === roleToken) return Math.max(current, 100);
      if (roleToken.length >= 2 && token.includes(roleToken)) return Math.max(current, 80);
      if (token.length >= 2 && roleToken.includes(token)) return Math.max(current, 70);
      return current;
    }, 0);
    if (score > 0 && (!best || score > best.score)) {
      best = { id: projectCharacter.project_character_id, score };
    }
  }
  return best?.id ?? null;
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

function fileNameFromPath(value: string, fallback: string): string {
  return value.split(/[\\/]/).filter(Boolean).at(-1)?.trim() || fallback;
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
  catalog: VoiceCatalogPublicView | null,
  manifest: GenerationManifest,
  t: Translate
): Array<{ id: "mode" | "services" | "resources" | "generation"; label: string; state: "ready" | "attention" | "done" }> {
  const localCoverage = coreProviderCoverage(services);
  const localReady = localCoverage.filter((item) => item.operational).length;
  const completed = Object.values(manifest.lines)
    .flatMap((history) => history.versions)
    .filter((version) => version.status === "completed" && coreLocalProviders.has(version.provider_type ?? version.engine)).length;
  const resourcesReady = voiceCatalogReady(catalog);
  return [
    { id: "mode", label: statusText(runtime?.service_mode ?? "real", t), state: runtime?.service_mode === "real" ? "done" : "attention" },
    { id: "services", label: `${localReady}/${localCoverage.length}`, state: localReady === localCoverage.length ? "done" : "attention" },
    { id: "resources", label: resourcesReady ? t("status.ready") : t("status.needsMapping"), state: resourcesReady ? "done" : "attention" },
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
    cancelling: "status.cancelling",
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
            <StatusPill tone={generationStatusTone(group.latestStatus)} label={t(generationStatusKey(group.latestStatus))} />
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
                    <span>{t(generationStatusKey(player.status))}</span>
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
                  <WaveformPlayer
                    audioPath={player.audioPath}
                    label={`${t("history.waveformLabel")} ${player.versionId}`}
                    downloadLabel={t("actions.downloadAudio")}
                    downloadName={fileNameFromPath(player.audioPath, `${player.versionId}.wav`)}
                  />
                ) : version.error ? (
                  <FailureHistoryMessage version={version} t={t} />
                ) : (
                  <p className="history-version-empty">{t(generationStatusKey(player.status))}</p>
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

function lineWorkbenchEmptyTitle(state: NonNullable<ReturnType<typeof lineWorkbenchControlsState>["emptyState"]>, t: Translate): string {
  if (state === "no_project") return t("empty.noProjectSelected");
  if (state === "no_lines") return t("empty.noExtractedLines");
  return t("empty.noLines");
}

function lineWorkbenchEmptyHint(state: NonNullable<ReturnType<typeof lineWorkbenchControlsState>["emptyState"]>, t: Translate): string {
  if (state === "no_project") return t("empty.noProjectAction");
  if (state === "no_lines") return t("empty.noExtractedLinesHint");
  return t("empty.noMatchingLinesHint");
}

function ReferencePreview({ groups, t }: { groups: CharacterReferenceAudioGroup[]; t: Translate }) {
  const preview = roleLibraryReferencePreview(groups);
  return (
    <div className="role-detail-card reference-preview-card">
      <span>{t("characters.referenceAudio")}</span>
      {preview.visibleSamples.length > 0 ? (
        <div className="reference-preview-list">
          {preview.visibleSamples.map((sample) => (
            <div className="reference-preview-row" key={`${sample.path}-${sample.group}`}>
              <div>
                <strong>{shortPath(sample.path)}</strong>
                <small>{sample.text || sample.group}</small>
              </div>
              {isLocalAudioAsset(sample.path) && <WaveformPlayer audioPath={sample.path} label={shortPath(sample.path)} />}
            </div>
          ))}
          {preview.hasOverflow && <small>{t("characters.moreReferenceAudio", { count: preview.hiddenSampleCount })}</small>}
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

function StatusPill({ tone, label }: { tone: GenerationStatusTone; label: string }) {
  return <span className={`status-pill ${tone}`}>{label}</span>;
}
