import { render } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import "./styles.css";
import "./agents.css";
import { AgentsScreen } from "./agents.jsx";
import { DocsScreen } from "./docs.jsx";
import { ModelsScreen } from "./models.jsx";
import "./models.css";
import { friendlyError } from "./util.js";

let HOSTS = ["local"];
const MAX_PENDING_INPUT_BYTES = 64 * 1024;
const FRESH_SNAPSHOT_SECONDS = 15;
const SNIPPET_STORAGE_KEY = "opsdeck-snippets";
const DEFAULT_SNIPPETS = [
  "nvidia-smi",
  "htop",
  "df -h",
  "ollama ps",
  "tmux ls",
  "docker ps",
  "git status",
  "journalctl -f -u ",
  "tail -f ",
];

function readSnippets() {
  try {
    const saved = localStorage.getItem(SNIPPET_STORAGE_KEY);
    const stored = JSON.parse(saved);
    if (!Array.isArray(stored)) return DEFAULT_SNIPPETS;
    const snippets = stored
      .filter((item) => typeof item === "string")
      // The original default omitted the argument-space for journalctl. Preserve
      // every saved entry while making that known argument-taking command safe.
      .map((item) => (item === "journalctl -f -u" ? "journalctl -f -u " : item));
    if (JSON.stringify(snippets) !== saved) {
      localStorage.setItem(SNIPPET_STORAGE_KEY, JSON.stringify(snippets));
    }
    return snippets;
  } catch {
    return DEFAULT_SNIPPETS;
  }
}

function commandNeedsArgument(command) {
  return /\s$/.test(command)
    || /<[^>]+>|\[[A-Za-z][^\]]*\]|\{\{[^}]+\}\}|\$\{[^}]+\}|\$[A-Za-z_]\w*/.test(command);
}

function age(value) {
  if (value == null) return "—";
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.floor(value / 60)}m`;
  if (value < 86400) return `${Math.floor(value / 3600)}h`;
  return `${Math.floor(value / 86400)}d`;
}

function mb(value) {
  if (value == null) return "—";
  return value >= 1024 ? `${(value / 1024).toFixed(1)}G` : `${value}M`;
}

function gpuHost(label) {
  return String(label || "").split("·").at(-1).trim();
}

function agentHost(agent) {
  const explicit = [agent?.host, agent?.hostname, agent?.machine]
    .find((value) => typeof value === "string" && value.trim());
  const normalized = gpuHost(explicit || "");
  return HOSTS.includes(normalized) ? normalized : "local";
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", `'"'"'`)}'`;
}

function fleetFreshness(payload, requestError) {
  if (!payload) {
    return {
      kind: requestError ? "error" : "loading",
      label: requestError ? "ERROR" : "LOADING",
      detail: requestError || "Waiting for first snapshot",
    };
  }
  const timestamp = Number(payload.ts);
  const ageSeconds = Number.isFinite(timestamp)
    ? Math.max(0, Math.floor(Date.now() / 1000 - timestamp))
    : null;
  const snapshotTime = Number.isFinite(timestamp)
    ? new Date(timestamp * 1000).toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    })
    : "unknown time";
  const detail = `Snapshot ${snapshotTime} · ${age(ageSeconds)} old`;
  if (payload.refresh_error) {
    return {
      kind: payload.loading ? "error" : "stale",
      label: payload.loading ? "ERROR" : "STALE",
      detail,
    };
  }
  if (payload.loading) {
    return {
      kind: requestError ? "error" : "loading",
      label: requestError ? "ERROR" : "LOADING",
      detail,
    };
  }
  if (payload.sample) return { kind: "sample", label: "SAMPLE", detail };
  if (requestError || ageSeconds == null || ageSeconds > FRESH_SNAPSHOT_SECONDS) {
    return { kind: "stale", label: "STALE", detail };
  }
  return { kind: "live", label: "LIVE", detail };
}

function Sparkline({ history, level, label }) {
  const values = Array.isArray(history)
    ? history.filter((value) => Number.isFinite(value)).map((value) => Math.max(0, Math.min(100, value)))
    : [];
  if (values.length === 0) return null;
  const points = values.map((value, index) => {
    const x = values.length === 1 ? 100 : (index / (values.length - 1)) * 100;
    return `${x.toFixed(2)},${(42 - (value / 100) * 40).toFixed(2)}`;
  }).join(" ");
  return (
    <svg class={`sparkline ${level}`} viewBox="0 0 100 44" preserveAspectRatio="none" role="img" aria-label={label}>
      <line x1="0" y1="42" x2="100" y2="42" />
      <polyline points={points} />
    </svg>
  );
}

