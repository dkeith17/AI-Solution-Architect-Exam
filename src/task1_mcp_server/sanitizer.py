"""Input sanitization and injection inspection for MCP tool arguments.

Stdlib only, so it can be unit-tested without the MCP SDK installed.

Every string argument goes through ``Sanitizer.clean`` in this exact order:

    1. sanitize            (see ``_sanitize_once``)
    2. trim exterior whitespace (space, tab, newline)
    3. deduplicate internal spaces (runs of U+0020 become one U+0020; tabs untouched)
    4. remove internal spaces adjacent to tabs (" \\t " becomes "\\t")
    5. remove spaces and tabs immediately before newlines
    6. re-sanitize         (defensive second pass of step 1)

Then the cleaned value is inspected for common code-injection patterns aimed at
MCP services. Each detection category has a policy, "reject" or "allow", and both
outcomes are logged. Length checks (such as the 10-character minimum for
``reason``) are applied by the caller to the *cleaned* value.

"Sanitize" (step 1 and 6) means, in order:

    a. normalize line endings: CRLF and lone CR become LF
    b. strip ANSI/VT escape sequences (CSI, OSC, two-byte ESC sequences)
    c. Unicode NFKC normalization (folds full-width and other look-alike forms)
    d. map Unicode line breaks (VT, FF, NEL, U+2028, U+2029) to LF and every other
       Unicode space separator (NBSP, ideographic space, ...) to U+0020
    e. remove format characters (Unicode category Cf): zero-width characters,
       bidi overrides ("Trojan Source"), and Unicode tag characters
       ("ASCII smuggling" of hidden prompt-injection text)
    f. remove control characters (category Cc) other than TAB and LF, and lone
       surrogates (category Cs)

Every change that is not purely a whitespace change is logged. Log lines never
contain the raw argument value (it may hold PII or attack payloads); they name the
field, the step, and the affected code points.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "Action",
    "CATEGORIES",
    "Change",
    "Finding",
    "InjectionRejected",
    "InputTooLong",
    "SanitizeOutcome",
    "Sanitizer",
    "SanitizerConfig",
]

log = logging.getLogger("task1.sanitizer")


class Action(str, Enum):
    REJECT = "reject"
    ALLOW = "allow"


@dataclass(frozen=True)
class Change:
    step: str
    detail: str
    whitespace_only: bool


@dataclass(frozen=True)
class Finding:
    category: str
    rule: str


class InjectionRejected(ValueError):
    """Raised when a detected pattern's category policy is ``reject``."""

    def __init__(self, field_name: str, finding: Finding) -> None:
        self.field_name = field_name
        self.finding = finding
        super().__init__(
            f"potential {finding.category.replace('_', ' ')} detected (rule '{finding.rule}')"
        )


class InputTooLong(ValueError):
    def __init__(self, field_name: str, length: int, limit: int) -> None:
        self.field_name = field_name
        super().__init__(f"value is {length} characters after sanitization; limit is {limit}")


@dataclass
class SanitizeOutcome:
    value: str
    changes: list[Change] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Step 1: sanitize
# --------------------------------------------------------------------------------------

_ANSI_RE = re.compile(
    r"""
    \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?      # OSC ... BEL or ST (or unterminated)
    | (?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]      # CSI (7-bit or 8-bit introducer)
    | \x1b[@-Z\\-_]                          # two-byte Fe escape sequences
    """,
    re.VERBOSE,
)

_LINE_BREAKS = {"\x0b", "\x0c", "\x85", "\u2028", "\u2029"}
_KEEP_CONTROLS = {"\t", "\n"}


def _codepoints(chars: list[str], limit: int = 8) -> str:
    shown = ", ".join(f"U+{ord(c):04X}" for c in chars[:limit])
    if len(chars) > limit:
        shown += f", ... ({len(chars)} total)"
    return shown


def _strip_ws(s: str) -> str:
    return "".join(ch for ch in s if not ch.isspace())


