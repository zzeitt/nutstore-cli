"""配置层：命令行参数 → 环境变量 → 配置文件 → 内置默认。

所有用例一律显式传 `env=` / `config=`，需要真实文件的用例也都落在
`tmp_path` 里 —— 测试永远不读、也不依赖真实的 HOME。
"""
import dataclasses
import os

import pytest

import nsdav


class Args:
    def __init__(self, **kw):
        self.__dict__.update({
            "url": None, "user": None, "password": None,
            "min_gap": None, "max_retries": None, "timeout": None,
        })
        self.__dict__.update(kw)


def test_defaults_to_jianguoyun(monkeypatch):
    env = {"NSDAV_WEBDAV_USER": "me@x.com", "NSDAV_WEBDAV_PASSWORD": "pw"}
    c = nsdav.load_config(Args(), env=env, config={})
    assert c.host == "dav.jianguoyun.com"
    assert c.base_path == "/dav"
    assert c.user == "me@x.com" and c.password == "pw"


def test_env_url_split_into_host_and_base(monkeypatch):
    env = {"NSDAV_WEBDAV_URL": "https://dav.jianguoyun.com/dav/",
           "NSDAV_WEBDAV_USER": "u", "NSDAV_WEBDAV_PASSWORD": "p"}
    c = nsdav.load_config(Args(), env=env, config={})
    assert c.host == "dav.jianguoyun.com"
    assert c.base_path == "/dav"


def test_custom_port_and_base():
    env = {"NSDAV_WEBDAV_URL": "http://127.0.0.1:8080/dav",
           "NSDAV_WEBDAV_USER": "u", "NSDAV_WEBDAV_PASSWORD": "p"}
    c = nsdav.load_config(Args(), env=env, config={})
    assert c.host == "127.0.0.1" and c.port == 8080 and c.use_tls is False
    assert c.base_path == "/dav"


def test_cli_args_beat_env():
    env = {"NSDAV_WEBDAV_USER": "env@x.com", "NSDAV_WEBDAV_PASSWORD": "envpw"}
    c = nsdav.load_config(Args(user="cli@x.com", password="clipw"),
                          env=env, config={})
    assert c.user == "cli@x.com" and c.password == "clipw"


def test_config_file_used_when_env_absent():
    """config 的 url 真的被用上：主机/端口/明文/路径四项都要断言。

    用非默认值（默认是 https://dav.jianguoyun.com/dav）—— 拿默认值断言的话，
    "根本没读 config 的 url"这个变异也能全绿。
    """
    c = nsdav.load_config(Args(), env={}, config={
        "url": "http://cfg.example.com:8081/dav2",
        "user": "file@x.com", "password": "filepw",
    })
    assert (c.host, c.port, c.use_tls, c.base_path) == (
        "cfg.example.com", 8081, False, "/dav2")
    assert c.user == "file@x.com" and c.password == "filepw"


def test_env_beats_config_file():
    env = {"NSDAV_WEBDAV_USER": "env@x.com", "NSDAV_WEBDAV_PASSWORD": "e"}
    c = nsdav.load_config(Args(), env=env, config={
        "user": "file@x.com", "password": "f"})
    assert c.user == "env@x.com"


def test_missing_credentials_raise_with_guidance():
    with pytest.raises(nsdav.AuthError) as ei:
        nsdav.load_config(Args(), env={}, config={})
    msg = str(ei.value)
    assert "NSDAV_WEBDAV_USER" in msg
    assert "--user" in msg
    assert "config.toml" in msg


def test_min_gap_and_retries_overridable():
    env = {"NSDAV_WEBDAV_USER": "u", "NSDAV_WEBDAV_PASSWORD": "p"}
    c = nsdav.load_config(
        Args(min_gap=1.5, max_retries=9, timeout=300), env=env, config={})
    assert c.min_gap == 1.5 and c.max_retries == 9 and c.timeout == 300


def test_config_file_numbers_are_coerced():
    env = {"NSDAV_WEBDAV_USER": "u", "NSDAV_WEBDAV_PASSWORD": "p"}
    c = nsdav.load_config(Args(), env=env, config={
        "min_gap": "1.5", "max_retries": "9", "timeout": "300"})
    assert c.min_gap == 1.5 and c.max_retries == 9 and c.timeout == 300


