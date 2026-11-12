// Shared phone-side helpers: clipboard, friendly errors, relative time.

// navigator.clipboard is missing on insecure origins (the LAN is plain http),
// so fall back to a hidden textarea + execCommand — works in WKWebView.
export async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the execCommand path
  }
  try {
    const textarea = document.createElement("textarea");
    textarea.value = String(text);
    textarea.setAttribute("readonly", "");
    textarea.style.cssText = "position:fixed;top:0;left:-9999px;opacity:0";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    const ok = document.execCommand("copy");
    textarea.remove();
    return ok;
  } catch {
    return false;
  }
}

// Map raw fetch failures to a short actionable line.
export function friendlyError(message, context) {
  const raw = String(message || "");
  if (/failed to fetch|networkerror|load failed|fetch failed/i.test(raw)) {
    return context === "fleet"
      ? "opsdeck unreachable — showing the cached snapshot"
      : "opsdeck unreachable — check the connection";
  }
  if (/unauthorized/i.test(raw)) return "Session expired — enter the token again.";
  return raw;
}

export function timeAgo(iso) {
  const at = Date.parse(iso || "");
  if (!Number.isFinite(at)) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - at) / 1000));
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function projectLabel(path) {
  const segments = String(path || "").split("/").filter(Boolean);
  return segments[segments.length - 1] || "";
}
