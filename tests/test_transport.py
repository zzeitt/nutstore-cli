import pytest

import nsdav
from mock_dav import MockDAV


@pytest.fixture
def dav():
    s = MockDAV()
    base = s.start()
    yield s, base
    s.stop()


def _transport(base, *, password="p", **kw):
    """password 必须走构造。

    它不能留在 **kw 里：Transport 的 __init__ 有显式的 password 形参，从 **kw
    再传一次就是 duplicate kwarg，直接 TypeError。
    """
    from urllib.parse import urlsplit
    u = urlsplit(base)
    return nsdav.Transport(
        u.hostname, port=u.port, use_tls=False,
        user="u", password=password,
        min_gap=0, **kw,
    )


def test_sends_preemptive_basic_auth(dav):
    s, base = dav
    t = _transport(base)
    r = t.request("PROPFIND", "/dav/", depth="0")
    assert r.status == 207
    assert s.requests == [("PROPFIND", "/dav/")]


def test_bad_credentials_raise_auth_error(dav):
    """错凭据要在**构造时**给进去，不能构造完再改属性。

    原来写的是 `t.password = "wrong"`，但 Transport 构造时就把 Authorization
    头算好存下（preemptive，这正是它的意义），对象上根本没有 user/password
    属性——那句赋值只是往实例上挂了个死属性，线上发的仍是 `Basic dTpw`(u:p)，
    服务器回 207，于是 `pytest.raises(AuthError)` 永远等不到 AuthError。
    写这两条时脑子里想的是 mock 的写法：MockDAV 每次请求现读
    `self.password`（plan:1343），改属性立刻生效；客户端不是这样，也不该是
    这样——整个计划里给客户端 password 的赋值只有这两条用例，生产调用点
    （plan:3117）只有构造那一次，所以是测试写歪了，不是实现少了个属性。
    """
    s, base = dav
    t = _transport(base, password="wrong")
    with pytest.raises(nsdav.AuthError):
        t.raise_for_status_or_raise(t.request("PROPFIND", "/dav/", depth="0"))


def test_missing_path_raises_not_found(dav):
    s, base = dav
    t = _transport(base)
    with pytest.raises(nsdav.NotFoundError):
        t.raise_for_status_or_raise(t.request("GET", "/dav/nope.txt"))


def test_retries_503_then_succeeds():
    s = MockDAV(fail_first_n=2, fail_status=503)
    base = s.start()
    try:
        t = _transport(base, max_retries=5, rand=lambda: 0.5)
        sleeps = []
        t._sleep = sleeps.append
        r = t.request("PROPFIND", "/dav/", depth="0")
        assert r.status == 207
        assert len(s.requests) == 3
        assert sleeps == [2.0, 4.0]     # 503 起跳 2 秒，退避两次
    finally:
        s.stop()


def test_gives_up_after_max_retries():
    s = MockDAV(fail_first_n=99, fail_status=503)
    base = s.start()
    try:
        t = _transport(base, max_retries=2, rand=lambda: 0.5)
        t._sleep = lambda _s: None
        r = t.request("PROPFIND", "/dav/", depth="0")
        assert r.status == 503          # 不再抛，交回调用方判断
        assert len(s.requests) == 3     # 首次 + 2 次重试
    finally:
        s.stop()


def test_auth_failure_is_not_retried(dav):
    s, base = dav
    t = _transport(base, password="wrong", max_retries=5)
    r = t.request("PROPFIND", "/dav/", depth="0")
    assert r.status == 401
    assert len(s.requests) == 1        # 401 绝不重试


def test_410_is_treated_as_missing():
    """410 也当未找到——全局约束里写死的一条，之前一条用例都没看它。

    `410 not in RETRYABLE_STATUS`，所以注入一次就够了，顺带断言它**不被重试**。
    """
    s = MockDAV(fail_first_n=1, fail_status=410)
    base = s.start()
    try:
        t = _transport(base, max_retries=5)
        t._sleep = lambda _s: None
        with pytest.raises(nsdav.NotFoundError):
            t.raise_for_status_or_raise(
                t.request("PROPFIND", "/dav/", depth="0"))
        assert len(s.requests) == 1     # 410 不是可重试状态
    finally:
        s.stop()


def test_429_raises_rate_limit_error():
    """429 映射到 RateLimitError（退出码 5），且确实按 2 秒起跳重试。"""
    s = MockDAV(fail_first_n=99, fail_status=429)
    base = s.start()
    try:
        t = _transport(base, max_retries=1, rand=lambda: 0.5)
        sleeps = []
        t._sleep = sleeps.append
        r = t.request("PROPFIND", "/dav/", depth="0")
        assert r.status == 429
        assert len(s.requests) == 2     # 首次 + 1 次重试
        assert sleeps == [2.0]          # 429 与 503 同为 2 秒起跳
        with pytest.raises(nsdav.RateLimitError):
            t.raise_for_status_or_raise(r)
    finally:
        s.stop()


def test_connection_is_reused(dav):
    """复用要**真的只有一条 TCP 连接**，这里数连接的创建次数。

    原来只断言 `t._conn is not None`——那只说明"请求完没关连接"。把 `_get_conn`
    改成每次都新建连接（复用 100% 失效）照样绿：实测 3 次请求 3 条连接。而关键点 1
    里"实测 6 倍速度差"靠的就是这条复用，名字里的行为不能没人看。
    """
    s, base = dav
    t = _transport(base)
    made = []
    real_new = t._new_conn

    def counting():
        c = real_new()
        made.append(c)
        return c

    t._new_conn = counting
    for _ in range(3):
        t.request("PROPFIND", "/dav/", depth="0")
    assert len(s.requests) == 3
    assert len(made) == 1, f"3 次请求建了 {len(made)} 条连接，复用没生效"
    assert t._conn is made[0]          # 请求完不关连接，且还是那一条