def test_bad_config_number_raises_usage_error():
    env = {"NSDAV_WEBDAV_USER": "u", "NSDAV_WEBDAV_PASSWORD": "p"}
    with pytest.raises(nsdav.UsageError, match="min_gap"):
        nsdav.load_config(Args(), env=env, config={"min_gap": "abc"})


# ── 调优项的默认值与环境变量 ──

def test_tuning_knobs_default_and_from_env():
    env = {"NSDAV_WEBDAV_USER": "u", "NSDAV_WEBDAV_PASSWORD": "p"}
    c = nsdav.load_config(Args(), env=env, config={})
    assert c.min_gap == nsdav.DEFAULT_MIN_GAP
    assert c.max_retries == nsdav.DEFAULT_MAX_RETRIES
    assert c.timeout == nsdav.DEFAULT_TIMEOUT
    assert c.port is None and c.use_tls is True

    env.update({"NSDAV_MIN_GAP": "0.75", "NSDAV_MAX_RETRIES": "2",
                "NSDAV_TIMEOUT": "45"})
    c = nsdav.load_config(Args(), env=env, config={})
    assert c.min_gap == 0.75 and c.max_retries == 2 and c.timeout == 45


# ── 配置文件：一律走显式路径，绝不落到真实 HOME ──

def _write(tmp_path, text: str) -> str:
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_config_file_path_honours_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert nsdav.config_file_path() == os.path.join(
        str(tmp_path), "nsdav", "config.toml")


def test_read_config_file_missing_path_is_empty(tmp_path):
    assert nsdav._read_config_file(str(tmp_path / "nope.toml")) == {}


def test_read_config_file_returns_strings(tmp_path):
    path = _write(tmp_path, 'user = "file@x.com"\n'
                            'password = "pw"\n'
                            'min_gap = 1.5\n'
                            'max_retries = 9\n')
    assert nsdav._read_config_file(path) == {
        "user": "file@x.com", "password": "pw",
        "min_gap": "1.5", "max_retries": "9",
    }


def test_load_config_from_file_on_disk(tmp_path):
    path = _write(tmp_path, 'url = "https://dav.jianguoyun.com/dav/"\n'
                            'user = "file@x.com"\npassword = "pw"\n'
                            'min_gap = "0.5"\nmax_retries = "7"\n'
                            'timeout = "30"\n')
    c = nsdav.load_config(Args(), env={}, config=nsdav._read_config_file(path))
    assert c.host == "dav.jianguoyun.com" and c.base_path == "/dav"
    assert c.user == "file@x.com" and c.password == "pw"
    assert c.min_gap == 0.5 and c.max_retries == 7 and c.timeout == 30


