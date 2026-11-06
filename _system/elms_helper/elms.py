"""ELMS page scraping and staging (never submits)."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import Page

from .config import POPPLER_DIR
from .pdfdoc import extract_pdf

DETAIL_LINK = re.compile(
    r"/student_assignments/(?!list/|analytics/)[a-z_-]+/\d+", re.IGNORECASE
)

SCAN_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const root = document.querySelector('#mainContent');
  if (!root) return [];
  const out = [];
  const seen = new Set();
  root.querySelectorAll('table').forEach((table) => {
    const rows = Array.from(table.querySelectorAll('tr'));
    const headerRow = rows.find((row) => {
      const labels = Array.from(row.cells).map((cell) => clean(cell.innerText).toLowerCase());
      return labels.some((x) => /^assignments?$/.test(x)) && labels.some((x) => /^submitted$/.test(x));
    });
    if (!headerRow) return;
    let column = 0;
    let assignmentRange = null;
    let submittedIndex = -1;
    Array.from(headerRow.cells).forEach((cell) => {
      const label = clean(cell.innerText).toLowerCase();
      const span = Math.max(1, Number(cell.colSpan) || 1);
      if (/^assignments?$/.test(label)) assignmentRange = [column, column + span];
      if (/^submitted$/.test(label)) submittedIndex = column;
      column += span;
    });
    if (!assignmentRange || submittedIndex < 0) return;
    rows.slice(rows.indexOf(headerRow) + 1).forEach((row) => {
      const cells = Array.from(row.cells);
      const assignmentCell = cells.slice(assignmentRange[0], assignmentRange[1])
        .find((cell) => cell.querySelector('a[href]'));
      const submittedCell = cells[submittedIndex];
      if (!assignmentCell || !submittedCell) return;
      const anchors = Array.from(assignmentCell.querySelectorAll('a[href]')).filter((a) =>
        !/\/student_assignments\/(list|analytics)\//i.test(a.href || '')
      );
      if (!anchors.length) return;
      const assignmentLink = anchors.find((a) =>
        /\/student_assignments\/(?!list\/|analytics\/)[a-z_-]+\/\d+/i.test(a.href || '')
      ) || anchors[0];
      const href = assignmentLink.href || '';
      if (!href || /^javascript:/i.test(href) || href.endsWith('#')) return;
      const title = clean(assignmentLink.innerText || assignmentLink.textContent)
        || clean(assignmentCell.innerText).split('\n')[0];
      const rowKey = row.id || row.getAttribute('data-id') || row.getAttribute('data-assignment-id')
        || `${href}::${title}`;
      if (!title || seen.has(rowKey)) return;
      seen.add(rowKey);
      const statusParts = [submittedCell.innerText, submittedCell.getAttribute('class'),
        submittedCell.getAttribute('aria-label'), submittedCell.getAttribute('title'),
        submittedCell.getAttribute('data-status'), submittedCell.getAttribute('aria-checked'),
        submittedCell.getAttribute('checked'),
        ...Array.from(submittedCell.querySelectorAll('*')).flatMap((el) => [
          el.getAttribute('class'), el.getAttribute('aria-label'), el.getAttribute('title'),
          el.getAttribute('alt'), el.getAttribute('role'), el.getAttribute('data-status'),
          el.getAttribute('aria-checked'), el.getAttribute('checked'),
        ])];
      const submittedSignal = statusParts.filter(Boolean).join(' ').toLowerCase();
      const pastDue = /past\s*due|overdue/.test(submittedSignal);
      // Treat a blank/unchecked Submitted cell as not submitted, per the scan rule.
      let submitted = false;
      if (/not submitted|pending|flag|false|unchecked|\bno\b|cross|times|fa-xmark|glyphicon-remove/.test(submittedSignal)) submitted = false;
      else if (/submitted|check|tick|complete|success|glyphicon-ok|fa-check|aria-checked true|checked|\btrue\b|\byes\b|✓|✔/.test(submittedSignal)) submitted = true;
      out.push({ title, href, rowKey, context: clean(row.innerText).slice(0, 700), submitted, past_due: pastDue });
    });
  });
  return out;
}
"""

