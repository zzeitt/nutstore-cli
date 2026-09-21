import contextlib
import os
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
    return nsdav.WebDAV(t), t


def test_download_small_file(dav, tmp_path):
    s, base = dav
    d, t = _dav(s, base)
    s.add_file("/a.bin", b"hello world")
    dest = tmp_path / "a.bin"
    path, n = nsdav.download(d, "/a.bin", str(dest), transport=t)
    assert path == str(dest)
    assert dest.read_bytes() == b"hello world"
    assert n == 11
    assert not os.path.exists(str(dest) + ".part")


def test_download_chunked(dav, tmp_path):
    """分块下载：51200 字节 / chunk=1024 必须是 50 次范围请求，每次正好 1024。

    只断言内容对的话，一次 GET 拉完的实现照样绿——而"分块只为大文件省内存"
    正是这个函数存在的理由（iSH 上内存比时间金贵）。所以这里直接钉住请求的
    **形状**，不只是结果：50 次、每段 1024、首块从 0 开始。段长一律不超过
    chunk 就等于说"任何时刻只持有一个 chunk 在内核之外"。
    """
    s, base = dav
    d, t = _dav(s, base)
    payload = bytes(range(256)) * 200        # 51200 字节
    s.add_file("/big.bin", payload)
    dest = tmp_path / "big.bin"

    ranges = []
    real_stream = t.stream
    def spy(method, target, **kw):
        hdr = kw.get("headers") or {}
        if "Range" in hdr:
            ranges.append(hdr["Range"])
        return real_stream(method, target, **kw)
    t.stream = spy

    nsdav.download(d, "/big.bin", str(dest), chunk=1024, transport=t)
    assert dest.read_bytes() == payload

    spans = []
    for r in ranges:
        lo, _, hi = r[len("bytes="):].partition("-")
        spans.append(int(hi) - int(lo) + 1)
    assert len(ranges) == 50, ranges
    assert ranges[0] == "bytes=0-1023", ranges[:3]
    assert all(sp == 1024 for sp in spans), spans


def test_download_resumes_from_part(dav, tmp_path):
    s, base = dav
    d, t = _dav(s, base)
    payload = bytes(range(256)) * 200        # 51200 字节
    s.add_file("/big.bin", payload)
    dest = tmp_path / "big.bin"
    part = str(dest) + ".part"
    with open(part, "wb") as f:              # 假装上次下了一半
        f.write(payload[:20000])

    ranges = []
    real_stream = t.stream
    def spy(method, target, **kw):
        hdr = kw.get("headers") or {}
        if "Range" in hdr:
            ranges.append(hdr["Range"])
        return real_stream(method, target, **kw)
    t.stream = spy

    nsdav.download(d, "/big.bin", str(dest), chunk=8192, transport=t)
    assert dest.read_bytes() == payload
    # 必须从断点开始，不能重下已经拿到的 20000 字节
    assert ranges[0] == "bytes=20000-28191"


def test_download_restarts_when_server_ignores_range(dav, tmp_path):
    """服务端不支持 Range（对范围请求回 200 全量）时必须丢掉断点重下。

    这条是上面关键点里明写的分支，此前一条用例都覆盖不到：mock 永远支持
    Range，这段代码从没被走到过。所以先把 mock 的 Range 支持关掉，再喂一个
    **长度对得上、内容全错**的残留 .part —— 只断"最终内容对"是不够的，若残留
    长度凑巧吻合，一个把 200 的全量直接追加到已有 .part 后面的实现也能蒙混
    过去。所以断言两层：内容，以及"只发过一次范围请求"这个形状（先照常试探
    续传，被 200 顶回来才发现不支持，不能连试都不试）。
    """
    s, base = dav
    d, t = _dav(s, base)
    payload = bytes(range(256)) * 200        # 51200 字节
    s.add_file("/big.bin", payload)
    s.ignore_range = True
    dest = tmp_path / "big.bin"
    with open(str(dest) + ".part", "wb") as f:
        f.write(b"X" * 20000)                # 长度对得上，内容全错

    ranges = []
    real_stream = t.stream
    def spy(method, target, **kw):
        hdr = kw.get("headers") or {}
        if "Range" in hdr:
            ranges.append(hdr["Range"])
        return real_stream(method, target, **kw)
    t.stream = spy

    # chunk 显式给，且大于文件：逼出"一次拉完"的路径，不依赖 DOWNLOAD_CHUNK 默认值
    nsdav.download(d, "/big.bin", str(dest), chunk=1 << 20, transport=t)
    assert dest.read_bytes() == payload
    assert ranges == ["bytes=20000-51199"], ranges


def test_stale_part_larger_than_remote_is_discarded(dav, tmp_path):
    s, base = dav
    d, t = _dav(s, base)
    s.add_file("/small.bin", b"tiny")
    dest = tmp_path / "small.bin"
    with open(str(dest) + ".part", "wb") as f:
        f.write(b"x" * 9999)
    nsdav.download(d, "/small.bin", str(dest), transport=t)
    assert dest.read_bytes() == b"tiny"


def test_empty_file_download(dav, tmp_path):
    s, base = dav
    d, t = _dav(s, base)
    s.add_file("/empty.bin", b"")
    dest = tmp_path / "empty.bin"
    nsdav.download(d, "/empty.bin", str(dest), transport=t)
    assert dest.exists() and dest.read_bytes() == b""


def test_no_part_file_left_on_success(dav, tmp_path):
    s, base = dav
    d, t = _dav(s, base)
    s.add_file("/x.bin", b"abc")
    dest = tmp_path / "x.bin"
    nsdav.download(d, "/x.bin", str(dest), transport=t)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.bin"]


