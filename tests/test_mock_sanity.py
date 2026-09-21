import base64
import http.client
import urllib.parse

from mock_dav import MockDAV


def _req(base, method, target, body=None, headers=None):
    u = urllib.parse.urlsplit(base)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    h = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
    h.update(headers or {})
    c.request(method, target, body=body, headers=h)
    r = c.getresponse()
    out = (r.status, dict(r.getheaders()), r.read())
    c.close()
    return out


def test_mock_requires_auth():
    s = MockDAV(); base = s.start()
    try:
        u = urllib.parse.urlsplit(base)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        c.request("PROPFIND", "/dav/", headers={"Depth": "0"})
        assert c.getresponse().status == 401
        c.close()
    finally:
        s.stop()


def test_mock_crud_roundtrip():
    s = MockDAV(); base = s.start()
    try:
        assert _req(base, "MKCOL", "/dav/a")[0] == 201
        assert _req(base, "PUT", "/dav/a/f.txt", b"hello")[0] == 201
        st, hd, bd = _req(base, "GET", "/dav/a/f.txt")
        assert st == 200 and bd == b"hello"
        st, hd, bd = _req(base, "GET", "/dav/a/f.txt", headers={"Range": "bytes=1-3"})
        assert st == 206 and bd == b"ell"
        assert hd["Content-Range"] == "bytes 1-3/5"
        st, _, _ = _req(base, "DELETE", "/dav/a", headers={"Depth": "infinity"})
        assert st == 204
        assert _req(base, "GET", "/dav/a/f.txt")[0] == 404
    finally:
        s.stop()


def test_mock_head_is_deliberately_useless():
    s = MockDAV(); base = s.start()
    try:
        _req(base, "PUT", "/dav/f.txt", b"12345")
        st, hd, _ = _req(base, "HEAD", "/dav/f.txt")
        assert st == 200 and hd["Content-Length"] == "0"   # 模仿坚果云
    finally:
        s.stop()


def test_mock_paginates_with_link_header():
    s = MockDAV(page_size=3); base = s.start()
    try:
        _req(base, "MKCOL", "/dav/d")
        for i in range(7):
            _req(base, "PUT", f"/dav/d/f{i}.txt", b"x")
        st, hd, bd = _req(base, "PROPFIND", "/dav/d", headers={"Depth": "1"})
        assert st == 207
        assert 'rel="next"' in hd["Link"]
        assert bd.count(b"<d:response>") == 3
    finally:
        s.stop()


def test_mock_fails_first_n_then_succeeds():
    s = MockDAV(fail_first_n=2, fail_status=503); base = s.start()
    try:
        assert _req(base, "PROPFIND", "/dav/", headers={"Depth": "0"})[0] == 503
        assert _req(base, "PROPFIND", "/dav/", headers={"Depth": "0"})[0] == 503
        assert _req(base, "PROPFIND", "/dav/", headers={"Depth": "0"})[0] == 207
    finally:
        s.stop()


def test_mock_root_propfind_and_pagination():
    """根目录也要能 PROPFIND，而且要能翻页。

    `dirs` 里根的键是 `"/"`（`_rel()` 也把根规范化成 `"/"`），但 `_do_propfind`
    当初查的是 `rel.rstrip("/")`，根落成 `""`，查不到，于是整棵树从根上就 404。
    `_rel()`、`_do_mkcol`、`_do_move`、`_do_copy` 四处都写了 `or "/"`，只有这里
    和 `_do_delete` 漏了——是笔误，不是设计。

    这条用例的另一半价值在**翻页**：原来唯一的分页用例打的是 `/dav/d` 子目录，
    根的 Link 头从没被跟随过。这里跟着 mock 自己发出来的 Link 走，等于同时钉住
    "Link 的目标真的可用"。

    别把每页的条数写成 4：page_size=2 时第一页只有 2 条，`== 4` 这种断言是
    把"总条数"当成了"单页条数"（这版第一稿就是这么写的，跑出来才发现）。
    四条的完整性由最后那行 href 汇总来钉。
    """
    s = MockDAV(page_size=2); base = s.start()
    try:
        _req(base, "MKCOL", "/dav/a")
        _req(base, "MKCOL", "/dav/b")
        _req(base, "PUT", "/dav/root.txt", b"r")
        hrefs = []
        st, hd, bd = _req(base, "PROPFIND", "/dav/", headers={"Depth": "1"})
        assert st == 207, ("root PROPFIND status", st)
        assert bd.count(b"<d:response>") == 2, bd[:300]
        hrefs += [h.split(b"<")[0] for h in bd.split(b"<d:href>")[1:]]
        assert 'rel="next"' in hd["Link"], hd

        sp = urllib.parse.urlsplit(hd["Link"].split(">")[0].lstrip("<"))
        st, hd, bd = _req(base, "PROPFIND", sp.path + "?" + sp.query,
                          headers={"Depth": "1"})
        assert st == 207, ("page2 status", st)
        assert bd.count(b"<d:response>") == 2, bd[:300]
        hrefs += [h.split(b"<")[0] for h in bd.split(b"<d:href>")[1:]]
        assert "Link" not in hd, ("还有下一页", hd.get("Link"))

        assert sorted(hrefs) == [b"/dav/", b"/dav/a/", b"/dav/b/", b"/dav/root.txt"]
    finally:
        s.stop()


