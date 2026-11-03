#!/usr/bin/env python3
"""Serve the local job workspace and daily reports.

Run from the repository root:
    python3 tools/serve_daily_jobs_dashboard.py

Then open http://127.0.0.1:8765 in a browser.
"""

from __future__ import annotations

import json
import re
import csv
import os
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "job_scraper" / "daily_jobs"
PAGE = ROOT / "job_scraper" / "dashboard.html"
WORKSPACE_FILE = ROOT / "job_scraper" / "web_workspace.json"
PROFILES_FILE = ROOT / "job_scraper" / "web_candidate_profiles.json"
TRACKER_FILE = ROOT / "job_search_tracker.csv"
REFRESH_SCRIPT = ROOT / "tools" / "refresh_daily_linkedin_jobs.ps1"
WORKSPACE_LOCK = threading.Lock()
REFRESH_LOCK = threading.Lock()
REFRESH_PROCESS: subprocess.Popen | None = None
REFRESH_STARTED: str | None = None
TRACKER_FIELDS = ["date", "company", "sector", "role", "role_type", "channel", "status", "contact_person", "fit_rating", "notes", "cv_file", "cover_letter_file", "source", "deadline"]
APPLICATION_STATUSES = {"drafted", "applied", "interview", "offer", "hired", "rejected", "no_response", "offer_declined", "withdrawn"}
DEFAULT_PROFILES = {
    "Lakshmi": {"experience": "1+ year combined internship and full-time experience; 8 months full-time", "roles": ["AI Engineer", "Generative AI Engineer", "Machine Learning Engineer", "Python Developer", "Backend Developer", "LLM Engineer", "Applied AI Engineer", "AI Developer", "Software AI Engineer", "ML Engineer", "AI Application Engineer", "NLP Engineer", "Data Scientist AI", "Python Machine Learning Engineer"], "locations": ["India", "Remote"], "target_per_day": 100},
    "Surya": {"experience": "1.5 years", "roles": ["Software Engineer", "Forward Deployed Engineer", "Full Stack Engineer"], "locations": ["Hyderabad", "Visakhapatnam", "Bengaluru", "Chennai"], "target_per_day": 100},
}


