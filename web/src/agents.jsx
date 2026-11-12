import { useEffect, useRef, useState } from "preact/hooks";
import { Markdown } from "./markdown.jsx";
import { Briefings } from "./briefings.jsx";
import { copyText, friendlyError, timeAgo, projectLabel } from "./util.js";

const POLL_OK_MS = 2500;
const POLL_BACKOFF_MS = 10000;
const MAX_ERRORS_BEFORE_BACKOFF = 5;

// Default view: the last 5 days across every enabled agent and machine.
// Empty agents/machines lists mean "all"; the filter persists the choice.
const DEFAULT_RECENT_FILTER = { days: 5, agents: [], machines: [] };
const RECENT_FILTER_KEY = "opsdeck-recent-filter";
const RECENT_CACHE_MS = 30000;
const recentCache = new Map(); // filter key -> { at, payload }
const catalogCache = { agents: null, machines: null };
const LAST_LAUNCH_KEY = "opsdeck-last-launch";

function loadRecentFilter() {
  try {
    const parsed = JSON.parse(localStorage.getItem(RECENT_FILTER_KEY) || "null");
    if (parsed && Array.isArray(parsed.agents) && Array.isArray(parsed.machines)
      && Number.isFinite(parsed.days)) {
      return parsed;
    }
  } catch {
    // fall through to the default
  }
  return DEFAULT_RECENT_FILTER;
}

function saveRecentFilter(filter) {
  try {
    localStorage.setItem(RECENT_FILTER_KEY, JSON.stringify(filter));
  } catch {
    // storage full/private mode — the view still works, just not remembered
  }
}

function loadLastLaunch() {
  try {
    const parsed = JSON.parse(localStorage.getItem(LAST_LAUNCH_KEY) || "null");
    if (parsed && typeof parsed === "object") {
      return {
        agent: typeof parsed.agent === "string" ? parsed.agent : "",
        machine: typeof parsed.machine === "string" ? parsed.machine : "",
        project: typeof parsed.project === "string" ? parsed.project : "",
      };
    }
  } catch {
    // fall through
  }
  return { agent: "", machine: "", project: "" };
}

// Auto-grow a composer textarea up to maxRows, then scroll inside.
function growTextarea(element, maxRows = 6) {
  if (!element) return;
  element.style.height = "auto";
  const style = window.getComputedStyle(element);
  const line = parseFloat(style.lineHeight) || 21;
  const pad = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
  const max = maxRows * line + pad;
  element.style.height = `${Math.min(element.scrollHeight, max)}px`;
  element.style.overflowY = element.scrollHeight > max ? "auto" : "hidden";
}

