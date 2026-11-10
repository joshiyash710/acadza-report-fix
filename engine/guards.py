"""Code-level safety guards and the routing policy.

The LLM proposes; this module disposes. Nothing reaches `auto_apply` unless
every check below passes. A failed check never drops the case - it downgrades
it to `faculty_review` and writes the reason into `guard_log`, which the
console shows next to the diff.

Principles:
  * Scope: an edit may only touch fields the taxonomy lists for its repair.
  * Surgical: `find` must exist exactly once in the live field.
  * Proof: each auto-applicable repair has a repair-specific proof obligation
    (e.g. answer_key needs two blind solves agreeing on the new answer).
  * Taint: a group containing a prompt-injection report never auto-applies.
"""
from __future__ import annotations

import copy
import difflib
import json
import re

from .checks import LETTERS, normalize_answer
from .parse import body, html_to_text, split_options

AUTO_MIN_CONFIDENCE = 0.85
NO_CHANGE_VERDICTS = {"no_error", "display_issue", "app_issue", "language_mode", "already_fixed", "not_actionable"}
REPLY_BANNED = re.compile(r"\b(AI|A\.I\.|model|LLM|system prompt|algorithm|chatbot)\b")
REPLY_CONTACT = re.compile(r"(https?://|www\.|\b[\w-]+\.(com|in|me|io|ly|gg|org|net|xyz)\b|t\.me/|@\w{3,}|\b\d{10}\b|\+91|[\w.]+@[\w.]+)", re.I)

# Tags/attributes an edit may introduce into the question bank. Everything else is held.
SAFE_TAGS = {"math", "mrow", "mi", "mn", "mo", "mtext", "mfrac", "msup", "msub", "msubsup", "msqrt", "mroot", "mover",
             "munder", "munderover", "mstyle", "mspace", "mtable", "mtr", "mtd", "semantics", "annotation",
             "sup", "sub", "b", "i", "strong", "em", "u", "br", "span", "p", "h3", "h4", "ol", "ul", "li"}
SAFE_ATTRS = {"xmlns", "display", "displaystyle", "mathvariant", "stretchy", "fence", "form", "width", "linebreak", "separator", "accent"}


def unsafe_markup(find: str, replace: str) -> list[str]:
    """New markup an edit would write that could run code, link out, or restyle the page."""
    probs = []
    old_tags = set(re.findall(r"<\s*/?\s*([a-zA-Z][\w-]*)", find or ""))
    for tag, attrs in re.findall(r"<\s*([a-zA-Z][\w-]*)([^>]*)>", replace or ""):
        t = tag.lower()
        if t not in SAFE_TAGS and tag not in old_tags:
            probs.append(f"edit adds a <{t}> tag")
        for a in re.findall(r"([a-zA-Z_:][\w:.-]*)\s*=", attrs):
            if a.lower() not in SAFE_ATTRS:
                probs.append(f"edit adds a '{a}' attribute")
    ns = re.compile(r'xmlns="http://www\.w3\.org/1998/Math/MathML"')  # the MathML namespace is not a link
    rep, fnd = ns.sub("", replace or ""), ns.sub("", find or "")
    if re.search(r"(javascript:|data:|vbscript:|https?://|www\.)", rep, re.I) and not re.search(r"(javascript:|data:|https?://|www\.)", fnd, re.I):
        probs.append("edit adds a URL or script scheme")
    return sorted(set(probs))


