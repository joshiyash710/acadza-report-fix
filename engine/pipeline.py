"""Report -> Fix pipeline.

    reports ──group by question──▶ [1] deterministic checks (free)
                                   [2] blind solver ×2      (never sees key / solution / comments)
                                   [3] adjudicator ×1 per question (all its reports together)
                                   [4] guards + router      (code, not LLM)
                                   ──▶ results.json, console/data/cases.json

Run:  python -m engine.run                 # replay committed LLM outputs (no key needed)
      python -m engine.run --provider gemini   # live, writes new cache entries
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from . import prompts as P
from .checks import fixed_after_report, question_findings, report_flags, safe_comment
from .guards import route
from .llm import CACHE, LLM, LLMError
from .parse import ROOT, body, html_to_text, load_all, project

TIER = {  # how much damage a defect of this kind does per student who meets it
    "answer_key": 1, "type_mismatch": 1, "unsound": 1, "identical_options": 1,
    "option_repair": 2, "stem_data": 2, "diagram": 2, "wrong_question": 2, "solution_fix": 2, "solution_missing": 2,
    "notation": 3, "markup_leak": 3, "spelling": 3, "noise": 3, "exam_reference": 3, "answer_form": 3,
    "wrong_chapter": 4, "syllabus": 4,
}
TIER_LABEL = {1: "Grading is wrong", 2: "Question unusable", 3: "Cosmetic", 4: "Filing / metadata", 9: "No defect"}
TIER_WEIGHT = {1: 100, 2: 60, 3: 30, 4: 15, 9: 0}


def _options_block(options: dict) -> str:
    if not options:
        return "(no options - integer answer)"
    return "\n".join(f"({k}) {v if v.strip() else '[BLANK]'}" for k, v in options.items())


def needs_second_solve(first: dict, proj: dict, findings: list) -> str | None:
    """Lazy mode: the second blind solve only runs when it can change a decision. Returns the reason, or None."""
    from .guards import _norm_val
    if not first.get("solvable", True) or first.get("answer") in (None, "", []):
        return "first solve found no clean answer"
    if _norm_val(first.get("answer")) != _norm_val(proj["answer"]):
        return "first solve disagrees with the stored key"
    if any(f["severity"] in ("critical", "high") for f in findings):
        return "code found a critical/high defect"
    if float(first.get("confidence") or 0) < 0.8:
        return "first solve is unsure"
    return None


def solve(llm: LLM, proj: dict, findings: list | None = None, lazy: bool = False) -> list[dict]:
    out = []
    for variant, framing in P.SOLVER_FRAMING.items():
        if lazy and variant == "alternate":
            why = needs_second_solve(out[0], proj, findings or [])
            if not why:
                break
            out[0]["second_solve_reason"] = why
        user = P.SOLVER_USER.format(framing=framing, subject=proj["subject"], chapter=proj["chapter"],
                                    qtype_human=P.qtype_human(proj["type"]), stem=proj["stem"] or "(see options)",
                                    options_block=_options_block(proj["options"]))
        res = llm.json_call("solver", P.SOLVER_VERSION, f"{proj['id']}.{variant}", P.SOLVER_SYSTEM, user)
        res["variant"] = variant
        out.append(res)
    return out


def adjudicate(llm: LLM, q: dict, proj: dict, findings: list, solves: list, reports: list, taxonomy: dict) -> dict:
    b = body(q)
    blocks = []
    for r in reports:
        f = report_flags(r)
        fx = fixed_after_report(q, r)
        blocks.append(
            f"- reportId: {r['reportId']} | source: {r['source']} | reportedAt: {r['reportedAt']}"
            f" | code_flags: {json.dumps(f)}" + (f" | faculty_edits_after_this_report: {json.dumps(fx, ensure_ascii=False)}" if fx else "")
            + f"\n  <student_comment>{safe_comment(r['comment'])}</student_comment>"
        )
    tax = json.dumps({"verdicts": taxonomy["verdicts"], "repairs": taxonomy["repairs"]}, indent=1)
    system = P.ADJUDICATOR_SYSTEM.format(taxonomy=tax)
    user = P.ADJUDICATOR_USER.format(
        qid=q["_id"], qtype=q["questionType"], subject=q.get("subject"), chapter=q.get("chapter"), level=q.get("level"),
        error_history=json.dumps(q.get("errorHistory") or [], ensure_ascii=False),
        stem=proj["stem"], options_json=json.dumps(proj["options"], ensure_ascii=False),
        answer_json=json.dumps(b.get("answer")), solution_text=proj["solution"],
        raw_question=b.get("question") or "", raw_solution=b.get("solution") or "",
        findings_json=json.dumps(findings, indent=1, ensure_ascii=False),
        solves_json=json.dumps(solves, indent=1, ensure_ascii=False),
        reports_block="\n".join(blocks) if blocks else "(none - proactive scan)",
    )
    if len(solves) == 2:
        return llm.json_call("adjudicator", P.ADJUDICATOR_VERSION, q["_id"], system, user)
    try:
        return llm.json_call("adjudicator", P.ADJUDICATOR_VERSION, f"{q['_id']}.s1", system, user)
    except LLMError:
        if llm.provider != "replay":
            raise
        # replay only: no 1-solve recording exists, reuse the 2-solve decision (prompt size is still metered from `user`)
        path = CACHE / "adjudicator" / P.ADJUDICATOR_VERSION / f"{q['_id']}.json"
        response = json.loads(path.read_text(encoding="utf-8"))["response"]
        llm.meter.append({"stage": "adjudicator", "system": len(system), "user": len(user),
                          "output": len(json.dumps(response, ensure_ascii=False)), "approx": True})
        return response


def _render_record(q: dict) -> dict:
    b = body(q)
    return {"questionType": q["questionType"], "question": b.get("question") or "", "solution": b.get("solution") or "",
            "answer": b.get("answer")}


def run(provider: str = "replay", model: str | None = None, lazy: bool = False) -> dict:
    qs, reports, taxonomy = load_all()
    llm = LLM(provider, model)
    by_q = defaultdict(list)
    for r in sorted(reports, key=lambda r: r["reportedAt"]):
        by_q[r["questionId"]].append(r)

    results, cases = [], []
    now = max(datetime.fromisoformat(r["reportedAt"].replace("Z", "+00:00")) for r in reports)

    for qid in list(by_q) + [i for i in qs if i not in by_q]:  # reported first, then proactive scan
        q = qs[qid]
        group = by_q.get(qid, [])
        proj = project(q)
        findings = question_findings(q)
        solves = solve(llm, proj, findings, lazy)
        adj = adjudicate(llm, q, proj, findings, solves, group, taxonomy)
        props = {p["reportId"]: p for p in adj.get("reports", [])}
        tainted = any(report_flags(r)["injection"] or props.get(r["reportId"], {}).get("injection_attempt") for r in group)

        case_reports, case_route, case_edits, case_after, case_repair, case_verdict = [], "no_change", [], None, None, None
        best = None
        for r in group:
            p = props.get(r["reportId"]) or {"verdict": "not_actionable", "confidence": 0.0,
                                              "evidence": "adjudicator returned nothing for this report",
                                              "student_reply": "Thanks for reporting - a faculty member will take a look."}
            g = route(p, q, taxonomy, solves, tainted, findings)
            edits = g["edits"]
            rec = {
                "reportId": r["reportId"], "questionId": qid,
                "verdict": g["verdict"], "repair": g["repair"],
                "edit": edits[0] if edits else None,
                "route": g["route"], "confidence": round(float(p.get("confidence") or 0), 2),
                "evidence": p.get("evidence", ""), "student_reply": p.get("student_reply", ""),
            }
            if len(edits) > 1:
                rec["extra_edits"] = edits[1:]
            results.append(rec)
            case_reports.append({**rec, "edits": edits, "comment": r["comment"], "source": r["source"],
                                 "studentId": r["studentId"], "reportedAt": r["reportedAt"], "claim": p.get("claim", ""),
                                 "suggestion": p.get("suggestion", ""), "derived_from": p.get("derived_from"),
                                 "flags": report_flags(r), "injection_attempt": bool(p.get("injection_attempt")) or report_flags(r)["injection"],
                                 "guard_log": g["guard_log"]})
            # the case takes the most severe defect among its reports
            rank = TIER.get(g["repair"], 9) if g["verdict"] == "defect" else 9
            if best is None or rank < best[0] or (rank == best[0] and g["route"] == "faculty_review"):
                best = (rank, g)
        if best:
            g = best[1]
            case_route, case_edits, case_after, case_repair, case_verdict = g["route"], g["edits"], g["q_after"], g["repair"], g["verdict"]
            if any(c["route"] == "faculty_review" for c in case_reports):
                case_route = "faculty_review" if case_route != "auto_apply" else case_route

        # proactive findings (no student reported them): always a human decision in week 1
        bonus = []
        for u in adj.get("unreported_defects", []) or []:
            item = dict(u)
            spec = taxonomy["repairs"].get(u.get("repair"))
            item["in_scope"] = bool(spec and (u.get("field") in spec["fields"] or not spec["fields"]))
            item["applies"] = False
            if u.get("find") and u.get("field") in ("question", "solution"):
                item["applies"] = (body(q).get(u["field"]) or "").count(u["find"]) == 1
            elif not u.get("find"):
                item["applies"] = None  # metadata / judgement suggestion, nothing to apply
            item["route"] = "faculty_review"
            bonus.append(item)

        tier = TIER.get(case_repair, 9) if case_verdict == "defect" else 9
        students = len({r["studentId"] for r in group})
        oldest = min((datetime.fromisoformat(r["reportedAt"].replace("Z", "+00:00")) for r in group), default=now)
        wait_h = (now - oldest).total_seconds() / 3600
        # urgency = damage per student x students hurt x how long it has been hurting them
        # students are capped at 5 so a burst of fake accounts can't outrank a real tier-1 defect
        urgency = TIER_WEIGHT[tier] + 12 * min(students, 5) + min(wait_h, 120) / 6
        if not group and bonus:  # nobody reported it yet: below any reported defect of the same tier
            tier_bonus = min(TIER.get(b.get("repair"), 9) for b in bonus)
            urgency = TIER_WEIGHT.get(tier_bonus, 0) * 0.5

        if not group and not bonus:
            continue
        cases.append({
            "questionId": qid, "subject": q.get("subject"), "chapter": q.get("chapter"), "level": q.get("level"),
            "questionType": q["questionType"], "summary": adj.get("question_summary", ""),
            "route": case_route if group else ("faculty_review" if bonus else "no_change"),
            "verdict": case_verdict, "repair": case_repair,
            "tier": tier, "tierLabel": TIER_LABEL[tier], "urgency": round(urgency, 1),
            "students": students, "oldestReport": oldest.isoformat() if group else None,
            "before": _render_record(q), "after": _render_record(case_after) if case_after else None,
            "edits": case_edits, "reports": case_reports, "unreported": bonus,
            "findings": findings, "solves": solves,
            "proactive": not group,
        })

    results.sort(key=lambda r: r["reportId"])
    cases.sort(key=lambda c: (-c["urgency"]))
    meta = {"provider": provider, "model": llm.model, "live_calls": llm.calls, "cache_hits": llm.cache_hits, "solver_mode": "lazy" if lazy else "always-2",
            "solver_version": P.SOLVER_VERSION, "adjudicator_version": P.ADJUDICATOR_VERSION,
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "taxonomy": taxonomy}
    return {"results": results, "cases": cases, "meta": meta, "meter": llm.meter}


def write_outputs(out: dict) -> None:
    (ROOT / "results.json").write_text(json.dumps(out["results"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    bonus = [{"questionId": c["questionId"], "subject": c["subject"], "chapter": c["chapter"], **{k: u[k] for k in
              ("repair", "field", "find", "replace", "evidence", "confidence", "applies", "route")}}
             for c in out["cases"] for u in c["unreported"]]
    (ROOT / "unreported_defects.json").write_text(json.dumps(bonus, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    console = ROOT / "console" / "data"
    console.mkdir(parents=True, exist_ok=True)
    (console / "cases.json").write_text(json.dumps({"meta": out["meta"], "cases": out["cases"]}, ensure_ascii=False), encoding="utf-8")
