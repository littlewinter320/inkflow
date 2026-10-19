function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

// Submit only edited fields so another window's saved settings remain authoritative.
export function settingsDiff(current: Record<string, unknown>, baseline: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(current)) {
    if (record(value) && record(baseline[key])) {
      const nested = settingsDiff(value, baseline[key]);
      if (Object.keys(nested).length) result[key] = nested;
    } else if (JSON.stringify(value) !== JSON.stringify(baseline[key])) result[key] = value;
  }
  return result;
}

export function mergeSettings<T extends Record<string, unknown>>(base: T, patch: Record<string, unknown>): T {
  const result: Record<string, unknown> = { ...base };
  for (const [key, value] of Object.entries(patch)) {
    result[key] = record(value) && record(result[key]) ? mergeSettings(result[key], value) : value;
  }
  return result as T;
}
