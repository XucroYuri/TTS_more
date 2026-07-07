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

  it("keeps production workstation labels fully localized", () => {
    expect(tText(resources["zh-CN"], "topbar.roleLibrary")).toBe("角色");
    expect(tText(resources["zh-CN"], "topbar.llmConfig")).toBe("解析");
    expect(tText(resources["zh-CN"], "services.llmApiTitle")).toBe("剧本解析");
    expect(tText(resources["zh-CN"], "parser.advancedConfig")).toBe("其他解析服务");
    expect(tText(resources["zh-CN"], "services.openSourceDetectAndSave")).toBe("检测并保存");
    expect(tText(resources["zh-CN"], "services.openSourceDetectNotSaved")).toBe("检测完成：{{state}}，未保存");
    expect(tText(resources["zh-CN"], "inspector.diagnosticsReadyShort")).toBe("API 正常");
    expect(tText(resources["zh-CN"], "inspector.title")).toBe("台词检查器");
    expect(tText(resources["zh-CN"], "inspector.provider")).toBe("服务商");
    expect(tText(resources["zh-CN"], "inspector.voiceBinding")).toBe("音色绑定");
    expect(tText(resources["zh-CN"], "characters.uploadAvatar")).toBe("上传头像");
    expect(tText(resources["zh-CN"], "audioInput.record")).toBe("录音");
    expect(tText(resources["zh-CN"], "script.drawer.list")).toBe("剧本列表");
    expect(tText(resources["en-US"], "topbar.roleLibrary")).toBe("Roles");
    expect(tText(resources["en-US"], "topbar.ttsConfig")).toBe("Setup");
    expect(tText(resources["en-US"], "services.llmApiTitle")).toBe("Script parser");
    expect(tText(resources["en-US"], "parser.advancedConfig")).toBe("Other parser services");
    expect(tText(resources["en-US"], "services.openSourceDetectAndSave")).toBe("Detect and save");
    expect(tText(resources["en-US"], "services.openSourceDetectNotSaved")).toBe("Detection complete: {{state}}; not saved");
    expect(tText(resources["en-US"], "inspector.diagnosticsReadyShort")).toBe("API ready");
    expect(tText(resources["en-US"], "inspector.title")).toBe("Line Inspector");
    expect(tText(resources["en-US"], "characters.uploadAvatar")).toBe("Upload avatar");
    expect(tText(resources["en-US"], "audioInput.record")).toBe("Record");
    expect(tText(resources["en-US"], "script.drawer.preview")).toBe("Preview");
  });
});
