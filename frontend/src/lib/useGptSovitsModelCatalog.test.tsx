// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GPTSoVITSModelCatalogResponse } from "../types";
import { useGptSovitsModelCatalog } from "./useGptSovitsModelCatalog";

function deferred() {
  let resolve!: (value: GPTSoVITSModelCatalogResponse) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<GPTSoVITSModelCatalogResponse>((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

describe("current-service GPT experiment catalog", () => {
  const container = document.createElement("div");
  let root: ReturnType<typeof createRoot>;
  afterEach(async () => { if (root) await act(async () => root.unmount()); });

  it("loads without a library scan and rejects old or cross-service responses", async () => {
    const first = deferred(), second = deferred();
    const load = vi.fn().mockImplementation((id: string) => id === "gpt-a" ? first.promise : second.promise);
    function Catalog({serviceId}: {serviceId: string | null}) {
      const models = useGptSovitsModelCatalog(serviceId, load);
      return <div>{models.map((model) => model.name).join(",")}</div>;
    }
    root = createRoot(container);
    await act(async () => root.render(<Catalog serviceId="gpt-a" />));
    expect(load).toHaveBeenCalledWith("gpt-a", 120);
    await act(async () => root.render(<Catalog serviceId="gpt-b" />));
    expect(container.textContent).toBe("");
    await act(async () => second.resolve({models: [{id:"b", name:"B", service_id:"gpt-b"}, {id:"wrong",name:"Wrong service",service_id:"gpt-a"}]}));
    expect(container.textContent).toBe("B");
    await act(async () => first.resolve({models: [{id:"a", name:"A", service_id:"gpt-a"}]}));
    expect(container.textContent).toBe("B");
    await act(async () => root.render(<Catalog serviceId={null} />));
    expect(container.textContent).toBe("");
  });

  it("clears another service's choices on load failure and scopes legacy entries to the requested service", async () => {
    const load = vi.fn().mockResolvedValueOnce({models: [{id:"a",name:"A"}]}).mockRejectedValueOnce(new Error("offline"));
    function Catalog({serviceId}: {serviceId: string}) {
      const models = useGptSovitsModelCatalog(serviceId, load);
      return <div>{models.map((model) => `${model.service_id}:${model.name}`).join(",")}</div>;
    }
    root = createRoot(container);
    await act(async () => root.render(<Catalog serviceId="gpt-a" />));
    expect(container.textContent).toBe("gpt-a:A");
    await act(async () => root.render(<Catalog serviceId="gpt-b" />));
    expect(container.textContent).toBe("");
  });
});
