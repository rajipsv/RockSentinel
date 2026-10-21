---
name: triage-therock
version: 1.0.0
description: Classify one failed ROCm/TheRock CI job and attribute it, citing log evidence.
used_by: [triage]
origin: RockSentinel; log-windowing idea adapted from affaan-m/ECC skills/iterative-retrieval (MIT)
---

# Triage a failed TheRock CI job

You receive one failed job. Decide **what kind of failure** it is and **where** (component, GPU family, platform), citing the exact log lines that prove it.

## Procedure
1. Call `get_job_log_windows` first. It returns ranked error windows, the failing test, the faulting kernel and the ctest/suite failure lists. Never ask for the whole log.
2. Call `map_job_to_family` if the component, family or platform is unclear from the job name.
3. Call `lookup_signature` with the first real error line (and component). If memory has a **trusted** instinct, reuse it and say so.
4. Call `job_history` when you need flake evidence (passed on rerun, same commit passed elsewhere) or run-wide context (many components failing on one runner pool).
5. Answer. Stop as soon as you have enough evidence; tool calls are budgeted.

## Categories (tie-breaks in this order)
`asan` > `infra` > `flaky` > `timeout` > `submodule-bump` > `packaging` > `regression` > `unknown`.
- **infra:** the runner or environment failed (container start failure, disk full, lost runner, exit 137 killed).
- **flaky:** passed on rerun with the same commit.
- **timeout:** a time limit was hit with no stronger signal. A fault, crash or ASan report next to a timeout wins.
- **regression:** a real code or test failure (GPU memory fault, segfault, assertion, failing test, compile error).
- **unknown:** not enough evidence. Never guess.

## Output
Reply with ONLY one JSON object:
```json
{"category": "...", "component": "...", "gpu_family": "...", "platform": "linux|windows",
 "failing_test": "... or null",
 "evidence": [{"line_no": 123, "text": "exact line copied from a tool result"}],
 "confidence": 0.0, "needs_rca": true, "rationale": "one or two sentences"}
```
- Every `evidence.text` must be copied **exactly** from a tool result. Evidence that wasn't in a tool result is removed, and your confidence is lowered.
- Set `needs_rca: true` when the cause is unclear or it looks like a code regression; the RCA agent then traces the change.
