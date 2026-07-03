import { UserRound } from "lucide-react";

import { avatarImageUrl } from "../lib/avatars";

interface RoleAvatarProps {
  avatarPath?: string | null;
  fallback: string;
  size?: "sm" | "md" | "lg";
}

export function RoleAvatar({ avatarPath, fallback, size = "md" }: RoleAvatarProps) {
  const imageUrl = avatarImageUrl(avatarPath);
  return (
    <span className={`role-avatar role-avatar-${size}`} aria-hidden="true">
      {imageUrl ? <img src={imageUrl} alt="" /> : fallback ? <span>{fallback}</span> : <UserRound size={14} />}
    </span>
  );
}
