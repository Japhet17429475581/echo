import asyncio
import csv
import hashlib
import io
import json
import re
from importlib.resources import files
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo

from jinja2 import Environment, StrictUndefined, select_autoescape
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from echo.coverage import coverage_text, search_coverage
from echo.domain import safe_url
from echo.engines import kill_tree
from echo.i18n import tr
from echo.services import as_utc


class ExportError(ValueError):
    pass


def csv_cell(value):
    text = "" if value is None else str(value)
    # Spreadsheet applications may trim whitespace before interpreting formulas.
    if text.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def fingerprint(recipient):
    return "SHA256:" + hashlib.sha256(recipient.encode("ascii")).hexdigest()


async def encrypt(data: bytes, recipient: str, binary="age") -> bytes:
    if not re.fullmatch(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}", recipient):
        raise ExportError("invalid_key")
    try:
        p = await asyncio.create_subprocess_exec(
            binary,
            "--encrypt",
            "--recipient",
            recipient,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=__import__("os").name == "posix",
        )
    except OSError as e:
        raise ExportError("age_unavailable") from e
    try:
        output, _ = await asyncio.wait_for(p.communicate(data), 30)
        if p.returncode:
            raise ExportError("invalid_key")
        return output
    finally:
        await kill_tree(p)


class Reports:
    def __init__(self, jobs, config):
        self.jobs, self.config = jobs, config

    async def snapshot(self, owner, job_id, language):
        j = await self.jobs.owned(owner, job_id)
        if j.kind != "search":
            raise ExportError("invalid_format")
        cards = await self.jobs.cards(owner, job_id, language)
        coverage = search_coverage(j, cards)
        for card in cards:
            if card["site"]:
                card["site"]["sources"] = [u for u in card["site"]["sources"] if safe_url(u)]
        # Only explicit public fields are serialized, never ORM objects/DSNs/exceptions.
        return {
            "schema_version": "echo.report/1",
            "brand": self.config.brand,
            "username": j.username,
            "job_id": j.id,
            "created_at": as_utc(j.created_at).astimezone(ZoneInfo(self.config.timezone)).isoformat(),
            "timezone": self.config.timezone,
            "state": j.state,
            "enrichment": j.enrichment,
            "parameters": {"engines": j.engines, "coverage": j.coverage, "ai_descriptions": j.ai},
            "engine_progress": j.progress,
            "engine_errors": j.failures,
            "limitations": tr(language, "limitations"),
            "results": cards,
            "coverage_summary": coverage,
            "coverage_text": coverage_text(coverage, language),
        }

    async def build(self, owner, job_id, fmt, language="ru", recipient=None):
        if fmt not in {"html", "pdf", "csv", "json"}:
            raise ExportError("invalid_format")
        data = await self.snapshot(owner, job_id, language)
        content = await asyncio.to_thread(self.render, data, fmt, language)
        if recipient:
            content = await encrypt(content, recipient, self.config.age_binary)
        if len(content) > self.config.telegram_file_mb * 1024 * 1024:
            raise ExportError("file_too_large")
        # Recheck after generation: a concurrent deletion invalidates delivery.
        await self.jobs.owned(owner, job_id)
        return content, f"echo-{job_id[:8]}.{fmt}" + (".age" if recipient else "")

    def render(self, data, fmt, language):
        if fmt == "json":
            return json.dumps(data, ensure_ascii=False, indent=2).encode()
        if fmt == "html":
            template = files("echo.templates").joinpath("report.html").read_text("utf-8")
            env = Environment(autoescape=select_autoescape(default=True), undefined=StrictUndefined)
            return (
                env.from_string(template)
                .render(report=data, lang=language, t=lambda key: tr(language, key))
                .encode()
            )
        if fmt == "csv":
            out = io.StringIO(newline="")
            writer = csv.writer(out)
            writer.writerow(
                [
                    "schema_version",
                    "username",
                    "created_at",
                    "timezone",
                    "job_state",
                    "coverage",
                    "site",
                    "site_id",
                    "url",
                    "engine",
                    "version",
                    "raw_status",
                    "status",
                    "http_code",
                    "final_url",
                    "evidence",
                    "check_method",
                    "error_type",
                    "checked_at",
                    "description",
                    "description_origin",
                    "category",
                    "activities",
                    "sources",
                    "mark",
                    "note",
                    "limitations",
                    "personal_review_reason",
                    "personal_reviewed_at",
                    "coverage_checks",
                    "coverage_blocked",
                    "coverage_timeouts",
                    "coverage_not_checked_known",
                    "coverage_known",
                ]
            )
            for card in data["results"]:
                site = card["site"] or {}
                for o in card["observations"]:
                    writer.writerow(
                        list(
                            map(
                                csv_cell,
                                [
                                    data["schema_version"],
                                    data["username"],
                                    data["created_at"],
                                    data["timezone"],
                                    data["state"],
                                    data["parameters"]["coverage"],
                                    card["name"],
                                    card["site_id"],
                                    card["url"],
                                    o["engine"],
                                    o["version"],
                                    o["raw_status"],
                                    o["status"],
                                    o.get("http_code"),
                                    o.get("final_url"),
                                    o.get("evidence"),
                                    o.get("check_method"),
                                    o.get("error_type"),
                                    o["checked_at"],
                                    site.get("description"),
                                    site.get("origin"),
                                    site.get("category"),
                                    "; ".join(site.get("activities", [])),
                                    "; ".join(site.get("sources", [])),
                                    card["mark"],
                                    card["note"],
                                    data["limitations"],
                                    card.get("false_positive_reason"),
                                    card.get("reviewed_at"),
                                    *[
                                        data.get("coverage_summary", {}).get(key)
                                        for key in (
                                            "checks",
                                            "blocked",
                                            "timeouts",
                                            "not_checked_known",
                                            "coverage_known",
                                        )
                                    ],
                                ],
                            )
                        )
                    )
            return out.getvalue().encode("utf-8-sig")
        return self.pdf(data, language)

    def pdf(self, data, language):
        font = self.config.pdf_font
        if not font.is_file():
            raise ExportError("pdf_font_unavailable")
        font_name = "EchoUnicode"
        if font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(font_name, str(font)))
        style = ParagraphStyle(
            "Echo",
            fontName=font_name,
            fontSize=9,
            leading=14,
            alignment=TA_LEFT,
            wordWrap="CJK",
            textColor=colors.HexColor("#183443"),
        )
        title = ParagraphStyle("Title", parent=style, fontSize=18, leading=24, spaceAfter=12)
        heading = ParagraphStyle("Heading", parent=style, fontSize=12, leading=18, spaceBefore=12)
        out, story = io.BytesIO(), []

        def para(text, selected=style):
            story.append(Paragraph(escape(str(text)).replace("\n", "<br/>"), selected))

        def link(url):
            clean = safe_url(url)
            if clean:
                story.append(
                    Paragraph(f'<link href={quoteattr(clean)} color="#126B78">{escape(clean)}</link>', style)
                )

        para(f"{data['brand']} · {data['username']}", title)
        para(f"{data['created_at']} · {data['timezone']} · {data['state']}")
        para(json.dumps(data["parameters"], ensure_ascii=False))
        para(data["limitations"])
        if data.get("coverage_text"):
            para(data["coverage_text"])
        para(f"AI: {data['enrichment']}; {data['engine_errors']}")
        for c in data["results"]:
            para(c["name"], heading)
            link(c["url"] or "")
            site = c["site"] or {}
            para(site.get("description") or tr(language, "description_unavailable"))
            para(
                f"{tr(language, 'origin')}: {site.get('origin', 'unavailable')} · "
                f"{tr(language, 'category')}: {site.get('category', 'other')}"
            )
            for activity in site.get("activities", []):
                para("• " + activity)
            for o in c["observations"]:
                para(f"{o['engine']} {o['version']} · {o['status']} ({o['raw_status']}) · {o['checked_at']}")
                para(f"HTTP: {o.get('http_code')} · {o.get('error_type')} · {o.get('check_method')}")
                para(o.get("evidence") or tr(language, "no_evidence"))
                if o.get("final_url"):
                    link(o["final_url"])
            para(f"{tr(language, 'mark')}: {c['mark'] or '—'}\n{tr(language, 'note')}: {c['note']}")
            if c["mark"] == "false_positive":
                para(
                    tr(
                        language,
                        "review_summary",
                        reason=tr(language, "reason_" + (c.get("false_positive_reason") or "unspecified")),
                    )
                )
                if c.get("reviewed_at"):
                    para(c["reviewed_at"])
            for source in site.get("sources", []):
                link(source)
            story.append(Spacer(1, 3 * mm))

        def footer(canvas, doc):
            canvas.setFont(font_name, 8)
            canvas.drawString(18 * mm, 12 * mm, f"{data['brand']} · {doc.page}")

        SimpleDocTemplate(
            out,
            pagesize=(210 * mm, 297 * mm),
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=16 * mm,
            bottomMargin=20 * mm,
            title=f"{data['brand']} report",
            author=data["brand"],
        ).build(story, onFirstPage=footer, onLaterPages=footer)
        return out.getvalue()
