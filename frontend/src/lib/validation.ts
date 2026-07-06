import type { GenerationManifest, WorkerHealth } from "../types";
import { coreLocalProviders, coreProviderCoverage } from "./workstation";

export interface ValidationStep {
  id: "mode" | "services" | "resources" | "generation";
  label: string;
  state: "ready" | "attention" | "done";
}

export interface RuntimeMode {
  service_mode: string;
}

export interface VoiceCandidates {
  ready: boolean;
}

export function validationSteps(
  runtime: RuntimeMode | null,
  services: WorkerHealth[],
  candidates: VoiceCandidates | null,
  manifest: GenerationManifest | null
): ValidationStep[] {
  const localCoverage = coreProviderCoverage(services);
  const localReady = localCoverage.filter((item) => item.operational).length;
  const completedVersions = Object.values(manifest?.lines ?? {}).flatMap((history) => history.versions).filter((version) => version.status === "completed");
  return [
    { id: "mode", label: runtime?.service_mode === "real" ? "Real service mode" : "Mock mode", state: runtime?.service_mode === "real" ? "done" : "attention" },
    { id: "services", label: `${localReady}/${localCoverage.length} core TTS providers ready`, state: localReady === localCoverage.length ? "done" : "attention" },
    { id: "resources", label: candidates?.ready ? "Voice resources ready" : "Voice resources need mapping", state: candidates?.ready ? "done" : "attention" },
    { id: "generation", label: `${completedVersions.length} completed validation versions`, state: completedVersions.length >= coreLocalProviders.size ? "done" : "ready" }
  ];
}
