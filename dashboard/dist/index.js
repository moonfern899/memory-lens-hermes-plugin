// Memory Lens — Hermes dashboard plugin (IIFE bundle)
//
// A tab that inspects ~/.hermes/memories/{MEMORY,USER}.md: shows
// char-limit pressure gauges, parsed entries, a snapshot timeline with
// diff view, a one-shot capture composer, and a raw editor.
//
// SDK Tabs primitive was flaky in the tested version; plain button strip is reliable.

(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) {
    console.error("[memory-lens] SDK missing");
    return;
  }
  const { React } = SDK;
  const { useState, useEffect, useMemo, useCallback, useRef } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button } =
    SDK.components;
  const cn = (SDK.utils && SDK.utils.cn) || ((...a) => a.filter(Boolean).join(" "));
  const timeAgo =
    (SDK.utils && SDK.utils.timeAgo) ||
    ((ts) => {
      const d = Math.max(0, Date.now() / 1000 - ts);
      if (d < 60) return Math.floor(d) + "s";
      if (d < 3600) return Math.floor(d / 60) + "m";
      if (d < 86400) return Math.floor(d / 3600) + "h";
      return Math.floor(d / 86400) + "d";
    });

  const API = "/api/plugins/memory-lens";
  const h = React.createElement;
  const ENTRY_DELIM_VIEW = "\n--- § ---\n";

  // -------- error boundary --------
  class ErrorBoundary extends React.Component {
    constructor(props) {
      super(props);
      this.state = { err: null };
    }
    static getDerivedStateFromError(err) {
      return { err };
    }
    componentDidCatch(err, info) {
      console.error("[memory-lens] render error:", err, info);
    }
    render() {
      if (this.state.err) {
        return h(
          "div",
          { className: "memlens-root" },
          h(
            "div",
            { className: "memlens-err" },
            h("strong", null, "Memory Lens crashed: "),
            String(this.state.err && (this.state.err.message || this.state.err)),
            "\n\n",
            (this.state.err && this.state.err.stack) || "",
          ),
        );
      }
      return this.props.children;
    }
  }

  // -------- helpers --------
  function fmtPct(p) {
    return Math.round((p || 0) * 100) + "%";
  }
  function pressureColor(p) {
    if (p >= 0.9) return "var(--color-destructive, #c46550)";
    if (p >= 0.7) return "var(--color-warning, #d9b66e)";
    return "var(--color-primary, #2a3e6e)";
  }

  function diffLines(a, b) {
    const al = (a || "").split("\n");
    const bl = (b || "").split("\n");
    const n = al.length;
    const m = bl.length;
    if (n + m > 4000) return [{ kind: "same", text: "(diff too large)" }];
    // Compare with trailing whitespace stripped so spurious trailing-space
    // differences don't fragment the diff into noise. Rendering still uses
    // the original line text, so any real visible change is preserved.
    const eq = (x, y) => x.replace(/\s+$/, "") === y.replace(/\s+$/, "");
    const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
    for (let i = 1; i <= n; i++) {
      for (let j = 1; j <= m; j++) {
        dp[i][j] = eq(al[i - 1], bl[j - 1])
          ? dp[i - 1][j - 1] + 1
          : Math.max(dp[i - 1][j], dp[i][j - 1]);
      }
    }
    const out = [];
    let i = n;
    let j = m;
    while (i > 0 && j > 0) {
      if (eq(al[i - 1], bl[j - 1])) {
        out.unshift({ kind: "same", text: al[i - 1] });
        i--;
        j--;
      } else if (dp[i - 1][j] >= dp[i][j - 1]) {
        out.unshift({ kind: "del", text: al[i - 1] });
        i--;
      } else {
        out.unshift({ kind: "add", text: bl[j - 1] });
        j--;
      }
    }
    while (i > 0) out.unshift({ kind: "del", text: al[--i] });
    while (j > 0) out.unshift({ kind: "add", text: bl[--j] });
    return out;
  }

  // -------- subcomponents --------
  function PressureGauge({ label, summary }) {
    const used = summary.chars_used || 0;
    const limit = summary.char_limit || 1;
    const p = Math.min(1, used / limit);
    return h(
      Card,
      null,
      h(
        CardHeader,
        null,
        h(
          "div",
          {
            style: {
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "0.5rem",
            },
          },
          h(CardTitle, null, label),
          h(
            Badge,
            { variant: p >= 0.9 ? "destructive" : "secondary" },
            used + " / " + limit + " chars",
          ),
        ),
      ),
      h(
        CardContent,
        null,
        h(
          "div",
          { className: "memlens-bar" },
          h("div", {
            className: "memlens-bar-fill",
            style: { width: fmtPct(p), background: pressureColor(p) },
          }),
        ),
        h(
          "div",
          {
            style: {
              display: "flex",
              justifyContent: "space-between",
              fontSize: "0.72rem",
              marginTop: "0.4rem",
              opacity: 0.7,
            },
          },
          h("span", null, fmtPct(p) + " of limit"),
          h(
            "span",
            null,
            (summary.entries || []).length +
              " entries · " +
              (summary.mtime ? timeAgo(summary.mtime) + " ago" : "never written"),
          ),
        ),
      ),
    );
  }

  function EntryCard({ entry, limit, target, entries, onSaved }) {
    const [expanded, setExpanded] = useState(false);
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(entry.body);
    const [saving, setSaving] = useState(false);
    const [editError, setEditError] = useState(null);
    const [confirmingDelete, setConfirmingDelete] = useState(false);
    const isLong = entry.chars > 220;
    const liveChars = editing ? draft.length : entry.chars;
    const pct = Math.min(1, liveChars / limit);

    const startEdit = () => {
      setDraft(entry.body);
      setEditError(null);
      setEditing(true);
    };
    const cancel = () => {
      setEditing(false);
      setEditError(null);
    };
    const save = async () => {
      const next = draft.replace(/^\n+/, "").replace(/\n+$/, "");
      if (!next.trim()) {
        setEditError("Entry can't be empty. Use Delete instead.");
        return;
      }
      if (next.includes("\n§\n") || next.trim() === "§") {
        setEditError("Entry contains the section delimiter (§). Use the raw editor for multi-entry edits.");
        return;
      }
      const rebuilt = (entries || [])
        .map((e) => (e.index === entry.index ? next : e.body))
        .join("\n§\n") + "\n";
      setSaving(true);
      try {
        await SDK.fetchJSON(API + "/raw-write", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target, content: rebuilt }),
        });
        setEditing(false);
        if (onSaved) await onSaved();
      } catch (err) {
        setEditError(String((err && err.message) || err));
      } finally {
        setSaving(false);
      }
    };
    const remove = async () => {
      const remaining = (entries || []).filter((e) => e.index !== entry.index);
      const rebuilt = remaining.length
        ? remaining.map((e) => e.body).join("\n§\n") + "\n"
        : "";
      setSaving(true);
      setEditError(null);
      try {
        await SDK.fetchJSON(API + "/raw-write", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target, content: rebuilt }),
        });
        setConfirmingDelete(false);
        if (onSaved) await onSaved();
      } catch (err) {
        setEditError(String((err && err.message) || err));
      } finally {
        setSaving(false);
      }
    };

    return h(
      "div",
      { className: "memlens-entry" },
      h(
        "div",
        { className: "memlens-entry-head" },
        h(Badge, { variant: "outline" }, "#" + (entry.index + 1)),
        h(
          "span",
          { className: "memlens-entry-chars" },
          liveChars + " chars",
        ),
        h(
          "div",
          { className: "memlens-entry-mini" },
          h("div", {
            className: "memlens-entry-mini-fill",
            style: { width: fmtPct(pct), background: pressureColor(pct) },
          }),
        ),
        !editing && !confirmingDelete &&
          target &&
          h(
            "div",
            { style: { marginLeft: "auto", display: "flex", gap: "0.5rem" } },
            h(
              "button",
              { className: "memlens-link", onClick: startEdit, disabled: saving },
              "Edit",
            ),
            h(
              "button",
              { className: "memlens-link", onClick: () => setConfirmingDelete(true), disabled: saving },
              "Delete",
            ),
          ),
      ),
      editing
        ? h(
            "div",
            { className: "memlens-entry-edit" },
            h("textarea", {
              className: "memlens-textarea",
              value: draft,
              onChange: (e) => setDraft(e.target.value),
              rows: Math.max(4, Math.min(20, draft.split("\n").length + 1)),
              disabled: saving,
              autoFocus: true,
            }),
            editError && h("div", { className: "memlens-err" }, editError),
            h(
              "div",
              { style: { display: "flex", gap: "0.5rem", marginTop: "0.4rem" } },
              h(Button, { onClick: save, disabled: saving }, saving ? "Saving..." : "Save"),
              h(Button, { variant: "outline", onClick: cancel, disabled: saving }, "Cancel"),
            ),
          )
        : confirmingDelete
        ? h(
            "div",
            { className: "memlens-entry-confirm" },
            h(
              "div",
              { className: "memlens-entry-confirm-msg" },
              "Sure you want to delete this entry?",
            ),
            h(
              "div",
              { className: "memlens-entry-confirm-sub" },
              "If you change your mind later, restore it from the Timeline tab.",
            ),
            editError && h("div", { className: "memlens-err" }, editError),
            h(
              "div",
              { style: { display: "flex", gap: "0.5rem", marginTop: "0.6rem" } },
              h(Button, { onClick: remove, disabled: saving }, saving ? "Deleting..." : "Delete"),
              h(Button, { variant: "outline", onClick: () => { setConfirmingDelete(false); setEditError(null); }, disabled: saving }, "Cancel"),
            ),
          )
        : h(
            "pre",
            {
              className: cn(
                "memlens-entry-body",
                !expanded && isLong && "memlens-entry-body--collapsed",
              ),
            },
            entry.body,
          ),
      !editing && !confirmingDelete && isLong &&
        h(
          "button",
          {
            className: "memlens-link",
            onClick: () => setExpanded((v) => !v),
          },
          expanded ? "Collapse" : "Expand",
        ),
    );
  }

  function FileSection({ summary, label, query, target, onSaved }) {
    const filtered = useMemo(() => {
      if (!query) return summary.entries || [];
      const q = query.toLowerCase();
      return (summary.entries || []).filter((e) =>
        (e.body || "").toLowerCase().includes(q),
      );
    }, [summary.entries, query]);
    return h(
      Card,
      null,
      h(
        CardHeader,
        null,
        // Filename label rendered as a plain styled div — keeps the
        // `.md` extension lowercase regardless of the active theme.
        h(
          "div",
          { className: "memlens-file-title memlens-mono" },
          label,
        ),
        h(
          "div",
          {
            className: "memlens-mono",
            style: { fontSize: "0.7rem", opacity: 0.6, marginTop: "0.25rem" },
          },
          summary.path,
        ),
      ),
      h(
        CardContent,
        null,
        filtered.length === 0
          ? h(
              "div",
              { className: "memlens-empty" },
              query
                ? 'No entries match "' + query + '".'
                : summary.exists
                  ? "File exists but contains no parsable entries."
                  : "File does not exist yet — Hermes creates it on first agent write.",
            )
          : filtered.map((entry, idx) =>
              h(EntryCard, {
                key: entry.index + ":" + idx,
                entry,
                limit: summary.char_limit,
                target,
                entries: summary.entries,
                onSaved,
              }),
            ),
      ),
    );
  }

  function CaptureComposer({ onCaptured }) {
    const [target, setTarget] = useState("memory");
    const [content, setContent] = useState("");
    const [busy, setBusy] = useState(false);
    const [msg, setMsg] = useState(null);

    const submit = useCallback(async () => {
      if (!content.trim()) return;
      setBusy(true);
      setMsg(null);
      try {
        const res = await SDK.fetchJSON(API + "/capture", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target, content }),
        });
        setContent("");
        setMsg({
          kind: res.over_limit ? "warn" : "ok",
          text: res.over_limit
            ? "Saved (" +
              res.chars_used +
              " chars) — file is now over its " +
              res.char_limit +
              "-char limit. Hermes' agent may compress on next write."
            : "Saved. " + res.warning,
        });
        if (onCaptured) onCaptured();
      } catch (err) {
        setMsg({ kind: "err", text: String((err && err.message) || err) });
      } finally {
        setBusy(false);
      }
    }, [target, content, onCaptured]);

    return h(
      Card,
      null,
      h(CardHeader, null, h(CardTitle, null, "Capture a memory")),
      h(
        CardContent,
        null,
        h(
          "div",
          { className: "memlens-banner" },
          "Hermes only reads memory at session start. Entries you capture here are visible to your ",
          h("strong", null, "next"),
          " session, not the current one.",
        ),
        h(
          "div",
          { style: { display: "flex", gap: "0.5rem", alignItems: "center", marginTop: "0.75rem" } },
          h(
            "label",
            { style: { fontSize: "0.7rem", textTransform: "uppercase", letterSpacing: "0.06em", opacity: 0.7 } },
            "Target",
          ),
          h(
            "select",
            {
              className: "memlens-select",
              value: target,
              onChange: (e) => setTarget(e.target.value),
            },
            h("option", { value: "memory" }, "MEMORY.md (project memory)"),
            h("option", { value: "user" }, "USER.md (user profile)"),
          ),
        ),
        h("textarea", {
          className: "memlens-textarea",
          rows: 5,
          placeholder:
            "Write the memory exactly as you want Hermes to see it.",
          value: content,
          onChange: (e) => setContent(e.target.value),
          style: { marginTop: "0.5rem" },
        }),
        h(
          "div",
          {
            style: {
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginTop: "0.5rem",
            },
          },
          h(
            "span",
            { style: { fontSize: "0.72rem", opacity: 0.7 } },
            content.length + " chars",
          ),
          h(
            Button,
            { onClick: submit, disabled: busy || !content.trim() },
            busy ? "Saving..." : "Save to memory",
          ),
        ),
        msg &&
          h(
            "div",
            {
              className: cn(
                "memlens-msg",
                msg.kind === "err" && "memlens-msg--err",
                msg.kind === "warn" && "memlens-msg--warn",
              ),
              style: { marginTop: "0.5rem" },
            },
            msg.text,
          ),
      ),
    );
  }

  function RawEditor({ state, onSaved }) {
    const [target, setTarget] = useState("memory");
    const [content, setContent] = useState("");
    const [original, setOriginal] = useState("");
    const [busy, setBusy] = useState(false);
    const [msg, setMsg] = useState(null);

    // Load file content from the raw-content endpoint (byte-faithful)
    // on mount and when target changes. Re-joining parsed entries
    // would drop blank lines and trailing whitespace.
    useEffect(() => {
      let cancelled = false;
      SDK.fetchJSON(API + "/raw-content?target=" + target)
        .then((res) => {
          if (cancelled) return;
          setContent(res.content || "");
          setOriginal(res.content || "");
          setMsg(null);
        })
        .catch((err) => {
          if (!cancelled) setMsg({ kind: "err", text: String((err && err.message) || err) });
        });
      return () => {
        cancelled = true;
      };
    }, [target]);

    const limit =
      target === "memory" ? state.memory.char_limit : state.user.char_limit;
    const dirty = content !== original;
    const overLimit = content.length > limit;

    const handleTargetChange = (e) => {
      const next = e.target.value;
      if (
        dirty &&
        !window.confirm(
          "You have unsaved changes. Switching files will discard them. Continue?",
        )
      ) {
        return;
      }
      setTarget(next);
    };

    const save = useCallback(async () => {
      setBusy(true);
      setMsg(null);
      try {
        const res = await SDK.fetchJSON(API + "/raw-write", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target, content }),
        });
        setOriginal(content);
        setMsg({
          kind: res.over_limit ? "warn" : "ok",
          text: res.over_limit
            ? "Saved (" +
              res.chars_used +
              " chars) — file is over its " +
              res.char_limit +
              "-char limit. Hermes' agent may compress on next write."
            : "Saved. " + res.warning,
        });
        if (onSaved) onSaved();
      } catch (err) {
        setMsg({ kind: "err", text: String((err && err.message) || err) });
      } finally {
        setBusy(false);
      }
    }, [target, content, onSaved]);

    const revert = () => {
      setContent(original);
      setMsg(null);
    };

    const textareaRef = useRef(null);
    const insertSeparator = () => {
      const ta = textareaRef.current;
      const start = ta ? ta.selectionStart : content.length;
      const end = ta ? ta.selectionEnd : content.length;
      const before = content.slice(0, start);
      const after = content.slice(end);
      // Make sure the § sits on its own line — that's what the parser
      // requires (\n§\n). Prepend/append newlines only if missing.
      const lead = before.length === 0 || before.endsWith("\n") ? "" : "\n";
      const trail = after.length === 0 || after.startsWith("\n") ? "" : "\n";
      const insert = lead + "§" + trail;
      const next = before + insert + after;
      setContent(next);
      setTimeout(() => {
        if (!ta) return;
        ta.focus();
        const pos = before.length + insert.length;
        ta.setSelectionRange(pos, pos);
      }, 0);
    };

    return h(
      Card,
      null,
      h(
        CardHeader,
        null,
        h(CardTitle, null, "Edit memory file"),
      ),
      h(
        CardContent,
        null,
        h(
          "div",
          { className: "memlens-banner" },
          "Editing the raw file overwrites all entries. A snapshot is taken automatically before saving so you can recover. Hermes only reads memory at session start, so changes apply on the ",
          h("strong", null, "next"),
          " session.",
        ),
        h(
          "div",
          {
            style: {
              display: "flex",
              gap: "0.5rem",
              alignItems: "center",
              marginTop: "0.75rem",
            },
          },
          h(
            "label",
            {
              style: {
                fontSize: "0.7rem",
                textTransform: "uppercase",
                letterSpacing: "0.06em",
                opacity: 0.7,
              },
            },
            "File",
          ),
          h(
            "select",
            {
              className: "memlens-select",
              value: target,
              onChange: handleTargetChange,
              style: { maxWidth: "320px" },
            },
            h("option", { value: "memory" }, "MEMORY.md (project memory)"),
            h("option", { value: "user" }, "USER.md (user profile)"),
          ),
        ),
        h("textarea", {
          ref: textareaRef,
          className: "memlens-textarea",
          rows: 16,
          placeholder:
            "(file is empty — type entries here, use Insert separator between them)",
          value: content,
          onChange: (e) => setContent(e.target.value),
          style: { marginTop: "0.5rem", minHeight: "300px" },
        }),
        h(
          "div",
          {
            style: {
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginTop: "0.5rem",
              gap: "0.5rem",
              flexWrap: "wrap",
            },
          },
          h(
            "span",
            {
              style: {
                fontSize: "0.72rem",
                opacity: 0.7,
                color: overLimit ? "var(--color-destructive)" : undefined,
              },
            },
            content.length + " / " + limit + " chars" + (overLimit ? " — over limit" : ""),
          ),
          h(
            "div",
            { style: { display: "flex", gap: "0.5rem" } },
            h(
              Button,
              { variant: "outline", onClick: insertSeparator, disabled: busy },
              "Insert separator",
            ),
            h(
              Button,
              { variant: "outline", onClick: revert, disabled: busy || !dirty },
              "Revert",
            ),
            h(
              Button,
              { onClick: save, disabled: busy || !dirty },
              busy ? "Saving..." : dirty ? "Save changes" : "No changes",
            ),
          ),
        ),
        h(
          "p",
          { style: { fontSize: "0.7rem", opacity: 0.6, marginTop: "0.4rem" } },
          "Separate entries with a line containing a single ",
          h("span", { className: "memlens-mono" }, "§"),
          " character — that's the delimiter Hermes' agent uses internally.",
        ),
        msg &&
          h(
            "div",
            {
              className: cn(
                "memlens-msg",
                msg.kind === "err" && "memlens-msg--err",
                msg.kind === "warn" && "memlens-msg--warn",
              ),
              style: { marginTop: "0.5rem" },
            },
            msg.text,
          ),
      ),
    );
  }

  // Inline trash-bin SVG — uses currentColor so it picks up the theme.
  const TrashIcon = () =>
    h(
      "svg",
      {
        viewBox: "0 0 16 16",
        width: 13,
        height: 13,
        fill: "none",
        stroke: "currentColor",
        strokeWidth: 1.4,
        strokeLinecap: "round",
        strokeLinejoin: "round",
        "aria-hidden": "true",
      },
      h("path", { d: "M2.5 4h11" }),
      h("path", { d: "M6 4V2.5h4V4" }),
      h("path", { d: "M3.5 4l.7 9a1 1 0 0 0 1 .9h5.6a1 1 0 0 0 1-.9l.7-9" }),
      h("path", { d: "M6.5 7v4M9.5 7v4" }),
    );

  function HistoryStrip({ history, picked, onPick, onDelete }) {
    return h(
      Card,
      null,
      h(CardHeader, null, h(CardTitle, null, "Snapshot timeline")),
      h(
        CardContent,
        null,
        history.length === 0
          ? h(
              "div",
              { className: "memlens-empty" },
              "No snapshots yet. One is taken automatically each time the memory files change. Click 'Snapshot now' above to take one manually.",
            )
          : h(
              "div",
              { className: "memlens-timeline" },
              history.map((s) =>
                h(
                  "div",
                  {
                    key: s.id,
                    className: cn(
                      "memlens-tl-item",
                      picked === s.id && "memlens-tl-item--active",
                    ),
                    title: new Date(s.ts * 1000).toLocaleString(),
                  },
                  // Card body is the click target for picking the snapshot.
                  h(
                    "button",
                    {
                      onClick: () => onPick(s.id),
                      className: "memlens-tl-pick",
                    },
                    h("div", { className: "memlens-tl-when" }, timeAgo(s.ts) + " ago"),
                    h("div", { className: "memlens-tl-reason" }, s.reason || "manual"),
                    h(
                      "div",
                      { className: "memlens-tl-counts" },
                      "M " + s.memory_chars + " · U " + s.user_chars,
                    ),
                  ),
                  // Trash button, bottom-right.
                  h(
                    "button",
                    {
                      onClick: (e) => {
                        e.stopPropagation();
                        onDelete(s.id);
                      },
                      className: "memlens-tl-delete",
                      title: "Delete this snapshot",
                      "aria-label": "Delete snapshot",
                    },
                    h(TrashIcon),
                  ),
                ),
              ),
            ),
      ),
    );
  }

  function DiffView({ before, after, label }) {
    const lines = useMemo(() => diffLines(before || "", after || ""), [before, after]);
    return h(
      Card,
      null,
      h(CardHeader, null, h(CardTitle, null, "Diff — " + label)),
      h(
        CardContent,
        null,
        h(
          "pre",
          { className: "memlens-diff" },
          lines.map((ln, i) =>
            h(
              "div",
              {
                key: i,
                className: cn(
                  "memlens-diff-line",
                  ln.kind === "add" && "memlens-diff-line--add",
                  ln.kind === "del" && "memlens-diff-line--del",
                ),
              },
              h(
                "span",
                { className: "memlens-diff-mark" },
                ln.kind === "add" ? "+" : ln.kind === "del" ? "−" : " ",
              ),
              h("span", null, ln.text || " "),
            ),
          ),
        ),
      ),
    );
  }

  // -------- main --------
  function MemoryLensPage() {
    const [state, setState] = useState(null);
    const [history, setHistory] = useState([]);
    const [pickedSnapId, setPickedSnapId] = useState(null);
    const [pickedSnap, setPickedSnap] = useState(null);
    const [view, setView] = useState("overview");
    const [query, setQuery] = useState("");
    const [error, setError] = useState(null);
    const [restoreOpen, setRestoreOpen] = useState(false);
    const [restoring, setRestoring] = useState(false);
    const [restoreError, setRestoreError] = useState(null);

    const load = useCallback(async () => {
      try {
        const s = await SDK.fetchJSON(API + "/state");
        const hist = await SDK.fetchJSON(API + "/history");
        setState(s);
        setHistory(((hist && hist.snapshots) || []).slice().reverse());
        setError(null);
      } catch (err) {
        setError(String((err && err.message) || err));
      }
    }, []);

    useEffect(() => {
      load();
      const t = setInterval(load, 4000);
      return () => clearInterval(t);
    }, [load]);

    useEffect(() => {
      // Reset the restore confirm UI whenever the picked snapshot changes,
      // so a stale "Restore both" panel never applies to a different snapshot.
      setRestoreOpen(false);
      setRestoreError(null);
      if (!pickedSnapId) {
        setPickedSnap(null);
        return;
      }
      SDK.fetchJSON(API + "/snapshot/" + pickedSnapId)
        .then(setPickedSnap)
        .catch((err) => setError(String((err && err.message) || err)));
    }, [pickedSnapId]);

    const restoreSnapshot = async (which) => {
      if (!pickedSnap) return;
      setRestoring(true);
      setRestoreError(null);
      try {
        const targets = which === "both" ? ["memory", "user"] : [which];
        for (const t of targets) {
          const src = pickedSnap[t];
          const ents = (src && src.entries) || [];
          const rebuilt = ents.length
            ? ents.map((e) => e.body).join("\n§\n") + "\n"
            : "";
          await SDK.fetchJSON(API + "/raw-write", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ target: t, content: rebuilt }),
          });
        }
        setRestoreOpen(false);
        await load();
      } catch (err) {
        setRestoreError(String((err && err.message) || err));
      } finally {
        setRestoring(false);
      }
    };

    if (error) {
      return h(
        "div",
        { className: "memlens-root" },
        h(
          Card,
          null,
          h(CardHeader, null, h(CardTitle, null, "Memory Lens — error")),
          h(
            CardContent,
            null,
            h("pre", { className: "memlens-err" }, error),
            h(
              "p",
              { style: { fontSize: "0.75rem", opacity: 0.7, marginTop: "0.5rem" } },
              "If you just installed the plugin, restart `hermes dashboard` so backend routes mount.",
            ),
          ),
        ),
      );
    }

    if (!state) {
      return h("div", { className: "memlens-loading" }, "Loading memory state...");
    }

    const tabBtn = (id, label) =>
      h(
        "button",
        {
          onClick: () => setView(id),
          className: cn("memlens-tab", view === id && "memlens-tab--active"),
        },
        label,
      );

    return h(
      "div",
      { className: "memlens-root" },
      // header
      h(
        "div",
        { className: "memlens-header" },
        h(
          "div",
          null,
          h("h1", { className: "memlens-h1" }, "Memory Lens"),
          h(
            "p",
            { className: "memlens-sub" },
            "Inspect Hermes' two flat memory files: ",
            h("span", { className: "memlens-mono" }, "MEMORY.md"),
            " and ",
            h("span", { className: "memlens-mono" }, "USER.md"),
            ". Char-limit pressure, per-entry breakdown, snapshot history, diffs.",
          ),
        ),
        h(
          Button,
          {
            variant: "outline",
            onClick: async () => {
              try {
                await SDK.fetchJSON(API + "/snapshot", { method: "POST" });
                load();
              } catch (err) {
                setError(String((err && err.message) || err));
              }
            },
          },
          "Snapshot now",
        ),
      ),
      // config banners — surface non-default setups so users aren't
      // confused when the gauges show data that Hermes won't actually use.
      state.config && state.config.provider && state.config.provider !== "builtin" &&
        h(
          "div",
          { className: "memlens-banner memlens-banner--warn" },
          h("strong", null, "External memory provider: "),
          h("span", { className: "memlens-mono" }, state.config.provider),
          ". Memory Lens reads the local builtin files only — your active memories live in the provider, not here.",
        ),
      state.config && state.config.memory_enabled === false &&
        h(
          "div",
          { className: "memlens-banner memlens-banner--warn" },
          h("strong", null, "Memory disabled. "),
          "Hermes won't read MEMORY.md at session start while ",
          h("span", { className: "memlens-mono" }, "memory.memory_enabled"),
          " is off. Existing entries are still on disk; new captures will be inert until you re-enable it.",
        ),
      state.config && state.config.user_profile_enabled === false &&
        h(
          "div",
          { className: "memlens-banner memlens-banner--warn" },
          h("strong", null, "User profile disabled. "),
          "Hermes won't read USER.md while ",
          h("span", { className: "memlens-mono" }, "memory.user_profile_enabled"),
          " is off.",
        ),

      // gauges
      h(
        "div",
        { className: "memlens-gauges" },
        h(PressureGauge, { label: "MEMORY.md", summary: state.memory }),
        h(PressureGauge, { label: "USER.md", summary: state.user }),
      ),
      // simple tab strip (no Radix Tabs primitive)
      h(
        "div",
        { className: "memlens-tabs" },
        tabBtn("overview", "Entries"),
        tabBtn("timeline", "Timeline"),
        tabBtn("capture", "Add entry"),
        tabBtn("raw", "Edit raw"),
      ),
      // body
      view === "overview" &&
        h(
          "div",
          { className: "memlens-body" },
          h(
            "div",
            { className: "memlens-search" },
            h("input", {
              type: "text",
              className: "memlens-input",
              placeholder: "Search entries...",
              value: query,
              onChange: (e) => setQuery(e.target.value),
            }),
          ),
          h(FileSection, { summary: state.memory, label: "MEMORY.md", query, target: "memory", onSaved: load }),
          h(FileSection, { summary: state.user, label: "USER.md", query, target: "user", onSaved: load }),
        ),
      view === "timeline" &&
        h(
          "div",
          { className: "memlens-body" },
          h(
            "div",
            { className: "memlens-legend" },
            h("strong", null, "Snapshot reasons: "),
            h("span", { className: "memlens-mono" }, "manual"),
            " (you clicked snapshot) · ",
            h("span", { className: "memlens-mono" }, "capture:X"),
            " (you added an entry via Add entry tab) · ",
            h("span", { className: "memlens-mono" }, "pre-raw-write:X"),
            " / ",
            h("span", { className: "memlens-mono" }, "raw-write:X"),
            " (you saved via Edit raw tab or per-entry Edit — old state then new state) · ",
            h("span", { className: "memlens-mono" }, "external-edit"),
            " (file changed outside the plugin — usually Hermes' agent, also vim/scripts).",
          ),
          h(HistoryStrip, {
            history,
            picked: pickedSnapId,
            onPick: setPickedSnapId,
            onDelete: async (id) => {
              try {
                await SDK.fetchJSON(API + "/snapshot/" + id, {
                  method: "DELETE",
                });
                if (pickedSnapId === id) setPickedSnapId(null);
                load();
              } catch (err) {
                setError(String((err && err.message) || err));
              }
            },
          }),
          pickedSnap &&
            h(
              "div",
              { className: "memlens-restore" },
              !restoreOpen
                ? h(
                    Button,
                    { variant: "outline", onClick: () => setRestoreOpen(true) },
                    "Restore from this snapshot",
                  )
                : h(
                    "div",
                    { className: "memlens-entry-confirm" },
                    h(
                      "div",
                      { className: "memlens-entry-confirm-msg" },
                      "Restore which file from this snapshot?",
                    ),
                    h(
                      "div",
                      { className: "memlens-entry-confirm-sub" },
                      "Current state will be saved as a recovery point first, so this is reversible.",
                    ),
                    restoreError && h("div", { className: "memlens-err" }, restoreError),
                    h(
                      "div",
                      { style: { display: "flex", gap: "0.5rem", marginTop: "0.6rem", flexWrap: "wrap" } },
                      h(Button, { onClick: () => restoreSnapshot("both"), disabled: restoring }, restoring ? "Restoring..." : "Restore both"),
                      h(Button, { onClick: () => restoreSnapshot("memory"), disabled: restoring }, "Just MEMORY.md"),
                      h(Button, { onClick: () => restoreSnapshot("user"), disabled: restoring }, "Just USER.md"),
                      h(Button, { variant: "outline", onClick: () => { setRestoreOpen(false); setRestoreError(null); }, disabled: restoring }, "Cancel"),
                    ),
                  ),
            ),
          pickedSnap &&
            h(
              "div",
              { className: "memlens-diff-wrap" },
              h(DiffView, {
                label: "MEMORY.md",
                before:
                  pickedSnap.memory && pickedSnap.memory.entries
                    ? pickedSnap.memory.entries.map((e) => e.body).join(ENTRY_DELIM_VIEW)
                    : "",
                after: state.memory.entries.map((e) => e.body).join(ENTRY_DELIM_VIEW),
              }),
              h(DiffView, {
                label: "USER.md",
                before:
                  pickedSnap.user && pickedSnap.user.entries
                    ? pickedSnap.user.entries.map((e) => e.body).join(ENTRY_DELIM_VIEW)
                    : "",
                after: state.user.entries.map((e) => e.body).join(ENTRY_DELIM_VIEW),
              }),
            ),
        ),
      view === "capture" &&
        h("div", { className: "memlens-body" }, h(CaptureComposer, { onCaptured: load })),
      view === "raw" &&
        h("div", { className: "memlens-body" }, h(RawEditor, { state, onSaved: load })),
      h(
        "p",
        { className: "memlens-footnote" },
        "Hermes home: ",
        h("span", { className: "memlens-mono" }, state.hermes_home),
        " · auto-snapshots on file change, capped at 200.",
      ),
    );
  }

  function Wrapped(props) {
    return h(ErrorBoundary, null, h(MemoryLensPage, props));
  }

  if (window.__HERMES_PLUGINS__) {
    window.__HERMES_PLUGINS__.register("memory-lens", Wrapped);
  } else {
    console.error("[memory-lens] window.__HERMES_PLUGINS__ missing");
  }
})();
