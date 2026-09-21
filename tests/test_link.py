import nsdav


def test_parses_plain_next():
    h = '<https://dav.jianguoyun.com/dav/notes/x?mk=%2Fx%2Fa.txt>; rel="next"'
    assert nsdav.parse_next_link(h) == \
        "https://dav.jianguoyun.com/dav/notes/x?mk=%2Fx%2Fa.txt"


def test_ignores_rel_prev_only():
    assert nsdav.parse_next_link('<https://h/p>; rel="prev"') is None


def test_picks_next_out_of_multiple_links():
    h = ('<https://h/a>; rel="prev", '
         '<https://h/b>; rel="next", '
         '<https://h/c>; rel="last"')
    assert nsdav.parse_next_link(h) == "https://h/b"


def test_rel_with_multiple_tokens():
    assert nsdav.parse_next_link('<https://h/b>; rel="next last"') == "https://h/b"


def test_no_header_or_garbage():
    assert nsdav.parse_next_link(None) is None
    assert nsdav.parse_next_link("") is None
    assert nsdav.parse_next_link("not a link header") is None


def test_url_to_target_keeps_query_encoded():
    u = "https://dav.jianguoyun.com/dav/notes/x?mk=%2Fx%2Fa.txt"
    assert nsdav.url_to_target(u) == "/dav/notes/x?mk=%2Fx%2Fa.txt"


def test_url_to_target_without_query():
    assert nsdav.url_to_target("https://h/dav/a/b") == "/dav/a/b"
