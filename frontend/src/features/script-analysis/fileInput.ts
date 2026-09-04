const SCRIPT_FILE_WARNING_LIMIT = 200_000;

const mediaTypeByExtension: Record<".txt" | ".md", string> = {
  ".txt": "text/plain",
  ".md": "text/markdown"
};

export interface ScriptFileWarning {
  code: "script_file_exceeds_recommended_limit";
  codePointCount: number;
  limit: number;
}

export interface ScriptFileInput {
  text: string;
  filename: string;
  mediaType: string;
  warning?: ScriptFileWarning;
}

export type ScriptFileTarget = "existing" | "new";
export type ScriptFileOrigin = "picker" | "drop";

export function selectDroppedMarkdown(files: readonly File[]) {
  if (files.length !== 1) return { kind: "rejected" as const, reason: "single_markdown_required" as const };
  const file = files[0];
  if (!file.name.toLowerCase().endsWith(".md")) return { kind: "rejected" as const, reason: "markdown_required" as const };
  return { kind: "accepted" as const, file };
}

export function scriptTitleFromFilename(filename: string): string {
  return filename.replace(/\.md$/i, "");
}

function unicodeCodePointCount(text: string): number {
  let count = 0;
  for (const _codePoint of text) count += 1;
  return count;
}

export async function readScriptFile(file: File): Promise<ScriptFileInput> {
  const dotIndex = file.name.lastIndexOf(".");
  const extension = (dotIndex >= 0 ? file.name.slice(dotIndex) : "").toLowerCase();
  if (extension !== ".txt" && extension !== ".md") {
    throw new Error("unsupported_script_file");
  }

  const text = await file.text();
  if (!text.trim()) throw new Error("empty_script_file");
  const result: ScriptFileInput = {
    text,
    filename: file.name,
    mediaType: file.type || mediaTypeByExtension[extension]
  };
  const codePointCount = unicodeCodePointCount(text);
  if (codePointCount > SCRIPT_FILE_WARNING_LIMIT) {
    result.warning = {
      code: "script_file_exceeds_recommended_limit",
      codePointCount,
      limit: SCRIPT_FILE_WARNING_LIMIT
    };
  }
  return result;
}