function TokenGate({ open, onSave }) {
  const [value, setValue] = useState("");
  if (!open) return null;
  return (
    <div class="token-gate">
      <form
        class="token-card"
        onSubmit={(event) => {
          event.preventDefault();
          if (value.trim()) onSave(value.trim());
        }}
      >
        <div class="brand-mark">OD</div>
        <p class="eyebrow">PRIVATE ACCESS</p>
        <h1>Connect to opsdeck</h1>
        <p class="muted">Enter your access token once. It stays on this device.</p>
        <input
          autofocus
          type="password"
          autocomplete="current-password"
          placeholder="Access token"
          value={value}
          onInput={(event) => setValue(event.currentTarget.value)}
        />
        <button type="submit" class="primary">Unlock</button>
      </form>
    </div>
  );
}

function GpuCard({ gpu, onKill }) {
  const pct = gpu.total_mb ? Math.round((gpu.used_mb / gpu.total_mb) * 100) : 0;
  const level = pct >= 85 ? "hot" : pct >= 60 ? "warm" : "cool";
  return (
    <article class="gpu-card">
      <header class="gpu-header">
        <div>
          <h2>{gpu.label}</h2>
          <p>{gpu.name}</p>
        </div>
        <span class={`percent ${level}`}>{pct}%</span>
      </header>
      <div class="meter" aria-label={`VRAM ${pct}% used`}>
        <span class={level} style={{ width: `${Math.min(100, pct)}%` }} />
      </div>
      <Sparkline history={gpu.history} level={level} label={`${gpu.label} recent VRAM usage`} />
      <div class="gpu-stats">
        <span><b>{mb(gpu.used_mb)}</b> / {mb(gpu.total_mb)} VRAM</span>
        <span><b>{gpu.util_pct ?? "—"}%</b> UTIL</span>
        <span><b>{gpu.temp_c ?? "—"}°</b> TEMP</span>
      </div>
      <div class="processes">
        {(gpu.procs || []).length === 0 ? (
          <p class="empty-row">No GPU processes</p>
        ) : gpu.procs.map((proc, index) => (
          <div class="process-row" key={`${proc.pid}-${index}`}>
            <span class="status-dot" />
            <span class="process-name">{proc.name}</span>
            {proc.pid != null && <span class="pid">#{proc.pid}</span>}
            <span class="proc-memory">{mb(proc.mem_mb)}</span>
            {Number.isInteger(proc.pid) && onKill && (
              <button
                class="kill-button"
                aria-label={`Kill ${proc.name}, PID ${proc.pid}`}
                onClick={() => onKill(gpu, proc)}
              ><span aria-hidden="true">■</span> Stop</button>
            )}
          </div>
        ))}
      </div>
    </article>
  );
}

function AgentList({ agents, onSelect }) {
  const stateOrder = { working: 0, active: 1, idle: 2 };
  const sorted = [...agents].sort(
    (left, right) => (stateOrder[left.state] ?? 1) - (stateOrder[right.state] ?? 1),
  );
  return (
    <section class="agent-section">
      <div class="section-heading">
        <h2>Agents</h2>
        <span>{agents.length} sessions</span>
      </div>
      <div class="agent-list">
        {sorted.map((agent, index) => (
          <button
            type="button"
            class="agent-row"
            key={`${agent.tool}-${agent.project}-${index}`}
            onClick={() => onSelect(agent)}
            aria-label={`Inspect ${agent.tool} session in ${agent.project}`}
          >
            <span class={`agent-pip ${agent.state}`} />
            <div class="agent-main">
              <div class="agent-title">
                <span class="tool">{agent.tool}</span>
                <b>{agent.project}</b>
                <span class={`agent-state ${agent.state}`}>{agent.state || "unknown"}</span>
                <span class="agent-age">{age(agent.age_seconds)}</span>
              </div>
              {agent.current?.value && (
                <div class="agent-current">
                  {agent.current.kind && <span>{agent.current.kind}</span>}
                  <code>{agent.current.value}</code>
                </div>
              )}
              {agent.last_text && <div class="agent-last">{agent.last_text}</div>}
            </div>
            <span class="agent-chevron" aria-hidden="true">›</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function AgentSheet({ agent, onClose, onOpenTerminal }) {
  if (!agent) return null;
  const host = agentHost(agent);
  const currentKind = agent.current?.kind || "current";
  const currentValue = agent.current?.value;
  return (
    <div class="drawer">
      <button class="drawer-backdrop" aria-label="Close session details" onClick={onClose} />
      <section class="drawer-sheet agent-sheet" role="dialog" aria-modal="true" aria-labelledby="agent-title">
        <div class="drawer-handle" />
        <header>
          <div>
            <p class="eyebrow">SESSION DETAIL</p>
            <h2 id="agent-title">{agent.tool} · {agent.project}</h2>
          </div>
          <button class="icon-button" onClick={onClose} aria-label="Close session details">×</button>
        </header>
        <div class="agent-sheet-meta">
          <span class={`agent-state ${agent.state}`}>{agent.state || "unknown"}</span>
          <span>{age(agent.age_seconds)} ago</span>
          <span>{host}</span>
        </div>
        <section class="agent-detail-block current-action">
          <p>{currentKind}</p>
          {currentValue
            ? <code>{currentValue}</code>
            : <span class="agent-detail-empty">No active action reported.</span>}
        </section>
        <section class="agent-detail-block">
          <p>LAST TEXT</p>
          {agent.last_text
            ? <div class="agent-full-text">{agent.last_text}</div>
            : <span class="agent-detail-empty">No recent text reported.</span>}
        </section>
        <dl class="agent-location">
          <div><dt>HOST</dt><dd>{host}</dd></div>
          <div><dt>CWD</dt><dd><code>{agent.cwd || "—"}</code></dd></div>
        </dl>
        <button class="open-terminal-button" onClick={() => onOpenTerminal(agent, host)}>
          <span aria-hidden="true">›_</span>
          Open in Terminal
        </button>
      </section>
    </div>
  );
}

function Hosts({ hosts, onSelect }) {
  return (
    <section class="compact-section">
      <div class="section-heading">
        <h2>Hosts</h2>
        <span>{hosts.length} {hosts.length === 1 ? "machine" : "machines"}</span>
      </div>
      <div class="compact-list">
        {hosts.map((host) => (
          <button
            type="button"
            class={`host-row ${host.state === "offline" ? "offline" : ""}`}
            key={host.label}
            onClick={() => onSelect && onSelect(host.label)}
            aria-label={`Open terminal on ${gpuHost(host.label)}`}
          >
            <span class={`host-dot ${host.state}`} />
            <b>{host.label}</b>
            <span class="host-detail">{host.detail}</span>
            <span class="host-chevron" aria-hidden="true">›_</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function ServerHealth({ metrics }) {
  if (!metrics || Object.keys(metrics).length === 0) return null;
  const load = Number.isFinite(metrics.load_1m) ? metrics.load_1m.toFixed(2) : "—";
  const memory = Number.isFinite(metrics.memory_used_mb) && Number.isFinite(metrics.memory_total_mb)
    ? `${mb(metrics.memory_used_mb)} / ${mb(metrics.memory_total_mb)}` : "—";
  const disk = Number.isFinite(metrics.disk_used_mb) && Number.isFinite(metrics.disk_total_mb)
    ? `${mb(metrics.disk_used_mb)} / ${mb(metrics.disk_total_mb)}` : "—";
  return (
    <section class="compact-section">
      <div class="section-heading"><h2>Server health</h2><span>local</span></div>
      <div class="server-health">
        <div><span>LOAD · 1M</span><b>{load}</b><small>{metrics.cpu_cores || "—"} cores</small></div>
        <div><span>MEMORY</span><b>{memory}</b><small>used / total</small></div>
        <div><span>HOME DISK</span><b>{disk}</b><small>used / total</small></div>
      </div>
    </section>
  );
}

function Runs({ runs }) {
  if (runs.length === 0) return null;
  return (
    <section class="compact-section">
      <div class="section-heading">
        <h2>Runs</h2>
        <span>{runs.length} tracked</span>
      </div>
      <div class="compact-list">
        {runs.map((run) => (
          <div class="run-row" key={`${run.host}-${run.pid}`}>
            <span class="status-dot" />
            <span class="run-name">{run.name}</span>
            <span class="run-host">{run.host}</span>
            <b>{mb(run.mem_mb)}</b>
          </div>
        ))}
      </div>
    </section>
  );
}

// Short actionable banner text; tap to reveal the raw error detail (S6).
function Ollama({ models }) {
  if (models.length === 0) return null;
  return (
    <section class="compact-section">
      <div class="section-heading">
        <h2>Ollama</h2>
        <span>{models.length} loaded</span>
      </div>
      <div class="compact-list">
        {models.map((model, index) => (
          <div class="ollama-row" key={`${model.name}-${index}`}>
            <span class="status-dot" />
            <b>{model.name}</b>
            <span>{model.size_gb}G</span>
            <span>{model.until}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function KillSheet({ target, busy, error, onCancel, onConfirm }) {
  if (!target) return null;
  return (
    <div class="drawer">
      <button class="drawer-backdrop" aria-label="Cancel kill" onClick={onCancel} />
      <section class="confirm-sheet" role="dialog" aria-modal="true" aria-labelledby="kill-title">
        <div class="drawer-handle" />
        <p class="eyebrow">CONFIRM SIGNAL</p>
        <h2 id="kill-title">Kill GPU process?</h2>
        <p>
          Kill <code>{target.proc.name} · PID {target.proc.pid} · {mb(target.proc.mem_mb)}</code> on{" "}
          <b>{target.gpu.label}</b>?
        </p>
        {error && <p class="confirm-error">{error}</p>}
        <div class="confirm-actions">
          <button onClick={onCancel} disabled={busy}>Cancel</button>
          <button class="danger" onClick={onConfirm} disabled={busy}>
            {busy ? "Killing…" : "Kill process"}
          </button>
        </div>
      </section>
    </div>
  );
}

function ErrorBanner({ short, raw }) {
  const [open, setOpen] = useState(false);
  return (
    <div class="error-banner" role="alert" onClick={() => setOpen((value) => !value)}>
      {short}
      {open && raw && <div class="banner-detail">{raw}</div>}
    </div>
  );
}

function Fleet({ token, onUnauthorized, onOpenTerminal, onOpenHost, active }) {
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState("");
  const [rawError, setRawError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [killTarget, setKillTarget] = useState(null);
  const [killBusy, setKillBusy] = useState(false);
  const [killError, setKillError] = useState("");
  const [selectedAgent, setSelectedAgent] = useState(null);

  // Keep-alive (M4): the screen stays mounted across tab switches; polling
  // pauses while another tab is in front and resumes (with state intact)
  // when Fleet becomes active again.
  useEffect(() => {
    if (!active) return undefined;
    let cancelled = false;
    const load = async () => {
      try {
        const response = await fetch("/api/status", {
          headers: { Authorization: `Bearer ${token}` },
          cache: "no-store",
        });
        if (response.status === 401) {
          onUnauthorized();
          return;
        }
        if (!response.ok) throw new Error(`Status ${response.status}`);
        const data = await response.json();
        if (!cancelled) {
          setPayload(data);
          setError("");
          setRawError("");
        }
      } catch (requestError) {
        if (!cancelled) {
          setRawError(requestError.message || "Fleet unavailable");
          setError(friendlyError(requestError.message, "fleet"));
        }
      }
    };
    load();
    const timer = window.setInterval(load, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [token, refreshKey, active]);

  const runs = (payload?.gpus || []).flatMap((gpu) =>
    (gpu.procs || [])
      .filter((proc) => Number.isInteger(proc.pid) && proc.mem_mb >= 2048)
      .map((proc) => ({ ...proc, host: gpu.label })),
  );

  const confirmKill = async () => {
    if (!killTarget) return;
    setKillBusy(true);
    setKillError("");
    try {
      const response = await fetch("/api/kill", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          host: gpuHost(killTarget.gpu.label),
          pid: killTarget.proc.pid,
        }),
      });
      if (response.status === 401) {
        onUnauthorized();
        return;
      }
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `Kill failed (${response.status})`);
      if (result.sample) {
        setKillError("Sample data — no process was stopped.");
        return;
      }
      setKillTarget(null);
      setRefreshKey((value) => value + 1);
    } catch (requestError) {
      setKillError(requestError.message || "Kill failed");
    } finally {
      setKillBusy(false);
    }
  };
  const freshness = fleetFreshness(payload, error);

  return (
    <main class={`fleet screen ${active ? "" : "tab-hidden"}`}>
      <header class="topbar">
        <div>
          <p class="eyebrow">OPSDECK</p>
          <h1>Fleet</h1>
        </div>
        <div class="topbar-actions">
          <button
            type="button"
            class="fleet-refresh"
            onClick={() => setRefreshKey((value) => value + 1)}
            aria-label="Refresh fleet"
          >↻</button>
          <div class="live-block">
            <div class="live-state">
              <span class={freshness.kind} />
              {freshness.label}
            </div>
            <div class="snapshot-detail">{freshness.detail}</div>
          </div>
        </div>
      </header>
      {payload?.sample && <div class="sample-banner">SAMPLE DATA</div>}
      {payload?.refresh_error && (
        <ErrorBanner short="CACHED SNAPSHOT — refresh failed" raw={payload.refresh_error} />
      )}
      {error && payload && <ErrorBanner short={error} raw={rawError} />}
      {!payload && !error && <div class="loading">Reading fleet…</div>}
      {error && !payload && (
        <ErrorBanner short="opsdeck unreachable — tap for detail" raw={rawError} />
      )}
      <div class="gpu-grid">
        {(payload?.gpus || []).map((gpu) => (
          <GpuCard
            gpu={gpu}
            key={gpu.label}
            onKill={HOSTS.includes(gpuHost(gpu.label)) ? (targetGpu, proc) => {
              setKillError("");
              setKillTarget({ gpu: targetGpu, proc });
            } : null}
          />
        ))}
      </div>
      {payload && <ServerHealth metrics={payload.local_metrics} />}
      {payload && <Hosts hosts={payload.hosts || []} onSelect={onOpenHost} />}
      <Runs runs={runs} />
      <Ollama models={payload?.ollama || []} />
      {payload && <AgentList agents={payload.agents || []} onSelect={setSelectedAgent} />}
      <KillSheet
        target={killTarget}
        busy={killBusy}
        error={killError}
        onCancel={() => !killBusy && setKillTarget(null)}
        onConfirm={confirmKill}
      />
      <AgentSheet
        agent={selectedAgent}
        onClose={() => setSelectedAgent(null)}
        onOpenTerminal={onOpenTerminal}
      />
    </main>
  );
}

function toControl(data) {
  if (data.length !== 1) return data;
  const code = data.toUpperCase().charCodeAt(0);
  return code >= 64 && code <= 95 ? String.fromCharCode(code - 64) : data;
}

function Snippets({ open, snippets, onChange, onSend, onClose }) {
  const [draft, setDraft] = useState("");
  const [editingIndex, setEditingIndex] = useState(null);
  const [editingDraft, setEditingDraft] = useState("");
  if (!open) return null;
  const save = (next) => {
    onChange(next);
    localStorage.setItem(SNIPPET_STORAGE_KEY, JSON.stringify(next));
  };
  const move = (from, to) => {
    if (to < 0 || to >= snippets.length) return;
    const next = [...snippets];
    const [snippet] = next.splice(from, 1);
    next.splice(to, 0, snippet);
    save(next);
  };
  const startEditing = (index) => {
    setEditingIndex(index);
    setEditingDraft(snippets[index]);
  };
  const cancelEditing = () => {
    setEditingIndex(null);
    setEditingDraft("");
  };
  const commitEditing = () => {
    if (!editingDraft.trim()) return;
    save(snippets.map((snippet, index) => (index === editingIndex ? editingDraft : snippet)));
    cancelEditing();
  };
  return (
    <div class="drawer">
      <button class="drawer-backdrop" aria-label="Close snippets" onClick={onClose} />
      <section class="drawer-sheet">
        <div class="drawer-handle" />
        <header>
          <div>
            <p class="eyebrow">RUN OR INSERT</p>
            <h2>Quick commands</h2>
          </div>
          <button class="icon-button" onClick={onClose} aria-label="Close snippets">×</button>
        </header>
        <p class="snippet-help">Tap a command to run it. Commands ending in a space insert so you can finish them.</p>
        <div class="snippet-list">
          {snippets.map((snippet, index) => {
            const needsArgument = commandNeedsArgument(snippet);
            const editing = editingIndex === index;
            return (
              <div class={`snippet-row ${editing ? "editing" : ""}`} key={`${snippet}-${index}`}>
                {editing ? (
                  <form
                    class="snippet-edit-form"
                    onSubmit={(event) => {
                      event.preventDefault();
                      commitEditing();
                    }}
                  >
                    <input
                      autofocus
                      value={editingDraft}
                      aria-label="Edit quick command"
                      onInput={(event) => setEditingDraft(event.currentTarget.value)}
                    />
                    <button type="button" onClick={cancelEditing}>Cancel</button>
                    <button type="submit" class="save">Save</button>
                  </form>
                ) : (
                  <>
                    <button
                      class="snippet-command"
                      onClick={() => {
                        const sent = onSend(needsArgument ? snippet : `${snippet}\r`);
                        if (sent && needsArgument) onClose();
                      }}
                      aria-label={`${needsArgument ? "Insert" : "Run"} ${snippet}`}
                    >
                      <code>{snippet || " "}</code>
                      <span>{needsArgument ? "INSERT" : "RUN"}</span>
                    </button>
                    <div class="snippet-actions" aria-label={`Manage ${snippet}`}>
                      <button
                        type="button"
                        aria-label={`Move ${snippet} up`}
                        disabled={index === 0}
                        onClick={() => move(index, index - 1)}
                      >↑</button>
                      <button
                        type="button"
                        aria-label={`Move ${snippet} down`}
                        disabled={index === snippets.length - 1}
                        onClick={() => move(index, index + 1)}
                      >↓</button>
                      <button type="button" aria-label={`Edit ${snippet}`} onClick={() => startEditing(index)}>Edit</button>
                      <button
                        type="button"
                        class="delete"
                        aria-label={`Delete ${snippet}`}
                        onClick={() => save(snippets.filter((_, itemIndex) => itemIndex !== index))}
                      >×</button>
                    </div>
                  </>
                )}
              </div>
            );
          })}
        </div>
        <form
          class="snippet-add"
          onSubmit={(event) => {
            event.preventDefault();
            if (!draft.trim()) return;
            save([...snippets, draft]);
            setDraft("");
          }}
        >
          <input
            value={draft}
            placeholder="New command (end with space to insert)…"
            onInput={(event) => setDraft(event.currentTarget.value)}
          />
          <button type="submit">Add</button>
        </form>
      </section>
    </div>
  );
}

function TerminalScreen({ token, active, host, hosts, onHostChange, onUnauthorized, launch }) {
  const terminalElement = useRef(null);
  const terminal = useRef(null);
  const socket = useRef(null);
  const socketReady = useRef(false);
  const ctrl = useRef(false);
  const [connected, setConnected] = useState(false);
  const [ctrlActive, setCtrlActive] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [snippets, setSnippets] = useState(readSnippets);
  const fitAddon = useRef(null);
  const intentionalClose = useRef(false);
  const reconnectAttempts = useRef(0);
  const reconnectTimer = useRef(null);
  const pingTimer = useRef(null);
  const pendingInput = useRef([]);
  const pendingInputBytes = useRef(0);
  const [queuedBytes, setQueuedBytes] = useState(0);
  const [inputNotice, setInputNotice] = useState("");
  const [reconnectNonce, setReconnectNonce] = useState(0);
  const handledLaunch = useRef(null);

  const clearPendingInput = () => {
    pendingInput.current = [];
    pendingInputBytes.current = 0;
    setQueuedBytes(0);
    setInputNotice("");
  };

  const send = (data) => {
    const output = String(data);
    if (socket.current?.readyState === WebSocket.OPEN && socketReady.current) {
      try {
        socket.current.send(output);
        terminal.current?.focus();
        return true;
      } catch {
        // The socket can transition between the readyState check and send.
      }
    }
    const bytes = new TextEncoder().encode(output).byteLength;
    if (pendingInputBytes.current + bytes > MAX_PENDING_INPUT_BYTES) {
      setInputNotice("Input queue full — wait for the terminal to connect.");
      terminal.current?.focus();
      return false;
    }
    pendingInput.current.push(output);
    pendingInputBytes.current += bytes;
    setQueuedBytes(pendingInputBytes.current);
    setInputNotice("Input queued until the terminal connects.");
    terminal.current?.focus();
    return true;
  };

  // Terminal instance lives across reconnects so scrollback survives a dropped socket.
  useEffect(() => {
    if (!active || !terminalElement.current) return;
    const term = new Terminal({
      cursorBlink: true,
      convertEol: false,
      fontFamily: '"SFMono-Regular", "Roboto Mono", Menlo, monospace',
      fontSize: 13,
      lineHeight: 1.15,
      scrollback: 5000,
      theme: {
        background: "#070b11",
        foreground: "#dbe7f3",
        cursor: "#62e6a7",
        selectionBackground: "#33435c",
        black: "#111923",
        brightBlack: "#5c6a7b",
        green: "#62e6a7",
        yellow: "#f3c969",
        red: "#ff6b72",
        blue: "#70a5ff",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(terminalElement.current);
    terminal.current = term;
    fitAddon.current = fit;
    fit.fit();

    const dataDisposable = term.onData((data) => {
      const output = ctrl.current ? toControl(data) : data;
      if (ctrl.current) {
        ctrl.current = false;
        setCtrlActive(false);
      }
      send(output);
    });
    const resize = () => {
      if (!terminal.current || !fitAddon.current) return;
      try {
        fitAddon.current.fit();
        const ws = socket.current;
        if (ws && ws.readyState === WebSocket.OPEN && socketReady.current) {
          ws.send(JSON.stringify({ resize: [terminal.current.cols, terminal.current.rows] }));
        }
      } catch {
        // A transient zero-sized viewport during iOS keyboard animation is harmless.
      }
    };
    const viewport = window.visualViewport;
    const resizeTimer = () => window.requestAnimationFrame(resize);
    window.addEventListener("resize", resizeTimer);
    viewport?.addEventListener("resize", resizeTimer);
    viewport?.addEventListener("scroll", resizeTimer);
    resize();

    return () => {
      dataDisposable.dispose();
      window.removeEventListener("resize", resizeTimer);
      viewport?.removeEventListener("resize", resizeTimer);
      viewport?.removeEventListener("scroll", resizeTimer);
      term.dispose();
      terminal.current = null;
      fitAddon.current = null;
    };
  }, [active]);

  // WebSocket lifecycle with auto-reconnect. Bumping reconnectNonce re-opens the socket
  // against the still-mounted terminal (new PTY, preserved scrollback).
  useEffect(() => {
    if (!active || !terminal.current) return;
    const term = terminal.current;
    const decoder = new TextDecoder();
    intentionalClose.current = false;

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${protocol}//${location.host}/api/term?host=${encodeURIComponent(host)}`);
    ws.binaryType = "arraybuffer";
    socket.current = ws;
    socketReady.current = false;

    ws.addEventListener("open", () => {
      ws.send(JSON.stringify({ auth: token }));
    });
    ws.addEventListener("message", (event) => {
      if (!socketReady.current) {
        let reply;
        try { reply = JSON.parse(event.data); } catch { reply = null; }
        if (reply?.type !== "ready") {
          ws.close();
          return;
        }
        socketReady.current = true;
        setConnected(true);
        reconnectAttempts.current = 0;
        try {
          fitAddon.current?.fit();
          ws.send(JSON.stringify({ resize: [term.cols, term.rows] }));
        } catch {
          // ignore transient fit failures
        }
        if (pendingInput.current.length) {
          try {
            ws.send(pendingInput.current.join(""));
            clearPendingInput();
          } catch {
            setInputNotice("Input is still queued while the terminal reconnects.");
          }
        }
        term.focus();
        pingTimer.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ ping: 1 }));
        }, 20000);
        return;
      }
      term.write(typeof event.data === "string" ? event.data : decoder.decode(event.data, { stream: true }));
    });
    ws.addEventListener("close", (event) => {
      socketReady.current = false;
      setConnected(false);
      if (pingTimer.current) {
        clearInterval(pingTimer.current);
        pingTimer.current = null;
      }
      if (intentionalClose.current || !active) return;
      if (event.code === 4401) {
        onUnauthorized();
        return;
      }
      if (event.code === 4429) {
        setInputNotice("Too many terminal connections. Retrying in one minute.");
        reconnectTimer.current = setTimeout(() => setReconnectNonce((n) => n + 1), 60000);
        return;
      }
      const attempt = reconnectAttempts.current++;
      const delay = Math.min(1000 * 2 ** attempt, 8000);
      term.write("\r\n\x1b[33m[reconnecting…]\x1b[0m\r\n");
      reconnectTimer.current = setTimeout(() => setReconnectNonce((n) => n + 1), delay);
    });

    return () => {
      intentionalClose.current = true;
      if (pingTimer.current) {
        clearInterval(pingTimer.current);
        pingTimer.current = null;
      }
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current);
        reconnectTimer.current = null;
      }
      try {
        ws.close();
      } catch {
        // already closing
      }
      if (socket.current === ws) socket.current = null;
      socketReady.current = false;
      setConnected(false);
    };
  }, [active, host, token, reconnectNonce]);

  // The Fleet session sheet can open a host at that session's working directory.
  useEffect(() => {
    if (!active || !launch || handledLaunch.current === launch.id) return;
    if (host !== launch.host) {
      onHostChange(launch.host);
      return;
    }
    handledLaunch.current = launch.id;
    if (launch.cwd) send(`cd -- ${shellQuote(launch.cwd)}\r`);
  }, [active, host, launch]);

  // When the app returns to the foreground, reconnect immediately if the socket died
  // while backgrounded (WKWebView suspends WebSockets).
  useEffect(() => {
    if (!active) return;
    const wake = () => {
      if (document.visibilityState !== "visible") return;
      const ws = socket.current;
      if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
        reconnectAttempts.current = 0;
        if (reconnectTimer.current) {
          clearTimeout(reconnectTimer.current);
          reconnectTimer.current = null;
        }
        setReconnectNonce((n) => n + 1);
      }
    };
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("focus", wake);
    return () => {
      document.removeEventListener("visibilitychange", wake);
      window.removeEventListener("focus", wake);
    };
  }, [active]);

  return (
    <main class={`terminal-screen screen ${active ? "active" : ""}`}>
      <header class="terminal-topbar">
        <div class="host-picker" aria-label="Terminal host">
          {hosts.map((item) => (
            <button
              class={item === host ? "selected" : ""}
              onClick={() => {
                clearPendingInput();
                onHostChange(item);
              }}
              key={item}
            >
              {item}
            </button>
          ))}
        </div>
        <div class={`connection ${connected ? "connected" : ""}`}>
          <span />
          {connected ? "connected" : queuedBytes ? `connecting · ${queuedBytes}B queued` : "connecting"}
        </div>
      </header>
      <div class="terminal-wrap" ref={terminalElement} />
      <div class="terminal-controls">
        <button class="snippets-button" onClick={() => setDrawerOpen(true)}>
          <span>›_</span> Snippets
        </button>
        {inputNotice && <div class={`input-notice ${connected ? "warning" : ""}`}>{inputNotice}</div>}
        <div class="extra-keys">
          <button onClick={() => send("\x1b")}>Esc</button>
          <button onClick={() => send("\t")}>Tab</button>
          <button
            class={ctrlActive ? "modifier-active" : ""}
            onClick={() => {
              ctrl.current = !ctrl.current;
              setCtrlActive(ctrl.current);
              terminal.current?.focus();
            }}
          >Ctrl</button>
          <button onClick={() => send("\x03")}>^C</button>
          {[
            ["←", "\x1b[D"], ["↓", "\x1b[B"], ["↑", "\x1b[A"], ["→", "\x1b[C"],
            ["|", "|"], ["-", "-"],
            ["~", "~"], ["/", "/"], [".", "."],
            ["^D", "\x04"], ["^R", "\x12"],
          ].map(([label, value]) => (
            <button onClick={() => send(value)} key={label}>{label}</button>
          ))}
        </div>
      </div>
      <Snippets
        open={drawerOpen}
        snippets={snippets}
        onChange={setSnippets}
        onSend={send}
        onClose={() => setDrawerOpen(false)}
      />
    </main>
  );
}

function App() {
  const [token, setToken] = useState(() => localStorage.getItem("opsdeck-token") || "");
  const [tokenOpen, setTokenOpen] = useState(() => !localStorage.getItem("opsdeck-token"));
  const [tab, setTab] = useState("fleet");
  const [terminalHost, setTerminalHost] = useState("local");
  const [terminalLaunch, setTerminalLaunch] = useState(null);
  const [hosts, setHosts] = useState(["local"]);

  useEffect(() => {
    const updateHeight = () => {
      const viewport = window.visualViewport;
      document.documentElement.style.setProperty("--app-height", `${viewport?.height || window.innerHeight}px`);
      document.documentElement.style.setProperty("--viewport-top", `${viewport?.offsetTop || 0}px`);
    };
    updateHeight();
    window.addEventListener("resize", updateHeight);
    window.visualViewport?.addEventListener("resize", updateHeight);
    window.visualViewport?.addEventListener("scroll", updateHeight);
    return () => {
      window.removeEventListener("resize", updateHeight);
      window.visualViewport?.removeEventListener("resize", updateHeight);
      window.visualViewport?.removeEventListener("scroll", updateHeight);
    };
  }, []);

  useEffect(() => {
    if (!token) return;
    fetch("/api/config", { headers: { Authorization: `Bearer ${token}` } })
      .then((response) => response.ok ? response.json() : Promise.reject(response))
      .then((config) => {
        const next = Array.isArray(config.hosts) && config.hosts.length ? config.hosts : ["local"];
        HOSTS = next;
        setHosts(next);
      })
      .catch(() => {});
  }, [token]);

  const clearToken = () => {
    localStorage.removeItem("opsdeck-token");
    setToken("");
    setTokenOpen(true);
  };

  return (
    <div class="app-shell">
      {token && (
        <Fleet
          token={token}
          onUnauthorized={clearToken}
          active={tab === "fleet"}
          onOpenTerminal={(agent, host) => {
            if (!hosts.includes(host)) return;
            setTerminalHost(host);
            setTerminalLaunch({ id: Date.now(), host, cwd: agent.cwd || "" });
            setTab("terminal");
          }}
          onOpenHost={(hostLabel) => {
            const host = gpuHost(hostLabel);
            if (HOSTS.includes(host)) {
              setTerminalHost(host);
              setTab("terminal");
            }
          }}
        />
      )}
      {token && (
        <AgentsScreen
          token={token}
          onUnauthorized={clearToken}
          active={tab === "agents"}
        />
      )}
      <DocsScreen
        token={token}
        onUnauthorized={clearToken}
        active={tab === "docs"}
      />
      <ModelsScreen
        token={token}
        onUnauthorized={clearToken}
        active={tab === "models"}
      />
      <TerminalScreen
        token={token}
        active={tab === "terminal" && Boolean(token)}
        host={terminalHost}
        hosts={hosts}
        onHostChange={setTerminalHost}
        onUnauthorized={clearToken}
        launch={terminalLaunch}
      />
      <nav class="tabbar">
        <button class={tab === "fleet" ? "active" : ""} onClick={() => setTab("fleet")}>
          <span class="fleet-icon">▦</span>Fleet
        </button>
        <button class={tab === "agents" ? "active" : ""} onClick={() => setTab("agents")}>
          <span class="agents-icon">✦</span>Agents
        </button>
        <button class={tab === "docs" ? "active" : ""} onClick={() => setTab("docs")}>
          <span class="docs-icon">▤</span>Docs
        </button>
        <button class={tab === "models" ? "active" : ""} onClick={() => setTab("models")}>
          <span class="models-icon">◍</span>Models
        </button>
        <button class={tab === "terminal" ? "active" : ""} onClick={() => setTab("terminal")}>
          <span class="terminal-icon">›_</span>Terminal
        </button>
      </nav>
      <TokenGate
        open={tokenOpen}
        onSave={(nextToken) => {
          localStorage.setItem("opsdeck-token", nextToken);
          setToken(nextToken);
          setTokenOpen(false);
        }}
      />
    </div>
  );
}

if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js"));
}

render(<App />, document.getElementById("app"));
