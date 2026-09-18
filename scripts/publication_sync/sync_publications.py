#!/usr/bin/env python3
"""Create conservative publication-site updates from a Google Scholar profile."""

from __future__ import annotations

import argparse
import calendar
import html
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / ".github" / "publication-sync.json"
SCHOLAR_BASE = "https://scholar.google.com"
OPENALEX_API = "https://api.openalex.org/works"
PREPRINT_DOI_MARKERS = ("10.48550/arxiv.", "10.26434/chemrxiv", "10.21203/")
MONTHS = {i: calendar.month_abbr[i].lower() for i in range(1, 13)}


class SyncError(RuntimeError):
    """A safe, user-actionable synchronization failure."""


@dataclass
class ScholarWork:
    title: str
    authors_summary: str
    venue_summary: str
    year: str
    detail_url: str
    scholar_id: str


@dataclass
class BibEntry:
    start: int
    end: int
    entry_type: str
    key: str
    text: str
    fields: dict[str, str]


def normalized_title(value: str) -> str:
    value = re.sub(r"[{}]", "", value)
    value = unicodedata.normalize("NFKD", html.unescape(value))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_bib_entries(content: str) -> list[BibEntry]:
    entries: list[BibEntry] = []
    start_pattern = re.compile(r"(?m)^@(\w+)\s*\{\s*([^,]+),")
    for match in start_pattern.finditer(content):
        depth = 1
        escaped = False
        end = match.end()
        while end < len(content) and depth:
            ch = content[end]
            if ch == "\\" and not escaped:
                escaped = True
                end += 1
                continue
            if not escaped:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
            escaped = False
            end += 1
        if depth:
            raise SyncError(f"Unbalanced BibTeX entry: {match.group(2).strip()}")
        text = content[match.start() : end]
        entries.append(
            BibEntry(
                start=match.start(),
                end=end,
                entry_type=match.group(1).lower(),
                key=match.group(2).strip(),
                text=text,
                fields=parse_bib_fields(text),
            )
        )
    return entries


def parse_bib_fields(entry: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    pattern = re.compile(r"(?m)^\s*([A-Za-z][\w-]*)\s*=\s*")
    for match in pattern.finditer(entry):
        pos = match.end()
        if pos >= len(entry):
            continue
        opener = entry[pos]
        if opener not in '{"':
            raw = entry[pos : entry.find(",", pos) if "," in entry[pos:] else len(entry)]
            fields[match.group(1).lower()] = raw.strip()
            continue
        closer = "}" if opener == "{" else '"'
        depth = 1
        i = pos + 1
        escaped = False
        while i < len(entry) and depth:
            ch = entry[i]
            if ch == "\\" and not escaped:
                escaped = True
                i += 1
                continue
            if not escaped:
                if opener == "{" and ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
            escaped = False
            i += 1
        fields[match.group(1).lower()] = entry[pos + 1 : i - 1].strip()
    return fields


def scholar_id_from_url(url: str) -> str:
    value = parse_qs(urlparse(url).query).get("citation_for_view", [""])[0]
    return value.split(":", 1)[-1] if ":" in value else value


class PublicationSync:
    def __init__(self, config: dict[str, Any], session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; personal-homepage-publication-sync/1.0; "
                    f"+mailto:{config['contact_email']})"
                )
            }
        )

    def get(self, url: str, *, params: dict[str, Any] | None = None) -> requests.Response:
        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SyncError(f"Could not fetch {url}: {exc}") from exc
        return response

    def scholar_works(self) -> list[ScholarWork]:
        response = self.get(
            f"{SCHOLAR_BASE}/citations",
            params={
                "user": self.config["google_scholar_user_id"],
                "hl": "en",
                "pagesize": 100,
            },
        )
        soup = BeautifulSoup(response.text, "html.parser")
        rows = soup.select(".gsc_a_tr")
        if not rows:
            if "captcha" in response.text.casefold() or "not a robot" in response.text.casefold():
                raise SyncError("Google Scholar requested a CAPTCHA; no files were changed")
            raise SyncError("Google Scholar profile layout was not recognized; no files were changed")

        works: list[ScholarWork] = []
        for row in rows:
            title_link = row.select_one(".gsc_a_at")
            if not title_link:
                continue
            gray = row.select(".gs_gray")
            year_node = row.select_one(".gsc_a_y span")
            detail_url = urljoin(SCHOLAR_BASE, title_link.get("href", ""))
            works.append(
                ScholarWork(
                    title=clean_text(title_link.get_text(" ", strip=True)),
                    authors_summary=clean_text(gray[0].get_text(" ", strip=True)) if gray else "",
                    venue_summary=clean_text(gray[1].get_text(" ", strip=True)) if len(gray) > 1 else "",
                    year=clean_text(year_node.get_text(" ", strip=True)) if year_node else "",
                    detail_url=detail_url,
                    scholar_id=scholar_id_from_url(detail_url),
                )
            )
        return works

    def scholar_details(self, work: ScholarWork) -> dict[str, str]:
        time.sleep(1)
        soup = BeautifulSoup(self.get(work.detail_url).text, "html.parser")
        labels = [clean_text(node.get_text(" ", strip=True)) for node in soup.select(".gsc_oci_field")]
        values = [clean_text(node.get_text(" ", strip=True)) for node in soup.select(".gsc_oci_value")]
        if not labels or len(labels) != len(values):
            raise SyncError(f"Could not read Scholar details for: {work.title}")
        return dict(zip(labels, values))

    def openalex_match(self, title: str) -> dict[str, Any] | None:
        data = self.get(
            OPENALEX_API,
            params={
                "search": title,
                "per-page": 10,
                "mailto": self.config["contact_email"],
            },
        ).json()
        wanted = normalized_title(title)
        candidates: list[tuple[tuple[int, float, int], dict[str, Any]]] = []
        for result in data.get("results", []):
            candidate_title = normalized_title(result.get("title") or "")
            similarity = SequenceMatcher(None, wanted, candidate_title).ratio()
            if similarity < 0.94:
                continue
            author_ids = {
                (authorship.get("author", {}).get("orcid") or "").removeprefix("https://orcid.org/")
                for authorship in result.get("authorships", [])
            }
            author_names = {
                normalized_title(authorship.get("author", {}).get("display_name") or "")
                for authorship in result.get("authorships", [])
            }
            correct_author = self.config["orcid"] in author_ids or "lichengxu" in author_names
            if not correct_author:
                continue
            doi = normalize_doi(result.get("doi") or "")
            published = int(result.get("publication_year") or 0)
            score = (0 if is_preprint_doi(doi) else 1, similarity, published)
            candidates.append((score, result))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None


