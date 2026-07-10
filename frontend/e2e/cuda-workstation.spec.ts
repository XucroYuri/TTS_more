import { existsSync, readFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";

import { expect, test, type APIRequestContext } from "@playwright/test";

const FORMAL_SERVICE_IDS = [
  "local-gpt-sovits-main",
  "local-indextts",
  "local-cosyvoice"
] as const;
const PROJECT_ID = process.env.TTS_MORE_CUDA_E2E_PROJECT_ID ?? "cuda-e2e-validation";
const MIXED_QUEUE_SIZE = 30;
const LINES_PER_SERVICE = MIXED_QUEUE_SIZE / FORMAL_SERVICE_IDS.length;
const AUTH_HEADERS = process.env.TTS_MORE_API_TOKEN
  ? { Authorization: `Bearer ${process.env.TTS_MORE_API_TOKEN}` }
  : {};

interface ValidationFixture {
  references: {
    gpt_sovits: string;
    indextts: string;
    cosyvoice: string;
  };
  gpt_weights: {
    v2ProPlus: { gpt: string; sovits: string };
  };
  prompts: {
    gpt: { text: string; language: string };
    cosyvoice: { text: string; language: string };
    index_emotion: string;
  };
  test_texts: {
    gpt_v2ProPlus: string;
    index_emotion: string;
    cosyvoice_zero_shot: string;
  };
}

interface ValidationLine {
  id: string;
  line_uid?: string;
  text: string;
  character_id: string;
  temporary_binding: Record<string, unknown>;
}

interface ValidationProject {
  title: string;
  default_language: string;
  project_characters: Array<Record<string, unknown>>;
  lines: ValidationLine[];
}

interface JsonResponse {
  ok(): boolean;
  status(): number;
  url(): string;
  text(): Promise<string>;
  json(): Promise<unknown>;
}

test.skip(process.env.TTS_MORE_RUN_CUDA_E2E !== "1", "Set TTS_MORE_RUN_CUDA_E2E=1 on a configured CUDA workstation");

test("imports a CUDA validation project and completes a 30-item mixed queue across all formal services", async ({ page, request }) => {
  const fixture = loadFixture();
  const project = buildValidationProject(fixture);

  await resetValidationProject(request, project);
  await waitForFormalServices(request);

  await page.addInitScript(
    ({ projectId, token }) => {
      window.localStorage.setItem("tts-more.currentProjectId", projectId);
      window.localStorage.setItem("i18nextLng", "en-US");
      if (token) window.localStorage.setItem("tts_more_token", token);
    },
    { projectId: PROJECT_ID, token: process.env.TTS_MORE_API_TOKEN ?? "" }
  );
  await page.goto("/");

  await expect(page.getByRole("option", { name: /CUDA E2E Validation/ })).toHaveAttribute("aria-selected", "true");
  for (const line of project.lines) {
    await expect(page.getByText(line.text, { exact: true })).toBeVisible();
  }

  const submission = page.waitForResponse(
    (response) => response.url().includes("/api/jobs/generation") && response.request().method() === "POST",
    { timeout: 10 * 60 * 1000 }
  );
  const generateButton = page.getByRole("button", { name: "Generate filtered lines" });
  await expect(generateButton).toBeEnabled();
  await generateButton.click();

  const submissionResponse = await submission;
  const submittedJob = await responseJson<{ job_id: string }>(submissionResponse);
  let maxSimultaneouslyLoaded = 0;
  await expect.poll(
    async () => {
      const serviceStatus = await apiJson<{
        services: Array<{ service_id?: string; loaded_signature?: string | null }>;
      }>(request, "/api/services/status");
      const loadedCount = serviceStatus.services.filter(
        (service) => FORMAL_SERVICE_IDS.includes(service.service_id as (typeof FORMAL_SERVICE_IDS)[number]) && Boolean(service.loaded_signature)
      ).length;
      maxSimultaneouslyLoaded = Math.max(maxSimultaneouslyLoaded, loadedCount);
      const response = await request.get(`/api/jobs/${encodeURIComponent(submittedJob.job_id)}`, { headers: AUTH_HEADERS });
      const job = await responseJson<{ status: string; error?: string | null }>(response);
      if (job.status === "failed" || job.status === "cancelled") {
        throw new Error(`CUDA generation job ${job.status}: ${job.error ?? "no job error"}`);
      }
      return job.status;
    },
    { timeout: 40 * 60 * 1000, intervals: [1_000, 2_000, 5_000] }
  ).toBe("completed");

  if (process.env.TTS_MORE_CUDA_VALIDATION_MODE === "distributed") {
    expect(maxSimultaneouslyLoaded).toBeGreaterThanOrEqual(2);
  } else {
    expect(maxSimultaneouslyLoaded).toBeLessThanOrEqual(1);
  }

  const storedProject = await apiJson<{ lines: ValidationLine[] }>(request, `/api/projects/${PROJECT_ID}`);
  const manifest = await apiJson<{
    lines: Record<string, { versions: Array<{ status: string; service_id?: string | null; audio_path?: string | null }> }>;
  }>(request, `/api/projects/${PROJECT_ID}/manifest`);
  const latest = storedProject.lines.map((line) => {
    const history = manifest.lines[line.line_uid ?? line.id] ?? manifest.lines[line.id];
    expect(history, `missing history for ${line.id}`).toBeTruthy();
    return history.versions.at(-1);
  });

  expect(latest).toHaveLength(MIXED_QUEUE_SIZE);
  for (const serviceId of FORMAL_SERVICE_IDS) {
    expect(latest.filter((version) => version?.service_id === serviceId)).toHaveLength(LINES_PER_SERVICE);
  }
  expect(latest.every((version) => version?.status === "completed" && Boolean(version.audio_path))).toBe(true);

  const representativeVersions = FORMAL_SERVICE_IDS.map((serviceId) => latest.find((version) => version?.service_id === serviceId));
  expect(representativeVersions).toHaveLength(3);

  await page.reload();
  await expect(page.locator('article.line-card[data-queue-state="completed"]')).toHaveCount(MIXED_QUEUE_SIZE);
  for (const line of project.lines.slice(0, 3)) {
    const lineCard = page.getByText(line.text, { exact: true }).locator("xpath=ancestor::article[contains(@class, 'line-card')]");
    await lineCard.click();
    await expect(lineCard.getByLabel(/Play version/)).toBeVisible();
  }

  for (const version of representativeVersions) {
    const response = await request.get(`/api/audio?path=${encodeURIComponent(version?.audio_path ?? "")}`, { headers: AUTH_HEADERS });
    expect(response.ok(), `audio request failed with ${response.status()}`).toBe(true);
    expect(response.headers()["content-type"]?.toLowerCase().startsWith("audio/")).toBe(true);
    const audio = await response.body();
    expect(audio.byteLength).toBeGreaterThan(1024);
    expect(audio.byteLength > 1024).toBe(true);
  }
});

function loadFixture(): ValidationFixture {
  const rawPath = process.env.TTS_MORE_CUDA_FIXTURE;
  if (!rawPath) throw new Error("TTS_MORE_CUDA_FIXTURE must point to the local validation fixture");
  const fixturePath = resolveFixturePath(rawPath);
  return JSON.parse(readFileSync(fixturePath, "utf8")) as ValidationFixture;
}

function resolveFixturePath(rawPath: string): string {
  if (isAbsolute(rawPath)) return rawPath;
  const candidates = [resolve(process.cwd(), rawPath), resolve(process.cwd(), "..", rawPath)];
  const match = candidates.find(existsSync);
  if (!match) throw new Error(`validation fixture not found: ${rawPath}`);
  return match;
}

function buildValidationProject(fixture: ValidationFixture): ValidationProject {
  const bindings = [
    binding("gpt-sovits", FORMAL_SERVICE_IDS[0], {
      gpt_weights_path: fixture.gpt_weights.v2ProPlus.gpt,
      sovits_weights_path: fixture.gpt_weights.v2ProPlus.sovits,
      ref_audio_path: fixture.references.gpt_sovits,
      prompt_text: fixture.prompts.gpt.text,
      prompt_lang: fixture.prompts.gpt.language,
      text_lang: "zh",
      media_type: "wav"
    }),
    binding("indextts", FORMAL_SERVICE_IDS[1], {
      voice: fixture.references.indextts,
      emotion_mode: "emotion_text",
      emotion_text: fixture.prompts.index_emotion
    }),
    binding("cosyvoice", FORMAL_SERVICE_IDS[2], {
      mode: "zero_shot",
      prompt_audio_path: fixture.references.cosyvoice,
      prompt_text: fixture.prompts.cosyvoice.text
    })
  ];
  const texts = [
    fixture.test_texts.gpt_v2ProPlus,
    fixture.test_texts.index_emotion,
    fixture.test_texts.cosyvoice_zero_shot
  ];

  const lines: ValidationLine[] = [];
  for (let round = 0; round < LINES_PER_SERVICE; round += 1) {
    for (let index = 0; index < FORMAL_SERVICE_IDS.length; index += 1) {
      lines.push({
        id: `cuda-line-${index + 1}-${round + 1}`,
        character_id: `cuda-role-${index + 1}`,
        text: `${texts[index]} ${round + 1}`,
        note: "CUDA closed-loop validation",
        language: index === 2 ? "zh" : "zh",
        service_override: FORMAL_SERVICE_IDS[index],
        temporary_binding: bindings[index]
      } as ValidationLine);
    }
  }

  return {
    title: "CUDA E2E Validation",
    default_language: "zh",
    project_characters: bindings.map((projectBinding, index) => ({
      project_character_id: `cuda-role-${index + 1}`,
      name: `CUDA Role ${index + 1}`,
      library_character_id: null,
      mode: "reference",
      project_binding: projectBinding,
      match_status: "manual"
    })),
    lines
  };
}

function binding(providerType: string, serviceId: string, config: Record<string, unknown>): Record<string, unknown> {
  return {
    binding_id: `cuda-${providerType}`,
    provider_type: providerType,
    service_id: serviceId,
    fallback_services: [],
    capabilities: [],
    config
  };
}

async function resetValidationProject(request: APIRequestContext, project: ValidationProject): Promise<void> {
  const existing = await request.get(`/api/projects/${PROJECT_ID}`, { headers: AUTH_HEADERS });
  if (existing.ok()) {
    const deleted = await request.delete(`/api/projects/${PROJECT_ID}`, { headers: AUTH_HEADERS });
    if (!deleted.ok()) throw new Error(`failed to reset validation project: ${deleted.status()} ${await deleted.text()}`);
  } else if (existing.status() !== 404) {
    throw new Error(`failed to inspect validation project: ${existing.status()} ${await existing.text()}`);
  }
  const created = await request.put(`/api/projects/${PROJECT_ID}`, {
    headers: { ...AUTH_HEADERS, "Content-Type": "application/json" },
    data: project
  });
  await responseJson(created);
}

async function waitForFormalServices(request: APIRequestContext): Promise<void> {
  await expect.poll(
    async () => {
      const payload = await apiJson<{ services: Array<{ service_id?: string; ready: boolean }> }>(request, "/api/services/status");
      return FORMAL_SERVICE_IDS.filter((serviceId) => payload.services.some((service) => service.service_id === serviceId && service.ready));
    },
    { timeout: 10 * 60 * 1000, intervals: [1_000, 2_000, 5_000] }
  ).toEqual([...FORMAL_SERVICE_IDS]);
}

async function apiJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(path, { headers: AUTH_HEADERS });
  return responseJson<T>(response);
}

async function responseJson<T = unknown>(response: JsonResponse): Promise<T> {
  if (!response.ok()) throw new Error(`${response.url()} failed: ${response.status()} ${await response.text()}`);
  return response.json() as Promise<T>;
}
