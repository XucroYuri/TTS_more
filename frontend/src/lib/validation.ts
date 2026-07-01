import type { GenerationManifest, WorkerHealth } from "../types";
import { coreLocalProviders, isServiceOperational } from "./workstation";

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
  const localServices = services.filter((service) => coreLocalProviders.has(service.provider_type ?? service.engine));
  const completedVersions = Object.values(manifest?.lines ?? {}).flatMap((history) => history.versions).filter((version) => version.status === "completed");
  return [
    { id: "mode", label: runtime?.service_mode === "real" ? "Real service mode" : "Mock mode", state: runtime?.service_mode === "real" ? "done" : "attention" },
    { id: "services", label: `${localServices.filter(isServiceOperational).length}/${localServices.length} local services ready`, state: localServices.length > 0 && localServices.every(isServiceOperational) ? "done" : "attention" },
    { id: "resources", label: candidates?.ready ? "Voice resources ready" : "Voice resources need mapping", state: candidates?.ready ? "done" : "attention" },
    { id: "generation", label: `${completedVersions.length} completed validation versions`, state: completedVersions.length >= coreLocalProviders.size ? "done" : "ready" }
  ];
}
