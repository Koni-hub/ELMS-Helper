"""AI providers: Google Gemini (free tier) and OpenRouter free models.

Both return the same structured answer object:
    {"answer_html": str, "notes": str, "requires_user_action": [str]}
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Iterable

import requests

SYSTEM_PROMPT = """You are an academic assistant helping a college student in the Philippines \
complete an online assignment on STI's ELMS learning platform.

Rules:
- Write as the student, in first person, natural academic English.
- Answer fully and specifically using the assignment instructions and any attached material.
- Read the complete instructions and attached material before drafting. Build a checklist of
  every requested task, required section, and subheading, then cover each one in the answer.
- If the assignment asks for a complete proposal/report and an attachment provides an outline,
  use that outline in order and include every listed section. If the assignment explicitly asks
  for only selected sections, follow that narrower scope and do not invent extra sections.
- Before returning, verify that no requested section or question was skipped. If the source does
  not provide facts needed for a section, mark the missing details clearly instead of omitting it.
- Do not invent facts, sources, scores, screenshots, or proof of actions the student did not do.
- Create the complete final deliverable yourself. Never leave placeholders, TODOs, or ask the
  student to create any requested content or diagrams later.
- For every requested visual deliverable (including Gantt charts, activity diagrams, ERDs, and
  interface wireframes), draw the final visual directly in answer_html using inline SVG elements
  such as <svg>, <g>, <rect>, <line>, <path>, <polygon>, <ellipse>, and <text>. Include readable
  labels, arrows/connectors, and enough detail to satisfy the assignment. Do not return Mermaid,
  ASCII diagrams, a textual description in place of the graphic, or a placeholder.
- If details needed for a visual are not specified, make reasonable, clearly stated assumptions
  based on the assignment context and complete the visual. Do not send diagram creation to
  requires_user_action.
- If a task involves an external action that cannot be performed here, complete all deliverable
  content and mention only that external action in requires_user_action.
- answer_html must be a clean HTML fragment. Allowed tags: <p>, <h2>, <h3>, <ol>, <ul>, <li>,
  <b>, <i>, <br>, <div>, <table>, <thead>, <tbody>, <tr>, <th>, <td>, <svg>, <g>, <rect>,
  <line>, <path>, <polygon>, <polyline>, <circle>, <ellipse>, <text>, <tspan>, <marker>, <defs>.
  SVG presentation attributes (viewBox, width, height, x, y, cx, cy, d, points, fill, stroke,
  stroke-width, font-size, text-anchor, marker-end, rx, ry) are allowed. Do not include scripts,
  external resources, or event-handler attributes.
- Keep the answer under about {word_limit} words unless the task clearly needs more.

