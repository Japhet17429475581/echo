import asyncio
import hashlib
import json
import secrets
from html import escape

from aiogram.types import CallbackQuery, CopyTextButton, FSInputFile, InlineKeyboardButton
from sqlalchemy import func, select

from echo.domain import JobState
from echo.engines import DomainLimiter
from echo.export import fingerprint
from echo.i18n import tr
from echo.modules import REGISTRY, ModuleError
from echo.paths import ROOT
from echo.presentation import button, keyboard
from echo.services import Denied, Limited
from echo.sources import public_target
from echo.storage import FileObject, WorkspaceItem
from echo.tasks import Tasks

ASSETS = ROOT / "assets/echo"
TOOL_ASSETS = {module: f"telegram/tools/{module}.png" for module in REGISTRY}


class ToolsUI:
    def __init__(self, ui):
        self.ui = ui
        self.config, self.jobs, self.redis = ui.config, ui.jobs, ui.redis
        self.tasks = Tasks(ui.config, ui.jobs, ui.redis, DomainLimiter(ui.redis, ui.config.domain_interval))
        self.vault, self.folders = self.tasks.vault, self.tasks.folders
        from echo.journeys import Journeys

        self.journeys = Journeys(self)

    async def brand(self, event, user, asset, caption, markup=None):
        path = ASSETS / asset
        if (
            self.config.brand_images
            and user.language == "ru"
            and len(caption) < 1000
            and await asyncio.to_thread(path.is_file)
        ):
            message = event.message if isinstance(event, CallbackQuery) else event
            await message.answer_photo(FSInputFile(path), caption=caption, reply_markup=markup)
        else:
            await self.ui.send_view(event, caption, markup, new=isinstance(event, CallbackQuery))

    def home_markup(self, lang):
        return keyboard(
            [button(tr(lang, "goal_nick"), "jg:nick"), button(tr(lang, "goal_site"), "jg:site")],
            [button(tr(lang, "goal_file"), "jg:file"), button(tr(lang, "tools"), "home:tools")],
            [button(tr(lang, "folders"), "home:folders"), button(tr(lang, "account"), "home:account")],
            [button(tr(lang, "settings_menu"), "home:settings")],
        )

    async def home(self, event, u, cover=False):
        await self.redis.delete(self.ui.key(u.id, "input"))
        if cover:
            caption = (
                tr(u.language, "welcome", brand=escape(self.config.brand))
                + "\n\n"
                + tr(u.language, "limitations")
            )
            await self.brand(event, u, "telegram/welcome.png", caption, self.home_markup(u.language))
        else:
            await self.ui.send_view(event, tr(u.language, "home"), self.home_markup(u.language))

    async def tools(self, event, u):
        availability = await self.tasks.availability()
        rows = [
            [
                button(
                    tr(u.language, "tool_" + key) + ("" if availability[key] else " · —"), "xt:choose:" + key
                )
            ]
            for key in REGISTRY
        ]
        rows.append([button(tr(u.language, "back"), "home:menu")])
        await self.ui.send_view(event, tr(u.language, "tools"), keyboard(*rows))

    async def tool(self, event, u, module, *, keep=False):
        if module not in REGISTRY or not (await self.tasks.availability()).get(module):
            raise ModuleError("module_unavailable")
        flow = await self.ui.get(u.id, "input") if keep else None
        if not flow:
            flow = {
                "type": "tool",
                "module": module,
                "language": "rus+eng",
                "variants": self.config.domain_variants,
                "dns": True,
            }
        await self.ui.put(u.id, "input", flow, 600)
        lang = u.language
        text = (
            "<b>"
            + tr(lang, "tool_" + module)
            + "</b>\n\n"
            + tr(lang, "intro_" + module)
            + "\n\n"
            + tr(
                lang,
                "tool_limits",
                mb=self.config.file_max_mb,
                pixels=self.config.image_max_pixels,
                seconds=getattr(self.config, module + "_timeout"),
            )
        )
        rows = []
        if module == "ocr":
            rows.append(
                [
                    button(("✓ " if flow["language"] == language else "") + label, "xt:lang:" + language)
                    for language, label in [("rus", "RU"), ("eng", "EN"), ("rus+eng", "RU+EN")]
                ]
            )
        if module == "domains":
            choices = sorted({min(n, self.config.domain_variants) for n in (10, 25, 50)})
            rows.append(
                [button(("✓ " if flow["variants"] == n else "") + str(n), "xt:n:" + str(n)) for n in choices]
            )
            text += "\n" + tr(
                lang,
                "domain_scope",
                variants=flow["variants"],
                queries=min(flow["variants"] * 2, self.config.domain_dns_queries),
            )
        rows.append([button(tr(lang, "cancel"), "home:tools")])
        if keep:
            await self.ui.send_view(event, text, keyboard(*rows))
        else:
            await self.brand(event, u, TOOL_ASSETS[module], text, keyboard(*rows))

    async def confirm_tool(self, event, u, module, parameters, *, check_recent=True):
        if check_recent and await self.journeys.offer_module(event, u, module, parameters):
            return
        nonce = secrets.token_hex(8)
        draft = {"nonce": nonce, "module": module, "parameters": parameters}
        await self.ui.put(u.id, "tool_draft", draft, 600)
        await self.redis.delete(self.ui.key(u.id, "input"))
        if "file_id" in parameters:
            file = await self.vault.get(u.id, parameters["file_id"])
            label = file.name
        else:
            label = parameters["target"]
        scope = (
            tr(
                u.language,
                "domain_scope",
                variants=parameters["variants"],
                queries=min(2 * parameters["variants"], self.config.domain_dns_queries)
                if parameters.get("dns")
                else 0,
            )
            if module == "domains"
            else ""
        )
        if parameters.get("action") == "clean":
            scope = tr(u.language, "clean_warning")
        text = tr(
            u.language,
            "tool_confirm",
            name=tr(u.language, "tool_" + module),
            input=escape(label[:1000]),
            scope=scope,
        )
        key = "authorize_domain" if module == "domains" else "confirm"
        await self.brand(
            event,
            u,
            "telegram/tools/metadata-clean.png"
            if parameters.get("action") == "clean"
            else TOOL_ASSETS[module],
            text,
            keyboard(
                [button(tr(u.language, key), "xt:run:" + nonce)],
                [button(tr(u.language, "cancel"), "home:tools")],
            ),
        )

    async def upload(self, message):
        u = await self.jobs.user(message.from_user.id)
        self.ui.generation.set(u.generation)
        lang = u.language
        flow = await self.ui.get(u.id, "input")
        if message.photo and flow and flow.get("module") == "metadata":
            await message.answer(tr(lang, "send_original"))
            return
        incoming = message.document or message.photo[-1]
        if incoming.file_size and incoming.file_size > self.config.file_max_mb * 1024**2:
            raise ModuleError("file_limit")
        await message.answer(tr(lang, "uploading"))
        file = await message.bot.get_file(incoming.file_id)
        if not file.file_path:
            raise ModuleError("invalid_file")
        name = getattr(incoming, "file_name", None) or "image.jpg"
        size = incoming.file_size or file.file_size or self.config.file_max_mb * 1024**2
        url = message.bot.session.api.file_url(message.bot.token, file.file_path)
        chunks = message.bot.session.stream_content(url, timeout=60, chunk_size=65536)
        entry = await self.vault.upload(u.id, u.generation, chunks, size, name)
        if flow and flow.get("type") == "folder_file":
            await self.folders.add_file(u.id, flow["folder"], entry.id)
            await self.vault.discard(entry)
            await self.redis.delete(self.ui.key(u.id, "input"))
            await message.answer(tr(lang, "folder_saved"))
        elif flow and flow.get("type") == "tool" and flow["module"] in {"metadata", "ocr", "qr"}:
            await self.confirm_tool(
                message, u, flow["module"], {"file_id": entry.id, "language": flow.get("language", "rus+eng")}
            )
        else:
            await self.ui.put(u.id, "input", {"type": "upload_choice", "file_id": entry.id}, 600)
            await message.answer(
                tr(lang, "file_choose_action"),
                reply_markup=keyboard(
                    *[
                        [button(tr(lang, "tool_" + key), "xt:uploaded:" + key)]
                        for key in ("metadata", "ocr", "qr")
                    ]
                ),
            )

    async def on_text(self, message, u):
        flow = await self.ui.get(u.id, "input")
        if not flow or flow.get("type") not in {
            "tool",
            "folder_create",
            "folder_rename",
            "folder_note",
            "folder_file",
        }:
            return False
        kind, text, lang = flow["type"], message.text.strip(), u.language
        if kind == "tool":
            module = flow["module"]
            if module in {"metadata", "ocr", "qr"}:
                await message.answer(tr(lang, "input_required"))
                return True
            parameters = {"target": public_target(text)}
            if module == "domains":
                parameters.update(variants=flow["variants"], dns=flow["dns"])
            await self.confirm_tool(message, u, module, parameters)
        elif kind == "folder_create":
            f = await self.folders.create(u.id, text)
            await self.redis.delete(self.ui.key(u.id, "input"))
            await self.folder_view(message, u, f.id)
        elif kind == "folder_rename":
            await self.folders.rename(u.id, flow["folder"], text)
            await self.redis.delete(self.ui.key(u.id, "input"))
            await self.folder_view(message, u, flow["folder"])
        elif kind == "folder_note":
            if not text or len(text) > 1000:
                await message.answer(tr(lang, "folder_note_prompt"))
                return True
            await self.folders.add(u.id, flow["folder"], "note", text[:80], {"text": message.text})
            await self.redis.delete(self.ui.key(u.id, "input"))
            await self.folder_view(message, u, flow["folder"])
        else:
            await message.answer(tr(lang, "folder_file_prompt", mb=self.config.file_max_mb))
        return True

    async def folder_list(self, event, u):
        folders = await self.folders.list(u.id)
        rows = [[button(f.name, "wf:view:" + f.id)] for f in folders]
        rows += [
            [button(tr(u.language, "folder_new"), "wf:create")],
            [button(tr(u.language, "back"), "home:menu")],
        ]
        await self.brand(
            event,
            u,
            "telegram/workspaces.png",
            tr(u.language, "folders" if folders else "folder_empty"),
            keyboard(*rows),
        )

    async def folder_view(self, event, u, folder_id, page=0):
        folder = await self.folders.owned(u.id, folder_id)
        items = await self.folders.items(u.id, folder_id)
        page = max(0, min(page, max(0, (len(items) - 1) // 8)))
        rows = [[button(i.title[:60], "wi:" + i.id)] for i in items[page * 8 : (page + 1) * 8]]
        if len(items) > 8:
            rows += [
                [
                    button("←", f"wf:view:{folder_id}:{max(0, page - 1)}"),
                    button("→", f"wf:view:{folder_id}:{min((len(items) - 1) // 8, page + 1)}"),
                ]
            ]
        rows += [
            [button(tr(u.language, "folder_add_report"), "wf:reports:" + folder_id)],
            [
                button(tr(u.language, "folder_add_file"), "wf:file:" + folder_id),
                button(tr(u.language, "folder_add_note"), "wf:note:" + folder_id),
            ],
            [button(tr(u.language, "folder_rename"), "wf:rename:" + folder_id)],
            [
                button(tr(u.language, "package"), "wf:export:" + folder_id),
                button(tr(u.language, "package_age"), "wf:age:" + folder_id),
            ],
            [button(tr(u.language, "folder_delete"), "wf:delete:" + folder_id)],
            [button(tr(u.language, "back"), "wf:list")],
        ]
        text = tr(
            u.language,
            "folder_view",
            name=escape(folder.name),
            count=len(items),
            until=folder.expires_at.isoformat(),
        )
        await self.ui.send_view(event, text, keyboard(*rows))

    async def save_picker(self, event, u, job_id, prefix=None):
        await self.jobs.owned(u.id, job_id)
        folders = await self.folders.list(u.id)
        if not folders:
            await self.folder_list(event, u)
            return
        nonce = secrets.token_hex(6)
        await self.ui.put(u.id, "folder_material", {"job": job_id, "prefix": prefix, "nonce": nonce}, 600)
        await self.ui.send_view(
            event,
            tr(u.language, "folder_choose"),
            keyboard(*[[button(f.name, f"wp:{nonce}:{f.id}")] for f in folders]),
        )

    async def account(self, event, u):
        jobs, folders = await self.jobs.list(u.id), await self.folders.list(u.id)
        async with self.jobs.sessions() as s:
            size = await s.scalar(
                select(func.coalesce(func.sum(FileObject.size), 0)).where(FileObject.owner_id == u.id)
            )
        text = tr(
            u.language,
            "account_summary",
            language=u.language,
            history=tr(u.language, "on" if u.history else "off"),
            ai=tr(u.language, "on" if u.ai else "off"),
            jobs=len(jobs),
            folders=len(folders),
            bytes=size,
            quota=self.config.user_disk_quota_mb * 1024**2,
            slots=self.config.heavy_slots,
            profile=self.config.resource_profile,
        )
        await self.brand(
            event,
            u,
            "future/personal-account.png",
            text,
            keyboard(
                [button(tr(u.language, "tasks"), "home:tasks"), button(tr(u.language, "folders"), "wf:list")],
                [button(tr(u.language, "settings_menu"), "home:settings")],
            ),
        )

    async def compare_first(self, event, u):
        jobs = [
            j
            for j in await self.jobs.list(u.id)
            if j.state in {JobState.COMPLETED, JobState.PARTIAL}
            and (j.kind == "search" or j.kind == "module" and j.module_id in {"passport", "page", "domains"})
        ]
        await self.brand(
            event,
            u,
            "telegram/comparison.png",
            tr(u.language, "compare_first"),
            keyboard(
                *[[button(self.label(j, u.language), "xc:first:" + j.id)] for j in jobs],
                [button(tr(u.language, "back"), "home:menu")],
            ),
        )

    @staticmethod
    def label(job, lang):
        name = job.username or (
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
        return (name + " · " + job.state + " · " + job.id[:6])[:60]

    async def queue_export(self, event, u, source, fmt, recipient=None):
        await self.jobs.owned(u.id, source)
        await self.vault.space()
        request = event.id if isinstance(event, CallbackQuery) else str(event.message_id)
        nonce = hashlib.sha256(json.dumps([u.id, source, fmt, recipient, request]).encode()).hexdigest()
        job = await self.jobs.create(
            u.id,
            "",
            [],
            "export",
            False,
            "export:" + nonce,
            kind="export",
            parameters={"source_job": source, "format": fmt, "language": u.language, "recipient": recipient},
        )
        await self.ui.queue_view(event, u, job)

    async def show_result(self, event, u, job_id):
        j = await self.jobs.owned(u.id, job_id)
        lang = u.language
        name = (
            tr(lang, "tool_" + j.module_id)
            if j.module_id
            else tr(
                lang,
                "comparison"
                if j.kind == "compare"
                else "folder_export"
                if j.kind == "folder_export"
                else "export_job",
            )
        )
        rows = []
        if j.failures and not j.result:
            reason = next(iter(j.failures.values()))
            summary = tr(lang, "task_failed_reason", reason=tr(lang, reason))
        elif not j.result:
            summary = tr(lang, "job_unready")
        else:
            data = j.result.get("data", {})
            if j.module_id == "metadata":
                summary = tr(
                    lang,
                    "metadata_summary",
                    mime=data["mime"],
                    size=data["bytes"],
                    fields=len(data["fields"]),
                    sensitive=len(data["sensitive"]),
                )
                rows += [[button(tr(lang, "all_fields"), f"xm:{j.id}:0")]]
                if data["sensitive"]:
                    rows += [[button(tr(lang, "sensitive_fields"), f"xm:{j.id}:1")]]
                if data["clean_supported"]:
                    rows += [[button(tr(lang, "clean_copy"), "xclean:" + j.id)]]
                else:
                    summary += "\n" + tr(lang, "clean_unavailable")
                for notice in data.get("warnings", []):
                    if notice in {
                        "metadata_dates_missing",
                        "metadata_dates_conflict",
                        "metadata_parser_warning",
                    }:
                        summary += "\n" + tr(lang, notice)
                if data.get("cleanup"):
                    summary += "\n" + tr(lang, "clean_report_notice")
            elif j.module_id == "ocr":
                summary = tr(lang, "ocr_summary", language=data["language"], count=len(data["text"]))
                rows += [[button(tr(lang, "text_view"), f"xp:{j.id}:0")]]
            elif j.module_id == "qr":
                summary = tr(lang, "qr_summary", count=data["count"])
                if data["codes"]:
                    rows += [[button(tr(lang, "qr_view"), f"xq:{j.id}:0")]]
            elif j.module_id == "page":
                summary = tr(
                    lang, "page_summary", title=escape(str(data["title"])[:300]), count=len(data["text"])
                )
                rows += [[button(tr(lang, "text_view"), f"xp:{j.id}:0")]]
            elif j.module_id == "passport":
                summary = tr(
                    lang,
                    "passport_summary",
                    domain=escape(data["domain_ascii"]),
                    idn=escape(data["domain_unicode"]),
                    status=data["http_status"],
                    tls=tr(lang, "on" if data["tls"]["verified"] else "unavailable"),
                    until=escape(str(data["tls"].get("not_after", "—"))),
                    technologies=escape(", ".join(t["technology"] for t in data["technologies"])[:300])
                    or "—",
                )
                rows += self.journeys.next_rows(j, lang)
            elif j.module_id == "domains":
                summary = tr(
                    lang,
                    "domains_summary",
                    count=len(data["variants"]),
                    queries=data["dns_queries"],
                    generated=data["generated"],
                )
                summary += "\n\n" + "\n".join(
                    escape(v["domain"]) + " · " + escape(v["transformation"]) for v in data["variants"][:10]
                )
            elif j.kind == "compare":
                diff = j.result["difference"]
                summary = tr(
                    lang,
                    "comparison_summary",
                    new=len(diff["new"]),
                    changed=len(diff["changed"]),
                    missing=len(diff["not_checked_in_second"]),
                    same=len(diff["unchanged"]),
                    warnings="\n".join(
                        tr(lang, w) for w in j.result["warnings"] if w != "comparison_not_account_deletion"
                    ),
                )
            else:
                summary = tr(lang, "checksum_notice")
            for f in await self.vault.for_job(u.id, j.id, "output"):
                label = tr(lang, "full_json") if f.name == "report.json" else f.name
                rows.append([button(label, "xf:" + f.id)])
            if j.kind in {"module", "compare"}:
                rows += [
                    [
                        button(tr(lang, "package"), "xz:" + j.id),
                        button(tr(lang, "package_age"), "xza:" + j.id),
                    ],
                    [button(tr(lang, "save_folder"), "fs:" + j.id)],
                ]
        rows += [
            [button(tr(lang, "refresh"), "xr:" + j.id), button(tr(lang, "stop"), "s:" + j.id)],
            [button(tr(lang, "delete_job"), "dj:" + j.id), button(tr(lang, "back"), "home:tools")],
        ]
        await self.ui.send_view(
            event,
            tr(lang, "tool_result", name=name, state=j.state, summary=summary),
            keyboard(*rows),
            new=isinstance(event, CallbackQuery) and event.message.message_id == j.message_id,
        )

    async def text_view(self, event, u, j, page, sensitive=False, qr_index=None):
        data = j.result["data"]
        if j.module_id == "metadata":
            value = data["sensitive"] if sensitive else data["fields"]
            text = json.dumps(value, ensure_ascii=False, indent=2)
            prefix = f"xm:{j.id}:{int(sensitive)}:"
        else:
            text = data["codes"][qr_index]["text"] if qr_index is not None else data["text"]
            prefix = f"xqp:{j.id}:{qr_index}:" if qr_index is not None else f"xp:{j.id}:"
        pages = max(1, (len(text) + 1199) // 1200)
        page = max(0, min(page, pages - 1))
        part = text[page * 1200 : (page + 1) * 1200]
        safe_part = "".join(c if ord(c) >= 32 or c in "\n\t" else f"\\u{ord(c):04x}" for c in part)
        rows = [
            [
                InlineKeyboardButton(
                    text=tr(u.language, "copy") + f" {i // 256 + 1}",
                    copy_text=CopyTextButton(text=safe_part[i : i + 256]),
                )
            ]
            for i in range(0, len(safe_part), 256)
        ]
        rows += [
            [
                button("←", prefix + str(max(0, page - 1))),
                button("→", prefix + str(min(pages - 1, page + 1))),
            ],
            [button(tr(u.language, "back"), "xr:" + j.id)],
        ]
        await self.ui.send_view(
            event, f"{page + 1}/{pages}\n<pre>" + escape(safe_part) + "</pre>", keyboard(*rows)
        )

    async def qr_view(self, event, u, j, index):
        codes = j.result["data"]["codes"]
        if not codes:
            raise Denied()
        index = max(0, min(index, len(codes) - 1))
        code = codes[index]
        text = tr(
            u.language,
            "qr_content",
            index=index + 1,
            total=len(codes),
            type=escape(code["content_type"]),
            domain=escape(code.get("domain") or "—"),
            content="<pre>" + escape(code["text"][:1000].replace("\x00", "\\u0000")) + "</pre>",
        )
        rows = [[button(tr(u.language, "text_view"), f"xqp:{j.id}:{index}:0")]]
        if code.get("url"):
            rows.append([InlineKeyboardButton(text=tr(u.language, "open"), url=code["url"])])
        rows += self.journeys.qr_rows(j, code, index, u.language)
        rows += [
            [
                button("←", f"xq:{j.id}:{max(0, index - 1)}"),
                button("→", f"xq:{j.id}:{min(len(codes) - 1, index + 1)}"),
            ],
            [button(tr(u.language, "back"), "xr:" + j.id)],
        ]
        await self.ui.send_view(event, text, keyboard(*rows))

    async def download(self, event, u, file_id):
        entry = await self.vault.get(u.id, file_id)
        path = self.vault.path(entry.storage_key)
        if await asyncio.to_thread(self.vault.digest, path) != entry.sha256:
            raise ModuleError("invalid_file")
        await self.vault.get(u.id, file_id)
        message = event.message if isinstance(event, CallbackQuery) else event
        await message.answer_document(FSInputFile(path, filename=entry.name))

    async def callback(self, cb, u, parts):
        if await self.journeys.callback(cb, u, parts):
            return True
        action, lang = parts[0], u.language
        if action == "home":
            choice = parts[1]
            await self.redis.delete(self.ui.key(u.id, "input"))
            if choice == "menu":
                await self.home(cb, u)
            elif choice == "tools":
                await self.tools(cb, u)
            elif choice == "folders":
                await self.folder_list(cb, u)
            elif choice == "account":
                await self.account(cb, u)
            elif choice == "settings":
                await self.ui.preferences(cb, u)
            elif choice == "tasks":
                await self.ui.task_list(cb, u)
            elif choice == "search":
                await self.ui.send_view(cb, tr(lang, "invalid_username"))
            return True
        if action == "xt":
            choice = parts[1]
            if choice == "choose":
                await self.tool(cb, u, parts[2])
            elif choice in {"lang", "n"}:
                flow = await self.ui.get(u.id, "input")
                if not flow or flow.get("type") != "tool":
                    raise Denied()
                if choice == "lang" and parts[2] in {"rus", "eng", "rus+eng"}:
                    flow["language"] = parts[2]
                elif choice == "n" and 1 <= int(parts[2]) <= self.config.domain_variants:
                    flow["variants"] = int(parts[2])
                else:
                    raise Denied()
                await self.ui.put(u.id, "input", flow, 600)
                await self.tool(cb, u, flow["module"], keep=True)
            elif choice == "uploaded":
                flow = await self.ui.get(u.id, "input")
                if not flow or flow.get("type") != "upload_choice":
                    raise Denied()
                await self.confirm_tool(cb, u, parts[2], {"file_id": flow["file_id"]})
            elif choice == "run":
                draft = await self.ui.get(u.id, "tool_draft")
                if not draft or draft["nonce"] != parts[2]:
                    raise Denied()
                params = dict(draft["parameters"])
                if draft["module"] == "domains":
                    params["authorized"] = True
                job = await self.tasks.submit(u.id, draft["module"], params, f"tool:{u.id}:{draft['nonce']}")
                await self.ui.queue_view(cb, u, job)
            else:
                raise Denied()
            return True
        if action in {"xr", "xf", "xp", "xm", "xq", "xqp", "xclean", "xz", "xza", "fs"}:
            if action == "xf":
                await self.download(cb, u, parts[1])
                return True
            j = await self.jobs.owned(u.id, parts[1])
            if action == "xr":
                await self.show_result(cb, u, j.id)
            elif action == "fs":
                await self.save_picker(cb, u, j.id, parts[2] if len(parts) > 2 else None)
            elif action in {"xz", "xza"}:
                if action == "xza":
                    await self.confirm_package(cb, u, {"source_job": j.id})
                else:
                    await self.queue_export(cb, u, j.id, "zip")
            elif not j.result:
                raise Limited()
            elif action == "xclean":
                if j.module_id != "metadata" or not j.result["data"]["clean_supported"]:
                    raise ModuleError("unsupported_clean")
                await self.confirm_tool(
                    cb, u, "metadata", {"file_id": j.parameters["file_id"], "action": "clean"}
                )
            elif action == "xp":
                await self.text_view(cb, u, j, int(parts[2]))
            elif action == "xm":
                await self.text_view(
                    cb, u, j, int(parts[3]) if len(parts) > 3 else 0, sensitive=parts[2] == "1"
                )
            elif action == "xqp":
                await self.text_view(cb, u, j, int(parts[3]), qr_index=int(parts[2]))
            elif action == "xq":
                await self.qr_view(cb, u, j, int(parts[2]))
            return True
        if action == "xc":
            if parts[1] == "list":
                await self.compare_first(cb, u)
            elif parts[1] == "first":
                first = await self.jobs.owned(u.id, parts[2])
                await self.ui.put(u.id, "comparison", {"first": first.id}, 600)
                jobs = [
                    j
                    for j in await self.jobs.list(u.id)
                    if j.id != first.id
                    and j.kind == first.kind
                    and j.module_id == first.module_id
                    and j.username == first.username
                    and j.state in {JobState.COMPLETED, JobState.PARTIAL}
                ]
                await self.ui.send_view(
                    cb,
                    tr(lang, "compare_second"),
                    keyboard(*[[button(self.label(j, lang), "xc:second:" + j.id)] for j in jobs]),
                )
            elif parts[1] == "second":
                state = await self.ui.get(u.id, "comparison")
                if not state:
                    raise Denied()
                await self.jobs.owned(u.id, state["first"])
                await self.jobs.owned(u.id, parts[2])
                nonce = secrets.token_hex(8)
                state.update(second=parts[2], nonce=nonce)
                await self.ui.put(u.id, "comparison", state, 600)
                await self.ui.send_view(
                    cb,
                    tr(lang, "compare_confirm", first=state["first"][:8], second=state["second"][:8]),
                    keyboard([button(tr(lang, "authorize_compare"), "xc:run:" + nonce)]),
                )
            elif parts[1] == "run":
                state = await self.ui.get(u.id, "comparison")
                if not state or state.get("nonce") != parts[2]:
                    raise Denied()
                for source in (state["first"], state["second"]):
                    await self.jobs.owned(u.id, source)
                job = await self.jobs.create(
                    u.id,
                    "",
                    [],
                    "compare",
                    False,
                    f"compare:{u.id}:{parts[2]}",
                    kind="compare",
                    parameters={"first": state["first"], "second": state["second"], "authorized": True},
                )
                await self.ui.queue_view(cb, u, job)
            return True
        if action == "wp":
            state = await self.ui.get(u.id, "folder_material")
            if not state or state["nonce"] != parts[1]:
                raise Denied()
            await self.folders.add_job(u.id, parts[2], state["job"], lang, state.get("prefix"))
            await self.redis.delete(self.ui.key(u.id, "folder_material"))
            await self.ui.send_view(cb, tr(lang, "folder_saved"))
            return True
        if action in {"wi", "wr"}:
            async with self.jobs.sessions() as s:
                item = await s.get(WorkspaceItem, parts[1])
            if not item:
                raise Denied()
            await self.folders.owned(u.id, item.folder_id)
            if action == "wr":
                await self.folders.remove_item(u.id, item.folder_id, item.id)
                await self.folder_view(cb, u, item.folder_id)
            else:
                rows = [
                    [button(tr(lang, "folder_remove_item"), "wr:" + item.id)],
                    [button(tr(lang, "back"), "wf:view:" + item.folder_id)],
                ]
                rows[0:0] = [
                    [button(tr(lang, "export"), "xf:" + fid)] for fid in item.data.get("file_ids", [])
                ]
                text = item.data.get("text") or json.dumps(
                    item.data.get("snapshot", item.data), ensure_ascii=False, indent=2
                )
                await self.ui.send_view(
                    cb,
                    "<b>" + escape(item.title) + "</b>\n<pre>" + escape(text[:2000]) + "</pre>",
                    keyboard(*rows),
                )
            return True
        if action == "wf":
            choice = parts[1]
            if choice == "list":
                await self.folder_list(cb, u)
            elif choice == "create":
                await self.ui.put(u.id, "input", {"type": "folder_create"}, 600)
                await self.ui.send_view(cb, tr(lang, "folder_name_prompt", days=self.config.folder_ttl_days))
            else:
                folder_id = parts[2]
                await self.folders.owned(u.id, folder_id)
                if choice == "view":
                    await self.folder_view(cb, u, folder_id, int(parts[3]) if len(parts) > 3 else 0)
                elif choice in {"rename", "note", "file"}:
                    await self.ui.put(u.id, "input", {"type": "folder_" + choice, "folder": folder_id}, 600)
                    key = {
                        "rename": "folder_name_prompt",
                        "note": "folder_note_prompt",
                        "file": "folder_file_prompt",
                    }[choice]
                    await self.ui.send_view(
                        cb, tr(lang, key, days=self.config.folder_ttl_days, mb=self.config.file_max_mb)
                    )
                elif choice == "reports":
                    jobs = await self.jobs.list(u.id)
                    await self.ui.put(u.id, "folder_destination", folder_id, 600)
                    await self.ui.send_view(
                        cb,
                        tr(lang, "folder_choose_report"),
                        keyboard(*[[button(self.label(j, lang), "wadd:" + j.id)] for j in jobs]),
                    )
                elif choice in {"export", "age"}:
                    if choice == "age":
                        await self.confirm_package(cb, u, {"folder_id": folder_id})
                    else:
                        await self.queue_folder(cb, u, folder_id)
                elif choice == "delete":
                    await self.ui.send_view(
                        cb,
                        tr(lang, "folder_delete_confirm"),
                        keyboard(
                            [
                                button(tr(lang, "confirm"), "wf:erase:" + folder_id),
                                button(tr(lang, "cancel"), "wf:view:" + folder_id),
                            ]
                        ),
                    )
                elif choice == "erase":
                    await self.folders.delete(u.id, folder_id)
                    await self.folder_list(cb, u)
                elif choice == "add":
                    # Here parts[2] is a job; use the user's explicitly selected destination.
                    raise Denied()
            return True
        if action == "wadd":
            folder_id = await self.ui.get(u.id, "folder_destination")
            if not folder_id:
                raise Denied()
            await self.folders.add_job(u.id, folder_id, parts[1], lang)
            await self.folder_view(cb, u, folder_id)
            return True
        if action == "wage":
            state = await self.ui.get(u.id, "package_recipient")
            if not state or state["nonce"] != parts[1] or state["recipient"] != u.recipient:
                raise Denied()
            await self.redis.delete(self.ui.key(u.id, "package_recipient"))
            if "folder_id" in state:
                await self.queue_folder(cb, u, state["folder_id"], u.recipient)
            else:
                await self.queue_export(cb, u, state["source_job"], "zip", u.recipient)
            return True
        return False

    async def confirm_package(self, event, u, parameters):
        if not u.recipient:
            await self.ui.send_view(event, tr(u.language, "need_key"))
            return
        nonce = secrets.token_hex(8)
        await self.ui.put(
            u.id, "package_recipient", {**parameters, "recipient": u.recipient, "nonce": nonce}, 300
        )
        await self.brand(
            event,
            u,
            "telegram/secure-export.png",
            tr(u.language, "recipient_confirm", format="ZIP", fingerprint=fingerprint(u.recipient)),
            keyboard([button(tr(u.language, "confirm"), "wage:" + nonce)]),
        )

    async def queue_folder(self, event, u, folder_id, recipient=None):
        await self.vault.space()
        await self.folders.owned(u.id, folder_id)
        request = event.id if isinstance(event, CallbackQuery) else str(event.message_id)
        nonce = hashlib.sha256(json.dumps([folder_id, recipient, request]).encode()).hexdigest()[:32]
        job = await self.jobs.create(
            u.id,
            "",
            [],
            "folder_export",
            False,
            f"folder-export:{u.id}:{nonce}",
            kind="folder_export",
            parameters={"folder_id": folder_id, "recipient": recipient},
        )
        await self.ui.queue_view(event, u, job)
