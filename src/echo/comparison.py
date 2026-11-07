from echo.domain import Status, now


def compare_maps(before, after):
    common = before.keys() & after.keys()
    return {
        "new": sorted(after.keys() - before.keys()),
        "not_checked_in_second": sorted(before.keys() - after.keys()),
        "changed": [
            {"key": key, "before": before[key], "after": after[key]}
            for key in sorted(common)
            if before[key] != after[key]
        ],
        "unchanged": sorted(key for key in common if before[key] == after[key]),
    }


async def compare(jobs, owner, first_id, second_id):
    first, second = await jobs.owned(owner, first_id), await jobs.owned(owner, second_id)
    if first.state not in {"COMPLETED", "PARTIAL"} or second.state not in {"COMPLETED", "PARTIAL"}:
        raise ValueError("not_comparable")
    if first_id == second_id or first.kind != second.kind or first.module_id != second.module_id:
        raise ValueError("not_comparable")
    warnings = []
    if first.kind == "search":
        if first.username != second.username:
            raise ValueError("not_comparable")
        before_cards, after_cards = await jobs.cards(owner, first.id), await jobs.cards(owner, second.id)

        def observations(cards):
            return {
                o["engine"] + ":" + o["site_key"] + ":" + c["url"]: {
                    k: o.get(k)
                    for k in ("status", "raw_status", "http_code", "final_url", "check_method", "version")
                }
                for c in cards
                for o in c["observations"]
            }

        before, after = observations(before_cards), observations(after_cards)
        if (
            first.coverage != second.coverage
            or first.engines != second.engines
            or before.keys() != after.keys()
        ):
            warnings.append("different_coverage")
        result = compare_maps(before, after)
        result["no_longer_detected"] = [
            item["key"]
            for item in result["changed"]
            if item["before"]["status"] == Status.POSSIBLE_MATCH
            and item["after"]["status"] == Status.NOT_FOUND
        ]
        result["new_possible_matches"] = [
            item["key"]
            for item in result["changed"]
            if item["before"]["status"] != Status.POSSIBLE_MATCH
            and item["after"]["status"] == Status.POSSIBLE_MATCH
        ]
        if {o["version"] for o in before.values()} != {o["version"] for o in after.values()}:
            warnings.append("different_versions")
    elif first.kind == "module" and first.module_id in {"passport", "page", "domains"}:
        if (
            first.parameters.get("target") != second.parameters.get("target")
            or not first.result
            or not second.result
        ):
            raise ValueError("not_comparable")

        def facts(result):
            return {
                k: v for k, v in result["data"].items() if k not in {"checked_at", "fetched_at", "warnings"}
            }

        before, after = facts(first.result), facts(second.result)
        if first.parameters != second.parameters:
            warnings.append("different_parameters")
        if first.result.get("versions") != second.result.get("versions"):
            warnings.append("different_versions")
        result = compare_maps(before, after)
    else:
        raise ValueError("not_comparable")
    # Never describe TIMEOUT/BLOCKED or coverage gaps as deletion.
    return {
        "schema_version": "echo.comparison/1",
        "first": first.id,
        "second": second.id,
        "dates": [first.created_at.isoformat(), second.created_at.isoformat()],
        "checked_at": now().isoformat(),
        "parameters": [first.parameters, second.parameters],
        "coverage": [first.coverage, second.coverage],
        "warnings": warnings + ["comparison_not_account_deletion"],
        "difference": result,
    }
