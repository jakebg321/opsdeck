import { useEffect, useRef, useState } from "preact/hooks";
import "./models.css";
import "./ask.css";
import { friendlyError } from "./util.js";
import { AskScreen } from "./ask.jsx";

const POLL_MS = 2000;
const VIEW_KEY = "opsdeck-models-view-v3";

async function authed(token, url, options, onUnauthorized) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers || {}),
      Authorization: `Bearer ${token}`,
    },
    cache: "no-store",
  });
  if (response.status === 401) {
    onUnauthorized();
    const error = new Error("Unauthorized");
    error.unauthorized = true;
    throw error;
  }
  return response;
}

function StatLine({ run }) {
  const bits = [];
  if (run.duration_s != null) bits.push(`${run.duration_s}s`);
  if (run.prompt_tokens != null || run.completion_tokens != null) {
    bits.push(`${run.prompt_tokens ?? "?"} in / ${run.completion_tokens ?? "?"} out`);
  }
  if (bits.length === 0) return null;
  return <div class="models-stats">{bits.join(" · ")}</div>;
}

// Resize an attached photo client-side to ~1024px before upload so the phone
// never sends a multi-MB original. Returns a {dataUrl, width, height} triple.
function resizePhoto(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      const scale = Math.min(1, 1024 / Math.max(img.width, img.height));
      const width = Math.max(1, Math.round(img.width * scale));
      const height = Math.max(1, Math.round(img.height * scale));
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      canvas.getContext("2d").drawImage(img, 0, 0, width, height);
      resolve({
        dataUrl: canvas.toDataURL("image/jpeg", 0.85),
        width,
        height,
      });
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("Could not read that photo"));
    };
    img.src = url;
  });
}

function SetupHelp({ active, viewSwitch }) {
  return (
    <main class={`models screen ${active ? "" : "tab-hidden"}`}>
      <header class="topbar">
        <div>
          <p class="eyebrow">OPSDECK</p>
          <h1>Models</h1>
        </div>
      </header>
      {viewSwitch}
      <section class="models-card">
        <div class="section-heading">
          <h2>No model box configured</h2>
          <span class="models-down">SETUP</span>
        </div>
        <p class="muted">
          Connect your own Ollama-compatible endpoint or a private model-box
          adapter. Prompts go only to the server-side box you configured.
          For a direct Ollama endpoint, set:
        </p>
        <pre class="models-raw">OPSDECK_MODELS="mymachine=http://127.0.0.1:11434"</pre>
        <p class="muted">
          Use one <code>label=url</code> entry per endpoint, comma-separated.
          Labels may contain letters, digits, dots, dashes, and underscores.
          Only <code>http://</code> and <code>https://</code> URLs without
          embedded credentials are accepted, for example a second machine over
          your private tailnet:
        </p>
        <pre class="models-raw">OPSDECK_MODELS="desk=http://127.0.0.1:11434,lab=https://gpu.example.com:11434"</pre>
        <p class="muted">
          Or set <code>OPSDECK_MODEL_ADAPTERS_FILE</code> to a private JSON box
          configuration for your own worker tools. The README documents the
          adapter contract. The box picker appears when a box is configured.
        </p>
        <p class="muted">
          Runs accept an optional photo for vision-capable models: it is resized
          on this device (max 1024px) and sent with the prompt. The server
          accepts PNG or JPEG up to 4 MB and never stores the image bytes.
        </p>
      </section>
    </main>
  );
}

function AskSetup({ active, viewSwitch }) {
  return (
    <main class={`models screen ask-screen ${active ? "" : "tab-hidden"}`}>
      <div class="models-console-bar">{viewSwitch}</div>
      <section class="models-card">
        <div class="section-heading">
          <h2>Ask chats isn't configured</h2>
          <span class="models-down">SETUP</span>
        </div>
        <p class="muted">
          Ask chats answers questions over a compatible external Ask service —
          that service is <b>not</b> included in this repository. Point this
          server at your own compatible service, then restart it:
        </p>
        <pre class="models-raw">OPSDECK_ASK_URL="http://127.0.0.1:8756"</pre>
        <p class="muted">
          Only <code>http://</code> and <code>https://</code> URLs without
          embedded credentials are accepted. The answering box id defaults to{" "}
          <code>local</code>; override it when your service uses another id:
        </p>
        <pre class="models-raw">OPSDECK_ASK_BOX="local"</pre>
        <p class="muted">
          When your service needs a bearer token, set{" "}
          <code>OPSDECK_ASK_TOKEN</code>; it is forwarded as an upstream{" "}
          <code>Authorization</code> header and never appears in URLs or error
          output. Quick Run below keeps working without any of this.
        </p>
        <p class="muted">
          Required service API: <code>GET /api/chats/projects</code>,{" "}
          <code>GET /api/local/{"{box}"}/status</code>,{" "}
          <code>POST /api/chats/fit</code>, <code>POST /api/chats/ask</code>,
          and <code>GET /api/chats/job?id=…</code>, all JSON.
        </p>
      </section>
    </main>
  );
}