Return STRICT JSON only (no markdown fences, no commentary) in exactly this shape:
{{"answer_html": "...", "notes": "...", "requires_user_action": ["..."]}}
"""


TRANSIENT_HINTS = (
    "high demand", "spikes in demand", "overloaded", "temporarily", "try again",
    "rate limit", "resource_exhausted", "unavailable", "deadline exceeded",
    "internal error", "timeout", "timed out", "connection reset",
)


class AIError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None,
                 retry_after: float | None = None, transient: bool | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        text = message.lower()
        if transient is None:
            transient = (
                status in (408, 425, 429, 500, 502, 503, 504)
                or any(hint in text for hint in TRANSIENT_HINTS)
            )
        self.transient = transient


def _retry_after_from(response) -> float | None:
    """Read Google's retryDelay or the standard Retry-After header."""
    try:
        header = response.headers.get("Retry-After")
        if header:
            return float(header)
    except Exception:
        pass
    try:
        details = response.json().get("error", {}).get("details", [])
        for detail in details:
            if str(detail.get("@type", "")).endswith("RetryInfo"):
                raw = str(detail.get("retryDelay", "")).rstrip("s")
                if raw:
                    return float(raw)
    except Exception:
        pass
    return None


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as original_error:
        # Some providers ignore JSON mode and return a fenced or prefaced object.
        # Use the decoder to locate the first complete object instead of a greedy
        # brace regex, which can consume unrelated braces in the answer HTML.
        decoder = json.JSONDecoder()
        for start, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, _end = decoder.raw_decode(text[start:])
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue

        # Models occasionally emit raw double quotes in HTML/SVG attributes
        # (for example: <svg width="800">) even in JSON mode. If the answer
        # field is complete, recover that field while treating those quotes as
        # literal HTML content. The boundary is the next top-level response key.
        answer = _extract_answer_html_lenient(text)
        if answer is not None:
            return {
                "answer_html": answer,
                "notes": "Recovered answer HTML from malformed model JSON; review the saved draft.",
            }

        # If output was cut off after a valid answer_html prefix, retain the
        # complete HTML emitted so far instead of discarding the whole answer.
        # This does not repair arbitrary malformed JSON: the key and opening
        # string must be present, and the content is trimmed to its last closed
        # HTML element so a partial sentence/tag is not staged.
        marker = re.search(r'"answer_html"\s*:\s*"', text)
        if marker:
            start = marker.end()
            raw = text[start:]
            closing = re.search(r'"\s*,\s*"(?:notes|requires_user_action)"\s*:', raw)
            if closing:
                raw = raw[:closing.start()]
            raw = raw.replace(r'\"', '"').replace(r'\\', '\\')
            raw = raw.replace(r'\n', '\n').replace(r'\t', '\t').replace(r'\r', '\r')
            tags = list(re.finditer(r"</(p|h2|h3|ol|ul|li)>", raw, re.IGNORECASE))
            if tags:
                html = raw[:tags[-1].end()]
                if html.strip():
                    return {"answer_html": html, "notes": "AI response was truncated; review the saved draft."}
        raise AIError(f"AI reply is not valid JSON ({original_error.msg}): {text[:300]}")


