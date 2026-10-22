"""Dev/test split for the golden set.

- `dev`: cases we may look at while improving patterns and rules.
- `test`: held out. Never inspected while writing patterns; release numbers come from here.

Assignment is deterministic from the job id (about 30% test), except cases that were
already used to tune extract_errors or the rules: those are pinned to `dev`, because
scoring on them would overstate how well the patterns generalise.
"""

from __future__ import annotations

import zlib

TEST_SHARE_PERCENT = 30

# Cases whose logs were inspected to add or fix patterns/rules (see commit history).
TUNED_ON_JOB_IDS = frozenset({
    118078343556,  # golden-001 rocsparse page fault: GPU HANG ANALYSIS inside fault dump
    118755950473,  # rocprofiler-sdk SegFault: ctest crash -> failing test name
    118506197594,  # tensilelite-common: hipModuleLoad + continuation lines + grouping
    118285601270,  # rocgdb-gpu: suite summary + DejaGnu FAIL lines
    118930553597,  # hipthreads timeout reviewed while defining the timeout category
    119589847503,  # rockrel hipblaslt: "HW Exception ... GPU Hang" + noise in passing tests
})
TUNED_ON_CASE_IDS = frozenset({"golden-002"})  # hip-tests timeout (no job id recorded)


def is_tuned_on(case: dict) -> bool:
    return case.get("job_id") in TUNED_ON_JOB_IDS or case.get("case_id") in TUNED_ON_CASE_IDS


def assign_split(case: dict) -> str:
    if is_tuned_on(case):
        return "dev"
    key = str(case.get("job_id") or case.get("case_id"))
    return "test" if zlib.crc32(key.encode()) % 100 < TEST_SHARE_PERCENT else "dev"
