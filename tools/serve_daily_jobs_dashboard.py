#!/usr/bin/env python3
"""Serve a read-only dashboard over job_scraper/daily_jobs markdown reports.

Run from the repository root:
    python3 tools/serve_daily_jobs_dashboard.py

Then open http://127.0.0.1:8765 in a browser.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "job_scraper" / "daily_jobs"
PAGE = ROOT / "job_scraper" / "dashboard.html"


def split_markdown_row(line: str) -> list[str]:
    """Split a Markdown table row without splitting escaped pipe characters."""
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line.strip().strip("|"):
        if char == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(char)
        if char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    cells.append("".join(current).strip())
    return cells


def markdown_link(value: str) -> tuple[str, str] | None:
    match = re.search(r"\[([^\]]+)\]\((https?://[^)]+)\)", value)
    return (match.group(1), match.group(2)) if match else None


def report_date(name: str, text: str) -> str:
    heading = re.search(r"^# Daily Remote AI Job Search\s*[—-]\s*(.+)$", text, re.M)
    if heading:
        return heading.group(1).strip()
    return name.removesuffix(".md").replace("_", " ")


def table_entries(text: str) -> list[dict[str, str | None]]:
    lines = text.splitlines()
    header_at = next((i for i, line in enumerate(lines) if line.startswith("|") and "title" in line.lower()), None)
    if header_at is None or header_at + 1 >= len(lines):
        return []

    headers = [h.strip().lower() for h in split_markdown_row(lines[header_at])]
    entries: list[dict[str, str | None]] = []
    for line in lines[header_at + 2 :]:
        if not line.startswith("|"):
            if entries:
                break
            continue
        values = split_markdown_row(line)
        row = {headers[i]: values[i] for i in range(min(len(headers), len(values)))}

        def field(*names: str) -> str:
            for name in names:
                for header, value in row.items():
                    if header == name or header.startswith(name):
                        return value or ""
            return ""

        title_cell = field("title", "role")
        linked_title = markdown_link(title_cell)
        source_cell = field("source / direct posting", "source")
        linked_source = markdown_link(source_cell)
        fit_note = field("candidate fit")
        explicit_fit = field("fit")
        fit_match = re.match(r"\*\*(high|medium|low)[^*]*\*\*", fit_note, re.I)
        fit = explicit_fit or (fit_match.group(1) if fit_match else "")
        seen = field("previously seen")

        entries.append(
            {
                "title": linked_title[0] if linked_title else title_cell,
                "url": (linked_title[1] if linked_title else linked_source[1] if linked_source else None),
                "employer": field("employer", "company"),
                "salary": field("salary"),
                "experience": field("experience"),
                "location": field("location"),
                "posted": field("posted / verified", "posted"),
                "fit": fit.strip().replace("**", ""),
                "fitNote": fit_note or explicit_fit,
                "source": linked_source[0] if linked_source else source_cell,
                "seen": seen,
            }
        )
    return entries


def read_report(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    entries = table_entries(text)
    first_block = text.split("\n## ", 1)[0]
    summary = " ".join(first_block.splitlines()[1:]).strip()
    notes = ""
    for heading in ("## Salary, location and freshness notes", "## Salary / location notes", "## Search and filtering notes"):
        if heading in text:
            notes = text[text.index(heading) :].strip()
            break
    return {
        "file": path.name,
        "date": report_date(path.name, text),
        "summary": summary,
        "count": len(entries),
        "entries": entries,
        "notes": notes,
    }


def list_reports() -> list[dict]:
    reports = [p for p in REPORT_DIR.glob("*.md") if re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}\.md", p.name)]
    reports.sort(key=lambda p: p.name, reverse=True)
    result = []
    for path in reports:
        report = read_report(path)
        result.append({"file": report["file"], "date": report["date"], "count": report["count"]})
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "DailyJobsDashboard/1.0"

    def respond(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def respond_json(self, value: object, status: int = 200) -> None:
        self.respond(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        request = urlparse(self.path)
        if request.path in ("/", "/index.html"):
            if not PAGE.is_file():
                self.respond(b"Dashboard page not found", "text/plain; charset=utf-8", 404)
                return
            self.respond(PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if request.path == "/api/runs":
            self.respond_json(list_reports())
            return
        if request.path == "/api/run":
            name = parse_qs(request.query).get("file", [""])[0]
            if not name or Path(name).name != name or not name.endswith(".md"):
                self.respond_json({"error": "Choose a daily report from the run list."}, 400)
                return
            path = REPORT_DIR / name
            if not path.is_file() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}\.md", name):
                self.respond_json({"error": "That report is not available."}, 404)
                return
            self.respond_json(read_report(path))
            return
        self.respond(b"Not found", "text/plain; charset=utf-8", 404)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[dashboard] {self.address_string()} {fmt % args}")


def main() -> None:
    host, port = "127.0.0.1", 8765
    print(f"Daily jobs dashboard: http://{host}:{port}")
    print("Press Ctrl+C to stop. The dashboard reads daily reports without changing them.")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
