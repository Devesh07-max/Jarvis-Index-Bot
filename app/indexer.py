"""Deterministic, streaming Telegram channel index construction.

The matcher intentionally favours precision over recall: uncertain matches are
left unresolved for user review rather than linking a wrong message.
"""
from __future__ import annotations

import re
import html
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

# More specific phrases must precede generic document terms.
MATERIALS: dict[str, tuple[str, ...]] = {
    "century pdf": ("century pdfs", "century pdf", "सेंचुरी पीडीएफ्स", "सेंचुरी पीडीएफ", "सेंचुरी pdf"),
    "digital notes": ("digital notes", "digital note", "handwritten notes", "class notes", "notes", "नोट्स"),
    "practice sets": ("practice set", "practice questions", "question bank", "mcq", "प्रैक्टिस सेट", "प्रैक्टिस प्रश्न"),
    "tests": ("mock tests", "mock test", "test series", "tests", "test", "quizzes", "quiz", "exams", "exam", "टेस्ट्स", "टेस्ट", "परीक्षा"),
    "planners": ("planners", "planner", "study plan", "schedule", "timetable", "प्लानर", "प्लानर्स", "समय सारणी"),
    "lectures": ("video lecture", "video lectures", "lectures", "lecture", "classes", "class", "sessions", "session", "लेक्चर्स", "लेcture", "लेक्चर", "व्याख्यान", "क्लास"),
    "pdfs": ("pdfs", "pdf", "पीडीएफ्स", "पीडीएफ"),
    "study materials": ("study material", "study materials", "study resource", "अध्ययन सामग्री"),
}

SUBJECT_STOPWORDS = {
    "the", "and", "for", "with", "from", "part", "chapter", "unit", "topic",
    "lecture", "lectures", "video", "class", "classes", "session", "sessions", "notes", "note", "digital",
    "handwritten", "pdf", "century", "planner", "schedule", "timetable", "practice",
    "set", "sets", "question", "questions", "bank", "mcq", "test", "tests", "mock",
    "quiz", "exam", "exams", "study", "material", "materials", "resource", "resources",
    "no", "number", "hindi", "english", "file", "document", "download", "planners", "pdfs", "पीडीएफ्स", "प्लानर्स", "लेक्चर्स", "टेस्ट्स", "का", "की", "के", "और", "में", "से", "भाग",
    "लेक्चर", "क्लास", "नोट्स", "पीडीएफ", "सेंचुरी", "टेस्ट", "परीक्षा", "प्रैक्टिस", "सेट", "सामग्री",
}

@dataclass
class MessageRecord:
    message_id: int
    text: str
    date: Any = None
    filename: str | None = None
    mime_type: str | None = None

    @property
    def search_text(self) -> str:
        return " ".join(x for x in (self.text, self.filename) if x)

@dataclass
class IndexEntry:
    label: str
    key: str
    message_id: int | None = None
    resolved: bool = False


def normalize(text: str) -> str:
    """Normalize Unicode words while retaining combining marks (e.g. Devanagari)."""
    value = unicodedata.normalize("NFKC", (text or "").casefold())
    words, current = [], []
    for char in value:
        category = unicodedata.category(char)
        if category[0] in {"L", "M", "N"}:
            current.append(char)
        elif current:
            words.append("".join(current)); current = []
    if current: words.append("".join(current))
    return " ".join(words)


def parse_template(template: str) -> list[str]:
    entries: list[str] = []
    for line in (template or "").splitlines():
        raw, stripped = line.rstrip(), line.strip()
        if not stripped or stripped.startswith("📋"):
            continue
        candidate = re.sub(r"^[├└│─\s]+", "", stripped).strip()
        if not candidate:
            continue
        if raw == stripped and not any(x in raw for x in ("→", "├", "└")):
            continue
        if candidate not in entries:
            entries.append(candidate)
    return entries


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token

def _tokens(text: str) -> list[str]:
    return [_stem(token) for token in normalize(text).split()]


def _score(label: str, caption: str) -> int:
    """Precision-weighted score; generic one-word overlaps never qualify."""
    a, b = _tokens(label), _tokens(caption)
    if not a or not b:
        return 0
    aset, bset = set(a), set(b)
    if a == b:
        return 100
    if len(a) > 1 and any(b[i:i + len(a)] == a for i in range(len(b) - len(a) + 1)):
        return 100
    # Require a meaningful multi-token match. One generic token is insufficient.
    meaningful = {x for x in aset if len(x) >= 3}
    overlap = meaningful & bset
    if not overlap:
        return 0
    coverage = len(overlap) / max(1, len(meaningful))
    # Subject+material usually yields >= 2 terms. A single match needs strong
    # evidence from a longer exact token and is capped below the auto threshold.
    if len(meaningful) == 1:
        return 25
    return int(100 * coverage) if coverage >= 0.75 else int(65 * coverage)


