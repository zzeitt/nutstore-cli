"""命令行层：argparse、cmd_* 分发、人类可读 / JSON 双输出。

中间那一段是计划里 T12 的测试围栏。文末"围栏之外"一节是围栏之后陆续补的
夹具与用例：HOME 隔离 fixture，P5 要求的 tree / quota 用例（围栏只给了行为
表，没有用例代码），修复轮 1 的 F1/F2，以及 `rm` 目录不带 `-r` 按用法错
（2）退出这条。
"""
import json
import pytest

import nsdav
from mock_dav import MockDAV


@pytest.fixture
def live_dav(monkeypatch):
    s = MockDAV()
    base = s.start()
    s.add_dir("/d"); s.add_file("/d/one.txt", b"1")
    s.add_file("/d/two.txt", b"22")
    monkeypatch.setenv("NSDAV_WEBDAV_URL", base + "/dav")
    monkeypatch.setenv("NSDAV_WEBDAV_USER", "u")
    monkeypatch.setenv("NSDAV_WEBDAV_PASSWORD", "p")
    yield s
    s.stop()


def run(capsys, *argv):
    code = nsdav.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_ls_lists_entries(live_dav, capsys):
    code, out, _ = run(capsys, "ls", "/d")
    assert code == 0
    assert "one.txt" in out and "two.txt" in out


def test_ls_json(live_dav, capsys):
    code, out, _ = run(capsys, "--json", "ls", "/d")
    assert code == 0
    data = json.loads(out)
    assert sorted(e["name"] for e in data) == ["one.txt", "two.txt"]
    assert data[0]["path"].startswith("/d/")


def test_stat_json(live_dav, capsys):
    code, out, _ = run(capsys, "--json", "stat", "/d/one.txt")
    assert code == 0
    d = json.loads(out)
    assert d["size"] == 1 and d["is_dir"] is False


def test_cat_prints_content(live_dav, capsys):
    code, out, _ = run(capsys, "cat", "/d/two.txt")
    assert code == 0 and out == "22"


def test_put_then_get_roundtrip(live_dav, capsys, tmp_path):
    src = tmp_path / "up.txt"
    src.write_bytes(b"roundtrip")
    code, _, _ = run(capsys, "put", str(src), "/d/up.txt")
    assert code == 0 and live_dav.store["/d/up.txt"] == b"roundtrip"

    dst = tmp_path / "down.txt"
    code, _, _ = run(capsys, "get", "/d/up.txt", str(dst))
    assert code == 0 and dst.read_bytes() == b"roundtrip"


def test_mkdir_and_rm(live_dav, capsys):
    assert run(capsys, "mkdir", "-p", "/d/a/b")[0] == 0
    assert "/d/a/b" in live_dav.dirs
    assert run(capsys, "rm", "-r", "-y", "/d/a")[0] == 0
    assert "/d/a/b" not in live_dav.dirs


def test_rm_recursive_handles_a_deep_tree(live_dav, capsys):
    # 三层。若实现把 walk 的结果逐个删（BFS 父在子前），删掉 /d/a/b 之后
    # 再删 /d/a/b/c 就 404 —— 实测退出码 4；只嵌一层的用例看不见这个。
    # 递归删必须是一个 DELETE，RFC 4918 §9.6.1 规定对集合缺省就是
    # Depth: infinity。
    assert run(capsys, "mkdir", "-p", "/d/a/b/c")[0] == 0

    assert run(capsys, "rm", "-r", "-y", "/d/a")[0] == 0

    assert not [d for d in live_dav.dirs if d.startswith("/d/a")]


def test_mv_and_cp(live_dav, capsys):
    assert run(capsys, "cp", "/d/one.txt", "/d/one-copy.txt")[0] == 0
    assert live_dav.store["/d/one-copy.txt"] == b"1"
    assert run(capsys, "mv", "/d/one-copy.txt", "/d/moved.txt")[0] == 0
    assert "/d/moved.txt" in live_dav.store


def test_rm_recursive_without_yes_asks_and_aborts(live_dav, capsys, monkeypatch):
    asked = _answer_input(monkeypatch, "n")
    code, out, _ = run(capsys, "rm", "-r", "/d")
    assert code != 0
    assert asked, "没有问过就中止了 —— 那不是确认，是直接拒绝"
    assert "/d/one.txt" in out                   # 待删清单先列出来
    assert "/d/one.txt" in live_dav.store        # 没有真删


def test_missing_remote_returns_notfound_exit_code(live_dav, capsys):
    code, _, err = run(capsys, "stat", "/d/absent.txt")
    assert code == nsdav.EXIT_NOTFOUND
    assert "不存在" in err


