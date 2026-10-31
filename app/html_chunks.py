"""Split Telegram HTML safely without cutting tags, entities, or Unicode codepoints."""
from html.parser import HTMLParser
import re

class _Tokens(HTMLParser):
    VOID = {"br"}
    def __init__(self):
        super().__init__(convert_charrefs=False); self.tokens=[]
    def handle_starttag(self, tag, attrs): self.tokens.append((self.get_starttag_text(), "open", tag))
    def handle_startendtag(self, tag, attrs): self.tokens.append((self.get_starttag_text(), "void", tag))
    def handle_endtag(self, tag): self.tokens.append((f"</{tag}>", "close", tag))
    def handle_data(self, data): self.tokens.append((data, "text", None))
    def handle_entityref(self, name): self.tokens.append((f"&{name};", "entity", None))
    def handle_charref(self, name): self.tokens.append((f"&#{name};", "entity", None))
    def handle_comment(self, data): self.tokens.append((f"<!--{data}-->", "void", None))

def split_html(text: str, limit: int = 3900) -> list[str]:
    """Return independently parseable HTML chunks, each no longer than limit."""
    if limit < 64: raise ValueError("limit must be at least 64")
    parser = _Tokens(); parser.feed(text); parser.close()
    chunks=[]; current=""; stack=[]
    def closers(): return "".join(f"</{tag}>" for tag, _ in reversed(stack))
    def flush():
        nonlocal current
        if current:
            chunks.append(current + closers())
            current = "".join(opening for _, opening in stack)
            if len(current) + len(closers()) > limit:
                raise ValueError("HTML nesting exceeds Telegram message limit")
    for token, kind, tag in parser.tokens:
        if kind == "text":
            pos=0
            while pos < len(token):
                room = limit - len(current) - len(closers())
                if room <= 0:
                    flush(); room = limit - len(current) - len(closers())
                take=min(room, len(token)-pos)
                if take <= 0: raise ValueError("Unable to split HTML text safely")
                current += token[pos:pos+take]; pos += take
                if pos < len(token): flush()
            continue
        if kind == "open":
            if len(current) + len(token) + len(closers()) + len(f"</{tag}>") > limit:
                flush()
            if len(token) + len(current) + len(closers()) + len(f"</{tag}>") > limit:
                raise ValueError("HTML tag exceeds message limit")
            current += token; stack.append((tag, token))
        elif kind == "close":
            if len(current) + len(token) + len(closers()) > limit: flush()
            current += token
            for i in range(len(stack)-1, -1, -1):
                if stack[i][0] == tag:
                    stack=stack[:i]; break
        else:
            if len(current) + len(token) + len(closers()) > limit: flush()
            if len(token) + len(current) + len(closers()) > limit:
                raise ValueError("HTML token exceeds message limit")
            current += token
    if current.strip(): chunks.append(current + closers())
    return chunks or [""]
