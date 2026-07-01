import { describe, expect, it } from "vitest";

import { defaultLanguage, languageOptions, nextLanguage, normalizeLanguage, resources, tText } from "./i18n";

describe("i18n configuration", () => {
  it("defaults to Simplified Chinese", () => {
    expect(defaultLanguage).toBe("zh-CN");
  });

  it("normalizes English system locales and falls back to Chinese", () => {
    expect(normalizeLanguage("en-US")).toBe("en-US");
    expect(normalizeLanguage("en-GB")).toBe("en-US");
    expect(normalizeLanguage("zh-TW")).toBe("zh-CN");
    expect(normalizeLanguage("fr-FR")).toBe("zh-CN");
  });

  it("ships complete language options and core workstation labels", () => {
    expect(languageOptions).toEqual([
      { value: "zh-CN", label: "中文" },
      { value: "en-US", label: "English" }
    ]);
    expect(tText(resources["zh-CN"], "app.title")).toBe("TTS More");
    expect(tText(resources["zh-CN"], "validation.run")).toBe("运行核心模型检查");
    expect(tText(resources["en-US"], "validation.run")).toBe("Run core-model check");
  });

  it("cycles between supported languages for the compact topbar toggle", () => {
    expect(nextLanguage("zh-CN")).toBe("en-US");
    expect(nextLanguage("en-US")).toBe("zh-CN");
    expect(nextLanguage("fr-FR")).toBe("en-US");
  });
});
