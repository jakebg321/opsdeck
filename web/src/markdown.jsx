// Phone-first markdown renderer. Hand-rolled, no dependencies, offline-safe.
// Safety: builds preact vnodes from plain strings only — preact escapes
// text children, so raw HTML in source is never injected as markup.

import { useState } from "preact/hooks";
import { copyText } from "./util.js";

function safeHref(value) {
  try {
    const url = new URL(value, window.location.href);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? value : null;
  } catch {
    return null;
  }
}

const KEYWORDS =
  /\b(const|let|var|function|return|if|else|for|while|import|from|export|default|class|extends|new|await|async|try|catch|throw|switch|case|break|continue|def|pass|None|True|False|in|is|not|and|or|with|as|lambda|struct|impl|fn|mut|pub|match|use|type|interface|null|true|false|nil|do|end|then|echo|fi|done|local|select|where|insert|update|delete|create|table)\b/;

// Split a code line into highlighted <span> tokens. Text stays strings,
// so preact escapes it; only our own span wrappers become markup.
function highlightLine(line, keyPrefix) {
  const pattern =
    /(\/\/[^\n]*|#[^\n]*|--[^\n]*|\/\*[\s\S]*?\*\/|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`|\b\d[\d_]*(?:\.\d+)?\b|\b(?:const|let|var|function|return|if|else|for|while|import|from|export|default|class|extends|new|await|async|try|catch|throw|switch|case|break|continue|def|pass|None|True|False|in|is|not|and|or|with|as|lambda|struct|impl|fn|mut|pub|match|use|type|interface|null|true|false|nil|do|end|then|echo|fi|done|local|select|where|insert|update|delete|create|table)\b)/g;
  const out = [];
  let last = 0;
  let match;
  let n = 0;
  const push = (text) => {
    if (text) out.push(text);
  };
  while ((match = pattern.exec(line)) !== null) {
    if (match.index > last) push(line.slice(last, match.index));
    const tok = match[0];
    let cls = "md-tok-num";
    if (/^(\/\/|#|--|\/\*)/.test(tok)) cls = "md-tok-com";
    else if (/^["'`]/.test(tok)) cls = "md-tok-str";
    else if (!KEYWORDS.test(tok)) cls = "md-tok-num";
    else cls = "md-tok-kw";
    out.push(<span class={cls} key={`${keyPrefix}-${n++}`}>{tok}</span>);
    last = match.index + tok.length;
  }
  push(line.slice(last));
  return out.length ? out : [line];
}

function CodeBlock({ lang, code }) {
  const [copied, setCopied] = useState(false);
  const lines = String(code).replace(/\n$/, "").split("\n");
  const copy = async () => {
    if (await copyText(code)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    }
  };
  return (
    <div class="md-codewrap">
      <div class="md-codehead">
        <span class="md-codelang">{lang || "code"}</span>
        <button
          type="button"
          class="md-copy"
          onClick={copy}
          aria-label={copied ? "Copied" : "Copy code"}
        >{copied ? "copied ✓" : "copy"}</button>
      </div>
      <pre class="md-code"><code>{lines.map((line, i) => (
        <span class="md-codeline" key={i}>{highlightLine(line, `l${i}`)}{i < lines.length - 1 ? "\n" : ""}</span>
      ))}</code></pre>
    </div>
  );
}

// Inline tokenizer: `code`, [link](url), **bold**, *italic*. Returns vnodes/strings.
function inline(text, keyPrefix = "i") {
  const out = [];
  // code, link, bold, italic — one pass, leftmost match wins
  const pattern = /(`[^`\n]+`|\[[^\]]+\]\([^)\s]+\)|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_)/g;
  let last = 0;
  let match;
  let n = 0;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const tok = match[0];
    const k = `${keyPrefix}-${n++}`;
    if (tok.startsWith("`")) {
      out.push(<code class="md-inlinecode" key={k}>{tok.slice(1, -1)}</code>);
    } else if (tok.startsWith("[")) {
      const m = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(tok);
      if (m) {
        const href = safeHref(m[2]);
        out.push(href
          ? <a href={href} target="_blank" rel="noopener noreferrer" key={k}>{m[1]}</a>
          : m[1]);
      } else {
        out.push(tok);
      }
    } else if (tok.startsWith("**") || tok.startsWith("__")) {
      out.push(<strong key={k}>{tok.slice(2, -2)}</strong>);
    } else {
      out.push(<em key={k}>{tok.slice(1, -1)}</em>);
    }
    last = match.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function parseListItem(text) {
  // Task list: [ ] / [x]
  const task = /^\[([ xX])\]\s+(.*)$/.exec(text);
  if (task) {
    return {
      checkbox: <input type="checkbox" checked={task[1].toLowerCase() === "x"} disabled readOnly tabIndex={-1} />,
      content: task[2],
    };
  }
  return { checkbox: null, content: text };
}

function ListItems({ items, ordered, keyPrefix }) {
  return items.map((item, i) => {
    const { checkbox, content } = parseListItem(item.text);
    return (
      <li key={`${keyPrefix}-${i}`}>
        {checkbox}
        {inline(content, `${keyPrefix}-${i}`)}
        {item.children && item.children.length > 0 && (
          <ul>{item.children.map((child, j) => {
            const sub = parseListItem(child);
            return (
              <li key={`${keyPrefix}-${i}-${j}`}>
                {sub.checkbox}
                {inline(sub.content, `${keyPrefix}-${i}-${j}`)}
              </li>
            );
          })}</ul>
        )}
      </li>
    );
  });
}

function isTableDelim(line) {
  return /^\s*\|?[\s:|-]+\|[\s:|.-]*$/.test(line) && /-/.test(line);
}

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((cell) => cell.trim());
}

// Block parser: lines -> preact vnodes. One level of list nesting.
function blocks(src) {
  const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  let n = 0;
  const key = () => `b${n++}`;

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block
    const fence = /^```\s*([\w+-]*)\s*$/.exec(line);
    if (fence) {
      const lang = fence[1] || "";
      const buf = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++; // skip closing fence (or EOF)
      out.push(<CodeBlock lang={lang} code={buf.join("\n")} key={key()} />);
      continue;
    }

    // ATX heading
    const heading = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      const Tag = `h${level}`;
      out.push(<Tag class={`md-h md-h${level}`} key={key()}>{inline(heading[2], `h${n}`)}</Tag>);
      i++;
      continue;
    }

    // Horizontal rule
    if (/^\s*([-*_]\s*){3,}$/.test(line)) {
      out.push(<hr class="md-hr" key={key()} />);
      i++;
      continue;
    }

    // Blockquote: group consecutive > lines
    if (/^\s*>/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*> ?/, ""));
        i++;
      }
      out.push(<blockquote class="md-quote" key={key()}>{inline(buf.join(" "), `q${n}`)}</blockquote>);
      continue;
    }

    // GitHub-style table
    if (line.includes("|") && i + 1 < lines.length && isTableDelim(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      const k = key();
      out.push(
        <div class="md-tablewrap" key={k}>
          <table class="md-table">
            <thead><tr>{head.map((cell, c) => <th key={c} scope="col">{inline(cell, `${k}-h${c}`)}</th>)}</tr></thead>
            <tbody>{rows.map((row, r) => (
              <tr key={r}>{row.map((cell, c) => <td key={c}>{inline(cell, `${k}-${r}-${c}`)}</td>)}</tr>
            ))}</tbody>
          </table>
        </div>,
      );
      continue;
    }

    // Lists (bulleted or numbered), one nested level via 2-space indent
    const listStart = /^\s*(?:[-*+]\s+|\d+[.)]\s+)/.test(line);
    if (listStart) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items = [];
      while (i < lines.length && /^\s*(?:[-*+]\s+|\d+[.)]\s+|\[)/.test(lines[i]) && lines[i].trim()) {
        const top = /^(?:[-*+]\s+|\d+[.)]\s+)(.*)$/.exec(lines[i].trim());
        if (!top) break;
        const item = { text: top[1], children: [] };
        i++;
        while (
          i < lines.length &&
          /^(?:\s{2,}|\t)(?:[-*+]\s+|\d+[.)]\s+)/.test(lines[i])
        ) {
          const sub = /^(?:\s{2,}|\t)(?:[-*+]\s+|\d+[.)]\s+)(.*)$/.exec(lines[i]);
          item.children.push(sub[1]);
          i++;
        }
        items.push(item);
      }
      const k = key();
      const Tag = ordered ? "ol" : "ul";
      out.push(
        <Tag class={ordered ? "md-ol" : "md-ul"} key={k}>
          <ListItems items={items} ordered={ordered} keyPrefix={k} />
        </Tag>,
      );
      continue;
    }

    // Blank line: skip
    if (!line.trim()) {
      i++;
      continue;
    }

    // Paragraph: group consecutive plain lines
    const buf = [line];
    i++;
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,6}\s+|```|>|(\s*([-*_]\s*){3,}\s*)|(\s*([-*+]\s+|\d+[.)]\s+)))/.test(lines[i]) &&
      !(lines[i].includes("|") && i + 1 < lines.length && isTableDelim(lines[i + 1]))
    ) {
      buf.push(lines[i].trim());
      i++;
    }
    out.push(<p class="md-p" key={key()}>{inline(buf.join(" "), `p${n}`)}</p>);
  }
  return out;
}

export function Markdown({ text }) {
  return <div class="md">{blocks(text)}</div>;
}

export function MarkdownBlock({ text }) {
  return <Markdown text={text} />;
}
