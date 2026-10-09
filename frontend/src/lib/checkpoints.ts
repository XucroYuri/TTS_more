export function checkpointOptions(config: Record<string, unknown>, kind: "gpt" | "sovits", fallback: Array<{name: string; path: string}>): Array<{name: string; path: string}> {
  const configured = config[`${kind}_weight_options`];
  const options = Array.isArray(configured) && configured.length ? configured as Array<{name: string; path: string}> : fallback;
  const root = String(config[`${kind}_weights_root`] ?? "").replace(/\\/g, "/").replace(/\/$/, "").toLocaleLowerCase();
  return options.filter((item) => typeof item.path === "string" && (!root || item.path.replace(/\\/g, "/").split("/").slice(0, -1).join("/").toLocaleLowerCase() === root));
}
