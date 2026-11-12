import { useEffect, useState } from "preact/hooks";
import "./briefings.css";

// Agent Briefings: spoken updates served from the operator-configured
// OPSDECK_BRIEFINGS_DIR. Newest first; tap to play. Audio bytes are fetched
// with the Authorization header and played through an object URL, so the
// bearer token never appears in a URL. Renders nothing until the server
// reports configured:true.
function ago(iso) {
  const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function clock(seconds) {
  const s = Math.round(seconds || 0);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function BriefingAudio({ token, onUnauthorized, briefingId }) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    let objectUrl = "";
    setUrl("");
    setError("");
    const load = async () => {
      try {
        const response = await fetch(`/api/briefings/${encodeURIComponent(briefingId)}/audio`, {
          headers: { Authorization: `Bearer ${token}` },
          cache: "no-store",
        });
        if (response.status === 401) { onUnauthorized(); return; }
        if (!response.ok) throw new Error(`Audio unavailable (${response.status})`);
        const blob = await response.blob();
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      } catch (requestError) {
        if (!cancelled) setError(requestError.message || "Could not load audio");
      }
    };
    load();
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [briefingId, token]);

  if (error) return <p class="ag-error">{error}</p>;
  if (!url) return <p class="ag-empty">Loading audio…</p>;
  return <audio controls preload="none" src={url} />;
}

export function Briefings({ token, onUnauthorized }) {
  const [items, setItems] = useState(null);
  const [open, setOpen] = useState("");
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const response = await fetch("/api/briefings", {
          headers: { Authorization: `Bearer ${token}` },
          cache: "no-store",
        });
        if (response.status === 401) { onUnauthorized(); return; }
        const data = response.ok ? await response.json() : null;
        if (!cancelled && data) {
          setItems(data.configured ? (Array.isArray(data.briefings) ? data.briefings : []) : []);
        }
      } catch {
        if (!cancelled) setItems((current) => current || []);
      }
    };
    load();
    const timer = setInterval(load, 20000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [token]);

  if (!items || items.length === 0) return null;
  const shown = showAll ? items : items.slice(0, 3);

  return (
    <section class="br-section" aria-label="Briefings">
      <div class="section-heading">
        <h2>Briefings</h2>
        <span>{items.length}</span>
      </div>
      <div class="br-list">
        {shown.map((item) => (
          <div class={`br-item ${open === item.id ? "open" : ""}`} key={item.id}>
            <button type="button" class="br-head" onClick={() => setOpen(open === item.id ? "" : item.id)}>
              <b>{item.title}</b>
              <span>{[item.agent, item.project, clock(item.seconds), ago(item.created_at)].filter(Boolean).join(" · ")}</span>
            </button>
            {open === item.id && (
              <BriefingAudio token={token} onUnauthorized={onUnauthorized} briefingId={item.id} />
            )}
            {open === item.id && item.text && <p class="br-text">{item.text}</p>}
          </div>
        ))}
      </div>
      {items.length > 3 && (
        <button type="button" class="br-more" onClick={() => setShowAll(!showAll)}>
          {showAll ? "Show fewer" : `Show all ${items.length}`}
        </button>
      )}
    </section>
  );
}