def _oldest_key(message: MessageRecord):
    if message.date is not None and hasattr(message.date, "timestamp"):
        try:
            return (0, message.date.timestamp(), message.message_id)
        except (ValueError, OSError):
            pass
    # Telegram message IDs increase with creation time within a channel.
    return (1, message.message_id, message.message_id)


def match_oldest(label: str, messages: list[MessageRecord]) -> MessageRecord | None:
    matches = [m for m in messages if _score(label, m.search_text) >= 75]
    return min(matches, key=_oldest_key) if matches else None


def manual_index(template: str, messages: list[MessageRecord]):
    entries = []
    for label in parse_template(template):
        found = match_oldest(label, messages)
        entries.append(IndexEntry(label, normalize(label), found.message_id if found else None))
    return template, entries


def render_manual_template(template: str, entries: list[IndexEntry], channel_id: int, username: str | None = None):
    by_label = {entry.label: entry for entry in entries}
    output, missing = [], []
    cid = str(channel_id).removeprefix("-100")
    for raw in (template or "").splitlines():
        stripped = raw.strip()
        candidate = re.sub(r"^[├└│─\s]+", "", stripped).strip()
        entry = by_label.get(candidate)
        if not entry:
            output.append(html.escape(raw))
            continue
        if not entry.message_id:
            output.append(html.escape(raw)); missing.append(entry.label); continue
        prefix = raw[:len(raw) - len(raw.lstrip())]
        content = raw.lstrip()
        tree_prefix = content[:len(content) - len(candidate)]
        href = f"https://t.me/{username}/{entry.message_id}" if username else f"https://t.me/c/{cid}/{entry.message_id}"
        output.append(prefix + tree_prefix + f'<a href="{href}">{html.escape(candidate)}</a>')
    return "\n".join(output), missing


def detect_material(caption: str) -> str | None:
    text = f" {normalize(caption)} "
    for material, phrases in MATERIALS.items():
        for phrase in phrases:
            needle = f" {normalize(phrase)} "
            if needle.strip() and needle in text:
                return material
    return None


def _subject_from_caption(caption: str) -> str:
    words = _tokens(caption)
    subject_words = []
    for word in words:
        if word in SUBJECT_STOPWORDS or word.isdigit() or re.fullmatch(r"[a-f]?\d{1,4}", word):
            continue
        if len(word) == 1 and word.isascii():
            continue
        subject_words.append(word)
    return " ".join(subject_words[:3]).title() or "Unclassified"


def automatic_index(messages: list[MessageRecord]):
    groups: OrderedDict[str, OrderedDict[str, int]] = OrderedDict()
    unresolved: list[str] = []
    for msg in sorted(messages, key=_oldest_key):
        caption = msg.search_text.strip()
        if not caption:
            continue
        material = detect_material(caption)
        if not material:
            if len(unresolved) < 100 and any(ch.isalpha() for ch in caption):
                unresolved.append(caption[:180])
            continue
        subject = _subject_from_caption(caption)
        groups.setdefault(subject, OrderedDict()).setdefault(material, msg.message_id)
    return groups, unresolved


def entries_from_groups(groups: OrderedDict) -> list[IndexEntry]:
    return [IndexEntry(f"{subject} {material}", f"{normalize(subject)}::{material}", int(message_id))
            for subject, materials in groups.items() for material, message_id in materials.items()]


