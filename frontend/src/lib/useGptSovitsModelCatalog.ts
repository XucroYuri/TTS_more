import { useEffect, useState } from "react";
import { fetchGptSovitsModelCatalog } from "../api";
import type { GPTSoVITSModelCatalogResponse, RoleLibraryCandidate } from "../types";

type CatalogLoader = (serviceId: string, limit: number) => Promise<GPTSoVITSModelCatalogResponse>;

export function useGptSovitsModelCatalog(serviceId: string | null, load: CatalogLoader = fetchGptSovitsModelCatalog): RoleLibraryCandidate[] {
  const [result, setResult] = useState<{ serviceId: string; models: RoleLibraryCandidate[] } | null>(null);
  useEffect(() => {
    if (!serviceId) return;
    let current = true;
    load(serviceId, 120)
      .then((payload) => {
        if (!current) return;
        const models = payload.models
          .filter((model) => !model.service_id || model.service_id === serviceId)
          .map((model) => ({ ...model, service_id: serviceId }));
        setResult({ serviceId, models });
      })
      .catch(() => {
        if (current) setResult({ serviceId, models: [] });
      });
    return () => { current = false; };
  }, [serviceId, load]);
  return serviceId && result?.serviceId === serviceId ? result.models : [];
}
