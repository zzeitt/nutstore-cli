import contextlib
import os
import pytest
import random

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
    """离开 `with` 时要把剩余响应体读干净（连接才能复用），但必须按块读 ——
    断言 drain 真的读到了 EOF（`sum(sizes)` 逼近整个 body，不是只有用例自己
    那 1024），且每一次 read 都带正数长度、不超过 64 KiB。

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
    # drain 必须真的把剩下的读完 —— 少了它，sizes 里只剩用例自己那 1024。
    assert sum(sizes) >= len(payload) - 65536, sizes
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


def test_upload_small(dav, tmp_path):
    s, base = dav
    d, t = _dav(s, base)
    src = tmp_path / "u.txt"
    src.write_bytes(b"payload")
    n = nsdav.upload(d, str(src), "/u.txt")
    assert n == 7
    assert s.store["/u.txt"] == b"payload"


def test_upload_creates_parents(dav, tmp_path):
    s, base = dav
    d, t = _dav(s, base)
    src = tmp_path / "u.txt"
    src.write_bytes(b"x")
    nsdav.upload(d, str(src), "/deep/dir/u.txt")
    assert s.store["/deep/dir/u.txt"] == b"x"
    # mock 现在对"父集合不存在"回 409，所以这条同时真的走过了 put 的补建重试。
    assert "/deep" in s.dirs and "/deep/dir" in s.dirs


def test_upload_verifies_size(dav, tmp_path, monkeypatch):
    s, base = dav
    d, t = _dav(s, base)
    src = tmp_path / "u.txt"
    src.write_bytes(b"12345")

    real_stat = d.stat
    def lying_stat(p):
        e = real_stat(p)
        e.size = 3                      # 假装服务端只存了 3 字节
        return e
    monkeypatch.setattr(d, "stat", lying_stat)

    with pytest.raises(nsdav.NsdavError, match="大小不符"):
        nsdav.upload(d, str(src), "/u.txt")


def test_upload_strong_verify_detects_corruption(dav, tmp_path):
    """strong 要抓到"长度不变、内容变了"，而 size 校验抓不到。

    篡改的是**末尾** 10 字节，因为 strong 读回并比对的是末尾 VERIFY_TAIL_BYTES
    个字节。这条用例原先篡改的是开头 10 字节，读回窗口覆盖不到 —— 实测
    （T10 预检，5 条里红 1 条）红在 `DID NOT RAISE NsdavError`，用例无法为它
    名字里的行为而通过。`verify="size"` 那一段钉住"两种校验真有差别"：长度
    没变时 size 校验必须放行，否则 strong 多花的那个请求就没有意义。
    """
    s, base = dav
    d, t = _dav(s, base)
    src = tmp_path / "u.txt"
    payload = b"abcdefghij" * 10        # 100 字节
    src.write_bytes(payload)

    # 让 PUT 写完就篡改**末尾**内容，长度不变 —— 只有 strong 校验能发现
    real_put = d.put
    def corrupting_put(p, data):
        real_put(p, data)
        s.store[p] = payload[:-10] + b"XXXXXXXXXX"
    d.put = corrupting_put

    # 长度没变，size 校验看不出来
    assert nsdav.upload(d, str(src), "/u.txt", verify="size") == 100
    with pytest.raises(nsdav.NsdavError, match="校验失败"):
        nsdav.upload(d, str(src), "/u.txt", verify="strong")


def test_upload_does_not_read_whole_file_into_memory(dav, tmp_path):
    """大文件必须走流式，验证传给 transport 的是 StreamBody 而不是 bytes。"""
    s, base = dav
    d, t = _dav(s, base)
    src = tmp_path / "big.bin"
    src.write_bytes(b"z" * (3 * 1024 * 1024))
    seen = []
    real_put = d.put
    def spy(p, data):
        seen.append(type(data).__name__)
        return real_put(p, data)
    d.put = spy
    nsdav.upload(d, str(src), "/big.bin")
    assert seen == ["StreamBody"]


def test_strong_verify_streams_when_server_ignores_range(dav, tmp_path):
    """服务端忽略 Range（回 200 全量）时，回读校验只留末尾的 tail 字节。

    这条路此前一个用例都走不到（mock 默认支持 Range），而它明确承认 200 是
    合法响应。整份 `read()` 会把远端文件全读进内存 —— 与 T9 回退分支同一个
    坑。断言两层：任何一次 read 都带正数长度（形状），以及"内容对得上时不
    误报"（滚动窗口必须留**末尾**；只留开头会在这里假报警）。载荷用非周期的
    随机字节：周期载荷的周期若整除 VERIFY_TAIL_BYTES（64），开头窗口会落在
    与末尾一模一样的字节上，这条用例就悄悄不再抓"只留开头"的 bug 了；非周期
    载荷不管 VERIFY_TAIL_BYTES 改成多少都保持这一口咬合。
    """
    s, base = dav
    d, t = _dav(s, base)
    payload = random.Random(20260921).randbytes(8 * 1024 * 1024)   # 8 MiB，非周期
    src = tmp_path / "big.bin"
    src.write_bytes(payload)
    s.add_file("/big.bin", payload)              # 远端内容与本地一致
    s.ignore_range = True
    spies = _spy_stream(t)

    n = nsdav.upload(d, str(src), "/big.bin", verify="strong")

    assert n == len(payload)
    sizes = [x for sp in spies for x in sp.sizes]
    assert sizes, "一次 read 都没有？"
    assert all(isinstance(x, int) and 0 < x <= 65536 for x in sizes), sizes


def test_strong_verify_over_range_reads_only_the_tail(dav, tmp_path):
    """206 成功路径：Range 只要尾巴，回读的 body 就只有 tail 字节。

    这条路径此前没有任何用例 —— 唯一走 206 的那条只期望报错，所以"Range 发错成
    整份"这类变异可以活着：内容比对用滚动窗口仍然对得上，只是把整个远端文件
    又拉了一遍（真机上默认走的就是 206）。这里用一个远大于一个块的载荷，让
    "只回了尾巴"和"回了整份"在读次数上分得开。
    """
    s, base = dav
    d, t = _dav(s, base)
    payload = random.Random(20260921).randbytes(256 * 1024)   # 256 KiB，非周期
    src = tmp_path / "big.bin"
    src.write_bytes(payload)
    s.add_file("/big.bin", payload)              # 远端内容与本地一致
    spies = _spy_stream(t)

    n = nsdav.upload(d, str(src), "/big.bin", verify="strong")

    assert n == len(payload)
    # 206 只回 64 字节的尾巴：一次 read 拿到它，再一次拿到空串结束 —— 两次。
    # 若 Range 要的是整份，256 KiB 至少要 4 次满块读才能读完，必红。
    sizes = [x for sp in spies for x in sp.sizes]
    assert sizes, "一次 read 都没有？"
    assert len(sizes) <= 2, sizes


# ── 复审补的用例：大小未知 / 三道守卫 / upload 的错误与回调 ──

class _StubDav:
    """只回答 stat/target 的最小 WebDAV 替身，用来把 download 逼到指定分支。

    这些分支（大小未知、目标是目录）都在**第一个请求之前**就决定了去向，
    所以不需要真服务器 —— 用真服务器反而没法造出"服务端不报大小"。
    """

    def __init__(self, entry, t=None):
        self._entry = entry
        self.t = t

    def stat(self, path):
        return self._entry

    def target(self, path):
        return "/dav" + nsdav.normalize_remote_path(path)


def test_download_refuses_when_the_remote_size_is_unknown(tmp_path):
    """大小未知时**拒绝下载**，绝不能把本地文件截成 0 字节还报成功。

    已复现过的形状：解析层把"缺 getcontentlength"落成 size=0，download 便认
    为"远端是空文件"→ 写一个空 .part、`os.replace` 覆盖本地文件、返回
    (path, 0)、退出码 0。本地那份数据就这么没了，没有任何信号。

    本用例钉两层：抛 NsdavError，以及**本地文件一个字节都没动**。只断"抛错"
    的话，一个"先截断再报错"的实现照样绿。
    """
    dest = tmp_path / "notes.md"
    dest.write_bytes(b"IMPORTANT" * 100)
    stub = _StubDav(nsdav.Entry(path="/f.bin", name="f.bin", is_dir=False,
                                size=None, mtime=None))

    with pytest.raises(nsdav.NsdavError, match="没有返回"):
        nsdav.download(stub, "/f.bin", str(dest))

    assert dest.read_bytes() == b"IMPORTANT" * 100
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.md"]


def test_upload_refuses_when_the_remote_size_is_unknown(dav, tmp_path):
    """传完读回大小、而服务端不报大小时，校验必须**明确说做不了**。

    不能退化成"大小不符：期望 5，远端 None" —— 那句话把"校验做不了"说成了
    "校验没过"，还印一个 None 出来。两者的处置完全不同：前者要找服务端，
    后者要查内容。

    文件确实已经传上去了（`dav.put` 是真的打到了 mock），所以这条同时钉住
    "报错之前该做的请求已经做了" —— 报错说的是校验这步，不是上传那步。
    """
    s, base = dav
    d, t = _dav(s, base)
    src = tmp_path / "up.bin"
    src.write_bytes(b"hello")

    real_stat = d.stat
    d.stat = lambda p: nsdav.Entry(path="/up.bin", name="up.bin",
                                   is_dir=False, size=None, mtime=None)

    with pytest.raises(nsdav.NsdavError, match="校验做不了") as ei:
        nsdav.upload(d, str(src), "/up.bin")
    assert "None" not in str(ei.value), str(ei.value)
    assert s.store["/up.bin"] == b"hello"

    # 而大小报得出来时，这条路是通的（上面那条不是把成功路径一起拒了）
    d.stat = real_stat
    assert nsdav.upload(d, str(src), "/up2.bin") == 5