COURSES_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  const seen = new Set();
  const enrolledTable = document.querySelector('form#classes');
  if (!enrolledTable) return [];
  enrolledTable.querySelectorAll('a[href*="/student_class/show/"]').forEach((a) => {
    const match = (a.href || '').match(/\/student_class\/show\/(\d+)/);
    if (!match) return;
    const id = match[1];
    if (seen.has(id)) return;
    seen.add(id);
    const row = a.closest('tr') || a.parentElement;
    const subtitle = row
      ? clean(Array.from(row.querySelectorAll('span.no-print')).map((s) => s.innerText).join(' '))
      : '';
    out.push({
      id,
      title: clean(a.innerText || a.textContent),
      subtitle,
      href: location.origin + '/student_assignments/list/' + id + '#mainContent',
    });
  });
  return out;
}
"""

PAGE_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const main = document.querySelector('main') || document.body;
  const headings = Array.from(document.querySelectorAll('h1, h2, h3'))
    .map((h) => clean(h.innerText)).filter(Boolean).slice(0, 12);
  const links = Array.from(document.querySelectorAll('a[href]'))
    .map((a) => ({ name: clean(a.innerText), href: a.href }))
    .filter((x) => /\.pdf(\?|$)|\/files?\/|download|attachment|force_download/i.test(x.href));
  const textareas = Array.from(document.querySelectorAll('textarea')).filter((t) => t.offsetParent !== null).length;
  const editors = Array.from(document.querySelectorAll('[contenteditable="true"]')).filter((t) => t.offsetParent !== null).length;
  const fileInputs = Array.from(document.querySelectorAll('input[type=file]')).map((i) => i.accept || '');
  const buttons = Array.from(document.querySelectorAll('button, input[type=submit], a.button'))
    .map((b) => clean(b.innerText || b.value)).filter((t) => t && t.length < 40).slice(0, 30);
  return {
    title: document.title,
    headings,
    text: (main.innerText || '').slice(0, 30000),
    links,
    textareas,
    editors,
    fileInputs,
    buttons,
  };
}
"""

ACTIVITY_WORDS = re.compile(
    r"(performance\s+task|assignment|activity|quiz|exam|seatwork|project|output|"
    r"laboratory|essay|module|discussion|reflection)",
    re.IGNORECASE,
)
SUBMITTED_WORDS = re.compile(
    r"(submitted\s+for\s+grading|graded|turned\s+in|submission\s+status\s*:?\s*submitted)",
    re.IGNORECASE,
)
NON_ASSIGNMENT_WORDS = re.compile(r"\bdivider\b|\(not\s+graded\)", re.IGNORECASE)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def assignment_key(href: str) -> str:
    """Stable id for an assignment link: '/student_assignments/due/123?x' -> 123."""
    match = re.search(r"/student_assignments/(?:due|view)/(\d+)", href or "")
    if match:
        return "assignment:" + match.group(1)
    return (href or "").split("?")[0]


def _guess_title(entry: dict) -> str:
    """The To-do widget links often wrap only an icon or a dash; read the row instead."""
    title = _clean(entry.get("title", ""))
    if len(title) >= 3 and re.search(r"[A-Za-z]{3}", title):
        return title
    aria = _clean(entry.get("aria", ""))
    if len(aria) >= 3:
        return aria
    lines = [_clean(line) for line in re.split(r"[\n\r\t]+", entry.get("context", ""))]
    for line in lines:
        if len(line) < 4 or len(line) > 160:
            continue
        if re.fullmatch(r"[-–—\d\s%./:]+", line):
            continue
        if re.search(r"(due|submitted|graded|points?|status)", line, re.IGNORECASE):
            continue
        return line
    return title or _clean(entry.get("context", ""))[:100]


def _dedupe_entries(entries: list[dict]) -> list[dict]:
    """One entry per assignment, preferring the copy that carries a title."""
    merged: dict[str, dict] = {}
    for entry in entries:
        href = entry.get("href", "")
        if not href:
            continue
        key = assignment_key(href)
        entry = dict(entry)
        entry["title"] = _guess_title(entry)
        old = merged.get(key)
        if old is None:
            merged[key] = entry
            continue
        # Prefer a real title / a clean href without the redirect query.
        if len(entry.get("title", "")) > len(old.get("title", "")):
            old["title"] = entry["title"]
        if "?" not in href and "?" in old.get("href", ""):
            old["href"] = href
        if not old.get("context") and entry.get("context"):
            old["context"] = entry["context"]
    return list(merged.values())


