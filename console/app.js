/* Report → Fix faculty console. No build step, no framework: one fetch, one render loop.
   Decisions and the audit log live in localStorage (this deployment loads a precomputed run). */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode: keep in memory */ } },
  };
  const K = { dec: "rf.decisions.v1", audit: "rf.audit.v1", who: "rf.reviewer", theme: "rf.theme", found: "rf.found.v1" };

  const S = {
    cases: [], meta: {}, byId: {},
    tab: "needs", sel: null, view: "marked", replyIdx: 0, editing: false, rejecting: false, undo: { reason: null, note: "" },
    decisions: store.get(K.dec, {}), found: store.get(K.found, {}), audit: store.get(K.audit, []),
    draft: null,
  };

  // ------------------------------------------------------------------ vocabulary
  const ROUTE = {
    faculty_review: { chip: "needs", label: "Needs your decision" },
    auto_apply: { chip: "auto", label: "Auto-applied" },
    no_change: { chip: "none", label: "No change" },
  };
  const VERDICT_LINE = {
    no_error: "No change: the student mis-solved it",
    display_issue: "No change: rendering or phone layout issue",
    app_issue: "No change: app behaviour, not this question",
    language_mode: "No change: language request",
    already_fixed: "Already fixed by faculty",
    not_actionable: "No change: report can't be acted on",
  };
  const TABS = [
    { id: "needs", label: "Needs you" },
    { id: "auto", label: "Auto-applied" },
    { id: "nochange", label: "No change" },
    { id: "scan", label: "Scan finds" },
    { id: "done", label: "Done" },
  ];

  // ------------------------------------------------------------------ html helpers
  function sanitize(html) {
    const t = document.createElement("template");
    t.innerHTML = html || "";
    t.content.querySelectorAll("script,iframe,object,embed,link,style").forEach((n) => n.remove());
    t.content.querySelectorAll("*").forEach((n) => {
      for (const a of [...n.attributes]) {
        if (/^on/i.test(a.name) || a.name === "style" || /javascript:/i.test(a.value)) n.removeAttribute(a.name);
      }
    });
    return t.innerHTML;
  }
  function textOf(html) {
    const t = document.createElement("template");
    t.innerHTML = (html || "").replace(/<br\s*\/?>/gi, " ");
    return t.content.textContent.replace(/[​]/g, "").replace(/\s+/g, " ").trim();
  }
  function stemOf(html) {
    const t = textOf(html);
    const i = t.search(/\(\s*A\s*\)/);
    return (i > 20 ? t.slice(0, i) : t).trim();
  }
  function optionsOf(html) {
    const t = textOf(html);
    const out = {};
    const re = /\(\s*([A-D])\s*\)/g;
    const marks = [...t.matchAll(re)];
    let start = marks.map((m) => m[1]).lastIndexOf("A");
    if (start < 0) return out;
    const run = [];
    let exp = "A";
    for (const m of marks.slice(start)) if (m[1] === exp) { run.push(m); exp = String.fromCharCode(exp.charCodeAt(0) + 1); }
    run.forEach((m, j) => { out[m[1]] = t.slice(m.index + m[0].length, j + 1 < run.length ? run[j + 1].index : t.length).trim(); });
    return out;
  }
  const balanced = (frag) => {
    const stack = [];
    for (const m of frag.matchAll(/<\/?([a-zA-Z][\w-]*)[^>]*?(\/?)>/g)) {
      const tag = m[1].toLowerCase(), close = m[0][1] === "/", self = m[2] === "/" || /^(br|img|hr|mspace|none)$/.test(tag);
      if (self) continue;
      if (close) { if (stack.pop() !== tag) return false; } else stack.push(tag);
    }
    return stack.length === 0 && !/<[^>]*$/.test(frag) && !/^[^<]*>/.test(frag);
  };
  const insideMath = (prefix) => (prefix.match(/<math\b/g) || []).length > (prefix.match(/<\/math>/g) || []).length;

  /* Build an inline red-pen/green-pen version of `before` for one find/replace edit.
     Trim the common prefix/suffix of find and replace to the smallest changed span,
     snap to word/tag boundaries, and mark only that span. */
  function markEdit(before, e, fragmentsOnly = false) {
    const at = before.indexOf(e.find);
    if (at < 0 || typeof e.find !== "string") return null;
    const f = e.find, r = e.replace ?? "";
    let p = 0;
    while (p < f.length && p < r.length && f[p] === r[p]) p++;
    // snap prefix back to a boundary that is not inside a tag or entity
    const snapBack = (str, i) => {
      while (i > 0 && !/[\s>]/.test(str[i - 1])) i--;
      const lt = str.lastIndexOf("<", i - 1), gt = str.lastIndexOf(">", i - 1);
      if (lt > gt) i = lt;
      return i;
    };
    const snapFwd = (str, i) => { // i counts from the end
      let j = str.length - i;
      const atBoundary = (k) => k >= str.length || k === 0 || /[\s>]/.test(str[k - 1]) || /[\s<]/.test(str[k]);
      while (!atBoundary(j)) j++;
      const gt = str.indexOf(">", j), lt = str.indexOf("<", j);
      if (gt >= 0 && (lt < 0 || gt < lt)) j = gt + 1;
      return str.length - j;
    };
    p = Math.min(snapBack(f, p), snapBack(r, p));
    let s = 0; // common suffix, computed after the prefix is final so the two never overlap
    while (s < f.length - p && s < r.length - p && f[f.length - 1 - s] === r[r.length - 1 - s]) s++;
    s = Math.min(snapFwd(f, s), snapFwd(r, s));
    let del = f.slice(p, f.length - s), ins = r.slice(p, r.length - s);
    if (!balanced(del) || !balanced(ins)) {
      if (!balanced(f) || !balanced(r)) return null;
      p = 0; s = 0; del = f; ins = r; // mark the whole edit when the minimal span cuts through a tag
    }
    const pre = before.slice(0, at) + f.slice(0, p);
    const post = f.slice(f.length - s) + before.slice(at + f.length);
    const inMath = insideMath(pre);
    if (fragmentsOnly) return { pre, post, del, ins, inMath };
    const D = del ? (inMath ? `<mrow class="mdel">${del}</mrow>` : `<del class="pen">${del}</del>`) : "";
    const I = ins ? (inMath ? `<mrow class="mins">${ins}</mrow>` : `<ins class="pen">${ins}</ins>`) : "";
    return pre + D + I + post;
  }


  /* Word-level diff of two plain-text strings, with unchanged runs collapsed to a few words of context. */
  function wordDiff(a, b, ctx = 5) {
    const A = a.split(/(\s+)/).filter((x) => x && !/^\s+$/.test(x)), B = b.split(/(\s+)/).filter((x) => x && !/^\s+$/.test(x));
    const n = A.length, m = B.length;
    const L = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    const ops = [];
    let i = 0, j = 0;
    while (i < n || j < m) {
      if (i < n && j < m && A[i] === B[j]) { ops.push(["=", A[i]]); i++; j++; }
      else if (j < m && (i >= n || L[i][j + 1] >= L[i + 1][j])) { ops.push(["+", B[j]]); j++; }
      else { ops.push(["-", A[i]]); i++; }
    }
    const out = [];
    let k = 0;
    while (k < ops.length) {
      const t = ops[k][0];
      let e = k; while (e < ops.length && ops[e][0] === t) e++;
      const words = ops.slice(k, e).map((o) => esc(o[1]));
      if (t === "=") {
        const first = k === 0, last = e === ops.length;
        if (words.length > ctx * 2 + (first || last ? 0 : 1)) {
          const head = first ? [] : words.slice(0, ctx), tail = last ? [] : words.slice(-ctx);
          out.push([...head, "…", ...tail].join(" "));
        } else out.push(words.join(" "));
      } else out.push(t === "+" ? `<ins class="pen">${words.join(" ")}</ins>` : `<del class="pen">${words.join(" ")}</del>`);
      k = e;
    }
    return out.join(" ");
  }

  function applyEdits(rec, edits) {
    const out = JSON.parse(JSON.stringify(rec));
    for (const e of edits || []) {
      if (e.field === "questionType") { out.questionType = e.replace; continue; }
      if (e.field === "answer") { out.answer = e.replace; continue; }
      if (e.field === "question" || e.field === "solution") {
        const cur = out[e.field] || "";
        const n = cur.split(e.find).length - 1;
        if (!e.find || n !== 1) throw new Error(`“find” must match exactly once in the ${e.field} (matches ${n}×).`);
        out[e.field] = cur.replace(e.find, () => e.replace);
      }
    }
    return out;
  }

  const fmtKey = (a) => Array.isArray(a) ? (a.length ? a.map((x) => x === "" ? "∅" : x).join(", ") : "∅") : (a === "" || a == null ? "∅" : String(a));
  const keyText = (rec, a) => {
    if (rec.questionType === "integerQuestion") return String(a);
    const opts = optionsOf(rec.question);
    const letters = Array.isArray(a) ? a.flatMap((x) => String(x).split(/[,\s]+/)).filter(Boolean) : [a];
    return letters.map((l) => `(${l})${opts[l] !== undefined ? " " + (opts[l] || "[blank]") : ""}`).join("  ");
  };

  // ------------------------------------------------------------------ case semantics
  const decisionOf = (c) => S.decisions[c.questionId];
  function bucket(c) {
    if (decisionOf(c)) return "done";
    if (c.proactive) return "scan";
    return { faculty_review: "needs", auto_apply: "auto", no_change: "nochange" }[c.route] || "needs";
  }
  function headline(c) {
    if (c.proactive) return `${c.unreported.length} issue${c.unreported.length > 1 ? "s" : ""} found that nobody reported`;
    if (c.verdict !== "defect") return VERDICT_LINE[c.verdict] || "No change";
    const e = c.edits || [];
    const ans = e.find((x) => x.field === "answer");
    const L = {
      answer_key: ans ? (Array.isArray(ans.find) && JSON.stringify(ans.find.flatMap(x=>x.split(/[,\s]+/)).filter(Boolean).sort()) === JSON.stringify([].concat(ans.replace).sort())
        ? "Answer key is malformed; same letters, fixed format" : `Answer key is wrong: ${fmtKey(ans.find)} → ${fmtKey(ans.replace)}`) : "Answer key is wrong",
      type_mismatch: "Stored as multi-correct; only one option is right",
      stem_data: "A value is missing from the question",
      option_repair: c.route === "auto_apply" ? "Blank option restored from the solution" : "An option is missing",
      spelling: "Typo in the question",
      noise: "Stray text in the question",
      exam_reference: "Exam tag inside the question text",
      markup_leak: "Raw code showing instead of math",
      answer_form: "Unit not stated in the question",
      notation: "A symbol was lost in an expression",
      solution_fix: "The solution is wrong",
      solution_missing: "The solution is missing",
      identical_options: "Two options are identical",
      diagram: "The figure is missing",
      wrong_question: "Solution belongs to another question",
      unsound: "No option is correct as written",
      wrong_chapter: "Filed under the wrong chapter",
      syllabus: "Student says it's outside the syllabus",
    };
    return L[c.repair] || "Defect";
  }
  const shortLine = (c) => headline(c).replace(/:.*/, "");
  const tierVar = (c) => `var(--t${c.tier})`;
  const conf = (c) => Math.max(0, ...c.reports.map((r) => r.confidence || 0));
  const primaryReport = (c) => c.reports.find((r) => r.repair === c.repair && r.verdict === c.verdict) || c.reports[0];

  function visible() {
    return S.cases.filter((c) => bucket(c) === S.tab);
  }

  // ------------------------------------------------------------------ rendering: chrome
  function renderTabs() {
    const counts = Object.fromEntries(TABS.map((t) => [t.id, 0]));
    S.cases.forEach((c) => counts[bucket(c)]++);
    $("#tabs").innerHTML = TABS.map((t) => `<button class="tab ${t.id}" role="tab" aria-selected="${S.tab === t.id}" data-tab="${t.id}"><b>${counts[t.id]}</b>${t.label}</button>`).join("");
    const notes = {
      needs: "Worst first: wrong grading, then unusable questions, cosmetic, filing. Ties go to more students, then longer wait.",
      auto: "Already live because every safety check passed. Spot-check and undo if needed.",
      nochange: "The question is fine. Check the reply each student will get.",
      scan: "Questions no student reported, where the scan found defects. Unreported issues in reported questions show inside those cases.",
      done: "Decided cases. Open one to see or reopen your decision.",
    };
    $("#order-note").textContent = notes[S.tab];
    const total = S.cases.length, done = counts.done;
    $("#progress").innerHTML = `<span>${done} of ${total} cases decided</span><span class="bar" aria-hidden="true"><i style="width:${total ? (100 * done) / total : 0}%"></i></span><span>${counts.needs} need you</span>`;
    $("#audit-count").textContent = S.audit.length || "";
  }

  function renderQueue() {
    const list = visible();
    if (!list.length) {
      $("#items").innerHTML = `<li class="empty">${S.tab === "needs" ? "Nothing waiting for you. Spot-check the auto-applied fixes next." : "Nothing here."}</li>`;
      return;
    }
    $("#items").innerHTML = list.map((c) => {
      const d = decisionOf(c);
      const who = c.students ? `<span class="who-n">${c.students} student${c.students > 1 ? "s" : ""}</span>` : `<span>no reports</span>`;
      return `<li><button class="item ${d ? "done" : ""}" style="--tier:${tierVar(c)}" data-q="${c.questionId}" aria-current="${S.sel === c.questionId}">
        <span class="headline">${esc(headline(c))}</span>
        <span class="stem">${c.route === "no_change" && c.reports.length ? "“" + esc(c.reports[0].comment) + "”" : esc(stemOf(c.before.question))}</span>
        <span class="meta">${who}<span>${esc(c.subject)}</span><span>${esc(c.tierLabel)}</span>${d ? `<span>${esc(d.label)}</span>` : ""}</span>
      </button></li>`;
    }).join("");
  }

  // ------------------------------------------------------------------ rendering: case
  function sheetHTML(c, field) {
    const before = c.before[field] || "";
    const edits = currentEdits(c);
    let after = null;
    try { after = edits.length ? applyEdits(c.before, edits)[field] : null; } catch { after = null; }
    const changed = after !== null && after !== before;
    if (!before && !after) return `<div class="sheet"><span class="empty-field">(empty)</span></div>`;
    let html;
    if (S.view === "before" || !changed) html = before;
    else if (S.view === "after") html = after;
    else {
      // marked: apply each text edit's markup in sequence where possible
      const fe = edits.filter((e) => e.field === field);
      html = fe.length === 1 ? markEdit(before, fe[0]) : null;
      if (!html) html = after; // fall back to the clean after-view; the change list shows the diff
    }
    return `<div class="sheet">${sanitize(html)}</div>`;
  }

  function currentEdits(c) {
    const d = decisionOf(c);
    if (S.editing && S.draft) return S.draft.edits;
    if (d && d.edits) return d.edits;
    return c.edits || [];
  }

  function changeList(c) {
    const edits = currentEdits(c);
    if (!edits.length) {
      const sug = c.reports.map((r) => r.suggestion).find(Boolean);
      return `<p class="none-text">${c.verdict === "defect" ? "No automatic edit: this kind of fix needs authored content or a judgement call." : "Nothing in the question changes."}</p>
        ${sug ? `<p class="suggestion"><b>Suggested:</b> ${esc(sug)}</p>` : ""}`;
    }
    const rows = edits.map((e) => {
      if (e.field === "answer") {
        const afterRec = (() => { try { return applyEdits(c.before, edits); } catch { return c.before; } })();
        return `<div class="change"><div class="field">Answer key</div>
          <div class="keyline"><span class="k old">${esc(keyText(c.before, e.find))}</span><span aria-hidden="true">→</span><span class="k new">${esc(keyText(afterRec, e.replace))}</span></div></div>`;
      }
      if (e.field === "questionType") {
        const T = { scq: "Single correct", mcq: "Multiple correct", integerQuestion: "Integer" };
        return `<div class="change"><div class="field">Question type</div><div class="keyline"><span class="k old">${T[e.find] || e.find}</span><span aria-hidden="true">→</span><span class="k new">${T[e.replace] || e.replace}</span></div></div>`;
      }
      let a = c.before[e.field] || "", b = a;
      try { b = applyEdits(c.before, [e])[e.field]; } catch { /* shown via error elsewhere */ }
      const whole = (e.find || "").length > 600;
      const fr = markEdit(a, e, true);
      let body;
      if (fr && !fr.inMath) {
        const before = textOf(fr.pre).split(" ").slice(-6).join(" "), after = textOf(fr.post).split(" ").slice(0, 6).join(" ");
        body = `<span class="muted">${before && !whole ? "…" + esc(before) : ""}</span> ${fr.del ? `<del class="pen">${sanitize(fr.del)}</del>` : ""} ${fr.ins ? `<ins class="pen">${sanitize(fr.ins)}</ins>` : ""} <span class="muted">${after && !whole ? esc(after) + "…" : ""}</span>`;
      } else body = wordDiff(textOf(a), textOf(b));
      return `<div class="change"><div class="field">${e.field === "question" ? "Question text" : "Solution"}${whole ? " (rewritten)" : ""}</div>
        <div class="pair">${body}</div></div>`;
    }).join("");
    const authored = c.reports.find((r) => r.derived_from === "authored");
    const sug = c.reports.map((r) => r.suggestion).find(Boolean);
    return rows + (authored && sug ? `<p class="suggestion"><b>Your call:</b> ${esc(sug)}</p>` : "");
  }

  function checksList(c) {
    const pr = primaryReport(c);
    const items = [];
    const key = c.before.answer;
    let finalKey = key;
    try { finalKey = applyEdits(c.before, currentEdits(c)).answer; } catch { /* draft invalid */ }
    const norm = (a) => JSON.stringify([].concat(a ?? []).flatMap((x) => String(x).split(/[,\s]+/)).filter(Boolean).sort());
    c.solves.forEach((s, i) => {
      const a = s.answer == null ? (s.solvable === false ? "can't solve as written" : "no option matches") : fmtKey(s.answer);
      const kind = s.answer == null ? "info" : norm(s.answer) === norm(finalKey) ? "ok" : "bad";
      items.push([kind, `Blind solve ${i + 1} (${s.variant}, never saw the key): <b>${esc(a)}</b>${s.final_value ? ` <span class="muted">(${esc(s.final_value)})</span>` : ""}`]);
    });
    items.push(["info", `Stored key: <b>${esc(fmtKey(key))}</b>${norm(finalKey) !== norm(key) ? ` → after fix <b>${esc(fmtKey(finalKey))}</b>` : ""}`]);
    (c.findings || []).filter((f) => f.severity !== "info").forEach((f) => items.push(["bad", `Code check: ${esc(f.detail)}`]));
    (pr?.guard_log || []).forEach((l) => items.push([/^hold|rejected|stripped/.test(l) ? "hold" : "ok", esc(l)]));
    if (c.reports.some((r) => r.injection_attempt)) items.push(["bad", "A report tried to give the system instructions. It was treated as data; nothing auto-applies on this question."]);
    const icon = { ok: "✓", hold: "!", bad: "✗", info: "•" };
    return `<ul class="checks">${items.map(([k, t]) => `<li><span class="${k}" aria-hidden="true">${icon[k]}</span><span>${t}</span></li>`).join("")}</ul>
      <details class="more"><summary>Solver working</summary><pre>${esc(c.solves.map((s) => `[${s.variant}] ${s.key_steps}${s.missing_or_broken ? "\n  ⚠ " + s.missing_or_broken : ""}`).join("\n\n"))}</pre></details>`;
  }

  function reportsList(c) {
    if (!c.reports.length) return `<p class="none-text">No student reported this. The scan found it.</p>`;
    return c.reports.map((r) => `<div class="report">
      <blockquote>${esc(r.comment)}</blockquote>
      <div class="rmeta"><span>${esc(r.reportId)}</span><span>${esc(r.source)}</span><span>${new Date(r.reportedAt).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</span>
        ${r.verdict !== c.verdict ? `<span class="chip">${esc(r.verdict)}</span>` : ""}${r.injection_attempt ? `<span class="chip warn">instruction attempt</span>` : ""}${r.flags?.gibberish ? `<span class="chip">unintelligible</span>` : ""}</div>
      ${r.claim ? `<div class="claim">Claim: ${esc(r.claim)}</div>` : ""}
    </div>`).join("");
  }

  function replyPreview(c) {
    if (!c.reports.length) return "";
    const i = Math.min(S.replyIdx, c.reports.length - 1);
    const r = c.reports[i];
    const tabs = c.reports.length > 1 ? `<div class="reply-tabs">${c.reports.map((x, j) => `<button data-reply="${j}" aria-pressed="${j === i}">${esc(x.reportId)}</button>`).join("")}</div>` : "";
    return `<section class="card"><h3>What the student sees <span class="muted">${r.student_reply.split(/\s+/).length} words</span></h3>${tabs}
      <div class="phone"><div class="from"><span>Acadza · reply to ${esc(r.reportId)}</span><span>${esc(r.source)}</span></div><div class="bubble">${esc(r.student_reply)}</div></div></section>`;
  }

  function foundList(c) {
    if (!c.unreported.length) return "";
    return `<section class="card"><h3>${c.proactive ? "Issues found" : "Also found in this question"} <span class="muted">nobody reported these</span></h3>
      ${c.unreported.map((u, i) => {
        const id = `${c.questionId}:${i}`, d = S.found[id];
        const pair = u.find ? `<del class="pen">${esc(textOf(u.find)) || "&nbsp;"}</del> <ins class="pen">${esc(textOf(u.replace)) || "(removed)"}</ins>` : "";
        return `<div class="found"><div class="fhead"><span><span class="chip">${esc(u.repair)}</span> <span class="muted">${esc(u.field)}</span></span>
          ${d ? `<span class="chip done">${esc(d.status)}</span>` : `<span class="mini">${u.find && u.applies ? `<button class="ghost" data-found="${id}" data-act="approve">Apply</button>` : ""}<button class="ghost" data-found="${id}" data-act="${u.find ? "dismiss" : "noted"}">${u.find ? "Dismiss" : "Noted"}</button></span>`}</div>
          ${pair ? `<div class="pair">${pair}</div>` : ""}<p class="why">${esc(u.evidence)}</p></div>`;
      }).join("")}</section>`;
  }

  function editorHTML(c) {
    if (!S.editing) return "";
    const d = S.draft;
    const blocks = d.edits.map((e, i) => {
      if (e.field === "answer") {
        if (c.before.questionType === "integerQuestion" && !d.edits.some(x => x.field === "questionType"))
          return `<label for="ed-${i}">New answer key</label><input type="text" id="ed-${i}" data-ed="${i}" data-kind="int" value="${esc(e.replace)}">`;
        const multi = Array.isArray(e.replace);
        const cur = [].concat(e.replace);
        return `<label>New answer key ${multi ? "(select all correct)" : ""}</label><div class="letters">${"ABCD".split("").map((l) =>
          `<button type="button" data-ed="${i}" data-letter="${l}" data-multi="${multi}" aria-pressed="${cur.includes(l)}">${l}</button>`).join("")}</div>`;
      }
      if (e.field === "questionType") return `<label>Question type</label><input type="text" disabled value="${esc(e.find)} → ${esc(e.replace)}">`;
      return `<label for="ed-${i}">${e.field === "question" ? "Question" : "Solution"}: replacement HTML for the matched text</label>
        <textarea id="ed-${i}" data-ed="${i}" data-kind="text" spellcheck="false">${esc(e.replace)}</textarea>`;
    }).join("");
    const write = !d.edits.length ? `<label for="ed-field">Field</label><select id="ed-field"><option value="question">Question text</option><option value="solution">Solution</option></select>
      <label for="ed-find">Find (exact text from the stored HTML)</label><textarea id="ed-find" spellcheck="false"></textarea>
      <label for="ed-rep">Replace with</label><textarea id="ed-rep" spellcheck="false"></textarea>` : "";
    return `<div class="editor"><strong>Edit the fix</strong> <span class="muted small">The preview on the left updates as you type.</span>${blocks}${write}
      <div class="err" id="ed-err" role="alert"></div></div>`;
  }

  const UNDO_REASONS = [
    "The fix is wrong",
    "Right problem, wrong fix",
    "The original was already correct",
    "Fix is unclear for students",
    "Other",
  ];
  const undoValid = () => !!S.undo.reason && (S.undo.reason !== "Other" || S.undo.note.trim().length >= 5);

  function rejectHTML(c) {
    if (!S.rejecting) return "";
    if (c && c.route === "auto_apply") {
      // Undoing a LIVE fix is deliberate: a reason is required, and "Other" needs a note.
      const needNote = S.undo.reason === "Other";
      return `<div class="editor undo-form"><strong>Why undo this fix?</strong> <span class="muted small">This fix is live for students. Your reason helps tune what goes live automatically.</span>
        <div class="reasons" role="radiogroup" aria-label="Reason for undo">${UNDO_REASONS.map((r) =>
          `<button data-undo-reason="${esc(r)}" role="radio" aria-checked="${S.undo.reason === r}">${esc(r)}</button>`).join("")}</div>
        <label for="undo-note">${needNote ? "What's wrong with it? (required)" : "Note (optional)"}</label>
        <textarea id="undo-note" class="note" placeholder="${needNote ? "At least a few words" : "e.g. the correct value is 90 mH, not 95.5"}">${esc(S.undo.note)}</textarea>
        <div class="undo-actions"><button class="btn" data-act="cancel-undo">Keep it live</button>
          <button class="btn danger solid" data-act="confirm-undo" ${undoValid() ? "" : "disabled"}>Undo fix</button></div>
        <div class="muted small" id="undo-hint">${undoValid() ? "" : S.undo.reason ? "Add a short note to continue." : "Pick a reason to continue."}</div></div>`;
    }
    const reasons = ["The student was right; the AI missed it", "Not a defect", "Right defect, wrong fix", "Needs a subject expert", "Other"];
    return `<div class="editor"><strong>Why reject?</strong> <span class="muted small">One click records it.</span>
      <div class="reasons">${reasons.map((r) => `<button data-reason="${esc(r)}">${esc(r)}</button>`).join("")}</div></div>`;
  }

  function actionBar(c) {
    const d = decisionOf(c);
    if (d) {
      return `<div class="actions"><div class="decided"><span class="chip done">${esc(d.label)}</span><span class="muted small">by ${esc(d.by)} · ${new Date(d.at).toLocaleString("en-IN")}</span></div>
        <span class="hint"></span><button class="btn" data-act="reopen">Reopen</button><button class="btn" data-act="next">Next case <kbd>J</kbd></button></div>`;
    }
    if (S.editing) {
      return `<div class="actions"><span class="hint">Saving records your version and the AI's original in the audit log.</span>
        <button class="btn" data-act="cancel">Cancel</button><button class="btn primary" data-act="save">Save and approve</button></div>`;
    }
    const auto = c.route === "auto_apply", none = c.route === "no_change" && !c.proactive;
    const hint = c.proactive ? "Apply or dismiss each finding, then mark the case reviewed."
      : auto ? "This fix is already live. Keep it, adjust it, or undo it."
      : none ? "Nothing changes in the question. Approve sends the replies."
      : "Nothing is live yet. Your approval applies the fix.";
    const A = c.proactive ? "Mark reviewed" : auto ? "Keep fix" : none ? "Send replies" : (c.edits?.length ? "Approve fix" : "Approve");
    const R = auto ? "Undo fix" : none ? "Reopen as defect" : "Reject";
    return `<div class="actions"><span class="hint">${hint}</span>
      ${c.proactive ? "" : `<button class="btn danger" data-act="reject">${R} <kbd>R</kbd></button><button class="btn" data-act="edit">Edit <kbd>E</kbd></button>`}
      <button class="btn primary" data-act="approve">${A} <kbd>A</kbd></button></div>`;
  }

  function renderCase() {
    const c = S.byId[S.sel];
    const el = $("#case");
    if (!c) { el.innerHTML = `<div class="empty">Pick a case from the queue.</div>`; return; }
    const r = ROUTE[c.route] || ROUTE.faculty_review;
    const d = decisionOf(c);
    const pr = primaryReport(c);
    const cf = c.proactive ? Math.max(...c.unreported.map((u) => u.confidence || 0)) : conf(c);
    const hasSol = !!(c.before.solution || "").trim();
    const solChanged = currentEdits(c).some((e) => e.field === "solution");
    el.innerHTML = `
      ${S.meta.provider === "replay" ? `<div class="banner">Showing a precomputed run (${esc(S.meta.solver_version)}, ${esc(S.meta.adjudicator_version)}). Your decisions are saved in this browser.</div>` : ""}
      <div class="crumbs"><span>${esc(c.subject)} › ${esc(c.chapter)}</span><span>${{ scq: "Single correct", mcq: "Multiple correct", integerQuestion: "Integer" }[c.questionType]}</span>${c.level ? `<span>${esc(c.level)}</span>` : ""}
        <button data-copy="${c.questionId}" title="Copy question id">${c.questionId}</button></div>
      <div class="verdict">
        <div><h2>${esc(headline(c)).replace("→", '<span class="arrow">→</span>')}</h2>
          <div class="chips">${d ? `<span class="chip done">${esc(d.label)}</span>` : `<span class="chip ${c.proactive ? "needs" : r.chip}">${c.proactive ? "Needs your decision" : r.label}</span>`}
            ${c.proactive ? `<span class="chip">Found by scan</span>` : `<span class="chip"><span class="dot" style="--tier:${tierVar(c)}"></span>${esc(c.tierLabel)}</span>`}
            ${c.repair ? `<span class="chip" title="${esc(S.meta.taxonomy?.repairs?.[c.repair]?.label || "")}">${esc(c.repair)}</span>` : ""}
            ${c.students > 1 ? `<span class="chip">${c.students} students reported</span>` : ""}</div></div>
        <div class="conf"><strong>${Math.round(cf * 100)}%</strong>confidence<div class="meter"><i style="width:${cf * 100}%"></i></div></div>
        <p class="evidence">${esc(c.proactive ? c.summary : pr?.evidence || c.summary)}</p>
      </div>
      <div class="grid">
        <div class="col">
          <section class="card">
            <div class="sheet-head"><h3>Question</h3>
              <div class="seg" role="group" aria-label="View">${[["marked", "Marked changes"], ["before", "Before"], ["after", "After"]].map(([v, l]) => `<button data-view="${v}" aria-pressed="${S.view === v}">${l}</button>`).join("")}</div></div>
            ${sheetHTML(c, "question")}
            <div class="keyline"><span class="muted">Key</span>${keyLine(c)}</div>
            ${hasSol || solChanged ? `<details ${solChanged ? "open" : ""}><summary class="label-row">Solution${solChanged ? " (changed)" : ""}</summary>${sheetHTML(c, "solution")}</details>` : ""}
          </section>
          ${foundList(c)}
        </div>
        <div class="col">
          ${c.proactive ? "" : `<section class="card"><h3>What changes</h3>${changeList(c)}${editorHTML(c)}${rejectHTML(c)}</section>`}
          ${c.route === "no_change" ? replyPreview(c) : ""}
          <section class="card"><h3>Why you can trust it</h3>${checksList(c)}</section>
          <section class="card"><h3>Reports <span class="muted">${c.reports.length}</span></h3>${reportsList(c)}</section>
          ${c.route !== "no_change" ? replyPreview(c) : ""}
        </div>
      </div>
      ${actionBar(c)}`;
  }

  function keyLine(c) {
    const edits = currentEdits(c);
    let after = null;
    try { after = applyEdits(c.before, edits); } catch { /* invalid draft */ }
    const changed = after && JSON.stringify(after.answer) !== JSON.stringify(c.before.answer);
    if (!changed && after && S.view !== "before" && keyText(after, after.answer) !== keyText(c.before, c.before.answer))
      return `<span class="k new">${esc(keyText(after, after.answer))}</span>`;
    if (!changed || S.view === "before") return `<span class="k">${esc(keyText(c.before, c.before.answer))}</span>`;
    if (S.view === "after") return `<span class="k new">${esc(keyText(after, after.answer))}</span>`;
    return `<span class="k old">${esc(keyText(c.before, c.before.answer))}</span><span aria-hidden="true">→</span><span class="k new">${esc(keyText(after, after.answer))}</span>`;
  }

  function render() { renderTabs(); renderQueue(); renderCase(); }

  // ------------------------------------------------------------------ actions
  function reviewer() {
    const v = $("#reviewer").value.trim();
    if (!v) { $("#reviewer").focus(); toast("Add your name first so the audit log knows who decided."); return null; }
    return v;
  }
  function log(entry) {
    S.audit.unshift({ id: crypto.randomUUID ? crypto.randomUUID() : String(Date.now()), at: new Date().toISOString(), ...entry });
    store.set(K.audit, S.audit);
  }
  function decide(c, action, label, extra = {}) {
    const by = reviewer();
    if (!by) return false;
    const prev = S.decisions[c.questionId];
    S.decisions[c.questionId] = { action, label, by, at: new Date().toISOString(), ...extra };
    store.set(K.dec, S.decisions);
    log({ by, action, questionId: c.questionId, reportIds: c.reports.map((r) => r.reportId), aiRoute: c.route, repair: c.repair,
      aiEdits: c.edits || [], finalEdits: extra.edits ?? (action === "reject" || action === "undo" ? [] : c.edits || []), reason: extra.reason || null, note: extra.note || null });
    toast(`${label}.`, () => { // undo
      if (prev) S.decisions[c.questionId] = prev; else delete S.decisions[c.questionId];
      store.set(K.dec, S.decisions);
      log({ by, action: "revert-decision", questionId: c.questionId, reportIds: c.reports.map((r) => r.reportId) });
      S.sel = c.questionId; S.tab = bucket(c); render();
    });
    return true;
  }
  function nextCase(dir = 1) {
    const list = visible();
    if (!list.length) { render(); return; }
    let i = list.findIndex((c) => c.questionId === S.sel);
    i = i < 0 ? 0 : Math.min(list.length - 1, Math.max(0, i + dir));
    select(list[i].questionId);
  }
  function afterDecision() {
    const list = visible();
    S.editing = S.rejecting = false; S.draft = null; S.undo = { reason: null, note: "" };
    const nxt = list.find((c) => !decisionOf(c));
    if (nxt) S.sel = nxt.questionId;
    render();
    $("#case").scrollTop = 0;
  }
  function select(qid) {
    S.sel = qid; S.replyIdx = 0; S.editing = S.rejecting = false; S.draft = null; S.view = "marked"; S.undo = { reason: null, note: "" };
    render();
    $("#case").scrollTop = 0;
    const btn = document.querySelector(`.item[data-q="${qid}"]`);
    btn && btn.scrollIntoView({ block: "nearest" });
  }

  function onAction(act) {
    const c = S.byId[S.sel];
    if (!c) return;
    if (act === "next") return nextCase(1);
    if (act === "approve") {
      const auto = c.route === "auto_apply";
      if (decide(c, auto ? "keep" : c.route === "no_change" ? "confirm" : c.proactive ? "reviewed" : "approve",
        c.proactive ? "Marked reviewed" : auto ? "Kept the fix" : c.route === "no_change" ? "Replies approved" : "Fix approved")) afterDecision();
    } else if (act === "reject") {
      S.rejecting = !S.rejecting; S.editing = false; renderCase();
    } else if (act === "edit") {
      S.editing = true; S.rejecting = false;
      S.draft = { edits: JSON.parse(JSON.stringify(c.edits || [])) };
      renderCase();
      const first = $(".editor textarea, .editor input:not([disabled])");
      first && first.focus();
    } else if (act === "cancel") {
      S.editing = false; S.draft = null; renderCase();
    } else if (act === "save") {
      if (!S.draft.edits.length) {
        const f = $("#ed-find")?.value, rp = $("#ed-rep")?.value;
        if (f) S.draft.edits = [{ field: $("#ed-field").value, find: f, replace: rp ?? "" }];
      }
      try { applyEdits(c.before, S.draft.edits); } catch (e) { $("#ed-err").textContent = e.message; return; }
      if (!S.draft.edits.length) { $("#ed-err").textContent = "Write a find/replace first, or cancel and approve as is."; return; }
      if (decide(c, "edit", "Approved with your edit", { edits: S.draft.edits })) afterDecision();
    } else if (act === "cancel-undo") {
      S.rejecting = false; S.undo = { reason: null, note: "" }; renderCase();
    } else if (act === "confirm-undo") {
      if (!undoValid()) return;
      if (decide(c, "undo", "Fix undone", { reason: S.undo.reason, note: S.undo.note.trim() || null, edits: [] })) afterDecision();
    } else if (act === "reopen") {
      const by = reviewer(); if (!by) return;
      log({ by, action: "reopen", questionId: c.questionId, reportIds: c.reports.map((r) => r.reportId) });
      delete S.decisions[c.questionId]; store.set(K.dec, S.decisions);
      S.tab = bucket(c); render();
    }
  }

  function onDraftInput(t) {
    const i = +t.dataset.ed;
    const e = S.draft.edits[i];
    if (t.dataset.kind === "text" || t.dataset.kind === "int") e.replace = t.value;
    const c = S.byId[S.sel];
    try { applyEdits(c.before, S.draft.edits); $("#ed-err").textContent = ""; } catch (err) { $("#ed-err").textContent = err.message; return; }
    // re-render only the sheets so the textarea keeps focus
    const sheets = document.querySelectorAll(".col:first-child .sheet");
    if (sheets[0]) sheets[0].outerHTML = sheetHTML(c, "question");
    const s2 = document.querySelectorAll(".col:first-child .sheet")[1];
    if (s2) s2.outerHTML = sheetHTML(c, "solution");
    const kl = $(".col:first-child .keyline"); if (kl) kl.innerHTML = `<span class="muted">Key</span>${keyLine(c)}`;
  }

  // ------------------------------------------------------------------ toast
  let toastTimer;
  function toast(msg, undo) {
    const t = $("#toast");
    t.innerHTML = `<span>${esc(msg)}</span>${undo ? `<button id="toast-undo">Undo</button>` : ""}`;
    t.hidden = false;
    if (undo) $("#toast-undo").onclick = () => { t.hidden = true; undo(); };
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.hidden = true), undo ? 6000 : 3500);
  }

  // ------------------------------------------------------------------ audit dialog
  const ACTION_WORDS = { approve: "approved the fix", edit: "approved with an edit", keep: "kept the auto-applied fix", undo: "undid the auto-applied fix",
    reject: "rejected", confirm: "approved the replies (no change)", reviewed: "marked reviewed", reopen: "reopened", "revert-decision": "reverted a decision",
    "scan-approve": "applied a scan finding", "scan-dismiss": "dismissed a scan finding", "scan-noted": "noted a scan finding" };
  function undoStats() {
    // Week-1 metric from PRODUCT.md: reverted auto-applies ÷ auto-applies, by repair type.
    const auto = S.cases.filter((c) => c.route === "auto_apply");
    const undone = auto.filter((c) => decisionOf(c)?.action === "undo");
    const kept = auto.filter((c) => decisionOf(c)?.action === "keep" || decisionOf(c)?.action === "edit");
    const byRepair = {};
    undone.forEach((c) => { byRepair[c.repair] = (byRepair[c.repair] || 0) + 1; });
    const pct = auto.length ? Math.round((100 * undone.length) / auto.length) : 0;
    return `<div class="stats"><b>${undone.length} of ${auto.length}</b> auto-applied fixes undone (${pct}%) · ${kept.length} kept
      ${undone.length ? `<span class="muted"> · by repair: ${Object.entries(byRepair).map(([k, v]) => `${esc(k)} ${v}`).join(", ")}</span>` : ""}</div>`;
  }
  function renderAudit() {
    $("#audit-stats").innerHTML = undoStats();
    $("#audit-list").innerHTML = S.audit.length ? S.audit.map((a) => `<li><time>${new Date(a.at).toLocaleString("en-IN")}</time>
      <div><b>${esc(a.by)}</b> ${esc(ACTION_WORDS[a.action] || a.action)} <span class="muted">${esc(a.questionId)}${a.reportIds?.length ? " · " + esc(a.reportIds.join(", ")) : ""}</span>
      ${a.reason ? `<div class="muted">Reason: ${esc(a.reason)}${a.note ? ` · “${esc(a.note)}”` : ""}</div>` : ""}
      ${a.action === "edit" && JSON.stringify(a.finalEdits) !== JSON.stringify(a.aiEdits) ? `<div class="muted">Changed the AI's edit.</div>` : ""}</div></li>`).join("")
      : `<li><span></span><span class="muted">No actions yet.</span></li>`;
  }

  // ------------------------------------------------------------------ events
  document.addEventListener("click", (ev) => {
    const t = ev.target.closest("button, [data-copy]");
    if (!t) return;
    if (t.dataset.tab) { S.tab = t.dataset.tab; const l = visible(); S.sel = l[0]?.questionId || null; S.editing = S.rejecting = false; render(); return; }
    if (t.dataset.q) return select(t.dataset.q);
    if (t.dataset.view) { S.view = t.dataset.view; renderCase(); return; }
    if (t.dataset.reply) { S.replyIdx = +t.dataset.reply; renderCase(); return; }
    if (t.dataset.act && !t.dataset.found) return onAction(t.dataset.act);
    if (t.dataset.reason) {
      const c = S.byId[S.sel];
      if (c.route === "auto_apply") return; // live fixes go through the undo form
      if (decide(c, "reject", "Rejected", { reason: t.dataset.reason, edits: [] })) afterDecision();
      return;
    }
    if (t.dataset.undoReason) {
      S.undo.reason = t.dataset.undoReason; renderCase();
      if (S.undo.reason === "Other") $("#undo-note")?.focus();
      return;
    }
    if (t.dataset.letter) {
      const e = S.draft.edits[+t.dataset.ed];
      if (t.dataset.multi === "true") {
        const set = new Set([].concat(e.replace).filter(Boolean));
        set.has(t.dataset.letter) ? set.delete(t.dataset.letter) : set.add(t.dataset.letter);
        e.replace = [...set].sort();
      } else e.replace = t.dataset.letter;
      renderCase();
      return;
    }
    if (t.dataset.found) {
      const by = reviewer(); if (!by) return;
      const [qid, idx] = t.dataset.found.split(":");
      const c = S.byId[qid], u = c.unreported[+idx];
      const status = { approve: "Applied", dismiss: "Dismissed", noted: "Noted" }[t.dataset.act];
      S.found[t.dataset.found] = { status, by, at: new Date().toISOString() };
      store.set(K.found, S.found);
      log({ by, action: `scan-${t.dataset.act}`, questionId: qid, reportIds: [], repair: u.repair, finalEdits: t.dataset.act === "approve" ? [{ field: u.field, find: u.find, replace: u.replace }] : [] });
      toast(`${status}.`);
      renderTabs(); renderCase();
      return;
    }
    if (t.dataset.copy) { navigator.clipboard?.writeText(t.dataset.copy); toast("Question id copied."); }
  });
  document.addEventListener("input", (ev) => {
    if (ev.target.dataset?.ed !== undefined && S.draft) onDraftInput(ev.target);
    if (ev.target.id === "undo-note") {
      S.undo.note = ev.target.value;
      const b = $('[data-act="confirm-undo"]'); if (b) b.disabled = !undoValid();
      const h = $("#undo-hint"); if (h) h.textContent = undoValid() ? "" : S.undo.reason ? "Add a short note to continue." : "Pick a reason to continue.";
    }
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.target.matches("input, textarea, select") || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (document.querySelector("dialog[open]")) return;
    const k = ev.key.toLowerCase();
    if (k === "j") nextCase(1);
    else if (k === "k") nextCase(-1);
    else if (k === "a" && !S.editing && !decisionOf(S.byId[S.sel] || {})) onAction("approve");
    else if (k === "e" && !S.editing && !S.byId[S.sel]?.proactive) onAction("edit");
    else if (k === "r" && !S.editing && !S.byId[S.sel]?.proactive) onAction("reject");
    else if (k === "escape") { S.editing = S.rejecting = false; renderCase(); }
    else if (["1", "2", "3"].includes(k)) { S.view = ["marked", "before", "after"][+k - 1]; renderCase(); }
    else if (k === "?") $("#help-dialog").showModal();
    else return;
    ev.preventDefault();
  });

  $("#reviewer").value = store.get(K.who, "");
  $("#reviewer").addEventListener("change", (e) => store.set(K.who, e.target.value.trim()));
  $("#btn-audit").onclick = () => { renderAudit(); $("#audit-dialog").showModal(); };
  $("#btn-close-audit").onclick = () => $("#audit-dialog").close();
  $("#btn-help").onclick = () => $("#help-dialog").showModal();
  $("#btn-close-help").onclick = () => $("#help-dialog").close();
  $("#btn-export").onclick = () => {
    const auto = S.cases.filter((c) => c.route === "auto_apply");
    const undone = auto.filter((c) => decisionOf(c)?.action === "undo").map((c) => ({ questionId: c.questionId, repair: c.repair, reason: decisionOf(c).reason, note: decisionOf(c).note }));
    const blob = new Blob([JSON.stringify({ exportedAt: new Date().toISOString(), autoApplyMetric: { autoApplied: auto.length, undone: undone.length, undoneFixes: undone }, decisions: S.decisions, scanDecisions: S.found, audit: S.audit }, null, 2)], { type: "application/json" });
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: `acadza-audit-${new Date().toISOString().slice(0, 10)}.json` });
    a.click(); URL.revokeObjectURL(a.href);
  };
  const theme = store.get(K.theme, matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const setTheme = (t) => { document.documentElement.dataset.theme = t; $("#btn-theme").textContent = t === "dark" ? "Light" : "Dark"; };
  setTheme(theme);
  $("#btn-theme").onclick = () => {
    const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    setTheme(t); store.set(K.theme, t);
  };

  // ------------------------------------------------------------------ boot
  fetch("data/cases.json").then((r) => r.json()).then((d) => {
    S.meta = d.meta; S.cases = d.cases;
    S.cases.forEach((c) => (S.byId[c.questionId] = c));
    if (!visible().length) S.tab = TABS.find((t) => S.cases.some((c) => bucket(c) === t.id))?.id || "needs";
    S.sel = visible()[0]?.questionId || null;
    render();
  }).catch((e) => { $("#case").innerHTML = `<div class="empty">Couldn't load data/cases.json (${esc(e.message)}). Serve this folder over HTTP, e.g. <code>python -m http.server</code>.</div>`; });
})();
