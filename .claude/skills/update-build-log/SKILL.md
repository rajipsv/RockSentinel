---
name: update-build-log
description: Update docs/BUILD-LOG.md (the interview-oriented build log) after a RockSentinel roadmap step or notable fix is finished, before its commit. Use whenever a step from CLAUDE.md's roadmap is completed, when the golden set or eval numbers change meaningfully, or when the user asks to update the build log.
---

# Update the RockSentinel build log

`docs/BUILD-LOG.md` tells the build story for interviews: what was built, why, the decisions and trade-offs, what went wrong and how it was found, and the numbers. Keep it accurate. It is read by interviewers and must survive "how do you know that?" follow-ups.

## When
- After a roadmap step's code and tests are done, **before** the step's commit, so the log update ships in the same commit.
- After a notable fix or decision inside a step (new category, overfitting fix, security bug found).

## Steps
1. **Collect the facts:**
   - what changed: `git diff --stat` and the step's files; the roadmap in `CLAUDE.md`;
   - fresh numbers: `.venv/Scripts/python -m rocksentinel.evals.runner` (use the **test** split as the headline number) and, if extraction changed, `.venv/Scripts/python -m rocksentinel.evals.coverage --sample 25`;
   - test count: `.venv/Scripts/python -c "import subprocess,sys; out=subprocess.run([sys.executable,'-m','pytest','--collect-only','-q'],capture_output=True,text=True).stdout; print(sum(int(l.rsplit(': ',1)[1]) for l in out.splitlines() if l.startswith('tests/') and ': ' in l))"` (avoid `$`-placeholders here: skill arguments are substituted into them).
2. **Update the "Current numbers" table** and the golden set, coverage and test counts. Never round in the system's favour, and keep the known caveats (e.g. B1's home advantage on B1-proposed labels).
3. **Add or extend the step's section,** using this shape:
   ```
   ## Step N: <title> (<short commit hash or "this commit">)
   **Built:** <what exists now, one or two sentences>
   **Why / decisions:** <trade-offs and the alternative rejected>
   **Found:** <bugs, surprises, fixes, and how they were caught (test, eval, review)>
   **Interview talking point(s):** "<one or two quotable lines>"
   ```
   Keep it to about 10–15 lines per step, and link files rather than pasting code.
4. **Keep "The story in 60 seconds" true.** Update it only if the headline changed (for example, the first agent beats B1).
5. **Write facts, not plans.** Things not done yet belong in the roadmap, not the log. If a number came from a small sample, say so.
6. **Stage `docs/BUILD-LOG.md` with the step's commit.** The commit itself follows CLAUDE.md: one commit per step, ask before pushing.

## Don'ts
- No secrets, tokens or signed URLs, ever.
- No claims about TheRock HUD internals beyond what was observed (mark them as observations, with dates).
- Don't rewrite history: past entries change only to fix a factual error. Note such corrections inline.
