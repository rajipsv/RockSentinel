"""Score systems (B0, B1, later the agents) on the golden set.

Run:
    python -m rocksentinel.evals.runner
    python -m rocksentinel.evals.runner --confirmed-only --out evals/reports/latest.json

Metrics map to the problems in the design doc: category accuracy and macro-F1 (P2),
component / family / platform match (P3), failing test and kernel (P1), and the
grounding rate (share of required evidence lines the system actually surfaces).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from rocksentinel.baseline.systems import SYSTEMS, Prediction

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = ROOT / "evals" / "golden" / "dataset.jsonl"


def load_cases(path: Path = DEFAULT_DATASET, confirmed_only: bool = False,
               split: str | None = None) -> list[dict]:
    """`split`: None/"all", "dev" (may be inspected when tuning) or "test" (held out)."""
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if confirmed_only:
        cases = [c for c in cases if c.get("label_status") == "confirmed"]
    if split and split != "all":
        cases = [c for c in cases if c.get("split") == split]
    return cases


def norm_component(name: str | None) -> str | None:
    """'rocSPARSE' -> 'rocsparse'; 'hip-tests (HIP runtime / hiprtc)' -> 'hip-tests'."""
    if not name:
        return None
    return name.split(" (")[0].strip().lower()


def macro_f1(gold: list[str], pred: list[str]) -> float:
    labels = sorted(set(gold) | set(pred))
    scores = []
    for label in labels:
        tp = sum(g == p == label for g, p in zip(gold, pred, strict=True))
        fp = sum(p == label and g != label for g, p in zip(gold, pred, strict=True))
        fn = sum(g == label and p != label for g, p in zip(gold, pred, strict=True))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return round(sum(scores) / len(scores), 3) if scores else 0.0


def grounding(expected: dict, pred: Prediction) -> float:
    required = expected.get("must_cite") or []
    if not required:
        return 1.0
    evidence = "\n".join(pred.evidence)
    return sum(s in evidence for s in required) / len(required)


def _rate(pairs: list[tuple[object, object]]) -> float | None:
    return round(sum(a == b for a, b in pairs) / len(pairs), 3) if pairs else None


def evaluate(
    cases: list[dict],
    systems: dict[str, Callable[..., Prediction]] = SYSTEMS,
    root: Path = ROOT,
) -> dict:
    report: dict = {"n_cases": len(cases), "n_confirmed": sum(
        c.get("label_status") == "confirmed" for c in cases), "systems": {}, "cases": []}
    per_system: dict[str, list[tuple[dict, Prediction]]] = {name: [] for name in systems}

    for case in cases:
        log = (root / case["log_fixture"]).read_text(encoding="utf-8")
        row = {"case_id": case["case_id"], "expected_category": case["expected"]["category"]}
        for name, run in systems.items():
            pred = run(log, case["workflow"], case["job_name"], case.get("runner_label"))
            per_system[name].append((case["expected"], pred))
            row[name] = {
                "category": pred.category,
                "reason": pred.reason,
                "component": pred.component,
                "failing_test": pred.failing_test,
                "faulting_kernel": pred.faulting_kernel,
                "grounding": round(grounding(case["expected"], pred), 3),
            }
        report["cases"].append(row)

    for name, results in per_system.items():
        gold = [e["category"] for e, _ in results]
        pred = [p.category for _, p in results]
        report["systems"][name] = {
            "category_accuracy": _rate(list(zip(gold, pred, strict=True))),
            "category_macro_f1": macro_f1(gold, pred),
            "component_match": _rate(
                [(norm_component(e.get("component")), norm_component(p.component))
                 for e, p in results if e.get("component")]),
            "gpu_family_match": _rate(
                [(e.get("gpu_family"), p.gpu_family) for e, p in results if e.get("gpu_family")]),
            "platform_match": _rate(
                [(e.get("platform"), p.platform) for e, p in results if e.get("platform")]),
            "failing_test_match": _rate(
                [(e["failing_test"], p.failing_test) for e, p in results if e.get("failing_test")]),
            "faulting_kernel_match": _rate(
                [(e["failing_kernel"], p.faulting_kernel)
                 for e, p in results if e.get("failing_kernel")]),
            "grounding_rate": round(
                sum(grounding(e, p) for e, p in results) / len(results), 3) if results else None,
        }
    return report


def format_table(report: dict) -> str:
    names = list(report["systems"])
    metrics = list(next(iter(report["systems"].values())).keys()) if names else []
    lines = [
        f"cases: {report['n_cases']} (confirmed: {report['n_confirmed']})",
        f"{'metric':<24}" + "".join(f"{n:>8}" for n in names),
    ]
    for m in metrics:
        cells = []
        for n in names:
            v = report["systems"][n][m]
            cells.append(f"{'-' if v is None else f'{v:.2f}':>8}")
        lines.append(f"{m:<24}" + "".join(cells))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Score baselines on the golden set.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--confirmed-only", action="store_true")
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--split", choices=("all", "dev", "test"), default="all")
    args = parser.parse_args()

    reports = {}
    for split in (["dev", "test", "all"] if args.split == "all" else [args.split]):
        cases = load_cases(args.dataset, args.confirmed_only, split)
        if not cases:
            continue
        reports[split] = evaluate(cases)
        label = {"test": "TEST (held out: the honest number)",
                 "dev": "DEV (may be tuned on)", "all": "ALL"}[split]
        print(f"\n== {label} ==\n{format_table(reports[split])}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(reports, indent=2), encoding="utf-8")
        print(f"report written to {args.out}")


if __name__ == "__main__":
    main()