def _num_tokens(s: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", s or "")


def _symbols(s: str) -> str:
    return "".join(ch for ch in html_to_text(s) if not ch.isalpha() and not ch.isspace())


def _norm_val(v):
    """Comparable form of a key: 'b' -> 'B', ['', 'B'] -> 'B', ['D','a'] -> ['A','D']."""
    if isinstance(v, list):
        letters = sorted({p.strip().upper() for x in v for p in re.split(r"[,\s]+", str(x)) if p.strip()})
        return letters[0] if len(letters) == 1 else letters
    return str(v).strip().upper() if v is not None else None


def _norm_text(s: str) -> str:
    return re.sub(r"[\s()]+", "", (s or "").lower()).replace("×", "x").replace("−", "-")


# ---------------------------------------------------------------------------
# applying edits
# ---------------------------------------------------------------------------

def apply_edits(q: dict, edits: list[dict]) -> dict:
    """Return a patched deep copy of the question record. Raises ValueError on a bad edit."""
    q2 = copy.deepcopy(q)
    for e in edits:
        f = e["field"]
        if f == "questionType":
            old, new = q2["questionType"], e["replace"]
            if old != e["find"]:
                raise ValueError(f"questionType is {old!r}, edit expects {e['find']!r}")
            q2[new] = q2.pop(old)
            q2["questionType"] = new
            continue
        b = body(q2)
        if f == "answer":
            if _norm_val(b.get("answer")) != _norm_val(e["find"]) and b.get("answer") != e["find"]:
                raise ValueError(f"answer is {b.get('answer')!r}, edit expects {e['find']!r}")
            b["answer"] = e["replace"]
            continue
        if f not in ("question", "solution"):
            raise ValueError(f"unsupported field {f}")
        cur = b.get(f) or ""
        n = cur.count(e["find"]) if e["find"] else 0
        if n != 1:
            raise ValueError(f"'find' occurs {n}x in {f} (must be exactly 1)")
        b[f] = cur.replace(e["find"], e["replace"], 1)
    return q2


# ---------------------------------------------------------------------------
# repair-specific proof obligations
# ---------------------------------------------------------------------------

def _solves_agree(solves: list[dict]) -> tuple[bool, object]:
    ans = [_norm_val(s.get("answer")) for s in solves if s.get("solvable", True)]
    if len(ans) < 2 or any(a in (None, "", []) for a in ans):
        return False, None
    return all(a == ans[0] for a in ans), ans[0]


def _proof(repair: str, edits: list[dict], q: dict, q_after: dict, solves: list[dict], derived_from) -> list[str]:
    """Return a list of unmet proof obligations (empty = proven)."""
    fails = []
    b_before, b_after = body(q), body(q_after)
    agree, solved = _solves_agree(solves)

    text_edits = [e for e in edits if e["field"] in ("question", "solution")]

    if repair == "answer_key":
        new = _norm_val(b_after.get("answer"))
        qtype = q_after["questionType"]
        norm_new, prob = normalize_answer(qtype, b_after.get("answer"))
        if prob:
            fails.append(f"new key still malformed: {prob}")
        if not agree:
            fails.append("blind solves do not agree with each other")
        elif solved != new:
            fails.append(f"blind solves say {solved}, edit says {new}")
        if qtype in ("scq", "mcq"):
            _, opts = split_options(html_to_text(b_after.get("question")))
            letters = new if isinstance(new, list) else [new]
            if any(l not in opts or not opts[l].strip() for l in letters):
                fails.append("new key points at a missing/blank option")
        if b_after.get("answer") == b_before.get("answer"):
            fails.append("edit does not change the stored key")

    elif repair == "type_mismatch":
        if not agree:
            fails.append("blind solves do not agree")
        elif solved != _norm_val(b_after.get("answer")):
            fails.append(f"blind solves say {solved}, new key is {b_after.get('answer')}")
        _, prob = normalize_answer(q_after["questionType"], b_after.get("answer"))
        if prob:
            fails.append(f"new key malformed for {q_after['questionType']}: {prob}")

    elif repair == "spelling":
        for e in text_edits:
            if _num_tokens(e["find"]) != _num_tokens(e["replace"]):
                fails.append("spelling edit changes a number")
            if _symbols(e["find"]) != _symbols(e["replace"]):
                fails.append("spelling edit changes a symbol")
            if difflib.SequenceMatcher(None, e["find"], e["replace"]).ratio() < 0.6:
                fails.append("spelling edit is not a small word change")

    elif repair == "exam_reference":
        for e in text_edits:
            if not re.search(r"(JEE|NEET|AIEEE|AIPMT|BITSAT)", e["find"], re.I) or re.search(r"(JEE|NEET|AIEEE|AIPMT)", e["replace"], re.I):
                fails.append("exam_reference edit must remove an exam tag and nothing else")
            if len(e["replace"]) >= len(e["find"]):
                fails.append("exam_reference edit must be a removal")

    elif repair == "noise":
        for e in text_edits:
            if len(e["replace"]) >= len(e["find"]):
                fails.append("noise edit must be a removal")

    elif repair == "markup_leak":
        for e in text_edits:
            if not re.search(r"\\[A-Za-z]+|\$|&lt;|&gt;", e["find"]):
                fails.append("markup_leak edit does not target raw markup")
            if _num_tokens(html_to_text(e["find"])) and not set(_num_tokens(html_to_text(e["find"]))) <= set(_num_tokens(html_to_text(e["replace"]))):
                fails.append("markup_leak edit drops a number")

    elif repair == "answer_form":
        for e in text_edits:
            ops = {op for op, *_ in difflib.SequenceMatcher(None, e["find"], e["replace"]).get_opcodes()}
            if not ops <= {"equal", "insert"}:
                fails.append("answer_form edit is not purely additive")

    elif repair == "stem_data":
        sol = html_to_text(b_before.get("solution"))
        for e in text_edits:
            added = [n for n in _num_tokens(html_to_text(e["replace"])) if n not in _num_tokens(html_to_text(e["find"]))]
            if not added:
                fails.append("stem_data edit adds no value")
            for n in added:
                if n not in _num_tokens(sol):
                    fails.append(f"value {n} is not stated in the stored solution")

    elif repair == "option_repair":
        if derived_from != "solution":
            fails.append("option text is authored, not recoverable from the record - needs a human")
        before_stem, _ = split_options(html_to_text(b_before.get("question")))
        after_stem, _ = split_options(html_to_text(b_after.get("question")))
        if before_stem != after_stem:
            fails.append("option_repair changed the stem")
        finals = [_norm_text(s.get("final_value", "")) for s in solves]
        added_txt = _norm_text(" ".join(html_to_text(e["replace"]) for e in text_edits))
        if derived_from == "solution" and finals and not any(f and f in added_txt for f in finals):
            fails.append("restored option does not match the blind solves' result")

    elif repair in ("solution_fix", "solution_missing"):
        qtype = q["questionType"]
        key = _norm_val(b_before.get("answer"))
        new_sol = html_to_text(b_after.get("solution"))
        if qtype == "integerQuestion":
            if key not in _num_tokens(new_sol)[-3:]:
                fails.append(f"new solution does not end at the keyed value {key}")
        else:
            _, opts = split_options(html_to_text(b_after.get("question")))
            target = _norm_text(opts.get(key, "")) if isinstance(key, str) else ""
            if target and target not in _norm_text(new_sol[-200:]):
                fails.append("new solution does not end at the keyed option")
        answered = [_norm_val(x.get("answer")) for x in solves if x.get("answer") not in (None, "", [])]
        if any(a != key for a in answered):
            fails.append("a blind solve disagrees with the key the solution must reach")

    elif repair == "notation":
        for e in text_edits:
            if difflib.SequenceMatcher(None, html_to_text(e["find"]), html_to_text(e["replace"])).ratio() < 0.5:
                fails.append("notation edit is too large")
    return fails


# ---------------------------------------------------------------------------
# the router
# ---------------------------------------------------------------------------

def _reply_problems(reply: str) -> list[str]:
    p = []
    if not reply or not reply.strip():
        return ["empty student reply"]
    sentences = [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(])", reply.strip()) if s.strip()]
    if len(sentences) > 3:
        p.append(f"student reply has {len(sentences)} sentences (max 3)")
    if len(reply.split()) > 70:
        p.append("student reply too long")
    if REPLY_BANNED.search(reply):
        p.append("student reply mentions AI/model")
    if re.match(r"\s*(no\b|wrong\b|incorrect\b|your answer is wrong)", reply, re.I):
        p.append("student reply opens with a rejection")
    if REPLY_CONTACT.search(reply):
        p.append("student reply contains a link, handle, email or phone number")
    return p


def route(proposal: dict, q: dict, taxonomy: dict, solves: list[dict], group_tainted: bool, findings: list | None = None) -> dict:
    """Validate one adjudicated report and decide its route. Returns the final record + guard log."""
    log = []
    verdict = proposal.get("verdict")
    repair = proposal.get("repair")
    edits = proposal.get("edits") or []
    conf = float(proposal.get("confidence") or 0)

    if verdict not in taxonomy["verdicts"]:
        log.append(f"unknown verdict {verdict!r} -> faculty_review")
        return {"route": "faculty_review", "edits": [], "q_after": None, "guard_log": log, "verdict": "not_actionable", "repair": None}

    if verdict in NO_CHANGE_VERDICTS:
        if edits:
            log.append(f"verdict {verdict} carries no edit; dropped {len(edits)} proposed edit(s)")
        # "nothing is wrong" must not contradict the evidence: a fooled or lazy verdict can't bury a real defect
        holds = []
        agree, solved = _solves_agree(solves)
        key = _norm_val(body(q).get("answer"))
        if agree and solved not in (None, key):
            holds.append(f"verdict says {verdict} but both blind solves say {solved}, key is {key}")
        crit = [f["detail"] for f in (findings or []) if f.get("severity") in ("critical", "high")]
        if crit:
            holds.append(f"verdict says {verdict} but code found: {crit[0]}")
        holds += _reply_problems(proposal.get("student_reply", ""))
        if holds:
            log += [f"hold: {h}" for h in holds]
            return {"route": "faculty_review", "edits": [], "q_after": None, "guard_log": log, "verdict": verdict, "repair": None}
        return {"route": "no_change", "edits": [], "q_after": None, "guard_log": log, "verdict": verdict, "repair": None}

    # verdict == defect
    if repair not in taxonomy["repairs"]:
        log.append(f"defect without a valid repair ({repair!r}) -> faculty_review")
        return {"route": "faculty_review", "edits": [], "q_after": None, "guard_log": log, "verdict": verdict, "repair": None}

    spec = taxonomy["repairs"][repair]
    allowed = set(spec["fields"])
    out_of_scope = [e for e in edits if e.get("field") not in allowed]
    if out_of_scope:
        log.append(f"scope: {repair} may touch {sorted(allowed) or 'nothing'}; stripped edits to "
                   f"{sorted({e.get('field') for e in out_of_scope})}")
        edits = [e for e in edits if e.get("field") in allowed]

    unsafe = False
    for e in list(edits):
        if e.get("field") in ("question", "solution"):
            bad = unsafe_markup(e.get("find", ""), e.get("replace", ""))
            if bad:
                log.append(f"edit rejected: {'; '.join(bad)}")
                edits.remove(e)
                unsafe = True

    q_after = None
    if edits:
        try:
            q_after = apply_edits(q, edits)
            log.append("edit applies cleanly (find matched exactly once)")
        except ValueError as ex:
            log.append(f"edit rejected: {ex}")
            edits, q_after = [], None

    if not spec["auto"]:
        log.append(f"{repair} is auto:false in taxonomy -> human decides")
        log += [f"hold: {r}" for r in _reply_problems(proposal.get("student_reply", ""))]
        return {"route": "faculty_review", "edits": edits, "q_after": q_after, "guard_log": log, "verdict": verdict, "repair": repair}

    reasons = []
    if unsafe:
        reasons.append("part of the proposed fix was unsafe; never apply half a fix")
    if not edits:
        reasons.append("no valid edit to apply")
    if conf < AUTO_MIN_CONFIDENCE:
        reasons.append(f"confidence {conf:.2f} < {AUTO_MIN_CONFIDENCE}")
    if group_tainted:
        reasons.append("a report on this question attempted prompt injection")
    if edits and q_after is not None:
        reasons += _proof(repair, edits, q, q_after, solves, proposal.get("derived_from"))
    reasons += _reply_problems(proposal.get("student_reply", ""))

    if reasons:
        log += [f"hold: {r}" for r in reasons]
        return {"route": "faculty_review", "edits": edits, "q_after": q_after, "guard_log": log, "verdict": verdict, "repair": repair}

    log.append(f"all proof obligations for {repair} met -> auto_apply")
    return {"route": "auto_apply", "edits": edits, "q_after": q_after, "guard_log": log, "verdict": verdict, "repair": repair}
