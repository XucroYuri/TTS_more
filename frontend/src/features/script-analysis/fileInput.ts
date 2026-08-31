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

  const source = await file.text();
  const text = source.startsWith("\uFEFF") ? source.slice(1) : source;
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