def test_mock_root_delete_clears_the_tree():
    """根 DELETE 必须连子目录一起清掉，而且根自己留下。

    `target + "/"` 在根上拼出 `"//"`，一个子项都匹配不到：根删了，里面的东西
    全留着。mock 是后续所有测试的回归网，网自己删一半留一半，比网小更糟。
    """
    s = MockDAV(); base = s.start()
    try:
        _req(base, "MKCOL", "/dav/a")
        _req(base, "PUT", "/dav/a/f.txt", b"x")
        st, _, _ = _req(base, "DELETE", "/dav/", headers={"Depth": "infinity"})
        assert st == 204
        assert _req(base, "GET", "/dav/a/f.txt")[0] == 404
        assert _req(base, "PROPFIND", "/dav/a", headers={"Depth": "0"})[0] == 404
        # 根是常驻的：内容清空后根自己还在，只有自身那一条。
        st, _, bd = _req(base, "PROPFIND", "/dav/", headers={"Depth": "1"})
        assert st == 207 and bd.count(b"<d:response>") == 1
    finally:
        s.stop()


def test_mock_put_into_missing_collection_conflicts():
    """PUT 的父集合不存在时必须 409，和真实服务器一致。

    这是 `WebDAV.put` 自动补建父目录那段的触发条件。mock 原来无条件回 201，
    那段就一次都没跑过；更糟的是它造出了真实服务器不可能有的状态——文件 GET
    得到，却不出现在任何一次列表里。
    """
    s = MockDAV(); base = s.start()
    try:
        assert _req(base, "PUT", "/dav/nope/f.txt", b"x")[0] == 409
        assert s.store == {}                     # 409 不能顺手把内容存下
        assert _req(base, "MKCOL", "/dav/nope")[0] == 201
        assert _req(base, "PUT", "/dav/nope/f.txt", b"x")[0] == 201
        assert s.store["/nope/f.txt"] == b"x"
    finally:
        s.stop()


def test_mock_link_url_is_encoded():
    """Link 里的路径必须和 href 一样是编码过的。

    分页 URL 由客户端原样透传（`url_to_target` 既不解码也不再编码），所以
    mock 发出带裸空格或非 ASCII 的 Link，客户端跟随时要么 `InvalidURL`、要么
    `UnicodeEncodeError`（中文名会在 `send_header` 里炸，客户端只见
    RemoteDisconnected）。而真实服务器给的本来就是编码过的分页 URL——mock 若
    只发这一种形式，客户端就只能自己再编一遍，正好落进本项目禁止的双编码。

    走**全部**页，不是在第二页停手：page_size=2 而这里有 5 条，所以是 2+2+1
    三页。第二页自己还会再发一个 Link，早停会把第三页整个漏掉——而"分页悄悄
    丢条目"正是这条要挡的东西。
    """
    s = MockDAV(page_size=2); base = s.start()
    try:
        _req(base, "MKCOL", "/dav/my%20dir")
        for i in range(4):
            # 用 f-string，别用 % 格式化：`%20d` 会被当成宽度 20 的 %d。
            _req(base, "PUT", f"/dav/my%20dir/f{i}.txt", b"x")
        hrefs = []
        target = "/dav/my%20dir"
        pages = 0
        while True:
            pages += 1
            st, hd, bd = _req(base, "PROPFIND", target, headers={"Depth": "1"})
            assert st == 207
            hrefs += [h.split(b"<")[0] for h in bd.split(b"<d:href>")[1:]]
            if "Link" not in hd:
                break
            link = hd["Link"].split(">")[0].lstrip("<")
            assert link.isascii() and " " not in link, link
            sp = urllib.parse.urlsplit(link)
            assert sp.path == "/dav/my%20dir", sp.path
            target = sp.path + "?" + sp.query
        assert pages == 3, pages
        assert sorted(hrefs) == [
            b"/dav/my%20dir/", b"/dav/my%20dir/f0.txt", b"/dav/my%20dir/f1.txt",
            b"/dav/my%20dir/f2.txt", b"/dav/my%20dir/f3.txt"]
    finally:
        s.stop()


def test_mock_fail_first_n_can_be_set_after_start():
    """start() 之后再设 fail_first_n 也必须生效。

    `fail_status`、`page_size`、`latency` 都是每次请求现读的，只有
    `fail_first_n` 在构造时被抄进 `_failures_left`。Task 7 的
    `test_stream_body_retry_reopens_file` 正是在 start() 之后写
    `s.fail_first_n = 1`——快照版本下不注入故障，那条用例首次请求就成功，
    永远碰不到它名字里的重试，`StreamBody` 重试时重开文件也就没人看。
    """
    s = MockDAV(); base = s.start()
    try:
        s.fail_first_n = 2
        s.fail_status = 503
        assert _req(base, "PROPFIND", "/dav/", headers={"Depth": "0"})[0] == 503
        assert _req(base, "PROPFIND", "/dav/", headers={"Depth": "0"})[0] == 503
        assert _req(base, "PROPFIND", "/dav/", headers={"Depth": "0"})[0] == 207
    finally:
        s.stop()
