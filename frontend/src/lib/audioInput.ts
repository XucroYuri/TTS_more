const ACCEPTED_AUDIO_EXTENSIONS = new Set(["wav", "mp3", "flac", "m4a", "aac", "ogg", "oga", "webm"]);

const RECORDING_MIME_PREFERENCES = [
  "audio/webm",
  "audio/webm;codecs=opus",
  "audio/ogg;codecs=opus",
  "audio/ogg",
  "audio/wav"
];

export interface AudioFileLike {
  name: string;
  type?: string;
}

export function isAcceptedAudioFile(file: AudioFileLike): boolean {
  if (file.type?.toLowerCase().startsWith("audio/")) return true;
  const extension = file.name.split(".").pop()?.toLowerCase();
  return Boolean(extension && ACCEPTED_AUDIO_EXTENSIONS.has(extension));
}

export function pickRecordingMimeType(supportsType: (mimeType: string) => boolean): string {
  return RECORDING_MIME_PREFERENCES.find(supportsType) ?? "";
}

export function audioExtensionFromMimeType(mimeType: string): string {
  const normalized = mimeType.toLowerCase();
  if (normalized.includes("ogg")) return "ogg";
  if (normalized.includes("wav")) return "wav";
  if (normalized.includes("mpeg")) return "mp3";
  if (normalized.includes("mp4")) return "m4a";
  return "webm";
}
