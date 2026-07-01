import { describe, expect, it } from "vitest";

import { validationSteps } from "./validation";

describe("validation wizard helpers", () => {
  it("marks the workflow ready when real mode, core services, resources, and core outputs are present", () => {
    const steps = validationSteps(
      { service_mode: "real" },
      [
        { service_id: "local-gpt", engine: "gpt-sovits", provider_type: "gpt-sovits", ready: true, base_url: "http://127.0.0.1:9880", supervisor: { service_id: "local-gpt", manageable: true, running: true } },
        { service_id: "local-index", engine: "indextts", provider_type: "indextts", ready: true, base_url: "http://127.0.0.1:9881", supervisor: { service_id: "local-index", manageable: true, running: true } }
      ],
      { ready: true },
      {
        project_id: "validation",
        lines: {
          a: { line_id: "a", versions: [{ version_id: "v001", engine: "gpt-sovits", profile: "a", status: "completed", audio_path: "a.wav", created_at: "now" }] },
          b: { line_id: "b", versions: [{ version_id: "v001", engine: "indextts", profile: "b", status: "completed", audio_path: "b.wav", created_at: "now" }] }
        }
      }
    );

    expect(steps.map((step) => step.state)).toEqual(["done", "done", "done", "done"]);
  });

  it("surfaces mock mode and missing services as attention states", () => {
    const steps = validationSteps(
      { service_mode: "mock" },
      [{ service_id: "local-gpt", engine: "gpt-sovits", provider_type: "gpt-sovits", ready: false }],
      { ready: false },
      null
    );

    expect(steps[0]).toMatchObject({ state: "attention", label: "Mock mode" });
    expect(steps[1].state).toBe("attention");
    expect(steps[2].state).toBe("attention");
  });
});
