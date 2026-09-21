import pytest
import nsdav


@pytest.mark.parametrize("raw,expected", [
    ("/a/b.txt", "/a/b.txt"),
    ("/a b/c.txt", "/a%20b/c.txt"),
    ("/a+b/c.txt", "/a%2Bb/c.txt"),          # '+' 必须变 %2B，不能留成 '+'
    ("/中文/文件.txt", "/%E4%B8%AD%E6%96%87/%E6%96%87%E4%BB%B6.txt"),
    ("/emoji/🎉.md", "/emoji/%F0%9F%8E%89.md"),
    ("/pct/100%.txt", "/pct/100%25.txt"),    # 字面 % 必须转义
    ("/hash/a#b.txt", "/hash/a%23b.txt"),
    ("/q/a?b.txt", "/q/a%3Fb.txt"),          # 文件名里的字面 '?' 也要能访问
])
def test_enc_path_encodes_segments(raw, expected):
    assert nsdav.enc_path(raw) == expected


def test_enc_path_has_no_query_concept():
    """enc_path 眼里没有 query 这回事，'?' 就是普通路径字符。

    分页 marker 从不经过这里 —— 它走 url_to_target（Task 2），那条路
    原样透传已编码的 URL。所以双编码 bug 在调用图上就被排除了，
    不需要 enc_path 去"小心处理 query"。

    期望值里 '=' 变 %3D、'%2F' 变 %252F，不是笔误：enc_path 收到的是
    "用户原始路径"，每个字符都是字面的，'%' 自然也当字面百分号编码
    （和上面 100%.txt 那例一致）。分页 marker 里那个已经是 %2F 的值
    永远不会走到这里，所以这里编出来的 %252F 无害 —— 双编码之所以
    不可能发生，靠的是调用图，不是这里的小心处理。
    """
    assert nsdav.enc_path("/dav/x?mk=%2Fy") == "/dav/x%3Fmk%3D%252Fy"


@pytest.mark.parametrize("raw,expected", [
    ("", "/"),
    ("/", "/"),
    (".", "/"),
    ("a.txt", "/a.txt"),
    ("notes/a.txt", "/notes/a.txt"),
    ("//a///b//", "/a/b"),
    ("/a/./b", "/a/b"),
    ("/a/b/../c", "/a/c"),
    ("/../../etc/passwd", "/etc/passwd"),   # 逃不出根
])
def test_normalize_remote_path(raw, expected):
    assert nsdav.normalize_remote_path(raw) == expected
