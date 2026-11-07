"""Explicit user journeys over existing jobs, modules and owned workspaces."""

import hashlib
import json
import secrets
from html import escape
from math import ceil
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from echo.coverage import coverage_text, search_coverage
from echo.domain import now
from echo.i18n import tr
from echo.modules import ModuleError, validate_input
from echo.presentation import button, keyboard, setup
from echo.services import Denied, FolderLimited, as_utc
from echo.sources import public_target

REASONS = {"g": "generic_page", "n": "different_username", "u": "unavailable_profile", "o": "other"}


class Journeys:
    def __init__(self, extras):
        self.extras, self.ui = extras, extras.ui
        self.jobs, self.config = extras.jobs, extras.config

    @staticmethod
    def search_signature(draft):
        return hashlib.sha256(
            json.dumps([draft["username"], sorted(draft["engines"]), draft["coverage"], draft["ai"]]).encode()
        ).hexdigest()

    async def offer_search(self, event, u, draft):
        if draft.get("recent_bypass") == self.search_signature(draft):
            return False
        recent = await self.jobs.recent(
            u.id,
            kind="search",
            nick=draft["username"],
            engines=draft["engines"],
            coverage=draft["coverage"],
            ai=draft["ai"],
        )
        return await self._offer(event, u, recent, {"kind": "search", "draft": draft}, draft["username"])

    async def offer_module(self, event, u, module, parameters):
        if module not in {"passport", "page", "domains"}:
            return False
        normalized = validate_input(module, {**parameters, "authorized": True}, self.config)
        recent = await self.jobs.recent(u.id, kind="module", module=module, parameters=normalized)
        return await self._offer(
            event,
            u,
            recent,
            {"kind": "module", "module": module, "parameters": parameters},
            normalized["target"],
        )

    async def _offer(self, event, u, recent, payload, label):
        if recent is None:
            return False
        nonce = secrets.token_hex(6)
        await self.ui.put(u.id, "recent", {**payload, "recent_job": recent.id, "nonce": nonce}, 600)
        await self.ui.redis.delete(self.ui.key(u.id, "input"))
        age = max(0, int((now() - as_utc(recent.finished_at)).total_seconds() // 60))
        await self.ui.send_view(
            event,
            tr(u.language, "recent_offer", target=escape(label[:250]), minutes=age, state=recent.state),
            keyboard(
                [button(tr(u.language, "recent_open"), "jr:open:" + nonce)],
                [button(tr(u.language, "recent_again"), "jr:again:" + nonce)],
                [button(tr(u.language, "back"), "home:menu")],
            ),
        )
        return True

    async def goal(self, event, u, choice):
        if choice not in {"nick", "site", "file"}:
            raise Denied()
        await self.ui.put(u.id, "input", {"type": "goal_" + choice}, 600)
        await self.ui.send_view(
            event,
            tr(u.language, "goal_prompt_" + choice),
            keyboard([button(tr(u.language, "back"), "home:menu")]),
        )

    async def on_text(self, message, u):
        flow = await self.ui.get(u.id, "input")
        if flow and flow.get("type") == "batch_folder":
            pending = await self.ui.get(u.id, "batch_pending")
            if not pending or pending["nonce"] != flow["nonce"]:
                raise Denied()
            folder = await self.extras.folders.create(u.id, message.text)
            await self.save_batch(message, u, folder.id, flow["nonce"])
            await self.ui.redis.delete(self.ui.key(u.id, "input"))
            return True
        if not flow or flow.get("type") not in {"goal_nick", "goal_site", "goal_file"}:
            return False
        if flow["type"] == "goal_nick":
            await self.ui.new_search(message, u, message.text)
        elif flow["type"] == "goal_file":
            await message.answer(tr(u.language, "goal_prompt_file"))
        else:
            target = public_target(message.text)
            nonce = secrets.token_hex(6)
            await self.ui.put(u.id, "site_choice", {"target": target, "nonce": nonce}, 600)
            await self.ui.redis.delete(self.ui.key(u.id, "input"))
            await self.ui.send_view(
                message,
                tr(u.language, "goal_site_actions", target=escape(target[:500])),
                keyboard(
                    *[
                        [button(tr(u.language, "tool_" + module), f"jg:tool:{module}:{nonce}")]
                        for module in ("passport", "page", "domains")
                    ],
                    [button(tr(u.language, "back"), "home:menu")],
                ),
            )
        return True

    def next_rows(self, job, lang):
        if job.module_id == "passport" and job.result:
            return [[button(tr(lang, "next_page"), "jn:" + job.id + ":p")]]
        return []

    def qr_rows(self, job, code, index, lang):
        try:
            public_target(code.get("url") or "")
        except ValueError:
            return []
        return [[button(tr(lang, "next_passport"), f"jn:{job.id}:q:{index}")]]

    async def reason_picker(self, event, u, job_id, prefix):
        await self.ui.find_card(u, job_id, prefix)
        await self.ui.send_view(
            event,
            tr(u.language, "review_choose"),
            keyboard(
                *[
                    [button(tr(u.language, "reason_" + reason), f"jf:{job_id}:{prefix}:{code}")]
                    for code, reason in REASONS.items()
                ],
                [button(tr(u.language, "back"), f"c:{job_id}:{prefix}")],
            ),
        )

    async def batch_view(self, event, u, job_id, page=0, *, start=False):
        job = await self.jobs.owned(u.id, job_id)
        if job.kind != "search":
            raise Denied()
        cards = await self.jobs.cards(u.id, job_id, u.language)
        state = await self.ui.get(u.id, "batch:" + job_id)
        if start or not state:
            state = {"keys": []}
            await self.ui.put(u.id, "batch:" + job_id, state, 1800)
        pages = max(1, ceil(len(cards) / 5))
        page = max(0, min(page, pages - 1))
        rows = [
            [
                button(
                    ("✓ " if c["key"] in state["keys"] else "☐ ") + c["name"][:40],
                    f"jb:t:{job_id}:{c['key'][:12]}:{page}",
                )
            ]
            for c in cards[page * 5 : (page + 1) * 5]
        ]
        rows += [
            [
                button("←", f"jb:v:{job_id}:{max(0, page - 1)}"),
                button("→", f"jb:v:{job_id}:{min(pages - 1, page + 1)}"),
            ],
            [button(tr(u.language, "batch_save"), "jb:pick:" + job_id)],
            [button(tr(u.language, "back"), f"r:{job_id}:a:0")],
        ]
        await self.ui.send_view(
            event,
            tr(
                u.language,
                "batch_select",
                count=len(state["keys"]),
                limit=self.config.batch_max_cards,
                page=page + 1,
                pages=pages,
            ),
            keyboard(*rows),
        )

    async def save_batch(self, event, u, folder_id, nonce):
        state = await self.ui.get(u.id, "batch_pending")
        if not state or state["nonce"] != nonce:
            raise Denied()
        key = hashlib.sha256(json.dumps([state, folder_id], sort_keys=True).encode()).hexdigest()
        items = await self.extras.folders.add_cards(
            u.id, folder_id, state["job"], state["keys"], u.language, key
        )
        folder = await self.extras.folders.owned(u.id, folder_id)
        until = (
            as_utc(folder.expires_at).astimezone(ZoneInfo(self.config.timezone)).isoformat(timespec="minutes")
        )
        names = "\n".join("• " + escape(i.title[:60]) for i in items[:10])
        if len(items) > 10:
            names += "\n" + tr(u.language, "batch_more", count=len(items) - 10)
        await self.ui.send_view(
            event,
            tr(
                u.language,
                "batch_saved",
                count=len(items),
                folder=escape(folder.name),
                until=until,
                materials=names,
            ),
            keyboard(
                [button(tr(u.language, "folders"), "wf:view:" + folder.id)],
                [button(tr(u.language, "back"), f"r:{state['job']}:a:0")],
            ),
        )

    async def callback(self, cb, u, parts):
        action, lang = parts[0], u.language
        if action == "jg":
            if parts[1] != "tool":
                await self.goal(cb, u, parts[1])
            else:
                module = parts[2]
                state = await self.ui.get(u.id, "site_choice")
                if not state or state["nonce"] != parts[3] or module not in {"passport", "page", "domains"}:
                    raise Denied()
                parameters = {"target": public_target(state["target"])}
                if module == "domains":
                    url = urlsplit(parameters["target"])
                    parameters.update(
                        target=urlunsplit((url.scheme, url.netloc, "/", "", "")),
                        variants=self.config.domain_variants,
                        dns=True,
                    )
                if not (await self.extras.tasks.availability()).get(module):
                    raise ModuleError("module_unavailable")
                await self.extras.confirm_tool(cb, u, module, parameters)
            return True
        if action == "jr":
            state = await self.ui.get(u.id, "recent")
            if not state or state["nonce"] != parts[2]:
                raise Denied()
            if parts[1] == "open":
                await self.ui.show_results(cb, u, state["recent_job"])
            elif parts[1] == "again":
                await self.ui.redis.delete(self.ui.key(u.id, "recent"))
                if state["kind"] == "search":
                    draft = {**state["draft"], "id": secrets.token_hex(16)}
                    draft["recent_bypass"] = self.search_signature(draft)
                    await self.ui.put(u.id, "draft", draft)
                    await self.ui.send_view(cb, *setup(draft, lang))
                else:
                    await self.extras.confirm_tool(
                        cb, u, state["module"], state["parameters"], check_recent=False
                    )
            else:
                raise Denied()
            return True
        if action == "jn":
            job = await self.jobs.owned(u.id, parts[1])
            if job.kind != "module" or not job.result:
                raise Denied()
            data = job.result["data"]
            if parts[2] == "p" and job.module_id == "passport":
                module, target = "page", data["final_url"]
            elif parts[2] == "q" and job.module_id == "qr":
                index = int(parts[3])
                if not 0 <= index < len(data["codes"]):
                    raise Denied()
                module, target = "passport", data["codes"][index].get("url")
                if not target:
                    raise Denied()
            else:
                raise Denied()
            if not (await self.extras.tasks.availability()).get(module):
                raise ModuleError("module_unavailable")
            await self.extras.confirm_tool(cb, u, module, {"target": public_target(target)})
            return True
        if action == "jf":
            card = await self.ui.find_card(u, parts[1], parts[2])
            if parts[3] not in REASONS:
                raise Denied()
            await self.jobs.annotate(
                u.id, parts[1], card["key"], mark="false_positive", reason=REASONS[parts[3]]
            )
            await self.ui.show_card(cb, u, parts[1], parts[2])
            return True
        if action == "jc":
            job = await self.jobs.owned(u.id, parts[1])
            if job.kind != "search":
                raise Denied()
            info = search_coverage(job, await self.jobs.cards(u.id, job.id, lang))
            text = (
                coverage_text(info, lang)
                + "\n\n"
                + "\n".join(
                    tr(
                        lang,
                        "coverage_engine",
                        engine=engine,
                        completed=c["completed"],
                        selected=c["selected"] if c["selected"] is not None else tr(lang, "unknown_count"),
                    )
                    for engine, c in info["engines"].items()
                )
            )
            await self.ui.send_view(
                cb,
                text,
                keyboard(
                    *([[button(tr(lang, "retry_failed"), "retry:" + job.id)]] if info["retryable"] else []),
                    [button(tr(lang, "back"), f"r:{job.id}:a:0")],
                ),
            )
            return True
        if action != "jb":
            return False
        choice = parts[1]
        if choice in {"start", "v", "t", "pick"}:
            job_id = parts[2]
            job = await self.jobs.owned(u.id, job_id)
            if job.kind != "search":
                raise Denied()
            if choice in {"start", "v"}:
                await self.batch_view(
                    cb, u, job_id, int(parts[3]) if len(parts) > 3 else 0, start=choice == "start"
                )
            elif choice == "t":
                card = await self.ui.find_card(u, job_id, parts[3])

                def toggle(state):
                    if state is None:
                        raise Denied()
                    keys = list(state["keys"])
                    if card["key"] in keys:
                        keys.remove(card["key"])
                    elif len(keys) < self.config.batch_max_cards:
                        keys.append(card["key"])
                    else:
                        raise FolderLimited()
                    return {"keys": keys}

                await self.ui.mutate(u.id, "batch:" + job_id, toggle, 1800)
                await self.batch_view(cb, u, job_id, int(parts[4]))
            else:
                state = await self.ui.get(u.id, "batch:" + job_id)
                if not state or not state["keys"]:
                    await self.ui.send_view(
                        cb,
                        tr(lang, "batch_empty"),
                        keyboard([button(tr(lang, "batch_select_button"), "jb:v:" + job_id + ":0")]),
                    )
                    return True
                nonce = secrets.token_hex(6)
                await self.ui.put(
                    u.id, "batch_pending", {"job": job_id, "keys": state["keys"], "nonce": nonce}, 600
                )
                folders = await self.extras.folders.list(u.id)
                await self.ui.send_view(
                    cb,
                    tr(lang, "folder_choose"),
                    keyboard(
                        *[[button(f.name, f"jb:save:{f.id}:{nonce}")] for f in folders],
                        [button(tr(lang, "folder_new"), "jb:new:" + nonce)],
                        [button(tr(lang, "back"), "jb:v:" + job_id + ":0")],
                    ),
                )
        elif choice == "save":
            await self.save_batch(cb, u, parts[2], parts[3])
        elif choice == "new":
            state = await self.ui.get(u.id, "batch_pending")
            if not state or state["nonce"] != parts[2]:
                raise Denied()
            await self.ui.put(u.id, "input", {"type": "batch_folder", "nonce": parts[2]}, 600)
            await self.ui.send_view(cb, tr(lang, "folder_name_prompt", days=self.config.folder_ttl_days))
        else:
            raise Denied()
        return True