function ConsoleSetup({ active, viewSwitch }) {
  return (
    <main class={`models screen ask-screen ${active ? "" : "tab-hidden"}`}>
      <div class="models-console-bar">{viewSwitch}</div>
      <section class="models-card">
        <div class="section-heading">
          <h2>No console URL configured</h2>
          <span class="models-down">SETUP</span>
        </div>
        <p class="muted">
          The full-console view embeds your own model-console web UI in this
          tab. Set its URL on the server, then restart it:
        </p>
        <pre class="models-raw">OPSDECK_ASK_CONSOLE_URL="https://gpu.example.com:8443/"</pre>
        <p class="muted">
          Only <code>http://</code> and <code>https://</code> URLs without
          embedded credentials are accepted, and only an address you trust —
          the page runs inside this tab. Quick Run and Ask chats are
          unaffected while this is unset.
        </p>
      </section>
    </main>
  );
}

function ViewSwitch({ view, setView }) {
  // Every tab is always visible: an unconfigured Ask or Console tab shows
  // its setup card instead of hiding, so the setup path stays discoverable.
  return (
    <div class="models-view-switch" role="tablist">
      <button class={view === "ask" ? "selected" : ""} onClick={() => setView("ask")}>Ask chats</button>
      <button class={view === "quick" ? "selected" : ""} onClick={() => setView("quick")}>Quick</button>
      <button class={view === "console" ? "selected" : ""} onClick={() => setView("console")}>All</button>
    </div>
  );
}

function ConsoleView({ active, viewSwitch, consoleUrl }) {
  const [loaded, setLoaded] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  return (
    <main class={`models models-console screen ${active ? "" : "tab-hidden"}`}>
      <div class="models-console-bar">
        {viewSwitch}
        <button type="button" class="fleet-refresh" onClick={() => { setLoaded(false); setReloadKey((k) => k + 1); }} aria-label="Reload console">
          ↻
        </button>
      </div>
      {!loaded && <p class="muted models-console-note">Loading your console…</p>}
      <iframe
        key={`${consoleUrl}-${reloadKey}`}
        class="models-console-frame"
        src={consoleUrl}
        title="Model console"
        referrerPolicy="no-referrer"
        onLoad={() => setLoaded(true)}
      />
      <p class="models-console-open">
        If the console refuses to embed,{" "}
        <a href={consoleUrl} target="_blank" rel="noreferrer">open it in a new tab</a>.
      </p>
    </main>
  );
}

