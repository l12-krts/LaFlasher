"""Settings files shown as simple forms: JSON objects and KEY=value style files.

Only values are changed when saving. Key=value files keep their comments, order, spacing
and quotes; JSON files keep their key order and indentation style.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

JSON_EXTENSIONS = {".json"}
KEY_VALUE_EXTENSIONS = {".ini", ".cfg", ".conf", ".env", ".properties", ".txt"}
SETTINGS_EXTENSIONS = JSON_EXTENSIONS | KEY_VALUE_EXTENSIONS
MAX_SETTINGS_SIZE = 256 * 1024
_BOM = b"\xef\xbb\xbf"
_SECRET = re.compile(r"pass|pwd|secret|token|api[_-]?key|private", re.I)
# Words that read better in their usual spelling: wifi_ssid -> "Wi-Fi SSID".
_WORDS = {"wifi": "Wi-Fi", "wlan": "WLAN", "ssid": "SSID", "ip": "IP", "mqtt": "MQTT", "url": "URL",
          "uri": "URI", "id": "ID", "api": "API", "ntp": "NTP", "http": "HTTP", "https": "HTTPS",
          "utc": "UTC", "gpio": "GPIO", "led": "LED", "ota": "OTA", "dns": "DNS", "mac": "MAC",
          "ble": "BLE", "tz": "TZ", "dhcp": "DHCP", "ap": "AP", "rgb": "RGB", "pwm": "PWM"}


@dataclass
class Field:
    key: object      # where the value lives: a key path (JSON) or a line number (key=value)
    name: str        # the key exactly as written in the file
    label: str       # friendly label for the form
    section: str     # group title, "" for top-level settings
    kind: str        # "text", "int", "float", "bool", "null" or "json"
    value: object
    secret: bool = False


def friendly(name: str) -> str:
    """'wifi_password' -> 'Wi-Fi password', 'mqttHost' -> 'MQTT host'."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    words = [w for w in re.split(r"[\s_\-.]+", spaced) if w]
    if not words:
        return str(name)
    out = []
    for word in words:
        lower = word.lower()
        out.append(_WORDS.get(lower, word if word.isupper() and len(word) > 1 else lower))
    first = out[0]
    out[0] = first[:1].upper() + first[1:]
    return " ".join(out)


def is_secret(name: str) -> bool:
    return bool(_SECRET.search(str(name)))


class JsonSettings:
    """A JSON object; nested objects become sections."""

    def __init__(self, path: Path):
        self.path = Path(path)
        raw = self.path.read_bytes()
        self._bom = raw.startswith(_BOM)
        text = raw.decode("utf-8-sig")
        self._newline = "\r\n" if "\r\n" in text else "\n"
        self._final_newline = text.endswith("\n")
        self._indent = self._detect_indent(text)
        self.data = json.loads(text)
        if not isinstance(self.data, dict):
            raise ValueError(f"{self.path.name} is not a JSON object")
        self.fields: list[Field] = []
        self._collect(self.data, (), "")

    @staticmethod
    def _detect_indent(text: str):
        match = re.search(r"\n([ \t]+)\S", text)
        if not match:
            return None  # written on one line
        indent = match.group(1)
        return "\t" if indent.startswith("\t") else len(indent)

    def _collect(self, obj: dict, path: tuple, section: str) -> None:
        for name, value in obj.items():
            key = path + (name,)
            if isinstance(value, dict) and value:
                title = " › ".join(friendly(p) for p in key)
                self._collect(value, key, title)
                continue
            if isinstance(value, bool):
                kind = "bool"
            elif isinstance(value, int):
                kind = "int"
            elif isinstance(value, float):
                kind = "float"
            elif isinstance(value, str):
                kind = "text"
            elif value is None:
                kind = "null"
            else:
                kind = "json"
            self.fields.append(Field(key, str(name), friendly(name), section, kind, value, is_secret(name)))

    def set(self, field: Field, value) -> None:
        target = self.data
        for part in field.key[:-1]:
            target = target[part]
        target[field.key[-1]] = value
        field.value = value

    def to_bytes(self) -> bytes:
        if self._indent is None:
            text = json.dumps(self.data, ensure_ascii=False)
        else:
            text = json.dumps(self.data, indent=self._indent, ensure_ascii=False)
        if self._final_newline:
            text += "\n"
        data = text.replace("\n", self._newline).encode("utf-8")
        return _BOM + data if self._bom else data


