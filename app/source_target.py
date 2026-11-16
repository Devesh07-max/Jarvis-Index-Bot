"""Source-to-target index reconstruction helpers.

The workflow intentionally uses source hyperlinks as the authoritative mapping,
then maps the linked source messages to the target channel's ordered file range.
Caption comparison ignores website URLs and trailing owner signatures.
"""
from __future__ import annotations
import html, re, unicodedata, copy
from urllib.parse import urlparse


def parse_tme_message_url(value: str):
    value=(value or '').strip()
    m=re.search(r'(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?(c/)?([^/?#]+)/(?:(\d+)/)?(\d+)', value)
    if not m: return None
    private=bool(m.group(1)); channel=m.group(2); mid=int(m.group(4))
    if private and channel.isdigit(): channel_id=int('-100'+channel)
    elif channel.lstrip('-').isdigit(): channel_id=int(channel) if channel.startswith('-100') else int('-100'+channel)
    else: channel_id=channel
    return channel_id,mid


def clean_caption(value: str) -> str:
    value=unicodedata.normalize('NFKC', value or '').casefold()
    # Ignore URLs (the common source/target difference) and trailing attribution.
    value=re.sub(r'https?://\S+|www\.\S+', ' ', value)
    value=re.sub(r'(?im)^\s*(?:uploaded\s+by|shared\s+by|by)\s*[:：-].*$', ' ', value)
    value=re.sub(r'(?i)\s+\bby\s+[\w@.-]+\s*$', ' ', value)
    value=re.sub(r'[^\w\s]', ' ', value)
    return ' '.join(value.split())


def _utf16_to_index(text: str, offset: int) -> int:
    units=0
    for i,ch in enumerate(text):
        if units >= offset: return i
        units += 2 if ord(ch)>0xFFFF else 1
    return len(text)


def source_links(message):
    """Return (visible_start, visible_end, url) for clickable entities."""
    text=getattr(message,'message','') or ''
    found=[]
    for ent in getattr(message,'entities',[]) or []:
        name=type(ent).__name__
        if name not in {'MessageEntityTextUrl','MessageEntityUrl'}: continue
        start=_utf16_to_index(text,int(ent.offset)); end=_utf16_to_index(text,int(ent.offset+ent.length))
        url=getattr(ent,'url',None) or text[start:end]
        if parse_tme_message_url(url): found.append((start,end,url))
    return sorted(found)


def target_message_url(target_channel_id: int, target_message_id: int, target_username: str|None=None) -> str:
    if target_username:
        return f'https://t.me/{target_username}/{int(target_message_id)}'
    return f'https://t.me/c/{str(target_channel_id).removeprefix("-100")}/{int(target_message_id)}'


def render_linked_index(message, source_to_target: dict[int,int], target_channel_id: int, target_username: str|None=None):
    """Rewrite TextUrl entities while preserving the source index's formatting.

    Telethon's HTML unparser preserves bold/italic/underline/spoiler/code entities,
    unlike rebuilding the message from plain text. The visible index text remains
    exactly as authored in the source channel.
    """
    text=getattr(message,'message','') or ''
    rewritten=copy.deepcopy(getattr(message,'entities',[]) or [])
    linked=missing=0
    kept=[]
    for entity in rewritten:
        name=type(entity).__name__
        if name not in {'MessageEntityTextUrl','MessageEntityUrl'}:
            kept.append(entity)
            continue
        start=_utf16_to_index(text,int(entity.offset)); end=_utf16_to_index(text,int(entity.offset+entity.length))
        url=getattr(entity,'url',None) or text[start:end]
        parsed=parse_tme_message_url(url)
        if not parsed:
            kept.append(entity)
            continue
        target_id=source_to_target.get(int(parsed[1]))
        if not target_id:
            # Never leave an unresolved source-channel URL active in the target index.
            missing+=1
            continue
        dest=target_message_url(target_channel_id,target_id,target_username)
        if name=='MessageEntityTextUrl':
            entity.url=dest
            kept.append(entity)
        else:
            # Convert visible-URL entities to hidden target links without shifting text.
            try:
                from telethon.tl.types import MessageEntityTextUrl
                kept.append(MessageEntityTextUrl(offset=entity.offset,length=entity.length,url=dest))
            except Exception:
                missing+=1
                continue
        linked+=1
    rewritten=kept
    try:
        from telethon.extensions import html as telethon_html
        rendered=telethon_html.unparse(text,rewritten)
    except Exception:
        # Safe fallback retains text and clickable links, though formatting may be
        # reduced on environments where Telethon's HTML extension is unavailable.
        out=[]; cursor=0
        for start,end,url in source_links(message):
            parsed=parse_tme_message_url(url); target_id=source_to_target.get(int(parsed[1])) if parsed else None
            out.append(html.escape(text[cursor:start])); label=html.escape(text[start:end])
            if target_id:
                dest=target_message_url(target_channel_id,target_id,target_username)
                out.append(f'<a href="{html.escape(dest, quote=True)}">{label}</a>')
            else:
                out.append(label)
            cursor=end
        out.append(html.escape(text[cursor:])); rendered=''.join(out)
    return rendered,linked,missing


