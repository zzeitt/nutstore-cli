"""对真实坚果云账号的实测。默认跳过（pytest.ini 里 -m "not live"）。

运行方式：
    NSDAV_WEBDAV_USER=... NSDAV_WEBDAV_PASSWORD=... \
    python -m pytest tests/test_live.py -v -m live

只在 /notes/nsdav-test/ 下操作，测试结束自动清理。
"""
import os
import sys

import pytest

import nsdav

TEST_DIR = "/notes/nsdav-test"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("NSDAV_WEBDAV_USER")
             and os.environ.get("NSDAV_WEBDAV_PASSWORD")),
        reason="需要 NSDAV_WEBDAV_USER / NSDAV_WEBDAV_PASSWORD"),
]


@pytest.fixture
def dav():
    cfg = nsdav.load_config(type("A", (), {
        "url": None, "user": None, "password": None,
        "min_gap": None, "max_retries": None, "timeout": None})(), config={})
    assert cfg.base_path == "/dav", (
        f"实测只许在 /dav/notes 下；当前 base_path={cfg.base_path}，"
        f"检查 NSDAV_WEBDAV_URL 与配置文件")
    t = nsdav.Transport(cfg.host, port=cfg.port, use_tls=cfg.use_tls,
                        user=cfg.user, password=cfg.password,
                        base_path=cfg.base_path, min_gap=cfg.min_gap,
                        max_retries=cfg.max_retries, timeout=cfg.timeout)
    try:
        d = nsdav.WebDAV(t, base_path=cfg.base_path)
        d.mkdirs(TEST_DIR)
        yield d
        try:
            d.delete(TEST_DIR, recursive=True)
        except nsdav.NsdavError as exc:
            print(f"清理 {TEST_DIR} 失败，请手动删除：{exc}", file=sys.stderr)
    finally:
        t.close()


def test_01_put_stat_read(dav, tmp_path):
    dav.put(f"{TEST_DIR}/hello.txt", "你好 坚果云\n".encode())
    e = dav.stat(f"{TEST_DIR}/hello.txt")
    assert e.size == len("你好 坚果云\n".encode())
    assert dav.read(f"{TEST_DIR}/hello.txt") == "你好 坚果云\n".encode()


def test_02_head_is_useless_but_propfind_is_not(dav):
    """把实测到的服务器怪癖固化成断言，将来服务端改了会立刻发现。"""
    dav.put(f"{TEST_DIR}/size.txt", b"12345")
    resp = dav.t.request("HEAD", dav.target(f"{TEST_DIR}/size.txt"))
    assert resp.headers.get("content-length") == "0"
    assert dav.stat(f"{TEST_DIR}/size.txt").size == 5


def test_03_missing_path_is_404(dav):
    """缺失路径是 404（410 也算）—— 钉的是**服务器事实**，不是异常类型。

    stat() 对"真 404"和"207 但零条目"抛的是同一个 NotFoundError，所以只写
    pytest.raises 的话，服务器改成回 207 空 multistatus 这条仍会绿。
    这里直接看状态码，客户端属性那一半留在下面。
    """
    resp = dav.t.request("PROPFIND", dav.target(f"{TEST_DIR}/definitely-absent.txt"),
                         depth="0")
    assert resp.status in (404, 410), f"缺失路径应回 404/410，实际 {resp.status}"
    with pytest.raises(nsdav.NotFoundError):
        dav.stat(f"{TEST_DIR}/definitely-absent.txt")


def test_04_range_download(dav, tmp_path):
    payload = bytes(range(256)) * 512
    dav.put(f"{TEST_DIR}/range.bin", payload)
    dest = tmp_path / "range.bin"
    nsdav.download(dav, f"{TEST_DIR}/range.bin", str(dest), chunk=4096)
    assert dest.read_bytes() == payload


