"""Deterministic, zero-cost checks.

These run before any LLM call. They do two jobs:
  1. Catch the defects a regex can prove (malformed key, blank option, raw
     LaTeX, exam tags, missing figure) so the LLM is *told* about them as
     facts instead of having to notice them.
  2. Flag hostile or empty reports (prompt injection, keyboard-mash) so the
     router can refuse to auto-apply anything they triggered.

Every finding carries the repair key it maps to in taxonomy.json.
"""
from __future__ import annotations

import re
from datetime import datetime

from .parse import body, html_to_text, split_options

LETTERS = "ABCD"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def normalize_answer(qtype: str, answer):
    """Return (normalized_answer, problem_or_None)."""
    if qtype == "scq":
        if isinstance(answer, str) and answer.strip().upper() in LETTERS and len(answer.strip()) == 1:
            return answer.strip().upper(), None
        return answer, f"SCQ key {answer!r} is not a single letter A-D"
    if qtype == "integerQuestion":
        if isinstance(answer, (int, float)) or (isinstance(answer, str) and re.fullmatch(r"-?\d+(\.\d+)?", answer.strip())):
            return str(answer).strip(), None
        return answer, f"Integer key {answer!r} is not numeric"
    if qtype == "mcq":
        if not isinstance(answer, list):
            return answer, f"MCQ key {answer!r} is not a list"
        letters, problems = [], []
        for item in answer:
            if not isinstance(item, str) or not item.strip():
                problems.append(f"empty element {item!r}")
                continue
            parts = [p.strip().upper() for p in re.split(r"[,\s]+", item) if p.strip()]
            if len(parts) > 1:
                problems.append(f"several letters packed in one element {item!r}")
            letters.extend(parts)
        bad = [p for p in letters if p not in LETTERS]
        if bad:
            problems.append(f"non-letter values {bad}")
        norm = sorted(set(p for p in letters if p in LETTERS))
        return norm, ("; ".join(problems) if problems else None)
    return answer, f"unknown question type {qtype}"


_LATEX = re.compile(r"\\(frac|sqrt|AA|times|alpha|beta|theta|lambda|mu|pi|left|right|cdot|text|mathrm|degree|circ)\b|\$[^$]+\$")
_EXAM = re.compile(r"\(\s*(JEE|NEET|AIEEE|AIPMT|BITSAT|KCET|MHT[- ]?CET)[^)]{0,30}?\b(19|20)\d{2}\s*\)", re.I)
_FIG = re.compile(r"\b(as shown in (the )?(figure|diagram|fig\.?)|shown in figure|given figure|in the figure|refer(ring)? (to )?(the )?(figure|diagram))\b", re.I)
_NOISE = re.compile(r"(space for rough work|^\s*Q\.?\s*\d+[.)]|^\s*\d+\.\s)", re.I | re.M)


