import nsdav

D = "{DAV:}"

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
    # 目录上常见：一部分属性 200，一部分 404，不能把 404 的当数据
    xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>
    <d:href>/dav/x</d:href>
    <d:propstat><d:prop><d:resourcetype/></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
    <d:propstat><d:prop><d:getcontentlength>99</d:getcontentlength></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
    </d:response></d:multistatus>'''
    e = nsdav.parse_multistatus(xml, "/dav")[0]
    assert e.size == 99


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