def normalize_doi(value: str) -> str:
    value = value.strip().casefold()
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)


def is_preprint_doi(doi: str) -> bool:
    return any(marker in normalize_doi(doi) for marker in PREPRINT_DOI_MARKERS)


def existing_is_preprint(entry: BibEntry) -> bool:
    journal = entry.fields.get("journal", "").casefold()
    return is_preprint_doi(entry.fields.get("doi", "")) or "arxiv" in journal or "chemrxiv" in journal


def scholar_suggests_journal(work: ScholarWork) -> bool:
    venue = work.venue_summary.casefold()
    if not venue or venue.startswith("- "):
        return False
    return not any(name in venue for name in ("arxiv", "chemrxiv", "preprint"))


def inverted_abstract(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    words = sorted(((position, word) for word, positions in index.items() for position in positions))
    return " ".join(word for _, word in words)


def metadata_from_sources(
    work: ScholarWork, details: dict[str, str], openalex: dict[str, Any] | None
) -> dict[str, str]:
    metadata: dict[str, str] = {
        "title": work.title,
        "google_scholar_id": work.scholar_id,
    }
    if openalex:
        metadata["author"] = " and ".join(
            authorship.get("author", {}).get("display_name", "")
            for authorship in openalex.get("authorships", [])
            if authorship.get("author", {}).get("display_name")
        )
        publication_date = openalex.get("publication_date") or ""
        if publication_date:
            year, *rest = publication_date.split("-")
            metadata["year"] = year
            if rest and rest[0].isdigit() and int(rest[0]) in MONTHS:
                metadata["month"] = MONTHS[int(rest[0])]
        location = openalex.get("primary_location") or {}
        source = location.get("source") or {}
        if source.get("display_name"):
            metadata["journal"] = source["display_name"]
        biblio = openalex.get("biblio") or {}
        for source_name, bib_name in (
            ("volume", "volume"),
            ("issue", "number"),
            ("first_page", "first_page"),
            ("last_page", "last_page"),
        ):
            if biblio.get(source_name):
                metadata[bib_name] = str(biblio[source_name])
        if metadata.get("first_page"):
            metadata["pages"] = metadata.pop("first_page")
            last_page = metadata.pop("last_page", "")
            if last_page and last_page != metadata["pages"]:
                metadata["pages"] += f"--{last_page}"
        doi = normalize_doi(openalex.get("doi") or "")
        if doi:
            metadata["doi"] = doi
            metadata["url"] = f"https://doi.org/{doi}"
        elif location.get("landing_page_url"):
            metadata["url"] = location["landing_page_url"]
        abstract = inverted_abstract(openalex.get("abstract_inverted_index"))
        if abstract:
            metadata["abstract"] = abstract
        metadata["entry_type"] = {
            "article": "article",
            "review": "article",
            "conference": "inproceedings",
            "conference-paper": "inproceedings",
            "preprint": "misc",
        }.get(openalex.get("type", ""), "article")
    else:
        metadata["author"] = details.get("Authors", work.authors_summary).replace(", ", " and ")
        publication_date = details.get("Publication date", work.year)
        parts = publication_date.split("/")
        metadata["year"] = parts[0] if parts else work.year
        if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) in MONTHS:
            metadata["month"] = MONTHS[int(parts[1])]
        if details.get("Journal"):
            metadata["journal"] = details["Journal"]
        elif details.get("Conference"):
            metadata["booktitle"] = details["Conference"]
        for source_name, bib_name in (("Volume", "volume"), ("Issue", "number"), ("Pages", "pages")):
            if details.get(source_name):
                metadata[bib_name] = details[source_name]
        if details.get("Description"):
            metadata["abstract"] = details["Description"].removesuffix("…").strip()
        arxiv = re.search(r"arXiv:(\d{4}\.\d{4,5})", details.get("Journal", ""), re.I)
        if arxiv:
            metadata.update(
                {
                    "entry_type": "misc",
                    "doi": f"10.48550/arxiv.{arxiv.group(1)}",
                    "url": f"https://arxiv.org/abs/{arxiv.group(1)}",
                }
            )
        else:
            metadata["entry_type"] = "inproceedings" if "Conference" in details else "article"
    return {key: clean_text(str(value)) for key, value in metadata.items() if value}


