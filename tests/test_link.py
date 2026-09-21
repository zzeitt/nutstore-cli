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


def test_comma_inside_angle_brackets_does_not_split():
    h = '<https://h/a,b>; rel="next", <https://h/c>; rel="last"'
    assert nsdav.parse_next_link(h) == "https://h/a,b"


def test_unquoted_rel_token_is_accepted():
    assert nsdav.parse_next_link('<https://h/b>; rel=next') == "https://h/b"


def test_stray_gt_does_not_disable_later_splitting():
    h = 'junk> garbage, <https://h/b>; rel="next"'
    assert nsdav.parse_next_link(h) == "https://h/b"


def test_rel_relation_type_is_case_insensitive():
    """RFC 8288 §2.1.1：注册关系类型逐字符不区分大小写比较。

    漏掉这种变体和漏掉裸 token 是同一类静默丢分页。
    """
    assert nsdav.parse_next_link('<https://h/b>; rel="Next"') == "https://h/b"
    assert nsdav.parse_next_link('<https://h/b>; rel=NEXT') == "https://h/b"


def test_rel_inside_another_quoted_value_is_not_a_match():
    """引号内的 rel=next 不是参数，不能误判。"""
    assert nsdav.parse_next_link('<https://h/x>; title="a; rel=next"') is None
