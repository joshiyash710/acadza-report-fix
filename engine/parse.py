"""Parsing helpers for Acadza's production question format.

Questions arrive as messy HTML with inline MathML and the options embedded
inside the question HTML. The LLM never needs the raw markup to *reason*
(it needs it only to propose a byte-exact `find` string), so we build a
compact, linear text projection that keeps math readable:

    <mfrac><mn>1</mn><mn>2</mn></mfrac>  ->  (1)/(2)
    <msup><mi>r</mi><mn>2</mn></msup>    ->  r^(2)

The projection is also what the console diff and the deterministic checks use.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

QTYPES = ("scq", "mcq", "integerQuestion")
_VOID = {"br", "img", "hr", "input", "meta", "mspace", "none", "mprescripts"}


class _Node:
    __slots__ = ("tag", "attrs", "kids")

    def __init__(self, tag, attrs):
        self.tag, self.attrs, self.kids = tag, dict(attrs), []


class _TreeBuilder(HTMLParser):
    """Forgiving tree builder: production HTML has unbalanced <span>s."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", [])
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs)
        self.stack[-1].kids.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].kids.append(_Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return  # unmatched close tags are ignored

    def handle_data(self, data):
        self.stack[-1].kids.append(data)


def _render(n) -> str:
    if isinstance(n, str):
        return n
    t = n.tag
    kids = [
        c
        for c in n.kids
        if not (isinstance(c, str) and not c.strip() and t.startswith("m") and t != "mtext")
    ]
    join = lambda xs: "".join(_render(x) for x in xs)  # noqa: E731
    if t == "br":
        return "\n"
    if t == "img":
        return "[image]"
    if t == "sup":
        return "^(" + join(kids) + ")"
    if t == "sub":
        return "_(" + join(kids) + ")"
    if t == "mfrac" and len(kids) >= 2:
        return f"({_render(kids[0])})/({_render(kids[1])})"
    if t == "msup" and len(kids) >= 2:
        return f"{_render(kids[0])}^({_render(kids[1])})"
    if t == "msub" and len(kids) >= 2:
        return f"{_render(kids[0])}_{_render(kids[1])}"
    if t == "msubsup" and len(kids) >= 3:
        return f"{_render(kids[0])}_{_render(kids[1])}^({_render(kids[2])})"
    if t == "msqrt":
        return f"sqrt({join(kids)})"
    if t == "mroot" and len(kids) >= 2:
        return f"root{_render(kids[1])}({_render(kids[0])})"
    if t in ("mover", "munder") and len(kids) >= 2:
        return f"{_render(kids[0])}{_render(kids[1])}"
    if t == "math":
        return " " + join(kids) + " "
    if t in ("p", "h1", "h2", "h3", "h4", "div", "li", "tr"):
        return join(kids) + "\n"
    return join(kids)


def html_to_text(html: str | None) -> str:
    """Readable plain-text projection of question/solution HTML (math kept linear)."""
    if not html:
        return ""
    b = _TreeBuilder()
    b.feed(html)
    s = _render(b.root)
    s = s.replace("​", "")
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


_OPT_RE = re.compile(r"\(\s*([A-D])\s*\)")


def split_options(question_text: str) -> tuple[str, dict[str, str]]:
    """Split the text projection into (stem, {letter: option_text}).

    Options live inside the question HTML as "(A) ... (B) ...". We take the
    *last* run of A..D markers so that "Assertion (A)" in a stem is not
    mistaken for an option.
    """
    marks = list(_OPT_RE.finditer(question_text))
    # find the last occurrence of "(A)" that starts a monotonic A,B,C.. run
    start_idx = None
    for i in range(len(marks) - 1, -1, -1):
        if marks[i].group(1) == "A":
            start_idx = i
            break
    if start_idx is None:
        return question_text.strip(), {}
    run, expected = [], "A"
    for m in marks[start_idx:]:
        if m.group(1) == expected:
            run.append(m)
            expected = chr(ord(expected) + 1)
    stem = question_text[: run[0].start()].strip()
    opts = {}
    for j, m in enumerate(run):
        end = run[j + 1].start() if j + 1 < len(run) else len(question_text)
        opts[m.group(1)] = question_text[m.end():end].strip()
    return stem, opts


def body(q: dict) -> dict:
    return q.get(q["questionType"]) or {}


def load_all():
    qs = json.loads((DATA / "questions.json").read_text(encoding="utf-8"))
    reports = json.loads((DATA / "reports.json").read_text(encoding="utf-8"))
    taxonomy = json.loads((DATA / "taxonomy.json").read_text(encoding="utf-8"))
    return {q["_id"]: q for q in qs}, reports, taxonomy


def project(q: dict) -> dict:
    """Everything the LLM stages need about a question, as plain text."""
    b = body(q)
    qtext = html_to_text(b.get("question"))
    stem, options = split_options(qtext)
    return {
        "id": q["_id"],
        "type": q["questionType"],
        "subject": q.get("subject"),
        "chapter": q.get("chapter"),
        "stem": stem,
        "options": options,
        "solution": html_to_text(b.get("solution")),
        "answer": b.get("answer"),
        "has_images": bool(b.get("quesImages")),
        "error_history": q.get("errorHistory") or [],
        "updatedAt": q.get("updatedAt"),
    }
