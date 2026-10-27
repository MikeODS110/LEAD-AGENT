"""Google Drive, read-only: where a team's daily lead sheets live.

Separate from the Gmail provider on purpose. Someone who sends from
Microsoft 365 still keeps their lead lists in Google Sheets, so this signs in
with one scope only, ``drive.readonly``: Harvey can read sheets and CSV files,
never change them, and gets no access to the mailbox.

Setup (one-time):
1. Google Cloud Console → create a project → enable the Google Drive API and
   the Google Sheets API.
2. OAuth consent screen (internal, or testing with yourself as a test user)
   → create an OAuth client of type "Desktop app". Put its id/secret in .env
   as GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (the GMAIL_* pair works too).
3. Run ``harvey leads connect`` — opens a browser, stores the refresh token
   in data/google_drive_token.json.

A sheet shared as "anyone with the link can view" can be read without
signing in at all (see ``public_csv_url``).
"""

import csv
import io
import json
import logging
import re
import time
import urllib.parse
import webbrowser

import httpx

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.google_drive")

TOKEN_FILE = PROJECT_ROOT / "data" / "google_drive_token.json"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_API = "https://www.googleapis.com/drive/v3"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
LOOPBACK_PORT = 8767
TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
# A lead file bigger than this is not a lead file.
MAX_DOWNLOAD_BYTES = 20_000_000
MAX_FOLDER_FILES = 200
# Newest files a name search reads each time.
MAX_SEARCH_FILES = 30

SHEET_MIME = "application/vnd.google-apps.spreadsheet"
CSV_MIMES = ("text/csv", "text/plain", "application/csv")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OLD_XLS_MIME = "application/vnd.ms-excel"
DOC_MIME = "application/vnd.google-apps.document"
LEAD_MIMES = (SHEET_MIME, XLSX_MIME, DOC_MIME, *CSV_MIMES)

_SHEET_RE = re.compile(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{10,})")
_FOLDER_RE = re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{10,})")
_FILE_RE = re.compile(r"(?:drive\.google\.com/(?:file/d/|open\?id=)|"
                      r"docs\.google\.com/document/(?:u/\d+/)?d/)([A-Za-z0-9_-]{10,})")


class DriveError(RuntimeError):
    pass


def client_credentials(env) -> tuple[str, str]:
    """(client id, secret): GOOGLE_* first, else the Gmail pair."""
    cid = getattr(env, "google_client_id", "") or getattr(env, "gmail_client_id", "")
    secret = (getattr(env, "google_client_secret", "")
              or getattr(env, "gmail_client_secret", ""))
    return cid, secret


NAME_PREFIX = "name:"


def parse_google_url(text: str) -> tuple[str, str, str] | None:
    """("sheet" | "folder" | "file" | "name", id or name, gid) for a Google
    link or a ``name:`` search, else None."""
    text = (text or "").strip()
    if text.lower().startswith(NAME_PREFIX):
        name = text[len(NAME_PREFIX):].strip()
        return ("name", name, "") if name else None
    if m := _SHEET_RE.search(text):
        query = urllib.parse.urlparse(text)
        params = urllib.parse.parse_qs(query.query)
        frag = urllib.parse.parse_qs(query.fragment)
        gid = (params.get("gid") or frag.get("gid") or [""])[0]
        return "sheet", m.group(1), gid
    if m := _FOLDER_RE.search(text):
        return "folder", m.group(1), ""
    if m := _FILE_RE.search(text):
        return "file", m.group(1), ""
    return None


def public_csv_url(sheet_id: str, gid: str = "") -> str:
    """CSV export of one tab of a link-shared sheet (no sign-in needed)."""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    return url + (f"&gid={gid}" if gid else "")


def _load_token() -> dict | None:
    try:
        if TOKEN_FILE.is_file():
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("refresh_token"):
                return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read Google Drive token file: {e}")
    return None


def _save_token(data: dict):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def is_connected() -> bool:
    return _load_token() is not None


def parse_csv(text: str) -> list[list[str]]:
    return [row for row in csv.reader(io.StringIO(text.lstrip("\ufeff")))]


_LINE_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LINE_DOMAIN_RE = re.compile(r"\b(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+[a-z]{2,})\b", re.I)