class _ReadSpy:
    """记下每次 read 索要的字节数。-1 就是"不带参数的整份读"。"""

    def __init__(self, raw):
        self.raw = raw
        self.sizes = []

    def read(self, n=-1):
        self.sizes.append(n)
        return self.raw.read(n)

    def __getattr__(self, name):
        return getattr(self.raw, name)


def _spy_stream(t):
    """把 t.stream 包一层，换掉响应体好记录 read 索要的大小。

    t.stream 是 contextmanager，spy 也得是；真身仍走 with，退出时由它把
    连接读干净，所以记到的都只是实现自己发起的 read。
    """
    spies = []
    real = t.stream

    @contextlib.contextmanager
    def spy(method, target, **kw):
        with real(method, target, **kw) as resp:
            body = _ReadSpy(resp.body)
            spies.append(body)
            yield resp._replace(body=body)

    t.stream = spy
    return spies


def test_ignored_range_fallback_still_reads_in_chunks(dav, tmp_path):
    """服务端忽略 Range 时，回退分支也必须按块读。

    这一段只在服务端不支持 Range 时才会走到，mock 默认支持，所以此前没人
    看得见 `f.write(resp.body.read())`：整个文件一次进内存，把分块省内存的
    初衷作废（iSH 上内存比时间金贵）。断言的是**形状**——任何一次 read 都
    必须带正数长度，也就是"整个响应体从不被一次性物化"。只断言内容的话，
    两种写法都绿。
    """
    s, base = dav
    d, t = _dav(s, base)
    payload = bytes(range(256)) * 32768          # 8 MiB
    s.add_file("/huge.bin", payload)
    s.ignore_range = True
    dest = tmp_path / "huge.bin"
    spies = _spy_stream(t)
    seen = []

    nsdav.download(d, "/huge.bin", str(dest), transport=t,
                   progress=lambda done, total: seen.append((done, total)))

    assert dest.read_bytes() == payload
    sizes = [n for sp in spies for n in sp.sizes]
    assert sizes, "一次 read 都没有？"
    assert all(isinstance(n, int) and 0 < n <= 65536 for n in sizes), sizes
    # 回退路径只有这条用例走得到，进度也就只能在这里钉：一路报到 total。
    assert seen[-1] == (len(payload), len(payload)), seen[-1]


class _ConnSpy:
    """把 `_get_conn()` 交出去的那条连接包一层，让 getresponse 交回可数的响应。"""

    def __init__(self, conn, sink):
        self._conn = conn
        self._sink = sink

    def request(self, *a, **kw):
        self._conn.request(*a, **kw)

    def getresponse(self):
        r = _ReadSpy(self._conn.getresponse())
        self._sink.append(r)
        return r

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_stream_drain_on_failure_is_bounded(dav):
    """离开 `with` 时要把剩余响应体读干净（连接才能复用），但必须按块读。

    成功路径看不出问题：两条下载循环都读到了 EOF，drain 读到空串。**中途失败**
    才看得见 —— 磁盘写满（ENOSPC，`.part`/续传存在的理由）、进度回调抛异常、
    Ctrl-C 都从这条 finally 穿出去，而整份 `read()` 会把剩余字节一次装进内存：
    一个 1 GiB 的下载在写满磁盘的瞬间，还要额外要一份剩余大小的内存。断言形状
    ——任何一次 read 都带正数且不超过 64 KiB。
    """
    s, base = dav
    d, t = _dav(s, base)
    payload = bytes(range(256)) * 32768          # 8 MiB
    s.add_file("/huge.bin", payload)

    sink = []
    real_get = t._get_conn
    t._get_conn = lambda: _ConnSpy(real_get(), sink)

    with pytest.raises(RuntimeError):
        with t.stream("GET", d.target("/huge.bin"),
                      headers={"Range": "bytes=0-8388607"}) as resp:
            assert resp.status == 206
            resp.body.read(1024)                 # 只读一点就炸
            raise RuntimeError("模拟中途失败")

    sizes = [n for r in sink for n in r.sizes]
    assert sizes, "一次 read 都没有？"
    assert all(isinstance(n, int) and 0 < n <= 65536 for n in sizes), sizes


def test_progress_reports_from_the_resume_point(dav, tmp_path):
    """progress 回调从断点接着报，一路报到 total。"""
    s, base = dav
    d, t = _dav(s, base)
    payload = bytes(range(256)) * 200            # 51200
    s.add_file("/big.bin", payload)
    dest = tmp_path / "big.bin"
    with open(str(dest) + ".part", "wb") as f:   # 上次下到一半
        f.write(payload[:20000])

    seen = []
    nsdav.download(d, "/big.bin", str(dest), chunk=8192, transport=t,
                   progress=lambda done, total: seen.append((done, total)))

    assert dest.read_bytes() == payload
    # 逐块上报：chunk=8192、断点 20000，四块各报一次，末值到 total
    assert seen == [(28192, 51200), (36384, 51200), (44576, 51200),
                    (51200, 51200)], seen


def test_progress_reports_when_part_is_already_complete(dav, tmp_path):
    """`.part` 已经是一整份时也要报一次收尾，不能一声不吭地改名返回。"""
    s, base = dav
    d, t = _dav(s, base)
    payload = bytes(range(256)) * 200            # 51200
    s.add_file("/big.bin", payload)
    dest = tmp_path / "big.bin"
    with open(str(dest) + ".part", "wb") as f:
        f.write(payload)

    seen = []
    nsdav.download(d, "/big.bin", str(dest), transport=t,
                   progress=lambda done, total: seen.append((done, total)))

    assert dest.read_bytes() == payload
    assert seen == [(51200, 51200)], seen