def read_json_file(path: Path, fallback: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json_file(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def get_workspace() -> dict:
    value = read_json_file(WORKSPACE_FILE, {"applications": []})
    return value if isinstance(value, dict) and isinstance(value.get("applications"), list) else {"applications": []}


def get_profiles() -> dict:
    stored = read_json_file(PROFILES_FILE, {})
    profiles = {name: dict(data) for name, data in DEFAULT_PROFILES.items()}
    if isinstance(stored, dict):
        for name in profiles:
            if isinstance(stored.get(name), dict):
                profiles[name].update(stored[name])
    return profiles


def sync_tracker(applications: list[dict]) -> None:
    existing: list[dict[str, str]] = []
    fieldnames = list(TRACKER_FIELDS)
    if TRACKER_FILE.is_file():
        with TRACKER_FILE.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames:
                fieldnames = list(reader.fieldnames)
                if "deadline" not in fieldnames:
                    fieldnames.append("deadline")
            existing = [dict(row) for row in reader]
    indexed = {(row.get("source", ""), row.get("company", "").casefold(), row.get("role", "").casefold()) for row in existing}
    for app in applications:
        key = (app.get("url", ""), app.get("company", "").casefold(), app.get("title", "").casefold())
        if key in indexed:
            for row in existing:
                if (row.get("source", ""), row.get("company", "").casefold(), row.get("role", "").casefold()) == key:
                    row.update({"status": app.get("status", "drafted"), "notes": app.get("notes", ""), "deadline": app.get("deadline", "")})
                    break
            continue
        existing.append({
            "date": app.get("applied_date") or app.get("created_at", "")[:10],
            "company": app.get("company", ""), "sector": "", "role": app.get("title", ""), "role_type": "",
            "channel": app.get("source", ""), "status": app.get("status", "drafted"), "contact_person": "",
            "fit_rating": "", "notes": app.get("notes", ""), "cv_file": app.get("cv_file", ""),
            "cover_letter_file": app.get("cover_letter_file", ""), "source": app.get("url", ""), "deadline": app.get("deadline", ""),
        })
        indexed.add(key)
    TRACKER_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = TRACKER_FILE.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in existing:
            safe_row = {}
            for field in fieldnames:
                value = str(row.get(field, "") or "")
                safe_row[field] = "'" + value if re.match(r"^[\s]*[=+@-]", value) else value
            writer.writerow(safe_row)
    os.replace(temporary, TRACKER_FILE)


def application_payload_error(payload: dict, creating: bool = False) -> str | None:
    if creating:
        for field in ("candidate", "company", "title", "url"):
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                return f"{field} is required."
        if payload["candidate"] not in DEFAULT_PROFILES:
            return "Choose Lakshmi or Surya."
        if not re.fullmatch(r"https?://\S+", payload["url"].strip()):
            return "Use a valid posting URL."
    if "status" in payload and payload["status"] not in APPLICATION_STATUSES:
        return "Choose a supported application status."
    for field in ("notes", "interview_prep", "skill_gaps", "deadline", "applied_date", "cv_file", "cover_letter_file"):
        if field in payload and (not isinstance(payload[field], str) or len(payload[field]) > 12000):
            return f"{field} must be text under 12,000 characters."
    return None


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
    heading = re.search(r"^# Daily (?:Remote )?AI Job Search\s*[—-]\s*(.+)$", text, re.M)
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
    candidate_match = re.search(r"^\*\*Candidate:\s*(.+?)\*\*\s*$", first_block, re.M)
    candidate = candidate_match.group(1).strip() if candidate_match else "Lakshmi"
    notes = ""
    for heading in ("## Salary, location and freshness notes", "## Salary / location notes", "## Search and filtering notes"):
        if heading in text:
            notes = text[text.index(heading) :].strip()
            break
    return {
        "file": path.name,
        "date": report_date(path.name, text),
        "candidate": candidate,
        "summary": summary,
        "count": len(entries),
        "entries": entries,
        "notes": notes,
    }


def list_reports() -> list[dict]:
    reports = [p for p in REPORT_DIR.glob("*.md") if re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}(?:_(?:Lakshmi|Surya)(?:_[a-f0-9]{6})?)?\.md", p.name)]
    reports.sort(key=lambda p: p.name, reverse=True)
    result = []
    for path in reports:
        report = read_report(path)
        linkedin_count = sum(1 for entry in report["entries"] if (entry.get("source") or "").strip().lower() == "linkedin")
        result.append({"file": report["file"], "date": report["date"], "candidate": report["candidate"], "count": report["count"], "linkedin_count": linkedin_count})
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

    def request_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 250_000:
            raise ValueError("Request body must be between 1 byte and 250 KB.")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Send a JSON object.")
        return value

    def respond_error(self, message: str, status: int = 400) -> None:
        self.respond_json({"error": message}, status)

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
            if not path.is_file() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}(?:_(?:Lakshmi|Surya)(?:_[a-f0-9]{6})?)?\.md", name):
                self.respond_json({"error": "That report is not available."}, 404)
                return
            self.respond_json(read_report(path))
            return
        if request.path == "/api/applications":
            data = get_workspace()
            self.respond_json(sorted(data["applications"], key=lambda app: app.get("updated_at", ""), reverse=True))
            return
        if request.path == "/api/profiles":
            self.respond_json(get_profiles())
            return
        if request.path == "/api/refresh":
            with REFRESH_LOCK:
                running = REFRESH_PROCESS is not None and REFRESH_PROCESS.poll() is None
                result_code = None if REFRESH_PROCESS is None or running else REFRESH_PROCESS.returncode
                self.respond_json({"running": running, "started": REFRESH_STARTED, "exit_code": result_code})
            return
        self.respond(b"Not found", "text/plain; charset=utf-8", 404)

    def do_POST(self) -> None:  # noqa: N802
        global REFRESH_PROCESS, REFRESH_STARTED
        try:
            payload = self.request_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.respond_error(str(error))
            return
        if self.path == "/api/applications":
            error = application_payload_error(payload, creating=True)
            if error:
                self.respond_error(error)
                return
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            with WORKSPACE_LOCK:
                workspace = get_workspace()
                existing = next((app for app in workspace["applications"] if app.get("candidate") == payload["candidate"] and app.get("url") == payload["url"]), None)
                if existing:
                    self.respond_json({"application": existing, "existing": True}, 200)
                    return
                application = {
                    "id": __import__("uuid").uuid4().hex,
                    "candidate": payload["candidate"], "company": re.sub(r"[\r\n\t]+", " ", payload["company"]).strip()[:300],
                    "title": re.sub(r"[\r\n\t]+", " ", payload["title"]).strip()[:300], "url": payload["url"].strip(),
                    "location": re.sub(r"[\r\n\t]+", " ", str(payload.get("location", ""))).strip()[:300], "source": str(payload.get("source", ""))[:100],
                    "posted": str(payload.get("posted", ""))[:200], "status": "drafted", "applied_date": "",
                    "deadline": "", "notes": "", "interview_prep": "", "skill_gaps": "",
                    "cv_file": "", "cover_letter_file": "", "created_at": now, "updated_at": now,
                }
                workspace["applications"].append(application)
                write_json_file(WORKSPACE_FILE, workspace)
                sync_tracker(workspace["applications"])
            self.respond_json({"application": application, "existing": False}, 201)
            return
        if self.path == "/api/refresh":
            with REFRESH_LOCK:
                if REFRESH_PROCESS is not None and REFRESH_PROCESS.poll() is None:
                    self.respond_json({"running": True, "started": REFRESH_STARTED}, 202)
                    return
                if not REFRESH_SCRIPT.is_file():
                    self.respond_error("The daily refresh script is missing.", 500)
                    return
                REFRESH_STARTED = datetime.now().astimezone().isoformat(timespec="seconds")
                REFRESH_PROCESS = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REFRESH_SCRIPT)],
                    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.respond_json({"running": True, "started": REFRESH_STARTED}, 202)
            return
        if self.path == "/api/prepare":
            with WORKSPACE_LOCK:
                workspace = get_workspace()
                application = next((app for app in workspace["applications"] if app.get("id") == payload.get("id")), None)
                if not application:
                    self.respond_error("Application not found.", 404)
                    return
                profile = get_profiles().get(application["candidate"], {})
                safe_name = re.sub(r"[^a-z0-9]+", "_", f"{application['company']}_{application['title']}".lower()).strip("_")[:80] or application["id"]
                folder = ROOT / "documents" / "applications" / safe_name
                folder.mkdir(parents=True, exist_ok=True)
                brief = (
                    f"# Application brief: {application['title']} at {application['company']}\n\n"
                    f"- Candidate: {application['candidate']}\n- Candidate experience: {profile.get('experience', 'See candidate profile')}\n"
                    f"- Location: {application.get('location') or 'Not stated'}\n- Posting: {application['url']}\n"
                    f"- Status: {application['status']}\n\n## Candidate notes\n\n{application.get('notes') or 'Add verified accomplishments and examples here.'}\n\n"
                    "## Next step\n\nUse the repository `/apply` workflow with the posting URL to evaluate fit, tailor the CV and cover letter, review the drafts, and verify the compiled documents. Do not add skills or experience that are not supported by the candidate's source materials.\n"
                )
                brief_path = folder / "application-brief.md"
                brief_path.write_text(brief, encoding="utf-8")
                application["brief_file"] = str(brief_path.relative_to(ROOT)).replace("\\", "/")
                application["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                write_json_file(WORKSPACE_FILE, workspace)
            self.respond_json({"file": application["brief_file"], "path": str(brief_path)})
            return
        self.respond(b"Not found", "text/plain; charset=utf-8", 404)

    def do_PUT(self) -> None:  # noqa: N802
        try:
            payload = self.request_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.respond_error(str(error))
            return
        if self.path == "/api/profiles":
            candidate = payload.get("candidate")
            if candidate not in DEFAULT_PROFILES:
                self.respond_error("Choose Lakshmi or Surya.")
                return
            roles = payload.get("roles", [])
            locations = payload.get("locations", [])
            if not isinstance(roles, list) or not isinstance(locations, list) or len(roles) > 30 or len(locations) > 30:
                self.respond_error("Roles and locations must be lists of up to 30 items.")
                return
            updated = get_profiles()
            old = updated[candidate]
            experience = str(payload.get("experience", old.get("experience", "")))[:300]
            target = payload.get("target_per_day", old.get("target_per_day", 100))
            if not isinstance(target, int) or not 1 <= target <= 100:
                self.respond_error("Daily target must be between 1 and 100.")
                return
            updated[candidate] = {"experience": experience, "roles": [str(x).strip()[:100] for x in roles if str(x).strip()], "locations": [str(x).strip()[:100] for x in locations if str(x).strip()], "target_per_day": target}
            write_json_file(PROFILES_FILE, updated)
            self.respond_json({"candidate": candidate, "profile": updated[candidate]})
            return
        if self.path == "/api/applications":
            application_id = payload.get("id")
            updates = payload.get("updates")
            if not isinstance(application_id, str) or not isinstance(updates, dict):
                self.respond_error("Provide an application id and updates object.")
                return
            error = application_payload_error(updates)
            if error:
                self.respond_error(error)
                return
            allowed = {"status", "applied_date", "deadline", "notes", "interview_prep", "skill_gaps", "cv_file", "cover_letter_file"}
            if set(updates) - allowed:
                self.respond_error("One or more application fields cannot be edited.")
                return
            with WORKSPACE_LOCK:
                workspace = get_workspace()
                application = next((app for app in workspace["applications"] if app.get("id") == application_id), None)
                if not application:
                    self.respond_error("Application not found.", 404)
                    return
                application.update(updates)
                if application.get("status") == "applied" and not application.get("applied_date"):
                    application["applied_date"] = datetime.now().astimezone().date().isoformat()
                application["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                write_json_file(WORKSPACE_FILE, workspace)
                sync_tracker(workspace["applications"])
            self.respond_json(application)
            return
        self.respond(b"Not found", "text/plain; charset=utf-8", 404)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[dashboard] {self.address_string()} {fmt % args}")


def main() -> None:
    host, port = "127.0.0.1", 8765
    print(f"Daily jobs dashboard: http://{host}:{port}")
    print("Press Ctrl+C to stop. Reports are read-only; application and profile data stay in this workspace.")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
