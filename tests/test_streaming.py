from datetime import datetime, timezone
from app.indexer import MessageRecord, StreamingIndexBuilder
from app.pagination import page_slice
from app.html_chunks import split_html
from html.parser import HTMLParser


def msg(i, text):
    return MessageRecord(i, text, datetime.fromtimestamp(i, timezone.utc))


def test_streaming_manual_keeps_oldest_and_restores_checkpoint():
    b = StreamingIndexBuilder("manual", "📋 Index\nSubject\n├ Subject → Lectures")
    b.add(msg(10, "Subject Lecture 02"))
    b.add(msg(20, "Subject Lecture 01"))
    entries, _, _ = b.result()
    assert entries[0].message_id == 10
    restored = StreamingIndexBuilder("manual", b.template).restore(b.snapshot())
    restored.add(msg(30, "Subject Lecture 00"))
    assert restored.result()[0][0].message_id == 10


def test_streaming_auto_groups_and_does_not_create_per_lecture_categories():
    b = StreamingIndexBuilder("automatic")
    for i in range(1, 30):
        b.add(msg(i, f"Classification Lecture {i}"))
    b.add(msg(31, "Classification Digital Notes"))
    entries, groups, uncertain = b.result()
    assert list(groups) == ["Classification"]
    assert list(groups["Classification"]) == ["lectures", "digital notes"]
    assert len(entries) == 2


def test_snapshot_restores_auto_categories():
    b = StreamingIndexBuilder("automatic")
    b.add(msg(1, "Anatomy Lecture 01"))
    restored = StreamingIndexBuilder("automatic").restore(b.snapshot())
    restored.add(msg(2, "Anatomy Lecture 02"))
    entries, groups, _ = restored.result()
    assert len(entries) == 1
    assert groups["Anatomy"]["lectures"] == 1


def test_pagination_clamps_and_slices():
    items = list(range(19))
    page, index, count = page_slice(items, 99, 8)
    assert (page, index, count) == ([16, 17, 18], 2, 3)
    assert page_slice(items, -1, 8)[1:] == (0, 3)


class TagBalance(HTMLParser):
    def __init__(self):
        super().__init__(); self.stack=[]
    def handle_starttag(self, tag, attrs):
        if tag != "br": self.stack.append(tag)
    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag
        self.stack.pop()


def test_html_chunks_are_bounded_and_each_chunk_has_balanced_tags():
    source = "<b>Header</b> " + "<a href='https://t.me/c/1/2'>" + ("hello &amp; world " * 80) + "</a>"
    chunks = split_html(source, 150)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 150
        parser = TagBalance(); parser.feed(chunk); parser.close()
        assert parser.stack == []
