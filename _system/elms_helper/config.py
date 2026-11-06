"""Configuration loading/writing for the ELMS Helper."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # ...\ELMS-HELP\_system
ROOT_DIR = BASE_DIR.parent                                  # ...\ELMS-HELP
CONFIG_PATH = BASE_DIR / "config.json"
PROFILE_DIR = BASE_DIR / ".browser-profile"
STATE_FILE = PROFILE_DIR / "session-state.json"
LOGS_DIR = BASE_DIR / "logs"
TEMPLATE_PATH = BASE_DIR / "templates" / "answer.html"
POPPLER_DIR = BASE_DIR / "tools" / "poppler"

DEFAULTS = {
    "student_name": "Argie Delgado",
    "ai": {
        "provider": "gemini",
        "gemini_api_key": "",
        "gemini_model": "gemini-3.5-flash-lite",
        "gemini_fallback_models": ["gemini-3.1-flash-lite", "gemini-3.8-flash", "gemini-3.7-flash"],
        "openrouter_api_key": "",
        "openrouter_model": "deepseek/deepseek-chat-v3.1:free",
        "request_timeout_s": 240,
        "temperature": 0.4,
        "word_limit": 2500,
    },
    "exclude_patterns": [r"teacher\s*evaluation", r"student\s*grade\s*monitoring"],
    "stage_only": True,
    "headless": True,
    "max_assignments": 12,
    "pdf": {
        "font": "Times New Roman",
        "size_pt": 12,
        "color": "#000000",
        "margin_in": 1.0,
        "page": "A4",
    },
}


LOG_FILE = LOGS_DIR / "helper.log"
DEBUG = bool(os.environ.get("ELMS_DEBUG"))


def log(message: str) -> None:
    """Append a timestamped line to logs/helper.log (never raises)."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
    except Exception:
        pass


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(require_key: bool = False) -> dict:
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"config.json is invalid JSON: {exc}") from exc
    else:
        user_cfg = {}
    cfg = _merge(DEFAULTS, user_cfg)
    cfg["_root"] = str(ROOT_DIR)
    cfg["_system"] = str(BASE_DIR)
    if require_key and not any_provider_key(cfg):
        raise SystemExit(
            "No AI API key configured.\n"
            "Run setup first (menu option 1) and paste a free Gemini key from\n"
            "https://aistudio.google.com/apikey"
        )
    return cfg


def save_config(cfg: dict) -> None:
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(clean, indent=2), encoding="utf-8")


def any_provider_key(cfg: dict) -> bool:
    ai = cfg.get("ai", {})
    return bool(ai.get("gemini_api_key") or ai.get("openrouter_api_key"))