async function authedFetch(token, url, options, onUnauthorized) {
  const response = await fetch(url, {
    ...options,
    headers: {
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

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function asArray(value) {
  if (Array.isArray(value)) return value;
  if (value && Array.isArray(value.sessions)) return value.sessions;
  if (value && Array.isArray(value.items)) return value.items;
  if (value && Array.isArray(value.data)) return value.data;
  if (value && Array.isArray(value.agents)) return value.agents;
  if (value && Array.isArray(value.machines)) return value.machines;
  if (value && Array.isArray(value.projects)) return value.projects;
  if (value && Array.isArray(value.messages)) return value.messages;
  if (value && Array.isArray(value.transcript)) return value.transcript;
  if (value && Array.isArray(value.hosts)) return value.hosts;
  return [];
}

function labelOf(entry, fallbacks) {
  if (typeof entry === "string") return entry;
  if (entry == null) return "";
  for (const key of fallbacks) {
    const value = entry[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return "";
}

function sessionIdOf(session) {
  return labelOf(session, ["session", "id", "session_id", "name"]);
}

function machineOf(entry) {
  return labelOf(entry, ["machine", "host", "hostname", "label", "name", "id"]);
}

function projectOf(entry) {
  return labelOf(entry, ["project", "path", "folder", "cwd", "name"]);
}

function agentIdOf(entry) {
  return labelOf(entry, ["id", "name", "agent"]);
}

// ---------------------------------------------------------------------------
// Skill picker: "/" button (or typing "/") lists the server's skill catalog;
// tapping one puts "/name " at the front of the message.
// ---------------------------------------------------------------------------

const skillCache = new Map();

function SkillPicker({ token, onUnauthorized, agent, machine, text, setText, inputRef }) {
  const [skills, setSkills] = useState(null);
  const [open, setOpen] = useState(false);
  const [unconfigured, setUnconfigured] = useState(false);
  const [error, setError] = useState("");
  const enabled = Boolean(agent && machine);
  const key = `${agent}|${machine}`;

  useEffect(() => {
    if (!enabled) return;
    if (skillCache.has(key)) {
      const cached = skillCache.get(key);
      setSkills(cached.list);
      setUnconfigured(cached.unconfigured);
      return;
    }
    let cancelled = false;
    setSkills(null);
    setUnconfigured(false);
    authedFetch(token, `/api/skills?agent=${encodeURIComponent(agent)}&machine=${encodeURIComponent(machine)}`,
      undefined, onUnauthorized)
      .then(async (response) => {
        const data = await readJson(response);
        if (!response.ok) throw new Error((data && data.detail) || `Skills unavailable (${response.status})`);
        const list = Array.isArray(data?.skills) ? data.skills : [];
        const entry = { list, unconfigured: data?.configured === false };
        skillCache.set(key, entry);
        if (!cancelled) { setSkills(list); setUnconfigured(entry.unconfigured); setError(""); }
      })
      .catch((requestError) => {
        if (!cancelled && !requestError.unauthorized) setError(requestError.message || "Could not load skills");
      });
    return () => { cancelled = true; };
  }, [key, enabled, token]);

  if (!enabled) return null;

  // Typing "/que" (no space yet) filters the list live.
  const typing = /^\/[\w:.-]*$/.test(text || "");
  const shown = open || typing;
  const query = typing ? text.slice(1).toLowerCase() : "";
  const matches = (skills || []).filter((s) => !query || s.name.toLowerCase().includes(query));

  const pick = (skill) => {
    const rest = (text || "").replace(/^\/[\w:.-]*\s*/, "");
    setText(`/${skill.name} ${rest}`);
    setOpen(false);
    requestAnimationFrame(() => {
      const el = inputRef?.current;
      if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
    });
  };

  return (
    <div class="ag-skills">
      <button type="button" class={`ag-skill-toggle ${shown ? "on" : ""}`}
        onClick={() => { if (typing) { setText(""); setOpen(false); } else setOpen(!open); }}>
        / Skills
      </button>
      {shown && (
        <div class="ag-skill-list" role="listbox">
          {error && <p class="ag-error">{error}</p>}
          {!error && unconfigured && <p class="ag-empty">No skills configured — set OPSDECK_SKILLS_DIR on the server.</p>}
          {!error && !unconfigured && skills === null && <p class="ag-empty">Loading…</p>}
          {!error && !unconfigured && skills && matches.length === 0 && <p class="ag-empty">No matching skill.</p>}
          {matches.map((skill) => (
            <button type="button" class="ag-skill" key={skill.name} onClick={() => pick(skill)}>
              <span class="ag-skill-name">/{skill.name}{skill.hint ? <em> {skill.hint}</em> : null}</span>
              {skill.description && <span class="ag-skill-desc">{skill.description}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// New session picker: five native selects then a chat box.
// ---------------------------------------------------------------------------

export function NewSession({ token, onUnauthorized, onLaunched }) {
  const [catalog, setCatalog] = useState(null);
  const [machines, setMachines] = useState([]);
  const [machinesConfigured, setMachinesConfigured] = useState(true);
  const [projects, setProjects] = useState([]);
  const [projectsConfigured, setProjectsConfigured] = useState(true);
  const [agent, setAgent] = useState("");
  const [machine, setMachine] = useState("");
  const [project, setProject] = useState("");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  const [message, setMessage] = useState("");
  const [loadingProjects, setLoadingProjects] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const messageRef = useRef(null);
  const [machineModels, setMachineModels] = useState(null);
  const lastLaunch = useRef(loadLastLaunch());

  // Each agent's model choices come from the server operator's config.
  useEffect(() => {
    if (!agent || !machine) return;
    let cancelled = false;
    authedFetch(token, `/api/models?agent=${encodeURIComponent(agent)}&machine=${encodeURIComponent(machine)}`,
      undefined, onUnauthorized)
      .then(async (response) => {
        const data = response.ok ? await readJson(response) : null;
        if (cancelled || !data) return;
        setMachineModels({ key: `${agent}|${machine}`, models: asArray(data.models), default: data.default || "" });
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [agent, machine, token]);

  // Model and effort are remembered per agent.
  const remember = (kind, value) => {
    try {
      if (value) localStorage.setItem(`opsdeck-${kind}:${agent}`, value);
      else localStorage.removeItem(`opsdeck-${kind}:${agent}`);
    } catch {}
  };
  const recall = (kind, id) => {
    try { return localStorage.getItem(`opsdeck-${kind}:${id}`) || ""; } catch { return ""; }
  };

  // Catalog (agents + their models/efforts) and machine list load once.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const response = await authedFetch(token, "/api/agents", undefined, onUnauthorized);
        if (!response.ok) throw new Error(`Agents unavailable (${response.status})`);
        const data = await readJson(response);
        if (!cancelled) setCatalog(data);
      } catch (requestError) {
        if (!cancelled && !requestError.unauthorized) {
          setError(friendlyError(requestError.message || "Could not load agents"));
        }
      }
      try {
        const response = await authedFetch(token, "/api/machines", undefined, onUnauthorized);
        if (!response.ok) throw new Error(`Machines unavailable (${response.status})`);
        const data = await readJson(response);
        if (!cancelled) setMachines(asArray(data));
      } catch (requestError) {
        if (!cancelled && !requestError.unauthorized) {
          // Older servers only have /api/config; the launcher still works.
          try {
            const fallback = await authedFetch(token, "/api/config", undefined, onUnauthorized);
            const config = fallback.ok ? await readJson(fallback) : null;
            if (!cancelled && config) {
              setMachines(asArray(config).map((host) => ({ id: host, label: host })));
              setMachinesConfigured(true);
            }
          } catch {
            setMachinesConfigured(false);
          }
        }
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const agentEntries = asArray(catalog);
  const agentsConfigured = catalog ? catalog.configured !== false : true;
  const agentIds = agentEntries.map(agentIdOf).filter(Boolean);
  const activeAgent = agentEntries.find((entry) =>
    typeof entry === "string" ? entry === agent : agentIdOf(entry) === agent,
  );
  const catalogModels = typeof activeAgent === "string"
    ? []
    : asArray(activeAgent?.models ?? activeAgent?.model_options).map((m) => labelOf(m, ["id", "name", "model"])).filter(Boolean);
  const modelOptions = machineModels?.key === `${agent}|${machine}` && machineModels.models.length
    ? machineModels.models
    : catalogModels;
  const defaultModel = machineModels?.key === `${agent}|${machine}` ? machineModels.default : "";
  const effortOptions = typeof activeAgent === "string"
    ? []
    : asArray(
      activeAgent?.efforts ?? activeAgent?.effort_options
        ?? activeAgent?.intelligence ?? activeAgent?.intelligence_options,
    ).map((e) => (typeof e === "string" ? e : labelOf(e, ["id", "name", "level"]))).filter(Boolean);
  const agentUnavailable = typeof activeAgent === "object" && activeAgent !== null
    && activeAgent.available === false;
  const agentDisabledReason = typeof activeAgent === "object" && activeAgent !== null
    ? (activeAgent.disabled_reason || "")
    : "";

  // Machine options: id is what the API takes, label is what the phone shows.
  const machineOptions = machines
    .map((m) => (typeof m === "string"
      ? { value: m, label: m }
      : { value: agentIdOf(m) || machineOf(m), label: machineOf(m) }))
    .filter((m) => m.value);

  useEffect(() => {
    if (machine && !machineOptions.some((m) => m.value === machine)) setMachine("");
  }, [agent, machines]);

  // Remembered picks: prefill agent/machine/project from the last launch,
  // and auto-select any field with exactly one option.
  useEffect(() => {
    if (!agent && lastLaunch.current.agent && agentIds.includes(lastLaunch.current.agent)) {
      setAgent(lastLaunch.current.agent);
      setModel(recall("model", lastLaunch.current.agent));
      setEffort(recall("effort", lastLaunch.current.agent));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentIds.length]);

  useEffect(() => {
    if (machine) return;
    if (machineOptions.length === 1) {
      setMachine(machineOptions[0].value);
      return;
    }
    if (lastLaunch.current.machine
      && machineOptions.some((m) => m.value === lastLaunch.current.machine)) {
      setMachine(lastLaunch.current.machine);
    }
  }, [agent, machines, machine]);

  // Projects reload whenever the machine changes.
  useEffect(() => {
    setProjects([]);
    setProject("");
    setProjectsConfigured(true);
    if (!machine) return;
    let cancelled = false;
    setLoadingProjects(true);
    authedFetch(
      token,
      `/api/projects?machine=${encodeURIComponent(machine)}`,
      undefined,
      onUnauthorized,
    ).then(async (response) => {
      if (!response.ok) throw new Error(`Projects unavailable (${response.status})`);
      const data = await readJson(response);
      if (!cancelled) {
        setProjects(asArray(data));
        setProjectsConfigured(data?.configured !== false);
      }
    }).catch((requestError) => {
      if (!cancelled && !requestError.unauthorized) {
        setError(friendlyError(requestError.message || "Could not load projects"));
      }
    }).finally(() => {
      if (!cancelled) setLoadingProjects(false);
    });
    return () => {
      cancelled = true;
    };
  }, [machine, token]);

  // Auto-select the folder when the machine has exactly one project, or the
  // remembered one exists on this machine.
  const projectOptions = projects.map(projectOf).filter(Boolean);
  useEffect(() => {
    if (project) return;
    if (projectOptions.length === 1) {
      setProject(projectOptions[0]);
      return;
    }
    if (lastLaunch.current.project && projectOptions.includes(lastLaunch.current.project)) {
      setProject(lastLaunch.current.project);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectOptions.length, machine, project]);

  // Keep the opening-message box sized to its text.
  useEffect(() => {
    growTextarea(messageRef.current, 6);
  }, [message]);

  const canSend = agent && machine && project && message.trim() && !sending && !agentUnavailable;

  const missingHint = !agentsConfigured ? "Agents are not configured — see setup below."
    : !agent ? "Pick an agent to start."
    : agentUnavailable ? agentDisabledReason
      : !machine ? "Pick a machine."
        : !projectsConfigured ? "Project folders are not configured for this machine — set OPSDECK_PROJECT_ROOTS on the server."
          : !project ? (machine ? "Pick a project folder." : "Pick a machine first.")
            : !message.trim() ? "Type the opening message." : "";

  const launch = async (event) => {
    event.preventDefault();
    if (!canSend) return;
    setSending(true);
    setError("");
    try {
      const response = await authedFetch(token, "/api/agent/launch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent,
          machine,
          project,
          ...(model ? { model } : {}),
          ...(effort ? { effort } : {}),
          message: message.trim(),
        }),
      }, onUnauthorized);
      const result = await readJson(response);
      if (!response.ok) {
        throw new Error(
          (result && (result.detail || result.error || result.message)) || `Launch failed (${response.status})`,
        );
      }
      const session = sessionIdOf(result) || sessionIdOf(result?.session);
      try {
        localStorage.setItem(LAST_LAUNCH_KEY, JSON.stringify({ agent, machine, project }));
      } catch {
        // remembering is best-effort
      }
      onLaunched({
        agent,
        machine,
        session,
        run_id: (result && (result.run_id || result.runId)) || "",
        project,
        launched_at: Date.now(),
      });
    } catch (requestError) {
      if (!requestError.unauthorized) setError(friendlyError(requestError.message || "Launch failed"));
    } finally {
      setSending(false);
    }
  };

  const selectField = (labelText, value, onChange, options, placeholder, disabled) => (
    <label class="ag-field">
      <span>{labelText}</span>
      <select value={value} disabled={disabled} onChange={(event) => onChange(event.currentTarget.value)}>
        <option value="">{placeholder}</option>
        {options.map((option) => {
          const val = typeof option === "string" ? option : option.value;
          const text = typeof option === "string" ? option : option.label;
          return <option value={val} key={val}>{text}</option>;
        })}
      </select>
    </label>
  );

  return (
    <form class="ag-launch" onSubmit={launch}>
      {selectField("Agent", agent, (next) => { setAgent(next); setModel(recall("model", next)); setEffort(recall("effort", next)); }, agentIds, agentsConfigured ? "Choose agent…" : "No agents configured…", !agentsConfigured)}
      {selectField("Machine", machine, setMachine, machineOptions, "Choose machine…")}
      {selectField(
        "Project folder",
        project,
        setProject,
        projectOptions,
        loadingProjects ? "Loading…" : !projectsConfigured ? "Not configured…" : machine ? "Choose folder…" : "Pick a machine first…",
        loadingProjects || !machine || !projectsConfigured,
      )}
      {selectField(
        "Model",
        model,
        (next) => { setModel(next); remember("model", next); },
        model && !modelOptions.includes(model) ? [model, ...modelOptions] : modelOptions,
        agent ? (defaultModel ? `Default (${defaultModel})` : "Default") : "Pick an agent first…",
        !agent,
      )}
      {selectField(
        "Intelligence / effort",
        effort,
        (next) => { setEffort(next); remember("effort", next); },
        effortOptions,
        agent ? (effortOptions.length ? "Default" : "Default (no effort setting)") : "Pick an agent first…",
        !agent,
      )}
      <SkillPicker token={token} onUnauthorized={onUnauthorized} agent={agent} machine={machine}
        text={message} setText={setMessage} inputRef={messageRef} />
      <div class="ag-compose">
        <textarea
          ref={messageRef}
          rows={3}
          placeholder="Message…"
          value={message}
          aria-label="Opening message"
          onInput={(event) => {
            setMessage(event.currentTarget.value);
            growTextarea(event.currentTarget, 6);
          }}
        />
        <button type="submit" class="ag-send" disabled={!canSend}>
          {sending ? "Starting…" : "Send"}
        </button>
      </div>
      {!canSend && !error && missingHint && <p class="ag-hint">{missingHint}</p>}
      {error && <p class="ag-error" role="alert">{error}</p>}
    </form>
  );
}

// ---------------------------------------------------------------------------
// Recent sessions: day range plus agent and machine filters. Filter chips
// autohide when only one value exists; a refresh button refetches now.
// ---------------------------------------------------------------------------

export function RecentSessions({
  token,
  onUnauthorized,
  onOpen,
  filter,
  setFilter,
  onNeedAgents,
}) {
  const [body, setBody] = useState(null);
  const [error, setError] = useState("");
  // The picker reads the server's own catalogs so its chips never drift.
  const [catalog, setCatalog] = useState([]);
  const [machines, setMachines] = useState([]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [agentsResponse, machinesResponse] = await Promise.all([
          authedFetch(token, "/api/agents", undefined, onUnauthorized),
          authedFetch(token, "/api/machines", undefined, onUnauthorized).catch(() => null),
        ]);
        if (!cancelled && agentsResponse.ok) {
          const agentsData = await readJson(agentsResponse);
          setCatalog(asArray(agentsData).map(agentIdOf).filter(Boolean));
          if (onNeedAgents && agentsData?.configured === false) onNeedAgents(false);
        }
        if (!cancelled && machinesResponse?.ok) {
          const machinesData = await readJson(machinesResponse);
          setMachines(asArray(machinesData).map((m) =>
            (typeof m === "string" ? m : agentIdOf(m) || machineOf(m))).filter(Boolean));
        }
      } catch (requestError) {
        if (!cancelled && !requestError.unauthorized) setError(friendlyError(requestError.message));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const [loading, setLoading] = useState(true);
  const timer = useRef(null);

  const load = async (force, current) => {
    const active = current || filter;
    const params = new URLSearchParams();
    params.set("days", String(active.days));
    if (active.agents.length) params.set("agents", active.agents.join(","));
    if (active.machines.length) params.set("machines", active.machines.join(","));
    const key = params.toString();
    if (!force) {
      const cached = recentCache.get(key);
      if (cached && Date.now() - cached.at < RECENT_CACHE_MS) {
        setBody(cached.payload);
        setLoading(false);
        return;
      }
    }
    setLoading(true);
    try {
      const response = await authedFetch(token, `/api/agent/recent?${key}`, undefined, onUnauthorized);
      if (!response.ok) throw new Error(`Recent sessions unavailable (${response.status})`);
      const payload = await readJson(response);
      recentCache.set(key, { at: Date.now(), payload });
      setBody(payload);
      setError("");
    } catch (requestError) {
      if (!requestError.unauthorized) setError(friendlyError(requestError.message || "Could not load sessions"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(false, filter);
    // The hardware back button closes the filter sheet but should not leave
    // a stale open session behind; the timers stay scoped to this view.
    clearInterval(timer.current);
    timer.current = setInterval(() => load(true, filter), POLL_OK_MS * 4);
    return () => clearInterval(timer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, token]);

  const sessions = asArray(body?.sessions ?? body);

  // Session identity: agent + machine + session id.
  const keyOf = (session) =>
    `${agentIdOf(session.agent) || session.agent || ""}|${machineOf(session.machine || session.host) || ""}|${sessionIdOf(session)}`;

  const toggle = (kind, value) => {
    const current = filter[kind] || [];
    const next = current.includes(value) ? current.filter((v) => v !== value) : [...current, value];
    const updated = { ...filter, [kind]: next };
    setFilter(updated);
    saveRecentFilter(updated);
  };

  const emptyHint = !loading && sessions.length === 0
    ? (body && body.configured === false
      ? "No session history is configured yet — set OPSDECK_AGENT_SESSIONS_DIR on the server. Runs you launch from this phone will still appear here."
      : "No sessions in this range. Start one above, or widen the filters.")
    : "";

  return (
    <div>
      <div class="ag-filter-row">
        {[1, 2, 5, 7, 14].map((days) => (
          <button
            key={days}
            type="button"
            class={`ag-filter-chip ${filter.days === days ? "on" : ""}`}
            onClick={() => {
              const updated = { ...filter, days };
              setFilter(updated);
              saveRecentFilter(updated);
            }}
          >
            {days}d
          </button>
        ))}
        <button
          type="button"
          class="ag-filter-chip ag-filter-refresh"
          onClick={() => {
            recentCache.clear();
            load(true, filter);
          }}
          aria-label="Refresh sessions"
        >
          ↻
        </button>
      </div>
      {machines.length > 1 && (
        <div class="ag-filter-row">
          <span class="ag-filter-label">Machine</span>
          {machines.map((name) => (
            <button
              type="button"
              key={name}
              class={`ag-filter-chip ${filter.machines.includes(name) ? "on" : ""}`}
              onClick={() => toggle("machines", name)}
            >
              {name}
            </button>
          ))}
        </div>
      )}
      {catalog.length > 1 && (
        <div class="ag-filter-row">
          <span class="ag-filter-label">Agent</span>
          {catalog.map((name) => (
            <button
              type="button"
              key={name}
              class={`ag-filter-chip ${filter.agents.includes(name) ? "on" : ""}`}
              onClick={() => toggle("agents", name)}
            >
              {name}
            </button>
          ))}
        </div>
      )}
      {error && <p class="ag-error" role="alert">{error}</p>}
      {emptyHint && !error && <p class="ag-empty">{emptyHint}</p>}
      {body?.unreachable?.length > 0 && (
        <p class="ag-hint">{`Unreachable: ${body.unreachable.join(", ")}`}</p>
      )}
      <div class="ag-recent-list">
        {sessions.map((session) => (
          <button type="button" class="ag-recent" key={keyOf(session) + (session.updated_at || "")}
            onClick={() => onOpen(session)}>
            <span class="ag-recent-title">{(session.preview || "").trim() || "(no preview)"}</span>
            <span class="ag-recent-sub">
              {[
                agentIdOf(session.agent),
                projectLabel(projectOf(session.project)),
                session.updated_at ? timeAgo(session.updated_at) : "",
                machineOf(session.machine || session.host),
              ].filter(Boolean).join(" · ")}
            </span>
          </button>
        ))}
      </div>
      {loading && <p class="ag-empty">Loading…</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Transcript view: pulls the messages, polls while running, falls back to
// the run log when there is no plain-text transcript yet.
// ---------------------------------------------------------------------------

function ToolSummary({ detail }) {
  const args = detail?.arguments;
  if (args == null || args === "") return null;
  const text = String(args);
  if (text.length < 80) return <code>{text}</code>;
  return (
    <details>
      <summary class="ag-tool-summary"><span class="ag-tool-caret">▸</span><code>{text.slice(0, 80)}…</code></summary>
      <pre class="ag-tool-full">{text}</pre>
    </details>
  );
}

function ChatBubbles({ messages }) {
  return (
    <div class="ag-thread">
      {messages.map((message, index) => {
        const text = typeof message === "string" ? message : message?.text || message?.content || message?.body || "";
        const role = (typeof message === "object" && message?.role) || "assistant";
        const kind = (typeof message === "object" && message?.kind) || "message";
        if (kind === "tool") {
          return (
            <div class={`ag-bubble tool ${role}`} key={index}>
              <div class="ag-tool-head">{message?.text || message?.summary || "tool"}</div>
              <ToolSummary detail={message?.detail} />
            </div>
          );
        }
        if (role === "user") {
          return (
            <div class="ag-bubble user" key={index}>
              <div class="ag-bubble-head">You</div>
              <div class="ag-bubble-body">{text}</div>
            </div>
          );
        }
        return (
          <div class={`ag-bubble assistant ${role !== "assistant" ? role : ""}`} key={index}>
            <Markdown text={text} />
          </div>
        );
      })}
    </div>
  );
}

export function ChatView({ token, onUnauthorized, target, onBack }) {
  const params = new URLSearchParams();
  params.set("agent", target.agent);
  params.set("machine", target.machine);
  params.set("session", target.session);
  const url = `/api/agent/transcript?${params}`;

  const [state, setState] = useState({ status: "loading", messages: [], agError: "", fallback: null });
  const errors = useRef(0);
  const timer = useRef(null);
  const jumpRef = useRef(null);
  const [jumpVisible, setJumpVisible] = useState(false);
  const [expandedErrors, setExpandedErrors] = useState({});
  const [copied, setCopied] = useState("");

  useEffect(() => {
    let cancelled = false;
    jumpRef.current = document.querySelector("main.agents.screen");
    const scrollEl = jumpRef.current;
    const onScroll = () => {
      if (!scrollEl) return;
      const remaining = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight;
      const hasOverflow = scrollEl.scrollHeight > scrollEl.clientHeight + 8;
      setJumpVisible(hasOverflow && remaining > 240);
    };
    scrollEl?.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => {
      scrollEl?.removeEventListener("scroll", onScroll);
      cancelled = true;
      clearInterval(timer.current);
      void cancelled;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    clearInterval(timer.current);
    setState({ status: "loading", messages: [], agError: "", fallback: null });

    const poll = async (initial) => {
      try {
        const response = await authedFetch(token, url, undefined, onUnauthorized);
        const payload = await readJson(response);
        if (cancelled) return;
        if (!response.ok) {
          const detail = payload?.detail ?? payload;
          if (response.status === 409 && detail && typeof detail === "object" && detail.message) {
            setState({ status: "noresume", messages: [], agError: detail.message, fallback: detail });
            clearInterval(timer.current);
            return;
          }
          throw new Error((typeof detail === "string" ? detail : detail?.message) || `Transcript unavailable (${response.status})`);
        }
        errors.current = 0;
        const list = asArray(payload).map((entry) => (typeof entry === "string" ? entry : entry));
        setState((current) => {
          if (!initial && current.status === "ready" && current.messages.length === list.length
            && current.messages.every((message, index) => message === list[index])) {
            return current;
          }
          return {
            status: "ready",
            messages: list,
            agError: "",
            fallback: payload && typeof payload === "object" && !Array.isArray(payload) ? payload : null,
          };
        });
        const running = Boolean(payload?.running);
        clearInterval(timer.current);
        if (running) timer.current = setInterval(poll, POLL_OK_MS);
      } catch (requestError) {
        if (cancelled || requestError.unauthorized) return;
        errors.current += 1;
        const wait = errors.current > MAX_ERRORS_BEFORE_BACKOFF ? POLL_BACKOFF_MS : POLL_OK_MS;
        clearInterval(timer.current);
        timer.current = setInterval(poll, wait);
        if (initial) setState({ status: "error", messages: [], agError: friendlyError(requestError.message), fallback: null });
      }
    };
    poll(true);
    timer.current = setInterval(poll, POLL_OK_MS);
    return () => {
      cancelled = true;
      clearInterval(timer.current);
    };
  }, [url, token]);

  if (state.status === "loading") {
    return (
      <div class="ag-chat">
        <div class="ag-chat-head">
          <button type="button" class="icon-btn" onClick={onBack} aria-label="Back to sessions">‹</button>
          <b>Transcript</b>
        </div>
        <p class="ag-empty">Loading transcript…</p>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div class="ag-chat">
        <div class="ag-chat-head">
          <button type="button" class="icon-btn" onClick={onBack} aria-label="Back to sessions">‹</button>
          <b>Transcript</b>
        </div>
        <p class="ag-error" role="alert">{state.agError}</p>
      </div>
    );
  }

  if (state.status === "noresume") {
    return (
      <div class="ag-chat">
        <div class="ag-chat-head">
          <button type="button" class="icon-btn" onClick={onBack} aria-label="Back to sessions">‹</button>
          <b>{projectLabel(target.project)}</b>
        </div>
        <div class="ag-fallback">
          <p>{state.agError}</p>
          {state.fallback?.log_tail?.length > 0 && (
            <pre class="ag-runlog">{state.fallback.log_tail.join("\n")}</pre>
          )}
        </div>
      </div>
    );
  }

  const fallback = state.fallback && typeof state.fallback === "object" ? state.fallback : null;
  const logTail = Array.isArray(fallback?.log_tail) ? fallback.log_tail : [];
  const isRunning = Boolean(fallback?.running);

  return (
    <div class="ag-chat">
      <div class="ag-chat-head">
        <button type="button" class="icon-btn" onClick={onBack} aria-label="Back to sessions">‹</button>
        <b>{projectLabel(target.project)}</b>
        {isRunning && <span class="ag-running">running</span>}
      </div>
      {state.agError && <p class="ag-error" role="alert">{state.agError}</p>}
      {state.messages.length === 0 && logTail.length === 0 && !isRunning && (
        <p class="ag-empty">No transcript yet. The agent's first reply will appear here.</p>
      )}
      {state.messages.length > 0 && <ChatBubbles messages={state.messages} />}
      {logTail.length > 0 && (
        <div class="ag-fallback">
          <p>Run log{isRunning ? " (running)" : ""}:</p>
          <pre class="ag-runlog">{logTail.join("\n")}</pre>
        </div>
      )}
      {isRunning && logTail.length === 0 && state.messages.length === 0 && (
        <p class="ag-empty">Agent is starting — the first output lands here.</p>
      )}
      <ResumeChatBar token={token} onUnauthorized={onUnauthorized} target={target}
        expandedErrors={expandedErrors} setExpandedErrors={setExpandedErrors}
        copied={copied} setCopied={setCopied} />
      {jumpVisible && (
        <button type="button" class="ag-jump" onClick={() => {
          const el = jumpRef.current;
          if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
        }}>
          ↓ Latest
        </button>
      )}
    </div>
  );
}

function ResumeChatBar({ token, onUnauthorized, target, expandedErrors, setExpandedErrors, copied, setCopied }) {
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [sentRunId, setSentRunId] = useState("");
  const draftRef = useRef(null);

  useEffect(() => {
    growTextarea(draftRef.current, 6);
  }, [draft]);

  const resume = async (event) => {
    event.preventDefault();
    if (!draft.trim() || sending) return;
    setSending(true);
    setError("");
    setSentRunId("");
    try {
      const response = await authedFetch(token, "/api/agent/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent: target.agent,
          machine: target.machine,
          session: target.session || "",
          message: draft.trim(),
        }),
      }, onUnauthorized);
      const result = await readJson(response);
      if (!response.ok) {
        const detail = result?.detail ?? result;
        if (response.status === 409 && detail && typeof detail === "object" && detail.message) {
          setError(friendlyError(detail.message));
          return;
        }
        throw new Error((typeof detail === "string" ? detail : detail?.message) || `Send failed (${response.status})`);
      }
      setDraft("");
      setSentRunId((result && (result.run_id || result.runId)) || "");
    } catch (requestError) {
      if (!requestError.unauthorized) setError(friendlyError(requestError.message || "Send failed"));
    } finally {
      setSending(false);
    }
  };

  const copyError = async (key, text) => {
    const ok = await copyText(text);
    setCopied(ok ? key : "");
    if (ok) setTimeout(() => setCopied(""), 1500);
  };

  const errorKey = `send-${target.agent}-${target.machine}-${target.session}`;
  const showCopy = copied === errorKey ? "Copied" : "Copy";

  return (
    <div>
      {sentRunId && <p class="ag-hint">Sent — the reply lands in this thread shortly.</p>}
      <form class="ag-reply" onSubmit={resume}>
        {error && (
          <p class="ag-error" role="alert">
            {error}{" "}
            <button type="button" class="ag-filter-chip" onClick={() => copyError(errorKey, error)}>
              {showCopy}
            </button>{" "}
            <button type="button" class="ag-filter-chip"
              onClick={() => setExpandedErrors({ ...expandedErrors, [errorKey]: !expandedErrors[errorKey] })}>
              {expandedErrors[errorKey] ? "Less" : "More"}
            </button>
          </p>
        )}
        <div class="ag-compose">
          <textarea
            ref={draftRef}
            rows={2}
            placeholder="Follow up…"
            value={draft}
            aria-label="Follow-up message"
            onInput={(event) => {
              setDraft(event.currentTarget.value);
              growTextarea(event.currentTarget, 6);
            }}
          />
          <button type="submit" class="ag-send" disabled={!draft.trim() || sending}>
            {sending ? "Sending…" : "Send"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Agents tab: New session, Recent sessions, then Briefings.
// ---------------------------------------------------------------------------

export function AgentsScreen({ token, onUnauthorized, active }) {
  const [chatTarget, setChatTarget] = useState(null);
  const [recentFilter, setRecentFilter] = useState(loadRecentFilter);
  const [agentsConfigured, setAgentsConfigured] = useState(true);
  const [lastLaunch, setLastLaunch] = useState(loadLastLaunch);

  const openSession = (session) => {
    const agent = agentIdOf(session.agent);
    const machine = machineOf(session.machine || session.host);
    const sessionId = sessionIdOf(session);
    if (!agent || !machine || !sessionId) return;
    setChatTarget({ agent, machine, session: sessionId, project: projectOf(session.project) });
  };

  // A launch drops straight into its run view when it already has a session,
  // otherwise back to the list where the new run shows up.
  const afterLaunch = (launched) => {
    recentCache.clear();
    setLastLaunch({ agent: launched.agent, machine: launched.machine, project: launched.project });
    if (launched.session) {
      setChatTarget({
        agent: launched.agent,
        machine: launched.machine,
        session: launched.session,
        project: launched.project,
      });
    }
  };

  return (
    <main class={`agents screen ${chatTarget ? "chat-open " : ""}${active ? "" : "tab-hidden"}`}>
      <header class="topbar">
        <div>
          <p class="eyebrow">OPSDECK</p>
          <h1>Agents</h1>
        </div>
      </header>
      {!agentsConfigured && (
        <section class="ag-setup" aria-label="Agent setup">
          <h2>Agents are not configured yet</h2>
          <p>An unconfigured install shows this instead of dead controls. Ask your operator to set:</p>
          <ul>
            <li><code>OPSDECK_AGENTS</code> — which agent CLIs this server may run (for example <code>claude,codex</code>)</li>
            <li><code>OPSDECK_AGENT_COMMANDS</code> — absolute CLI path per agent (for example <code>claude=/usr/local/bin/claude</code>)</li>
            <li><code>OPSDECK_PROJECT_ROOTS</code> — project folders per machine (for example <code>/srv/projects,lab=/data/projects</code>)</li>
          </ul>
          <p>Optional: <code>OPSDECK_AGENT_MODELS</code>, <code>OPSDECK_AGENT_SESSIONS_DIR</code>, <code>OPSDECK_SKILLS_DIR</code>, <code>OPSDECK_BRIEFINGS_DIR</code>.</p>
        </section>
      )}
      {chatTarget ? (
        <ChatView token={token} onUnauthorized={onUnauthorized} target={chatTarget}
          onBack={() => setChatTarget(null)} />
      ) : (
        <>
          <NewSession token={token} onUnauthorized={onUnauthorized} onLaunched={afterLaunch}
            key={`${lastLaunch.agent}|${lastLaunch.machine}`} />
          <section aria-label="Recent sessions">
            <div class="section-heading">
              <h2>Recent</h2>
            </div>
            <RecentSessions token={token} onUnauthorized={onUnauthorized} onOpen={openSession}
              filter={recentFilter} setFilter={setRecentFilter}
              onNeedAgents={(configured) => setAgentsConfigured(configured)} />
          </section>
          <Briefings token={token} onUnauthorized={onUnauthorized} />
        </>
      )}
    </main>
  );
}
