"""Coverage describes observations, never inferred account existence."""

from echo.domain import TRANSIENT, Status
from echo.i18n import tr


def search_coverage(job, cards):
    observations = [o for c in cards for o in c["observations"]]
    result = {
        "checks": len(observations),
        "cards": len(cards),
        "matches": sum(o["status"] == Status.POSSIBLE_MATCH for o in observations),
        "not_found": sum(o["status"] == Status.NOT_FOUND for o in observations),
        "blocked": sum(o["status"] in {Status.BLOCKED, Status.RATE_LIMITED} for o in observations),
        "timeouts": sum(o["status"] == Status.TIMEOUT for o in observations),
        "retryable": sum(o["status"] in TRANSIENT for o in observations),
        "personal_false_positives": sum(c.get("mark") == "false_positive" for c in cards),
        "not_checked_known": 0,
        "coverage_known": True,
        "engines": {},
    }
    result["other_errors"] = result["checks"] - sum(
        result[k] for k in ("matches", "not_found", "blocked", "timeouts")
    )
    for engine in job.engines:
        actual = sum(o["engine"] == engine for o in observations)
        total = job.progress.get(engine, {}).get("total")
        if total is None and job.targets and engine in job.targets:
            total = len(job.targets[engine])
        known = isinstance(total, int) and not isinstance(total, bool) and total >= actual
        result["engines"][engine] = {"completed": actual, "selected": total if known else None}
        result["coverage_known"] &= known
        if known:
            result["not_checked_known"] += total - actual
    return result


def coverage_text(data, lang):
    text = tr(lang, "coverage_summary", **data)
    if not data["coverage_known"]:
        text += "\n" + tr(lang, "coverage_unknown")
    if data["blocked"] or data["timeouts"] or data["other_errors"] or data["not_checked_known"]:
        text += "\n" + tr(lang, "coverage_incomplete")
    if data["personal_false_positives"]:
        text += "\n" + tr(lang, "coverage_personal", count=data["personal_false_positives"])
    return text
