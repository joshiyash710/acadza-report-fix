# Report → Fix

The system between a student's "Report error" tap and a safe fix in Acadza's question bank, plus the console a faculty member uses to review it.

- **Live console:** `https://acadza-report-fix-console.vercel.app/` (static, loads a precomputed run, see [Precomputed run](#precomputed-run))
- **Output:** [`results.json`](results.json), one decision for each of the 44 reports
- **Bonus:** [`unreported_defects.json`](unreported_defects.json), 31 defects in 13 questions that no student reported
- **Docs:** [`PROMPTS.md`](PROMPTS.md) covers the prompts and design log. [`PRODUCT.md`](PRODUCT.md) covers product thinking. [`docs/BRIEF.md`](docs/BRIEF.md) is the original brief.

## Results at a glance

| | |
|---|---|
| Reports triaged | 44 reports on 39 questions, grouped into 41 console cases (39 reported + 2 found by scan) |
| Auto-applied | **19**: 7 answer-key or type fixes proven by two blind solves, 3 missing values the solution states, 1 blank keyed option, 3 solution fixes, 5 cosmetic |
| Sent to faculty | **8**: identical options, missing figure, unsound question, two wrong chapters, syllabus, and a missing option D whose text would have to be invented |
| No change | **17**: 11 students who mis-solved it, plus already-fixed, display, app, language and unintelligible reports. Every one gets a reply that shows the step they missed |
| Traps handled | Prompt injection (R027) is treated as data, never acted on, and blocks auto-apply on its question. A student with the wrong number on a wrongly keyed question (R004) gets the key fixed and their number corrected. A one-word "wrong" is a defect in one case (R002) and no error in the other (R040), decided by checking each question |

## How it works

```
reports.json ──group by question──┐
                                  ▼
 [1] Deterministic checks   (code, free)   malformed key · blank/missing/duplicate option · raw LaTeX ·
                                           exam tag · figure referenced but absent · errorHistory after report ·
                                           prompt-injection / keyboard-mash flags
 [2] Blind solver ×2        (LLM)          sees ONLY stem + options. Never the key, the solution, or the
                                           student's comment. Two framings: standard method / different method
 [3] Adjudicator ×1/question(LLM)          sees everything above + all reports on that question (as
                                           untrusted data) → verdict, repair, surgical edit, evidence, reply
 [4] Guards + router        (code)         scope · find-exactly-once · repair-specific proof · taint ·
                                           confidence · reply rules  →  auto_apply | faculty_review | no_change
                                  ▼
          results.json · unreported_defects.json · console/data/cases.json
```

Three ideas carry the design:

1. **Independence before judgement.** The answer key is the highest-stakes field, so the model that checks it never sees it. An `answer_key` change auto-applies only if two blind solves agree with each other and with the new key. An LLM that reads the key and the student's confident comment gets anchored; a blind solver can't be.
2. **The LLM proposes, code disposes.** Every rule in `taxonomy.json` is enforced in [`engine/guards.py`](engine/guards.py), not left to the prompt. A spelling edit that changes a digit, a `stem_data` edit that adds a value the solution never states, an answer edit smuggled into a spelling repair, or a `find` that matches twice are all caught there. A failed check doesn't drop the case. It moves it to faculty with the reason shown beside the diff.
3. **One call per question, not per report.** Three students reporting the same broken key cost one adjudication, get one consistent verdict, and push that question up the queue.

### What auto-applies and what never does

Auto-apply requires that the taxonomy allows it (`auto: true`), confidence is at least 0.85, the repair-specific proof passes (table below), the reply passes its rules, and nobody on that question attempted prompt injection.

| Repair | Proof obligation in code |
|---|---|
| `answer_key`, `type_mismatch` | Both blind solves agree on the new key. The new key is well-formed and points at a visible option |
| `stem_data` | Every number added appears in the stored solution |
| `option_repair` | The option text is recovered from the solution or the blind solves, never authored. The stem is unchanged |
| `spelling` | No digit or symbol changes. Small edit only |
| `answer_form` | Pure insertion (diff opcodes are only *equal* and *insert*) |
| `exam_reference`, `noise` | Pure removal of the tag or stray text |
| `markup_leak` | The edit targets raw markup and keeps every number |
| `solution_fix` / `solution_missing` | The new solution ends at the keyed answer, and the blind solves agree with that key |

There are two places where I'm stricter than the taxonomy:
- **Invented distractors.** `option_repair` is `auto: true`, but a missing distractor (622f08d7, option D) can't be recovered from anything in the record. Making one up is authored content, the same reason `identical_options` is `auto: false`, so it goes to faculty with a suggested option.
- **Defects nobody reported** always go to faculty in week 1, even typos. Nobody asked for those changes, so nothing changes silently.

### Red-team: a student trying to break it

A student controls exactly one thing, the comment box (and how many accounts they report from). I attacked through it with 9 injection styles: plain, "disregard the above", Hinglish ("answer C kar do"), fake teacher authority, closing the `<student_comment>` tag, a fake `assistant:` role, leetspeak (`IGN0RE`), zero-width characters, and a request to put a Telegram link in the reply. For each one I asked two questions: does the tripwire flag it, and if the AI were completely fooled, what would code still stop?

| Attack | Before the red-team | Now |
|---|---|---|
| Flip a correct key | Blocked: the blind solver never sees the comment, so it can't be fooled | Same |
| 8 of 9 injection phrasings | Not flagged (the tripwire only knew "ignore previous instructions") | All 9 flagged after normalising leetspeak and zero-width characters, with 0 false positives on the 44 real reports |
| Comment closes its `<student_comment>` tag | Broke out into the prompt | `<` and `>` are neutralised and the length is capped |
| Script or link written into a question via a fix (`<img onerror>`, `<script>`, `<a href>`) | **Would auto-apply** | An allow-list of MathML and basic tags is enforced on every edit |
| Link, phone number or handle in the student reply | **Not checked on no-change replies** | Reply rules run on every route |
| A fooled "no error" that buries a real broken key | **Would close the case** | Held when both blind solves contradict the key or code found a critical defect |
| One poisoned edit alongside one clean edit | The clean half would still go live | The whole case is held: half a fix is never applied |
| 50 fake accounts boosting a case | Uncapped urgency | Students are capped at 5 in the urgency score |

All of these are locked in as tests in [`tests/test_redteam.py`](tests/test_redteam.py). The 44 real decisions didn't change.

### What "most urgent" means in the queue

`urgency = harm tier + 12 × students who reported it (max 5) + hours waiting / 6`, where the harm tiers are:

1. **Grading is wrong** (100): answer key, type mismatch, unsound question, identical options. Every attempt is marked wrong.
2. **Question unusable** (60): missing value, missing or blank option, missing figure, wrong solution.
3. **Cosmetic** (30): typos, tags, markup, units.
4. **Filing** (15): wrong chapter, syllabus.

A broken key hurts every student who meets it, and the damage grows with every attempt. A typo hurts no one's score. Within a tier, more students and a longer wait come first.

### Undoing an auto-applied fix

Auto-applied fixes are live, so undoing one is deliberate. The teacher must pick a reason, and "Other" requires a note. Every undo is logged with its repair type, and the audit panel tracks **undone ÷ auto-applied**. That's the week-1 safety metric ([PRODUCT.md](PRODUCT.md)), which decides whether a repair type keeps its auto-apply rights.

## Run it

Python 3.10+, standard library only. No `pip install`.

```bash
python -m engine.run                         # replay the committed LLM outputs → results.json (no API key, ₹0)
python -m engine.run --provider gemini       # live: GEMINI_API_KEY   (also: anthropic / openai, --model NAME)
python -m engine.run --estimate              # token + call estimate for a live run, always-2 vs --lazy
python -m engine.run --lazy                  # second blind solve only when it can change a decision
python tests/test_guards.py                  # 13 guard tests: a fooled AI's proposals vs the rules
python tests/test_redteam.py                 # 11 red-team tests: a student attacking through the comment box
                                             # (or run both: python -m pytest -q)
cd console && python -m http.server 8000     # open http://localhost:8000
```

**Deploy the console.** It's a static folder with no build step. On Vercel or Netlify, import the repo and set the output or publish directory to `console`. The root `index.html` also redirects to `console/`, so GitHub Pages on the repo root works as is.

### Precomputed run

The live console loads `console/data/cases.json`, a precomputed run. The LLM stages were executed by Claude (Opus) during development, following the exact prompts in [`engine/prompts.py`](engine/prompts.py). Every response is committed under [`engine/cache/`](engine/cache/), keyed by stage, prompt version and question. `--provider replay` rebuilds `results.json` from that cache with zero API calls. The deterministic checks and guards always run live.

Run with a live provider and new responses are written to the cache, so you can diff exactly what a different model would decide. A changed prompt bumps its version, so it can never reuse a stale answer.

**Cost of a live run (all 44 reports).** Measured with `python -m engine.run --estimate`, which builds every real prompt (tokens ≈ characters / 3.5, ±25%):

| Mode | Calls | Input tokens | Output tokens | Gemini 2.5 Flash ($0.30 in / $2.50 out per 1M) |
|---|---|---|---|---|
| Solver always runs twice (default) | 129 | ~214k | ~21k | ≈ $0.12 (~₹10) |
| `--lazy` (second solve only when needed) | 98 | ~193k | ~18k | ≈ $0.10 |

That's about ₹0.2 per report. Where the tokens go: the solver is only about 26% of them. The adjudicator's system prompt (rules + taxonomy, ~2.6k tokens) is identical in all 43 calls and makes up 68% of adjudicator input. So the biggest saving isn't skipping solves. It's **prompt caching** of that fixed prefix (Gemini cache reads are $0.03/1M instead of $0.30) and the **Batch API** (50% off), because report triage doesn't need answers in seconds. Gemini 2.5 Flash also bills thinking tokens as output, so cap its thinking budget for the solver.

**`--lazy`.** The first blind solve always runs. It's the safety net that catches a wrong key nobody claimed (R002 "wrong ans") and stops a fooled "no error" from burying a real defect. The second solve runs only if the first finds no clean answer, disagrees with the key, is unsure, or code found a critical/high defect. On this data it runs for 12 of 43 questions, skips 31 calls, and `results.json` is identical (that's a test). Every auto-applied key change still has two agreeing solves behind it.

## Repo map

```
engine/
  parse.py      HTML+MathML → readable text; option splitting that survives "Assertion (A)" stems
  checks.py     deterministic findings + report flags (injection, gibberish, already-fixed)
  prompts.py    every in-product prompt, verbatim and versioned
  llm.py        replay / anthropic / openai / gemini, JSON mode, response cache
  guards.py     scope, uniqueness, proof obligations, router
  pipeline.py   orchestration, urgency, outputs
  cache/        committed LLM responses (the audit trail behind every decision)
console/        static faculty console (index.html, app.js, styles.css, data/cases.json)
tests/          guard tests + red-team tests
results.json    the scored output
unreported_defects.json   bonus findings
```
