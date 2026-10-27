"""Lead import from sheets and CSV files. Fictional data only."""

import asyncio
import os
import tempfile

import pytest

from harvey.collectors import leads_import as li
from harvey.config import ComplianceConfig
from harvey.integrations import google_drive as gd
from harvey.state import StateManager


def _run(coro):
    return asyncio.run(coro)


async def _state(tmp):
    sm = StateManager(os.path.join(tmp, "leads.db"))
    await sm.init_db()
    return sm


async def _verified(email, env):
    return "verified"


class _Config:
    """Just enough config for the do-not-contact list."""

    compliance = ComplianceConfig(do_not_contact=["blocked.example"])
    _source_path = None


SHEET = [
    ["Daily leads"],                      # a title row above the header
    [],
    ["Full Name", "E-mail Address", "Company Name", "Website URL", "Job Title", "Notes"],
    ["Ada Example", "ada@acme.example", "Acme Roofing", "https://www.acme.example/", "Owner", "Asked for a quote"],
    ["Bo Sample", "bo@freemail.example", "Bo's Bakery", "", "Founder", ""],
    ["", "", "Widget Co", "widget.example", "", ""],
    ["Cy Nobody", "", "No Site Ltd", "", "", ""],
    ["Di Blocked", "di@blocked.example", "Blocked Inc", "", "", ""],
    ["", "", "", "", "", ""],
]


def test_headers_are_matched_loosely():
    cols = li.map_headers(["E-mail", "First Name", "Surname", "Company", "Website", "Phone #"])
    assert cols == {"email": 0, "first_name": 1, "last_name": 2,
                    "company": 3, "website": 4, "phone": 5}


@pytest.fixture(autouse=True)
def _fictional_free_mail(monkeypatch):
    """A made-up shared mailbox provider stands in for the real ones."""
    monkeypatch.setattr(li, "FREE_MAIL_DOMAINS", li.FREE_MAIL_DOMAINS | {"freemail.example"})


def test_rows_to_leads_finds_header_below_title_rows():
    leads, skipped = li.rows_to_leads(SHEET, origin="sheet")
    assert skipped == 1                    # Cy: no email, no website
    by_company = {lead.company: lead for lead in leads}
    ada = by_company["Acme Roofing"]
    assert (ada.first_name, ada.last_name, ada.email, ada.domain) == (
        "Ada", "Example", "ada@acme.example", "acme.example")
    # A free-mail address is never taken as the company's domain.
    assert by_company["Bo's Bakery"].domain == ""
    assert by_company["Widget Co"].domain == "widget.example"
    assert by_company["Widget Co"].email == ""


def test_no_header_means_no_leads():
    assert li.rows_to_leads([["a", "b"], ["c", "d"]]) == ([], 0)


def test_clean_domain():
    assert li.clean_domain("HTTPS://www.Acme.example/contact?x=1") == "acme.example"
    assert li.clean_domain("not a domain") == ""


def test_import_adds_verifies_and_dedups():
    with tempfile.TemporaryDirectory() as tmp:
        async def go():
            sm = await _state(tmp)
            leads, _ = li.rows_to_leads(SHEET, origin="sheet")
            first = await li.import_leads(sm, leads, config=_Config(), verify=_verified)
            assert first.new_prospects == 2          # Ada, Bo
            assert first.sendable == 2
            assert first.blocked == 1                # Di
            assert first.new_companies == 2          # acme.example, widget.example
            ada = await sm.get_prospect_by_email("ada@acme.example")
            assert ada.email_status == "verified" and ada.source == li.SOURCE
            assert ada.company_id
            assert ada.personalization_notes == "Asked for a quote"
            assert await sm.company_exists("widget.example")

            # The same sheet again tomorrow: nothing new.
            again = await li.import_leads(sm, leads, config=_Config(), verify=_verified)
            assert again.new_prospects == 0 and again.already_known == 2
        _run(go())


def test_unverifiable_email_is_kept_but_not_sendable():
    with tempfile.TemporaryDirectory() as tmp:
        async def go():
            sm = await _state(tmp)

            async def unsure(email, env):
                return None

            leads, _ = li.rows_to_leads(SHEET[2:4])
            report = await li.import_leads(sm, leads, verify=unsure)
            assert report.new_prospects == 1 and report.sendable == 0
            assert (await sm.get_prospect_by_email("ada@acme.example")).email_status == "guess"
            assert await sm.count_writable_prospects() == 0
        _run(go())


def test_local_csv_file_and_saved_sources(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "leads.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("﻿Email,First Name,Last Name,Company\n"
                    "eve@example.org,Eve,Example,Example Org\n")

        async def go():
            sm = await _state(tmp)
            assert not await li.sync_due(sm)         # nothing saved yet
            await li.save_sources(sm, [path, path])
            assert await li.saved_sources(sm) == [path]
            assert await li.sync_due(sm)
            report = await li.sync_saved(sm, verify=_verified)
            assert report.new_prospects == 1 and not report.errors
            assert not await li.sync_due(sm)         # just synced
        _run(go())


def test_unreadable_source_is_reported_not_raised():
    with tempfile.TemporaryDirectory() as tmp:
        async def go():
            sm = await _state(tmp)
            report = await li.import_source(sm, os.path.join(tmp, "missing.csv"))
            assert report.errors and report.new_prospects == 0
        _run(go())


@pytest.mark.parametrize("url,expected", [
    ("https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOp/edit#gid=42",
     ("sheet", "1AbCdEfGhIjKlMnOp", "42")),
    ("https://drive.google.com/drive/folders/1FolderIdAbCdEf?usp=sharing",
     ("folder", "1FolderIdAbCdEf", "")),
    ("https://drive.google.com/file/d/1FileIdAbCdEfGh/view",
     ("file", "1FileIdAbCdEfGh", "")),
    ("https://example.com/leads.csv", None),
])
def test_parse_google_url(url, expected):
    assert gd.parse_google_url(url) == expected


def test_private_sheet_without_sign_in_says_how(monkeypatch):
    monkeypatch.setattr(gd, "is_connected", lambda: False)

    async def html(url):
        raise gd.DriveError("that sheet isn't public. Run 'harvey leads connect'")

    monkeypatch.setattr(gd, "fetch_public_csv", html)
    with tempfile.TemporaryDirectory() as tmp:
        async def go():
            sm = await _state(tmp)
            report = await li.import_source(
                sm, "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOp/edit")
            assert "harvey leads connect" in report.errors[0]
        _run(go())


def test_drive_client_credentials_fall_back_to_gmail_pair():
    class Env:
        google_client_id = ""
        google_client_secret = ""
        gmail_client_id = "gid"
        gmail_client_secret = "gsecret"

    assert gd.client_credentials(Env()) == ("gid", "gsecret")
    Env.google_client_id, Env.google_client_secret = "id", "secret"
    assert gd.client_credentials(Env()) == ("id", "secret")
