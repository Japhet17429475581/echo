from html import escape
from math import ceil

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from echo.coverage import coverage_text, search_coverage
from echo.domain import ERRORS, TRANSIENT, Category, Status, disagreement, now
from echo.i18n import tr
from echo.services import as_utc

CATEGORIES = list(Category)


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def keyboard(*rows):
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def setup(draft, lang):
    engines = ", ".join(draft["engines"])
    text = tr(
        lang,
        "setup",
        nick=escape(draft["username"]),
        engines=engines,
        coverage=tr(lang, draft["coverage"]),
        ai=tr(lang, "on" if draft["ai"] else "off"),
    )
    rows = [
        [
            button("Sherlock", "cfg:e:sherlock"),
            button("Maigret", "cfg:e:maigret"),
            button(tr(lang, "both"), "cfg:e:both"),
        ],
        [button(tr(lang, c), "cfg:c:" + c) for c in ("curated", "top100", "extended")],
        [button(tr(lang, "ai_toggle", value=tr(lang, "on" if draft["ai"] else "off")), "cfg:a:toggle")],
        [button(tr(lang, "start_search"), "cfg:run")],
    ]
    return text, keyboard(*rows)


def progress(job, config, lang):
    start = as_utc(job.started_at or job.created_at)
    finish = as_utc(job.finished_at) if job.finished_at else now()
    if getattr(job, "kind", "search") != "search":
        module = job.progress.get("module", {})
        name = (
            tr(lang, "tool_" + job.module_id)
            if job.module_id
            else tr(
                lang,
                "comparison"
                if job.kind == "compare"
                else "folder_export"
                if job.kind == "folder_export"
                else "export_job",
            )
        )
        counters = f"{module['completed']}/{module.get('total', '?')}" if "completed" in module else ""
        return tr(
            lang,
            "tool_progress",
            name=name,
            state=job.state,
            phase=tr(lang, module.get("phase", "waiting")),
            seconds=max(0, int((finish - start).total_seconds())),
            counts=counters,
        ), keyboard([button(tr(lang, "show_ready"), "xr:" + job.id), button(tr(lang, "stop"), "s:" + job.id)])
    rows = [
        tr(
            lang,
            "engine_count",
            engine=e,
            completed=job.progress.get(e, {}).get("completed", 0),
            total=job.progress.get(e, {}).get("total", tr(lang, "unknown_count")),
        )
        for e in job.engines
    ]
    text = tr(
        lang,
        "progress",
        brand=escape(config.brand),
        state=job.state,
        nick=escape(job.username),
        seconds=max(0, int((finish - start).total_seconds())),
        engines="\n".join(rows),
        matches=sum(p.get("matches", 0) for p in job.progress.values()),
        errors=sum(p.get("errors", 0) for p in job.progress.values()),
        enrichment=job.enrichment,
    )
    if job.failures:
        text += "\n" + escape(str(job.failures))
    return text, keyboard(
        [button(tr(lang, "show_ready"), f"r:{job.id}:a:0"), button(tr(lang, "stop"), f"s:{job.id}")]
    )


def matches(card):
    return any(o["status"] == Status.POSSIBLE_MATCH for o in card["observations"])


def filtered(cards, mode):
    if mode == "m":
        return [c for c in cards if matches(c)]
    if mode == "e":
        return [c for c in cards if any(o["status"] in ERRORS for o in c["observations"])]
    if mode == "d":
        return [c for c in cards if disagreement(c["observations"])]
    if mode in {"u", "f"}:
        return [c for c in cards if c["mark"] == ("useful" if mode == "u" else "false_positive")]
    if mode.startswith("c"):
        try:
            category = CATEGORIES[int(mode[1:])]
        except (ValueError, IndexError):
            return cards
        return [c for c in cards if (c["site"] or {}).get("category", "other") == category]
    return cards


def results(job, cards, mode, page, lang):
    selected = filtered(cards, mode)
    pages = max(1, ceil(len(selected) / 5))
    page = max(0, min(page, pages - 1))
    batch = selected[page * 5 : (page + 1) * 5]
    text = tr(
        lang,
        "results",
        state=job.state,
        total=len(cards),
        filtered=len(selected),
        matches=sum(matches(c) for c in cards),
        disagreements=sum(disagreement(c["observations"]) for c in cards),
        page=page + 1,
        pages=pages,
        rows="\n".join(
            f"{page * 5 + i + 1}. {escape(c['name'])} · " + ", ".join(o["status"] for o in c["observations"])
            for i, c in enumerate(batch)
        ),
    )
    j = job.id
    text += "\n\n" + coverage_text(search_coverage(job, cards), lang)
    rows = [[button(c["name"][:45], f"c:{j}:{c['key'][:12]}")] for c in batch]
    rows += [
        [
            button(tr(lang, key), f"r:{j}:{code}:0")
            for key, code in [("all", "a"), ("matches", "m"), ("errors", "e")]
        ],
        [
            button(tr(lang, key), f"r:{j}:{code}:0")
            for key, code in [("disagreements", "d"), ("useful", "u"), ("false_positive", "f")]
        ],
        [button(tr(lang, "category"), f"categories:{j}")],
        [
            button("←", f"r:{j}:{mode}:{max(0, page - 1)}"),
            button(tr(lang, "refresh"), f"r:{j}:{mode}:{page}"),
            button("→", f"r:{j}:{mode}:{min(pages - 1, page + 1)}"),
        ],
        [button(tr(lang, "export"), f"formats:{j}:0"), button(tr(lang, "encrypted"), f"formats:{j}:1")],
        *(
            [[button(tr(lang, "retry_failed"), f"retry:{j}")]]
            if any(o["status"] in TRANSIENT for c in cards for o in c["observations"])
            else []
        ),
        [button(tr(lang, "coverage_details"), f"jc:{j}")],
        [button(tr(lang, "batch_select_button"), f"jb:start:{j}")],
        [button(tr(lang, "save_folder"), f"fs:{j}"), button(tr(lang, "compare"), "xc:list")],
        [button(tr(lang, "package"), f"xz:{j}"), button(tr(lang, "package_age"), f"xza:{j}")],
        [button(tr(lang, "stop"), f"s:{j}"), button(tr(lang, "delete_job"), f"dj:{j}")],
    ]
    return text, keyboard(*rows)


