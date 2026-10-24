"""Console API handlers as plain functions (framework-free).

The local dev server (server.py) calls these; in Azure the same functions sit behind
Static Web Apps managed Functions. Views read the job store (runs, jobs, deterministic
triage results) and the golden set / eval reports. The one network action is
`triage_now`: a user-triggered, single-job log fetch (read-only GitHub).

Flow: Home (what's failing, grouped by root cause; repo and workflow cards) -> workflow ->
run -> case (root cause first). Golden-set labelling is CLI-only (`evals.labeler`).
"""

from __future__ import annotations

import re
from collections import defaultdict

from rocksentinel.evals.labeler import Paths, _read_jsonl
from rocksentinel.evals.runner import evaluate

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SIG = re.compile(r"^[0-9a-f]{16}$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _days(since: str | None, until: str | None) -> tuple[str | None, str | None]:
    """Validate an inclusive UTC day range; a single day is since == until."""
    since, until = since or None, until or None
    for d in (since, until):
        if d is not None and not _DAY.match(d):
            raise ApiError(400, f"bad date {d!r}: use YYYY-MM-DD")
    if since and until and since > until:
        raise ApiError(400, "'since' is after 'until'")
    return since, until


def _job_url(repo: str, run_id: int, job_id: int) -> str:
    return f"https://github.com/{repo}/actions/runs/{run_id}/job/{job_id}"


# --- Quality: golden set and evals ---------------------------------------------------------

def _summary(case: dict) -> dict:
    e = case.get("expected", {})
    return {
        "case_id": case["case_id"],
        "status": case.get("label_status") or case.get("status"),
        "split": case.get("split"),
        "repo": case.get("repo", "ROCm/TheRock"),
        "run_id": case.get("run_id"),
        "job_id": case.get("job_id"),
        "workflow": case.get("workflow"),
        "job_name": case.get("job_name"),
        "job_url": case.get("job_url") or (
            _job_url(case.get("repo", "ROCm/TheRock"), case.get("run_id"), case["job_id"])
            if case.get("job_id") else None),
        "label_source": case.get("label_source"),
        "category": e.get("category"),
        "component": e.get("component"),
        "gpu_family": e.get("gpu_family"),
        "platform": e.get("platform"),
        "failing_test": e.get("failing_test"),
        "failing_kernel": e.get("failing_kernel"),
        "review_note": case.get("review_note"),
    }


def list_golden(paths: Paths, jobs=None) -> dict:
    cases = _read_jsonl(paths.dataset)
    items = [_summary(c) for c in cases]
    for item in items:  # link to the Case page when the job is in the store
        item["in_store"] = bool(jobs and item["job_id"] and jobs.get_job(item["job_id"]))
    return {
        "count": len(cases),
        "confirmed": sum(c.get("label_status") == "confirmed" for c in cases),
        "items": items,
    }


def evals(paths: Paths, confirmed_only: bool = False, split: str = "all") -> dict:
    cases = _read_jsonl(paths.dataset)
    if confirmed_only:
        cases = [c for c in cases if c.get("label_status") == "confirmed"]
    if split in ("dev", "test"):
        cases = [c for c in cases if c.get("split") == split]
    if not cases:
        return {"n_cases": 0, "n_confirmed": 0, "systems": {}, "cases": []}
    return evaluate(cases, root=paths.root)


# --- Home: repos, workflows, what's failing ------------------------------------------------

def _group_failures(rows: list[dict], first_seen: dict[str, str], new_from: str | None) -> list:
    """Failed jobs grouped by root-cause signature; NEW = first seen on/after `new_from`."""
    groups: dict[str, dict] = {}
    for r in rows:  # newest first
        key = r["signature"] or f"job:{r['job_id']}"
        g = groups.get(key)
        if g is None:
            fs = first_seen.get(r["signature"]) if r["signature"] else r["started_at"]
            g = groups[key] = {
                "signature": r["signature"], "headline": r["headline"],
                "root_kind": r["root_kind"], "root_line": r["root_line"],
                "category": r["category"], "latest_job_id": r["job_id"],
                "last_seen": r["started_at"], "first_seen": fs,
                "new": bool(new_from and fs and fs[:10] >= new_from),
                "repos": set(), "workflows": set(), "components": set(), "families": set(),
                "runs": set(), "job_ids": [],
            }
        g["repos"].add(r["repo"])
        g["workflows"].add(r["workflow"])
        if r["component"]:
            g["components"].add(r["component"])
        if r["gpu_family"]:
            g["families"].add(r["gpu_family"])
        g["runs"].add(r["run_id"])
        g["job_ids"].append(r["job_id"])
    out = []
    for g in groups.values():
        g["jobs"] = len(g["job_ids"])
        g["runs"] = len(g["runs"])
        g["job_ids"] = g["job_ids"][:50]
        for k in ("repos", "workflows", "components", "families"):
            g[k] = sorted(g[k])
        out.append(g)
    # new first, then most jobs; ties keep newest-first order (the rows are newest first)
    return sorted(out, key=lambda g: (not g["new"], -g["jobs"]))


def overview(jobs, since: str | None = None, until: str | None = None,
             repo: str | None = None) -> dict:
    since, until = _days(since, until)
    workflows = jobs.workflow_summaries(since=since, until=until, repo=repo)
    failed_jobs, triaged = jobs.failed_job_count(since=since, until=until, repo=repo)
    first, last = jobs.run_date_range()
    groups = _group_failures(jobs.triaged_failures(since=since, until=until, repo=repo),
                             jobs.signature_first_seen(), since or last)
    return {
        "since": since, "until": until, "repo": repo,
        "data_range": {"first": first, "last": last},
        "summary": {
            "repos": len({w["repo"] for w in workflows}),
            "workflows": len(workflows),
            "runs": sum(w["runs"] for w in workflows),
            "failed_runs": sum(w["failed_runs"] for w in workflows),
            "failed_jobs": failed_jobs,
            "triaged": triaged,
            "root_causes": len(groups),
            "new_root_causes": sum(g["new"] for g in groups),
        },
        "failing": groups,
    }


def list_repos(jobs, since: str | None = None, until: str | None = None, config=None) -> dict:
    """One card per repo (configured repos without data yet are listed too)."""
    from rocksentinel.config import load_config

    since, until = _days(since, until)
    config = config or load_config()
    per: dict[str, dict] = {}
    for w in jobs.workflow_summaries(since=since, until=until):
        r = per.setdefault(w["repo"], {"repo": w["repo"], "runs": 0, "failed_runs": 0,
                                       "jobs": 0, "failed_jobs": 0, "workflows": 0,
                                       "last_run_at": None, "recent": []})
        r["workflows"] += 1
        for k in ("runs", "failed_runs", "jobs", "failed_jobs"):
            r[k] += w[k]
        r["last_run_at"] = max(filter(None, (r["last_run_at"], w["last_run_at"])), default=None)
        r["recent"] += w["recent"]
    for cfg in config.enabled():
        per.setdefault(cfg.repo, {"repo": cfg.repo, "runs": 0, "failed_runs": 0, "jobs": 0,
                                  "failed_jobs": 0, "workflows": 0, "last_run_at": None,
                                  "recent": []})
    items = []
    for r in per.values():
        cfg = config.repo(r["repo"])
        r["tenant"] = cfg.tenant if cfg else r["repo"].split("/")[0]
        r["packs"] = list(cfg.packs) + ["core"] if cfg else ["rocm", "core"]
        r["configured"] = cfg is not None
        r["recent"] = sorted(r["recent"], key=lambda x: x["started_at"] or "", reverse=True)[:20]
        r["failure_rate"] = round(r["failed_runs"] / r["runs"], 3) if r["runs"] else 0.0
        items.append(r)
    items.sort(key=lambda r: (-r["failed_runs"], r["repo"]))
    return {"count": len(items), "items": items}


def list_runs(jobs, limit: int = 300, workflow: str | None = None,
              since: str | None = None, until: str | None = None,
              repo: str | None = None) -> dict:
    since, until = _days(since, until)
    runs = jobs.recent_runs(limit=max(1, min(limit, 500)), workflow=workflow,
                            since=since, until=until, repo=repo)
    return {"count": len(runs), "repo": repo, "workflow": workflow, "since": since,
            "until": until, "items": runs}


def list_workflows(jobs, since: str | None = None, until: str | None = None,
                   repo: str | None = None) -> dict:
    since, until = _days(since, until)
    items = jobs.workflow_summaries(since=since, until=until, repo=repo)
    first, last = jobs.run_date_range()
    return {"count": len(items), "since": since, "until": until, "repo": repo,
            "data_range": {"first": first, "last": last}, "items": items}


# --- Run and Case ------------------------------------------------------------------------

def _triage_brief(t: dict | None) -> dict | None:
    if t is None:
        return None
    return {k: t[k] for k in ("headline", "category", "reason", "root_kind", "signature")}


def run_detail(jobs, run_id: int) -> dict:
    all_jobs = jobs.jobs_for_run(run_id)
    if not all_jobs:
        raise ApiError(404, f"run {run_id} not in the job store")
    failed = [j for j in all_jobs if j.failed]
    groups: dict = defaultdict(list)
    for j in failed:
        groups[j.runner_label or "?"].append({
            "job_id": j.job_id, "job_name": j.job_name, "component": j.component,
            "gpu_family": j.gpu_family, "platform": j.platform, "conclusion": j.conclusion,
            "attempt": j.attempt, "triage": _triage_brief(jobs.get_triage(j.job_id))})
    by_pool = sorted(({"runner_label": k, "failed": v,
                       "components": sorted({x["component"] for x in v if x["component"]})}
                      for k, v in groups.items()), key=lambda g: -len(g["failed"]))
    first = all_jobs[0]
    return {"run_id": run_id, "repo": first.repo, "workflow": first.workflow,
            "head_sha": first.head_sha, "branch": first.branch, "event": first.event,
            "started_at": min((j.started_at for j in all_jobs if j.started_at), default=None),
            "jobs": len(all_jobs), "failed": len(failed),
            "attempts": max(j.attempt for j in all_jobs),
            "by_runner_pool": by_pool,
            "platform_event_hint": any(len(g["components"]) >= 3 for g in by_pool),
            "run_url": f"https://github.com/{first.repo}/actions/runs/{run_id}"}


def case_detail(jobs, job_id: int) -> dict:
    """One job: root cause first (from stored triage), then history and context."""
    from rocksentinel.ci_evidence.history import job_history

    job = jobs.get_job(job_id)
    if job is None:
        raise ApiError(404, f"job {job_id} not in the job store")
    triage = jobs.get_triage(job_id)
    same = []
    if triage and triage["signature"]:
        same = [{"job_id": r["job_id"], "repo": r["repo"], "workflow": r["workflow"],
                 "run_id": r["run_id"], "gpu_family": r["gpu_family"],
                 "platform": r["platform"], "started_at": r["started_at"]}
                for r in jobs.triaged_failures() if r["signature"] == triage["signature"]
                and r["job_id"] != job_id][:20]
    return {
        "job": {"job_id": job.job_id, "run_id": job.run_id, "repo": job.repo,
                "workflow": job.workflow, "job_name": job.job_name,
                "component": job.component, "gpu_family": job.gpu_family,
                "platform": job.platform, "shard": job.shard, "runner_label": job.runner_label,
                "conclusion": job.conclusion, "attempt": job.attempt,
                "started_at": job.started_at, "finished_at": job.finished_at,
                "head_sha": job.head_sha, "branch": job.branch},
        "failed": job.failed,
        "triage": triage,
        "can_fetch": job.failed and triage is None,
        "same_signature": same,
        "history": job_history(jobs, job_id),
        "job_url": _job_url(job.repo, job.run_id, job_id),
        "run_url": f"https://github.com/{job.repo}/actions/runs/{job.run_id}",
    }


def triage_now(jobs, job_id: int) -> dict:
    """User-triggered: download ONE job's log (read-only) and triage it."""
    from rocksentinel.ci_evidence import server as evidence
    from rocksentinel.triage.worker import triage_one_live

    try:
        triage_one_live(jobs, evidence.get_github(), job_id)
    except KeyError as exc:
        raise ApiError(404, str(exc)) from exc
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    except Exception as exc:  # e.g. log expired (404) or no token
        raise ApiError(502, f"could not fetch the log: {exc}") from exc
    return case_detail(jobs, job_id)


# --- Signatures --------------------------------------------------------------------------

def list_signatures(jobs, since: str | None = None, until: str | None = None,
                    repo: str | None = None) -> dict:
    since, until = _days(since, until)
    first, last = jobs.run_date_range()
    groups = [g for g in _group_failures(jobs.triaged_failures(since=since, until=until,
                                                               repo=repo),
                                         jobs.signature_first_seen(), since or last)
              if g["signature"]]
    return {"count": len(groups), "items": groups}


def signature_detail(jobs, signature: str) -> dict:
    if not _SIG.match(signature):
        raise ApiError(400, "bad signature")
    rows = [r for r in jobs.triaged_failures() if r["signature"] == signature]
    if not rows:
        raise ApiError(404, f"signature {signature} not seen")
    group = _group_failures(rows, jobs.signature_first_seen(), None)[0]
    group["items"] = rows
    return group