def bibtex_escape(value: str) -> str:
    value = value.replace("\\", "\\textbackslash{}")
    for source, replacement in (("&", r"\&"), ("%", r"\%"), ("_", r"\_")):
        value = value.replace(source, replacement)
    return value


def citation_key(metadata: dict[str, str], used: set[str]) -> str:
    first_author = metadata.get("author", "unknown").split(" and ", 1)[0]
    family = re.sub(r"[^a-z0-9]", "", first_author.split()[-1].casefold()) or "unknown"
    stop = {"a", "an", "the", "of", "for", "and", "in", "on", "with", "from", "to"}
    words = re.findall(r"[a-z0-9]+", metadata["title"].casefold())
    keyword = next((word for word in words if word not in stop), "paper")
    base = f"{family}_{keyword}_{metadata.get('year', date.today().year)}"
    key = base
    suffix = 2
    while key in used:
        key = f"{base}_{suffix}"
        suffix += 1
    return key


def render_bib_entry(metadata: dict[str, str], key: str) -> str:
    preferred = [
        "author",
        "title",
        "journal",
        "booktitle",
        "year",
        "month",
        "volume",
        "number",
        "pages",
        "doi",
        "url",
        "google_scholar_id",
        "abstract",
    ]
    lines = [f"@{metadata.get('entry_type', 'article')}{{{key},"]
    for field in preferred:
        if metadata.get(field):
            lines.append(f"  {field} = {{{bibtex_escape(metadata[field])}}},")
    lines.append("}")
    return "\n".join(lines)


def replace_or_add_field(entry: str, field: str, value: str) -> str:
    escaped = bibtex_escape(value)
    pattern = re.compile(rf"(?im)^(\s*{re.escape(field)}\s*=\s*)\{{(?:[^{{}}]|\{{[^{{}}]*\}})*\}}(\s*,?)")
    if pattern.search(entry):
        return pattern.sub(lambda match: f"{match.group(1)}{{{escaped}}}{match.group(2)}", entry, count=1)
    closing = entry.rfind("}")
    return entry[:closing].rstrip() + f"\n  {field} = {{{escaped}}},\n" + entry[closing:]


def upgrade_entry(entry: BibEntry, metadata: dict[str, str]) -> str:
    updated = entry.text
    for field in ("journal", "booktitle", "year", "month", "volume", "number", "pages", "doi", "url"):
        if metadata.get(field):
            updated = replace_or_add_field(updated, field, metadata[field])
    return updated