def scan_todo(page: Page, exclude_patterns: list[str]) -> list[dict]:
    page.goto("https://elms.sti.edu/", wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    def collect() -> list[dict]:
        return page.evaluate(SCAN_JS) or []

    entries = collect()

    if not entries:
        # Try expanding collapsed To-do groups by clicking course-like rows.
        candidates = page.locator(
            "li, div[role='listitem'], .course, .group"
        ).all()[:40]
        clicked = 0
        for row in candidates:
            if clicked >= 10:
                break
            try:
                text = _clean(row.inner_text(timeout=1000))
            except Exception:
                continue
            if not text or len(text) > 120:
                continue
            looks_like_group = bool(re.search(r"[A-Z]{2,6}\d{2,4}|SY\d{4}|evaluation|monitoring", text))
            if not looks_like_group:
                continue
            if any(re.search(p, text, re.IGNORECASE) for p in exclude_patterns):
                clicked += 1
                continue
            try:
                row.click(timeout=2000)
                clicked += 1
                page.wait_for_timeout(800)
            except Exception:
                continue
            entries = collect()
            if entries:
                break

    unique: dict[str, dict] = {}
    for entry in entries:
        href = entry.get("href", "")
        if href and href not in unique:
            unique[href] = entry
    return _dedupe_entries(list(unique.values()))


def _merge_courses(base: list[dict], extra: list[dict]) -> list[dict]:
    """Merge by course link, keeping the most descriptive title."""
    by_href = {course.get("href", ""): course for course in base if course.get("href")}
    for course in extra:
        href = course.get("href", "")
        if not href:
            continue
        old = by_href.get(href)
        if old is None or len(course.get("title", "")) > len(old.get("title", "")):
            by_href[href] = course
    return list(by_href.values())


def _open_course_menu(page: Page) -> None:
    """Open the top-nav Courses dropdown (where all enrolled courses live)."""
    try:
        page.get_by_role("link", name=re.compile(r"^Courses$", re.I)).first.click(timeout=3000)
        page.wait_for_timeout(800)
    except Exception:
        try:
            page.get_by_text("Courses", exact=True).first.click(timeout=3000)
            page.wait_for_timeout(800)
        except Exception:
            return
    try:
        enrolled = page.get_by_text("Enrolled", exact=True).first
        if enrolled.count():
            enrolled.click(timeout=3000)
            page.wait_for_timeout(1000)
    except Exception:
        pass


def scan_courses(page: Page) -> list[dict]:
    """Current dashboard enrollment table only; excludes historical courses."""
    courses: list[dict] = []
    try:
        page.goto("https://elms.sti.edu/user_dashboard", wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_selector('form#classes a[href*="/student_class/show/"]', timeout=8000)
        courses = page.evaluate(COURSES_JS) or []
    except Exception:
        pass

    for course in courses:
        title = course.get("title", "")
        hint = subject_code(course.get("subtitle", ""))
        if not hint:
            match = re.search(r"\b(?!SY\d{4}\b)([A-Z]{2,6}\d{2,4}[A-Z]?)\b", title)
            hint = match.group(1) if match else ""
        course["code"] = hint
    return courses


def scan_course_assignments(page: Page, course: dict) -> list[dict]:
    """Assignment links inside one course page."""
    try:
        page.goto(course["href"], wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(800)
        entries = page.evaluate(SCAN_JS) or []
        if not entries:
            try:
                page.wait_for_function(
                    """() => {
                      const root = document.querySelector('#mainContent');
                      return root && Array.from(root.querySelectorAll('tr')).some((row) => {
                        const text = (row.innerText || '').toLowerCase();
                        return text.includes('assignment') && text.includes('submitted');
                      });
                    }""",
                    timeout=4000,
                )
                entries = page.evaluate(SCAN_JS) or []
            except Exception:
                pass
    except Exception:
        return []
    for entry in entries:
        entry["course"] = course.get("title", "")
        entry["code"] = course.get("code", "")
    return entries


def scan_all(page: Page, exclude_patterns: list[str],
             courses: list[dict] | None = None) -> list[dict]:
    """Individual assignment links in each course's #mainContent only."""
    if courses is None:
        courses = scan_courses(page)

    merged: dict[str, dict] = {}
    for course in courses:
        if not course.get("href"):
            continue
        if is_excluded(course.get("title", ""), exclude_patterns):
            continue
        for entry in scan_course_assignments(page, course):
            entry["title"] = _guess_title(entry)
            if NON_ASSIGNMENT_WORDS.search(
                f"{entry['title']} {entry.get('context', '')}"
            ):
                continue
            href = entry.get("href", "")
            if not href:
                continue
            key = entry.get("rowKey") or assignment_key(href)
            if key in merged:
                merged[key].setdefault("course", course.get("title", ""))
                merged[key].setdefault("code", course.get("code", ""))
                if not merged[key].get("title"):
                    merged[key]["title"] = _guess_title(entry)
                if merged[key].get("submitted") is None:
                    merged[key]["submitted"] = entry.get("submitted")
            else:
                merged[key] = entry
    return list(merged.values())


def is_excluded(text: str, exclude_patterns: list[str]) -> bool:
    return any(re.search(pattern, text or "", re.IGNORECASE) for pattern in exclude_patterns)


def read_assignment(page: Page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    data = page.evaluate(PAGE_JS) or {}

    headings = [_clean(h) for h in data.get("headings", []) if _clean(h)]
    course = ""
    title = ""
    for heading in headings:
        if re.search(r"\b[A-Z]{2,6}\d{2,4}[A-Z]?\b", heading) or re.search(r"SY\d{4}", heading):
            course = course or heading
        elif ACTIVITY_WORDS.search(heading) and not title:
            title = heading
    if not course and headings:
        course = headings[0]
    if not title:
        title = headings[1] if len(headings) > 1 else (headings[0] if headings else data.get("title", ""))
    if not re.search(r"[A-Za-z]{3}", title or ""):
        title = _clean(data.get("title", "")) or title
    if re.search(r"\bSY\d{4}", course or ""):
        # A heading that is only the term is not a course name.
        course = ""

    submitted = bool(SUBMITTED_WORDS.search(data.get("text", "")))

    return {
        "url": url,
        "course": course,
        # Subject code such as INSY010, for titles that do not start with one.
        "code": subject_code(data.get("text", "")),
        "title": title,
        "instructions": data.get("text", ""),
        "links": data.get("links", []),
        "textareas": int(data.get("textareas", 0)) + int(data.get("editors", 0)),
        "file_inputs": data.get("fileInputs", []),
        "buttons": data.get("buttons", []),
        "submitted": submitted,
    }


def download_attachments(context, assignment: dict, temp_dir: Path) -> list[Path]:
    """Download PDF attachments through the browser session (keeps auth cookies)."""
    temp_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    seen: set[str] = set()
    for link in assignment.get("links", []):
        href = link.get("href", "")
        if not href or href in seen:
            continue
        if not re.search(r"\.pdf(\?|$)", href, re.IGNORECASE):
            continue
        seen.add(href)
        try:
            response = context.request.get(href, timeout=60000)
            if response.status != 200:
                continue
            body = response.body()
            if not body.startswith(b"%PDF"):
                continue
            name = unquote(Path(urlparse(href).path).name) or "attachment.pdf"
            if not name.lower().endswith(".pdf"):
                name += ".pdf"
            target = temp_dir / re.sub(r"[^A-Za-z0-9._-]+", "_", name)
            target.write_bytes(body)
            saved.append(target)
        except Exception:
            continue
    return saved


def collect_attachment_text(paths: list[Path]) -> tuple[str, list[Path]]:
    texts: list[str] = []
    images: list[Path] = []
    for path in paths:
        text, page_images = extract_pdf(path)
        if text:
            texts.append(f"--- {path.name} ---\n{text}")
        images.extend(page_images)
    return "\n\n".join(texts), images


def stage_answer(page: Page, assignment: dict, answer_pdf: Path,
                 answer_plain: str) -> dict:
    """Fill/upload the answer without ever clicking a submit control."""
    staged = {"uploaded": False, "filled_text": False, "submit_button_present": False}
    staged["submit_button_present"] = any(
        re.search(r"submit|turn\s*in|finish|finali[sz]e|post", b, re.IGNORECASE)
        for b in assignment.get("buttons", [])
    )

    if assignment.get("file_inputs"):
        try:
            inputs = page.locator("input[type=file]")
            for index in range(inputs.count()):
                item = inputs.nth(index)
                if not item.is_visible():
                    continue
                accept = (item.get_attribute("accept") or "").lower()
                if accept and "pdf" not in accept:
                    continue
                item.set_input_files(str(answer_pdf))
                staged["uploaded"] = True
                break
        except Exception as exc:
            staged["upload_error"] = str(exc)

    if assignment.get("textareas") and answer_plain.strip():
        try:
            target = None
            for selector in ("textarea", "[contenteditable='true']"):
                locator = page.locator(selector)
                for index in range(locator.count()):
                    item = locator.nth(index)
                    if item.is_visible():
                        target = item
                        break
                if target:
                    break
            if target:
                target.click()
                target.fill(answer_plain)
                staged["filled_text"] = True
        except Exception as exc:
            staged["fill_error"] = str(exc)

    return staged


def short_name(title: str) -> str:
    clean = _clean(title)
    match = re.match(r"^(\d{1,3})[.)\s-]+(.+)$", clean)
    number = match.group(1) if match else ""
    rest = (match.group(2) if match else clean).lower()

    labels = [
        ("task performance", "PT"),
        ("performance task", "PT"),
        ("seatwork", "SW"),
        ("activity", "ACT"),
        ("assignment", "ASG"),
        ("quiz", "QZ"),
        ("exam", "EXAM"),
        ("project", "PROJ"),
        ("output", "OUT"),
        ("laboratory", "LAB"),
        ("essay", "ESSAY"),
        ("reflection", "REFL"),
        ("discussion", "DISC"),
    ]
    label = next((value for key, value in labels if key in rest), None)
    if not label:
        words = [
            w for w in re.findall(r"[A-Za-z]+", rest)
            if w not in {"the", "a", "an", "and", "of", "in", "for", "to", "take",
                         "home", "with", "on", "at", "by", "my", "our"}
        ]
        label = "".join(w[0] for w in words[:3]).upper() or "TASK"

    name = f"{number} {label}" if number else label
    name = re.sub(r'[<>:"/\\|?*]+', "", name).strip(" .")
    return (name or "TASK")[:48]


def course_code(course: str) -> str:
    clean = _clean(course)
    # Skip term markers like SY2627 so they are never mistaken for a code.
    match = re.search(r"\b(?!SY\d{4}\b)([A-Z]{2,6}\d{2,4}[A-Z]?)\b", clean)
    if match:
        return match.group(1)
    first = clean.split(" ")[0] if clean else "COURSE"
    first = re.sub(r'[<>:"/\\|?*]+', "", first).strip(" .")
    return first[:24] or "COURSE"


def subject_code(text: str) -> str:
    """Pull the subject code from lines like 'INSY010 - CS501A' or 'INSY1010 · CS501A'."""
    for match in re.finditer(
            r"\b(?!SY\d{4}\b)([A-Z]{2,6}\d{3,4})\s*[-–·]\s*[A-Z]{2,6}\d{2,4}[A-Z]?\b",
            _clean(text)):
        return match.group(1)
    return ""


def course_folder(course: str, code: str = "") -> str:
    """Folder name: 'CS501A - Application Development and Emerging Technologies'.

    Some course titles have no code ('Intermediate Web Programming - SY2627-IT');
    for those, pass the code shown on the course card (e.g. COSC1030) or the
    subject becomes the folder name on its own.
    """
    clean = _clean(course)
    code = (code or "").strip()
    if not code:
        match = re.search(r"\b(?!SY\d{4}\b)([A-Z]{2,6}\d{2,4}[A-Z]?)\b", clean)
        code = match.group(1) if match else ""

    subject = clean
    if code and code in clean:
        subject = clean.split(code, 1)[1]
    # Terms appear as '- SY2627-IT', 'SY2026-2027-IT' or 'SY2024-2025-2T'.
    subject = re.sub(r"\s*[-–]?\s*S?Y?\d{4}\s*[-–]\s*\d{4}\s*[-–]\s*[\w]+\s*$", "",
                     subject, flags=re.IGNORECASE)
    subject = re.sub(r"\s*[-–]\s*S?Y?\d{4}\s*[-–]\s*\w+\s*$", "", subject, flags=re.IGNORECASE)
    subject = re.sub(r"\s*[-–]\s*\d{4}\s*$", "", subject)
    subject = re.sub(r"\s+", " ", subject).strip(" -–|")
    subject = re.sub(r'[<>:"/\\|?*]+', "", subject).strip(" .")

    if code and subject:
        return f"{code} - {subject}"[:90].strip(" .")
    if code:
        return code
    return subject[:90].strip(" .") or "COURSE"
