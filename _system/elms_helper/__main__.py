"""Command-line interface and menu for the ELMS Helper."""

from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime

import requests

from . import ai
from .browser import ELMSBrowser
from .config import (CONFIG_PATH, DEFAULTS, LOGS_DIR, POPPLER_DIR, PROFILE_DIR,
                     TEMPLATE_PATH, any_provider_key, load_config, log,
                     save_config)
from .elms import is_excluded, scan_all, scan_courses
from .run import run

BANNER = r"""
   ______   _        __  __    _____
  |  ____| | |      |  \/  |  / ____|
  | |__    | |      | \  / | | (___
  |  __|   | |      | |\/| |  \___ \
  | |____  | |____  | |  | |  ____) |
  |______| |______| |_|  |_| |_____/

           ELMS Assistant
"""


def setup() -> int:
    cfg = load_config()
    print(BANNER)
    print("SETUP")
    print("-" * 60)
    print("Free Gemini key: https://aistudio.google.com/apikey")
    print("(Create it with your Google account. It stays on this PC in config.json.)\n")

    current = cfg["ai"].get("gemini_api_key", "")
    hint = " (press Enter to keep the current key)" if current else ""
    key = input(f"Gemini API key{hint}: ").strip()
    if key:
        cfg["ai"]["gemini_api_key"] = key

    or_current = cfg["ai"].get("openrouter_api_key", "")
    print("\nOptional backup: OpenRouter key (free models). Leave blank to skip.")
    or_key = input("OpenRouter API key: ").strip()
    if or_key:
        cfg["ai"]["openrouter_api_key"] = or_key
    elif not or_current:
        pass

    name = input(f"\nStudent name [{cfg.get('student_name', '')}]: ").strip()
    if name:
        cfg["student_name"] = name

    model = input(f"Gemini model [{cfg['ai'].get('gemini_model')}]: ").strip()
    if model:
        cfg["ai"]["gemini_model"] = model

    save_config(cfg)

    if cfg["ai"].get("gemini_api_key"):
        print("\nChecking the Gemini key...")
        try:
            response = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": cfg["ai"]["gemini_api_key"]}, timeout=30,
            )
            if response.status_code == 200:
                print("  Key is valid.")
            else:
                print(f"  Key check returned HTTP {response.status_code}: {response.text[:200]}")
                print("  Setup saved anyway; fix the key later by running setup again.")
        except Exception as exc:
            print(f"  Could not reach Gemini: {exc}")
    elif not cfg["ai"].get("openrouter_api_key"):
        print("\nNo AI key saved. Answers cannot be generated until a key is added.")

    print(f"\nSaved: {CONFIG_PATH}")
    print("Next: menu option 2 to log in, then 3 to scan, then 4 to run.")
    return 0


def login() -> int:
    cfg = load_config()
    print("=" * 72)
    print(" LOG IN TO ELMS")
    print(" A Microsoft sign-in window will open in MICROSOFT EDGE.")
    print(" Type your STI account in THAT window - not in Brave or Chrome.")
    print("=" * 72)
    with ELMSBrowser(headless=False) as browser:
        browser.ensure_login()
    print("Login session saved. Future runs reuse it.")
    return 0


def scan() -> int:
    cfg = load_config()
    with ELMSBrowser(headless=True) as browser:
        if not browser.is_logged_in():
            print("\n  Not logged in. Run menu option 2 (Log in to ELMS) first.")
            return 3
        courses = scan_courses(browser.page)
        print(f"\nEnrolled courses: {len(courses)}")
        for index, course in enumerate(courses, start=1):
            code = course.get("code") or "-"
            print(f"  {index:<3}{code:<10}{course.get('title', '')[:72]}")
        entries = scan_all(browser.page, cfg["exclude_patterns"], courses)
    if not entries:
        print("No assignment detail links found in course #mainContent lists.")
        return 2
    pending_count = sum(entry.get("submitted") is False for entry in entries)
    submitted_count = sum(entry.get("submitted") is True for entry in entries)
    unknown_count = len(entries) - pending_count - submitted_count
    print(f"\nFound {len(entries)} individual assignment link(s):\n")
    print(f"Status totals: {pending_count} not submitted, {submitted_count} submitted, "
          f"{unknown_count} unknown. Option 4 processes not-submitted assignments only.\n")
    print(f"{'#':<4}{'Status':<22}Title")
    print("-" * 90)
    for index, entry in enumerate(entries, start=1):
        title = entry.get("title", "")[:60]
        excluded = is_excluded(title, cfg["exclude_patterns"])
        status = "SKIP (excluded group)" if excluded else "assignment"
        if not excluded:
            if entry.get("past_due"):
                status = "past due"
            elif entry.get("submitted") is True:
                status = "submitted"
            elif entry.get("submitted") is False:
                status = "not submitted"
            else:
                status = "status unknown"
        course = entry.get("course", "")
        label = f"{course} — {title}" if course else title
        print(f"{index:<4}{status:<22}{label}")
        print(f"    {entry.get('href', '')}")
    print("\nRead-only scan. Nothing was changed on ELMS.")
    return 0


