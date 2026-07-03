import { describe, expect, it } from "vitest";

import { avatarImageUrl } from "./avatars";

describe("avatar helpers", () => {
  it("builds a safe image asset URL for stored avatar paths", () => {
    expect(avatarImageUrl("data/character_avatars/小品.png")).toBe("/api/assets/image?path=data%2Fcharacter_avatars%2F%E5%B0%8F%E5%93%81.png");
  });

  it("returns null when no avatar path is available", () => {
    expect(avatarImageUrl(null)).toBeNull();
    expect(avatarImageUrl("")).toBeNull();
  });
});