def test_05_download_resumes(dav, tmp_path):
    payload = bytes(range(256)) * 512
    dav.put(f"{TEST_DIR}/resume.bin", payload)
    dest = tmp_path / "resume.bin"
    with open(str(dest) + ".part", "wb") as f:
        f.write(payload[:10000])
    nsdav.download(dav, f"{TEST_DIR}/resume.bin", str(dest), chunk=8192)
    assert dest.read_bytes() == payload


def test_06_special_characters_roundtrip(dav, tmp_path):
    name = "中文 文件名 + 加号 #井号.txt"
    dav.put(f"{TEST_DIR}/{name}", b"content")
    assert dav.read(f"{TEST_DIR}/{name}") == b"content"
    assert name in [e.name for e in dav.listdir(TEST_DIR)]


# raises= 不能省：只挂 xfail 标记的话，任何异常与断言失败都算"预期失败"，
# 客户端真出别的故障就被吞了。限定只认 NotFoundError，才把"已知服务端缺陷"
# 和"真出事了"分开（pytest 9.1.1 实测：别的异常/取回条数不足仍报 FAIL，
# 服务端哪天不二次编码了则 XPASS 判红）。
@pytest.mark.xfail(strict=True, raises=nsdav.NotFoundError, reason="服务端分页 Link 把已编码的 path 又编码一次（坚果云缺陷，见函数内注释）")
def test_07_pagination_over_750_in_special_directory(dav):
    """分页 + 特殊字符目录名。这条最慢，放最后。

    **目录名故意带空格和中文**，不是装饰。分页 URL 的编码形式是 T6 的 P35 留
    下的悬案：mock 修好之前发的是解码后的 rel（裸空格 → http.client.InvalidURL；
    中文目录名 → send_header 抛 UnicodeEncodeError，客户端只见 RemoteDisconnected
    且拿不到 Link 头），而**真实服务器发什么形状一直没人看过**。只看 ASCII
    名字分页抓不住这件事——编码函数对 ASCII 是恒等的，路径里没有需要编码的
    字符，服务器给什么形状都"能用"。

    两个症状都只在"分页 + 特殊字符"这个组合下出现，所以这条必须同时具备两者。
    ASCII 的通用分页由 mock 层的 test_listdir_follows_pagination 覆盖，分工不重
    叠：那边证明客户端逻辑，这边证明真实服务器的 Link 客户端吃得下。

    断言的是**客户端属性**（能不能把 760 条都取回来），不是服务器的字节形状：
    服务器真要是发了双编码的 Link，这条会以"取不满"或直接抛错失败，那才是我们
    要立刻知道的事；把 Link 的具体字节焊进断言则会在服务器无害改版时误报。

    2026-09-21 实测证实服务器确实发了双编码的 Link，故本用例 xfail：
    服务器页大小 750；这类目录的 Link 里 path 是 %25E5%2588...（编码了
    两次），而同一个 Link 的 ?mk= 参数只编码一次；把 Link 原样重发 →
    404 ObjectNotFound；把 path 手动 unquote 一次、query 原样 → 207 且
    剩下 10 条全回来；纯 ASCII 的同规模目录（760 条）分页完全正常。
    """
    sub = f"{TEST_DIR}/分页 目录"
    dav.mkdirs(sub)
    for i in range(760):
        dav.put(f"{sub}/p-{i:04d}.txt", b"x")
    try:
        names = [e.name for e in dav.listdir(sub)]
    except nsdav.NotFoundError as exc:
        pytest.xfail(f"服务端分页 Link 的 path 二次编码导致 404：{exc}")
    paged = [n for n in names if n.startswith("p-")]
    assert len(paged) == 760


def test_08_mv_cp(dav):
    dav.put(f"{TEST_DIR}/a.txt", b"data")
    dav.move(f"{TEST_DIR}/a.txt", f"{TEST_DIR}/b.txt")
    assert not dav.exists(f"{TEST_DIR}/a.txt")
    dav.copy(f"{TEST_DIR}/b.txt", f"{TEST_DIR}/c.txt")
    assert dav.read(f"{TEST_DIR}/c.txt") == b"data"
