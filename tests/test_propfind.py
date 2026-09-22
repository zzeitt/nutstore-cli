import pytest

import nsdav

TWO_ITEMS = b'''<?xml version="1.0" encoding="UTF-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:s="http://ns.jianguoyun.com">
 <d:response>
  <d:href>/dav/notes/</d:href>
  <d:propstat><d:prop>
   <d:displayname>notes</d:displayname>
   <d:resourcetype><d:collection/></d:resourcetype>
   <d:getcontentlength>0</d:getcontentlength>
   <d:getlastmodified>Mon, 21 Sep 2026 08:03:53 GMT</d:getlastmodified>
  </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
 <d:response>
  <d:href>/dav/notes/hello.txt</d:href>
  <d:propstat><d:prop>
   <d:displayname>hello.txt</d:displayname>
   <d:resourcetype/>
   <d:getcontentlength>16</d:getcontentlength>
   <d:getlastmodified>Mon, 21 Sep 2026 08:04:25 GMT</d:getlastmodified>
  </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
</d:multistatus>'''


def test_parses_dir_and_file():
    entries = nsdav.parse_multistatus(TWO_ITEMS, "/dav")
    assert len(entries) == 2
    d, f = entries
    assert d.path == "/notes/" and d.is_dir and d.name == "notes"
    assert d.size == 0
    assert f.path == "/notes/hello.txt" and not f.is_dir
    assert f.name == "hello.txt" and f.size == 16
    assert f.mtime is not None


def test_single_response_not_wrapped_in_list():
    one = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>
    <d:href>/dav/a.txt</d:href><d:propstat><d:prop><d:resourcetype/>
    <d:getcontentlength>3</d:getcontentlength></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''
    assert len(nsdav.parse_multistatus(one, "/dav")) == 1


def test_empty_multistatus():
    empty = b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>'
    assert nsdav.parse_multistatus(empty, "/dav") == []


