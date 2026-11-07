"""On-demand, networkless tools bridge. Inputs/outputs are JSON, never CLI text scraping for results."""

import base64
import ctypes
import importlib.metadata
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import warnings
from pathlib import Path

from sandbox import isolate


class ToolError(ValueError):
    pass


def parent_death():
    parent = os.getppid()
    ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0)
    if os.getppid() != parent or parent == 1:
        os._exit(1)


def cli(args, maximum=524288):
    p = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        preexec_fn=parent_death,
    )
    output = bytearray()
    try:
        while chunk := p.stdout.read(8192):
            output.extend(chunk)
            if len(output) > maximum:
                raise ToolError("output_limit")
        if p.wait() != 0:
            raise ToolError("tool_failed")
        return bytes(output)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        p.stdout.close()


def exif(path):
    records = json.loads(cli(["exiftool", "-config", "", "-json", "-G1", "-a", "-s", "-n", str(path)]))
    if not records or records[0].get("ExifTool:Error"):
        raise ToolError("unsupported_format")
    fields = records[0]
    for key in list(fields):
        if key.startswith("System:") or key in {
            "SourceFile",
            "File:FileName",
            "File:Directory",
            "File:FilePermissions",
            "File:FileAccessDate",
            "File:FileModifyDate",
            "File:FileInodeChangeDate",
        }:
            fields.pop(key)
    return fields


def image(path, request):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = request["max_pixels"]
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    try:
        with Image.open(path) as original:
            if original.format not in {"JPEG", "PNG", "WEBP"}:
                raise ToolError("unsupported_format")
            width, height = original.size
            if width * height > request["max_pixels"] or getattr(original, "n_frames", 1) > 1:
                raise ToolError("image_limit")
            fmt = original.format
            original.verify()
        with Image.open(path) as original:
            oriented = ImageOps.exif_transpose(original)
            oriented.load()
            return oriented.copy(), fmt
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise ToolError("image_limit") from e
    except ToolError:
        raise
    except Exception as e:
        raise ToolError("invalid_file") from e


def metadata(path, request):
    mime = cli(["file", "--brief", "--mime-type", "--", str(path)], 4096).decode().strip()
    supported = {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/tiff",
        "image/gif",
        "application/pdf",
        "audio/mpeg",
        "video/mp4",
        "audio/x-wav",
        "audio/vnd.wave",
        "audio/wav",
    }
    if mime not in supported:
        raise ToolError("unsupported_format")
    fields = exif(path)
    sensitive, common = {}, {}
    for key, value in fields.items():
        if any(
            word in key.lower()
            for word in (
                "gps",
                "location",
                "serial",
                "owner",
                "author",
                "artist",
                "creator",
                "person",
                "address",
            )
        ):
            sensitive[key] = value
        else:
            common[key] = value
    dates = {k: v for k, v in fields.items() if "date" in k.lower() or "timeoriginal" in k.lower()}
    notices = ["metadata_editable", "dates_unverified"]
    if not dates:
        notices.append("metadata_dates_missing")
    originals = {str(value) for key, value in fields.items() if key.endswith("DateTimeOriginal")}
    if len(originals) > 1:
        notices.append("metadata_dates_conflict")
    if any("warning" in key.lower() for key in fields):
        notices.append("metadata_parser_warning")
    data = {
        "mime": mime,
        "bytes": path.stat().st_size,
        "fields": common,
        "sensitive": sensitive,
        "dates_as_stored": dates,
        "clean_supported": mime in {"image/jpeg", "image/png"},
        "warnings": notices,
    }
    outputs = []
    if request.get("action") == "clean":
        if not data["clean_supported"]:
            raise ToolError("unsupported_clean")
        from PIL import Image

        picture, fmt = image(path, request)
        mode = (
            "RGBA" if fmt == "PNG" and (picture.mode == "RGBA" or "transparency" in picture.info) else "RGB"
        )
        picture = picture.convert(mode)
        fresh = Image.frombytes(mode, picture.size, picture.tobytes())
        target = Path("cleaned.jpg" if fmt == "JPEG" else "cleaned.png")
        fresh.save(target, format=fmt, **({"quality": 95} if fmt == "JPEG" else {}))
        check, check_format = image(target, request)
        if check.size != fresh.size or check_format != fmt:
            raise ToolError("cleanup_verification_failed")
        clean_fields = exif(target)
        forbidden = (
            "gps",
            "exififd:",
            "ifd0:",
            "xmp",
            "iptc",
            "comment",
            "author",
            "artist",
            "description",
            "makernote",
        )
        if any(any(word in k.lower() for word in forbidden) for k in clean_fields):
            raise ToolError("cleanup_verification_failed")
        data["cleanup"] = {
            "verified": True,
            "width": check.width,
            "height": check.height,
            "orientation_applied": True,
            "remaining_fields": clean_fields,
            "removed": ["EXIF", "GPS", "XMP", "IPTC", "text_comments", "ICC"],
            "warnings": ["cleanup_not_anonymization", "color_profile_removed"],
        }
        outputs.append({"path": str(target), "name": str(target), "mime": mime})
    return data, outputs, {"exiftool": cli(["exiftool", "-ver"], 4096).decode().strip()}


