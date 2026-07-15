export function validatePortableProxyUrl(value: string): boolean {
  if (!value || value.length > 2048 || value !== value.trim() || /[\u0000-\u001f\u007f]/.test(value)) return false;
  try {
    const parsed = new URL(value);
    return (parsed.protocol === "http:" || parsed.protocol === "https:")
      && Boolean(parsed.hostname)
      && (parsed.pathname === "/" || parsed.pathname === "")
      && !parsed.search
      && !parsed.hash
      && !value.includes("\\");
  } catch {
    return false;
  }
}