def render_automatic_index(entries: list[IndexEntry], channel_id: int, title="📋 ɴᴀᴠɪɢᴀᴛɪᴏɴ ɪɴᴅᴇx", username: str | None = None):
    lines = [html.escape(title)]
    missing = []
    grouped: OrderedDict[str, list[IndexEntry]] = OrderedDict()
    for entry in entries:
        if "::" in entry.key:
            material = entry.key.split("::", 1)[1]
            subject = entry.label[:-len(material)].rstrip() if material and entry.label.lower().endswith(material.lower()) else entry.label
        else:
            subject = entry.label.rsplit(" ", 1)[0] if " " in entry.label else entry.label
        grouped.setdefault(subject, []).append(entry)
    cid = str(channel_id).removeprefix("-100")
    for subject, group in grouped.items():
        lines.append(f"\n<b>{html.escape(subject)}</b>")
        for entry in group:
            if entry.message_id:
                href = f"https://t.me/{username}/{entry.message_id}" if username else f"https://t.me/c/{cid}/{entry.message_id}"
                lines.append(f'<a href="{href}">{html.escape(entry.label)}</a>')
            else:
                lines.append(html.escape(entry.label)); missing.append(entry.label)
    return "\n".join(lines), missing


def render_index(channel_id, entries, bot_username="", title="📋 ɴᴀᴠɪɢᴀᴛɪᴏɴ ɪɴᴅᴇx", username: str | None = None):
    lines, missing = [html.escape(title)], []
    for entry in entries:
        if entry.message_id:
            link = f"https://t.me/{username}/{entry.message_id}" if username else f"https://t.me/c/{str(channel_id).removeprefix('-100')}/{entry.message_id}"
            lines.append(f'<a href="{link}">{html.escape(entry.label)}</a>')
        else:
            lines.append(html.escape(entry.label)); missing.append(entry.label)
    return "\n".join(lines), missing


class StreamingIndexBuilder:
    """Bounded-memory builder that keeps oldest matches and serializable state."""
    def __init__(self, mode: str, template: str = ""):
        if mode not in {"manual", "automatic"}:
            raise ValueError("mode must be manual or automatic")
        self.mode, self.template = mode, template
        self.entries = [IndexEntry(label, normalize(label)) for label in parse_template(template)] if mode == "manual" else []
        self.groups: OrderedDict[str, OrderedDict[str, int]] = OrderedDict()
        self.unresolved_captions: list[str] = []
        self.processed = 0

    def add(self, msg: MessageRecord):
        self.processed += 1
        caption = msg.search_text.strip()
        if not caption:
            return
        if self.mode == "manual":
            for entry in self.entries:
                if entry.message_id is None and _score(entry.label, caption) >= 75:
                    entry.message_id = msg.message_id
            return
        material = detect_material(caption)
        if not material:
            if len(self.unresolved_captions) < 100 and any(ch.isalpha() for ch in caption):
                self.unresolved_captions.append(caption[:180])
            return
        subject = _subject_from_caption(caption)
        self.groups.setdefault(subject, OrderedDict()).setdefault(material, msg.message_id)

    def apply_resolutions(self, resolutions: dict[str, int], validator=None):
        """Apply saved user overrides by stable entry label/key after scan."""
        if self.mode == "manual":
            for entry in self.entries:
                if entry.label in resolutions:
                    entry.message_id = int(resolutions[entry.label]); entry.resolved = True
        else:
            entries = entries_from_groups(self.groups)
            for entry in entries:
                if entry.label in resolutions:
                    entry.message_id = int(resolutions[entry.label]); entry.resolved = True
            rebuilt: OrderedDict[str, OrderedDict[str, int]] = OrderedDict()
            for entry in entries:
                if "::" in entry.key:
                    material = entry.key.split("::", 1)[1]
                    subject = entry.label[:-len(material)].rstrip() if material and entry.label.lower().endswith(material.lower()) else entry.label
                else:
                    subject, material = entry.label.rsplit(" ", 1)
                rebuilt.setdefault(subject, OrderedDict())[material] = int(entry.message_id)
            self.groups = rebuilt

    def result(self):
        if self.mode == "manual":
            return self.entries, OrderedDict(), list(self.unresolved_captions)
        return entries_from_groups(self.groups), self.groups, list(self.unresolved_captions)

    def snapshot(self):
        return {"entries": [e.__dict__.copy() for e in self.entries],
                "groups": {s: dict(m) for s, m in self.groups.items()},
                "unresolved_captions": self.unresolved_captions[-100:], "processed": self.processed}

    def restore(self, snapshot):
        if not snapshot:
            return self
        self.entries = [IndexEntry(**item) for item in snapshot.get("entries", [])]
        self.groups = OrderedDict((s, OrderedDict((k, int(v)) for k, v in m.items()))
                                  for s, m in snapshot.get("groups", {}).items())
        self.unresolved_captions = list(snapshot.get("unresolved_captions", []))[:100]
        self.processed = int(snapshot.get("processed", 0))
        return self
