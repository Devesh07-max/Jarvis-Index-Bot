"""Deterministic, streaming Telegram channel index construction.

The matcher intentionally favours precision over recall: uncertain matches are
left unresolved for user review rather than linking a wrong message.
"""
from __future__ import annotations

import re
import html
import unicodedata
from difflib import SequenceMatcher
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

# More specific phrases must precede generic document terms.
MATERIALS: dict[str, tuple[str, ...]] = {
    "century pdf": ("century pdfs", "century pdf", "सेंचुरी पीडीएफ्स", "सेंचुरी पीडीएफ", "सेंचुरी pdf"),
    "digital notes": ("digital notes", "digital note", "handwritten notes", "class notes", "notes", "नोट्स"),
    "syllabus": ("syllabus", "syllabi", "course outline", "पाठ्यक्रम"),
    "class worksheets": ("class worksheet", "class worksheets", "worksheet", "worksheets", "कार्यपत्रक"),
    "handouts": ("handout", "handouts", "सहायक सामग्री"),
    "practice sets": ("daily practice problems", "daily practice paper", "practice set", "practice questions", "question bank", "dpp", "pyq", "previous year questions", "mcq", "प्रैक्टिस सेट", "प्रैक्टिस प्रश्न"),
    "tests": ("mock tests", "mock test", "test series", "test paper", "tests", "test", "quizzes", "quiz", "exams", "exam", "टेस्ट्स", "टेस्ट", "परीक्षा"),
    "planners": ("planners", "planner", "study plan", "schedule", "timetable", "प्लानर", "प्लानर्स", "समय सारणी"),
    "lectures": ("video lecture", "video lectures", "lectures", "lecture", "classes", "class", "sessions", "session", "लेक्चर्स", "लेcture", "लेक्चर", "व्याख्यान", "क्लास"),
    "pdfs": ("pdfs", "pdf", "पीडीएफ्स", "पीडीएफ"),
    "study materials": ("study material", "study materials", "study resource", "अध्ययन सामग्री"),
}

SUBJECT_STOPWORDS = {
    "the", "and", "for", "with", "from", "part", "chapter", "unit", "topic",
    "lecture", "lectures", "video", "class", "classes", "session", "sessions", "notes", "note", "digital", "syllabus", "syllabi", "worksheet", "worksheets", "handout", "handouts",
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


# Explicitly decode Telegram small-cap Unicode characters; NFKC alone does
# not map all of these glyphs to ordinary Latin characters.
_SMALL_CAPS = str.maketrans({
    "ᴀ":"a","ʙ":"b","ᴄ":"c","ᴅ":"d","ᴇ":"e","ꜰ":"f","ɢ":"g","ʜ":"h",
    "ɪ":"i","ᴊ":"j","ᴋ":"k","ʟ":"l","ᴍ":"m","ɴ":"n","ᴏ":"o","ᴘ":"p",
    "ǫ":"q","ʀ":"r","ꜱ":"s","ᴛ":"t","ᴜ":"u","ᴠ":"v","ᴡ":"w","ʏ":"y","ᴢ":"z",
    "ı":"i",
})
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\U0000200D]+")

def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", (text or "").translate(_SMALL_CAPS).casefold())
    words, current = [], []
    for char in value:
        if unicodedata.category(char)[0] in {"L", "M", "N"}:
            current.append(char)
        elif current:
            words.append("".join(current)); current = []
    if current:
        words.append("".join(current))
    return " ".join(words)


def _canonical_label(label: str) -> str:
    value = (label or "").replace("→", " ").replace("➜", " ").replace("&", " and ")
    value = _EMOJI_RE.sub(" ", value)
    value = re.sub(r"^[├└│─\s]+", " ", value)
    return normalize(value)