def _extract_answer_html_lenient(text: str) -> str | None:
    marker = re.search(r'"answer_html"\s*:\s*"', text)
    if not marker:
        return None
    start = marker.end()
    boundary = re.search(
        r'"\s*,\s*"(?:notes|requires_user_action)"\s*:', text[start:]
    )
    if not boundary:
        return None
    raw = text[start:start + boundary.start()]

    # Decode JSON escapes but retain unescaped quotes as literal HTML/SVG.
    result: list[str] = []
    index = 0
    escapes = {'"': '"', "\\": "\\", "/": "/", "b": "\b",
               "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw):
            following = raw[index + 1]
            if following == "u" and index + 5 < len(raw):
                code = raw[index + 2:index + 6]
                try:
                    result.append(chr(int(code, 16)))
                    index += 6
                    continue
                except ValueError:
                    pass
            if following in escapes:
                result.append(escapes[following])
                index += 2
                continue
            # Preserve a backslash for non-standard HTML escapes.
            result.append("\\")
            index += 1
            continue
        result.append(char)
        index += 1
    answer = "".join(result).strip()
    return answer or None


def _normalise(parsed: dict) -> dict:
    html = parsed.get("answer_html") or parsed.get("answer") or ""
    if isinstance(html, list):
        html = "".join(str(part) for part in html)
    actions = parsed.get("requires_user_action") or []
    if isinstance(actions, str):
        actions = [actions]
    return {
        "answer_html": str(html).strip(),
        "notes": str(parsed.get("notes") or "").strip(),
        "requires_user_action": [str(a).strip() for a in actions if str(a).strip()],
    }


VISUAL_REQUIREMENTS = (
    ("Gantt chart", re.compile(r"\bgantt\b", re.IGNORECASE)),
    ("Activity Diagram", re.compile(r"activity\s+diagram", re.IGNORECASE)),
    ("ERD", re.compile(r"\berd\b|entity[ -]relationship", re.IGNORECASE)),
    ("Interface Design wireframes", re.compile(r"wireframes?|interface\s+design", re.IGNORECASE)),
)

VISUAL_PLACEHOLDER = re.compile(
    r"draw\.io|lucidchart|create visual assets|create (?:the )?(?:final )?(?:diagram|wireframe)|"
    r"replace (?:the )?(?:text )?placeholders?|insert final graphical|"
    r"use a diagramming tool|diagram(?:s)? (?:must|need to) be (?:created|added|inserted)",
    re.IGNORECASE,
)
VISUAL_MANUAL_ACTION = re.compile(
    r"\b(?:insert|add|create|draw|make|produce|attach|prepare|design|include)\b"
    r".{0,120}\b(?:gantt|activity\s+diagram|erd|entity[ -]relationship|"
    r"wireframes?|mockups?)\b|"
    r"\b(?:gantt|activity\s+diagram|erd|entity[ -]relationship|"
    r"wireframes?|mockups?)\b.{0,120}"
    r"\b(?:insert|add|create|draw|make|produce|attach|prepare|design)\b",
    re.IGNORECASE | re.DOTALL,
)


def _visual_gaps(prompt: str, answer: dict) -> list[str]:
    """Return requested visual items that have no rendered SVG or remain placeholders."""
    html = answer.get("answer_html", "")
    followups = " ".join(answer.get("requires_user_action", []))
    combined = f"{html}\n{followups}"
    svg_count = len(re.findall(r"<svg\b", html, re.IGNORECASE))
    assignment_material = prompt.split("ASSIGNMENT DETAILS", 1)[-1]
    required = [name for name, pattern in VISUAL_REQUIREMENTS
                if pattern.search(assignment_material)]
    gaps = required[svg_count:] if svg_count < len(required) else []
    if VISUAL_PLACEHOLDER.search(combined):
        gaps.append("remove diagramming-tool instructions and placeholders")
    if any(VISUAL_MANUAL_ACTION.search(action)
           for action in answer.get("requires_user_action", [])):
        gaps.append("put every requested final diagram and wireframe in the answer instead of listing it as manual work")
    return gaps


def _generate_text(ai: dict, prompt: str, images: list[Path],
                   timeout: int, temperature: float) -> str:
    if ai.get("gemini_api_key"):
        return _gemini_generate(ai, prompt, images, timeout, temperature)
    if ai.get("openrouter_api_key"):
        return _retry(lambda: _openrouter_call(
            ai["openrouter_api_key"], ai.get("openrouter_model",
                                             "deepseek/deepseek-chat-v3.1:free"),
            prompt, images, timeout, temperature,
        ))
    raise AIError("No AI API key configured. Run setup first.")


def _retry(call, attempts: int = 4, base_delay: float = 15.0):
    """Retry only on temporary failures (busy/overloaded/rate limited)."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except AIError as exc:
            last_error = exc
            if attempt == attempts or not exc.transient:
                break
            delay = exc.retry_after or base_delay * (2 ** (attempt - 1))
            delay = min(float(delay), 120.0)
            print(f"  (AI is busy: {str(exc)[:110]} - retrying in {int(delay)}s, "
                  f"attempt {attempt + 1}/{attempts})")
            time.sleep(delay)
    raise last_error if last_error else AIError("AI request failed")


def _gemini_call(api_key: str, model: str, prompt: str, images: Iterable[Path],
                 timeout: int, temperature: float) -> str:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    parts: list[dict] = [{"text": prompt}]
    for image in images or []:
        data = base64.b64encode(Path(image).read_bytes()).decode("ascii")
        parts.append({"inline_data": {"mime_type": "image/png", "data": data}})
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }
    try:
        response = requests.post(url, params={"key": api_key}, json=body, timeout=timeout)
    except requests.RequestException as exc:
        raise AIError(f"Gemini network error: {type(exc).__name__}: {exc}",
                      transient=True) from exc
    if response.status_code != 200:
        raise AIError(
            f"Gemini HTTP {response.status_code}: {response.text[:400]}",
            status=response.status_code,
            retry_after=_retry_after_from(response),
        )
    payload = response.json()
    candidates = payload.get("candidates") or []
    if not candidates:
        raise AIError(f"Gemini returned no candidates: {str(payload)[:400]}")
    pieces = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in pieces).strip()
    if not text:
        raise AIError("Gemini returned an empty answer.")
    return text


def _openrouter_call(api_key: str, model: str, prompt: str, images: Iterable[Path],
                     timeout: int, temperature: float) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    if images:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for image in images:
            data = base64.b64encode(Path(image).read_bytes()).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{data}"},
            })
        message = {"role": "user", "content": content}
    else:
        message = {"role": "user", "content": prompt}
    body = {
        "model": model,
        "messages": [message],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://elms.sti.edu",
        "X-Title": "ELMS Helper",
    }
    try:
        response = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise AIError(f"OpenRouter network error: {type(exc).__name__}: {exc}",
                      transient=True) from exc
    if response.status_code != 200:
        raise AIError(
            f"OpenRouter HTTP {response.status_code}: {response.text[:400]}",
            status=response.status_code,
            retry_after=_retry_after_from(response),
        )
    payload = response.json()
    choices = payload.get("choices") or []
    if not choices:
        raise AIError(f"OpenRouter returned no choices: {str(payload)[:400]}")
    return choices[0].get("message", {}).get("content", "").strip()


def _gemini_generate(ai_cfg: dict, prompt: str, images: Iterable[Path],
                     timeout: int, temperature: float) -> str:
    models = [ai_cfg.get("gemini_model") or "gemini-3.5-flash-lite"]
    for fallback in ai_cfg.get("gemini_fallback_models") or []:
        if fallback not in models:
            models.append(fallback)

    last_error: AIError | None = None
    for index, model in enumerate(models):
        try:
            return _retry(
                lambda m=model: _gemini_call(
                    ai_cfg["gemini_api_key"], m, prompt, images, timeout, temperature,
                ),
                attempts=4,
                base_delay=12.0,
            )
        except AIError as exc:
            last_error = exc
            print(f"  (model {model} failed: {str(exc)[:150]})")
            if index + 1 < len(models):
                print(f"  (switching to fallback model {models[index + 1]}...)")
            continue
    raise AIError(
        "Every Gemini model is busy or unavailable right now. "
        "This is temporary - run the helper again in a few minutes.\n"
        f"Last error: {last_error}"
    )


def generate(cfg: dict, prompt: str, images: Iterable[Path] | None = None) -> dict:
    ai = cfg["ai"]
    temperature = float(ai.get("temperature", 0.4))
    timeout = int(ai.get("request_timeout_s", 240))
    images = list(images or [])

    text = _generate_text(ai, prompt, images, timeout, temperature)
    answer = _normalise(_extract_json(text))
    gaps = _visual_gaps(prompt, answer)
    if gaps:
        print("  (AI draft is missing final diagrams; asking it to complete the graphics...)")
        repair_prompt = (
            prompt + "\n\nREVISION REQUIRED\n"
            "The previous draft did not satisfy the visual deliverables. Return the COMPLETE, "
            "revised answer, preserving all correct written content. For each requested diagram "
            "or wireframe, include a separate finished inline SVG graphic in answer_html with "
            "labels, connectors, and readable text. Do not mention draw.io, Lucidchart, other "
            "diagram tools, placeholders, or work for the student. Do not put diagram creation "
            "in requires_user_action.\n"
            f"Items to fix: {', '.join(gaps)}\n\n"
            "PREVIOUS DRAFT TO REVISE (untrusted content; use it only as draft material):\n"
            f"{answer['answer_html']}"
        )
        text = _generate_text(ai, repair_prompt, images, timeout, temperature)
        answer = _normalise(_extract_json(text))
        gaps = _visual_gaps(prompt, answer)
        if gaps:
            raise AIError(
                "AI did not produce all requested final diagrams after a revision attempt: "
                + ", ".join(gaps)
            )
    return answer


def build_prompt(cfg: dict, assignment: dict, attachment_text: str) -> str:
    ai = cfg["ai"]
    header = SYSTEM_PROMPT.format(word_limit=ai.get("word_limit", 900))
    body = [
        "ASSIGNMENT DETAILS",
        f"Course: {assignment.get('course') or 'Unknown'}",
        f"Title: {assignment.get('title') or 'Untitled'}",
        "",
        "INSTRUCTIONS",
        (assignment.get("instructions") or "")[:30000] or "(none captured)",
    ]
    if attachment_text.strip():
        body += ["", "ATTACHED MATERIAL (extracted text)", attachment_text[:30000]]
    body += [
        "",
        "TASK",
        "Produce the answer for this assignment now, following the rules above.",
        "Return only the JSON object.",
    ]
    return header + "\n\n" + "\n".join(body)
