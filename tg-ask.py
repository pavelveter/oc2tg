#!/usr/bin/env python3
"""tg-ask — ask a question in Telegram and get an answer.

Modes:
  notify <text>   send a message; stdout one JSON line: {"ok":true,"message_id":N}
  ask              JSON request on stdin, JSON response on stdout (see below)
  --daemon         run the ephemeral coordinator (owns Telegram polling)
  --selfcheck      offline checks (no network)
  --check          installation diagnostics: creds, bot, chat type (needs network)
  --whoami         chat ids from messages to the bot (for filling .env)

Bot data taken from env TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID,
otherwise from .env next to the script.

ask request:
  {"text": "title", "timeout": 900, "context": "project",
   "fields": [{"key": "k", "title": "Question?", "type": "string",
               "options": [{"label": "Yes", "value": "yes"}], "custom": true,
               "required": true, "external": false}]}
Empty fields — free text; answer comes in answers["answer"].
Fields with options are shown as buttons; type=multiselect toggles buttons
and has a "Done" button.

ask response:
  {"ok": true, "answers": {"k": "yes"}}            — answers (values already coerced to Form.Value)
  {"ok": false, "reason": "timeout", "field": "k"} — didn't answer in time
  {"ok": false, "reason": "invalid"|"config"|"error", "error": "..."}
When parsing an unambiguous choice, trailing text of the reply is returned as
answers["k_note"] (e.g. "3 because dangerous" -> reject + reason).

Exit code: 0 — ok:true, 1 — any ok:false.
"""

from __future__ import annotations

import argparse
import fcntl
import html
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOCK_PATH = SCRIPT_DIR / ".tg-ask.lock"
DAEMON_LOCK_PATH = SCRIPT_DIR / ".tg-ask.daemon.lock"
SOCKET_PATH = SCRIPT_DIR / "tg-ask.sock"
STATE_PATH = SCRIPT_DIR / "tg-ask.state.json"
LOG_PATH = SCRIPT_DIR / "tg-ask.log"

PRE_TITLE = "🤖 <b>OpenCode</b>"
ANSWER_MARK = "✅ <b>Accepted</b>"
EXPIRED_MARK = "⌛️ <i>Timed out, no need to answer anymore.</i>"
MAX_EDIT = 3900  # margin under Telegram's 4096-char edit limit
CHECKED = "✅"
UNCHECKED = "⬜️"
DONE_LABEL = "Done"
PART_LIMIT = 3500  # margin under Telegram's 4096-char limit
MAX_PARTS = 6
MAX_FIELDS = 20
TRUE_WORDS = {"y", "yes", "true", "t", "1", "+", "ok", "yeah", "yep"}
FALSE_WORDS = {"n", "no", "false", "f", "0", "-", "nope"}
SKIP_WORDS = {"-", "skip", "pass", "none"}


class TgError(Exception):
    pass


def log_event(**fields: object) -> None:
    """Bridge log: written by both the script and the OpenCode plugin."""
    try:
        record = {"ts": round(time.time(), 3), **fields}
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --- .env and credentials --------------------------------------------------
def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def env_values() -> dict[str, str]:
    """Values from .env next to the script; environment variables take priority."""
    values: dict[str, str] = {}
    env_file = SCRIPT_DIR / ".env"
    if env_file.is_file():
        values = parse_env(env_file.read_text(encoding="utf-8"))
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        from_env = os.environ.get(key)
        if from_env:
            values[key] = from_env
    return values


def env_value(key: str) -> str:
    """Environment variable wins over .env next to the script."""
    return (os.environ.get(key) or env_values().get(key) or "").strip()


def env_number(key: str, default: float) -> float:
    """Positive number from env; empty, invalid or non-positive — default."""
    try:
        value = float(env_value(key))
    except ValueError:
        return default
    return value if value > 0 else default


# --- tunables from .env ----------------------------------------------------
DEFAULT_TIMEOUT = env_number("TG_ASK_TIMEOUT", 900.0)  # wait for a reply, seconds
IDLE_TIMEOUT = env_number("TG_ASK_IDLE_TIMEOUT", 60.0)  # daemon exits after this idleness
HTTP_TIMEOUT = env_number("TG_ASK_HTTP_TIMEOUT", 30.0)  # one Telegram API request
POLL_TIMEOUT = max(1, int(env_number("TG_ASK_POLL_TIMEOUT", 20)))  # getUpdates long poll
MAX_ATTEMPTS = max(1, int(env_number("TG_ASK_MAX_ATTEMPTS", 3)))
MAX_INPUTS = max(1, int(env_number("TG_ASK_MAX_INPUTS", 25)))  # guard against infinite button loop


def credentials() -> tuple[str, str]:
    values = env_values()
    token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = values.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise TgError(f"missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID (check {SCRIPT_DIR / '.env'})")
    return token, chat_id