def ocr(path, request):
    picture, _ = image(path, request)
    language = request.get("language", "rus+eng")
    if language not in {"rus", "eng", "rus+eng"}:
        raise ToolError("invalid_language")
    picture.convert("RGB").save("ocr-input.png")
    text = cli(
        ["tesseract", "ocr-input.png", "stdout", "-l", language, "--psm", "3"], request["output_bytes"]
    ).decode("utf-8")
    Path("text.txt").write_text(text, "utf-8")
    version = cli(["tesseract", "--version"], 8192).decode()
    return (
        {
            "text": text,
            "language": language,
            "width": picture.width,
            "height": picture.height,
            "warnings": ["ocr_may_contain_errors"],
            "omp_thread_limit": int(os.environ["OMP_THREAD_LIMIT"]),
            "openmp_reported_by_build": "OpenMP" in version,
        },
        [{"path": "text.txt", "name": "ocr.txt", "mime": "text/plain"}],
        {"tesseract": version.splitlines()[0]},
    )


def qr(path, request):
    import zxingcpp

    picture, _ = image(path, request)
    codes = zxingcpp.read_barcodes(picture.convert("RGB"), formats=zxingcpp.BarcodeFormat.QRCode)
    if len(codes) > 64:
        raise ToolError("output_limit")
    entries = []
    for code in codes:
        text = code.text
        if len(text.encode()) > 16384:
            raise ToolError("output_limit")
        entries.append(
            {
                "text": text,
                "content_type": str(code.content_type),
                "format": str(code.format),
                "raw_base64": base64.b64encode(code.bytes).decode(),
            }
        )
    return (
        {"codes": entries, "count": len(entries), "warnings": ["qr_not_safety_verdict"]},
        [],
        {"zxing-cpp": importlib.metadata.version("zxing-cpp")},
    )


def page(path, request):
    import trafilatura

    extracted = trafilatura.bare_extraction(
        path.read_bytes(),
        url=request["url"],
        include_comments=False,
        include_tables=True,
        include_links=False,
        with_metadata=True,
    )
    if not extracted or not extracted.text:
        raise ToolError("text_not_found")
    text = extracted.text
    if len(text.encode()) > request["output_bytes"] // 2:
        raise ToolError("output_limit")
    data = {
        "title": extracted.title or "",
        "text": text,
        "metadata": {
            "author": extracted.author,
            "date": extracted.date,
            "sitename": extracted.sitename,
            "provenance": "automatically_extracted_from_page_unverified",
        },
        "warnings": ["page_not_archive", "page_metadata_unverified"],
    }
    Path("page.txt").write_text(text, "utf-8")
    # Prefix every untrusted Markdown line with a quote; prevent embedded HTML/links.
    title = str(data["title"]).replace("\n", " ")

    def md(value):
        return re.sub(r"([\\`*_{}\[\]()#+.!<>])", r"\\\1", value)

    Path("page.md").write_text(
        "# " + md(title) + "\n\n" + "\n".join("> " + md(line) for line in text.splitlines()), "utf-8"
    )
    from html import escape

    html = (
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'\">"
        "<style>body{max-width:70ch;margin:2rem auto;padding:1rem;font:18px/1.6 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>"
        "<h1>" + escape(title) + "</h1><pre>" + escape(text) + "</pre>"
    )
    Path("page.html").write_text(html, "utf-8")
    return (
        data,
        [
            {"path": "page." + ext, "name": "page." + ext, "mime": mime}
            for ext, mime in (("txt", "text/plain"), ("md", "text/markdown"), ("html", "text/html"))
        ],
        {"trafilatura": trafilatura.__version__},
    )


def technologies(path, request):
    target = Path("page.html")
    shutil.copyfile(path, target)
    cli(
        [
            "whatweb",
            "--aggression=1",
            "--max-threads=1",
            "--no-errors",
            "--log-json=whatweb.json",
            str(target.resolve()),
        ],
        16384,
    )
    records = json.loads(Path("whatweb.json").read_text("utf-8"))
    allowed = {
        "Apache",
        "nginx",
        "WordPress",
        "Drupal",
        "Joomla",
        "PHP",
        "ASP_NET",
        "jQuery",
        "HTML5",
        "HTTPServer",
        "MetaGenerator",
        "Bootstrap",
        "React",
        "Vuejs",
        "Wix",
        "Shopify",
        "Cloudflare",
    }
    detected = []
    for record in records:
        for name, evidence in record.get("plugins", {}).items():
            if name in allowed:
                clean = {
                    k: v
                    for k, v in evidence.items()
                    if k in {"version", "string", "certainty", "module", "model"}
                }
                detected.append(
                    {"technology": name, "evidence": clean, "origin": "WhatWeb_passive_saved_response"}
                )
    version = cli(["whatweb", "--version"], 8192).decode().strip()
    return (
        {
            "technologies": detected,
            "activity": "aggression_1_offline",
            "warnings": ["technologies_are_indications"],
        },
        [],
        {"whatweb": version[:120]},
    )


