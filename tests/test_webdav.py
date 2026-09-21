import pytest

import nsdav
from mock_dav import MockDAV


@pytest.fixture
def dav():
    s = MockDAV(); base = s.start()
    yield s, base
    s.stop()


def _dav(s, base, **kw):
    from urllib.parse import urlsplit
    u = urlsplit(base)
    t = nsdav.Transport(u.hostname, port=u.port, use_tls=False,
                        user="u", password="p", min_gap=0, rand=lambda: 0.5, **kw)
    t._sleep = lambda _s: None
    return nsdav.WebDAV(t)


def test_listdir_excludes_self(dav):
    s, base = dav
    d = _dav(s, base)
    d.mkcol("/a")
    d.put("/a/one.txt", b"1")
    d.put("/a/two.txt", b"22")
    names = sorted(e.name for e in d.listdir("/a"))
    assert names == ["one.txt", "two.txt"]


def test_listdir_follows_pagination():
    s = MockDAV(page_size=3); base = s.start()
    try:
        d = _dav(s, base)
        d.mkcol("/big")
        for i in range(10):
            d.put(f"/big/f{i:02d}.txt", b"x")
        names = sorted(e.name for e in d.listdir("/big"))
        assert len(names) == 10            # 不跟随分页只会拿到 3 个
        assert names[0] == "f00.txt" and names[-1] == "f09.txt"
    finally:
        s.stop()


def test_pagination_does_not_double_encode_special_names():
    """目录名含空格时分页必须仍然正确 —— 这是双编码 bug 的回归测试。

    只断言"拿到 7 个名字"是个**代理**，所以这里把 wire 形式整个钉死：三次
    PROPFIND 的 target 必须逐字等于服务器给的形式——第一页由 `self.target()`
    编出来，后两页的 marker 原样透传 Link 里的 `%2Fmy%20dir%2Ff1.txt` 与
    `%2Fmy%20dir%2Ff4.txt`（page_size=3、7 个文件，正好三页）。

    **marker 被破坏时，先红的是异常，不是这条断言**（实测，别指望断言先红）：
    整条 Link 再过一次 `enc_path()` 会被 mock 的 `_rel()` 断言拦下并断连
    （客户端看到 NetworkError）；只把 marker 再编一次会让 mock 反复重发第一页，
    客户端一直翻到 10000 层保护（NsdavError，实测约 13 秒）；把 marker 解编码
    则 URL 根本发不出去（http.client 的 InvalidURL）。所以这条断言管的是另一
    层：**分页能走完、但 wire 形式被改动**的那些情形——例如小写百分号转义
    （`%2f`），它照样翻完 7 页、旧的两条子串断言全绿，只有这里能红（实测）。
    """
    s = MockDAV(page_size=3); base = s.start()
    try:
        d = _dav(s, base)
        d.mkcol("/my dir")
        for i in range(7):
            d.put(f"/my dir/f{i}.txt", b"x")
        names = sorted(e.name for e in d.listdir("/my dir"))
        assert len(names) == 7

        propfinds = [t for m, t in s.requests if m == "PROPFIND"]
        assert propfinds == [
            "/dav/my%20dir",
            "/dav/my%20dir?mk=%2Fmy%20dir%2Ff1.txt",
            "/dav/my%20dir?mk=%2Fmy%20dir%2Ff4.txt",
        ], f"分页 target 不是服务器给的形式: {propfinds}"
    finally:
        s.stop()


def test_stat_file_and_dir(dav):
    s, base = dav
    d = _dav(s, base)
    d.put("/x.txt", b"hello")
    e = d.stat("/x.txt")
    assert e.size == 5 and not e.is_dir and e.name == "x.txt"
    assert d.stat("/").is_dir


def test_exists(dav):
    s, base = dav
    d = _dav(s, base)
    assert not d.exists("/nope.txt")
    d.put("/y.txt", b"z")
    assert d.exists("/y.txt")


def test_read(dav):
    s, base = dav
    d = _dav(s, base)
    d.put("/r.txt", b"content here")
    assert d.read("/r.txt") == b"content here"


def test_mkdirs_creates_parents(dav):
    s, base = dav
    d = _dav(s, base)
    d.mkdirs("/p/q/r")
    assert d.exists("/p") and d.exists("/p/q") and d.exists("/p/q/r")


def test_put_creates_parents_on_conflict(dav):
    """PUT 撞 409 之后要**真的**把父目录建出来。

    这条原来只断言 `d.read(...) == b"v"`——而 mock 当初无条件回 201、无条件
    把内容存进 store，所以 `mkdirs` 那段一次都没跑过，这条照样绿。名字里写的
    是"on_conflict"，断言里却没有任何东西依赖那个冲突发生过。必须断言父目录
    **存在**，那才是被检验的行为。

    `assert not d.exists("/deep")` 是前置条件，不是凑数：它保证后面的 409
    确实是撞出来的，而不是这颗树碰巧已经在了。
    """
    s, base = dav
    d = _dav(s, base)
    assert not d.exists("/deep")
    d.put("/deep/nested/f.txt", b"v")
    assert d.read("/deep/nested/f.txt") == b"v"
    assert d.stat("/deep").is_dir
    assert d.stat("/deep/nested").is_dir


def test_delete_recursive(dav):
    s, base = dav
    d = _dav(s, base)
    d.mkcol("/t"); d.put("/t/a.txt", b"1"); d.put("/t/sub/b.txt", b"2")
    d.delete("/t", recursive=True)
    assert not d.exists("/t")


def test_move_and_copy(dav):
    s, base = dav
    d = _dav(s, base)
    d.put("/m.txt", b"data")
    d.move("/m.txt", "/n.txt")
    assert not d.exists("/m.txt") and d.read("/n.txt") == b"data"
    d.copy("/n.txt", "/o.txt")
    assert d.read("/n.txt") == b"data" and d.read("/o.txt") == b"data"


def test_stat_missing_raises(dav):
    s, base = dav
    d = _dav(s, base)
    with pytest.raises(nsdav.NotFoundError):
        d.stat("/absent.txt")


def test_walk_depth_limited(dav):
    s, base = dav
    d = _dav(s, base)
    d.mkcol("/w"); d.mkcol("/w/sub"); d.put("/w/f.txt", b"1")
    d.put("/w/sub/g.txt", b"2")
    shallow = [e.path for e in d.walk("/w", max_depth=1)]
    assert "/w/f.txt" in shallow and "/w/sub/" in shallow
    assert "/w/sub/g.txt" not in shallow