# --- Telegram --------------------------------------------------------------
class Telegram:
    def __init__(self, token: str, chat_id: str, thread_id: str | None = None) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.is_group = self.chat_id.startswith("-")
        if thread_id is None:
            thread_id = env_values().get("TELEGRAM_THREAD_ID", "").strip()
        self.thread_id = thread_id or None

    def call(self, method: str, params: dict, timeout: float) -> object:
        body = json.dumps(params).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            try:
                payload = json.loads(detail)
            except ValueError:
                raise TgError(f"{method}: HTTP {error.code}: {detail[:200]}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise TgError(f"{method}: {error}") from error
        if not payload.get("ok"):
            raise TgError(f"{method}: {payload.get('description') or payload}")
        return payload.get("result")

    def send(self, text: str, keyboard: list[list[dict]] | None = None) -> list[int]:
        """Send text (with or without keyboard), return ids of sent messages."""
        parts = split_message(text)
        message_ids = []
        for index, part in enumerate(parts):
            params: dict = {"chat_id": self.chat_id, "rich_message": {"html": part}}
            if self.thread_id:
                try:
                    params["message_thread_id"] = int(self.thread_id)
                except (TypeError, ValueError):
                    pass
            if keyboard:
                # keyboard attaches to the last part, otherwise to the message without options
                if index == len(parts) - 1:
                    params["reply_markup"] = {"inline_keyboard": keyboard}
            result = self.call("sendRichMessage", params, HTTP_TIMEOUT)
            assert isinstance(result, dict)
            message_ids.append(int(result["message_id"]))
        return message_ids

    def edit(self, message_id: int, text: str, keyboard: list[list[dict]] | None = None) -> None:
        """Edit a message. keyboard=None removes keyboard, a button list replaces it."""
        params = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "rich_message": {"html": text},
            "reply_markup": {"inline_keyboard": keyboard or []},
        }
        if self.thread_id:
            try:
                params["message_thread_id"] = int(self.thread_id)
            except (TypeError, ValueError):
                pass
        self.call("editMessageText", params, HTTP_TIMEOUT)

    def best_effort(self, what: str, function, *args) -> None:
        try:
            function(*args)
        except TgError as error:
            log_event(kind=f"{what}-failed", error=str(error))

    def react(self, message_id: int, emoji: str) -> None:
        self.best_effort("react", self.call, "setMessageReaction", {"chat_id": self.chat_id, "message_id": message_id, "reaction": [{"type": "emoji", "emoji": emoji}]}, HTTP_TIMEOUT)

    def answer_button(self, callback_id: str, note: str = "") -> None:
        self.best_effort("answer-button", self.call, "answerCallbackQuery", {"callback_query_id": callback_id, "text": note[:200]}, HTTP_TIMEOUT)

    def group_privacy_on(self) -> bool:
        """In a group with Privacy mode, the bot only receives replies to its own messages (getMe)."""
        if not self.is_group:
            return False
        try:
            me = self.call("getMe", {}, HTTP_TIMEOUT)
            return not bool((me or {}).get("can_read_all_group_messages"))
        except TgError as error:
            log_event(kind="getme-failed", error=str(error))
            return True

    def drain(self) -> int:
        """Drop accumulated updates, return offset for the next request."""
        offset = 0
        for _ in range(20):
            updates = self.call("getUpdates", {"offset": offset, "timeout": 0, "limit": 100, "allowed_updates": UPDATES}, HTTP_TIMEOUT)
            if not updates:
                break
            offset = int(updates[-1]["update_id"]) + 1
        return offset


UPDATES = ["message", "callback_query"]


# --- texts, buttons, parsing replies ---------------------------------------
def split_message(text: str) -> list[str]:
    """Split text into parts under Telegram's limit, preferably on line boundaries."""
    parts: list[str] = []
    rest = text.strip()
    while rest and len(parts) < MAX_PARTS:
        if len(rest) <= PART_LIMIT:
            parts.append(rest)
            break
        cut = rest.rfind("\n", 0, PART_LIMIT)
        if cut < PART_LIMIT // 2:
            cut = rest.rfind(" ", 0, PART_LIMIT)
        if cut < PART_LIMIT // 2:
            cut = PART_LIMIT
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest and parts:
        parts[-1] = parts[-1][: PART_LIMIT - 1] + "…"
    return parts or [""]


def normalize_fields(raw: object) -> list[dict]:
    """Validate and coerce the field description from the plugin into internal form."""
    fields: list[dict] = []
    if not isinstance(raw, list):
        return fields
    for item in raw[:MAX_FIELDS]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        kind = str(item.get("type") or "string")
        if kind not in ("string", "number", "integer", "boolean", "multiselect"):
            kind = "string"
        options = []
        for option in (item.get("options") or [])[:50]:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or "").strip()
            value = option.get("value", label)
            if label:
                options.append(
                    {"label": label, "value": value, "description": str(option.get("description") or "").strip()}
                )
        fields.append(
            {
                "key": key,
                "title": str(item.get("title") or "").strip(),
                "type": kind,
                "options": options,
                "custom": bool(item.get("custom")),
                "required": bool(item.get("required", True)),
                "external": bool(item.get("external")),
                "url": str(item.get("url") or ""),
                "minItems": item.get("minItems"),
                "maxItems": item.get("maxItems"),
                "pattern": str(item.get("pattern") or ""),
                "minLength": item.get("minLength"),
                "maxLength": item.get("maxLength"),
            }
        )
    return fields


def match_option(options: list[dict], raw: str) -> tuple[object, str] | None:
    """Match a reply to a list item: number, label, or value."""
    text = raw.strip()
    numbered = re.match(r"^(\d+)\s*[.)]?\s*(.*)$", text, re.S)
    if numbered:
        index = int(numbered.group(1))
        if 1 <= index <= len(options):
            return options[index - 1]["value"], numbered.group(2).strip()
    lowered = text.lower()
    for option in options:
        for candidate in (option["label"], option["value"]):
            token = str(candidate).strip().lower()
            if not token:
                continue
            if lowered == token:
                return option["value"], ""
            if lowered.startswith(token):
                return option["value"], text[len(token) :].lstrip(" :,—-\t").strip()
    return None