_SECTION_LINE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_PAIR_LINE = re.compile(r"^(?P<indent>\s*)(?P<export>export\s+)?(?P<key>[A-Za-z0-9_.\-]+)"
                        r"(?P<sep>\s*[=:]\s*)(?P<value>.*?)(?P<trail>\s*)$")
_BOOL_WORDS = {"true": True, "false": False}


class KeyValueSettings:
    """KEY=value lines (.env, .ini, .cfg, .properties...). [sections] become groups."""

    def __init__(self, path: Path):
        self.path = Path(path)
        raw = self.path.read_bytes()
        self._bom = raw.startswith(_BOM)
        self.lines = raw.decode("utf-8-sig").splitlines(keepends=True)
        self.fields: list[Field] = []
        self._parts: dict[int, dict] = {}
        section, meaningful = "", 0
        for number, line in enumerate(self.lines):
            body = line.rstrip("\r\n")
            stripped = body.strip()
            if not stripped or stripped.startswith(("#", ";", "//")):
                continue
            meaningful += 1
            header = _SECTION_LINE.match(body)
            if header:
                section = friendly(header.group(1).strip())
                continue
            match = _PAIR_LINE.match(body)
            if not match:
                continue
            parts = match.groupdict()
            value, quote = parts["value"], ""
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value, quote = value[1:-1], value[0]
            parts["quote"] = quote
            parts["ending"] = line[len(body):]
            self._parts[number] = parts
            kind = "bool" if not quote and value.lower() in _BOOL_WORDS else "text"
            shown = _BOOL_WORDS[value.lower()] if kind == "bool" else value
            self.fields.append(Field(number, parts["key"], friendly(parts["key"]), section, kind,
                                     shown, is_secret(parts["key"])))
        if not self.fields or (self.path.suffix.lower() == ".txt" and len(self.fields) * 2 < meaningful):
            raise ValueError(f"{self.path.name} doesn't look like a KEY=value settings file")

    def set(self, field: Field, value) -> None:
        parts = self._parts[field.key]
        if field.kind == "bool":
            old = parts["value"]
            text = "true" if value else "false"
            if old[:1].isupper():  # keep the file's style: True/False, TRUE/FALSE
                text = text.upper() if old.isupper() else text.capitalize()
        else:
            text = str(value)
        quote = parts["quote"]
        if not quote and text != text.strip():
            quote = '"'  # leading/trailing spaces would be lost without quotes
        self.lines[field.key] = (f"{parts['indent']}{parts['export'] or ''}{parts['key']}{parts['sep']}"
                                 f"{quote}{text}{quote}{parts['trail']}{parts['ending']}")
        field.value = value

    def to_bytes(self) -> bytes:
        data = "".join(self.lines).encode("utf-8")
        return _BOM + data if self._bom else data


def load_settings(path: str | Path) -> JsonSettings | KeyValueSettings:
    """Open a settings file for form editing. Raises ValueError if it can't be shown as a form."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SETTINGS_EXTENSIONS:
        raise ValueError(f"{path.name} is not a settings file (.json, .ini, .env, .cfg, ...)")
    if path.stat().st_size > MAX_SETTINGS_SIZE:
        raise ValueError(f"{path.name} is too large to show as a form")
    try:
        return JsonSettings(path) if ext in JSON_EXTENSIONS else KeyValueSettings(path)
    except UnicodeDecodeError:
        raise ValueError(f"{path.name} is not a text file") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON ({exc.msg}, line {exc.lineno})") from None