def parse_doc_html(html: str) -> list[tuple[str, list[list[str]]]]:
    """Leads in a Google Doc. Every table becomes rows as-is; with no
    table, each line naming an email or a website becomes a row of
    (Email, Website, Notes), the line itself kept as the note."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for i, table in enumerate(soup.find_all("table"), start=1):
        rows = [[cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"])]
                for tr in table.find_all("tr")]
        tables.append((f"table {i}", rows))
    if tables:
        return tables
    rows = [["Email", "Website", "Notes"]]
    for line in soup.get_text("\n").splitlines():
        line = line.strip()
        if not line:
            continue
        email_match = _LINE_EMAIL_RE.search(line)
        email = email_match.group(0) if email_match else ""
        rest = line.replace(email, " ") if email else line
        domain_match = _LINE_DOMAIN_RE.search(rest)
        domain = domain_match.group(1) if domain_match else ""
        if email or domain:
            rows.append([email, domain, line[:500]])
    return [("text", rows)] if len(rows) > 1 else []


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# Uncompressed size cap for any one part of a workbook (zip bombs).
MAX_XLSX_PART_BYTES = 50_000_000


def _column_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - 64)
    return max(index - 1, 0)


def parse_xlsx(data: bytes) -> list[tuple[str, list[list[str]]]]:
    """Every tab of an Excel (.xlsx) workbook as (tab name, rows): text
    only, standard library only. Raises DriveError for anything else."""
    import posixpath
    import zipfile
    from xml.etree import ElementTree as ET

    try:
        book = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DriveError("that file isn't an Excel .xlsx workbook. Save it as .xlsx "
                         "or CSV, or open it in Google Sheets.")

    def read(name: str) -> bytes:
        info = book.getinfo(name)
        if info.file_size > MAX_XLSX_PART_BYTES:
            raise DriveError("that workbook is too large to be a lead list.")
        return book.read(name)

    names = set(book.namelist())
    try:
        workbook = ET.fromstring(read("xl/workbook.xml"))
        rels = ET.fromstring(read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError):
        raise DriveError("that .xlsx workbook couldn't be read.")
    targets = {r.get("Id"): r.get("Target", "") for r in rels}

    text_tag = f"{{{_NS['m']}}}t"
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(read("xl/sharedStrings.xml")).findall("m:si", _NS):
            shared.append("".join(t.text or "" for t in si.iter(text_tag)))

    tables = []
    for sheet in workbook.findall("m:sheets/m:sheet", _NS):
        target = targets.get(sheet.get(f"{{{_REL_NS}}}id"), "")
        path = (target.lstrip("/") if target.startswith("/")
                else posixpath.normpath(posixpath.join("xl", target)))
        if path not in names:
            continue
        rows: list[list[str]] = []
        for row in ET.fromstring(read(path)).iter(f"{{{_NS['m']}}}row"):
            cells: dict[int, str] = {}
            for i, c in enumerate(row.findall("m:c", _NS)):
                kind = c.get("t", "")
                v = c.find("m:v", _NS)
                if kind == "inlineStr":
                    text = "".join(t.text or "" for t in c.iter(text_tag))
                elif kind == "s":
                    try:
                        text = shared[int(v.text or "")] if v is not None else ""
                    except (ValueError, IndexError):
                        text = ""
                else:
                    text = (v.text or "") if v is not None else ""
                    if kind == "" and text.endswith(".0"):
                        text = text[:-2]          # 5550100.0 → 5550100
                ref = c.get("r", "")
                cells[_column_index(ref) if ref else i] = text
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
        tables.append((sheet.get("name", ""), rows))
    return tables


class DriveClient:
    """Read-only access to sheets and CSV files the signed-in user can see."""

    def __init__(self, env):
        self.client_id, self.client_secret = client_credentials(env)
        self._access_token = ""
        self._access_expires = 0.0

    async def _token(self) -> str:
        if self._access_token and time.time() < self._access_expires - 60:
            return self._access_token
        stored = _load_token()
        if not stored:
            raise DriveError("not connected to Google Drive. Run 'harvey leads connect'.")
        if not self.client_id or not self.client_secret:
            raise DriveError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET aren't set in .env.")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(TOKEN_URL, data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": stored["refresh_token"],
                    "grant_type": "refresh_token",
                })
        except httpx.HTTPError as e:
            raise DriveError(f"couldn't reach Google ({type(e).__name__}).")
        if resp.status_code in (400, 401, 403):
            raise DriveError(f"Google rejected the saved Drive sign-in ({resp.status_code}). "
                             "Run 'harvey leads connect' again.")
        if resp.status_code != 200:
            raise DriveError(f"Google token refresh failed (HTTP {resp.status_code}).")
        try:
            data = resp.json()
        except ValueError:
            data = None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise DriveError("Google's token answer had no access token.")
        try:
            expires_in = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600.0
        self._access_token = token
        self._access_expires = time.time() + expires_in
        return token

    async def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        token = await self._token()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(url, params=params,
                                        headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            raise DriveError(f"couldn't reach Google ({type(e).__name__}).")
        if resp.status_code == 404:
            raise DriveError("Google says that file or folder doesn't exist, or the "
                             "signed-in account can't see it.")
        if resp.status_code == 403:
            raise DriveError("Google refused access (403). Check the Drive and Sheets "
                             "APIs are enabled, and that the signed-in account can open it.")
        if resp.status_code != 200:
            raise DriveError(f"Google answered HTTP {resp.status_code}.")
        if len(resp.content) > MAX_DOWNLOAD_BYTES:
            raise DriveError("that file is too large to be a lead list.")
        return resp

    async def sheet_tables(self, sheet_id: str, gid: str = "") -> list[tuple[str, list[list[str]]]]:
        """Every tab of a spreadsheet (or just ``gid``), as (label, rows)."""
        resp = await self._get(f"{SHEETS_API}/{sheet_id}",
                               {"fields": "properties.title,sheets.properties"})
        meta = resp.json()
        title = (meta.get("properties") or {}).get("title", sheet_id)
        tabs = [s.get("properties") or {} for s in meta.get("sheets") or []]
        if gid:
            tabs = [t for t in tabs if str(t.get("sheetId")) == str(gid)]
        tables = []
        for tab in tabs:
            name = tab.get("title", "")
            if not name:
                continue
            quoted = "'" + name.replace("'", "''") + "'"
            values = await self._get(
                f"{SHEETS_API}/{sheet_id}/values/{urllib.parse.quote(quoted, safe='')}",
                {"valueRenderOption": "FORMATTED_VALUE"},
            )
            rows = values.json().get("values") or []
            tables.append((f"{title} / {name}",
                           [[str(c) for c in row] for row in rows]))
        return tables

    async def file_info(self, file_id: str) -> dict:
        resp = await self._get(f"{DRIVE_API}/files/{file_id}",
                               {"fields": "id,name,mimeType,modifiedTime",
                                "supportsAllDrives": "true"})
        return resp.json()

    async def download(self, file_id: str) -> bytes:
        resp = await self._get(f"{DRIVE_API}/files/{file_id}",
                               {"alt": "media", "supportsAllDrives": "true"})
        return resp.content

    async def csv_file(self, file_id: str) -> list[list[str]]:
        return parse_csv((await self.download(file_id)).decode("utf-8-sig", errors="replace"))

    async def file_tables(self, info: dict) -> list[tuple[str, list[list[str]]]]:
        """(label, rows) for any lead file: a Google Sheet or Doc, Excel or CSV."""
        file_id, name = info["id"], info.get("name", info["id"])
        mime = info.get("mimeType", "")
        if mime == SHEET_MIME:
            return await self.sheet_tables(file_id)
        if mime == DOC_MIME:
            resp = await self._get(f"{DRIVE_API}/files/{file_id}/export",
                                   {"mimeType": "text/html"})
            return [(f"{name} / {label}", rows)
                    for label, rows in parse_doc_html(resp.content.decode("utf-8", errors="replace"))]
        if mime.startswith("application/vnd.google-apps.") or mime == "application/pdf":
            raise DriveError(f"{name} isn't a sheet, Doc, Excel or CSV file, so Harvey "
                             "can't read leads from it.")
        if mime == OLD_XLS_MIME or name.lower().endswith(".xls"):
            raise DriveError(f"{name} is an old-style .xls file. Save it as .xlsx or CSV, "
                             "or open it with Google Sheets, then add that.")
        if mime == XLSX_MIME or name.lower().endswith(".xlsx"):
            return [(f"{name} / {tab}", rows)
                    for tab, rows in parse_xlsx(await self.download(file_id))]
        return [(name, await self.csv_file(file_id))]

    def _name_query(self, name: str) -> str:
        # Drive matches name terms by word prefix, so a phrase is searched
        # word by word: "domain data" finds "Oct 2026 Domain Data".
        words = [w.replace("\\", "\\\\").replace("'", "\\'") for w in name.split()]
        return " and ".join(f"name contains '{w}'" for w in words) + " and trashed = false"

    async def _search(self, q: str, limit: int) -> list[dict]:
        params = {
            "q": q,
            "fields": "files(id,name,mimeType,modifiedTime)",
            "orderBy": "modifiedTime desc",
            "pageSize": str(limit),
            "corpora": "allDrives",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        return ((await self._get(f"{DRIVE_API}/files", params)).json().get("files") or [])[:limit]

    async def search_files(self, name: str, limit: int = MAX_SEARCH_FILES) -> list[dict]:
        """Sheets, Docs, Excel and CSV files whose name contains every word
        of ``name``, newest first: in My Drive, shared with me, and shared
        drives."""
        mimes = " or ".join(f"mimeType = '{m}'" for m in LEAD_MIMES)
        return await self._search(f"{self._name_query(name)} and ({mimes})", limit)

    async def search_any(self, name: str, limit: int = 5) -> list[dict]:
        """Files of any type matching ``name``: to say why none could be read."""
        return await self._search(self._name_query(name), limit)

    async def folder_files(self, folder_id: str) -> list[dict]:
        """Sheets, Excel and CSV files directly in a folder, newest first."""
        mimes = " or ".join(f"mimeType = '{m}'" for m in LEAD_MIMES)
        files: list[dict] = []
        page = ""
        while len(files) < MAX_FOLDER_FILES:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false and ({mimes})",
                "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
                "orderBy": "modifiedTime desc",
                "pageSize": "100",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page:
                params["pageToken"] = page
            data = (await self._get(f"{DRIVE_API}/files", params)).json()
            files.extend(data.get("files") or [])
            page = data.get("nextPageToken") or ""
            if not page:
                break
        return files[:MAX_FOLDER_FILES]


async def fetch_public_csv(url: str) -> list[list[str]]:
    """A CSV anyone can download: a link-shared sheet's export, or any CSV URL."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        raise DriveError(f"couldn't download {url} ({type(e).__name__}).")
    kind = resp.headers.get("content-type", "")
    if resp.status_code != 200 or "text/html" in kind:
        raise DriveError(
            "that sheet isn't public. Run 'harvey leads connect' so Harvey can read "
            "it with your Google account, or share it as 'anyone with the link'.")
    if len(resp.content) > MAX_DOWNLOAD_BYTES:
        raise DriveError("that file is too large to be a lead list.")
    return parse_csv(resp.content.decode("utf-8-sig", errors="replace"))


