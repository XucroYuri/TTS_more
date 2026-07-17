export function expandFixtureEnvironment(value: unknown, env: NodeJS.ProcessEnv): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => expandFixtureEnvironment(item, env));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, expandFixtureEnvironment(item, env)]),
    );
  }
  if (typeof value !== "string") return value;

  const expanded = value
    .replace(/\$\{([^}]+)\}/g, (match, name: string) => env[name] ?? match)
    .replace(/%([^%]+)%/g, (match, name: string) => env[name] ?? match);
  if (/\$\{[^}]+\}|%[^%]+%/.test(expanded)) {
    throw new Error("CUDA fixture has unresolved environment variables");
  }
  return expanded;
}
