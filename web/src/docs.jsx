import { useEffect, useRef, useState } from "preact/hooks";
import { Markdown } from "./markdown.jsx";
import "./markdown.css";
import "./docs.css";
import { friendlyError, timeAgo } from "./util.js";

const LAST_DOC_KEY = "opsdeck-last-doc";

const SAMPLE_DOC = "# Your docs\n\nSet `OPSDECK_DOCS_DIR` to a dedicated folder of Markdown files on the server. Files in that folder will appear here.\n\n- Only people with your Opsdeck token can read them.\n- Keep private files outside the configured folder.\n";

function readerText(doc) {
  const heading = /^(?:\r?\n)*#\s+([^\r\n]+)(?:\r?\n|$)/.exec(doc.text);
  return heading && heading[1].trim() === doc.title
    ? doc.text.slice(heading[0].length)
    : doc.text;
}

// Docs tab (S4): when the server has OPSDECK_DOCS_DIR set, list its markdown
// files and read them; otherwise the sample doc stays as the offline
// fallback. The last-opened doc is remembered and reopened.
export function DocsScreen({ token, onUnauthorized, active }) {
  const [listing, setListing] = useState(null); // null = loading; {configured, docs}
  const [listError, setListError] = useState("");
  const [doc, setDoc] = useState(null); // {path, title, text, loading}
  const [docError, setDocError] = useState("");
  const tokenGeneration = useRef(0);

  useEffect(() => {
    tokenGeneration.current += 1;
    setListing(null);
    setDoc(null);
    setListError("");
    setDocError("");
    if (!token) return;
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch("/api/docs", {
          headers: { Authorization: `Bearer ${token}` },
          cache: "no-store",
        });
        if (response.status === 401) {
          onUnauthorized();
          return;
        }
        const data = await response.json().catch(() => null);
        if (!response.ok) {
          throw new Error(
            (data?.detail && (typeof data.detail === "string" ? data.detail : data.detail.message))
            || `Docs unavailable (${response.status})`,
          );
        }
        if (!cancelled) {
          setListing({
            configured: Boolean(data?.configured),
            docs: Array.isArray(data?.docs) ? data.docs : [],
          });
        }
      } catch (requestError) {
        if (!cancelled) {
          setListError(friendlyError(requestError.message));
          setListing({ configured: false, docs: [] });
        }
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const openDoc = async (path) => {
    const generation = tokenGeneration.current;
    setDoc({ path, title: "", text: "", loading: true });
    setDocError("");
    try {
      localStorage.setItem(LAST_DOC_KEY, path);
    } catch {
      // best-effort memory
    }
    try {
      const response = await fetch(
        `/api/docs/content?path=${encodeURIComponent(path)}`,
        { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" },
      );
      if (response.status === 401) {
        onUnauthorized();
        return;
      }
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error((data?.detail) || `Doc unavailable (${response.status})`);
      }
      if (generation === tokenGeneration.current) {
        setDoc({ path, title: data?.title || path, text: data?.text || "", loading: false });
      }
    } catch (requestError) {
      if (generation === tokenGeneration.current) {
        setDocError(friendlyError(requestError.message));
        setDoc(null);
      }
    }
  };

  // Reopen the remembered doc once the listing lands.
  useEffect(() => {
    if (!listing?.configured || doc || listing.docs.length === 0) return;
    let last = "";
    try {
      last = localStorage.getItem(LAST_DOC_KEY) || "";
    } catch {
      // no memory — just show the list
    }
    if (last && listing.docs.some((entry) => entry.path === last)) {
      openDoc(last);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listing]);

  const showSample = !token || Boolean(listError) || (listing !== null && !listing.configured);
  const sampleNote = !token
    ? "Locked — enter the access token to load server docs."
    : listError
      ? `${listError} — showing the sample.`
      : "Set OPSDECK_DOCS_DIR on the server to show your docs — showing the sample.";

  return (
    <main class={`fleet screen ${active ? "" : "tab-hidden"}`}>
      {doc ? (
        <>
          <header class="topbar docs-topbar">
            <button
              type="button"
              class="icon-button"
              onClick={() => { setDoc(null); setDocError(""); }}
              aria-label="Back to docs list"
            >‹</button>
            <div>
              <p class="eyebrow">DOCS</p>
              <h1>{doc.title || doc.path}</h1>
            </div>
          </header>
          {doc.loading
            ? <div class="loading">Reading doc…</div>
            : <Markdown text={readerText(doc)} />}
        </>
      ) : showSample ? (
        <>
          <header class="topbar">
            <div>
              <p class="eyebrow">OPSDECK</p>
              <h1>Docs</h1>
            </div>
          </header>
          <p class="docs-note">{sampleNote}</p>
          <Markdown text={SAMPLE_DOC} />
        </>
      ) : (
        <>
          <header class="topbar">
            <div>
              <p class="eyebrow">OPSDECK</p>
              <h1>Docs</h1>
            </div>
          </header>
          {docError && <p class="docs-error" role="alert">{docError}</p>}
          {listing === null && <div class="loading">Reading docs…</div>}
          {listing !== null && listing.docs.length === 0 && (
            <p class="docs-note">No markdown files found in the docs directory.</p>
          )}
          {listing !== null && listing.docs.length > 0 && (
            <div class="compact-list docs-list">
              {listing.docs.map((entry) => (
                <button
                  type="button"
                  class="doc-row"
                  key={entry.path}
                  onClick={() => openDoc(entry.path)}
                  aria-label={`Open ${entry.title || entry.path}`}
                >
                  <span class="doc-title">{entry.title || entry.path}</span>
                  <span class="doc-path">{entry.path}</span>
                  <span class="doc-time">{timeAgo(entry.updated_at)}</span>
                </button>
              ))}
            </div>
          )}
        </>
      )}
    </main>
  );
}
