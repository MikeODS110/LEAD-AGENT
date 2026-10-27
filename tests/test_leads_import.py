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
            f.write("\ufeffEmail,First Name,Last Name,Company\n"
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


def _xlsx(rows_by_tab: dict[str, list[list[str]]]) -> bytes:
    """A minimal real .xlsx: shared strings for text, numbers as values."""
    import io
    import zipfile
    from xml.sax.saxutils import escape

    strings: list[str] = []
    sheets = []
    for rows in rows_by_tab.values():
        xml_rows = []
        for r, row in enumerate(rows, start=1):
            cells = []
            for c, value in enumerate(row):
                ref = f"{chr(65 + c)}{r}"
                if value == "":
                    continue
                if value.replace(".", "").isdigit():
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    strings.append(value)
                    cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
            xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
        sheets.append('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                      f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    tabs = "".join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                   for i, n in enumerate(rows_by_tab))
    rels = "".join(f'<Relationship Id="rId{i + 1}" Target="worksheets/sheet{i + 1}.xml" '
                   'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
                   for i in range(len(rows_by_tab)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>{tabs}</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{main}">'
                   + "".join(f"<si><t>{escape(s)}</t></si>" for s in strings) + "</sst>")
        for i, xml in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", xml)
    return buf.getvalue()


def test_parse_xlsx_reads_every_tab_and_keeps_gaps():
    data = _xlsx({
        "Monday": [["Email", "Name", "Phone"], ["ada@acme.example", "Ada Example", "5550100"]],
        "Tuesday": [["Email", "", "Company"], ["bo@widget.example", "", "Widget Co"]],
    })
    tables = gd.parse_xlsx(data)
    assert [name for name, _ in tables] == ["Monday", "Tuesday"]
    assert tables[0][1][1] == ["ada@acme.example", "Ada Example", "5550100"]
    assert tables[1][1][1] == ["bo@widget.example", "", "Widget Co"]


def test_parse_xlsx_rejects_other_files():
    with pytest.raises(gd.DriveError):
        gd.parse_xlsx(b"not a zip")


def test_local_xlsx_file_imports():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "leads.xlsx")
        with open(path, "wb") as f:
            f.write(_xlsx({"Leads": [["Email", "First Name", "Company"],
                                     ["eve@example.org", "Eve", "Example Org"]]}))

        async def go():
            sm = await _state(tmp)
            report = await li.import_source(sm, path, verify=_verified)
            assert report.new_prospects == 1 and not report.errors
        _run(go())


def test_drive_file_link_picks_the_right_reader(monkeypatch):
    data = _xlsx({"Leads": [["Email"], ["ada@acme.example"]]})
    monkeypatch.setattr(gd, "is_connected", lambda: True)

    async def info(self, file_id):
        return {"id": file_id, "name": "daily leads.xlsx", "mimeType": gd.XLSX_MIME}

    async def download(self, file_id):
        return data

    monkeypatch.setattr(gd.DriveClient, "file_info", info)
    monkeypatch.setattr(gd.DriveClient, "download", download)
    with tempfile.TemporaryDirectory() as tmp:
        async def go():
            sm = await _state(tmp)
            report = await li.import_source(
                sm, "https://drive.google.com/file/d/1FileIdAbCdEfGh/view?usp=drivesdk",
                verify=_verified)
            assert report.new_prospects == 1 and not report.errors
        _run(go())


def test_old_xls_says_what_to_do():
    async def go():
        client = gd.DriveClient(object())
        with pytest.raises(gd.DriveError, match=".xlsx"):
            await client.file_tables({"id": "x", "name": "leads.xls",
                                      "mimeType": gd.OLD_XLS_MIME})
    _run(go())


def test_name_source_parses():
    assert gd.parse_google_url("name: domain data") == ("name", "domain data", "")
    assert gd.parse_google_url("name:") is None


def test_name_search_imports_every_matching_file(monkeypatch):
    monkeypatch.setattr(gd, "is_connected", lambda: True)
    asked = {}

    async def fake_get(self, url, params=None):
        asked.update(params or {})

        class Resp:
            def json(self):
                return {"files": [
                    {"id": "f1", "name": "27 Oct domain data.xlsx", "mimeType": gd.XLSX_MIME},
                    {"id": "f2", "name": "28 Oct domain data.xlsx", "mimeType": gd.XLSX_MIME},
                ]}
        return Resp()

    books = {"f1": _xlsx({"Leads": [["Email"], ["ada@acme.example"]]}),
             "f2": _xlsx({"Leads": [["Email"], ["ada@acme.example"], ["bo@widget.example"]]})}

    async def download(self, file_id):
        return books[file_id]

    monkeypatch.setattr(gd.DriveClient, "_get", fake_get)
    monkeypatch.setattr(gd.DriveClient, "download", download)
    with tempfile.TemporaryDirectory() as tmp:
        async def go():
            sm = await _state(tmp)
            report = await li.import_source(sm, "name:it's domain data", verify=_verified)
            assert report.new_prospects == 2 and report.already_known == 1
        _run(go())
    # Quotes are escaped, and shared-with-me / shared drives are searched.
    assert "name contains 'it\\'s' and name contains 'domain' and name contains 'data'" in asked["q"]
    assert asked["corpora"] == "allDrives"


def test_name_search_with_no_match_says_so(monkeypatch):
    monkeypatch.setattr(gd, "is_connected", lambda: True)

    async def none(self, name, limit=30):
        return []

    monkeypatch.setattr(gd.DriveClient, "search_files", none)
    monkeypatch.setattr(gd.DriveClient, "search_any", none)
    with tempfile.TemporaryDirectory() as tmp:
        async def go():
            sm = await _state(tmp)
            report = await li.import_source(sm, "name:domain data")
            assert "no file whose name contains 'domain data'" in report.errors[0]
        _run(go())


def test_name_search_names_files_it_cannot_read(monkeypatch):
    monkeypatch.setattr(gd, "is_connected", lambda: True)

    async def none(self, name, limit=30):
        return []

    async def slides(self, name, limit=5):
        return [{"id": "s1", "name": "domain data deck",
                 "mimeType": "application/vnd.google-apps.presentation"}]

    monkeypatch.setattr(gd.DriveClient, "search_files", none)
    monkeypatch.setattr(gd.DriveClient, "search_any", slides)
    with tempfile.TemporaryDirectory() as tmp:
        async def go():
            sm = await _state(tmp)
            report = await li.import_source(sm, "name:domain data")
            assert "'domain data deck' (Google presentation)" in report.errors[0]
        _run(go())


def test_doc_tables_are_read_as_rows():
    html = ("<html><body><p>Leads</p><table>"
            "<tr><td>Name</td><td>Email</td><td>Website</td></tr>"
            "<tr><td>Ada Example</td><td>ada@acme.example</td><td>acme.example</td></tr>"
            "</table></body></html>")
    tables = gd.parse_doc_html(html)
    leads, _ = li.rows_to_leads(tables[0][1])
    assert [(lead.first_name, lead.email, lead.domain) for lead in leads] == [
        ("Ada", "ada@acme.example", "acme.example")]


def test_doc_lines_without_a_table():
    html = ("<html><body><p>October domains</p>"
            "<p>acme.example - ada@acme.example - roofing</p>"
            "<p>widget.example</p><p>nothing here</p></body></html>")
    tables = gd.parse_doc_html(html)
    leads, _ = li.rows_to_leads(tables[0][1])
    assert [(lead.email, lead.domain) for lead in leads] == [
        ("ada@acme.example", "acme.example"), ("", "widget.example")]
    assert "roofing" in leads[0].notes


def test_drive_doc_file_is_exported_as_html(monkeypatch):
    monkeypatch.setattr(gd, "is_connected", lambda: True)
    asked = []

    async def fake_get(self, url, params=None):
        asked.append((url, params))

        class Resp:
            content = (b"<table><tr><td>Email</td></tr>"
                       b"<tr><td>ada@acme.example</td></tr></table>")

            def json(self):
                return {"id": "d1", "name": "Oct domain data", "mimeType": gd.DOC_MIME}
        return Resp()

    monkeypatch.setattr(gd.DriveClient, "_get", fake_get)
    with tempfile.TemporaryDirectory() as tmp:
        async def go():
            sm = await _state(tmp)
            report = await li.import_source(
                sm, "https://docs.google.com/document/d/1DocIdAbCdEfGhIj/edit", verify=_verified)
            assert report.new_prospects == 1, report.errors
        _run(go())
    assert asked[0][0].endswith("/files/1DocIdAbCdEfGhIj")
    assert asked[-1][0].endswith("/files/d1/export")
    assert asked[-1][1] == {"mimeType": "text/html"}
