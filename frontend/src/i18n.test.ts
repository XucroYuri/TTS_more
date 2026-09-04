import { describe, expect, it } from "vitest";

import { defaultLanguage, languageOptions, nextLanguage, normalizeLanguage, resources, tText } from "./i18n";

function leafKeyPaths(value: unknown, prefix = ""): string[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [prefix];
  return Object.entries(value as Record<string, unknown>)
    .flatMap(([key, child]) => leafKeyPaths(child, prefix ? `${prefix}.${key}` : key))
    .sort();
}

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
    expect(tText(resources["zh-CN"], "status.cancelling")).toBe("取消中");
    expect(tText(resources["zh-CN"], "status.cancelled")).toBe("已取消");
    expect(tText(resources["en-US"], "status.cancelling")).toBe("Cancelling");
    expect(tText(resources["en-US"], "status.cancelled")).toBe("Cancelled");
  });

  it("cycles between supported languages for the compact topbar toggle", () => {
    expect(nextLanguage("zh-CN")).toBe("en-US");
    expect(nextLanguage("en-US")).toBe("zh-CN");
    expect(nextLanguage("fr-FR")).toBe("en-US");
  });

  it("keeps production workstation labels fully localized", () => {
    expect(tText(resources["zh-CN"], "topbar.llmConfig")).toBe("解析");
    expect(tText(resources["zh-CN"], "characters.libraryManager")).toBe("角色库");
    expect(tText(resources["zh-CN"], "characters.bindToProjectRole")).toBe("用于当前项目");
    expect(tText(resources["zh-CN"], "services.ttsAccessTitle")).toBe("添加 TTS 服务");
    expect(tText(resources["zh-CN"], "services.openSourceDetect")).toBe("检测连接");
    expect(tText(resources["zh-CN"], "services.openSourceBaseUrl")).toBe("ComfyUI 地址");
    expect(tText(resources["zh-CN"], "services.llmApiTitle")).toBe("剧本解析");
    expect(tText(resources["zh-CN"], "parser.advancedConfig")).toBe("高级服务");
    expect(tText(resources["zh-CN"], "parser.activateKwjm")).toBe("保存并检查");
    expect(tText(resources["zh-CN"], "services.openSourceDetectAndSave")).toBe("检测并保存");
    expect(tText(resources["zh-CN"], "services.openSourceDetectNotSaved")).toBe("检测完成：{{state}}，未保存");
    expect(tText(resources["zh-CN"], "inspector.diagnosticsReadyShort")).toBe("API 正常");
    expect(tText(resources["zh-CN"], "inspector.title")).toBe("台词检查器");
    expect(tText(resources["zh-CN"], "inspector.provider")).toBe("服务商");
    expect(tText(resources["zh-CN"], "inspector.voiceBinding")).toBe("音色绑定");
    expect(tText(resources["zh-CN"], "characters.uploadAvatar")).toBe("上传头像");
    expect(tText(resources["zh-CN"], "audioInput.record")).toBe("录音");
    expect(tText(resources["zh-CN"], "script.drawer.list")).toBe("剧本列表");
    expect(tText(resources["zh-CN"], "script.workspaceTitle")).toBe("剧本");
    expect(tText(resources["zh-CN"], "script.workspaceHint")).toBe("选择剧本，或直接在下方新建。");
    expect(tText(resources["zh-CN"], "script.analyze")).toBe("开始分析");
    expect(tText(resources["zh-CN"], "app.reviewConfirmedAnnotations")).toBe("返回分析结果");
    expect(tText(resources["zh-CN"], "inspector.voiceConfiguration")).toBe("配音配置");
    expect(tText(resources["zh-CN"], "inspector.method.gpt")).toBe("GPT");
    expect(tText(resources["zh-CN"], "inspector.createIndexTemporary")).toBe("设为临时音色");
    expect(tText(resources["en-US"], "characters.libraryManager")).toBe("Character");
    expect(tText(resources["en-US"], "characters.bindToProjectRole")).toBe("Use in this project");
    expect(tText(resources["en-US"], "services.ttsAccessTitle")).toBe("Add TTS service");
    expect(tText(resources["en-US"], "services.openSourceDetect")).toBe("Test connection");
    expect(tText(resources["en-US"], "services.openSourceBaseUrl")).toBe("ComfyUI URL");
    expect(tText(resources["en-US"], "topbar.ttsConfig")).toBe("Access");
    expect(tText(resources["en-US"], "services.llmApiTitle")).toBe("Script parser");
    expect(tText(resources["en-US"], "parser.advancedConfig")).toBe("Advanced services");
    expect(tText(resources["en-US"], "parser.activateKwjm")).toBe("Save and check");
    expect(tText(resources["en-US"], "services.openSourceDetectAndSave")).toBe("Detect and save");
    expect(tText(resources["en-US"], "services.openSourceDetectNotSaved")).toBe("Detection complete: {{state}}; not saved");
    expect(tText(resources["en-US"], "inspector.diagnosticsReadyShort")).toBe("API ready");
    expect(tText(resources["en-US"], "inspector.title")).toBe("Line Inspector");
    expect(tText(resources["en-US"], "characters.uploadAvatar")).toBe("Upload avatar");
    expect(tText(resources["en-US"], "audioInput.record")).toBe("Record");
    expect(tText(resources["en-US"], "script.drawer.preview")).toBe("Preview");
    expect(tText(resources["en-US"], "script.workspaceTitle")).toBe("Scripts");
    expect(tText(resources["en-US"], "script.workspaceHint")).toBe("Select a script, or create one below.");
    expect(tText(resources["en-US"], "script.parseRevision")).toBe("Legacy parse");
    expect(tText(resources["en-US"], "inspector.method.indextts")).toBe("Index");
    expect(tText(resources["en-US"], "inspector.createIndexTemporary")).toBe("Set temporary voice");
  });

  it("keeps every Simplified Chinese and English translation leaf in lockstep", () => {
    expect(leafKeyPaths(resources["zh-CN"])).toEqual(leafKeyPaths(resources["en-US"]));
  });

  it("ships a complete independent semantic-analysis review namespace", () => {
    expect(tText(resources["zh-CN"], "analysis.input.analyze")).toBe("\u5206\u6790\u5267\u672c");
    expect(tText(resources["en-US"], "analysis.input.analyze")).toBe("Analyze script");
    expect(tText(resources["zh-CN"], "analysis.input.upload")).toBe("\u4e0a\u4f20 .txt / .md");
    expect(tText(resources["en-US"], "analysis.input.upload")).toBe("Upload .txt / .md");
    expect(tText(resources["zh-CN"], "analysis.input.unsupportedFile")).toBe("\u4ec5\u652f\u6301 .txt \u548c .md \u5267\u672c\u6587\u4ef6");
    expect(tText(resources["en-US"], "analysis.input.unsupportedFile")).toBe("Only .txt and .md script files are supported");
    expect(tText(resources["zh-CN"], "analysis.input.readFailed")).toBe("\u65e0\u6cd5\u8bfb\u53d6\u5267\u672c\u6587\u4ef6");
    expect(tText(resources["en-US"], "analysis.input.readFailed")).toBe("Could not read the script file");
    expect(tText(resources["zh-CN"], "analysis.input.largeFileWarning")).toBe("\u5267\u672c\u5171 {{count}} \u4e2a\u5b57\u7b26\uff0c\u8d85\u8fc7\u5efa\u8bae\u4e0a\u9650 {{limit}}");
    expect(tText(resources["en-US"], "analysis.input.largeFileWarning")).toBe("The script has {{count}} characters, above the recommended limit of {{limit}}");
    expect(tText(resources["zh-CN"], "analysis.filters.lowConfidence")).toBe("低置信度");
    expect(tText(resources["en-US"], "analysis.filters.lowConfidence")).toBe("Low confidence");
    expect(tText(resources["zh-CN"], "analysis.errors.copyDiagnostics")).toBe("复制诊断信息");
    expect(tText(resources["en-US"], "analysis.errors.copyDiagnostics")).toBe("Copy diagnostics");
    expect(tText(resources["zh-CN"], "analysis.confirm.importCount")).toBe("将导入 {{count}} 条台词");
    expect(tText(resources["en-US"], "analysis.confirm.importCount")).toBe("Import {{count}} utterances");
    expect(tText(resources["zh-CN"], "analysis.characters.createHuman")).toBe("新建人工角色");
    expect(tText(resources["en-US"], "analysis.characters.createHuman")).toBe("Create human character");
    expect(tText(resources["zh-CN"], "analysis.confirm.recover")).toBe("恢复上次已确认版本");
    expect(tText(resources["en-US"], "analysis.confirm.recover")).toBe("Restore last confirmed version");
    expect(tText(resources["zh-CN"], "analysis.review.partialQuality")).toBe("部分结果需人工复核");
    expect(tText(resources["en-US"], "analysis.review.partialQuality")).toBe("Partial results require review");
    expect(tText(resources["zh-CN"], "analysis.review.unresolvedTitle")).toBe("未解决候选");
    expect(tText(resources["en-US"], "analysis.review.unresolvedTitle")).toBe("Unresolved candidates");
  });
});
