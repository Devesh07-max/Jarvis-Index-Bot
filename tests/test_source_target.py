from types import SimpleNamespace
from app.source_target import parse_tme_message_url, clean_caption, match_ordered_messages, source_links, render_linked_index


def test_parse_private_and_public_message_links():
    assert parse_tme_message_url('https://t.me/c/123456/789') == (-100123456, 789)
    assert parse_tme_message_url('https://t.me/course_name/789') == ('course_name', 789)


def test_caption_normalization_ignores_website_and_attribution():
    assert clean_caption('Classification Lecture 01\nhttps://example.com') == clean_caption('CLASSIFICATION Lecture 01\nBy: Devesh')


def test_order_mapping_uses_same_sequence_and_reports_differences():
    src=[SimpleNamespace(id=10,message='Lecture 01 https://source.site'),SimpleNamespace(id=11,message='Lecture 02')]
    dst=[SimpleNamespace(id=90,message='Lecture 01 By Devesh'),SimpleNamespace(id=91,message='Lecture 02')]
    mapping,warnings=match_ordered_messages(src,dst)
    assert mapping == {10:90,11:91}
    assert warnings == []


def test_index_hyperlink_destination_is_rewritten_to_target():
    text='Lecture 01'
    Entity=type('MessageEntityTextUrl',(),{})
    entity=Entity(); entity.offset=0; entity.length=len(text); entity.url='https://t.me/c/123/10'
    msg=SimpleNamespace(message=text,entities=[entity])
    assert source_links(msg)[0][2]=='https://t.me/c/123/10'
    rendered,linked,missing=render_linked_index(msg,{10:90},-100456)
    assert 'https://t.me/c/456/90' in rendered
    assert linked==1 and missing==0


def test_matching_handles_different_target_order():
    src=[
        SimpleNamespace(id=10,message='Classification Lecture 01 https://source.site'),
        SimpleNamespace(id=11,message='Anatomy Digital Notes'),
        SimpleNamespace(id=12,message='Physiology Lecture 02'),
    ]
    dst=[
        SimpleNamespace(id=90,message='Physiology Lecture 02 By Devesh'),
        SimpleNamespace(id=91,message='Classification Lecture 01 By Devesh'),
        SimpleNamespace(id=92,message='Anatomy Digital Notes By Devesh'),
    ]
    mapping,warnings=match_ordered_messages(src,dst)
    assert mapping == {10:91,11:92,12:90}
    assert not [w for w in warnings if 'No reliable target' in w]


def test_matching_is_one_to_one_for_duplicate_source_captions():
    src=[SimpleNamespace(id=10,message='Same Lecture 01'),SimpleNamespace(id=11,message='Same Lecture 01')]
    dst=[SimpleNamespace(id=90,message='Same Lecture 01')]
    mapping,warnings=match_ordered_messages(src,dst)
    assert len(mapping) == 1
    assert any('No reliable target' in w for w in warnings)


def test_unrelated_target_is_not_paired_by_position():
    src=[SimpleNamespace(id=10,message='Classification Lecture 01')]
    dst=[SimpleNamespace(id=90,message='Unrelated Chemistry Notes')]
    mapping,warnings=match_ordered_messages(src,dst)
    assert mapping == {}
    assert any('No reliable target' in w for w in warnings)
