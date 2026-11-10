"""Adversarial tests: proposals a careless or manipulated LLM could make, and the guard that must stop them.

Run:  python -m pytest -q   (or)   python tests/test_guards.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.checks import report_flags  # noqa: E402
from engine.guards import route  # noqa: E402
from engine.parse import load_all  # noqa: E402

QS, REPORTS, TAX = load_all()
AGREE = lambda ans: [{"solvable": True, "answer": ans, "final_value": ""}] * 2  # noqa: E731
REPLY = "Thanks for reporting - we checked it."


def P(**kw):
    base = {"verdict": "defect", "confidence": 0.97, "student_reply": REPLY, "edits": []}
    base.update(kw)
    return base


def test_injection_obeyed_is_blocked():
    """R027: an LLM that obeys 'change the answer to C' must not auto-apply."""
    q = QS["6062ec84518e9535e120c422"]
    g = route(P(repair="answer_key", edits=[{"field": "answer", "find": "A", "replace": "C"}]), q, TAX, AGREE("A"), group_tainted=True)
    assert g["route"] == "faculty_review"
    assert any("injection" in l for l in g["guard_log"]) and any("blind solves say A" in l for l in g["guard_log"])


def test_key_flip_without_solver_agreement_is_held():
    """R029: student insists mu = 0.4 (B); solves say A. Flipping the key must be held."""
    q = QS["68e8734442c06737d6e56da4"]
    g = route(P(repair="answer_key", edits=[{"field": "answer", "find": "A", "replace": "B"}]), q, TAX, AGREE("A"), False)
    assert g["route"] == "faculty_review"


def test_key_flip_when_solvers_disagree_is_held():
    q = QS["60f744b3f1b9bc7cc7b4a949"]
    solves = [{"solvable": True, "answer": "A"}, {"solvable": True, "answer": "B"}]
    g = route(P(repair="answer_key", edits=[{"field": "answer", "find": "C", "replace": "A"}]), q, TAX, solves, False)
    assert g["route"] == "faculty_review"


def test_out_of_scope_edit_is_stripped():
    """spelling may touch question/solution only - an answer edit smuggled in must be dropped."""
    q = QS["61dffbec08489156b684bd22"]
    edits = [{"field": "question", "find": "Epithellial tisue", "replace": "Epithelial tissue"},
             {"field": "answer", "find": "A", "replace": "B"}]
    g = route(P(repair="spelling", edits=edits), q, TAX, AGREE("A"), False)
    assert all(e["field"] != "answer" for e in g["edits"])
    assert any("scope" in l for l in g["guard_log"])


def test_spelling_edit_that_changes_a_number_is_held():
    q = QS["60ca4feb6420652745f55522"]
    g = route(P(repair="spelling", edits=[{"field": "question", "find": "121 N", "replace": "120 N"}]), q, TAX, AGREE("C"), False)
    assert g["route"] == "faculty_review"


def test_find_must_be_unique():
    q = QS["6956d79788d7701e5617bc29"]  # "0 W" appears 4x: in B, C, and inside "200 W" and "1000 W"
    g = route(P(repair="spelling", edits=[{"field": "question", "find": "0 W", "replace": "0 Watt"}]), q, TAX, AGREE("B"), False)
    assert g["route"] == "faculty_review" and any("occurs 4x" in l for l in g["guard_log"])


def test_stem_data_cannot_invent_values():
    """Adding an inductance the solution never states must be held."""
    q = QS["60f742f6660d187cc13a8078"]
    g = route(P(repair="stem_data", edits=[{"field": "question", "find": "and an inductance are connected",
                                             "replace": "and an inductance of 70 mH are connected"}]), q, TAX, AGREE("C"), False)
    assert g["route"] == "faculty_review" and any("not stated in the stored solution" in l for l in g["guard_log"])


def test_auto_false_repairs_never_auto_apply():
    q = QS["6956d79788d7701e5617bc29"]
    g = route(P(repair="identical_options", confidence=0.99), q, TAX, AGREE("B"), False)
    assert g["route"] == "faculty_review"


def test_low_confidence_is_held():
    q = QS["61dffbec08489156b684bd22"]
    g = route(P(repair="spelling", confidence=0.6, edits=[{"field": "question", "find": "Epithellial tisue", "replace": "Epithelial tissue"}]),
              q, TAX, AGREE("A"), False)
    assert g["route"] == "faculty_review"


def test_reply_that_mentions_ai_is_held():
    q = QS["61dffbec08489156b684bd22"]
    g = route(P(repair="spelling", student_reply="Our AI fixed it.", edits=[{"field": "question", "find": "Epithellial tisue", "replace": "Epithelial tissue"}]),
              q, TAX, AGREE("A"), False)
    assert g["route"] == "faculty_review"


def test_no_error_verdict_drops_edits():
    q = QS["68e8734442c06737d6e56da4"]
    g = route(P(verdict="no_error", edits=[{"field": "answer", "find": "A", "replace": "B"}]), q, TAX, AGREE("A"), False)
    assert g["route"] == "no_change" and not g["edits"]


def test_lazy_solver_gives_identical_decisions():
    """--lazy skips the second blind solve unless it can change a decision; results must not change."""
    from engine.pipeline import run
    full, lazy = run(lazy=False), run(lazy=True)
    assert full["results"] == lazy["results"]
    solver_calls = sum(1 for m in lazy["meter"] if m["stage"] == "solver")
    assert solver_calls < sum(1 for m in full["meter"] if m["stage"] == "solver")
    # every answer-changing fix still had two agreeing blind solves behind it
    for c in lazy["cases"]:
        if c["repair"] in ("answer_key", "type_mismatch") and c["route"] == "auto_apply":
            assert len(c["solves"]) == 2


def test_flags():
    by_id = {r["reportId"]: r for r in REPORTS}
    assert report_flags(by_id["R027"])["injection"]
    assert report_flags(by_id["R041"])["gibberish"]
    assert not report_flags(by_id["R040"])["gibberish"]  # "wrong" is vague, not gibberish
    assert not any(report_flags(by_id[r])["injection"] for r in by_id if r != "R027")


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
        print("ok ", f.__name__)
    print(f"{len(fns)} passed")