def run_auth_flow(client_id: str, client_secret: str) -> bool:
    """One-time OAuth (installed-app loopback), Drive read-only. Blocks until
    the browser round-trip completes; stores the refresh token."""
    import http.server
    import secrets
    import threading

    if not client_id or not client_secret:
        print("  Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first.")
        print("  (Google Cloud Console → APIs → Credentials → OAuth client, Desktop app)")
        return False

    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://localhost:{LOOPBACK_PORT}"
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })

    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_response(400); self.end_headers()
                return
            result["code"] = query.get("code", [""])[0]
            result["error"] = query.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2>Harvey can now read your lead sheets.</h2>You can close this tab.")
            done.set()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("localhost", LOOPBACK_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"\n  Opening browser for Google sign-in (read-only Drive access)...\n  {auth_url}\n")
    webbrowser.open(auth_url)
    if not done.wait(timeout=300):
        server.shutdown()
        print("  Timed out waiting for the browser round-trip.")
        return False
    server.shutdown()

    if result.get("error") or not result.get("code"):
        print(f"  Authorization failed: {result.get('error') or 'no code returned'}")
        return False

    resp = httpx.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": result["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, timeout=30)
    if resp.status_code != 200:
        print(f"  Token exchange failed ({resp.status_code}): {resp.text[:200]}")
        return False
    data = resp.json()
    if not data.get("refresh_token"):
        print("  Google returned no refresh token. Remove Harvey from your"
              " account's third-party access list and retry.")
        return False
    _save_token({
        "refresh_token": data["refresh_token"],
        "scope": data.get("scope", SCOPE),
        "obtained_at": int(time.time()),
    })
    print(f"  ✓ Google Drive connected (read-only). Token stored at {TOKEN_FILE}")
    return True
