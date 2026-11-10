# PRODUCT.md

## Who loses the most when a question is broken

**The student who got it right and was marked wrong.** They solved R003's LCR question correctly (√(40² + 30²) = 50 V), lost marks, and the key said 130 V. Two weeks before JEE, they don't decide the key is broken. They decide they don't understand phasors. A wrong key costs them marks and trust in their own method. It does this to every student who meets the question, silently, until someone fixes it. Only a few of them ever report it.

How the design reflects that:
- **The queue is ordered by harm, not arrival.** Wrong grading comes first (keys, malformed MCQ keys, identical options, no correct option), and a key fix proven by two blind solves goes live without waiting for anyone. A typo can wait. A wrong key can't.
- **Replies are written for that student.** When they're right, we say so plainly ("Your working is exactly right"). When they're wrong, we show the one step they missed with the real numbers ("0.08 is right, in metres; the question asks cm"), never a bare "no error". When the student's number is wrong but the key was also wrong (R004), we tell them both.
- **Vague reports still get checked.** "wrong ans" (R002) led to a real broken key. The comment is a lead, not a verdict.

## What I would never let the AI auto-apply

1. **Any content a human has to author:** new distractors, figures, rewritten stems, a chapter move, a syllabus call. The model can suggest, but only faculty can write it. A plausible-looking invented option is worse than a missing one, because nobody notices it.
2. **An answer-key change without independent proof.** It needs two blind solves that agree with each other and with the new key. The stored solution, the student's confidence and the model's own reading don't count.
3. **Anything on a question where a report tried to instruct the system.** R027 asked the bot to change the answer to C. The answer stays A (γ = 3α), and that question is barred from auto-apply even for unrelated fixes, so an attacker can't use a real typo to sneak an edit through.
4. **Changes nobody asked for** (the 31 scan findings). In week 1 every one goes to faculty until we know the scanner's precision.

## One metric for week 1

**Reverted auto-applies ÷ auto-applies.** This is the share of live fixes that faculty undo. The console requires a reason for every undo and shows this ratio by repair type in the audit panel, so the metric is measured from day one. It's the only number that tells us whether auto-apply is safe enough to keep, and it sets the confidence threshold per repair type: a repair type with zero reverts earns a lower bar, and any revert of an `answer_key` fix pauses that repair type. I'd track time-to-fix for tier-1 defects alongside it, but safety comes first.

## One idea beyond the brief: catch broken questions before anyone reports them

Only a few students ever report a broken question. Every attempt is a silent signal, though. When the strongest students (top decile on that chapter) pick one wrong option far more often than the key, the key is probably wrong. Run the blind solver automatically on any question whose top-decile accuracy drops below its difficulty band, and send it into this same pipeline as a scan case. The 31 defects this build found by reading every question show how many problems sit unreported. Attempt data would surface the grading-breaking ones within hours of a question going live.
