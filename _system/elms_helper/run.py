"""Orchestration: scan -> answer -> save PDF -> stage (never submit)."""

from __future__ import annotations

import re
import shutil
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

from . import ai
from .browser import ELMSBrowser
from .config import LOGS_DIR, ROOT_DIR
from .elms import (
    collect_attachment_text,
    course_code,
    course_folder,
    download_attachments,
    is_excluded,
    NON_ASSIGNMENT_WORDS,
    read_assignment,
    scan_all,
    scan_courses,
    short_name,
    stage_answer,
)
from .pdfdoc import build_html, html_to_pdf, plain_text

DETAIL_LINK = re.compile(r"/student_assignments/(due|view)/\d+", re.IGNORECASE)


def _print_table(rows: list[dict]) -> None:
    terminal_width = shutil.get_terminal_size((100, 24)).columns
    # Reserve space for readable identifying fields; wrap long values instead
    # of allowing Windows console output to run past the visible screen.
    index_w = max(2, max((len(str(row.get("index", ""))) for row in rows), default=1))
    course_w = min(18, max(8, max((len(str(row.get("course", ""))) for row in rows), default=8)))
    status_w = min(26, max(10, max((len(str(row.get("status", ""))) for row in rows), default=10)))
    assign_w = max(18, terminal_width - index_w - course_w - status_w - 10)
    widths = [index_w, course_w, assign_w, status_w]
    headers = ["#", "Course", "Assignment", "Status"]
    line = "  ".join(header.ljust(width) for header, width in zip(headers, widths))
    print(line)
    print("-" * min(terminal_width, len(line)))
    for row in rows:
        cells = [str(row.get("index", "")), str(row.get("course", "")),
                 str(row.get("title", "")), str(row.get("status", ""))]
        wrapped = [textwrap.wrap(cell, width=width) or [""]
                   for cell, width in zip(cells, widths)]
        for line_no in range(max(map(len, wrapped))):
            print("  ".join(
                (wrapped[col][line_no] if line_no < len(wrapped[col]) else "").ljust(widths[col])
                for col in range(len(widths))
            ))
        if row.get("folder"):
            print(f"   Folder: {row['folder']}")
        for action in row.get("requires_user_action", []):
            prefix = "   ! "
            print(textwrap.fill(str(action), width=max(30, terminal_width - len(prefix)),
                                initial_indent=prefix, subsequent_indent="     "))
        if row.get("error"):
            prefix = "   x "
            print(textwrap.fill(str(row["error"]), width=max(30, terminal_width - len(prefix)),
                                initial_indent=prefix, subsequent_indent="     "))