def _sanitize_once(value: str) -> tuple[str, list[Change]]:
    changes: list[Change] = []

    # a. line endings
    v = value.replace("\r\n", "\n").replace("\r", "\n")
    if v != value:
        changes.append(Change("normalize_newlines", "CRLF/CR converted to LF", True))

    # b. ANSI escape sequences
    matches = _ANSI_RE.findall(v)
    if matches:
        v = _ANSI_RE.sub("", v)
        changes.append(Change("strip_ansi_escapes", f"removed {len(matches)} escape sequence(s)", False))

    # c. NFKC
    nfkc = unicodedata.normalize("NFKC", v)
    if nfkc != v:
        ws_only = _strip_ws(nfkc) == _strip_ws(v)
        changes.append(Change("nfkc_normalize", f"length {len(v)} -> {len(nfkc)}", ws_only))
        v = nfkc

    # d. Unicode whitespace mapping
    mapped: list[str] = []
    remapped = False
    for ch in v:
        if ch in _LINE_BREAKS:
            mapped.append("\n")
            remapped = True
        elif ch not in (" ", "\t", "\n") and unicodedata.category(ch) == "Zs":
            mapped.append(" ")
            remapped = True
        else:
            mapped.append(ch)
    if remapped:
        v = "".join(mapped)
        changes.append(Change("map_unicode_whitespace", "Unicode spaces/line breaks mapped to ASCII", True))

    # e. format characters (Cf)
    removed_cf = [ch for ch in v if unicodedata.category(ch) == "Cf"]
    if removed_cf:
        v = "".join(ch for ch in v if unicodedata.category(ch) != "Cf")
        changes.append(Change("remove_format_chars", f"removed {_codepoints(removed_cf)}", False))

    # f. control characters (Cc except TAB/LF) and lone surrogates (Cs)
    def _bad(ch: str) -> bool:
        cat = unicodedata.category(ch)
        return (cat == "Cc" and ch not in _KEEP_CONTROLS) or cat == "Cs"

    removed_cc = [ch for ch in v if _bad(ch)]
    if removed_cc:
        v = "".join(ch for ch in v if not _bad(ch))
        changes.append(Change("remove_control_chars", f"removed {_codepoints(removed_cc)}", False))

    return v, changes


# --------------------------------------------------------------------------------------
# Steps 2-5: whitespace normalization
# --------------------------------------------------------------------------------------

_MULTI_SPACE_RE = re.compile(r" {2,}")
_SPACE_AROUND_TAB_RE = re.compile(r" *\t *")
_WS_BEFORE_NEWLINE_RE = re.compile(r"[ \t]+\n")


def _trim(v: str) -> str:
    return v.strip(" \t\n")


def _dedupe_spaces(v: str) -> str:
    return _MULTI_SPACE_RE.sub(" ", v)


def _remove_spaces_adjacent_to_tabs(v: str) -> str:
    return _SPACE_AROUND_TAB_RE.sub("\t", v)


def _remove_ws_before_newlines(v: str) -> str:
    return _WS_BEFORE_NEWLINE_RE.sub("\n", v)


# --------------------------------------------------------------------------------------
# Injection inspection
# --------------------------------------------------------------------------------------

_I = re.IGNORECASE

# Each rule is (rule_name, compiled_pattern). Patterns run on the fully cleaned value,
# so obfuscation with full-width forms, zero-width splits, or bidi tricks is already
# undone by the sanitize step before inspection.
_RULES: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "prompt_injection": [
        ("ignore_instructions", re.compile(
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|system)\b[^.\n]{0,40}\b(instructions?|prompts?|rules|messages?)\b", _I)),
        ("role_reassignment", re.compile(r"\byou\s+are\s+now\b|\bact\s+as\s+(an?\s+)?(admin|administrator|root|system)\b", _I)),
        ("system_prompt_probe", re.compile(r"\b(reveal|print|show|output|repeat)\b[^.\n]{0,30}\bsystem\s+prompt\b", _I)),
        ("chat_template_tokens", re.compile(r"<\|(im_start|im_end|system|endoftext)\|>|\[/?INST\]|<<SYS>>", _I)),
        ("tool_poisoning_tags", re.compile(r"</?\s*(important|system|instructions?|tool_call|function_call)\s*>", _I)),
    ],
    "sql_injection": [
        ("tautology", re.compile(r"['\"]\s*(or|and)\s+['\"]?\w+['\"]?\s*=\s*['\"]?\w+", _I)),
        ("stacked_query", re.compile(r";\s*(drop|delete|insert|update|alter|truncate|exec(ute)?|create|grant)\b", _I)),
        ("union_select", re.compile(r"\bunion\b\s+(all\s+)?\bselect\b", _I)),
        ("comment_terminator", re.compile(r"['\"]\s*(--|#|/\*)")),
        ("time_based", re.compile(r"\b(sleep|benchmark|pg_sleep|waitfor\s+delay)\s*\(", _I)),
    ],
    "command_injection": [
        ("command_substitution", re.compile(r"\$\([^)]*\)|`[^`]+`")),
        ("chained_command", re.compile(
            r"(;|&&|\|\||\|)\s*(rm|curl|wget|bash|sh|zsh|powershell|pwsh|cmd(\.exe)?|nc|ncat|python\d?|perl|ruby|chmod|chown|del|certutil|bitsadmin|iex|invoke-expression)\b", _I)),
        ("powershell_download", re.compile(r"\b(invoke-webrequest|downloadstring|frombase64string)\b", _I)),
    ],
    "path_traversal": [
        ("dot_dot_segment", re.compile(r"(^|[\\/])\.\.([\\/]|$)")),
        ("encoded_traversal", re.compile(r"%2e%2e|%252e|\.\.%2f|\.\.%5c", _I)),
        ("sensitive_path", re.compile(r"/etc/(passwd|shadow)|[a-z]:\\windows\\system32", _I)),
    ],
    "template_injection": [
        ("jinja_expression", re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)),
        ("dollar_brace", re.compile(r"\$\{[^}]*\}")),
        ("ruby_interpolation", re.compile(r"#\{[^}]*\}")),
    ],
    "script_injection": [
        ("script_tag", re.compile(r"<\s*/?\s*(script|iframe|object|embed|svg)\b", _I)),
        ("javascript_uri", re.compile(r"\b(javascript|vbscript)\s*:", _I)),
        ("event_handler_attr", re.compile(r"<[^>]*\bon[a-z]+\s*=", _I)),
        ("data_html_uri", re.compile(r"\bdata\s*:\s*text/html", _I)),
    ],
    "jsonrpc_smuggling": [
        ("embedded_jsonrpc", re.compile(r"[\"']jsonrpc[\"']\s*:", _I)),
        ("embedded_method_call", re.compile(r"[\"']method[\"']\s*:\s*[\"'](tools|resources|prompts|sampling)/", _I)),
    ],
}

