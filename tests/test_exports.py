import asyncio
import csv
import io
import json
import os
import shutil
import subprocess

import pytest
from pypdf import PdfReader

from echo.catalog import seed_catalog
from echo.export import Reports, csv_cell, encrypt, fingerprint
from echo.services import Denied


@pytest.fixture
async def report(jobs, config, observation, sessions):
    await seed_catalog(sessions)
    await jobs.user(1)
    j = await jobs.create(1, "echo_fixture_user", ["sherlock"], "curated", False, "report")
    j = await jobs.claim()
    observation.site_name = '<script>alert("XSS")</script>'
    await jobs.save(j.id, j.lease, observation, "github")
    c = (await jobs.cards(1, j.id))[0]
    await jobs.annotate(
        1, j.id, c["key"], note='=HYPERLINK("https://evil.test") <img src=x onerror=alert(1)>\nКириллица'
    )
    await jobs.finish(j.id, j.lease, {})
    return Reports(jobs, config), j


async def test_html_is_standalone_and_escapes(report):
    reports, j = report
    data, name = await reports.build(1, j.id, "html")
    html = data.decode()
    assert name.endswith(".html")
    assert "<script>alert(" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html and "&lt;img" in html
    assert 'src="https://' not in html and "cdn." not in html
    assert 'data-match="1"' in html and "Content-Security-Policy" in html
    assert "https://github.com/about" in html


async def test_json_has_versioned_schema_and_no_secrets(report):
    reports, j = report
    content, _ = await reports.build(1, j.id, "json")
    data = json.loads(content)
    assert data["schema_version"] == "echo.report/1"
    assert data["results"][0]["observations"][0]["version"] == "0.16.2"
    for secret in ("database_url", "bot_token", "ai_key", "owner_id", "lease"):
        assert secret not in data
    with pytest.raises(Denied):
        await reports.build(2, j.id, "json")


async def test_csv_injection_and_columns(report):
    reports, j = report
    content, _ = await reports.build(1, j.id, "csv")
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    assert rows[0]["note"].startswith("'=HYPERLINK")
    assert rows[0]["raw_status"] == "Claimed"
    assert rows[0]["sources"] == "https://github.com/about"
    assert rows[0]["timezone"] == "Europe/Moscow"


@pytest.mark.parametrize("text", ["=1+1", " +SUM(1)", "-2", "@SUM(1)", "\t=1", "\n=1", "\ufeff=1"])
def test_formula_guard(text):
    assert csv_cell(text).startswith("'")


async def test_pdf_unicode_layout_and_links(report):
    reports, j = report
    content, _ = await reports.build(1, j.id, "pdf")
    assert content.startswith(b"%PDF")
    reader = PdfReader(io.BytesIO(content))
    text = "".join(page.extract_text() for page in reader.pages)
    assert "Кириллица" in text and "POSSIBLE_MATCH" in text and "<script>" in text
    assert any(page.get("/Annots") for page in reader.pages)


def age_programs():
    age = os.getenv("ECHO_TEST_AGE") or shutil.which("age")
    keygen = os.getenv("ECHO_TEST_AGE_KEYGEN") or shutil.which("age-keygen")
    return age, keygen


async def test_age_real_roundtrip(tmp_path):
    age, keygen = age_programs()
    if not age or not keygen:
        pytest.skip("standard age binary required")
    private = tmp_path / "identity.txt"
    await asyncio.to_thread(subprocess.run, [keygen, "-o", str(private)], check=True, capture_output=True)
    result = await asyncio.to_thread(
        subprocess.run, [keygen, "-y", str(private)], check=True, capture_output=True, text=True
    )
    public = result.stdout.strip()
    original = "Echo: проверка шифрования".encode()
    encrypted = await encrypt(original, public, age)
    assert original not in encrypted and encrypted.startswith(b"age-encryption.org/v1")
    result = await asyncio.to_thread(
        subprocess.run,
        [age, "--decrypt", "--identity", str(private)],
        input=encrypted,
        capture_output=True,
        check=True,
    )
    decrypted = result.stdout
    assert decrypted == original and fingerprint(public).startswith("SHA256:")
    private.unlink()


async def test_export_after_deletion(report, jobs):
    reports, j = report
    await jobs.delete_user(1)
    with pytest.raises(Denied):
        await reports.build(1, j.id, "pdf")
