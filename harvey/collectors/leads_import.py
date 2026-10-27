"""IMPORT — leads someone else already found: a Google Sheet, an Excel or CSV
file (on Drive or this computer), or a Drive folder of them.

Plenty of teams get a fresh lead sheet every day. Importing it puts those
people into the same pipeline as anyone Harvey found itself: the website is
profiled, the email is verified before anything is written, and the Writer
and Sender pick them up as usual. Nothing here sends anything.

Columns are matched by header name, loosely ("E-mail", "Work Email",
"Company Name", "Website URL" all work), so a sheet doesn't have to be
reformatted for Harvey. A row needs an email, or a company website so Harvey
can find the right person there itself. Everything else is optional.

Re-importing is safe: a person already on file (same email, or same name at
the same company) is skipped, and so is anyone on the do-not-contact list or
who opted out.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import FREE_MAIL_DOMAINS

logger = logging.getLogger("harvey.leads_import")

SOURCE = "lead_sheet"
SOURCES_KEY = "lead_sources"
SYNCED_AT_KEY = "lead_sources_synced_at"
SYNC_EVERY_HOURS = 6
# Addresses verified at once. Verifiers rate-limit, and a daily sheet is
# tens of rows, not thousands.
VERIFY_CONCURRENCY = 4

# Header aliases, compared after lowercasing and dropping everything but
# letters and digits ("E-mail Address" → "emailaddress").
HEADER_ALIASES = {
    "email": ("email", "emailaddress", "workemail", "businessemail", "mail",
              "contactemail", "emailid", "primaryemail"),
    "first_name": ("firstname", "first", "fname", "givenname", "forename"),
    "last_name": ("lastname", "last", "lname", "surname", "familyname"),
    "full_name": ("name", "fullname", "contactname", "contact", "person",
                  "leadname", "contactperson"),
    "company": ("company", "companyname", "business", "businessname",
                "organization", "organisation", "account", "accountname", "firm"),
    "website": ("website", "websiteurl", "url", "domain", "companywebsite",
                "companydomain", "site", "web", "homepage"),
    "title": ("title", "jobtitle", "position", "role", "designation"),
    "phone": ("phone", "phonenumber", "mobile", "telephone", "tel", "cell",
              "contactnumber", "phoneno"),
    "linkedin_url": ("linkedin", "linkedinurl", "linkedinprofile", "personlinkedinurl"),
    "industry": ("industry", "sector", "vertical", "category", "niche"),
    "location": ("location", "city", "address", "country", "state", "region"),
    "notes": ("notes", "note", "comments", "comment", "description", "details",
              "remarks", "info"),
}

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


def map_headers(header: list[str]) -> dict[str, int]:
    """{field: column index} for the columns this header row names."""
    lookup = {alias: name for name, aliases in HEADER_ALIASES.items() for alias in aliases}
    found: dict[str, int] = {}
    for i, raw in enumerate(header):
        name = lookup.get(_key(raw))
        # First matching column wins ("Email" before "Email 2").
        if name and name not in found:
            found[name] = i
    return found


def find_header_row(rows: list[list[str]], scan: int = 10) -> int:
    """Index of the header row: the first of the top rows naming an email
    or website column. Sheets often carry a title row or two above it."""
    for i, row in enumerate(rows[:scan]):
        fields = map_headers(row)
        if "email" in fields or "website" in fields:
            return i
    return -1


def clean_domain(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^[a-z]+://", "", value)
    value = value.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
    if value.startswith("www."):
        value = value[4:]
    return value if "." in value and " " not in value else ""


def split_name(full: str) -> tuple[str, str]:
    parts = (full or "").replace(",", " ").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


@dataclass
class Lead:
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""
    website: str = ""
    title: str = ""
    phone: str = ""
    linkedin_url: str = ""
    industry: str = ""
    location: str = ""
    notes: str = ""
    origin: str = ""


def rows_to_leads(rows: list[list[str]], origin: str = "") -> tuple[list[Lead], int]:
    """(leads, rows skipped for having neither an email nor a website)."""
    start = find_header_row(rows)
    if start < 0:
        return [], 0
    cols = map_headers(rows[start])

    def cell(row, name):
        i = cols.get(name)
        return row[i].strip() if i is not None and i < len(row) and row[i] else ""

    leads, skipped = [], 0
    for row in rows[start + 1:]:
        if not any((c or "").strip() for c in row):
            continue
        email = cell(row, "email").lower().removeprefix("mailto:")
        if email and not _EMAIL_RE.match(email):
            email = ""
        website = cell(row, "website")
        domain = clean_domain(website)
        if not domain and email:
            email_domain = email.rpartition("@")[2]
            if email_domain not in FREE_MAIL_DOMAINS:
                domain = email_domain
        if not email and not domain:
            skipped += 1
            continue
        first, last = cell(row, "first_name"), cell(row, "last_name")
        if not first and not last:
            first, last = split_name(cell(row, "full_name"))
        leads.append(Lead(
            email=email, first_name=first, last_name=last,
            company=cell(row, "company"), domain=domain,
            website=website if domain else "",
            title=cell(row, "title"), phone=cell(row, "phone"),
            linkedin_url=cell(row, "linkedin_url"), industry=cell(row, "industry"),
            location=cell(row, "location"), notes=cell(row, "notes"),
            origin=origin,
        ))
    return leads, skipped


@dataclass
class ImportReport:
    rows: int = 0
    new_prospects: int = 0
    sendable: int = 0
    new_companies: int = 0
    already_known: int = 0
    no_contact: int = 0
    blocked: int = 0
    tables: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "ImportReport"):
        for name in ("rows", "new_prospects", "sendable", "new_companies",
                     "already_known", "no_contact", "blocked", "tables"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.errors.extend(other.errors)

    def summary_line(self) -> str:
        def n(count, one, many):
            return f"{count} {one if count == 1 else many}"

        parts = [n(self.new_prospects, "new contact", "new contacts")]
        if self.new_prospects:
            parts[0] += f" ({self.sendable} with a verified or catch-all email)"
        if self.new_companies:
            parts.append(n(self.new_companies, "new company", "new companies"))
        if self.already_known:
            parts.append(f"{self.already_known} already known")
        if self.blocked:
            parts.append(f"{self.blocked} on the do-not-contact list")
        if self.no_contact:
            parts.append(n(self.no_contact, "row", "rows") + " with no email or website")
        return ", ".join(parts)


async def read_source(source: str, env) -> list[tuple[str, list[list[str]]]]:
    """(label, rows) for every table a source holds. Raises DriveError."""
    from harvey.integrations import google_drive as gd

    google = gd.parse_google_url(source)
    if google:
        kind, file_id, gid = google
        if not gd.is_connected():
            if kind == "sheet":
                return [(source, await gd.fetch_public_csv(gd.public_csv_url(file_id, gid)))]
            raise gd.DriveError("reading a Drive folder, file or name search needs "
                                "'harvey leads connect' first.")
        drive = gd.DriveClient(env)
        if kind == "sheet":
            return await drive.sheet_tables(file_id, gid)
        if kind == "file":
            return await drive.file_tables(await drive.file_info(file_id))
        files = (await drive.search_files(file_id) if kind == "name"
                 else await drive.folder_files(file_id))
        if kind == "name" and not files:
            raise gd.DriveError(f"no sheet, Excel or CSV file whose name contains "
                                f"'{file_id}' is visible to the signed-in account.")
        tables = []
        for f in files:
            try:
                tables.extend(await drive.file_tables(f))
            except gd.DriveError as e:
                # One unreadable file doesn't stop the rest of the folder.
                logger.warning(f"lead import: skipped {f.get('name', 'a file')}: {e}")
        return tables
    if re.match(r"^https?://", source, re.I):
        return [(source, await gd.fetch_public_csv(source))]
    path = Path(source).expanduser()
    if not path.is_file():
        raise gd.DriveError(f"{source} isn't a Google link, a CSV URL or a file on this computer.")
    if path.suffix.lower() == ".xlsx":
        return [(f"{path.name} / {tab}", rows) for tab, rows in gd.parse_xlsx(path.read_bytes())]
    return [(path.name, gd.parse_csv(path.read_text(encoding="utf-8-sig", errors="replace")))]


async def import_leads(state, leads: list[Lead], config=None, env=None,
                       verify=None) -> ImportReport:
    """Put leads into the pipeline. ``verify(email, env) -> status`` grades
    each new address (default: Harvey's verifier chain)."""
    from harvey.config import live_do_not_contact
    from harvey.gate import do_not_contact_domain_match, do_not_contact_match

    if verify is None:
        from harvey.integrations.email_finder import verify_published_address as verify
    dnc = live_do_not_contact(config) if config is not None else []
    report = ImportReport(rows=len(leads))
    seen: set[str] = set()
    todo: list[Lead] = []

    for lead in leads:
        if (lead.email and do_not_contact_match(lead.email, dnc)) or (
                lead.domain and do_not_contact_domain_match(lead.domain, dnc)):
            report.blocked += 1
            continue
        ident = lead.email or f"{lead.first_name}|{lead.last_name}|{lead.domain}".lower()
        if ident in seen:
            report.already_known += 1
            continue
        seen.add(ident)
        if lead.email or (lead.first_name and lead.last_name):
            if await state.prospect_exists(
                email=lead.email, linkedin_url=lead.linkedin_url,
                first_name=lead.first_name, last_name=lead.last_name,
                company=lead.company,
            ):
                report.already_known += 1
                continue
        todo.append(lead)

    gate = asyncio.Semaphore(VERIFY_CONCURRENCY)

    async def grade(lead: Lead) -> str:
        if not lead.email:
            return ""
        async with gate:
            try:
                return await verify(lead.email, env) or "guess"
            except Exception as e:
                logger.warning(f"lead import: couldn't verify an address ({type(e).__name__})")
                return "guess"

    statuses = await asyncio.gather(*(grade(lead) for lead in todo))

    for lead, status in zip(todo, statuses):
        company_id = ""
        if lead.domain:
            existed = await state.company_exists(lead.domain)
            company_id = await state.add_company(Company(
                name=lead.company or lead.domain, domain=lead.domain,
                website=lead.website or f"https://{lead.domain}",
                industry=lead.industry, location=lead.location,
                phone="" if lead.email else lead.phone,
                source=SOURCE, source_url=lead.origin[:500],
            ))
            if not existed:
                report.new_companies += 1
        if not lead.email:
            # No address: the company is in, and Scout finds the person there.
            if not lead.domain:
                report.no_contact += 1
            continue
        await state.add_prospect(Prospect(
            company_id=company_id,
            first_name=lead.first_name, last_name=lead.last_name,
            email=lead.email, email_status=status,
            email_verified=status == "verified",
            phone=lead.phone, linkedin_url=lead.linkedin_url, title=lead.title,
            source=SOURCE, source_url=lead.origin[:500],
            personalization_notes=lead.notes[:1000],
            company=lead.company or lead.domain, industry=lead.industry,
        ))
        report.new_prospects += 1
        if status in ("verified", "risky"):
            report.sendable += 1
    return report


async def import_source(state, source: str, config=None, env=None, verify=None) -> ImportReport:
    """Read one source and import every table in it. Never raises."""
    report = ImportReport()
    try:
        tables = await read_source(source, env)
    except Exception as e:
        report.errors.append(f"{source}: {e}")
        return report
    leads: list[Lead] = []
    for label, rows in tables:
        found, skipped = rows_to_leads(rows, origin=label)
        if found or skipped:
            report.tables += 1
        report.no_contact += skipped
        leads.extend(found)
    if not leads and not report.no_contact:
        report.errors.append(f"{source}: no table with an email or website column was found.")
        return report
    report.merge(await import_leads(state, leads, config=config, env=env, verify=verify))
    return report


# ── Saved sources: what `harvey run` re-reads on its own ──────────────────

async def saved_sources(state) -> list[str]:
    raw = await state.get_setting(SOURCES_KEY, "[]")
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return [s for s in value if isinstance(s, str) and s.strip()] if isinstance(value, list) else []


async def save_sources(state, sources: list[str]):
    await state.set_setting(SOURCES_KEY, json.dumps(list(dict.fromkeys(sources))))


async def sync_saved(state, config=None, env=None, verify=None) -> ImportReport:
    """Import every saved source now, and remember when."""
    report = ImportReport()
    for source in await saved_sources(state):
        report.merge(await import_source(state, source, config=config, env=env, verify=verify))
    await state.set_setting(SYNCED_AT_KEY, datetime.now(timezone.utc).isoformat())
    return report


async def sync_due(state, every_hours: float = SYNC_EVERY_HOURS) -> bool:
    if not await saved_sources(state):
        return False
    last = await state.get_setting(SYNCED_AT_KEY, "")
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when >= timedelta(hours=every_hours)


async def run_sync_stage(state, config=None, env=None) -> ImportReport | None:
    """The heartbeat's ride-along: re-read saved sources when due."""
    if not await sync_due(state):
        return None
    report = await sync_saved(state, config=config, env=env)
    logger.info(f"lead import: {report.summary_line()}")
    for problem in report.errors:
        logger.warning(f"lead import: {problem}")
    return report