def insert_new_entries(content: str, rendered: list[str]) -> str:
    if not rendered:
        return content
    header = re.match(r"\A---\s*\n---\s*\n", content)
    position = header.end() if header else 0
    block = "\n\n".join(rendered) + "\n\n"
    return content[:position] + block + content[position:]


def next_news_number(news_directory: Path) -> int:
    numbers = []
    for path in news_directory.glob("announcement_*.md"):
        match = re.fullmatch(r"announcement_(\d+)\.md", path.name)
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def write_news(news_directory: Path, number: int, metadata: dict[str, str]) -> Path:
    publication_date = f"{metadata.get('year', date.today().year)}-01-01"
    if metadata.get("month") in MONTHS.values():
        month = next(key for key, value in MONTHS.items() if value == metadata["month"])
        publication_date = f"{metadata.get('year', date.today().year)}-{month:02d}-01"
    title = metadata["title"].replace('"', "'")
    venue = metadata.get("journal") or metadata.get("booktitle") or "a preprint server"
    url = metadata.get("url") or f"https://scholar.google.com/scholar?q={normalized_title(title)}"
    if is_preprint_doi(metadata.get("doi", "")) or "preprint" in venue.casefold():
        sentence = f'Our paper titled "{title}" is now available as a [{venue}]({url}) preprint.'
    else:
        sentence = f'Our paper titled "{title}" has been published in [{venue}]({url}).'
    content = (
        "---\n"
        "layout: post\n"
        f"date: {publication_date} 12:00:00+0000\n"
        "inline: true\n"
        "related_posts: false\n"
        "---\n"
        f"{sentence}\n"
    )
    path = news_directory / f"announcement_{number}.md"
    path.write_text(content, encoding="utf-8")
    return path


def run(config: dict[str, Any], *, dry_run: bool = False, session: requests.Session | None = None) -> int:
    bib_path = ROOT / config["bibliography"]
    news_directory = ROOT / config["news_directory"]
    original = bib_path.read_text(encoding="utf-8")
    entries = parse_bib_entries(original)
    by_title = {normalized_title(entry.fields.get("title", "")): entry for entry in entries}
    excluded = {normalized_title(title) for title in config.get("excluded_titles", [])}
    used_keys = {entry.key for entry in entries}
    sync = PublicationSync(config, session=session)

    additions: list[tuple[dict[str, str], str]] = []
    upgrades: list[tuple[BibEntry, dict[str, str]]] = []
    for work in sync.scholar_works():
        normalized = normalized_title(work.title)
        if normalized in excluded:
            continue
        existing = by_title.get(normalized)
        needs_upgrade = bool(existing and existing_is_preprint(existing) and scholar_suggests_journal(work))
        if existing and not needs_upgrade:
            continue
        details = sync.scholar_details(work)
        openalex = sync.openalex_match(work.title)
        metadata = metadata_from_sources(work, details, openalex)
        if not metadata.get("author") or not metadata.get("year"):
            raise SyncError(f"Incomplete metadata for {work.title}; no files were changed")
        if existing:
            new_doi = metadata.get("doi", "")
            if not new_doi or is_preprint_doi(new_doi):
                continue
            upgrades.append((existing, metadata))
        else:
            key = citation_key(metadata, used_keys)
            used_keys.add(key)
            additions.append((metadata, render_bib_entry(metadata, key)))

    updated = original
    for entry, metadata in sorted(upgrades, key=lambda item: item[0].start, reverse=True):
        replacement = upgrade_entry(entry, metadata)
        updated = updated[: entry.start] + replacement + updated[entry.end :]
    updated = insert_new_entries(updated, [rendered for _, rendered in additions])

    changed_metadata = [metadata for _, metadata in upgrades] + [metadata for metadata, _ in additions]
    if dry_run:
        for metadata in changed_metadata:
            print(f"Would update: {metadata['title']}")
        if not changed_metadata:
            print("No publication updates found")
        return len(changed_metadata)

    if updated != original:
        bib_path.write_text(updated, encoding="utf-8")
    if config.get("create_news", True) and changed_metadata:
        number = next_news_number(news_directory)
        for metadata in changed_metadata:
            write_news(news_directory, number, metadata)
            number += 1
    for metadata in changed_metadata:
        print(f"Updated: {metadata['title']}")
    if not changed_metadata:
        print("No publication updates found")
    return len(changed_metadata)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing files")
    args = parser.parse_args()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    try:
        run(config, dry_run=args.dry_run)
    except (SyncError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"publication-sync: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