export function ModelsScreen({ token, onUnauthorized, active }) {
  const [providers, setProviders] = useState(null);
  const [configured, setConfigured] = useState(null);
  const [provider, setProvider] = useState("");
  const [ask, setAsk] = useState({ configured: false, box: "local", console_url: null });
  const [askLoaded, setAskLoaded] = useState(false);
  const [view, setViewState] = useState(() => {
    try { return localStorage.getItem(VIEW_KEY) || "quick"; } catch { return "quick"; }
  });
  const [status, setStatus] = useState(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [statusError, setStatusError] = useState("");
  const [model, setModel] = useState("");
  const [prompt, setPrompt] = useState("");
  const [maxTokens, setMaxTokens] = useState(256);
  const [think, setThink] = useState(false);
  const [photo, setPhoto] = useState(null); // {dataUrl, width, height}
  const [photoError, setPhotoError] = useState("");
  const [job, setJob] = useState(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState("");
  const [history, setHistory] = useState([]);
  const [actionNote, setActionNote] = useState("");
  const pollTimer = useRef(null);
  const fileRef = useRef(null);
  const caps = providers?.find((item) => item.id === provider) || {};

  const setView = (next) => {
    setViewState(next);
    try { localStorage.setItem(VIEW_KEY, next); } catch {}
  };

  const loadProviders = async () => {
    try {
      const response = await authed(token, "/api/models/providers", null, onUnauthorized);
      const data = await response.json();
      setProviders(data.providers || []);
      setConfigured(Boolean(data.configured));
      if (data.configured && !data.providers.some((item) => item.id === provider)) {
        setProvider(data.providers[0]?.id || "");
      }
    } catch (error) {
      if (!error.unauthorized) setConfigured(false);
    }
  };

  const loadAskConfig = async () => {
    try {
      const response = await authed(token, "/api/ask/config", null, onUnauthorized);
      const data = await response.json();
      setAsk({
        configured: Boolean(data.configured),
        box: typeof data.box === "string" && data.box ? data.box : "local",
        console_url: typeof data.console_url === "string" && data.console_url ? data.console_url : null,
      });
    } catch {
      setAsk({ configured: false, box: "local", console_url: null });
    } finally {
      setAskLoaded(true);
    }
  };

  const loadStatus = async (id) => {
    const target = id || provider;
    if (!target) return;
    setStatusLoading(true);
    setStatusError("");
    try {
      // /status already carries the model list; a second /models call would
      // hit /api/tags on the endpoint again for the same data.
      const statusRes = await authed(
        token, `/api/models/status?provider=${encodeURIComponent(target)}`, null, onUnauthorized,
      );
      const merged = await statusRes.json();
      setStatus(merged);
      const usable = (merged.models || []).filter((name) => !(merged.disabled || []).includes(name));
      setModel((current) => (usable.includes(current) ? current : (usable[0] || "")));
    } catch (error) {
      if (!error.unauthorized) setStatusError(friendlyError(error.message, "models status"));
    } finally {
      setStatusLoading(false);
    }
  };

  const loadHistory = async () => {
    try {
      const response = await authed(token, "/api/models/history", null, onUnauthorized);
      const data = await response.json();
      setHistory(Array.isArray(data.runs) ? data.runs : []);
    } catch {
      // History is a convenience; a failure must not block prompting.
    }
  };

  useEffect(() => {
    if (!token || !active) return;
    loadProviders();
    loadAskConfig();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, active]);

  useEffect(() => {
    if (!token || !active || !provider) return;
    setStatus(null);
    setJob(null);
    setRunError("");
    setActionNote("");
    loadStatus(provider);
    loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider, token, active]);

  useEffect(() => () => {
    if (pollTimer.current) window.clearInterval(pollTimer.current);
  }, []);

  const askReady = askLoaded && ask.configured;
  const consoleReady = askLoaded && Boolean(ask.console_url);
  const effectiveView = view;

  const pollJob = (jobId) => {
    if (pollTimer.current) window.clearInterval(pollTimer.current);
    pollTimer.current = window.setInterval(async () => {
      try {
        const response = await authed(
          token, `/api/models/jobs/${encodeURIComponent(jobId)}`, null, onUnauthorized,
        );
        const data = await response.json();
        setJob(data);
        if (data.state && data.state !== "queued" && data.state !== "running") {
          window.clearInterval(pollTimer.current);
          pollTimer.current = null;
          setRunning(false);
          loadHistory();
        }
      } catch (error) {
        if (!error.unauthorized) {
          window.clearInterval(pollTimer.current);
          pollTimer.current = null;
          setRunning(false);
          setRunError(friendlyError(error.message, "job poll"));
        }
      }
    }, POLL_MS);
  };

  const run = async (event) => {
    event?.preventDefault();
    if (running || !prompt.trim() || !model || !provider) return;
    setRunning(true);
    setRunError("");
    setActionNote("");
    setJob({ state: "queued", job_id: "", provider, model });
    try {
      const response = await authed(token, "/api/models/submit", {
        method: "POST",
        body: JSON.stringify({
          provider,
          model,
          prompt: prompt.trim(),
          image_base64: caps.image && photo ? photo.dataUrl : null,
          max_tokens: Number(maxTokens) || 256,
          think: Boolean(caps.think && think),
        }),
      }, onUnauthorized);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Run failed (${response.status})`);
      pollJob(data.job_id);
    } catch (error) {
      if (!error.unauthorized) {
        setRunning(false);
        setRunError(error.message || "Run failed");
      }
    }
  };

  const unload = async () => {
    if (!model) return;
    if (!window.confirm(`Unload ${model} from ${provider} to free VRAM?`)) return;
    setActionNote("");
    try {
      const response = await authed(token, "/api/models/unload", {
        method: "POST",
        body: JSON.stringify({ provider, model }),
      }, onUnauthorized);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Unload failed (${response.status})`);
      setActionNote("Model unloaded.");
      loadStatus();
    } catch (error) {
      if (!error.unauthorized) setActionNote(`Unload failed: ${error.message}`);
    }
  };

  const workerAction = async (action) => {
    if (action === "stop" && !window.confirm(`Stop worker on ${caps.label || provider}? In-flight runs may be lost.`)) return;
    setActionNote("");
    try {
      const response = await authed(token, `/api/models/worker-${action}`, {
        method: "POST",
        body: JSON.stringify({ provider }),
      }, onUnauthorized);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Worker ${action} failed (${response.status})`);
      setActionNote(`Worker ${action === "start" ? "started" : "stopped"}.`);
      loadStatus();
    } catch (error) {
      if (!error.unauthorized) setActionNote(`Worker ${action} failed: ${error.message}`);
    }
  };

  const pickPhoto = async (event) => {
    const file = event.currentTarget.files?.[0];
    setPhotoError("");
    if (!file) return;
    try {
      setPhoto(await resizePhoto(file));
    } catch (error) {
      setPhotoError(error.message || "Could not read that photo");
    }
  };

  const viewSwitch = (
    <ViewSwitch view={effectiveView} setView={setView} />
  );

  if (effectiveView === "ask") {
    if (!askLoaded) {
      return (
        <main class={`models screen ask-screen ${active ? "" : "tab-hidden"}`}>
          <div class="models-console-bar">{viewSwitch}</div>
          <p class="muted models-console-note">Loading Ask chats…</p>
        </main>
      );
    }
    if (!askReady) return <AskSetup active={active} viewSwitch={viewSwitch} />;
    return (
      <main class={`models screen ask-screen ${active ? "" : "tab-hidden"}`}>
        <div class="models-console-bar">{viewSwitch}</div>
        <AskScreen token={token} onUnauthorized={onUnauthorized} active={active} box={ask.box} />
      </main>
    );
  }

  if (effectiveView === "console") {
    if (!askLoaded) {
      return (
        <main class={`models screen ask-screen ${active ? "" : "tab-hidden"}`}>
          <div class="models-console-bar">{viewSwitch}</div>
          <p class="muted models-console-note">Loading console…</p>
        </main>
      );
    }
    if (!consoleReady) return <ConsoleSetup active={active} viewSwitch={viewSwitch} />;
    return (
      <ConsoleView
        active={active}
        viewSwitch={viewSwitch}
        consoleUrl={ask.console_url}
      />
    );
  }

  if (configured === false || (providers && providers.length === 0)) {
    return <SetupHelp active={active} viewSwitch={viewSwitch} />;
  }

  const modelList = status?.models || [];

  return (
    <main class={`models screen ${active ? "" : "tab-hidden"}`}>
      <header class="topbar">
        <div>
          <p class="eyebrow">OPSDECK</p>
          <h1>Models</h1>
        </div>
        <button type="button" class="fleet-refresh" onClick={() => loadStatus()} aria-label="Refresh model status">
          ↻
        </button>
      </header>

      {viewSwitch}

      {providers && providers.length > 0 && (
        <div class="box-picker" aria-label="Model box">
          {providers.map((item) => (
            <button
              key={item.id}
              class={item.id === provider ? "selected" : ""}
              onClick={() => setProvider(item.id)}
            >
              {item.label.split(" ")[0]}
            </button>
          ))}
        </div>
      )}

      <section class="models-card">
        <div class="section-heading">
          <h2>{caps.label || provider || "Models"}</h2>
          <span class={status?.ok ? "models-ok" : "models-down"}>
            {statusLoading ? "…" : status ? (status.ok ? "UP" : "DOWN") : "—"}
          </span>
        </div>
        {statusError && <p class="confirm-error">{statusError}</p>}
        {status?.holder && <p class="models-holder">holder: {status.holder}</p>}
        {status?.gpu && Object.keys(status.gpu).length > 0 && (
          <p class="models-holder">
            {status.gpu.temp_c ?? "—"}° · {status.gpu.mem_used_mb ?? "—"}/{status.gpu.mem_total_mb ?? "—"}M · {status.gpu.util_pct ?? "—"}% util
          </p>
        )}
        {status?.error && <p class="confirm-error">{status.error}</p>}
        {caps.note && <p class="muted">{caps.note}</p>}
      </section>

      <section class="models-card">
        <label class="models-label" for="models-model">Model</label>
        <select
          id="models-model"
          value={model}
          onChange={(event) => setModel(event.currentTarget.value)}
        >
          <option value="" disabled>
            {modelList.length ? "Pick a model" : "No models listed"}
          </option>
          {modelList.map((name) => (
            <option value={name} key={name} disabled={(status?.disabled || []).includes(name)}>{name}</option>
          ))}
        </select>

        <label class="models-label" for="models-prompt">Prompt</label>
        <textarea
          id="models-prompt"
          rows={4}
          placeholder="Ask the model…"
          value={prompt}
          onInput={(event) => setPrompt(event.currentTarget.value)}
        />

        <div class="models-row">
          <label class="models-inline">
            Max tokens
            <input
              type="number"
              min={1}
              max={4096}
              value={maxTokens}
              onInput={(event) => setMaxTokens(event.currentTarget.value)}
            />
          </label>
          {caps.think && (
            <label class="models-inline models-check">
              <input type="checkbox" checked={think}
                onChange={(event) => setThink(event.currentTarget.checked)} />
              Thinking
            </label>
          )}
        </div>

        {caps.image && <div class="models-photo">
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            onChange={pickPhoto}
            aria-label="Attach a photo (optional, vision models only)"
          />
          {photoError && <p class="confirm-error">{photoError}</p>}
          {photo && (
            <div class="models-preview">
              <img src={photo.dataUrl} alt="Attached prompt" />
              <div>
                <p>{photo.width}×{photo.height}px (resized)</p>
                <button type="button" onClick={() => { setPhoto(null); if (fileRef.current) fileRef.current.value = ""; }}>
                  Remove
                </button>
              </div>
            </div>
          )}
        </div>}

        <button type="button" class="primary models-run" onClick={run} disabled={running || !prompt.trim() || !model}>
          {running ? "Running…" : "Run"}
        </button>
        {runError && <p class="confirm-error">{runError}</p>}
      </section>

      {job && (
        <section class="models-card">
          <div class="section-heading">
            <h2>Result</h2>
            {job?.state && <span class={`models-state ${job.state}`}>{job.state}</span>}
          </div>
          {job?.state === "refused" && (
            <p class="models-refused">Box busy — held by <b>{job.held_by || "another job"}</b>. Not retried.</p>
          )}
          {job?.answer && <div class="models-answer">{job.answer}</div>}
          {job?.error && job.state !== "queued" && job.state !== "refused" && <p class="confirm-error">{job.error}</p>}
          {job?.state === "refused" && job.error && <pre class="models-raw">{job.error}</pre>}
          {job && <StatLine run={job} />}
        </section>
      )}

      {(caps.unload || caps.worker_start || caps.worker_stop || actionNote) && (
        <section class="models-card models-actions">
          {caps.unload && <button type="button" onClick={unload} disabled={!model}>Unload model</button>}
          {caps.worker_start && <button type="button" onClick={() => workerAction("start")}>Start worker</button>}
          {caps.worker_stop && <button type="button" class="danger" onClick={() => workerAction("stop")}>Stop worker</button>}
          {actionNote && <p class="muted">{actionNote}</p>}
        </section>
      )}

      {history.length > 0 && (
        <section class="compact-section">
          <div class="section-heading">
            <h2>Recent runs</h2>
            <span>{history.length} kept</span>
          </div>
          <div class="compact-list">
            {history.map((item) => (
              <button
                type="button"
                class="run-row models-history-row"
                key={item.job_id}
                onClick={() => {
                  setModel(item.model);
                  setPrompt(item.prompt);
                  if (item.provider && item.provider !== provider) setProvider(item.provider);
                }}
                aria-label={`Reuse run ${item.job_id}`}
              >
                <span class="status-dot" />
                <span class="run-name">{item.prompt || item.model}</span>
                <span class="run-host">{item.model}</span>
                <b>{item.state}</b>
              </button>
            ))}
          </div>
        </section>
      )}
    </main>
  );
}