def test_status_retry_drops_the_connection(dav):
    """重试**必须换一条**连接 —— 这是状态分支里 `_drop_conn()` 那行的红线。

    服务器读完请求头就回 503、不读请求体（代理拒绝上传的常见形状）时，请求体
    还在 socket 里；在**同一条**连接上重试，请求行会被接在那堆字节后面，服务器
    看到的方法名是 `abcdefPUT`，重试必败。这条已用裸 socket 独立复现过。

    只断言"最终 201"抓不到它：mock 会把 body 读干净，怎么重试都成功。所以数
    连接条数——503 之后的那次重试必须新建一条。删掉 `_drop_conn()` 这行，本用例
    立刻红，而其余 10 条全绿（实测）。
    """
    s, base = dav
    s.fail_first_n = 1
    s.fail_status = 503
    t = _transport(base, max_retries=1)
    t._sleep = lambda _s: None
    made = []
    real_new = t._new_conn

    def counting():
        c = real_new()
        made.append(c)
        return c

    t._new_conn = counting
    r = t.request("PUT", "/dav/f.bin", body=b"abcdef")
    assert r.status == 201
    assert len(s.requests) == 2, "503 之后必须重试一次"
    assert len(made) == 2, f"重试复用了旧连接（只建了 {len(made)} 条）"


def test_stream_body_retry_reopens_file(tmp_path, dav):
    s, base = dav
    p = tmp_path / "f.bin"
    p.write_bytes(b"abcdef")
    s.fail_first_n = 1
    s.fail_status = 503
    t = _transport(base, max_retries=3, rand=lambda: 0.5)
    t._sleep = lambda _s: None
    body = nsdav.StreamBody(lambda: open(p, "rb"), 6)
    r = t.request("PUT", "/dav/f.bin", body=body)
    assert r.status == 201
    assert s.store["/f.bin"] == b"abcdef"   # 重试后内容完整，不是空的


# ── 复审补的用例：连接失败路径与状态码映射 ──

def test_connection_failure_raises_network_error_after_retries(dav):
    """连不上要走 NetworkError（退出码 6），并且真的退避重试过。

    整条连接失败路径此前**一条用例都没走到**：README 的退出码表里写着 6、
    代码里有 `raise NetworkError`，而 177 条用例没有一条让它执行过。所以
    "把 `raise` 改成 `return None`"这种变异是静默的。

    断言四层：异常类型、退出码、试了几次（首次 + max_retries）、退避序列
    （连接错误 0.5 秒起跳，与 503 的 2 秒不同）。
    """
    s, base = dav
    t = _transport(base, max_retries=2, rand=lambda: 0.5)
    sleeps = []
    t._sleep = sleeps.append
    attempts = []

    def boom():
        attempts.append(1)
        raise OSError("connection refused")

    t._new_conn = boom
    with pytest.raises(nsdav.NetworkError) as ei:
        t.request("PROPFIND", "/dav/", depth="0")

    assert ei.value.exit_code == nsdav.EXIT_NETWORK == 6
    assert len(attempts) == 3, f"只试了 {len(attempts)} 次"
    assert sleeps == [0.5, 1.0], sleeps
    assert s.requests == []          # 一个请求都没发出去


def test_connection_failure_gives_up_immediately_without_retries(dav):
    """max_retries=0 时第一次失败就报错，不再退避等待。"""
    s, base = dav
    t = _transport(base, max_retries=0)
    sleeps = []
    t._sleep = sleeps.append
    t._new_conn = lambda: (_ for _ in ()).throw(OSError("refused"))
    with pytest.raises(nsdav.NetworkError):
        t.request("PROPFIND", "/dav/", depth="0")
    assert sleeps == []


def test_stream_connection_failure_raises_network_error():
    """`stream()` 的连接失败也必须落进 NetworkError。

    下载整条路都走 stream，所以"连不上 / 下到一半断了"是最可能发生的真实
    故障，而它的处理器此前从没被执行过（漏出去就是绕开退出码映射的裸
    OSError，用户看到 traceback 而不是"退出码 6 + 一句话"）。

    用一个**刚刚关掉的**端口打真 socket，不 mock：假造的 OSError 只能证明
    `except` 那行长什么样，证明不了连接失败真的走到那里。
    """
    s = MockDAV()
    base = s.start()
    s.stop()                          # 端口上已经没人听了
    t = _transport(base)
    with pytest.raises(nsdav.NetworkError, match="失败"):
        with t.stream("GET", "/dav/x.bin"):
            pass


def test_403_maps_to_a_permission_error():
    """403 要落到"没有权限"那句话上，而不是被当成未映射状态。"""
    s = MockDAV(fail_first_n=99, fail_status=403)
    base = s.start()
    try:
        t = _transport(base, max_retries=0)
        r = t.request("GET", "/dav/x")
        assert r.status == 403
        with pytest.raises(nsdav.NsdavError, match="没有权限") as ei:
            t.raise_for_status_or_raise(r)
        assert ei.value.exit_code == nsdav.EXIT_ERROR
    finally:
        s.stop()


def test_unmapped_status_is_reported_verbatim(dav):
    """没专门映射的状态码也要说清是几号 —— 走的是最后那条兜底。

    409 / 500 / 501 / 507 重试耗尽后都是从这条出去的，此前没有用例钉它。
    """
    s, base = dav
    t = _transport(base)
    r = t.request("PATCH", "/dav/x")        # mock 的 _unsupported 回 501
    assert r.status == 501
    with pytest.raises(nsdav.NsdavError, match="HTTP 501"):
        t.raise_for_status_or_raise(r)