CATEGORIES: tuple[str, ...] = tuple(_RULES)


def inspect(value: str) -> list[Finding]:
    findings: list[Finding] = []
    for category, rules in _RULES.items():
        for rule_name, pattern in rules:
            if pattern.search(value):
                findings.append(Finding(category, rule_name))
                break  # one finding per category is enough to apply its policy
    return findings


# --------------------------------------------------------------------------------------
# Configuration and the public entry point
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SanitizerConfig:
    default_action: Action = Action.REJECT
    category_actions: dict[str, Action] = field(default_factory=dict)
    max_string_length: int = 4096

    def action_for(self, category: str) -> Action:
        return self.category_actions.get(category, self.default_action)

    @classmethod
    def from_mapping(cls, data: dict) -> "SanitizerConfig":
        """Build from the ``[sanitizer]`` table of the TOML config file."""
        section = data.get("sanitizer", {})
        default = Action(section.get("default_action", Action.REJECT.value))
        cats_raw = section.get("categories", {})
        unknown = set(cats_raw) - set(CATEGORIES)
        if unknown:
            raise ValueError(f"unknown sanitizer categories in config: {sorted(unknown)}")
        cats = {k: Action(v) for k, v in cats_raw.items()}
        max_len = int(section.get("max_string_length", 4096))
        if max_len <= 0:
            raise ValueError("max_string_length must be positive")
        return cls(default_action=default, category_actions=cats, max_string_length=max_len)


class Sanitizer:
    def __init__(self, config: SanitizerConfig | None = None, logger: logging.Logger | None = None) -> None:
        self.config = config or SanitizerConfig()
        self.log = logger or log

    def normalize(self, value: str) -> SanitizeOutcome:
        """Run steps 1-6 only (no inspection, no policy). Pure and idempotent."""
        out = SanitizeOutcome(value=value)
        v, ch = _sanitize_once(value)
        out.changes.extend(ch)
        for step, fn in (
            ("trim_exterior_whitespace", _trim),
            ("dedupe_internal_spaces", _dedupe_spaces),
            ("remove_spaces_adjacent_to_tabs", _remove_spaces_adjacent_to_tabs),
            ("remove_whitespace_before_newlines", _remove_ws_before_newlines),
        ):
            nv = fn(v)
            if nv != v:
                out.changes.append(Change(step, f"length {len(v)} -> {len(nv)}", True))
                v = nv
        v, ch = _sanitize_once(v)
        out.changes.extend(Change("resanitize:" + c.step, c.detail, c.whitespace_only) for c in ch)
        out.value = v
        return out

    def clean(self, field_name: str, value: str) -> str:
        """Normalize, log non-whitespace changes, inspect, apply policy.

        Returns the cleaned value or raises ``InjectionRejected`` / ``InputTooLong``.
        """
        out = self.normalize(value)
        for change in out.changes:
            if not change.whitespace_only:
                level = logging.INFO if change.step.endswith("nfkc_normalize") else logging.WARNING
                self.log.log(level, "sanitized field=%s step=%s %s", field_name, change.step, change.detail)

        if len(out.value) > self.config.max_string_length:
            self.log.warning(
                "rejected field=%s reason=too_long length=%d limit=%d",
                field_name, len(out.value), self.config.max_string_length,
            )
            raise InputTooLong(field_name, len(out.value), self.config.max_string_length)

        for finding in inspect(out.value):
            action = self.config.action_for(finding.category)
            self.log.warning(
                "injection pattern field=%s category=%s rule=%s action=%s",
                field_name, finding.category, finding.rule,
                "rejected" if action is Action.REJECT else "allowed",
            )
            if action is Action.REJECT:
                raise InjectionRejected(field_name, finding)
        return out.value