def doctor() -> int:
    cfg = load_config()
    print("DOCTOR")
    print("-" * 60)
    checks: list[tuple[str, bool, str]] = []

    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright
        checks.append(("Playwright", True, "installed"))
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel="msedge", headless=True)
                browser.close()
            checks.append(("Edge automation", True, "works"))
        except Exception as exc:
            checks.append(("Edge automation", False, str(exc)[:160]))
    except Exception as exc:
        checks.append(("Playwright", False, str(exc)[:160]))

    checks.append(("AI key", any_provider_key(cfg),
                   "configured" if any_provider_key(cfg) else "missing - run setup"))
    poppler_ready = ((POPPLER_DIR / "pdftotext.exe").exists()
                     and (POPPLER_DIR / "pdftoppm.exe").exists())
    checks.append(("Poppler (optional)", True,
                   "installed" if poppler_ready else
                   "not installed; scanned PDF image extraction is unavailable"))
    checks.append(("Answer template", TEMPLATE_PATH.exists(), str(TEMPLATE_PATH)))
    checks.append(("Browser profile", PROFILE_DIR.exists(),
                   "created" if PROFILE_DIR.exists() else "created on first login"))
    checks.append(("Output root", True, str(__import__("pathlib").Path(cfg["_root"]))))
    try:
        from .pdfdoc import find_edge
        checks.append(("Microsoft Edge", True, str(find_edge())))
    except Exception as exc:
        checks.append(("Microsoft Edge", False, str(exc)))

    for name, ok, detail in checks:
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] {name:<18} {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def menu() -> int:
    cfg = load_config()
    while True:
        print(BANNER)
        print("1) Setup (free AI key + settings)")
        print("2) Log in to ELMS (first time)")
        print("3) Scan assignments (read-only)")
        print("4) Run helper (answer + save PDFs + stage, never submit)")
        print("5) Doctor (check installation)")
        print("0) Exit")
        choice = input("\nSelect: ").strip()
        if choice == "0":
            return 0
        try:
            if choice == "1":
                setup()
            elif choice == "2":
                login()
            elif choice == "3":
                scan()
            elif choice == "4":
                if not any_provider_key(load_config()):
                    print("No AI key yet. Run setup first.")
                    continue
                limit_raw = input("Max assignments this run [12]: ").strip()
                limit = int(limit_raw) if limit_raw.isdigit() else None
                run(load_config(), limit=limit)
                input("\nPress Enter to return to the menu...")
            elif choice == "5":
                doctor()
            else:
                print("Unknown choice.")
        except KeyboardInterrupt:
            print("\nCancelled.")
        except Exception as exc:  # noqa: BLE001 - keep the menu alive
            _report_crash(exc)


def _report_crash(exc: BaseException) -> int:
    """Print a short error, write full details to a log file, and stay visible."""
    detail = traceback.format_exc()
    log(f"FATAL {exc!r}\n{detail}")
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / f"crash-{datetime.now():%Y%m%d-%H%M%S}.log"
    try:
        path.write_text(
            f"ELMS Helper crash at {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"{detail}\n",
            encoding="utf-8",
        )
    except Exception:
        path = None

    print("\n" + "=" * 72)
    print("SOMETHING WENT WRONG")
    print("=" * 72)
    print(f"ERROR: {exc}")
    if path is not None:
        print(f"\nFull details saved to:\n  {path}")
        print(f"Debug log:\n  {LOGS_DIR / 'helper.log'}")
    print("\nThe window will stay open. Send me that details file and I'll fix it.")
    return 1


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="ELMS assignment helper")
    parser.add_argument("command", nargs="?",
                        choices=["setup", "login", "scan", "run", "doctor", "menu"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    log(f"START command={args.command or 'menu'} argv={argv}")
    try:
        if args.command == "setup":
            result = setup()
        elif args.command == "login":
            result = login()
        elif args.command == "scan":
            result = scan()
        elif args.command == "doctor":
            result = doctor()
        elif args.command == "run":
            cfg = load_config(require_key=not args.dry_run)
            result = run(cfg, limit=args.limit, dry_run=args.dry_run)
        else:
            result = menu()
    except KeyboardInterrupt:
        print("\nCancelled.")
        log("STOP keyboard interrupt")
        return 130
    except SystemExit as exc:
        log(f"STOP SystemExit {exc}")
        if isinstance(exc.code, str):
            print(f"\n{exc.code}")
            return 1
        raise
    except Exception as exc:  # noqa: BLE001 - top-level safety net
        return _report_crash(exc)

    log(f"STOP result={result}")
    return result


if __name__ == "__main__":
    sys.exit(main())