def domains(path, request):
    import dnstwist

    fuzzer = dnstwist.Fuzzer(domain=request["domain"])
    fuzzer.generate()
    entries = fuzzer.permutations()
    selected = [
        {"domain": p["domain"], "transformation": p["fuzzer"]} for p in entries[: request["variants"]]
    ]
    return (
        {
            "domain": request["domain"],
            "variants": selected,
            "generated": len(entries),
            "truncated": len(entries) > len(selected),
            "warnings": ["similarity_not_phishing"],
        },
        [],
        {"dnstwist": importlib.metadata.version("dnstwist")},
    )


def report(path, request):
    from echo.config import Settings
    from echo.export import Reports

    snapshot = json.loads(path.read_text("utf-8"))
    config = Settings(_env_file=None, timezone=request["timezone"], pdf_font=Path(request["pdf_font"]))
    binary = Reports(None, config).render(snapshot, request["format"], request["language"])
    if len(binary) > request["export_mb"] * 1024**2:
        raise ToolError("output_limit")
    name = "report." + request["format"]
    Path(name).write_bytes(binary)
    return (
        {"format": request["format"]},
        [{"path": name, "name": name, "mime": "application/octet-stream"}],
        {},
    )


def archive(path, request):
    from echo.config import Settings
    from echo.packages import package

    payload = json.loads(path.read_text("utf-8"))
    materials = []
    for item in payload["materials"]:
        filename = item["path"]
        if not re.fullmatch(r"material-[0-9]+", filename):
            raise ToolError("invalid_input")
        materials.append((item["name"], Path(filename).read_bytes(), item["detail"]))
    config = Settings(_env_file=None, timezone=request["timezone"], export_max_mb=request["export_mb"])
    binary = package(materials, payload["metadata"], config)
    Path("archive.zip").write_bytes(binary)
    return {"format": "zip"}, [{"path": "archive.zip", "name": "archive.zip", "mime": "application/zip"}], {}


def probe():
    modules = {
        "metadata": bool(shutil.which("exiftool") and shutil.which("file")),
        "ocr": bool(shutil.which("tesseract") and importlib.util.find_spec("PIL")),
        "qr": bool(importlib.util.find_spec("zxingcpp")),
        "page": bool(importlib.util.find_spec("trafilatura")),
        "passport": bool(shutil.which("whatweb")),
        "domains": bool(importlib.util.find_spec("dnstwist")),
    }
    if modules["ocr"]:
        languages = cli(["tesseract", "--list-langs"], 8192).decode().splitlines()
        modules["ocr"] = "rus" in languages and "eng" in languages
    return (
        {
            "available": modules,
            "sandbox": "landlock+seccomp",
            "omp_thread_limit": os.environ["OMP_THREAD_LIMIT"],
        },
        [],
        {},
    )


def main():
    logging.disable(logging.CRITICAL)
    request = json.loads(sys.stdin.readline())
    import resource

    memory = request["memory_mb"] * 1024**2
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_FSIZE, (request["scratch_bytes"], request["scratch_bytes"]))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_CPU, (request["cpu_seconds"], request["cpu_seconds"] + 1))
    try:
        isolate(os.getcwd())
        action = request["module"]
        functions = {
            "metadata": metadata,
            "ocr": ocr,
            "qr": qr,
            "page": page,
            "passport": technologies,
            "domains": domains,
            "report": report,
            "package": archive,
        }
        if action == "probe":
            data, outputs, versions = probe()
        else:
            data, outputs, versions = functions[action](Path("input.bin"), request)
        result = {
            "ok": True,
            "data": data,
            "files": outputs,
            "versions": versions,
            "schema_version": "echo.module/1",
            "sandbox": "landlock+seccomp",
        }
        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded.encode()) > request["output_bytes"]:
            raise ToolError("output_limit")
        print(encoded)
    except Exception as error:
        code = (
            str(error)
            if isinstance(error, ToolError)
            else ("sandbox_unavailable" if type(error).__name__ == "SandboxUnavailable" else "tool_failed")
        )
        print(json.dumps({"ok": False, "error": code}))
        sys.exit(1)


if __name__ == "__main__":
    main()
