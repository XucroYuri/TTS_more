import { describe, expect, it } from "vitest";

import { expandFixtureEnvironment } from "./cuda-fixture";

describe("expandFixtureEnvironment", () => {
  it("recursively expands PowerShell and POSIX environment placeholders", () => {
    const fixture = {
      references: ["${TTS_MORE_VALIDATION_GPT_REF}", "%TTS_MORE_VALIDATION_INDEX_REF%"],
      nested: { enabled: true },
    };

    expect(
      expandFixtureEnvironment(fixture, {
        TTS_MORE_VALIDATION_GPT_REF: "D:\\private\\gpt.wav",
        TTS_MORE_VALIDATION_INDEX_REF: "D:\\private\\index.wav",
      }),
    ).toEqual({
      references: ["D:\\private\\gpt.wav", "D:\\private\\index.wav"],
      nested: { enabled: true },
    });
  });

  it("rejects unresolved placeholders without exposing their names or values", () => {
    expect(() =>
      expandFixtureEnvironment(
        { reference: "${TTS_MORE_PRIVATE_REFERENCE}" },
        {},
      ),
    ).toThrowError("CUDA fixture has unresolved environment variables");
  });
});