def parse_answer(field: dict, raw: str) -> tuple[str, object, str]:
    """-> ("ok", value, note) | ("skip", None, "") | ("invalid", None, reason)."""
    text = raw.strip()
    options = field["options"]
    required = field["required"]
    if text.lower() in SKIP_WORDS:
        if required:
            return "invalid", None, "this is a required question"
        return "skip", None, ""
    if field["type"] == "boolean":
        lowered = text.lower()
        if lowered in TRUE_WORDS:
            return "ok", True, ""
        if lowered in FALSE_WORDS:
            return "ok", False, ""
        return "invalid", None, "answer \"yes\" or \"no\""
    if options:
        if field["type"] == "multiselect":
            tokens = [token.strip() for token in re.split(r"[,;\n]+", text) if token.strip()]
            picked: list[str] = []
            for token in tokens:
                one = match_option(options, token)
                if not one:
                    picked = []
                    break
                picked.append(str(one[0]))
            if picked:
                bounds = multiselect_bounds(field)
                if bounds and not (bounds[0] <= len(picked) <= bounds[1]):
                    return "invalid", None, f"need to choose between {bounds[0]} and {bounds[1]}"
                return "ok", picked, ""
        matched = match_option(options, text)
        if matched:
            return "ok", matched[0], matched[1]
        if field["custom"]:
            return validate_string(field, text)
        return "invalid", None, "choose an item from the list"
    if field["type"] == "multiselect":
        tokens = [token.strip() for token in re.split(r"[,;\n]+", text) if token.strip()]
        if not tokens:
            return ("invalid", None, "at least one option needed") if required else ("skip", None, "")
        return "ok", tokens, ""
    if field["type"] in ("number", "integer"):
        cleaned = text.replace(",", ".").replace(" ", "")
        try:
            number = float(cleaned)
        except ValueError:
            return "invalid", None, "answer with a number"
        if field["type"] == "integer":
            if number != int(number):
                return "invalid", None, "answer with an integer"
            return "ok", int(number), ""
        return "ok", number, ""
    return validate_string(field, text)


def multiselect_bounds(field: dict) -> tuple[int, int] | None:
    minimum = int(field["minItems"]) if field["minItems"] else 1
    maximum = int(field["maxItems"]) if field["maxItems"] else max(1, len(field["options"]))
    if minimum > maximum:
        return None
    return minimum, maximum


def validate_string(field: dict, text: str) -> tuple[str, object, str]:
    if not text:
        if field["required"]:
            return "invalid", None, "answer can't be empty"
        return "skip", None, ""
    if field["pattern"] and not re.fullmatch(field["pattern"], text):
        return "invalid", None, "answer doesn't match the required format"
    if field["minLength"] is not None and len(text) < int(field["minLength"]):
        return "invalid", None, f"minimum {field['minLength']} characters"
    if field["maxLength"] is not None and len(text) > int(field["maxLength"]):
        return "invalid", None, f"maximum {field['maxLength']} characters"
    return "ok", text, ""


def button_data(token: int, choice: object) -> str:
    return f"c{token}:{choice}"


def parse_button_data(data: str) -> tuple[int, str] | None:
    matched = re.fullmatch(r"c(\d+):(done|\d+)", data.strip())
    if not matched:
        return None
    return int(matched.group(1)), matched.group(2)


def keyboard_for(field: dict, token: int, selected: set[int] | None = None) -> list[list[dict]]:
    """Option buttons: rows of at most two buttons; for multiselect — with "Done"."""
    selected = selected or set()
    rows: list[list[dict]] = []
    row: list[dict] = []
    for index, option in enumerate(field["options"], start=1):
        label = option["label"]
        if field["type"] == "multiselect":
            label = f"{CHECKED if index in selected else UNCHECKED} {label}"
        row.append({"text": label[:60], "callback_data": button_data(token, index)})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if field["type"] == "multiselect":
        rows.append([{"text": DONE_LABEL, "callback_data": button_data(token, "done")}])
    return rows


def _inline_code(escaped: str) -> str:
    return re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)


def option_html(option: dict) -> str:
    """Option label with its description on the next line (empty descriptions are skipped).

    The blank line after the description keeps it from running into the next list item.
    """
    label = html.escape(option["label"])
    description = option.get("description") or ""
    if not description:
        return label
    return f"{label}<br><i>{_inline_code(html.escape(description))}</i><br><br>"


def format_text(text: str) -> str:
    """Render plain text as paragraphs, turning fenced/inline code into code blocks."""
    source = str(text)
    blocks: list[str] = []
    last = 0
    for match in re.finditer(r"```([\w+-]*)\n(.*?)```", source, re.S):
        before = source[last : match.start()]
        if before.strip():
            blocks.append(f"<p>{_inline_code(html.escape(before))}</p>")
        language = match.group(1)
        code = html.escape(match.group(2).rstrip("\n"))
        attr = f' class="language-{language}"' if language else ""
        blocks.append(f"<pre><code{attr}>{code}</code></pre>")
        last = match.end()
    rest = source[last:]
    if rest.strip():
        blocks.append(f"<p>{_inline_code(html.escape(rest))}</p>")
    return "\n".join(blocks) or "<p></p>"


def build_prompt(header: str, field: dict, context: str, reply_required: bool, code: str = "") -> str:
    e = html.escape
    blocks = ["<h2>🤖 OpenCode</h2>"]
    if context:
        blocks.append(f"<h3>{e(context)}</h3>")
    blocks.append(format_text(header or field["title"] or field["key"]))
    if field["title"] and field["title"] != header:
        blocks.append(f"<p><i>{e(field['title'])}</i></p>")
    if code:
        blocks.append(f"<pre><code>{e(code)}</code></pre>")
    if field["external"]:
        if field["url"]:
            url = field["url"]
            blocks.append(f'<p><a href="{html.escape(url, quote=True)}">{e(url)}</a></p>')
        return "\n".join(blocks)
    if field["options"]:
        blocks.append("<br>")  # line break so the list does not sit on the question text
        if field["type"] == "multiselect":
            blocks.append("<p>Select options:</p>")
            blocks.append("<ul>")
            for option in field["options"]:
                blocks.append(f'<li><input type="checkbox">{option_html(option)}</li>')
            blocks.append("</ul>")
        else:
            blocks.append("<ol>")
            for option in field["options"]:
                blocks.append(f"<li>{option_html(option)}</li>")
            blocks.append("</ol>")
    if field["type"] == "boolean":
        blocks.append('<p>Answer "yes" or "no".</p>')
    elif field["type"] == "multiselect" and field["options"]:
        blocks.append('<p>Mark options and press "Done".</p>')
    elif field["options"]:
        blocks.append("<p>Press a button or send a number/text.</p>")
    elif field["type"] == "multiselect":
        blocks.append("<p>Answer with options separated by commas.</p>")
    else:
        blocks.append("<p>Answer with text.</p>")
    if not field["required"]:
        blocks.append('<p>To skip — send "-".</p>')
    if reply_required and (not field["options"] or field["custom"]):
        blocks.append("<p>Bot can't see regular messages in this group: reply by swiping (Reply) to this message.</p>")
    return "\n".join(blocks)