def normalize_index_text(value: str) -> str:
    """Normalize harmless whitespace differences when validating copied indexes."""
    return ' '.join(unicodedata.normalize('NFKC', value or '').split())


def _message_filename(message) -> str:
    """Best-effort filename extraction from Telegram document/media metadata."""
    media = getattr(message, "media", None)
    document = getattr(media, "document", None) if media else None
    for attr in (getattr(document, "attributes", None) or []):
        if type(attr).__name__ == "DocumentAttributeFilename":
            return clean_caption(getattr(attr, "file_name", "") or "")
    return clean_caption(getattr(message, "file_name", "") or getattr(message, "filename", "") or "")


def _similarity(left: str, right: str) -> float:
    """Token-aware caption similarity, tolerant of URL/signature differences."""
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    from difflib import SequenceMatcher
    a, b = set(left.split()), set(right.split())
    overlap = len(a & b) / max(1, len(a | b))
    containment = len(a & b) / max(1, min(len(a), len(b)))
    sequence = SequenceMatcher(None, left, right).ratio()
    return max(sequence, 0.65 * overlap + 0.35 * containment)


def _pair_score(source, target, source_index: int, target_index: int, source_count: int, target_count: int) -> float:
    source_caption = clean_caption(getattr(source, "message", "") or "")
    target_caption = clean_caption(getattr(target, "message", "") or "")
    source_name = _message_filename(source)
    target_name = _message_filename(target)
    caption_score = _similarity(source_caption, target_caption)
    filename_score = _similarity(source_name, target_name)
    if source_name and target_name and source_caption and target_caption:
        # Require corroboration from both caption and filename. This prevents
        # repeated generic captions (e.g. "Lecture 01") from overriding distinct
        # filenames and producing a confident but incorrect one-to-one pairing.
        content_score = 0.55 * filename_score + 0.45 * caption_score
    elif source_name and target_name:
        content_score = filename_score
    else:
        content_score = caption_score
    # Relative order is only a tie-breaker, not the primary matching criterion.
    sp = source_index / max(1, source_count - 1)
    tp = target_index / max(1, target_count - 1)
    position_score = 1.0 - min(1.0, abs(sp - tp))
    return 0.96 * content_score + 0.04 * position_score


def match_messages(source_messages, target_messages, threshold: float = 0.58):
    """One-to-one caption/filename matching that supports reordered channels.

    Website URLs and common attribution lines are removed before comparison.
    Relative position is a small tie-breaker only. Unmatched and ambiguous
    cases are reported for manual review rather than paired by position.
    """
    mapping, warnings = {}, []
    if not source_messages or not target_messages:
        return mapping, ["One of the selected file ranges is empty."]
    sources = sorted(source_messages, key=lambda m: int(m.id))
    targets = sorted(target_messages, key=lambda m: int(m.id))
    candidates = []
    for si, source in enumerate(sources):
        for ti, target in enumerate(targets):
            candidates.append((_pair_score(source, target, si, ti, len(sources), len(targets)), si, ti))
    candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
    used_s, used_t, scores = set(), set(), {}
    for score, si, ti in candidates:
        if score < threshold or si in used_s or ti in used_t:
            continue
        used_s.add(si); used_t.add(ti)
        sid, tid = int(sources[si].id), int(targets[ti].id)
        mapping[sid] = tid; scores[sid] = score
    for si, source in enumerate(sources):
        sid = int(source.id)
        if si not in used_s:
            warnings.append(f"No reliable target match found for source message #{sid}.")
        elif scores[sid] < 0.78:
            warnings.append(f"Low-confidence match for source message #{sid} → target message #{mapping[sid]} (review recommended).")
    for sid, tid in mapping.items():
        source = next(m for m in sources if int(m.id) == sid)
        sc = clean_caption(getattr(source, "message", "") or "")
        if sc:
            similar = [m for m in targets if _similarity(sc, clean_caption(getattr(m, "message", "") or "")) >= 0.92]
            if len(similar) > 1:
                warnings.append(f"Duplicate/ambiguous caption near source #{sid}; verify the selected target #{tid}.")
    if len(used_t) < len(targets):
        warnings.append(f"{len(targets) - len(used_t)} target message(s) were not used; extra target files may be present.")
    return mapping, warnings


def match_ordered_messages(source_messages, target_messages):
    """Backward-compatible alias; matching is no longer order-dependent."""
    return match_messages(source_messages, target_messages)