def question_findings(q: dict) -> list[dict]:
    """Facts about a question record that need no judgement."""
    out = []
    qtype = q["questionType"]
    b = body(q)
    qhtml, shtml = b.get("question") or "", b.get("solution") or ""
    qtext, stext = html_to_text(qhtml), html_to_text(shtml)
    stem, opts = split_options(qtext)

    def add(code, repair, field, detail, severity):
        out.append({"code": code, "repair_hint": repair, "field": field, "detail": detail, "severity": severity})

    norm, prob = normalize_answer(qtype, b.get("answer"))
    if prob:
        add("key_malformed", "answer_key", "answer", prob, "critical")

    if qtype in ("scq", "mcq"):
        present = [k for k in LETTERS if k in opts]
        missing = [k for k in LETTERS if k not in opts]
        blank = [k for k in present if not opts[k].strip()]
        if missing:
            add("option_missing", "option_repair", "question", f"options {missing} not present in question HTML (found {present})", "high")
        for k in blank:
            keyed = (qtype == "scq" and norm == k) or (qtype == "mcq" and isinstance(norm, list) and k in norm)
            add("option_blank", "option_repair", "question",
                f"option ({k}) is blank" + (" AND it is the keyed answer - every student is graded against an invisible option" if keyed else ""),
                "critical" if keyed else "high")
        seen = {}
        for k in present:
            t = _norm(opts[k])
            if t and t in seen:
                add("identical_options", "identical_options", "question", f"options ({seen[t]}) and ({k}) are identical: {opts[k]!r}", "high")
            seen.setdefault(t, k)
        if qtype == "mcq" and isinstance(norm, list) and len(norm) == 1:
            add("mcq_single_key", "type_mismatch", "questionType", f"stored as multi-correct but key has a single letter {norm}", "high")

    for field, text in (("question", qtext), ("solution", stext)):
        m = _LATEX.search(text)
        if m:
            add("latex_leak", "markup_leak", field, f"raw LaTeX visible as text: {m.group(0)!r}", "medium")
        m = _EXAM.search(text)
        if m:
            add("exam_reference", "exam_reference", field, f"exam tag in text: {m.group(0)!r}", "low")
        m = _NOISE.search(text)
        if m:
            add("stray_noise", "noise", field, f"stray text: {m.group(0).strip()!r}", "low")

    if _FIG.search(qtext) and not b.get("quesImages") and "<img" not in qhtml.lower():
        add("figure_missing", "diagram", "question", "text refers to a figure but quesImages is empty and there is no <img>", "high")

    if not stext.strip():
        add("solution_empty", "solution_missing", "solution", "solution field is empty", "medium")

    if qhtml.count("<span") != qhtml.count("</span>"):
        add("unbalanced_html", None, "question",
            f"{qhtml.count('<span')} <span> vs {qhtml.count('</span>')} </span> - options may wrap/cut on small screens", "info")

    if q.get("level") in (None, "", "None"):
        add("metadata_level_missing", None, "level", "difficulty level is null", "info")
    return out


_INJECTION = re.compile(
    r"(ignore|disregard|forget|override|bypass)\b.{0,40}\b(instruction|prompt|rule|above|previous|prior|system)|"
    r"you are (now )?(the |an? )?(admin|system|assistant|bot|ai|model)|"
    r"\b(system|assistant|developer|admin)\s*:|</?\s*(student_comment|system|instructions?)\b|"
    r"system prompt|developer mode|jailbreak|as an ai|"
    r"mark (this|the) report as|(change|set|update|make) (the )?(answer|key|answer key) (to|=|as)|"
    r"\b(answer|ans|key)\s*(is\s*)?\(?[a-d]\)?\s*(kar|kardo|kar do|set kar|bana)\b|"
    r"official correction|i am (the |your )?(teacher|faculty|admin|sir)|"
    r"(in|with) (your|the) reply.{0,30}(include|add|write|send)",
    re.I,
)
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "@": "a", "$": "s"})
_ZW = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


def _normalise(c: str) -> str:
    """Undo cheap evasions before matching: zero-width chars, leetspeak digits, spacing."""
    c = _ZW.sub("", c)
    return re.sub(r"\s+", " ", c.translate(_LEET))


def safe_comment(c: str, limit: int = 600) -> str:
    """Comment text as it may appear inside the prompt: cannot open/close tags, bounded length."""
    c = _ZW.sub("", c or "").replace("<", "‹").replace(">", "›")
    return c[:limit] + (" …[truncated]" if len(c) > limit else "")


_KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")


def report_flags(report: dict) -> dict:
    c = (report.get("comment") or "").strip()
    flags = {"injection": bool(_INJECTION.search(c) or _INJECTION.search(_normalise(c))), "empty": not c, "gibberish": False}
    letters = re.sub(r"[^a-z]", "", c.lower())
    if c and len(c) <= 12 and " " not in c and letters:
        run = max((sum(1 for ch in letters if ch in row) for row in _KEYBOARD_ROWS), default=0)
        vowels = sum(ch in "aeiou" for ch in letters)
        flags["gibberish"] = run == len(letters) and (vowels <= 1 or len(letters) >= 5)
    return flags


def fixed_after_report(q: dict, report: dict) -> list[dict]:
    """errorHistory entries written by faculty *after* the student reported."""
    try:
        t = datetime.fromisoformat(report["reportedAt"].replace("Z", "+00:00"))
    except Exception:
        return []
    out = []
    for h in q.get("errorHistory") or []:
        try:
            if datetime.fromisoformat(h["at"].replace("Z", "+00:00")) > t:
                out.append(h)
        except Exception:
            continue
    return out