def test_explicit_config_is_not_second_guessed_by_the_real_file(
        tmp_path, monkeypatch):
    """显式传了 config= 就只认它。

    把 XDG_CONFIG_HOME 指到 tmp_path 并真放一份可用配置：即便它就在手边，
    显式传入的空映射也必须说了算。这条同时也是"测试不碰真实 HOME"的护栏 ——
    只要哪天 load_config 又顺手去读默认路径，这里就会红。
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "nsdav").mkdir()
    (tmp_path / "nsdav" / "config.toml").write_text(
        'user = "home@x.com"\npassword = "pw"\n', encoding="utf-8")
    with pytest.raises(nsdav.AuthError):
        nsdav.load_config(Args(), env={}, config={})


# ── 与 Task 12 的接口约定 ──

def test_config_spreads_directly_into_transport():
    """Task 12 会把 Config 直接摊平交给 Transport，字段名必须一一对上。"""
    fields = {f.name for f in dataclasses.fields(nsdav.Config)}
    assert fields == {"host", "port", "use_tls", "user", "password",
                      "base_path", "min_gap", "max_retries", "timeout"}
    env = {"NSDAV_WEBDAV_URL": "http://127.0.0.1:8080/dav",
           "NSDAV_WEBDAV_USER": "u", "NSDAV_WEBDAV_PASSWORD": "p"}
    c = nsdav.load_config(Args(), env=env, config={})
    t = nsdav.Transport(c.host, port=c.port, use_tls=c.use_tls, user=c.user,
                        password=c.password, base_path=c.base_path,
                        min_gap=c.min_gap, max_retries=c.max_retries,
                        timeout=c.timeout)
    assert (t.host, t.port, t.use_tls) == ("127.0.0.1", 8080, False)
    assert t.base_path == "/dav" and t.max_retries == 5 and t.timeout == 120.0


# ── 修复轮 1：复审提出的洞 ──

@pytest.mark.parametrize("bad", ["https://h:99999/dav", "https://h:abc/dav"])
def test_bad_port_is_usage_error_not_valueerror(bad):
    """端口越界/非数字要报 UsageError（exit 2），不是裸 ValueError 的 traceback。"""
    with pytest.raises(nsdav.UsageError):
        nsdav.load_config(Args(url=bad), env={}, config={})


_RANGE_CASES = [
    # (env 键, config 键, Args 键, 文本值, 命令行值)
    ("NSDAV_MIN_GAP", "min_gap", "min_gap", "-1", -1.0),
    ("NSDAV_MAX_RETRIES", "max_retries", "max_retries", "-1", -1),
    ("NSDAV_TIMEOUT", "timeout", "timeout", "0", 0.0),
    ("NSDAV_TIMEOUT", "timeout", "timeout", "-5", -5.0),
]

_CRED = {"NSDAV_WEBDAV_USER": "u", "NSDAV_WEBDAV_PASSWORD": "p"}


@pytest.mark.parametrize("source", ["cli", "env", "config"])
@pytest.mark.parametrize("env_key,cfg_key,arg_key,text,cli_value", _RANGE_CASES)
def test_out_of_range_numbers_are_usage_errors(
        source, env_key, cfg_key, arg_key, text, cli_value):
    """越界值从三个来源进来都要报 UsageError。

    校验是单点收口（`pick` → `_number`），这条用例按来源参数化就是钉住"单点"这件事：
    任何"某个来源跳过校验"的实现都会在这里红。
    """
    if source == "cli":
        args, env, cfg = Args(**{arg_key: cli_value}), _CRED, {}
    elif source == "env":
        args, env, cfg = Args(), dict(_CRED, **{env_key: text}), {}
    else:
        args, env, cfg = Args(), _CRED, {cfg_key: text}
    with pytest.raises(nsdav.UsageError):
        nsdav.load_config(args, env=env, config=cfg)


def test_version_gate_warns_and_ignores_config(tmp_path, monkeypatch, capsys):
    """没有 tomllib 时：配置文件被忽略，但必须**出声**，不能静默。"""
    path = _write(tmp_path, 'url = "https://h/dav"\n')
    monkeypatch.setattr(nsdav, "TOML_AVAILABLE", False)
    assert nsdav._read_config_file(path) == {}
    err = capsys.readouterr().err
    assert "已忽略配置文件" in err and path in err


def test_config_file_path_falls_back_to_dot_config(monkeypatch):
    """没设 XDG_CONFIG_HOME 时落到 ~/.config（expanduser 的结果）。"""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(nsdav.os.path, "expanduser",
                        lambda p: "/fake/home" if p == "~" else p)
    # 分隔符交给 os.path.join：本仓库在 Windows 上跑，硬编 "/" 会红。
    assert nsdav.config_file_path() == os.path.join(
        "/fake/home", ".config", "nsdav", "config.toml")


# ── 修复轮 2：范围化复审提出的洞 ──

@pytest.mark.parametrize("bad", ["inf", "-inf", "nan"])
def test_non_finite_numbers_are_usage_errors(bad):
    """inf / nan 能穿过上下限比较，必须在入口就被拒（否则 settimeout 处 traceback）。"""
    env = dict(_CRED, **{"NSDAV_TIMEOUT": bad})
    with pytest.raises(nsdav.UsageError):
        nsdav.load_config(Args(), env=env, config={})


@pytest.mark.parametrize("bad", ["https://", "http:///dav"])
def test_url_without_host_is_usage_error(bad):
    """解析不出主机名要报 UsageError（exit 2），不是让 None 流进 Transport。"""
    with pytest.raises(nsdav.UsageError):
        nsdav.load_config(Args(url=bad), env={}, config={})
