export function avatarImageUrl(path: string | null | undefined): string | null {
  const trimmed = path?.trim();
  if (!trimmed) return null;
  return `/api/assets/image?path=${encodeURIComponent(trimmed)}`;
}
