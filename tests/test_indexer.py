from datetime import datetime, timezone, timedelta
from app.indexer import MessageRecord, manual_index, automatic_index, parse_template, match_oldest

def test_template_preserves_original_and_extracts_leaf_entries():
    template='📋 ɴᴀᴠɪɢᴀᴛɪᴏɴ ɪɴᴅᴇx\n\nᴄʟᴀꜱꜱɪꜰɪᴄᴀᴛɪᴏɴ\n├ Classification → Lectures\n└ Classification → Digital Notes'
    assert parse_template(template)==['Classification → Lectures','Classification → Digital Notes']
    original, entries=manual_index(template,[])
    assert original==template and len(entries)==2

def test_oldest_match_not_newest():
    now=datetime.now(timezone.utc)
    msgs=[MessageRecord(20,'Classification Lecture 02',now+timedelta(days=1)),MessageRecord(10,'Classification Lecture 01',now)]
    assert match_oldest('Classification Lectures',msgs).message_id==10

def test_automatic_groups_related_lectures():
    msgs=[MessageRecord(1,'Classification Lecture 01'),MessageRecord(2,'Classification Lecture 02'),MessageRecord(3,'Classification Digital Notes'),MessageRecord(4,'Anatomy Lecture 01')]
    groups, unresolved=automatic_index(msgs)
    assert len(groups)==2
    assert len(groups['Classification'])==2
    assert groups['Classification']['lectures']==1
    assert unresolved==[]

def test_empty_captions_ignored():
    groups,_=automatic_index([MessageRecord(1,''),MessageRecord(2,'random attachment')])
    assert groups=={}

def test_manual_render_keeps_unicode_layout_and_links_matched_label():
    from app.indexer import render_manual_template, IndexEntry
    template='📋 ɴᴀᴠɪɢᴀᴛɪᴏɴ ɪɴᴅᴇx\n\nᴄʟᴀꜱꜱɪꜰɪᴄᴀᴛɪᴏɴ\n├ Classification → Lectures'
    rendered, missing=render_manual_template(template,[IndexEntry('Classification → Lectures','classification lectures',77)],-100123456)
    assert 'ᴄʟᴀꜱꜱɪꜰɪᴄᴀᴛɪᴏɴ' in rendered
    assert '├ ' in rendered and 'https://t.me/c/123456/77' in rendered
    assert missing==[]

def test_automatic_distinguishes_classification_from_class_material_keyword():
    groups,_=automatic_index([MessageRecord(1,'Classification Lecture 01'),MessageRecord(2,'Classification Digital Notes')])
    assert list(groups['Classification'].keys())==['lectures','digital notes']


def test_generic_pdf_is_distinct_from_century_pdf():
    from app.indexer import detect_material
    assert detect_material("Anatomy PDF") == "pdfs"
    assert detect_material("Anatomy Century PDF") == "century pdf"
