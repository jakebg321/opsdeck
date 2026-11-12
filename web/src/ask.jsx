// Ask chats view for the Models tab. The operator's compatible Ask service
// decides what to read, runs the answer, and checks the quotes
// (/api/ask relays to it); this screen is only the phone UI. The answering
// box id always comes from server-side configuration — the client never
// sends an upstream URL or box.
import { useEffect, useRef, useState } from "preact/hooks";
import { Markdown } from "./markdown.jsx";
import "./markdown.css";
import "./ask.css";

const SIZES = [["16K", 16384], ["32K", 32768], ["64K", 65536], ["128K", 131072]];
const PROJECT_KEY = "opsdeck-ask-project";
const SIZE_KEY = "opsdeck-ask-size";
const ANSWERS_KEY = "opsdeck-ask-answers";
const FIT_FRESH_MS = 60_000;

const load = (key, fallback) => {
  try {
    const raw = localStorage.getItem(key);
    return raw == null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
};
const save = (key, value) => {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
};

const kilo = (n) => (n >= 1000 ? `${Math.round(n / 1000)}K` : `${n || 0}`);
const day = (epoch) =>
  new Date(epoch * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" });
const clock = () =>
  new Date().toLocaleString("en-US", { hour: "numeric", minute: "2-digit", weekday: "short" });
const ago = (epoch) => {
  const s = Date.now() / 1000 - epoch;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
};
const stripThink = (text) =>
  String(text || "").replace(/<think>[\s\S]*?<\/think>/g, "").replace(/^[\s\S]*<\/think>/, "").trim();

function describe(project, fit) {
  if (!fit) return "";
  const r = fit.reach || {};
  const n = (r.conversations || []).length || (fit.files || []).length;
  if (!n) return `No chats found in ${project}.`;
  const chats = `${n} chat${n === 1 ? "" : "s"}`;
  if (r.whole) return `Reads all of ${project}: ${chats} back to ${day(r.earliest)} · ${kilo(fit.tokens)}`;
  return `Reads ${chats} back to ${day(r.since)} · ${kilo(fit.tokens)}`;
}

export function AskScreen({ token, onUnauthorized, active, box }) {
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState(() => load(PROJECT_KEY, ""));
  const [size, setSize] = useState(() => load(SIZE_KEY, 32768));
  const [fit, setFit] = useState(null);
  const [fitAt, setFitAt] = useState(0);
  const [fitError, setFitError] = useState("");
  const [ready, setReady] = useState(null);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState("");
  const [answers, setAnswers] = useState(() => load(ANSWERS_KEY, []));
  const [copied, setCopied] = useState(-1);
  const fitToken = useRef(0);

  const call = async (path, body) => {
    const response = await fetch(`/api/ask/${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
    if (response.status === 401) {
      onUnauthorized();
      throw new Error("Unauthorized");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
    return data;
  };

  useEffect(() => {
    call("projects")
      .then((data) => {
        setProjects(data.projects || []);
        if (!project && data.projects?.length) setProject(data.projects[0].name);
      })
      .catch((error) => setFitError(error.message));
    const check = () =>
      call("ready")
        .then((s) => setReady(Boolean(s.current || s.loadedModel || s.ok)))
        .catch(() => setReady(false));
    check();
    const timer = setInterval(check, 30_000);
    return () => clearInterval(timer);
  }, []);

  const refit = async () => {
    if (!project) return null;
    const mine = ++fitToken.current;
    setFitError("");
    try {
      // No box is sent: the server always uses its configured box id.
      const result = await call("fit", { project, size });
      if (mine !== fitToken.current) return null;
      setFit(result);
      setFitAt(Date.now());
      return result;
    } catch (error) {
      if (mine === fitToken.current) setFitError(error.message);
      return null;
    }
  };

  useEffect(() => {
    save(PROJECT_KEY, project);
    save(SIZE_KEY, size);
    setFit(null);
    // Mounted hidden on every app open; only size the read once the tab is on screen.
    if (active) refit();
  }, [project, size, active]);

  const ask = async () => {
    const q = question.trim();
    if (!q || busy) return;
    const started = Date.now();
    setBusy("Reading the chats…");
    const tick = setInterval(() => setBusy(`Reading the chats… ${Math.round((Date.now() - started) / 1000)}s`), 1000);
    let entry;
    try {
      const current = fit && Date.now() - fitAt < FIT_FRESH_MS ? fit : await refit();
      if (!current || !current.params || current.empty) throw new Error("Nothing to read for that project.");
      const { job } = await call("ask", { ...current.params, project, size, thread: [], question: q, maxTokens: 4096 });
      let result = null;
      while (!result) {
        await new Promise((r) => setTimeout(r, 1500));
        const v = await call(`job?id=${encodeURIComponent(job)}`);
        if (v.status === "done") result = v.result || {};
        else if (v.status === "error") throw new Error(v.result?.error || "The answer failed.");
      }
      const text = stripThink(result.text);
      if (!text) throw new Error("The model came back empty.");
      const t = result.timings || {};
      const speed = Math.round(t.predicted_per_second || (result.seconds && result.outputTokens ? result.outputTokens / result.seconds : 0));
      entry = {
        q, a: text, project, at: clock(), quotes: result.quoteCheck || "",
        meta: [result.seconds ? `${Math.round(result.seconds)}s` : "", result.promptTokens ? `read ${kilo(result.promptTokens)}` : "", speed ? `${speed} tok/s` : ""].filter(Boolean).join(" · "),
      };
      setQuestion("");
    } catch (error) {
      entry = { q, a: error.message || "That didn't work.", project, at: clock(), error: true };
    } finally {
      clearInterval(tick);
      setBusy("");
    }
    setAnswers((prev) => {
      const next = [entry, ...prev].slice(0, 30);
      save(ANSWERS_KEY, next);
      return next;
    });
  };

  const copy = async (index, text) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(index);
      setTimeout(() => setCopied(-1), 1500);
    } catch {}
  };

  return (
    <div class="ask">
      <div class="ask-row">
        <select class="ask-project" value={project} onChange={(e) => setProject(e.currentTarget.value)}>
          {!projects.length && project && <option value={project}>{project}</option>}
          {projects.map((p) => (
            <option key={p.name} value={p.name}>{`${p.name} · ${ago(p.lastActive)}`}</option>
          ))}
        </select>
        <span class={`ask-ready ${ready ? "on" : ready === false ? "off" : ""}`} title={`${box || "answering"} model`}>
          {box || "…"}
        </span>
      </div>
      <div class="ask-sizes" role="radiogroup" aria-label="How much to read">
        {SIZES.map(([label, value]) => (
          <button key={value} class={size === value ? "selected" : ""} onClick={() => setSize(value)}>{label}</button>
        ))}
      </div>
      <p class={`ask-reach ${fitError ? "err" : ""}`}>
        {fitError || (fit ? describe(project, fit) : project ? "Working out what to read…" : "")}
      </p>
      <textarea
        class="ask-q"
        rows={3}
        placeholder={`Ask about ${project || "a project"}…`}
        value={question}
        onInput={(e) => setQuestion(e.currentTarget.value)}
      />
      <button class="ask-go" disabled={!question.trim() || Boolean(busy) || !project} onClick={ask}>
        {busy || "Ask"}
      </button>
      {ready === false && <p class="ask-reach err">The answering model isn't up.</p>}

      <div class="ask-answers">
        {answers.map((item, index) => (
          <article key={`${item.at}-${index}`} class={`ask-answer ${item.error ? "err" : ""}`}>
            <div class="ask-answer-q">{item.q}</div>
            <div class="ask-answer-a">{item.error ? item.a : <Markdown text={item.a} />}</div>
            {item.quotes && <div class="ask-answer-quotes">{item.quotes}</div>}
            <div class="ask-answer-foot">
              <span>{[item.project, item.at, item.meta].filter(Boolean).join(" · ")}</span>
              {!item.error && (
                <button onClick={() => copy(index, item.a)}>{copied === index ? "Copied" : "Copy"}</button>
              )}
            </div>
          </article>
        ))}
        {answers.length > 0 && (
          <button class="ask-clear" onClick={() => { setAnswers([]); save(ANSWERS_KEY, []); }}>Clear answers</button>
        )}
      </div>
    </div>
  );
}