def card_view(card, job_id, lang):
    site = card["site"] or {}
    observations = "\n".join(
        f"{o['engine']} {o['version']} · {o['status']} ({escape(o['raw_status'])})\n"
        f"{o['checked_at']} · HTTP {o.get('http_code') or '—'}"
        for o in card["observations"][:2]
    )
    found = [o["engine"] for o in card["observations"] if o["status"] == Status.POSSIBLE_MATCH]
    agreement = (
        tr(lang, "engines_differ")
        if disagreement(card["observations"])
        else tr(lang, "found_both")
        if len(set(found)) > 1
        else tr(lang, "found_engine", engine=found[0])
        if found
        else ""
    )
    text = tr(
        lang,
        "card",
        name=escape(card["name"]),
        nick=escape(card["username"]),
        description=escape(site.get("description") or tr(lang, "description_unavailable")),
        origin=tr(lang, site.get("origin", "unavailable")),
        category=tr(lang, site.get("category", "other")),
        activities="\n".join("• " + escape(a) for a in site.get("activities", [])),
        observations=observations,
        agreement=agreement,
        mark=tr(lang, card["mark"]) if card["mark"] else "—",
        note=escape(card["note"][:1000]) or "—",
        sources=escape(
            "\n".join(s[:160] + ("…" if len(s) > 160 else "") for s in site.get("sources", [])[:3])
        )
        or "—",
    )
    suffix = f"{job_id}:{card['key'][:12]}"
    if card["mark"] == "false_positive":
        text += "\n\n" + tr(
            lang,
            "review_summary",
            reason=tr(lang, "reason_" + (card.get("false_positive_reason") or "unspecified")),
        )
    links = []
    if card["url"]:
        links.append(InlineKeyboardButton(text=tr(lang, "open"), url=card["url"]))
        if len(card["url"]) <= 256:
            links.append(
                InlineKeyboardButton(text=tr(lang, "copy"), copy_text=CopyTextButton(text=card["url"]))
            )
        else:
            links.append(button(tr(lang, "copy"), f"url:{suffix}"))
    return text, keyboard(
        links,
        [button(tr(lang, "why"), f"why:{suffix}")],
        [button(tr(lang, "useful"), f"m:{suffix}:u"), button(tr(lang, "false_positive"), f"m:{suffix}:f")],
        [button(tr(lang, "note"), f"n:{suffix}"), button(tr(lang, "clear_mark"), f"m:{suffix}:c")],
        [button(tr(lang, "save_folder"), f"fs:{suffix}")],
        [button(tr(lang, "back"), f"r:{job_id}:a:0")],
    )


def explain(card, lang):
    explanations = []
    if disagreement(card["observations"]):
        explanations.append(tr(lang, "engines_differ"))
    unusual = False
    for o in card["observations"]:
        status, code = o["status"], o.get("http_code")
        if code == 403 or status == Status.BLOCKED:
            key = "explain_403"
        elif code == 429 or status == Status.RATE_LIMITED:
            key = "explain_429"
        elif status == Status.TIMEOUT:
            key = "explain_timeout"
        elif code and code >= 500 or status == Status.SITE_ERROR:
            key = "explain_5xx"
        elif o.get("redirect_kind") in {"home", "login", "search"}:
            key = "explain_redirect"
        elif status == Status.NETWORK_ERROR:
            key = "explain_network"
        elif status == Status.POSSIBLE_MATCH:
            key = "explain_match"
        elif status == Status.NOT_FOUND:
            key = "explain_not_found"
        else:
            key = "explain_unknown"
            unusual = bool(o.get("check_method") or o.get("error_type"))
        explanations.append(
            o["engine"]
            + ": "
            + tr(lang, key)
            + "\n"
            + (escape(o.get("evidence")) if o.get("evidence") else tr(lang, "no_evidence"))
        )
    return "\n\n".join(explanations), unusual
