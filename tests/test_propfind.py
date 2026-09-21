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

    这里两个断言是配对设计的，缺一个就抓不住 bug：

    - `getcontentlength` 在 404 块里是 777、在 200 块里是 99，且 404 块在后。
      "后写的盖前面"那种合并实现会得到 777。
    - `getlastmodified` 只出现在 404 块里。"先写的赢"那种合并实现在 200 块
      里找不到它，仍会退到 404 块，于是 mtime 不为 None。

    只放一个冲突值只能抓住其中一种合并顺序——第一版就只放了一个
    `getcontentlength`，而 404 块里没有它，于是一份"完全不看 status"的实现
    照样通过，测试对它所命名的行为完全无感。
    """
    xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>
    <d:href>/dav/x</d:href>
    <d:propstat><d:prop><d:getcontentlength>99</d:getcontentlength></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
    <d:propstat><d:prop><d:resourcetype/><d:getcontentlength>777</d:getcontentlength>
      <d:getlastmodified>Mon, 21 Sep 2026 08:03:53 GMT</d:getlastmodified></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
    </d:response></d:multistatus>'''
    e = nsdav.parse_multistatus(xml, "/dav")[0]
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
    xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>
    <d:href>/dav/x</d:href><d:propstat><d:prop><d:resourcetype/></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''
    e = nsdav.parse_multistatus(xml, "/dav")[0]
    assert e.size == 0 and e.mtime is None and not e.is_dir


def test_dir_path_keeps_trailing_slash():
    d = nsdav.parse_multistatus(TWO_ITEMS, "/dav")[0]
    assert d.path.endswith("/")