def test_bad_credentials_return_auth_exit_code(monkeypatch, capsys):
    s = MockDAV(); base = s.start()
    try:
        monkeypatch.setenv("NSDAV_WEBDAV_URL", base + "/dav")
        monkeypatch.setenv("NSDAV_WEBDAV_USER", "u")
        monkeypatch.setenv("NSDAV_WEBDAV_PASSWORD", "WRONG")
        code, _, err = run(capsys, "ls", "/")
        assert code == nsdav.EXIT_AUTH
    finally:
        s.stop()


def test_dry_run_does_not_delete(live_dav, capsys):
    code, out, _ = run(capsys, "--dry-run", "rm", "-r", "-y", "/d")
    assert code == 0
    assert "/d/one.txt" in live_dav.store
    assert "/d/one.txt" in out


def test_format_size():
    assert nsdav.format_size(0) == "0 B"
    assert nsdav.format_size(999) == "999 B"
    assert nsdav.format_size(1024) == "1.0 KiB"
    assert nsdav.format_size(1536) == "1.5 KiB"
    assert nsdav.format_size(5 * 1024 * 1024) == "5.0 MiB"


# ── 围栏之外：HOME 隔离、P5 的两条用例、修复轮 1 的 F1/F2 ──

def _answer_input(monkeypatch, reply):
    """把 `builtins.input` 换成记录器，返回它收到的提问列表。

    只有"被问过"这件事被钉住，`_confirm` 才不是摆设：一个从不调 `input`、
    直接回中止的变异，光看"退出码非 0、文件还在"是看不出来的。
    """
    asked = []

    def fake(prompt=""):
        asked.append(prompt)
        return reply

    monkeypatch.setattr("builtins.input", fake)
    return asked


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """测试永远不读、也不依赖真实的 HOME。

    `nsdav.main()` 会经过 `load_config()` → `config_file_path()`，后者在没设
    `XDG_CONFIG_HOME` 时回落到 `~/.config`。开发机上若真有那个文件，它就会
    参与配置合并（`--url` 之外的字段被它悄悄改写），用例结果随开发机的 home
    目录而变。指向 tmp_path 之后，配置文件路径必然落在临时目录里、必然不存
    在 —— 与 tests/test_config.py 的约定一致。

    autouse 放在文件末尾不影响作用域：fixture 是运行时按模块命名空间解析的。
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_rm_recursive_answering_yes_deletes(live_dav, capsys, monkeypatch):
    """确认了才删：答 `y` 走真删路径（`n` 那半在围栏里）。"""
    asked = _answer_input(monkeypatch, "y")
    code, _, _ = run(capsys, "rm", "-r", "/d")
    assert code == 0
    assert asked
    assert not [k for k in live_dav.store if k.startswith("/d")]


def test_rm_with_yes_does_not_ask(live_dav, capsys, monkeypatch):
    """`-y` 就是"别再问"：此时 `input` 一次都不能被调到。"""
    def boom(prompt=""):
        raise AssertionError(f"给了 -y 还问：{prompt!r}")

    monkeypatch.setattr("builtins.input", boom)
    code, _, _ = run(capsys, "rm", "-r", "-y", "/d")
    assert code == 0
    assert "/d/one.txt" not in live_dav.store


def test_tree_prints_nested_entries(live_dav, capsys):
    """tree 的缩进要真的反映层级，不能把整棵树拉平。"""
    live_dav.add_dir("/d/sub")
    live_dav.add_file("/d/sub/deep.txt", b"x")

    code, out, _ = run(capsys, "tree", "/d")
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "/d"

    def indent_of(name):
        line = [ln for ln in lines if ln.rstrip().endswith(name)][0]
        return len(line) - len(line.lstrip())

    assert "sub/" in out and "deep.txt" in out
    assert indent_of("deep.txt") > indent_of("one.txt")


def test_quota_without_rfc4331_reports_clearly(live_dav, capsys):
    """服务端不返回 RFC 4331 属性时要说人话，不是 traceback。"""
    code, _, err = run(capsys, "quota")
    assert code == nsdav.EXIT_ERROR
    assert "服务端不支持配额查询" in err
    assert "Traceback" not in err


# RFC 4331 的配额响应，属性带诱饵：`urn:example` 里的同名属性排在真值**两侧**，
# 数值都不同（真值 222 / 444，诱饵 111 / 999 / 333 / 888）。命名空间一旦被忽略，
# "后写覆盖"的实现取到尾诱饵 999、"首个命中"的实现取到前诱饵 111，两种写法都
# 拿不到 222 —— 只喂一条 `DAV:` 属性的话，这两种退化实现全是绿的。
# （诱饵只在真值前面时挡不住前者：实测 M1 那种"退回 local name 匹配"的实现是
# 后写覆盖，前诱饵会被真值覆盖掉。见 task-12-report-fix1.md 的变异表。）
QUOTA_XML = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<D:multistatus xmlns:D="DAV:" xmlns:x="urn:example">'
    b'<D:response><D:href>/dav/</D:href><D:propstat><D:prop>'
    b'<x:quota-available-bytes>111</x:quota-available-bytes>'
    b'<x:quota-used-bytes>333</x:quota-used-bytes>'
    b'<D:quota-available-bytes>222</D:quota-available-bytes>'
    b'<D:quota-used-bytes>444</D:quota-used-bytes>'
    b'<x:quota-available-bytes>999</x:quota-available-bytes>'
    b'<x:quota-used-bytes>888</x:quota-used-bytes>'
    b'</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>'
    b'</D:response></D:multistatus>'
)


def test_quota_non_multistatus_root_reports_unsupported(live_dav, capsys):
    """外层不是 multistatus 时也要报"服务端不支持"。

    这里故意带一条 `DAV:` 的配额属性：没有根元素那道闸，它会被当成一条正常
    配额响应读出来（退出码 0、"可用 222 B"），把一个不是 multistatus 的响应
    静默当成了配额。
    """
    live_dav.quota_body = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<D:propfind xmlns:D="DAV:"><D:prop>'
        b'<D:quota-available-bytes>222</D:quota-available-bytes>'
        b'</D:prop></D:propfind>')

    code, _, err = run(capsys, "quota")
    assert code == nsdav.EXIT_ERROR
    assert "服务端不支持配额查询" in err


def test_quota_ignores_foreign_namespace_lookalikes(live_dav, capsys):
    """成功路径：只认 `DAV:` 里的配额属性，同名外来属性一律不算数。"""
    live_dav.quota_body = QUOTA_XML

    code, out, _ = run(capsys, "quota")
    assert code == 0
    assert out.strip() == "可用 222 B，已用 444 B，合计 666 B"


def test_quota_json_ignores_foreign_namespace_lookalikes(live_dav, capsys):
    """`--json` 成功分支同样按命名空间取值。"""
    live_dav.quota_body = QUOTA_XML

    code, out, _ = run(capsys, "--json", "quota")
    assert code == 0
    assert json.loads(out) == {"available": 222, "used": 444}


def test_rm_dir_without_recursive_is_a_usage_error(live_dav, capsys):
    """目录不带 `-r`：CLI 层就当用法错（2）拦下，而不是 DELETE 失败后再报。

    只断退出码是没有区分力的：旧实现由库里的守卫抛 NsdavError，退出码 1，
    而一个"先发 DELETE、再返回 2"的实现同样是 2。所以还钉住 DELETE 一次都
    没发出去、目录与里面的文件都还在。`--dry-run` 也走同一条路 —— 旧实现
    在那种输入下会打印"将删除"并以 0 退出，承诺了一件真跑时必然做不到的事。
    """
    code, _, err = run(capsys, "rm", "/d")
    assert code == nsdav.EXIT_USAGE
    assert "-r" in err

    code, out, _ = run(capsys, "--dry-run", "rm", "/d")
    assert code == nsdav.EXIT_USAGE
    assert "将删除" not in out

    assert not [m for m, _ in live_dav.requests if m == "DELETE"]
    assert "/d" in live_dav.dirs
    assert "/d/one.txt" in live_dav.store



def test_unknown_size_renders_as_a_question_mark(live_dav, capsys, monkeypatch):
    """服务端不报大小时，人类可读的大小列是 `?`，不是 `0 B`。

    这与 `Entry.size` 用 None 而不是 0 是同一条理由的另一半：解析层分开了
    "不知道"和"空"，输出层**必须**跟着分开，否则 `0 B` 又把两者合了回去，
    而且这次是在用户眼前合的。

    用 dataclasses.replace 改真跑出来的条目，让 ls/stat 的其余管路（分页、
    排序、JSON 那条）都还是真的。
    """
    import dataclasses
    real = nsdav.WebDAV.propfind

    def no_size(self, path, depth="1"):
        return [dataclasses.replace(e, size=None) if not e.is_dir else e
                for e in real(self, path, depth)]

    monkeypatch.setattr(nsdav.WebDAV, "propfind", no_size)

    code, out, _ = run(capsys, "ls", "/d")
    assert code == 0
    assert "?" in out and "0 B" not in out, out

    code, out, _ = run(capsys, "stat", "/d/one.txt")
    assert code == 0
    assert out.strip().endswith("?"), out

    # 目录那行仍然是 <dir>，没被这条改动带歪（列 /d 的子项里没有子目录，
    # 要列根才会出现 <dir> 那一行）
    assert "<dir>" in run(capsys, "ls", "/")[1]

    # JSON 那条路给出的是 null（"不知道"），不是 0
    code, out, _ = run(capsys, "--json", "stat", "/d/one.txt")
    assert code == 0
    assert json.loads(out)["size"] is None
