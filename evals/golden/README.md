# Golden eval set

Real, human-reviewed ROCm/TheRock CI failures used by the L2 offline evals (see `docs/ADLC-RockSentinel.md` §4.2).

- `dataset.jsonl`: one case per line. `expected` is the ground truth; `must_cite` lines must appear in the agent's evidence (checked by the grounding metric).
- Log excerpts live in `tests/fixtures/logs/` and are copied by hand from the GitHub "Test" step (logs need a sign-in).
- `label_status`: `provisional` until the case's `open_questions` are resolved, then `confirmed`. Only `confirmed` cases count toward release gates.
- `baseline_comparison.therock_hud_ai` records what TheRock HUD's AI Investigation said for the same failure, for side-by-side reporting.

| Case | Job | Expected | Status |
|---|---|---|---|
| golden-001 | rocsparse shard 2/3, gfx1151, run 39249671429 | regression: GPU memory fault in `csrgemm_bitmap_emit_kernel<256,long,long>` (rocm-libraries #12830). The HUD said platform instability | provisional (cross-family check pending) |
| golden-002 | hip-tests shard 4/4, gfx1151, run 39249671429 | infra/platform hang: 600 s per-test timeouts, 15/1284 done, 120 min step timeout | provisional (timeout count pending) |