def parse_template(template: str) -> list[str]:
    """Extract actionable arrow rows; preserve exact row text for rendering."""
    entries: list[str] = []
    for line in (template or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("📋"):
            continue
        if "→" not in stripped and "➜" not in stripped:
            continue
        candidate = re.sub(r"^[├└│─\s]+", "", stripped).strip()
        if candidate and candidate not in entries:
            entries.append(candidate)
    return entries


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _tokens(text: str) -> list[str]:
    return [_stem(token) for token in _canonical_label(text).split()]


_TOPIC_ALIASES = {
    "profit and loss": ("profit loss", "profit n loss", "p and l", "p l", "p&l"),
    "simple interest": ("simple int",),
    "compound interest": ("compound int",),
    "ratio and proportion": ("ratio proportion", "ratio n proportion"),
    "mixture and allegation": ("mixture allegation", "mixture alligation", "mixture & alligation"),
    "time and work": ("time work",),
    "percentage": ("percent", "percentages"),
    "number system": ("number systems",),
    "mensuration": ("mensuration 2d 3d",),
}

def _expanded_tokens(text: str) -> list[str]:
    value = _canonical_label(text)
    for canonical, aliases in _TOPIC_ALIASES.items():
        for alias in aliases:
            alias_norm = normalize(alias)
            value = re.sub(rf"(?<!\w){re.escape(alias_norm)}(?!\w)", normalize(canonical), value)
    return [_stem(token) for token in value.split()]


def _score(label: str, caption: str) -> int:
    """Hybrid exact/alias/fuzzy score; topic and material context must agree."""
    requested = _expanded_tokens(label)
    candidates = [x for x in _expanded_tokens(caption) if not re.fullmatch(r"(?:part)?\d{1,4}", x)]
    if not requested or not candidates:
        return 0
    if requested == candidates:
        return 100
    if len(requested) > 1 and any(candidates[i:i+len(requested)] == requested for i in range(len(candidates)-len(requested)+1)):
        return 100

    generic = set(SUBJECT_STOPWORDS) | {
        "and", "or", "the", "a", "an", "pdf", "lecture", "lectures", "notes", "tests",
        "test", "quiz", "quizzes", "worksheet", "worksheets", "planner", "planners",
        "class", "classes", "part", "chapter", "unit", "topic", "download",
    }
    meaningful = [x for x in requested if x not in generic and len(x) >= 2]
    if not meaningful:
        return 0
    matched = 0.0
    for token in meaningful:
        best = max((1.0 if token == candidate else SequenceMatcher(None, token, candidate).ratio()
                    if min(len(token), len(candidate)) >= 4 else 0.0 for candidate in candidates), default=0.0)
        if best >= 0.84:
            matched += best
    coverage = matched / len(meaningful)
    if len(meaningful) == 1:
        # A single broad word (e.g. "tests", "basic") is not a reliable topic.
        return 82 if coverage == 1 and len(meaningful[0]) >= 7 else 0
    if coverage >= 0.95: return 96
    if coverage >= 0.80: return 86
    if coverage >= 0.67: return 76
    return int(coverage * 60)


def _topic_in(text: str) -> str | None:
    """Return a known topic only when its complete phrase/alias is present."""
    value = f" {_canonical_label(text)} "
    topics = sorted(_KNOWN_TOPICS, key=len, reverse=True)
    for topic in topics:
        aliases = _TOPIC_ALIASES.get(topic, ())
        for phrase in (topic, *aliases):
            needle = f" {_canonical_label(phrase)} "
            if needle.strip() and needle in value:
                # Short SI/CI aliases are intentionally not inferred alone.
                if normalize(phrase) in {"si", "ci"} and not any(x in value for x in (" simple interest ", " compound interest ")):
                    continue
                return topic
    return None


def _message_match_score(label: str, message: MessageRecord) -> int:
    """Score caption and filename independently; reject topic/material conflicts."""
    caption = (message.text or "").strip()
    filename = (message.filename or "").strip()
    target_topic = _topic_in(label)
    target_material = detect_material(label)
    caption_topic = _topic_in(caption) if caption else None
    filename_topic = _topic_in(filename) if filename else None

    # A caption and filename that explicitly name different chapters are
    # contradictory evidence; do not guess which one is correct.
    if target_topic and caption_topic and target_topic != caption_topic:
        return 0
    if target_topic and filename_topic and filename_topic != target_topic:
        return 0
    if caption_topic and filename_topic and caption_topic != filename_topic:
        return 0

    scores = []
    for value in (caption, filename):
        if not value:
            continue
        found_material = detect_material(value)
        # A category-specific row needs evidence for that material. A bare
        # chapter heading must not become a lecture/notes/test link.
        if target_material and not found_material:
            continue
        # A requested material cannot be linked to a message that explicitly
        # identifies itself as a different material (e.g. lecture vs notes).
        if target_material and found_material != target_material:
            continue
        scores.append(_score(label, value))
    return max(scores, default=0)

def _oldest_key(message: MessageRecord):
    if message.date is not None and hasattr(message.date, "timestamp"):
        try:
            return (0, message.date.timestamp(), message.message_id)
        except (ValueError, OSError):
            pass
    # Telegram message IDs increase with creation time within a channel.
    return (1, message.message_id, message.message_id)


def match_oldest(label: str, messages: list[MessageRecord]) -> MessageRecord | None:
    # Search every message, but only accept a high-confidence independent
    # caption/filename match. Never combine unrelated caption and filename words.
    matches = [m for m in messages if _message_match_score(label, m) >= 75]
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


_KNOWN_TOPICS = (
    "ratio and proportion", "profit and loss", "simple interest",
    "compound interest", "mixture and allegation", "time and work",
    "partnership", "percentage", "discount", "average", "age",
    "fraction", "number system", "mensuration", "geometry",
    "algebra", "trigonometry", "probability", "venn diagram",
    "weighted average", "dishonest dealer", "installment",
    "income expenditure",
)

def _subject_from_caption(caption: str) -> str:
    normalized = f" {_canonical_label(caption)} "
    known = _topic_in(caption)
    if known:
        return known.title()

    # Remove material/category words before deriving an unknown subject from a
    # filename or caption. This prevents labels such as "Digital Notes" or
    # "Lecture 01" from becoming standalone chapters.
    words = _tokens(caption)
    subject_words = []
    for word in words:
        if word in SUBJECT_STOPWORDS or word in {"and", "or", "the", "a", "an", "download", "latest"}:
            continue
        if word.isdigit() or re.fullmatch(r"[a-f]?\d{1,4}", word):
            continue
        if len(word) == 1 and word.isascii():
            continue
        subject_words.append(word)
    # A single unknown token is too weak to invent a new chapter (examples
    # seen in channel captions: Election, Income, Basic, Weighted, MP).
    if not subject_words:
        return "Unclassified"
    if len(subject_words) == 1:
        ambiguous_single = {"election", "income", "basic", "weighted", "dishonest",
                            "miscellaneous", "miscellaneou", "mp", "venn", "merging",
                            "profit", "simple", "compound", "ratio", "proportion",
                            "mixture", "time", "work", "math", "mathematics"}
        if subject_words[0] in ambiguous_single:
            return "Unclassified"
    return " ".join(subject_words[:3]).title()


def _classify_message(msg: MessageRecord) -> tuple[str, str] | None:
    """Classify caption and filename as separate evidence sources.

    Caption wins when it identifies a topic. Filename supplies missing topic
    or material metadata only when it does not contradict the caption.
    """
    caption = (msg.text or "").strip()
    filename = (msg.filename or "").strip()
    cap_topic = _subject_from_caption(caption) if caption else "Unclassified"
    file_topic = _subject_from_caption(filename) if filename else "Unclassified"
    cap_material = detect_material(caption) if caption else None
    file_material = detect_material(filename) if filename else None

    if (cap_topic != "Unclassified" and file_topic != "Unclassified"
            and normalize(cap_topic) != normalize(file_topic)):
        return None

    if cap_topic != "Unclassified":
        subject = cap_topic
        same_file_topic = file_topic == "Unclassified" or normalize(file_topic) == normalize(cap_topic)
        material = cap_material or (file_material if same_file_topic else None)
    elif file_topic != "Unclassified":
        subject = file_topic
        material = file_material or cap_material
    else:
        return None

    if not material:
        return None
    return subject, material

def automatic_index(messages: list[MessageRecord]):
    builder = StreamingIndexBuilder("automatic")
    for message in sorted(messages, key=_oldest_key):
        builder.add(message)
    entries, groups, unresolved = builder.result()
    return groups, unresolved

def entries_from_groups(groups: OrderedDict) -> list[IndexEntry]:
    return [IndexEntry(f"{subject} {material}", f"{normalize(subject)}::{material}", int(message_id))
            for subject, materials in groups.items() for material, message_id in materials.items()]


_SMALL_STYLE = str.maketrans({
    "a":"ᴀ","b":"ʙ","c":"ᴄ","d":"ᴅ","e":"ᴇ","f":"ꜰ","g":"ɢ","h":"ʜ",
    "i":"ɪ","j":"ᴊ","k":"ᴋ","l":"ʟ","m":"ᴍ","n":"ɴ","o":"ᴏ","p":"ᴘ",
    "q":"ǫ","r":"ʀ","s":"ꜱ","t":"ᴛ","u":"ᴜ","v":"ᴠ","w":"ᴡ","y":"ʏ","z":"ᴢ",
})

def small_caps(text: str) -> str:
    return (text or "").lower().translate(_SMALL_STYLE)


def render_automatic_index(entries: list[IndexEntry], channel_id: int, title="📋 ɴᴀᴠɪɢᴀᴛɪᴏɴ ɪɴᴅᴇx", username: str | None = None, channel_title: str | None = None):
    lines = [html.escape(title)]
    if channel_title:
        lines.append(f"\n📚 <b>{html.escape(small_caps(channel_title))}</b>")
    missing = []
    grouped: OrderedDict[str, list[IndexEntry]] = OrderedDict()
    for entry in entries:
        if "::" in entry.key:
            subject, _material = entry.key.split("::", 1)
        else:
            subject = entry.label.rsplit(" ", 1)[0] if " " in entry.label else entry.label
        grouped.setdefault(subject, []).append(entry)
    cid = str(channel_id).removeprefix("-100")
    for subject, group in grouped.items():
        lines.append(f"\n📖 <b>{html.escape(small_caps(subject))}</b>")
        for entry in group:
            material = entry.key.split("::", 1)[1] if "::" in entry.key else entry.label.rsplit(" ", 1)[-1]
            icon = "🎬" if material == "lectures" else "📝" if material in {"tests", "practice sets"} else "📄"
            display = f"{icon} {small_caps(subject)} → {small_caps(material)}"
            if entry.message_id:
                href = f"https://t.me/{username}/{entry.message_id}" if username else f"https://t.me/c/{cid}/{entry.message_id}"
                lines.append(f'<a href="{href}">{html.escape(display)}</a>')
            else:
                lines.append(html.escape(display)); missing.append(entry.label)
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
        self.context_topic: str | None = None
        self.context_message_id: int | None = None
        self.context_date: Any = None

    def _context_is_fresh(self, msg: MessageRecord) -> bool:
        if not self.context_topic or self.context_message_id is None:
            return False
        id_gap = int(msg.message_id) - int(self.context_message_id)
        fresh = 0 <= id_gap <= 5
        if self.context_date is not None and msg.date is not None:
            try:
                delta = (msg.date - self.context_date).total_seconds()
                fresh = fresh and 0 <= delta <= 6 * 3600
            except (TypeError, ValueError):
                return False
        return fresh

    def add(self, msg: MessageRecord):
        self.processed += 1
        caption = (msg.text or "").strip()
        filename = (msg.filename or "").strip()
        evidence = caption or filename
        if not evidence:
            return

        # A standalone known chapter heading can provide short-lived context
        # for a nearby generic attachment such as notes.pdf or quiz.pdf.
        caption_topic = _topic_in(caption) if caption else None
        if caption_topic:
            self.context_topic = caption_topic.title()
            self.context_message_id = int(msg.message_id)
            self.context_date = msg.date

        if self.mode == "manual":
            for entry in self.entries:
                if entry.message_id is not None:
                    continue
                score = _message_match_score(entry.label, msg)
                if score < 75:
                    cap_material = detect_material(caption) if caption else None
                    file_material = detect_material(filename) if filename else None
                    material = cap_material or file_material
                    file_topic = _topic_in(filename) if filename else None
                    caption_explicit_topic = _topic_in(caption) if caption else None
                    material_conflict = bool(cap_material and file_material and cap_material != file_material)
                    topic_conflict = bool(file_topic and self.context_topic and file_topic.title() != self.context_topic)
                    topic_conflict = topic_conflict or bool(caption_explicit_topic and self.context_topic and caption_explicit_topic.title() != self.context_topic)
                    if material and self._context_is_fresh(msg) and not material_conflict and not topic_conflict:
                        contextual = MessageRecord(msg.message_id, f"{self.context_topic} {material}", msg.date)
                        score = _message_match_score(entry.label, contextual)
                if score >= 75:
                    entry.message_id = msg.message_id
            return

        classified = _classify_message(msg)
        if not classified:
            cap_material = detect_material(caption) if caption else None
            file_material = detect_material(filename) if filename else None
            material_conflict = bool(cap_material and file_material and cap_material != file_material)
            material = cap_material or file_material
            explicit_topic = _topic_in(filename) if filename else None
            topic_conflict = bool(explicit_topic and self.context_topic and explicit_topic.title() != self.context_topic)
            if material and self._context_is_fresh(msg) and not material_conflict and not topic_conflict:
                classified = (self.context_topic, material)
            else:
                if len(self.unresolved_captions) < 100 and any(ch.isalpha() for ch in evidence):
                    self.unresolved_captions.append(evidence[:180])
                return
        subject, material = classified
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
                "unresolved_captions": self.unresolved_captions[-100:], "processed": self.processed,
                "context_topic": self.context_topic, "context_message_id": self.context_message_id,
                "context_date": self.context_date}

    def restore(self, snapshot):
        if not snapshot:
            return self
        self.entries = [IndexEntry(**item) for item in snapshot.get("entries", [])]
        self.groups = OrderedDict((s, OrderedDict((k, int(v)) for k, v in m.items()))
                                  for s, m in snapshot.get("groups", {}).items())
        self.unresolved_captions = list(snapshot.get("unresolved_captions", []))[:100]
        self.processed = int(snapshot.get("processed", 0))
        self.context_topic = snapshot.get("context_topic")
        self.context_message_id = snapshot.get("context_message_id")
        self.context_date = snapshot.get("context_date")
        return self
