import { describe, expect, it } from "vitest";

import { audioExtensionFromMimeType, isAcceptedAudioFile, pickRecordingMimeType } from "./audioInput";

describe("audio input helpers", () => {
  it("accepts common audio files by MIME type or extension", () => {
    expect(isAcceptedAudioFile({ name: "voice.wav", type: "audio/wav" })).toBe(true);
    expect(isAcceptedAudioFile({ name: "voice.flac", type: "" })).toBe(true);
    expect(isAcceptedAudioFile({ name: "voice.WEBM", type: "" })).toBe(true);
    expect(isAcceptedAudioFile({ name: "notes.txt", type: "text/plain" })).toBe(false);
  });

  it("prefers the highest quality MediaRecorder type the browser supports", () => {
    const supported = new Set(["audio/webm", "audio/ogg;codecs=opus"]);

    expect(pickRecordingMimeType((mimeType) => supported.has(mimeType))).toBe("audio/webm");
  });

  it("maps recorder MIME types to stable upload extensions", () => {
    expect(audioExtensionFromMimeType("audio/webm;codecs=opus")).toBe("webm");
    expect(audioExtensionFromMimeType("audio/ogg;codecs=opus")).toBe("ogg");
    expect(audioExtensionFromMimeType("audio/wav")).toBe("wav");
  });
});
