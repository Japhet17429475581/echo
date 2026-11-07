import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
from contextvars import ContextVar
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, LinkPreviewOptions, Message
from sqlalchemy import select

from echo import __version__
from echo.ai import (
    Explanation,
    Intent,
    IntentName,
    Operation,
    Unavailable,
    masked_intent_text,
    technical_facts,
)
from echo.domain import Category, username
from echo.export import ExportError, encrypt, fingerprint
from echo.i18n import tr
from echo.intents import local_intent
from echo.modules import ModuleError
from echo.presentation import CATEGORIES, button, card_view, explain, keyboard, progress, results, setup
from echo.services import Denied, FolderLimited, Limited, StorageLimited
from echo.storage import Job, User
from echo.tools_ui import ASSETS

log = logging.getLogger("echo")


def limit_key(error):
    if isinstance(error, FolderLimited):
        return "folder_capacity"
    return "storage_full" if isinstance(error, StorageLimited) else "limited"


class Telegram:
    def __init__(self, config, jobs, redis, reports, gateway, admin):
        self.config, self.jobs, self.redis = config, jobs, redis
        self.reports, self.gateway, self.admin = reports, gateway, admin
        self.generation = ContextVar("echo_ui_generation", default=None)
        self.router = Router()
        self.router.message.filter(F.chat.type == "private")
        self.router.callback_query.filter(F.message.chat.type == "private")
        self.router.message.register(
            self.command,
            Command(
                "start",
                "help",
                "search",
                "tasks",
                "settings",
                "privacy",
                "delete_my_data",
                "about",
                "cancel",
                "admin",
                "menu",
                "tools",
                "folders",
                "compare",
                "account",
            ),
        )
        self.router.callback_query.register(self.callback)
        self.router.message.register(self.document, F.document | F.photo)
        self.router.message.register(self.text, F.text)
        from echo.tools_ui import ToolsUI

        self.extras = ToolsUI(self)

    def key(self, owner, name):
        return f"echo:user:{owner}:{name}"

    async def put(self, owner, name, value, ttl=3600):
        return await self.mutate(owner, name, lambda previous: value, ttl)

    async def mutate(self, owner, name, change, ttl=3600):
        async with self.jobs.sessions.begin() as s:
            u = await s.get(User, owner, with_for_update=True)
            if not u or u.generation != self.generation.get():
                raise Denied()
            previous = await self.get(owner, name)
            value = change(previous)
            await self.redis.set(self.key(owner, name), json.dumps(value), ex=ttl)
            return value

    async def clear_ui(self, owner):
        async for key in self.redis.scan_iter(match=self.key(owner, "*")):
            await self.redis.delete(key)

    async def erase_user_ui(self, owner):
        await self.extras.vault.erase_owner(owner)
        await self.clear_ui(owner)

    async def ai_active(self, user, job_id):
        try:
            job = await self.jobs.owned(user.id, job_id)
        except Denied:
            return False
        async with self.jobs.sessions() as s:
            current = await s.get(User, user.id)
        return bool(current and current.ai and current.generation == user.generation and not job.cancelled)

    async def get(self, owner, name):
        value = await self.redis.get(self.key(owner, name))
        return json.loads(value) if value else None

    async def send_view(self, event, text, markup=None, *, new=False):
        if isinstance(event, CallbackQuery):
            if new or event.message.caption is not None:
                return await event.message.answer(text, reply_markup=markup)
            try:
                return await event.message.edit_text(text, reply_markup=markup)
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    return event.message
                raise
        return await event.answer(text, reply_markup=markup)

    async def preferences(self, event, u):
        lang = u.language
        text = tr(
            lang,
            "settings",
            history=tr(lang, "on" if u.history else "off"),
            ai=tr(lang, "on" if u.ai else "off"),
            language=u.language,
            recipient=fingerprint(u.recipient) if u.recipient else "—",
        )
        markup = keyboard(
            [
                button(
                    tr(lang, "history_toggle", value=tr(lang, "on" if u.history else "off")), "pref:history"
                ),
                button(tr(lang, "ai_toggle", value=tr(lang, "on" if u.ai else "off")), "pref:ai"),
            ],
            [button(tr(lang, "language"), "pref:language")],
            [button(tr(lang, "add_key"), "pref:key"), button(tr(lang, "remove_key"), "pref:remove")],
        )
        await self.send_view(event, text, markup)

    async def command(self, message: Message):
        u = await self.jobs.user(message.from_user.id)
        self.generation.set(u.generation)
        lang = u.language
        cmd, _, arg = message.text.partition(" ")
        cmd = cmd.split("@")[0][1:]
        if cmd == "start":
            await self.extras.home(message, u, cover=True)
        elif cmd in {"menu", "tools", "folders", "compare", "account"}:
            method = {
                "menu": self.extras.home,
                "tools": self.extras.tools,
                "folders": self.extras.folder_list,
                "compare": self.extras.compare_first,
                "account": self.extras.account,
            }[cmd]
            await method(message, u)
        elif cmd == "help":
            await message.answer(tr(lang, "help"))
        elif cmd == "search":
            if arg.strip():
                await self.new_search(message, u, arg)
            else:
                await message.answer(tr(lang, "invalid_username"))
        elif cmd == "settings":
            await self.preferences(message, u)
        elif cmd == "tasks":
            await self.task_list(message, u)
        elif cmd == "privacy":
            await message.answer(
                tr(lang, "privacy", hours=self.config.result_ttl_hours, days=self.config.history_ttl_days)
            )
        elif cmd == "delete_my_data":
            nonce = secrets.token_hex(8)
            await self.put(u.id, "delete", nonce, 300)
            await message.answer(
                tr(lang, "delete_confirm"),
                reply_markup=keyboard(
                    [button(tr(lang, "confirm"), f"delete:{nonce}"), button(tr(lang, "cancel"), "dismiss")]
                ),
            )
        elif cmd == "about":
            await message.answer(
                tr(
                    lang,
                    "about",
                    brand=escape(self.config.brand),
                    version=__version__,
                    limits=tr(lang, "limitations"),
                )
            )
        elif cmd == "cancel":
            await self.redis.delete(self.key(u.id, "input"))
            await message.answer(tr(lang, "cancel"))
        elif cmd == "admin" and u.id in self.config.admin_ids:
            await self.admin_view(message, u)

    async def new_search(self, event, u, value):
        try:
            nick = username(value)
        except ValueError:
            await self.send_view(event, tr(u.language, "invalid_username"))
            return
        await self.redis.delete(self.key(u.id, "input"))
        draft = {
            "username": nick,
            "engines": ["sherlock", "maigret"],
            "coverage": "curated",
            "ai": u.ai,
            "id": secrets.token_hex(16),
        }
        await self.put(u.id, "draft", draft)
        if await self.extras.journeys.offer_search(event, u, draft):
            return
        text, markup = setup(draft, u.language)
        await self.send_view(event, text, markup)

    async def task_list(self, event, u):
        jobs = await self.jobs.list(u.id)
        if not jobs:
            await self.send_view(event, tr(u.language, "no_tasks"))
            return
        rows = [[button(self.extras.label(j, u.language), f"r:{j.id}:a:0")] for j in jobs]
        rows.append([button(tr(u.language, "compare"), "xc:list")])
        await self.send_view(event, tr(u.language, "tasks"), keyboard(*rows))

    async def show_results(self, event, u, job_id, mode="a", page=0):
        job = await self.jobs.owned(u.id, job_id)
        if job.kind != "search":
            await self.extras.show_result(event, u, job_id)
            return
        cards = await self.jobs.cards(u.id, job_id, u.language)
        await self.put(u.id, "last_job", job_id)
        text, markup = results(job, cards, mode, page, u.language)
        new = isinstance(event, CallbackQuery) and event.message.message_id == job.message_id
        await self.send_view(event, text, markup, new=new)

    async def find_card(self, u, job_id, prefix):
        if len(prefix) != 12 or any(c not in "0123456789abcdef" for c in prefix):
            raise Denied()
        cards = await self.jobs.cards(u.id, job_id, u.language)
        found = [c for c in cards if c["key"].startswith(prefix)]
        if len(found) != 1:
            raise Denied()
        return found[0]

    async def show_card(self, event, u, job_id, prefix):
        card = await self.find_card(u, job_id, prefix)
        text, markup = card_view(card, job_id, u.language)
        await self.send_view(event, text, markup)

    async def queue_view(self, event, u, job):
        if job.message_id:
            await self.show_results(event, u, job.id)
            return
        await self.put(u.id, "last_job", job.id)
        if job.kind == "search":
            await self.extras.brand(event, u, "telegram/search-running.png", tr(u.language, "home_search"))
        text, markup = progress(job, self.config, u.language)
        m = await self.send_view(event, text, markup, new=isinstance(event, CallbackQuery))
        await self.jobs.attach(u.id, job.id, m.chat.id, m.message_id)
        if job.kind == "search":
            async with self.jobs.sessions.begin() as s:
                current = await s.get(Job, job.id, with_for_update=True)
                if current and current.owner_id == u.id:
                    current.ui_stage = "started"

    async def export(self, event, u, job_id, fmt, recipient=None):
        source = await self.jobs.owned(u.id, job_id)
        if source.kind != "search" and fmt != "zip":
            matching = [
                f
                for f in await self.extras.vault.for_job(u.id, job_id, "output")
                if f.name.endswith("." + fmt)
            ]
            if matching and recipient:
                await self.extras.queue_export(event, u, job_id, fmt, recipient)
            elif matching:
                await self.extras.download(event, u, matching[0].id)
            else:
                await self.extras.show_result(event, u, job_id)
            return
        if fmt in {"pdf", "zip"} or recipient:
            await self.extras.queue_export(event, u, job_id, fmt, recipient)
            return
        # Export retries are explicit, with a short lock to absorb double clicks.
        lock = self.key(u.id, f"export:{job_id}")
        if not await self.redis.set(lock, "1", nx=True, ex=90):
            return
        try:
            content, name = await self.reports.build(u.id, job_id, fmt, u.language, recipient)
            message = event.message if isinstance(event, CallbackQuery) else event
            await message.answer_document(BufferedInputFile(content, name))
        finally:
            await self.redis.delete(lock)

    async def callback(self, cb: CallbackQuery):
        await cb.answer()
        u = await self.jobs.user(cb.from_user.id)
        self.generation.set(u.generation)
        lang = u.language
        parts = (cb.data or "").split(":")
        action = parts[0]
        try:
            if await self.extras.callback(cb, u, parts):
                return
            if action == "dismiss":
                await self.redis.delete(self.key(u.id, "input"))
                await self.send_view(cb, tr(lang, "cancel"))
            elif action == "cfg":
                draft = await self.get(u.id, "draft")
                if not draft:
                    raise Denied()
                if parts[1] == "run":
                    if await self.extras.journeys.offer_search(cb, u, draft):
                        return
                    for e in draft["engines"]:
                        if await self.redis.exists(f"echo:disabled:{e}"):
                            raise Limited()
                    job = await self.jobs.create(
                        u.id,
                        draft["username"],
                        draft["engines"],
                        draft["coverage"],
                        draft["ai"],
                        f"{u.id}:{draft['id']}",
                    )
                    await self.queue_view(cb, u, job)
                else:
                    field, value = parts[1:3]
                    if field == "e" and value in {"sherlock", "maigret", "both"}:
                        draft["engines"] = ["sherlock", "maigret"] if value == "both" else [value]
                    elif field == "c" and value in {"curated", "top100", "extended"}:
                        draft["coverage"] = value
                    elif field == "a":
                        draft["ai"] = not draft["ai"]
                        if draft["ai"]:
                            await self.jobs.settings(u.id, ai=True)
                    else:
                        raise Denied()
                    await self.put(u.id, "draft", draft)
                    text, markup = setup(draft, lang)
                    await self.send_view(cb, text, markup)
            elif action == "pref":
                choice = parts[1]
                if choice == "key":
                    await self.put(u.id, "input", {"type": "key"}, 300)
                    await self.send_view(cb, tr(lang, "key_prompt"))
                    return
                updates = {
                    "history": {"history": not u.history},
                    "ai": {"ai": not u.ai},
                    "language": {"language": "en" if lang == "ru" else "ru"},
                    "remove": {"recipient": None},
                }
                if choice not in updates:
                    raise Denied()
                await self.jobs.settings(u.id, **updates[choice])
                if choice == "remove":
                    await self.redis.delete(self.key(u.id, "recipient_confirm"))
                await self.preferences(cb, await self.jobs.user(u.id))
            elif action == "delete":
                if await self.get(u.id, "delete") != parts[1]:
                    raise Denied()
                await self.jobs.delete_user(u.id, clear=lambda: self.erase_user_ui(u.id))
                await self.send_view(cb, tr(lang, "deleted"))
            elif action == "r":
                await self.show_results(cb, u, parts[1], parts[2], int(parts[3]))
            elif action == "categories":
                job_id = parts[1]
                await self.jobs.owned(u.id, job_id)
                await self.send_view(
                    cb,
                    tr(lang, "category"),
                    keyboard(
                        *[[button(tr(lang, c), f"r:{job_id}:c{i}:0")] for i, c in enumerate(CATEGORIES)]
                    ),
                )
            elif action == "c":
                await self.show_card(cb, u, parts[1], parts[2])
            elif action in {"m", "n", "why", "more", "url"}:
                job_id, prefix = parts[1:3]
                card = await self.find_card(u, job_id, prefix)
                if action == "m":
                    mark = {"u": "useful", "f": "false_positive", "c": "clear"}[parts[3]]
                    if mark == "false_positive":
                        await self.extras.journeys.reason_picker(cb, u, job_id, prefix)
                        return
                    await self.jobs.annotate(u.id, job_id, card["key"], mark=mark)
                    await self.show_card(cb, u, job_id, prefix)
                elif action == "n":
                    await self.put(u.id, "input", {"type": "note", "job": job_id, "key": card["key"]}, 600)
                    await self.send_view(cb, tr(lang, "note_prompt"))
                elif action == "url":
                    await cb.message.answer("<code>" + escape(card["url"]) + "</code>")
                elif action == "why":
                    text, unusual = explain(card, lang)
                    rows = [[button(tr(lang, "back"), f"c:{job_id}:{prefix}")]]
                    if unusual:
                        rows.insert(0, [button(tr(lang, "more"), f"more:{job_id}:{prefix}")])
                    await self.send_view(cb, text, keyboard(*rows))
                elif action == "more":
                    _, unusual = explain(card, lang)
                    if not u.ai or not unusual:
                        raise Unavailable()
                    response = await self.gateway.cached(
                        Operation.EXPLANATION,
                        {"observations": [technical_facts(o) for o in card["observations"]]},
                        lang,
                        u.generation,
                        Explanation,
                        guard=lambda: self.ai_active(u, job_id),
                    )
                    await self.jobs.owned(u.id, job_id)
                    await self.send_view(
                        cb,
                        escape(response.explanation),
                        keyboard([button(tr(lang, "back"), f"c:{job_id}:{prefix}")]),
                    )
            elif action == "s":
                await self.jobs.cancel(u.id, parts[1])
                await cb.message.answer(tr(lang, "stopped"))
            elif action == "retry":
                job = await self.jobs.retry(u.id, parts[1], f"retry:{u.id}:{parts[1]}")
                await self.queue_view(cb, u, job)
            elif action == "formats":
                job_id, encrypted = parts[1:3]
                await self.jobs.owned(u.id, job_id)
                if encrypted == "1" and not u.recipient:
                    await cb.message.answer(tr(lang, "need_key"))
                    return
                await self.send_view(
                    cb,
                    tr(lang, "export_choose"),
                    keyboard(
                        [button(fmt.upper(), f"x:{job_id}:{fmt}:{encrypted}") for fmt in ("html", "pdf")],
                        [button(fmt.upper(), f"x:{job_id}:{fmt}:{encrypted}") for fmt in ("csv", "json")],
                    ),
                )
            elif action == "x":
                job_id, fmt, encrypted = parts[1:4]
                await self.jobs.owned(u.id, job_id)
                if fmt not in {"html", "pdf", "csv", "json"}:
                    raise Denied()
                if encrypted == "1":
                    if not u.recipient:
                        raise Denied()
                    token = secrets.token_hex(12)
                    await self.put(
                        u.id,
                        "recipient_confirm",
                        {"job": job_id, "format": fmt, "recipient": u.recipient, "token": token},
                        300,
                    )
                    await self.extras.brand(
                        cb,
                        u,
                        "telegram/secure-export.png",
                        tr(
                            lang,
                            "recipient_confirm",
                            format=fmt.upper(),
                            fingerprint=fingerprint(u.recipient),
                        ),
                        keyboard(
                            [
                                button(tr(lang, "confirm"), "age:" + token),
                                button(tr(lang, "cancel"), "dismiss"),
                            ]
                        ),
                    )
                else:
                    await self.export(cb, u, job_id, fmt)
            elif action == "age":
                confirmed = await self.get(u.id, "recipient_confirm")
                if not confirmed or confirmed["token"] != parts[1] or confirmed["recipient"] != u.recipient:
                    raise Denied()
                await self.redis.delete(self.key(u.id, "recipient_confirm"))
                await self.export(cb, u, confirmed["job"], confirmed["format"], u.recipient)
            elif action == "dj":
                await self.jobs.owned(u.id, parts[1])
                token = secrets.token_hex(8)
                await self.put(u.id, "delete_job", {"job": parts[1], "token": token}, 300)
                await self.send_view(
                    cb,
                    tr(lang, "delete_job_confirm"),
                    keyboard(
                        [
                            button(tr(lang, "confirm"), f"deletejob:{token}"),
                            button(tr(lang, "cancel"), "dismiss"),
                        ]
                    ),
                )
            elif action == "deletejob":
                confirmed = await self.get(u.id, "delete_job")
                if not confirmed or confirmed["token"] != parts[1]:
                    raise Denied()
                await self.jobs.delete_job(u.id, confirmed["job"], clear=lambda: self.clear_ui(u.id))
                await self.extras.vault.janitor()
                await self.redis.delete(self.key(u.id, "delete_job"))
                await self.send_view(cb, tr(lang, "deleted"))
            elif action.startswith("admin"):
                if u.id not in self.config.admin_ids:
                    raise Denied()
                if action == "admintoggle" and parts[1] in {"sherlock", "maigret", "ai"}:
                    key = "echo:disabled:" + parts[1]
                    if await self.redis.exists(key):
                        await self.redis.delete(key)
                    else:
                        await self.redis.set(key, "1")
                    await self.admin_view(cb, u)
                elif action == "admincatalog":
                    await self.send_view(cb, tr(lang, "catalog_hint", count=await self.admin.catalog_count()))
                else:
                    await self.admin_view(cb, u)
            else:
                raise Denied()
        except Denied:
            await cb.message.answer(tr(lang, "denied"))
        except Limited as error:
            await cb.message.answer(tr(lang, limit_key(error)))
        except Unavailable:
            await cb.message.answer(tr(lang, "ai_unavailable"))
        except ExportError as e:
            await cb.message.answer(tr(lang, str(e)))
        except ModuleError as e:
            await cb.message.answer(tr(lang, str(e)))
        except (IndexError, KeyError, ValueError):
            await cb.message.answer(tr(lang, "expired"))

    async def text(self, message: Message):
        u = await self.jobs.user(message.from_user.id)
        self.generation.set(u.generation)
        lang = u.language
        try:
            if await self.extras.journeys.on_text(message, u):
                return
            if await self.extras.on_text(message, u):
                return
            flow = await self.get(u.id, "input")
            if flow:
                if flow["type"] == "key":
                    key = message.text.strip()
                    await encrypt(b"", key, self.config.age_binary)  # actual age checksum validation
                    await self.jobs.settings(u.id, recipient=key)
                    await message.answer(tr(lang, "key_saved", fingerprint=fingerprint(key)))
                elif flow["type"] == "note":
                    await self.jobs.annotate(u.id, flow["job"], flow["key"], note=message.text)
                    await message.answer(tr(lang, "saved"))
                await self.redis.delete(self.key(u.id, "input"))
                return
            intent = local_intent(message.text)
            if intent:
                await self.handle_intent(message, u, intent)
                return
            try:
                username(message.text)
            except ValueError:
                pass
            else:
                await self.new_search(message, u, message.text)
                return
            last = await self.get(u.id, "last_job")
            if u.ai and self.config.ai_intents and last:
                job = await self.jobs.owned(u.id, last)
                known = [j.username for j in await self.jobs.list(u.id)]
                masked = masked_intent_text(
                    message.text,
                    [
                        *known,
                        job.username,
                        str(u.id),
                        message.from_user.username or "",
                        message.from_user.full_name,
                    ],
                )
                if masked:
                    result = await self.gateway.generate(
                        Operation.INTENT,
                        {"text": masked, "available_actions": [i.value for i in IntentName]},
                        lang,
                        u.generation,
                        Intent,
                        guard=lambda: self.ai_active(u, last),
                    )
                    # AI-parsed actions always become confirmation buttons, never automatic effects.
                    await self.handle_intent(message, u, result, confirm=True)
                    return
            await message.answer(tr(lang, "choose_action"))
        except Denied:
            await message.answer(tr(lang, "denied"))
        except Limited as error:
            await message.answer(tr(lang, limit_key(error)))
        except Unavailable:
            await message.answer(tr(lang, "ai_unavailable"))
        except ExportError as e:
            await message.answer(tr(lang, str(e)))
        except (ModuleError, ValueError):
            await message.answer(tr(lang, "invalid_input"))

    async def document(self, message: Message):
        try:
            await self.extras.upload(message)
        except Denied:
            await message.answer(tr("ru", "denied"))
        except Limited as error:
            u = await self.jobs.user(message.from_user.id)
            await message.answer(tr(u.language, limit_key(error)))
        except ModuleError as e:
            u = await self.jobs.user(message.from_user.id)
            await message.answer(tr(u.language, str(e)))

    async def handle_intent(self, message, u, intent, confirm=False):
        lang = u.language
        if intent.intent in {IntentName.HELP, IntentName.UNKNOWN}:
            await message.answer(tr(lang, "help" if intent.intent == IntentName.HELP else "choose_action"))
            return
        job_id = await self.get(u.id, "last_job")
        if not job_id:
            await self.task_list(message, u)
            return
        await self.jobs.owned(u.id, job_id)
        if intent.intent == IntentName.CANCEL_JOB:
            data = f"s:{job_id}"
        elif intent.intent == IntentName.RETRY_FAILED:
            data = f"retry:{job_id}"
        elif intent.intent == IntentName.EXPORT_REPORT:
            data = f"x:{job_id}:{intent.format}:0"
        elif intent.intent == IntentName.SHOW_ERRORS:
            data = f"r:{job_id}:e:0"
        elif intent.intent == IntentName.SET_CATEGORY_FILTER:
            data = f"r:{job_id}:c{CATEGORIES.index(Category(intent.category))}:0"
        else:
            data = f"r:{job_id}:a:0"
        if confirm or intent.intent in {IntentName.CANCEL_JOB, IntentName.RETRY_FAILED}:
            await message.answer(
                tr(lang, "choose_action"), reply_markup=keyboard([button(tr(lang, "confirm"), data)])
            )
        elif intent.intent == IntentName.EXPORT_REPORT:
            await self.export(message, u, job_id, intent.format)
        else:
            mode = data.split(":")[2]
            await self.show_results(message, u, job_id, mode)

    async def admin_view(self, event, u):
        status = await self.admin.snapshot()
        rows = [
            [button(tr(u.language, "admin_toggle", feature=f), f"admintoggle:{f}")]
            for f in ("sherlock", "maigret", "ai")
        ]
        rows += [
            [button(tr(u.language, "catalog"), "admincatalog")],
            [button(tr(u.language, "refresh"), "admin")],
        ]
        await self.send_view(
            event,
            tr(
                u.language,
                "admin_status",
                status=escape(json.dumps(status, ensure_ascii=False, indent=2)[:3000]),
            ),
            keyboard(*rows),
        )

    async def progress_loop(self, bot):
        seen = {}
        while True:
            try:
                async with self.jobs.sessions() as s:
                    jobs = list(await s.scalars(select(Job).where(Job.message_id.is_not(None))))
                seen = {key: value for key, value in seen.items() if key in {j.id for j in jobs}}
                for job in jobs:
                    async with self.jobs.sessions() as s:
                        u = await s.get(User, job.owner_id)
                    if not u or u.generation != job.generation:
                        continue
                    try:
                        job = await self.jobs.owned(u.id, job.id)
                    except Denied:
                        continue
                    text, markup = progress(job, self.config, u.language)
                    digest = hashlib.sha256(text.encode()).digest()
                    if seen.get(job.id) == digest:
                        continue
                    try:
                        await bot.edit_message_text(
                            text, chat_id=job.chat_id, message_id=job.message_id, reply_markup=markup
                        )
                        seen[job.id] = digest
                        if (
                            self.config.brand_images
                            and job.kind == "search"
                            and job.ui_stage == "started"
                            and job.state in {"COMPLETED", "PARTIAL"}
                            and await asyncio.to_thread((ASSETS / "telegram/search-completed.png").is_file)
                        ):
                            await bot.send_photo(
                                job.chat_id,
                                FSInputFile(ASSETS / "telegram/search-completed.png"),
                                caption=tr(u.language, "show_ready"),
                                reply_markup=keyboard(
                                    [button(tr(u.language, "show_ready"), f"r:{job.id}:a:0")]
                                ),
                            )
                            async with self.jobs.sessions.begin() as s:
                                current = await s.get(Job, job.id, with_for_update=True)
                                if current and current.generation == job.generation:
                                    current.ui_stage = "finished"
                    except TelegramRetryAfter as e:
                        await asyncio.sleep(min(e.retry_after, 30))
                    except (TelegramBadRequest, TelegramForbiddenError):
                        seen[job.id] = digest
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("progress_update_failed")
            await asyncio.sleep(self.config.progress_interval)

    async def run(self):
        if not self.config.bot_token.get_secret_value():
            raise ValueError("ECHO_BOT_TOKEN is required")
        bot = Bot(
            self.config.bot_token.get_secret_value(),
            default=DefaultBotProperties(
                parse_mode="HTML", link_preview=LinkPreviewOptions(is_disabled=True)
            ),
        )
        dispatcher = Dispatcher()
        dispatcher.include_router(self.router)

        @dispatcher.message()
        async def private_only(message: Message):
            if message.chat.type != "private":
                await message.answer(tr("ru", "private_only"))

        @dispatcher.errors()
        async def safe_error(event):
            # aiogram's default exception logger includes Update content: replace it completely.
            log.warning("telegram_operation_failed type=%s", type(event.exception).__name__)
            msg = event.update.message or (
                event.update.callback_query.message if event.update.callback_query else None
            )
            if msg and msg.chat.type == "private":
                with contextlib.suppress(Exception):
                    await msg.answer(tr("ru", "error"))
            return True

        updater = asyncio.create_task(self.progress_loop(bot))
        try:
            await dispatcher.start_polling(
                bot,
                allowed_updates=["message", "callback_query"],
                handle_signals=True,
                close_bot_session=True,
            )
        finally:
            updater.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await updater
