export function formatScriptNote(note?: string | null): string {
  const trimmed = note?.trim();
  if (!trimmed) {
    return "";
  }

  const unwrapped = trimmed.match(/^[（(]([\s\S]*)[）)]$/)?.[1]?.trim() ?? trimmed;
  return unwrapped ? `（${unwrapped}）` : "";
}