def _write_report(rows: list[dict], started: datetime) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / f"report-{started:%Y%m%d-%H%M%S}.md"
    lines = [f"# ELMS Helper report — {started:%Y-%m-%d %H:%M}", ""]
    lines += ["| # | Course | Assignment | Status | Folder |", "|---:|---|---|---|---|"]
    for row in rows:
        lines.append(
            f"| {row.get('index', '')} | {row.get('course', '')} | {row.get('title', '')} "
            f"| {row.get('status', '')} | {row.get('folder', '')} |"
        )
    for row in rows:
        notes = row.get("requires_user_action") or []
        if notes or row.get("error"):
            lines += ["", f"### {row.get('course', '')} — {row.get('title', '')}"]
            lines += [f"- {note}" for note in notes]
            if row.get("error"):
                lines.append(f"- ERROR: {row['error']}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _filter_assignments(entries: list[dict]) -> list[dict]:
    detailed = [entry for entry in entries if DETAIL_LINK.search(entry.get("href", ""))]
    return detailed or entries


def run(cfg: dict, limit: int | None = None, dry_run: bool = False) -> int:
    started = datetime.now()
    limit = limit or int(cfg.get("max_assignments", 12))
    rows: list[dict] = []

    with ELMSBrowser(headless=bool(cfg.get("headless", False))) as browser:
        if not browser.is_logged_in():
            print("\n  Not logged in. Run menu option 2 (Log in to ELMS) first.")
            return 3
        page = browser.page

        print("\nScanning each course's #mainContent assignment list...")
        try:
            courses = scan_courses(page)
        except Exception as exc:
            courses = []
            print(f"  (could not list courses: {exc})")
        if courses:
            print(f"  Enrolled courses found: {len(courses)}")
        entries = scan_all(page, cfg["exclude_patterns"], courses)
        divider_count = sum(
            bool(NON_ASSIGNMENT_WORDS.search(
                f"{entry.get('title', '')} {entry.get('context', '')}"
            ))
            for entry in entries
        )
        entries = [
            entry for entry in entries
            if not NON_ASSIGNMENT_WORDS.search(
                f"{entry.get('title', '')} {entry.get('context', '')}"
            )
        ]
        if divider_count:
            print(f"  Ignoring {divider_count} divider row(s).")
        past_due_count = sum(entry.get("past_due", False) for entry in entries)
        entries = [entry for entry in entries if not entry.get("past_due", False)]
        if past_due_count:
            print(f"  Ignoring {past_due_count} past-due assignment(s).")
        submitted_count = sum(entry.get("submitted") is True for entry in entries)
        entries = [entry for entry in entries if entry.get("submitted") is not True]
        if submitted_count:
            print(f"  Ignoring {submitted_count} assignment(s) already marked submitted.")
        if not entries:
            print("No unsubmitted assignment links were found in the course lists.")
            return 2

        skipped_groups: list[str] = []
        for entry in entries:
            label = entry.get("title", "")
            if is_excluded(label, cfg["exclude_patterns"]):
                skipped_groups.append(label)
        if skipped_groups:
            print("Skipped non-assignment To-do groups:")
            for label in sorted(set(skipped_groups)):
                print(f"  - {label}")

        processed = 0
        for entry in entries:
            if processed >= limit:
                break
            if is_excluded(entry.get("title", ""), cfg["exclude_patterns"]):
                continue

            print(
                f"\n[{processed + 1}] Opening: {entry.get('title', '')} - "
                f"({entry.get('course') or 'Unknown course'})"
            )
            try:
                data = read_assignment(page, entry["href"])
            except Exception as exc:
                rows.append({
                    "index": processed + 1,
                    "title": entry.get("title", ""),
                    "status": "ERROR",
                    "error": f"could not open assignment: {exc}",
                })
                processed += 1
                continue

            # Trust the course list row; assignment pages may have misleading headings.
            course = entry.get("course") or data.get("course") or "Unknown course"
            title = entry.get("title") or data.get("title") or "Untitled assignment"
            combined = f"{course} {title} {entry.get('title', '')}"

            if is_excluded(combined, cfg["exclude_patterns"]):
                rows.append({
                    "index": processed + 1, "course": course_code(course), "title": title,
                    "status": "SKIPPED (excluded group)",
                })
                processed += 1
                continue

            if data.get("submitted") and entry.get("submitted") is not False:
                rows.append({
                    "index": processed + 1, "course": course_code(course), "title": title,
                    "status": "SKIPPED (already submitted)",
                })
                processed += 1
                continue

            if dry_run:
                rows.append({
                    "index": processed + 1, "course": course_code(course), "title": title,
                    "status": "SCANNED (dry run)",
                })
                processed += 1
                continue

            temp_dir = Path(tempfile.mkdtemp(prefix="elms-att-"))
            try:
                pdfs = download_attachments(browser.context, data, temp_dir)
                attachment_text, images = collect_attachment_text(pdfs)
                print(f"  Attachments: {len(pdfs)} PDF(s), AI reading {len(images)} page image(s)")

                prompt = ai.build_prompt(cfg, data, attachment_text)
                print("  Asking the AI for the answer...")
                answer = ai.generate(cfg, prompt, images)
                if not answer.get("answer_html"):
                    raise ai.AIError("AI returned an empty answer.")

                code_hint = entry.get("code") or data.get("code", "")
                folder = ROOT_DIR / "output" / course_folder(course, code_hint) / short_name(title)
                folder.mkdir(parents=True, exist_ok=True)
                meta = f"{course} — {cfg.get('student_name', '')}".strip(" —")
                page_html = build_html(title, meta, answer["answer_html"])
                pdf_path = html_to_pdf(page_html, folder / "answer.pdf")
                print(f"  Saved: {pdf_path}")

                staged = stage_answer(page, data, pdf_path, plain_text(answer["answer_html"]))
                try:
                    page.screenshot(path=str(folder / "staged.png"), full_page=True)
                except Exception:
                    pass

                status = "STAGED (not submitted)"
                if not staged.get("uploaded") and not staged.get("filled_text"):
                    status = "DRAFT SAVED (manual upload needed)"
                row = {
                    "index": processed + 1,
                    "course": course_code(course),
                    "title": title,
                    "status": status,
                    "folder": str(folder),
                    "requires_user_action": answer.get("requires_user_action", []),
                }
                if staged.get("submit_button_present"):
                    row["requires_user_action"] = row["requires_user_action"] + [
                        "Review the staged answer, then click Submit yourself."
                    ]
                if answer.get("notes"):
                    row["notes"] = answer["notes"]
                rows.append(row)
            except Exception as exc:
                rows.append({
                    "index": processed + 1, "course": course_code(course), "title": title,
                    "status": "ERROR", "error": str(exc)[:500],
                })
            finally:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)

            processed += 1

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    _print_table(rows)
    report = _write_report(rows, started)
    print(f"\nReport saved to: {report}")
    print("Nothing was submitted. Review each assignment on ELMS and click Submit yourself.")
    return 0 if any(row["status"].startswith("STAGED") for row in rows) else 0
