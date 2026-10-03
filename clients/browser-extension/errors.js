/*
 * NetSanctum Capture — shared error text.
 *
 * A server error is not always a string. FastAPI answers a request-validation
 * failure with `detail` as a LIST of error objects, so the obvious
 * `String(payload.detail)` renders "[object Object]" and the user learns nothing.
 * Every page in this extension funnels its messages through here instead.
 */
function netsanctumErrorText(status, payload, fallback) {
  const detail = payload && payload.detail;

  if (typeof detail === "string" && detail.trim()) return detail;

  if (Array.isArray(detail)) {
    const parts = [];
    for (const entry of detail) {
      if (entry === null || entry === undefined) continue;
      if (typeof entry !== "object") {
        parts.push(String(entry));
        continue;
      }
      const message = typeof entry.msg === "string" ? entry.msg : JSON.stringify(entry);
      const field = Array.isArray(entry.loc)
        ? entry.loc.filter((part) => part !== "body" && typeof part === "string" && part !== "").join(".")
        : "";
      parts.push(field ? `${field}: ${message}` : message);
    }
    if (parts.length) return parts.join("; ");
  }

  if (detail && typeof detail === "object") {
    if (typeof detail.message === "string") return detail.message;
    try {
      return JSON.stringify(detail);
    } catch (error) {
      return fallback;
    }
  }

  if (detail !== undefined && detail !== null) return String(detail);
  return fallback || `NetSanctum returned ${status}`;
}

/* Last line of defence: nothing object-shaped may reach the UI as text. */
function netsanctumSafeText(value, fallback) {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return fallback || "";
  if (value instanceof Error) return value.message || fallback || "";
  try {
    return JSON.stringify(value);
  } catch (error) {
    return fallback || "";
  }
}