def label_for(field: dict, value: object) -> str:
    for option in field["options"]:
        if option["value"] == value:
            return option["label"]
    if isinstance(value, list):
        return ", ".join(label_for(field, item) for item in value)
    return str(value)


def answered_text(prompt: str, answer: str) -> str:
    """Message after reply: original question on top, answer below (chat history)."""
    tail = f"\n\n<hr/>\n<p>{ANSWER_MARK}: {html.escape(str(answer))}</p>"
    room = MAX_EDIT - len(tail)
    if len(prompt) <= room:
        head = prompt
    else:
        head = prompt[: room - 1].rstrip() + "…"
    return head + tail


# --- dialog ----------------------------------------------------------------
class Dialog:
    def __init__(self, telegram: Telegram, request: dict, update_queue: queue.Queue, clock=time.monotonic) -> None:
        self.tg = telegram
        self.text = str(request.get("text") or "").strip()
        self.context = str(request.get("context") or "").strip()
        self.code = str(request.get("code") or "").strip()
        self.fields = normalize_fields(request.get("fields") or [])
        try:
            timeout = float(request.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        self.deadline = clock() + min(max(timeout, 5.0), 86400.0)
        self.update_queue = update_queue
        self.message_ids: set[int] = set()
        self.awaiting = False
        self.id: str | None = None
        self.token = 0

    def wait_reply(self) -> dict | None:
        self.awaiting = True
        try:
            while True:
                left = self.deadline - time.monotonic()
                if left <= 0:
                    return None
                try:
                    return self.update_queue.get(timeout=min(left, 1.0))
                except queue.Empty:
                    continue
        finally:
            self.awaiting = False

    def expire(self, message_id: int, prompt: str) -> None:
        self.tg.best_effort("expire", self.tg.edit, message_id, f"{prompt}\n\n{EXPIRED_MARK}")
        self.tg.best_effort("react", self.tg.react, message_id, "👎")

    def run(self) -> dict:
        if not self.fields:
            return self.run_text_only()
        answers: dict[str, object] = {}
        for index, field in enumerate(self.fields):
            header = self.text if index == 0 else ""
            if field["external"]:
                ids = self.tg.send(build_prompt(field["title"], field, self.context, self.reply_required, self.code))
                # reply_required not set yet in this path; compute lazily below
                self.message_ids.update(ids)
                continue
            status, value, note = self.ask_field(field, header)
            if status == "timeout":
                return {"ok": False, "reason": "timeout", "field": field["key"]}
            if status == "invalid":
                return {"ok": False, "reason": "invalid", "field": field["key"], "error": note}
            if status == "skip":
                continue
            answers[field["key"]] = value
            if note:
                answers[f"{field['key']}_note"] = note
        return {"ok": True, "answers": answers}

    def run_text_only(self) -> dict:
        free_text = normalize_fields([{"key": "answer", "type": "string"}])[0]
        prompt = build_prompt(self.text, free_text, self.context, self.reply_required, self.code)
        base = split_message(prompt)[-1]
        message_ids = self.tg.send(prompt)
        self.message_ids.update(message_ids)
        if message_ids:
            self.tg.best_effort("react", self.tg.react, message_ids[-1], "👀")
        reply = self.wait_reply()
        if not reply or reply.get("kind") != "text":
            self.expire(message_ids[-1], base)
            return {"ok": False, "reason": "timeout"}
        answer = reply["text"]
        self.tg.best_effort("confirm", self.tg.edit, message_ids[-1], answered_text(base, answer))
        self.tg.best_effort("react", self.tg.react, message_ids[-1], "👍")
        return {"ok": True, "answers": {"answer": answer}, "raw": answer}

    def ask_field(self, field: dict, header: str) -> tuple[str, object, str]:
        self.token += 1
        token = self.token
        has_buttons = bool(field["options"])
        selected: set[int] = set()
        prompt = build_prompt(header, field, self.context, self.reply_required, self.code)
        base = split_message(prompt)[-1]
        keyboard = keyboard_for(field, token, selected) if has_buttons else None
        message_ids = self.tg.send(prompt, keyboard)
        self.message_ids.update(message_ids)
        message_id = message_ids[-1]
        self.tg.best_effort("react", self.tg.react, message_id, "👀")
        attempts = 0
        inputs = 0
        while inputs < MAX_INPUTS:
            reply = self.wait_reply()
            if reply is None:
                self.expire(message_id, base)
                return "timeout", None, ""
            inputs += 1
            if reply.get("kind") == "button":
                parsed = parse_button_data(str(reply.get("data")))
                if parsed is None or parsed[0] != token:
                    self.tg.answer_button(reply["id"], "Button expired")
                    continue
                _, choice = parsed
                if choice == "done":
                    bounds = multiselect_bounds(field)
                    if bounds and not (bounds[0] <= len(selected) <= bounds[1]):
                        self.tg.answer_button(reply["id"], f"Need to choose between {bounds[0]} and {bounds[1]}")
                        continue
                    if not selected:
                        self.tg.answer_button(reply["id"], "Nothing selected")
                        continue
                    value = [field["options"][index - 1]["value"] for index in sorted(selected)]
                    self.tg.answer_button(reply["id"], "Accepted")
                    self.tg.best_effort("confirm", self.tg.edit, message_id, answered_text(base, label_for(field, value)))
                    self.tg.best_effort("react", self.tg.react, message_id, "👍")
                    return "ok", value, ""
                index = int(choice)
                if not 1 <= index <= len(field["options"]):
                    self.tg.answer_button(reply["id"], "No such option")
                    continue
                if field["type"] == "multiselect":
                    selected.symmetric_difference_update({index})
                    self.tg.answer_button(reply["id"], "Marked" if index in selected else "Unmarked")
                    self.tg.best_effort("keyboard", self.tg.edit, message_id, base, keyboard_for(field, token, selected))
                    continue
                value = field["options"][index - 1]["value"]
                self.tg.answer_button(reply["id"], "Accepted")
                self.tg.best_effort("confirm", self.tg.edit, message_id, answered_text(base, label_for(field, value)))
                self.tg.best_effort("react", self.tg.react, message_id, "👍")
                return "ok", value, ""
            status, value, note = parse_answer(field, str(reply.get("text") or ""))
            if status == "ok":
                self.tg.best_effort("confirm", self.tg.edit, message_id, answered_text(base, str(value)[:150]))
                self.tg.best_effort("react", self.tg.react, message_id, "👍")
                return status, value, note
            if status == "skip":
                self.tg.best_effort("confirm", self.tg.edit, message_id, answered_text(base, "skipped"))
                self.tg.best_effort("react", self.tg.react, message_id, "👍")
                return status, value, note
            attempts += 1
            if not has_buttons and attempts >= MAX_ATTEMPTS:
                return "invalid", None, note
            ids = self.tg.send(f"⚠️ {note}\nTry again.")
            self.message_ids.update(ids)
        return "invalid", None, "too many invalid answers"


# reply_required is computed once per dialog; set in run via property-like init
@property
def _reply_required(self) -> bool:
    return getattr(self, "_rr", False)

# We'll set reply_required as attribute during a lazy init helper instead.
def _ensure_reply_required(self) -> None:
    if not hasattr(self, "reply_required"):
        self.reply_required = self.tg.group_privacy_on()
        self.offset = 0


Dialog._ensure_reply_required = _ensure_reply_required

# Monkey: call _ensure_reply_required at start of run
_orig_run = Dialog.run
def _run_with_rr(self):
    self._ensure_reply_required()
    return _orig_run(self)
Dialog.run = _run_with_rr
_orig_run_text = Dialog.run_text_only
def _run_text_with_rr(self):
    self._ensure_reply_required()
    return _orig_run_text(self)
Dialog.run_text_only = _run_text_with_rr
_orig_ask_field = Dialog.ask_field
def _ask_with_rr(self, field, header):
    self._ensure_reply_required()
    return _orig_ask_field(self, field, header)
Dialog.ask_field = _ask_with_rr


def with_lock(callback):
    LOCK_PATH.touch(exist_ok=True)
    with LOCK_PATH.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            return callback()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


def chat_ids_from_updates(updates: object) -> dict[str, dict]:
    """Chats that texted the bot (for --whoami)."""
    chats: dict[str, dict] = {}
    for update in updates if isinstance(updates, list) else []:
        message = (update or {}).get("message") or {}
        chat = message.get("chat") or {}
        if message.get("text") and chat.get("id") is not None:
            chats[str(chat["id"])] = chat
    return chats


def whoami() -> int:
    """Show chat ids from unprocessed messages to the bot (for filling TELEGRAM_CHAT_ID)."""
    token = env_values().get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print(f"✗ missing TELEGRAM_BOT_TOKEN (check {SCRIPT_DIR / '.env'})")
        return 1
    telegram = Telegram(token, "0")
    try:
        updates = telegram.call(
            "getUpdates", {"offset": 0, "timeout": 0, "limit": 20, "allowed_updates": UPDATES}, 30
        )
    except TgError as error:
        print(f"✗ bot unavailable (network or token): {error}")
        return 1
    chats = chat_ids_from_updates(updates)
    if not chats:
        print("No messages to the bot.")
        print("Write any message to the bot (in DM or group) and retry.")
        return 1
    for chat_id, chat in chats.items():
        kind = "personal" if chat.get("type") == "private" else f"group ({chat.get('type')})"
        print(f"{chat_id}\t{kind}\t{chat.get('title') or chat.get('username') or chat.get('first_name')}")
    print("Copy the id into TELEGRAM_CHAT_ID. Personal chat is more reliable: in groups Privacy mode blocks plain text.")
    return 0


def check() -> int:
    """Diagnostics: creds, bot access, chat type and group limits."""
    try:
        token, chat_id = credentials()
    except TgError as error:
        print(f"✗ {error}")
        print(f"  fill in {SCRIPT_DIR / '.env'}: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return 1
    telegram = Telegram(token, chat_id)
    print(f"✓ .env: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID present ({SCRIPT_DIR / '.env'})")
    try:
        me = telegram.call("getMe", {}, HTTP_TIMEOUT)
    except TgError as error:
        print(f"✗ bot unavailable (network or token): {error}")
        return 1
    me = me or {}
    print(f"✓ bot: @{me.get('username')} (id {me.get('id')})")
    if telegram.is_group:
        if telegram.group_privacy_on():
            print(f"⚠ chat {chat_id} — group, bot has Privacy mode on: plain text won't reach the bot.")
            print("  Reply by swiping (Reply) to the bot's message,")
            print("  disable the mode in BotFather (/setprivacy → Disable) or switch to a personal chat with the bot.")
        else:
            print(f"✓ chat {chat_id} — group, Privacy mode off: text reaches the bot.")
    else:
        print(f"✓ chat {chat_id} — personal: text and buttons work without limits.")
    return 0


# --- daemon / client -------------------------------------------------------
def save_state(active: dict) -> None:
    try:
        data = {"dialogs": [{"message_ids": sorted(d.message_ids)} for d in active.values()]}
        STATE_PATH.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"dialogs": []}


def expire_stale(tg: Telegram) -> None:
    state = load_state()
    for dialog in state.get("dialogs", []):
        for mid in dialog.get("message_ids", []):
            tg.best_effort("expire", tg.edit, mid, EXPIRED_MARK)
    try:
        STATE_PATH.unlink()
    except FileNotFoundError:
        pass


def ensure_daemon() -> None:
    # try connect
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(str(SOCKET_PATH))
        sock.close()
        return
    except (OSError, socket.error):
        pass
    # spawn
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(50):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(str(SOCKET_PATH))
            sock.close()
            return
        except (OSError, socket.error):
            time.sleep(0.1)
    raise TgError("failed to start daemon")


def daemon_request(request: dict) -> dict:
    ensure_daemon()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # the dialog deadline plus a margin: the daemon answers when the dialog ends
    sock.settimeout(DEFAULT_TIMEOUT + 60)
    sock.connect(str(SOCKET_PATH))
    sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    sock.close()
    if not buf.strip():
        raise TgError("empty daemon response")
    return json.loads(buf.decode("utf-8"))


def client_ask(request: dict) -> dict:
    return daemon_request({"type": "ask", "request": request})


def daemon_main() -> int:
    # singleton
    DAEMON_LOCK_PATH.touch(exist_ok=True)
    lock_handle = open(DAEMON_LOCK_PATH, "a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        # another daemon is running
        return 0
    try:
        token, chat_id = credentials()
    except TgError as error:
        print(f"tg-ask: {error}", file=sys.stderr)
        return 1
    tg = Telegram(token, chat_id)
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    expire_stale(tg)

    stop_event = threading.Event()
    update_queue: queue.Queue = queue.Queue()
    active_dialogs: dict[str, Dialog] = {}
    active_lock = threading.Lock()
    client_count = {"n": 0}
    client_lock = threading.Lock()
    last_activity = [time.monotonic()]

    def route_update(update: dict) -> None:
        cb = update.get("callback_query")
        if cb:
            msg = (cb.get("message") or {})
            mid = msg.get("message_id")
            with active_lock:
                targets = [d for d in active_dialogs.values() if mid in d.message_ids]
            if targets:
                targets[0].update_queue.put({"kind": "button", "data": str(cb.get("data") or ""), "id": str(cb.get("id") or "")})
            return
        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id")) != str(tg.chat_id):
            return
        text = message.get("text")
        if not text:
            return
        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        with active_lock:
            if reply_to is not None:
                targets = [d for d in active_dialogs.values() if reply_to in d.message_ids]
            else:
                awaiting = [d for d in active_dialogs.values() if getattr(d, "awaiting", False)]
                if len(awaiting) == 1:
                    targets = awaiting
                elif len(awaiting) > 1:
                    try:
                        tg.send("Multiple questions are open. Reply by swiping (Reply) to the specific message.")
                    except TgError:
                        pass
                    return
                else:
                    targets = []
        if targets:
            targets[0].update_queue.put({"kind": "text", "text": str(text)})

    def poll_loop() -> None:
        offset = tg.drain()
        while not stop_event.is_set():
            try:
                updates = tg.call("getUpdates", {"offset": offset, "timeout": POLL_TIMEOUT, "limit": 10, "allowed_updates": UPDATES}, POLL_TIMEOUT + 10)
            except TgError as error:
                log_event(kind="poll-failed", error=str(error))
                time.sleep(2)
                continue
            for update in updates or []:
                offset = int(update["update_id"]) + 1
                route_update(update)

    threading.Thread(target=poll_loop, daemon=True).start()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET_PATH))
    server.listen(16)
    server.settimeout(1.0)

    def handle_client(conn: socket.socket) -> None:
        with client_lock:
            client_count["n"] += 1
        last_activity[0] = time.monotonic()
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if not buf.strip():
                return
            request = json.loads(buf.decode("utf-8"))
            if request.get("type") == "notify":
                try:
                    ids = tg.send(str(request.get("text") or ""))
                    conn.sendall((json.dumps({"ok": True, "message_ids": ids, "message_id": ids[0] if ids else None}) + "\n").encode("utf-8"))
                except TgError as error:
                    conn.sendall((json.dumps({"ok": False, "reason": "error", "error": str(error)}) + "\n").encode("utf-8"))
                return
            payload = request.get("request") or {}
            dialog_id = uuid.uuid4().hex
            dq: queue.Queue = queue.Queue()
            dialog = Dialog(tg, payload, dq)
            dialog.id = dialog_id
            with active_lock:
                active_dialogs[dialog_id] = dialog
            save_state(active_dialogs)
            last_activity[0] = time.monotonic()
            try:
                result = dialog.run()
            except Exception as error:  # pragma: no cover
                log_event(kind="dialog-crashed", id=dialog_id, error=str(error))
                result = {"ok": False, "reason": "error", "error": str(error)}
            finally:
                with active_lock:
                    active_dialogs.pop(dialog_id, None)
                save_state(active_dialogs)
            conn.sendall((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
            last_activity[0] = time.monotonic()
        except Exception as error:
            log_event(kind="client-error", error=str(error))
        finally:
            conn.close()
            with client_lock:
                client_count["n"] -= 1

    def accept_loop() -> None:
        while not stop_event.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=handle_client, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()

    def idle_watcher() -> None:
        while not stop_event.is_set():
            time.sleep(5)
            with active_lock:
                active_count = len(active_dialogs)
            with client_lock:
                cc = client_count["n"]
            if active_count == 0 and cc == 0:
                if time.monotonic() - last_activity[0] >= IDLE_TIMEOUT:
                    stop_event.set()
            else:
                last_activity[0] = time.monotonic()

    threading.Thread(target=idle_watcher, daemon=True).start()

    while not stop_event.is_set():
        time.sleep(1)
    server.close()
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
    except OSError:
        pass
    lock_handle.close()
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tg-ask.py", description=__doc__)
    parser.add_argument("mode", nargs="?", choices=["notify", "ask"], help="notify | ask")
    parser.add_argument("text", nargs="*", help="text for notify mode")
    parser.add_argument("--daemon", action="store_true", help="run the ephemeral coordinator")
    parser.add_argument("--selfcheck", action="store_true", help="offline checks")
    parser.add_argument("--check", action="store_true", help="installation diagnostics")
    parser.add_argument("--whoami", action="store_true", help="show chat ids from messages to the bot")
    args = parser.parse_args(argv)
    if args.daemon:
        return daemon_main()
    if args.selfcheck:
        return selfcheck()
    if args.check:
        return check()
    if args.whoami:
        return whoami()
    if args.mode is None:
        parser.print_usage(sys.stderr)
        return 1
    try:
        token, chat_id = credentials()
        telegram = Telegram(token, chat_id)
        if args.mode == "notify":
            body = " ".join(args.text).strip()
            if not body:
                raise TgError("nothing to send")
            message_ids = telegram.send(body)
            log_event(kind="notify", message_ids=message_ids)
            return emit({"ok": True, "message_ids": message_ids, "message_id": message_ids[0]})
        raw_request = sys.stdin.read()
        try:
            request = json.loads(raw_request) if raw_request.strip() else {}
        except ValueError as error:
            raise TgError(f"invalid JSON request: {error}") from error
        if not isinstance(request, dict):
            raise TgError("request must be a JSON object")
        log_event(kind="ask", text=str(request.get("text") or "")[:200], fields=len(request.get("fields") or []))
        result = client_ask(request)
        log_event(kind="ask-done", ok=result.get("ok"), reason=result.get("reason"))
        return emit(result)
    except TgError as error:
        log_event(kind="error", error=str(error))
        print(f"tg-ask: {error}", file=sys.stderr)
        return emit({"ok": False, "reason": "config", "error": str(error)})
    except KeyboardInterrupt:
        return emit({"ok": False, "reason": "interrupted"})


# --- offline checks ---------------------------------------------------------
def selfcheck() -> int:
    failures: list[str] = []

    def check(name: str, got: object, expected: object) -> None:
        if got != expected:
            failures.append(f"{name}: got {got!r}, expected {expected!r}")

    check("env", parse_env('# c\nexport A = "x y"\nB=1\nbroken\n'), {"A": "x y", "B": "1"})

    # tunables from env: valid value wins, junk and non-positive fall back to the default
    os.environ["TG_ASK_SELFCHECK_KEY"] = "7.5"
    check("tunable-env", env_number("TG_ASK_SELFCHECK_KEY", 1.0), 7.5)
    os.environ["TG_ASK_SELFCHECK_KEY"] = "junk"
    check("tunable-invalid", env_number("TG_ASK_SELFCHECK_KEY", 1.0), 1.0)
    os.environ["TG_ASK_SELFCHECK_KEY"] = "-5"
    check("tunable-non-positive", env_number("TG_ASK_SELFCHECK_KEY", 1.0), 1.0)
    del os.environ["TG_ASK_SELFCHECK_KEY"]

    choice = normalize_fields(
        [{"key": "k", "type": "string", "options": [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]}]
    )[0]
    check("option-number", parse_answer(choice, "2"), ("ok", "no", ""))
    check("option-number-note", parse_answer(choice, "1 because needed"), ("ok", "yes", "because needed"))
    check("option-label", parse_answer(choice, "no"), ("ok", "no", ""))
    check("option-bad", parse_answer(choice, "maybe"), ("invalid", None, "choose an item from the list"))
    check(
        "keyboard-single",
        keyboard_for(choice, 3),
        [[{"text": "Yes", "callback_data": "c3:1"}, {"text": "No", "callback_data": "c3:2"}]],
    )
    check("label-for", label_for(choice, "no"), "No")

    custom = normalize_fields([{**choice, "custom": True}])[0]
    check("option-custom", parse_answer(custom, "custom option"), ("ok", "custom option", ""))

    boolean = normalize_fields([{"key": "b", "type": "boolean"}])[0]
    check("bool-yes", parse_answer(boolean, "yes"), ("ok", True, ""))
    check("bool-no", parse_answer(boolean, "n"), ("ok", False, ""))
    check("bool-bad", parse_answer(boolean, "xxx")[0], "invalid")

    numbers = normalize_fields([{"key": "n", "type": "number"}, {"key": "i", "type": "integer"}])
    check("number", parse_answer(numbers[0], "3,5"), ("ok", 3.5, ""))
    check("integer", parse_answer(numbers[1], "7"), ("ok", 7, ""))
    check("integer-bad", parse_answer(numbers[1], "1.5")[0], "invalid")

    multi = normalize_fields(
        [
            {
                "key": "m",
                "type": "multiselect",
                "minItems": 1,
                "maxItems": 2,
                "options": [
                    {"label": "First", "value": "one"},
                    {"label": "Second option", "value": "two"},
                    {"label": "Third", "value": "three"},
                ],
            }
        ]
    )[0]
    check("multi-numbers", parse_answer(multi, "1, 2"), ("ok", ["one", "two"], ""))
    check("multi-labels", parse_answer(multi, "First, Third"), ("ok", ["one", "three"], ""))
    check("multi-too-many", parse_answer(multi, "1,2,3")[0], "invalid")
    keyboard = keyboard_for(multi, 7, {1, 3})
    check("keyboard-multiselect", keyboard[0][0]["text"], f"{CHECKED} First")
    check("keyboard-multiselect-unchecked", keyboard[0][1]["text"], f"{UNCHECKED} Second option")
    check("keyboard-multiselect-second-row", keyboard[1][0]["text"], f"{CHECKED} Third")
    check("keyboard-done", keyboard[-1][0], {"text": DONE_LABEL, "callback_data": "c7:done"})
    check("button-data", parse_button_data("c12:3"), (12, "3"))
    check("button-data-done", parse_button_data("c12:done"), (12, "done"))
    check("button-data-bad", parse_button_data("garbage"), None)
    check("multi-label-join", label_for(multi, ["one", "three"]), "First, Third")

    # --whoami: only text messages, walk update by update
    whoami_updates = [
        {"update_id": 1, "message": {"text": "hello", "chat": {"id": 37433667, "type": "private"}}},
        {"update_id": 2, "message": {"text": None, "chat": {"id": -100, "type": "supergroup"}}},
        {"update_id": 3, "callback_query": {"id": "x"}},
        {"update_id": 4, "message": {"text": "in group", "chat": {"id": -1003843008477, "type": "supergroup"}}},
    ]
    check("whoami-chats", list(chat_ids_from_updates(whoami_updates)), ["37433667", "-1003843008477"])
    check("whoami-empty", chat_ids_from_updates(None), {})

    optional = normalize_fields([{"key": "s", "type": "string", "required": False}])[0]
    check("skip", parse_answer(optional, "-"), ("skip", None, ""))
    check("empty-required", parse_answer(normalize_fields([{"key": "s"}])[0], "  ")[0], "invalid")
    check("pattern", parse_answer(normalize_fields([{"key": "s", "pattern": r"\d{3}"}])[0], "12")[0], "invalid")

    parts = split_message("x" * 8000)
    check("split-count", len(parts) <= MAX_PARTS and all(len(p) <= PART_LIMIT for p in parts), True)
    check("split-keeps-text", sum(len(p) for p in parts) >= 7000, True)

    prompt = build_prompt("Permission required: shell", choice, "project", True)
    check("prompt-options", "<li>Yes</li>" in prompt and "<li>No</li>" in prompt and "Press a button" in prompt, True)
    check("prompt-heading", "<p>Permission required: shell</p>" in prompt, True)
    check("prompt-list-break", "<p>Permission required: shell</p>\n<br>\n<ol>" in prompt, True)
    check("prompt-rich-header", "<h2>🤖 OpenCode</h2>" in prompt and "<h3>project</h3>" in prompt, True)
    check("prompt-checklist", '<input type="checkbox">' in build_prompt("Pick", multi, "project", False), True)
    check("prompt-code", "<pre><code>echo hi</code></pre>" in build_prompt("Run", optional, "", False, "echo hi"), True)
    check("prompt-fenced-code", '<pre><code class="language-sh">ls -la</code></pre>' in build_prompt("Run ```sh\nls -la\n```", optional, "", False), True)
    check("prompt-inline-code", "<code>npm test</code>" in build_prompt("Run `npm test`", optional, "", False), True)
    check("prompt-reply-hint", "Reply" in build_prompt("Question", optional, "", True), True)
    check("prompt-no-reply-hint", "Reply" in build_prompt("Question", optional, "", False), False)

    described = normalize_fields(
        [{"key": "k", "options": [{"label": "Fast", "value": "fast", "description": "Uses `say`, ~2 min"}]}]
    )[0]
    check("option-description", described["options"][0]["description"], "Uses `say`, ~2 min")
    check(
        "prompt-option-description",
        "<li>Fast<br><i>Uses <code>say</code>, ~2 min</i><br><br></li>" in build_prompt("Pick", described, "", False),
        True,
    )
    check("prompt-option-no-empty-italic", "<i></i>" in build_prompt("Pick", choice, "", False), False)

    external = normalize_fields([{"key": "u", "type": "external", "external": True, "url": "https://x.y"}])[0]
    check("external-url", "https://x.y" in build_prompt("OAuth", external, "", False), True)

    # free text (empty fields) is rendered through the same normalized view
    free_text = normalize_fields([{"key": "answer", "type": "string"}])[0]
    check(
        "free-text-keys",
        {"key", "title", "type", "options", "external", "required"} <= set(free_text),
        True,
    )
    check("free-text-prompt", "Answer with text." in build_prompt("Question", free_text, "", False), True)

    # reply is appended to the original question, not replacing it
    history = answered_text("Keep supergroup?", "Keep supergroup")
    check("answered-keeps-prompt", history.startswith("Keep supergroup?\n\n"), True)
    check("answered-appends", history.endswith(f"{ANSWER_MARK}: Keep supergroup</p>"), True)
    huge = answered_text("x" * 5000, "Yes")
    check("answered-fits-limit", len(huge) <= MAX_EDIT and huge.endswith(f"{ANSWER_MARK}: Yes</p>"), True)
    check("answered-truncated", "…" in huge, True)

    # message edit: without buttons keyboard is removed, with a list it is replaced
    class FakeTelegram(Telegram):
        def __init__(self, is_group: bool = False, me: dict | None = None) -> None:
            self.chat_id = "-1" if is_group else "1"
            self.is_group = is_group
            self.me = me or {}
            self.thread_id = None
            self.calls: list[tuple[str, dict]] = []

        def call(self, method: str, params: dict, timeout: float) -> object:  # type: ignore[override]
            self.calls.append((method, params))
            return self.me

    fake = FakeTelegram()
    fake.edit(5, "text")
    check("edit-clears-keyboard", fake.calls[-1][1]["reply_markup"], {"inline_keyboard": []})
    fake.edit(5, "text", keyboard_for(choice, 1))
    check("edit-sets-keyboard", fake.calls[-1][1]["reply_markup"], {"inline_keyboard": keyboard_for(choice, 1)})

    # Privacy mode is detected via getMe, not chat_id format
    check("privacy-on-in-group", FakeTelegram(True, {}).group_privacy_on(), True)
    check("privacy-off-in-group", FakeTelegram(True, {"can_read_all_group_messages": True}).group_privacy_on(), False)
    check("privacy-in-private-chat", FakeTelegram(False, {}).group_privacy_on(), False)
    probing = FakeTelegram(True, {})
    probing.group_privacy_on()
    check("privacy-uses-getme", probing.calls[0][0], "getMe")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        print(f"selfcheck: {len(failures)} checks failed", file=sys.stderr)
        return 1
    print("selfcheck: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
