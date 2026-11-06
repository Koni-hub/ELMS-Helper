"""Playwright browser with a persistent profile (log in once, stays logged in)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .config import LOGS_DIR, PROFILE_DIR, STATE_FILE, log

ELMS_HOME = "https://elms.sti.edu/"


class BrowserError(RuntimeError):
    pass


class ELMSBrowser:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
        self.context: BrowserContext | None = None
        self.authenticated = False

    def __enter__(self) -> "ELMSBrowser":
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        log(f"browser: starting Playwright headless={self.headless} profile={PROFILE_DIR}")
        try:
            self._pw = sync_playwright().start()
        except Exception as exc:
            log(f"browser: Playwright start failed: {exc!r}")
            raise BrowserError(f"Could not start Playwright.\nDetails: {exc}") from exc
        width, height = self._work_area()
        launch_args = [
            "--no-first-run",
            "--no-default-browser-check",
            "--window-position=0,0",
        ]
        if width and height:
            launch_args.append(f"--window-size={width},{height}")
        try:
            self.context = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="msedge",
                headless=self.headless,
                accept_downloads=True,
                chromium_sandbox=True,
                # no_viewport keeps the 1280x720 default viewport from being
                # forced, so the page follows the real window size instead of
                # rendering into a fixed box with dead grey space beside it.
                no_viewport=True,
                args=launch_args,
            )
        except Exception as exc:  # pragma: no cover - environment specific
            log(f"browser: launch failed: {exc!r}")
            text = str(exc)
            hint = ""
            if "ProcessSingleton" in text or "profile" in text.lower() and "lock" in text.lower():
                hint = "\nThe ELMS Helper browser profile is already in use."
            raise BrowserError(
                "Could not start the Edge browser for the helper.\n"
                f"Details: {exc}{hint}\n"
                "If another ELMS-Helper browser window is open, close it and retry.\n"
                "If Edge is missing, install Microsoft Edge and retry."
            ) from exc
        log(f"browser: launched, {len(self.context.pages)} page(s)")
        for page in self.context.pages[1:]:
            try:
                page.close()
            except Exception:
                pass
        self._restore_state()
        if not self.headless:
            self._maximise_window()
        return self

    @staticmethod
    def _work_area() -> tuple[int, int]:
        """Real desktop work area in physical pixels (Windows DPI aware)."""
        try:
            import ctypes
            from ctypes import wintypes

            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()

            class RECT(ctypes.Structure):
                _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                            ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

            rect = RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(rect), 0):
                return rect.right - rect.left, rect.bottom - rect.top
        except Exception as exc:
            log(f"login: work area lookup failed: {exc!r}")
        return 0, 0

    def _maximise_window(self) -> None:
        """Make the login window large and bring it to the front."""
        try:
            page = self.page
            session = self.context.new_cdp_session(page)
            window = session.send("Browser.getWindowForTarget")
            window_id = window["windowId"]
            # Use explicit physical bounds instead of windowState=maximized:
            # Edge keeps a 1280px renderer viewport otherwise, which leaves a
            # dead grey strip on the right of the screen.
            width, height = self._work_area()
            if width < 800 or height < 600:
                size = page.evaluate(
                    "({ w: screen.availWidth, h: screen.availHeight })")
                width, height = int(size["w"]), int(size["h"])
            session.send("Browser.setWindowBounds", {
                "windowId": window_id,
                "bounds": {"left": 0, "top": 0,
                           "width": int(width), "height": int(height)},
            })
            page.wait_for_timeout(500)
            inner = page.evaluate("({ w: innerWidth, h: innerHeight, d: devicePixelRatio })")
            log(f"login: window {width}x{height}, viewport {inner['w']}x{inner['h']} dpr={inner['d']}")
            page.bring_to_front()
            try:
                session.detach()
            except Exception:
                pass
            log("login: window maximized")
        except Exception as exc:
            log(f"login: maximize failed: {exc!r}")

    def _restore_state(self) -> None:
        """Re-add saved cookies (ELMS uses session cookies that die with the browser)."""
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            cookies = data.get("cookies") or []
            if cookies:
                self.context.add_cookies(cookies)
        except Exception:
            pass

    def save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.context.storage_state(path=str(STATE_FILE))
        except Exception:
            pass

    @property
    def page(self) -> Page:
        assert self.context is not None
        if not self.context.pages:
            return self.context.new_page()
        return self.context.pages[0]

    def goto(self, url: str, timeout_ms: int = 60000) -> Page:
        page = self.page
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        return page

    LOGIN_TOKENS = ("/log_in", "not_logged_in", "/auth/", "login.microsoftonline",
                    "adfs", "bridge/fido", "microsoftonline.com")

    def _page_url(self, page: Page) -> str:
        """Real URL from the renderer (page.url can stay stale after SSO)."""
        try:
            page.set_default_timeout(4000)
            real = page.evaluate("location.href")
            if real:
                return str(real)
        except Exception:
            pass
        try:
            return page.url
        except Exception:
            return ""

    def _page_logged_in(self, page: Page) -> bool:
        """Cheap check: an ELMS page with no Log in link means we are signed in."""
        url = self._page_url(page).lower()
        if "elms.sti.edu" not in url and "neolms.com" not in url:
            return False
        if any(token in url for token in self.LOGIN_TOKENS):
            return False
        try:
            page.set_default_timeout(5000)
            has_login_link = page.evaluate(
                "!!document.querySelector('a[href*=\"/log_in\"]')")
            return not has_login_link
        except Exception as exc:
            log(f"login: login link check failed on {url}: {exc!r}")
            return False

    def _request_logged_in(self) -> bool:
        """Fallback: ask ELMS directly with the browser's own cookies."""
        try:
            response = self.context.request.get(
                "https://elms.sti.edu/user_dashboard", timeout=20000)
        except Exception as exc:
            log(f"login: request check failed: {exc!r}")
            return False
        url = response.url.lower()
        if "/log_in" in url or "not_logged_in" in url:
            return False
        if response.status != 200:
            log(f"login: request check status {response.status}")
            return False
        try:
            html = response.text().lower()
        except Exception:
            return False
        return 'href="/log_in' not in html[:60000]

    def is_logged_in(self) -> bool:
        try:
            page = self.goto("https://elms.sti.edu/user_dashboard", timeout_ms=45000)
        except Exception as exc:
            log(f"login: dashboard check failed: {exc!r}")
            return False
        logged_in = self._page_logged_in(page)
        if logged_in:
            self.authenticated = True
        return logged_in

    def _clear_microsoft_cookies(self) -> None:
        """Drop stale Microsoft SSO cookies but keep an existing ELMS session."""
        try:
            cookies = self.context.cookies()
        except Exception as exc:
            log(f"login: reading cookies failed: {exc!r}")
            return
        microsoft = ("microsoftonline.com", "live.com", "microsoft.com",
                     "office.com", "msftauth", "msauth")
        cleared = 0
        for cookie in cookies:
            domain = (cookie.get("domain") or "").lower()
            if not any(key in domain for key in microsoft):
                continue
            try:
                self.context.clear_cookies(
                    name=cookie["name"], domain=cookie["domain"],
                    path=cookie.get("path", "/"),
                )
                cleared += 1
            except TypeError:
                # Older Playwright without filtered clear_cookies.
                try:
                    self.context.clear_cookies()
                except Exception:
                    pass
                log("login: cleared all cookies (old Playwright API)")
                return
            except Exception:
                pass
        log(f"login: cleared {cleared} Microsoft cookie(s)")

    def ensure_login(self, timeout_s: int = 900) -> None:
        page = self.page
        try:
            page.goto(ELMS_HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as exc:
            log(f"login: opening ELMS home failed: {exc!r}")
        log(f"login: home url={page.url}")

        try:
            logged_in = self.is_logged_in()
        except Exception as exc:
            log(f"login: login check failed: {exc!r}")
            logged_in = False
        if logged_in:
            print("  Already logged in to ELMS.")
            log("login: already logged in")
            self.save_state()
            return

        # Stale Microsoft sign-in cookies make the SSO flow jump to a
        # passkey/FIDO bridge page, but clearing everything also throws away a
        # valid ELMS session, so only the Microsoft cookies are dropped.
        self._clear_microsoft_cookies()

        # Go straight to the Microsoft Entra sign-in route (the ELMS page's own
        # button can fail because its page JavaScript errors).
        try:
            page.goto("https://elms.sti.edu/auth/microsoft_office365",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
        except Exception as exc:
            log(f"login: SSO redirect failed: {exc!r}")
        log(f"login: after SSO url={page.url}")
        if not self.headless:
            self._maximise_window()
            try:
                page.bring_to_front()
            except Exception:
                pass
        if "elms.sti.edu" in page.url and "/log_in" not in page.url and "microsoft" not in page.url:
            # Fall back to the login form if the SSO route did not redirect.
            try:
                page.goto("https://elms.sti.edu/log_in/form",
                          wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass

        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        screenshot = LOGS_DIR / "login-wait.png"
        try:
            page.screenshot(path=str(screenshot))
        except Exception:
            pass

        print("\n  Not logged in yet.")
        print(f"  Sign-in page: {page.url}")
        print(f"  Screenshot: {screenshot}")
        print("  >>> TYPE YOUR STI ACCOUNT IN THE HELPER'S EDGE WINDOW <<<")
        print("  Look for the Edge window titled 'Sign in to your account'.")
        print("  It has its own profile, so signing in to your normal Edge/Chrome")
        print("  window does NOT count and the helper will keep waiting.")
        print("  Enter your STI email, password, and any MFA prompt.")
        print(f"  Waiting up to {timeout_s // 60} minutes...")

        deadline = time.time() + timeout_s
        seen_urls: set[str] = set()
        tries = 0
        while time.time() < deadline:
            time.sleep(3)
            tries += 1
            try:
                pages = [p for p in self.context.pages if not p.is_closed()]
            except Exception as exc:
                log(f"login: reading pages failed: {exc!r}")
                pages = []
            if not pages:
                log("login: all browser windows were closed")
                raise BrowserError(
                    "The ELMS Helper browser window was closed before login finished.\n"
                    "Run menu option 2 (Log in to ELMS) again."
                )
            for candidate in pages:
                url = self._page_url(candidate)
                if url not in seen_urls:
                    seen_urls.add(url)
                    log(f"login: tab url={url}")
                if self._page_logged_in(candidate):
                    print("  Login detected. Continuing.")
                    log("login: detected")
                    self.authenticated = True
                    self.save_state()
                    return
            # Every 5th pass ask ELMS directly too; page state can go stale
            # when the SSO callback swaps the tab.
            if tries % 5 == 0:
                log(f"login: heartbeat {tries} urls={sorted(seen_urls)}")
                if self._request_logged_in():
                    print("  Login detected. Continuing.")
                    log("login: detected via request")
                    self.authenticated = True
                    self.save_state()
                    return
        log("login: timed out")
        raise BrowserError("Login was not completed in time. Run the helper again when ready.")

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self.context is not None:
                # Never overwrite a good saved session with a logged-out one.
                if self.authenticated:
                    self.save_state()
                else:
                    log("browser: not saving session state (not logged in)")
                try:
                    self.context.close()
                except Exception as exc:
                    log(f"browser: close failed: {exc!r}")
        finally:
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception as exc:
                    log(f"browser: playwright stop failed: {exc!r}")