def test_ignores_propstat_that_is_not_200():
    """404 的 propstat 一个属性都不许贡献。

    第二个 response 的块顺序是**刻意反过来**的，别把它"理顺"。两种顺序都要
    有，因为不同的错法埋伏在不同的顺序里；只留一种顺序就等于给另一种错法留
    后门。下面每条都在讲它挡的是哪种实现：

    - `/after`（200 在前、404 在后）：`getcontentlength` 在 404 块里是 777、
      200 块里是 99。"后写的盖前面"那种合并实现拿到 777。
    - `/before`（404 在前、200 在后）：只看第一个 propstat 的实现（`resp.find`
      而不是 `findall`）在这里只会看到 404 块——不看 status 的版本拿到 777，
      看 status 的版本直接跳过、什么都拿不到，于是 size 落成 0。两种都露馅。
    - 两台都带 `getlastmodified`，而它只该来自 200 块："先写的赢"那种合并在
      200 块里找不到它，会退到 404 块，于是 mtime 不为 None。

    这个用例改过两轮，历史值得留着：第一版 404 块里只有 `resourcetype`，对
    `size` 毫无影响，一份完全不看 status 的合并实现照样通过；第二版把 404 块
    一律挪到后面，补上了合并这一类，却又放走了 `resp.find` 那一类——同一份
    "不看 status"的缺陷换个形状就隐形了。收窄顺序覆盖不是免费的。
    """
    xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">
    <d:response>
    <d:href>/dav/after</d:href>
    <d:propstat><d:prop><d:getcontentlength>99</d:getcontentlength></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
    <d:propstat><d:prop><d:resourcetype/><d:getcontentlength>777</d:getcontentlength>
      <d:getlastmodified>Mon, 21 Sep 2026 08:03:53 GMT</d:getlastmodified></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
    </d:response>
    <d:response>
    <d:href>/dav/before</d:href>
    <d:propstat><d:prop><d:resourcetype/><d:getcontentlength>777</d:getcontentlength>
      <d:getlastmodified>Mon, 21 Sep 2026 08:03:53 GMT</d:getlastmodified></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
    <d:propstat><d:prop><d:getcontentlength>99</d:getcontentlength></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
    </d:response>
    </d:multistatus>'''
    after, before = nsdav.parse_multistatus(xml, "/dav")
    assert after.path == "/after" and before.path == "/before"
    for e in (after, before):
        assert e.size == 99
        assert e.mtime is None


def test_dir_size_is_zero_even_if_server_reports_one():
    """目录的 getcontentlength 无意义，实测服务端给 0，但给了数也不能信。"""
    xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>
    <d:href>/dav/big</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>
    <d:getcontentlength>4096</d:getcontentlength></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''
    e = nsdav.parse_multistatus(xml, "/dav")[0]
    assert e.is_dir
    assert e.size == 0


def test_base_path_itself_resolves_to_root():
    """href 恰好等于 base（collection 自身那条）时的基准取值。

    `name` 取到空串是 `"/".rstrip("/")` 的自然结果，规范没规定；这里钉住
    是为了有基线——将来若要过滤掉自身记录，改动会在这里现形。
    """
    xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>
    <d:href>/dav</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''
    e = nsdav.parse_multistatus(xml, "/dav")[0]
    assert e.path == "/"
    assert e.is_dir and e.name == ""


def test_malformed_xml_raises_nsdav_error():
    """响应体不是 XML 时要落进 NsdavError 体系，不能漏出裸 ET.ParseError。

    ET.ParseError 是 SyntaxError 的子类、不在本项目的异常树里，漏出去就会
    绕开退出码映射，用户看到的是 traceback 而不是"退出码 1 + 一句话"。
    """
    with pytest.raises(nsdav.NsdavError):
        nsdav.parse_multistatus(b"<d:multistatus", "/dav")


def test_non_multistatus_root_raises_nsdav_error():
    """根元素不是 multistatus 时必须响亮报错，而不是静默返回空列表。

    最危险的形态是服务器整份响应都不带命名空间：按 {DAV:} 限定名匹配会一个
    条目都找不到，返回 []，用户看到的是一个空目录——正是"绝不静默丢文件"
    这条红线最怕的样子。
    """
    no_ns = b'<?xml version="1.0"?><multistatus><response/></multistatus>'
    with pytest.raises(nsdav.NsdavError):
        nsdav.parse_multistatus(no_ns, "/dav")


def test_href_is_percent_decoded():
    xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>
    <d:href>/dav/%E4%B8%AD%E6%96%87/a%20b.txt</d:href>
    <d:propstat><d:prop><d:resourcetype/></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''
    e = nsdav.parse_multistatus(xml, "/dav")[0]
    assert e.path == "/中文/a b.txt"
    assert e.name == "a b.txt"


def test_base_prefix_does_not_match_a_sibling_directory():
    """base '/dav' 不能匹配 '/davos/x.txt'。

    朴素前缀匹配会把 '/davos/x.txt' 切成 'os/x.txt' 再补成 '/os/x.txt'——
    列表里的路径是错的，用户照着它 rm 就会打错目标。必须按整段边界比。
    """
    xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>
    <d:href>/davos/x.txt</d:href>
    <d:propstat><d:prop><d:resourcetype/></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''
    e = nsdav.parse_multistatus(xml, "/dav")[0]
    assert e.path == "/davos/x.txt"


def test_missing_size_and_mtime_are_tolerated():
    """缺 getcontentlength 时 size 是 None（"不知道"），不是 0（"空"）。

    这条以前断言的是 `e.size == 0`。那个契约本身就是 bug：download() 用
    `total == 0` 表示"远端是空文件"，于是"问不到大小"被当成空文件处理 ——
    把本地文件截成 0 字节、`os.replace` 覆盖掉，然后返回成功。解析层必须
    把两者分开，否则上层没有任何办法区分。mtime 那头没这个问题：None 本来
    就是"没有"。
    """
    xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>
    <d:href>/dav/x</d:href><d:propstat><d:prop><d:resourcetype/></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''
    e = nsdav.parse_multistatus(xml, "/dav")[0]
    assert e.size is None and e.mtime is None and not e.is_dir


def test_unparsable_size_is_unknown_not_zero():
    """非纯数字的 getcontentlength 同样是"不知道"，不能退化成一个确定的数。

    `1,024` / `12 KB` / 空元素都是 RFC 允许服务端发出来的形状。旧实现在这些
    值上落到 `size = 0`，与"缺元素"一起构成同一个静默失败面。
    """
    for value in (b"", b"1,024", b"12 KB", b"  "):
        xml = (b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>'
               b'<d:href>/dav/x</d:href><d:propstat><d:prop><d:resourcetype/>'
               b'<d:getcontentlength>' + value + b'</d:getcontentlength>'
               b'</d:prop><d:status>HTTP/1.1 200 OK</d:status>'
               b'</d:propstat></d:response></d:multistatus>')
        e = nsdav.parse_multistatus(xml, "/dav")[0]
        assert e.size is None, (value, e.size)


def test_explicit_zero_size_is_still_zero():
    """而服务端**明说** 0 时仍然是 0 —— "空文件"这条路不能被上面的改动吃掉。

    没有这条，把 `size = None` 写死在解析里（永远不取值）同样是绿的，而
    真正的空文件下载会全部失败。
    """
    xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>
    <d:href>/dav/x</d:href><d:propstat><d:prop><d:resourcetype/>
    <d:getcontentlength>0</d:getcontentlength></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''
    e = nsdav.parse_multistatus(xml, "/dav")[0]
    assert e.size == 0


def test_dir_path_keeps_trailing_slash():
    d = nsdav.parse_multistatus(TWO_ITEMS, "/dav")[0]
    assert d.path.endswith("/")
