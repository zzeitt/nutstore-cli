# nsdav 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个单文件、零依赖的 Python 3 CLI，用原生 WebDAV 协议可靠读写坚果云文件，可在 iOS iSH 里直接运行。

**Architecture:** 四层单向依赖 —— `Transport`（HTTP 长连接、限流、重试、状态码归一）→ `WebDAV`（协议操作，含分页循环）→ 命令函数 → `argparse` 分发。全部代码在 `nsdav.py` 一个文件里，按上述顺序分节。测试基础设施在 `tests/`。

**Tech Stack:** Python 3.11+ 标准库（`http.client`、`xml.etree.ElementTree`、`argparse`、`email.utils`、`base64`），测试用 pytest。**不允许任何第三方运行时依赖。**

**Spec:** `docs/superpowers/specs/2026-09-21-nutstore-webdav-cli-design.md`

## Global Constraints

- 运行时**只能**用 Python 标准库。不得 import `requests`、`httpx` 等任何第三方包。
- 唯一运行产物是仓库根目录的 `nsdav.py`，单文件。测试代码不得被它 import。
- Python 版本下限 3.11（`tomllib`、`X | Y` 类型语法）。用 `from __future__ import annotations` 保持类型标注向后兼容。
- 所有面向用户的输出走 `stdout`，日志与进度走 `stderr`。
- 硬性服务器事实（实测得出，违反即 bug）：
  - **永远不用 `HEAD` 取大小**——坚果云恒返回 `Content-Length: 0`。用 `PROPFIND`。
  - **缺失路径是 `404`**。`410` 也当未找到处理（防御性）。
  - **目录列表满 750 条会分页**，靠 `Link: <...>; rel="next"` 判断，marker 式。
  - **分页返回的 URL 已经编码过**，禁止再过 `enc_path`。
- XML 必须按命名空间解析，`DAV:` 前缀绑定为 `{DAV:}`。不得用字符串匹配。
- 退出码：`0` 成功 / `1` 一般错误 / `2` 用法错 / `3` 认证失败 / `4` 未找到 / `5` 被限流 / `6` 网络错误。
- 提交信息遵循 Conventional Commits + 3Cs 骨架（`Changes` / `Context` / `Considerations`），subject ≤ 60 字符，body 每行 ≤ 72 字符。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `nsdav.py` | 唯一运行产物。内部按节排列：异常与退出码 → `Entry` → 路径编码 → Link 解析 → PROPFIND 解析 → 退避 → 限流器 → `Transport` → `WebDAV` → 配置 → 命令 → `main` |
| `pytest.ini` | `pythonpath = .`，注册 `live` marker 并默认跳过 |
| `tests/mock_dav.py` | 进程内假 WebDAV 服务器，可注入故障（分页、503、404、断连） |
| `tests/test_paths.py` | `enc_path` / `normalize_remote_path` |
| `tests/test_link.py` | `parse_next_link` |
| `tests/test_propfind.py` | `parse_multistatus` |
| `tests/test_retry.py` | `backoff_delay` / `is_retryable` |
| `tests/test_ratelimit.py` | `RateLimiter` |
| `tests/test_transport.py` | `Transport` 对 mock 服务器 |
| `tests/test_webdav.py` | `WebDAV` 各操作，重点测分页循环 |
| `tests/test_transfer.py` | 下载分块/续传、上传校验 |
| `tests/test_config.py` | 配置优先级 |
| `tests/test_cli.py` | 端到端跑 `main()` |
| `tests/test_live.py` | 实测，`@pytest.mark.live` |
| `README.md` / `AGENTS.md` / `LICENSE` | 文档与 AGPL-3.0 许可证 |

---

### Task 1: 骨架、异常、Entry、路径编码

**Files:**
- Create: `pytest.ini`, `nsdav.py`, `tests/test_paths.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `NsdavError(Exception)`，类属性 `exit_code: int`；子类 `UsageError(2)`、`AuthError(3)`、`NotFoundError(4)`、`RateLimitError(5)`、`NetworkError(6)`
  - `@dataclass Entry(path: str, name: str, is_dir: bool, size: int, mtime: float | None)`
  - `enc_path(raw: str) -> str` —— 只编码，不解析相对路径
  - `normalize_remote_path(raw: str) -> str` —— 相对路径 → 服务器绝对路径（未编码）

- [ ] **Step 1: 写失败的测试**

`tests/test_paths.py`:

```python
import pytest
import nsdav


@pytest.mark.parametrize("raw,expected", [
    ("/a/b.txt", "/a/b.txt"),
    ("/a b/c.txt", "/a%20b/c.txt"),
    ("/a+b/c.txt", "/a%2Bb/c.txt"),          # '+' 必须变 %2B，不能留成 '+'
    ("/中文/文件.txt", "/%E4%B8%AD%E6%96%87/%E6%96%87%E4%BB%B6.txt"),
    ("/emoji/🎉.md", "/emoji/%F0%9F%8E%89.md"),
    ("/pct/100%.txt", "/pct/100%25.txt"),    # 字面 % 必须转义
    ("/hash/a#b.txt", "/hash/a%23b.txt"),
    ("/q/a?b.txt", "/q/a%3Fb.txt"),          # 文件名里的字面 '?' 也要能访问
])
def test_enc_path_encodes_segments(raw, expected):
    assert nsdav.enc_path(raw) == expected


def test_enc_path_has_no_query_concept():
    """enc_path 眼里没有 query 这回事，'?' 就是普通路径字符。

    分页 marker 从不经过这里 —— 它走 url_to_target（Task 2），那条路
    原样透传已编码的 URL。所以双编码 bug 在调用图上就被排除了，
    不需要 enc_path 去"小心处理 query"。

    期望值里 '=' 变 %3D、'%2F' 变 %252F，不是笔误：enc_path 收到的是
    "用户原始路径"，每个字符都是字面的，'%' 自然也当字面百分号编码
    （和上面 100%.txt 那例一致）。分页 marker 里那个已经是 %2F 的值
    永远不会走到这里，所以这里编出来的 %252F 无害 —— 双编码之所以
    不可能发生，靠的是调用图，不是这里的小心处理。
    """
    assert nsdav.enc_path("/dav/x?mk=%2Fy") == "/dav/x%3Fmk%3D%252Fy"


@pytest.mark.parametrize("raw,expected", [
    ("", "/"),
    ("/", "/"),
    (".", "/"),
    ("a.txt", "/a.txt"),
    ("notes/a.txt", "/notes/a.txt"),
    ("//a///b//", "/a/b"),
    ("/a/./b", "/a/b"),
    ("/a/b/../c", "/a/c"),
    ("/../../etc/passwd", "/etc/passwd"),   # 逃不出根
])
def test_normalize_remote_path(raw, expected):
    assert nsdav.normalize_remote_path(raw) == expected
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_paths.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'nsdav'`

- [ ] **Step 3: 写 pytest.ini**

```ini
[pytest]
pythonpath = .
testpaths = tests
markers =
    live: 需要真实坚果云账号，默认跳过
addopts = -m "not live"
```

- [ ] **Step 4: 写 nsdav.py 的前四节**

```python
#!/usr/bin/env python3
"""nsdav — 坚果云 WebDAV 命令行工具。

单文件，仅标准库。为 iOS iSH 等受限环境设计。
"""
from __future__ import annotations

import argparse
import base64
import email.utils
import http.client
import json
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Iterator, NamedTuple
from urllib.parse import quote, unquote, urlsplit

__version__ = "0.1.0"

DEFAULT_HOST = "dav.jianguoyun.com"
DEFAULT_BASE = "/dav"
DEFAULT_MIN_GAP = 0.2
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RETRIES = 5
DOWNLOAD_CHUNK = 16 * 1024 * 1024

DAV = "{DAV:}"
NS_UA = "Obsidian (iOS; Phone; ObsidianNutstoreSync/1.5.0)"
MOCK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<propfind xmlns="DAV:"><prop>'
    b'<displayname/><resourcetype/><getlastmodified/>'
    b'<getcontentlength/><getcontenttype/>'
    b'</prop></propfind>'
)


# ─────────────────────────── 退出码与异常 ───────────────────────────

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NOTFOUND = 4
EXIT_RATELIMIT = 5
EXIT_NETWORK = 6


class NsdavError(Exception):
    exit_code = EXIT_ERROR


class UsageError(NsdavError):
    exit_code = EXIT_USAGE


class AuthError(NsdavError):
    exit_code = EXIT_AUTH


class NotFoundError(NsdavError):
    exit_code = EXIT_NOTFOUND


class RateLimitError(NsdavError):
    exit_code = EXIT_RATELIMIT


class NetworkError(NsdavError):
    exit_code = EXIT_NETWORK


# ─────────────────────────────── 数据模型 ───────────────────────────────

@dataclass
class Entry:
    """远端一个文件或目录。path 是相对 WebDAV 根的路径，已解码。"""
    path: str
    name: str
    is_dir: bool
    size: int
    mtime: float | None


# ─────────────────────────────── 路径处理 ───────────────────────────────

def enc_path(raw: str) -> str:
    """把未编码的路径逐段编码。

    这里没有 query 的概念：'?' 和 '#' 一样，都是普通路径字符，会被
    编码掉。这样文件名里带 '?' 也能正常访问。

    分页 marker 不走这里 —— 它由 url_to_target() 原样透传已编码的
    URL 给 Transport。双编码 bug 因此在调用图上就不可能发生，而不是
    靠"记得别编 query"这种约定来避免。
    """
    return "/".join(quote(seg, safe="") for seg in raw.split("/"))


def normalize_remote_path(raw: str) -> str:
    """把用户输入变成相对 WebDAV 根的绝对路径（未编码）。

    折叠重复斜杠、解析 '.' 与 '..'，并且不允许越过根。
    """
    raw = (raw or "").strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    out: list[str] = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    return "/" + "/".join(out)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_paths.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add pytest.ini nsdav.py tests/test_paths.py
git commit -m "feat(core): 添加路径编码与数据模型"
```

---

### Task 2: Link 分页头解析

**Files:**
- Modify: `nsdav.py`（新增一节）
- Test: `tests/test_link.py`

**Interfaces:**
- Consumes: 无
- Produces: `parse_next_link(header: str | None) -> str | None`、`url_to_target(url: str) -> str`

**为什么重要：** 目录满 750 条时坚果云返回 `Link: <https://...?mk=xxx>; rel="next"`。
不解析就静默丢文件 —— 这是整个工具最核心的一条可靠性。

- [ ] **Step 1: 写失败的测试**

`tests/test_link.py`:

```python
import nsdav


def test_parses_plain_next():
    h = '<https://dav.jianguoyun.com/dav/notes/x?mk=%2Fx%2Fa.txt>; rel="next"'
    assert nsdav.parse_next_link(h) == \
        "https://dav.jianguoyun.com/dav/notes/x?mk=%2Fx%2Fa.txt"


def test_ignores_rel_prev_only():
    assert nsdav.parse_next_link('<https://h/p>; rel="prev"') is None


def test_picks_next_out_of_multiple_links():
    h = ('<https://h/a>; rel="prev", '
         '<https://h/b>; rel="next", '
         '<https://h/c>; rel="last"')
    assert nsdav.parse_next_link(h) == "https://h/b"


def test_rel_with_multiple_tokens():
    assert nsdav.parse_next_link('<https://h/b>; rel="next last"') == "https://h/b"


def test_no_header_or_garbage():
    assert nsdav.parse_next_link(None) is None
    assert nsdav.parse_next_link("") is None
    assert nsdav.parse_next_link("not a link header") is None


def test_url_to_target_keeps_query_encoded():
    u = "https://dav.jianguoyun.com/dav/notes/x?mk=%2Fx%2Fa.txt"
    assert nsdav.url_to_target(u) == "/dav/notes/x?mk=%2Fx%2Fa.txt"


def test_url_to_target_without_query():
    assert nsdav.url_to_target("https://h/dav/a/b") == "/dav/a/b"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_link.py -v`
Expected: FAIL —— `AttributeError: module 'nsdav' has no attribute 'parse_next_link'`

- [ ] **Step 3: 实现**

追加到 `nsdav.py`：

```python
# ─────────────────────────── Link 头 / 分页 ───────────────────────────

def _split_link_values(header: str) -> list[str]:
    """按逗号切分 Link 头，但不切 <> 和引号内部的逗号。"""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    in_quotes = False
    for ch in header:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "<" and not in_quotes:
            depth += 1
        elif ch == ">" and not in_quotes:
            depth -= 1
        if ch == "," and depth == 0 and not in_quotes:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def parse_next_link(header: str | None) -> str | None:
    """从 Link 头里取出 rel="next" 的 URL，没有就返回 None。"""
    if not header:
        return None
    for part in _split_link_values(header):
        m = re.match(r"\s*<([^>]+)>\s*(.*)$", part, re.S)
        if not m:
            continue
        url, params = m.group(1), m.group(2)
        for pm in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', params):
            if pm.group(1).lower() == "rel" and "next" in pm.group(2).split():
                return url
    return None


def url_to_target(url: str) -> str:
    """把完整 URL 变成 http.client 用的 target。

    注意：path 和 query 都已经是编码过的，结果必须【直接】交给
    Transport，绝不能再过一次 enc_path。
    """
    sp = urlsplit(url)
    return sp.path + (("?" + sp.query) if sp.query else "")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_link.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add nsdav.py tests/test_link.py
git commit -m "feat(core): 解析 Link 头的 rel=next 分页标记"
```

---

### Task 3: PROPFIND 响应解析

**Files:**
- Modify: `nsdav.py`
- Test: `tests/test_propfind.py`

**Interfaces:**
- Consumes: `Entry`（Task 1）
- Produces: `parse_multistatus(xml_bytes: bytes, base_path: str) -> list[Entry]`、`parse_http_date(s: str) -> float | None`

**关键点：** 必须按命名空间解析（实测响应是 `<d:multistatus xmlns:d="DAV:">`）。
只认 `propstat` 里 `status` 含 `200` 的那些 prop，否则会把 404 的占位条目读成文件。

- [ ] **Step 1: 写失败的测试**

`tests/test_propfind.py`:

```python
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
    xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>
    <d:href>/dav/x</d:href><d:propstat><d:prop><d:resourcetype/></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''
    e = nsdav.parse_multistatus(xml, "/dav")[0]
    assert e.size == 0 and e.mtime is None and not e.is_dir


def test_dir_path_keeps_trailing_slash():
    d = nsdav.parse_multistatus(TWO_ITEMS, "/dav")[0]
    assert d.path.endswith("/")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_propfind.py -v`
Expected: FAIL —— `AttributeError: module 'nsdav' has no attribute 'parse_multistatus'`

- [ ] **Step 3: 实现**

```python
# ────────────────────────── PROPFIND 响应解析 ──────────────────────────

def parse_http_date(value: str | None) -> float | None:
    """解析 RFC 1123 日期（坚果云用的格式），失败返回 None。"""
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _status_is_ok(status_text: str | None) -> bool:
    return bool(status_text) and " 200 " in f" {status_text.strip()} "


def parse_multistatus(xml_bytes: bytes, base_path: str) -> list[Entry]:
    """把 multistatus 响应解析成 Entry 列表。

    base_path 是 WebDAV 根的路径前缀（如 '/dav'），解析出的 Entry.path
    相对该前缀。响应里的 href 是百分号编码的，这里解码。

    响应体不是合法 XML、或根元素不是 {DAV:}multistatus 时抛 NsdavError。
    这两种情况下继续解析都会得到空列表，而空列表被上层当成"空目录"，
    正是本项目要避免的静默失败。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise NsdavError(f"响应不是合法 XML: {exc}") from exc
    if root.tag != f"{DAV}multistatus":
        raise NsdavError(
            f"响应根元素是 {root.tag!r}，不是 {DAV}multistatus"
            " —— 服务器没有按 WebDAV 返回，继续解析只会得到空列表"
        )
    base = base_path.rstrip("/")
    out: list[Entry] = []

    for resp in root.findall(f"{DAV}response"):
        href_el = resp.find(f"{DAV}href")
        if href_el is None or not href_el.text:
            continue
        href = unquote(href_el.text.strip())

        props: dict[str, Any] = {}
        for ps in resp.findall(f"{DAV}propstat"):
            if not _status_is_ok(ps.findtext(f"{DAV}status")):
                continue
            prop = ps.find(f"{DAV}prop")
            if prop is None:
                continue
            for child in prop:
                props[child.tag] = child

        rtype = props.get(f"{DAV}resourcetype")
        is_dir = rtype is not None and rtype.find(f"{DAV}collection") is not None

        size_el = props.get(f"{DAV}getcontentlength")
        size = 0
        if size_el is not None and size_el.text and size_el.text.strip().isdigit():
            size = int(size_el.text.strip())

        mtime = parse_http_date(
            props[f"{DAV}getlastmodified"].text
            if f"{DAV}getlastmodified" in props else None
        )

        # 按整段边界比，不能用朴素前缀：base '/dav' 会匹配上 '/davos/x'。
        # href 恰好等于 base（自身那一条）时 rel 落成 ""，下面的补 '/' 会接管。
        if base and (href == base or href.startswith(base + "/")):
            rel = href[len(base):]
        else:
            rel = href
        if not rel.startswith("/"):
            rel = "/" + rel
        if is_dir:
            if not rel.endswith("/"):
                rel += "/"
            name = rel.rstrip("/").rsplit("/", 1)[-1]
        else:
            name = rel.rsplit("/", 1)[-1]

        out.append(Entry(path=rel, name=name, is_dir=is_dir,
                         size=0 if is_dir else size, mtime=mtime))
    return out
```

同时在文件顶部的 import 里加上 `import xml.etree.ElementTree as ET` 和
`from datetime import timezone`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_propfind.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add nsdav.py tests/test_propfind.py
git commit -m "feat(core): 按命名空间解析 PROPFIND 多状态响应"
```

---

### Task 4: 退避策略与可重试判定

**Files:**
- Modify: `nsdav.py`
- Test: `tests/test_retry.py`

**Interfaces:**
- Consumes: 无
- Produces: `RETRYABLE_STATUS: frozenset[int]`、`backoff_delay(attempt: int, status: int | None, *, jitter: float = 0.25, rand: Callable[[], float] = random.random) -> float`

**设计取舍：** 插件在 503 上是**死等 60 秒**且不可中断。这里改成指数退避：
429/503 起跳 2 秒，其它 5xx 与连接错误起跳 0.5 秒，封顶 60 秒，带 ±25% 抖动
（抖动是为了避免多个客户端同时重试形成尖峰）。5 次重试累计约 62 秒，
与插件量级相当但能快速失败。**60 秒是实际等待时间的上界**，抖动加完再封顶。

- [ ] **Step 1: 写失败的测试**

`tests/test_retry.py`:

```python
import nsdav


def test_retryable_status_set():
    assert 503 in nsdav.RETRYABLE_STATUS
    assert 429 in nsdav.RETRYABLE_STATUS
    assert 500 in nsdav.RETRYABLE_STATUS
    # 这几个绝不能重试
    assert 401 not in nsdav.RETRYABLE_STATUS
    assert 403 not in nsdav.RETRYABLE_STATUS
    assert 404 not in nsdav.RETRYABLE_STATUS
    assert 410 not in nsdav.RETRYABLE_STATUS


def _no_jitter():
    return 0.5


def test_backoff_grows_exponentially_for_503():
    d = [nsdav.backoff_delay(i, 503, rand=_no_jitter) for i in range(1, 6)]
    assert d == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_backoff_for_connection_error_starts_lower():
    assert nsdav.backoff_delay(1, None, rand=_no_jitter) == 0.5


def test_backoff_for_429_also_starts_at_two_seconds():
    """429 的起跳值要和 503 一样是 2 秒。

    只断言 `429 in RETRYABLE_STATUS` 抓不住"重试它，但按 0.5 秒起跳"这种
    实现——那等于没把 429 当成降速信号。必须断言它的**基值**。
    """
    assert nsdav.backoff_delay(1, 429, rand=_no_jitter) == 2.0


def test_backoff_is_capped():
    assert nsdav.backoff_delay(20, 503, rand=_no_jitter) == 60.0


def test_backoff_never_exceeds_the_cap():
    """封顶是**实际等待时间**的上界，抖动加完也不能越过。

    先封顶再加抖动的话，rand() 取 1.0 时实际会等到 60*1.25 = 75 秒，而规格
    写的是封顶 60 秒——用户读到的是一个代码不兑现的承诺。
    """
    assert nsdav.backoff_delay(20, 503, rand=lambda: 1.0) == 60.0
    assert nsdav.backoff_delay(20, None, rand=lambda: 1.0) == 60.0


def test_jitter_stays_within_bounds():
    lo = nsdav.backoff_delay(3, 503, rand=lambda: 0.0)
    hi = nsdav.backoff_delay(3, 503, rand=lambda: 1.0)
    assert lo == 8.0 * 0.75
    assert hi == 8.0 * 1.25
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_retry.py -v`
Expected: FAIL —— `AttributeError: module 'nsdav' has no attribute 'RETRYABLE_STATUS'`

- [ ] **Step 3: 实现**

```python
# ──────────────────────────── 重试与退避 ────────────────────────────

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_BACKOFF_BASE_SOFT = 0.5    # 连接错误、普通 5xx
_BACKOFF_BASE_HARD = 2.0    # 429 / 503，服务端明确要求降速
_BACKOFF_CAP = 60.0


def backoff_delay(
    attempt: int,
    status: int | None,
    *,
    jitter: float = 0.25,
    rand: Callable[[], float] = random.random,
) -> float:
    """第 attempt 次重试前该等多久（attempt 从 1 开始）。

    抖动在封顶**之前**加，所以 `_BACKOFF_CAP` 是实际等待时间的真正上界。
    反过来先封顶再加抖动的话，rand() 取 1.0 时会等到 75 秒，而规格说的是
    封顶 60 秒。代价是退避到顶之后抖动只剩一半区间（48~60 而不是 45~75），
    但本工具是串行的、还有 200ms 最小间隔，抖动的意义本就只是兜底，不值得
    为它让规格里那句承诺变成假的。
    """
    base = _BACKOFF_BASE_HARD if status in (429, 503) else _BACKOFF_BASE_SOFT
    raw = base * (2 ** (attempt - 1))
    raw *= 1.0 + jitter * (2.0 * rand() - 1.0)
    return min(raw, _BACKOFF_CAP)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_retry.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add nsdav.py tests/test_retry.py
git commit -m "feat(core): 指数退避重试策略"
```

---

### Task 5: 限流器

**Files:**
- Modify: `nsdav.py`
- Test: `tests/test_ratelimit.py`

**Interfaces:**
- Consumes: 无
- Produces: `RateLimiter(min_gap: float, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep)`，方法 `wait() -> None`

**为什么：** 插件的 Bottleneck 配置是 `maxConcurrent: 1, minTime: 200`。
串行 + 200ms 间隔是它验证过的、不被限流的节奏。

- [ ] **Step 1: 写失败的测试**

`tests/test_ratelimit.py`:

```python
import inspect
import time

import pytest

import nsdav


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_first_call_does_not_sleep():
    c = FakeClock()
    rl = nsdav.RateLimiter(0.2, clock=c.now, sleep=c.sleep)
    rl.wait()
    assert c.slept == []


def test_second_call_sleeps_remaining_gap():
    """睡的是"还差的那部分"：已经过去 0.05 秒，就只补 0.15 秒。

    这里必须用 approx。假时钟走的是 `1000.0 + 0.05`，而浮点上
    `1000.05 - 1000.0` 不等于字面量 `0.05`（差约 4.5e-14），于是实现算出的
    "还差多少"和断言里手写的 `0.2 - 0.05` 也不是同一个数。拿被减出来的量做
    精确相等比较，工具就用错了——第一版就是这么写的，按计划自己的实现跑
    也过不去。
    """
    c = FakeClock()
    rl = nsdav.RateLimiter(0.2, clock=c.now, sleep=c.sleep)
    rl.wait()
    c.t += 0.05
    rl.wait()
    assert c.slept == pytest.approx([0.2 - 0.05])


def test_negative_gap_is_clamped_to_zero():
    """负的间隔按 0 处理，`min_gap` 这个公开属性也不该是负数。"""
    assert nsdav.RateLimiter(-1.0).min_gap == 0.0


def test_consecutive_waits_are_never_closer_than_min_gap():
    """核心不变量：相邻两次 wait() **返回时的钟值**至少差 min_gap。

    前面几条最多只走两次 wait()，而且没有一条在"上次没睡"之后再调一次，所以
    这条路径原来无人看管。把 `self._last = self._clock()` 顺手写进
    `if delta < self.min_gap:` 分支里——最自然的滑法——前面几条全绿，间隔却
    真的破了：返回时刻会是 [1000.0, 1000.2, 1000.7, 1000.71]，最后两跳只隔
    0.01 秒，而 min_gap 是 0.2。这个限流器的全部意义就是这个间隔。
    """
    c = FakeClock()
    rl = nsdav.RateLimiter(0.2, clock=c.now, sleep=c.sleep)
    stamps = []
    for step in (0.0, 0.05, 0.5, 0.01, 0.19):
        c.t += step
        rl.wait()
        stamps.append(c.t)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(gaps) == 4
    assert min(gaps) >= 0.2 - 1e-9


def test_defaults_are_the_production_clock_and_sleep():
    """默认值必须是真在生产里用的那对，因为假时钟的用例永远走不到它们。

    把 `time.monotonic` 换成 `time.time` 之类同样全绿，而生产上钟被回拨就会
    算错间隔。默认值不钉，这一层就没人看。
    """
    params = inspect.signature(nsdav.RateLimiter.__init__).parameters
    assert params["clock"].default is time.monotonic
    assert params["sleep"].default is time.sleep


def test_no_sleep_when_gap_already_elapsed():
    c = FakeClock()
    rl = nsdav.RateLimiter(0.2, clock=c.now, sleep=c.sleep)
    rl.wait()
    c.t += 5.0
    rl.wait()
    assert c.slept == []


def test_zero_gap_disables_throttling():
    c = FakeClock()
    rl = nsdav.RateLimiter(0.0, clock=c.now, sleep=c.sleep)
    rl.wait()
    rl.wait()
    assert c.slept == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_ratelimit.py -v`
Expected: FAIL —— `AttributeError: module 'nsdav' has no attribute 'RateLimiter'`

- [ ] **Step 3: 实现**

```python
# ────────────────────────────── 限流器 ──────────────────────────────

class RateLimiter:
    """串行 + 最小间隔。单线程 CLI，不需要锁。"""

    def __init__(
        self,
        min_gap: float = DEFAULT_MIN_GAP,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_gap = max(0.0, min_gap)
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None and self.min_gap > 0:
            delta = self._clock() - self._last
            if delta < self.min_gap:
                self._sleep(self.min_gap - delta)
        self._last = self._clock()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_ratelimit.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add nsdav.py tests/test_ratelimit.py
git commit -m "feat(core): 请求限流器"
```

---

### Task 6: 进程内 mock WebDAV 服务器

**Files:**
- Create: `tests/mock_dav.py`, `tests/test_mock_sanity.py`

**Interfaces:**
- Consumes: 无（测试基础设施，不依赖 nsdav）
- Produces:
  - `class MockDAV`，构造参数 `page_size: int | None = None`、`fail_first_n: int = 0`、`fail_status: int = 503`、`latency: float = 0.0`
  - `.start() -> str` 返回 `http://127.0.0.1:<port>`；`.stop() -> None`
  - `.store: dict[str, bytes]` 路径 → 内容；`.dirs: set[str]`
  - `.requests: list[tuple[str, str]]` 收到的 (方法, target)
  - `.base_path` 固定为 `/dav`

**为什么：** 这是回归网。分页、503 重试、404、Range 这些行为只有在服务器
**故意做错事**的时候才测得到。后面所有 Transport / WebDAV 测试都依赖它。

- [ ] **Step 1: 写 mock 服务器**

`tests/mock_dav.py`:

```python
"""进程内假 WebDAV 服务器，用于制造分页、503、404、Range 等场景。"""
from __future__ import annotations

import base64
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

BASE_PATH = "/dav"


class MockDAV:
    def __init__(self, *, page_size=None, fail_first_n=0, fail_status=503,
                 latency=0.0, user="u", password="p"):
        self.page_size = page_size
        self._fail_first_n = fail_first_n
        self.fail_status = fail_status
        self.latency = latency
        self.user = user
        self.password = password
        self.store: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self.requests: list[tuple[str, str]] = []
        self._failures_left = fail_first_n
        self._srv = None
        self._thread = None

    # `fail_status`、`page_size`、`latency` 都是每次请求现读的，只有这一个
    # 在 __init__ 里被抄进 _failures_left。于是 `start()` 之后再写
    # `s.fail_first_n = 1`（Task 7 的 test_stream_body_retry_reopens_file
    # 就是这么写的）被静默忽略：不注入 503，那条用例首次请求就成功，
    # 永远不会碰到它名字里那个重试——StreamBody 重试时重开文件这件事
    # 也就没人看。做成属性，设值时一并重置计数器。
    @property
    def fail_first_n(self):
        return self._fail_first_n

    @fail_first_n.setter
    def fail_first_n(self, n):
        self._fail_first_n = n
        self._failures_left = n

    # ── 生命周期 ──
    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):   # 静音
                pass

            def _dispatch(self, method):
                outer.requests.append((method, self.path))
                if outer.latency:
                    time.sleep(outer.latency)
                # 无论走哪条分支，都必须先把请求体读干净，否则 body 留在
                # socket 里，keep-alive 的下一个请求会读到脏数据。
                body = self._read_body()
                if not outer._check_auth(self.headers.get("Authorization")):
                    self._simple(401)
                    return
                if outer._failures_left > 0:
                    outer._failures_left -= 1
                    self._simple(outer.fail_status)
                    return
                getattr(self, f"_do_{method.lower()}", self._unsupported)(body)

            do_GET = lambda self: self._dispatch("GET")
            do_PUT = lambda self: self._dispatch("PUT")
            do_HEAD = lambda self: self._dispatch("HEAD")
            do_MKCOL = lambda self: self._dispatch("MKCOL")
            do_DELETE = lambda self: self._dispatch("DELETE")
            do_MOVE = lambda self: self._dispatch("MOVE")
            do_COPY = lambda self: self._dispatch("COPY")
            do_PROPFIND = lambda self: self._dispatch("PROPFIND")

            # ── 工具 ──
            def _simple(self, code, body=b"", extra=None):
                self.send_response(code)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _rel(self):
                p = unquote(urlsplit(self.path).path)
                assert p.startswith(BASE_PATH), p
                r = p[len(BASE_PATH):] or "/"
                return r if r.startswith("/") else "/" + r

            def _read_body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            # ── 方法实现 ──
            # 每个方法都收 body（已在 _dispatch 里读完），用不到的忽略。
            def _do_get(self, body):
                rel = self._rel()
                if rel not in outer.store:
                    self._simple(404)
                    return
                data = outer.store[rel]
                rng = self.headers.get("Range")
                if rng:
                    m = re.match(r"bytes=(\d+)-(\d*)", rng)
                    if m:
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else len(data) - 1
                        end = min(end, len(data) - 1)
                        chunk = data[start:end + 1]
                        self._simple(206, chunk, {
                            "Content-Range": f"bytes {start}-{end}/{len(data)}",
                            "Accept-Ranges": "bytes",
                        })
                        return
                self._simple(200, data, {"Accept-Ranges": "bytes"})

            def _do_head(self, body):
                # 刻意模仿坚果云：永远返回 Content-Length: 0
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _do_put(self, body):
                rel = self._rel()
                # 父集合不存在时必须 409（RFC 4918）。无条件回 201 的话，
                # WebDAV.put 里"撞 409 → mkdirs → 重试"那段永远走不到，
                # 而且 mock 会造出真实服务器不可能有的状态：这个文件 GET 得到，
                # 却不出现在任何一次列表里。真实服务器就是这里回 409，
                # put 的补建逻辑正是照它写的。
                parent = rel.rstrip("/").rsplit("/", 1)[0] or "/"
                if parent not in outer.dirs:
                    self._simple(409)
                    return
                existed = rel in outer.store
                outer.store[rel] = body
                self._simple(204 if existed else 201)

            def _do_mkcol(self, body):
                rel = self._rel().rstrip("/") or "/"
                if rel in outer.dirs:
                    self._simple(405)
                    return
                parent = rel.rsplit("/", 1)[0] or "/"
                if parent not in outer.dirs:
                    self._simple(409)
                    return
                outer.dirs.add(rel)
                self._simple(201)

            def _do_delete(self, body):
                rel = self._rel()
                depth = (self.headers.get("Depth") or "infinity").lower()
                if rel in outer.store:
                    del outer.store[rel]
                    self._simple(204)
                    return
                target = rel.rstrip("/") or "/"
                if target in outer.dirs:
                    if depth != "infinity":
                        self._simple(400)
                        return
                    # 根上 target + "/" 会拼出 "//"，一个子项都匹配不到，于是
                    # 删了根、里面的东西原封不动留下来——mock 自己造出一个
                    # "删了一半"的世界，正是这个项目最怕的形状。根是常驻的
                    # （`dirs` 初值就是 {"/"}），所以它自己留下，只清内容。
                    prefix = target if target == "/" else target + "/"
                    for k in [k for k in outer.store
                              if k == target or k.startswith(prefix)]:
                        del outer.store[k]
                    for d in [d for d in outer.dirs if d != "/"
                              and (d == target or d.startswith(prefix))]:
                        outer.dirs.discard(d)
                    self._simple(204)
                    return
                self._simple(404)

            def _do_move(self, body):
                src = self._rel()
                dest = unquote(urlsplit(self.headers.get("Destination", "")).path)
                if not dest.startswith(BASE_PATH):
                    self._simple(400)
                    return
                dst = dest[len(BASE_PATH):] or "/"
                if src not in outer.store:
                    self._simple(404)
                    return
                if dst in outer.store and self.headers.get("Overwrite", "T") == "F":
                    self._simple(412)
                    return
                outer.store[dst] = outer.store.pop(src)
                self._simple(201)

            def _do_copy(self, body):
                src = self._rel()
                dest = unquote(urlsplit(self.headers.get("Destination", "")).path)
                dst = dest[len(BASE_PATH):] or "/"
                if src not in outer.store:
                    self._simple(404)
                    return
                outer.store[dst] = outer.store[src]
                self._simple(201)

            def _do_propfind(self, body):
                rel = self._rel()
                depth = (self.headers.get("Depth") or "1").lower()
                if rel in outer.store:
                    items = [(rel, False)]
                elif (rel.rstrip("/") or "/") in outer.dirs:
                    base = rel.rstrip("/") or ""
                    items = [(rel if rel.endswith("/") else rel + "/", True)]
                    if depth != "0":
                        for d in sorted(outer.dirs):
                            if d != "/" and d.startswith(base + "/") \
                                    and "/" not in d[len(base) + 1:]:
                                items.append((d + "/", True))
                        for k in sorted(outer.store):
                            if k.startswith(base + "/") \
                                    and "/" not in k[len(base) + 1:]:
                                items.append((k, False))
                else:
                    self._simple(404)
                    return

                # 必须整体排序：marker 分页靠的是"取路径大于 mk 的那些"，
                # 若列表是"先目录后文件"而不是全局有序，翻页会漏条目。
                items.sort(key=lambda i: i[0])

                # 分页：marker 是上一页最后一项的路径
                q = urlsplit(self.path).query
                mk = None
                if q.startswith("mk="):
                    mk = unquote(q[3:])
                if mk is not None:
                    items = [i for i in items if i[0] > mk]

                link = None
                if outer.page_size and len(items) > outer.page_size:
                    page = items[:outer.page_size]
                    last = page[-1][0]
                    # 路径要和 href 用同一套编码。`rel` 是解码后的形式，直接拼进去
                    # 会发出一个带裸空格的非法 URI；中文目录名更糟——send_header
                    # 会抛 UnicodeEncodeError，客户端只看到 RemoteDisconnected。
                    # 而且真实服务器给的本来就是编码过的分页 URL，mock 若只发这一种
                    # 形式，客户端就只能自己再编一遍，正好落进本项目禁止的双编码。
                    href = BASE_PATH + quote(rel, safe="/")
                    link = (f"<http://{self.headers.get('Host', '127.0.0.1')}"
                            f"{href}?mk=" + quote(last, safe="") +
                            '>; rel="next"')
                    items = page

                body = self._build_multistatus(items)
                extra = {"Content-Type": "application/xml; charset=utf-8"}
                if link:
                    extra["Link"] = link
                self._simple(207, body, extra)

            def _build_multistatus(self, items):
                parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                         '<d:multistatus xmlns:d="DAV:">']
                for path, is_dir in items:
                    href = BASE_PATH + quote(path, safe="/")
                    rtype = "<d:resourcetype><d:collection/></d:resourcetype>" \
                        if is_dir else "<d:resourcetype/>"
                    size = 0 if is_dir else len(outer.store.get(path, b""))
                    parts.append(
                        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                        f"{rtype}<d:getcontentlength>{size}</d:getcontentlength>"
                        f"<d:getlastmodified>Mon, 21 Sep 2026 08:00:00 GMT"
                        f"</d:getlastmodified></d:prop>"
                        f"<d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
                        f"</d:response>")
                parts.append("</d:multistatus>")
                return "".join(parts).encode()

            def _unsupported(self, body):
                self._simple(501)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None

    def _check_auth(self, header: str | None) -> bool:
        if not header or not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode()
        except Exception:
            return False
        return raw == f"{self.user}:{self.password}"

    # ── 便捷填充 ──
    def add_dir(self, path: str) -> None:
        self.dirs.add(path.rstrip("/") or "/")

    def add_file(self, path: str, data: bytes) -> None:
        self.store[path] = data
```

注意 `quote` 需要从 `urllib.parse` 一并 import。

- [ ] **Step 2: 写自检测试**

`tests/test_mock_sanity.py`：

```python
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
```

- [ ] **Step 3: 跑测试**

Run: `python -m pytest tests/test_mock_sanity.py -v`
Expected: 全部 PASS。若 `test_mock_paginates_with_link_header` 失败，检查
`_do_propfind` 里 `items` 的排序与 `mk` 过滤是否一致。

- [ ] **Step 4: 提交**

```bash
git add tests/mock_dav.py tests/test_mock_sanity.py
git commit -m "test: 添加可注入故障的进程内 mock WebDAV 服务器"
```

---

### Task 7: Transport 层

**Files:**
- Modify: `nsdav.py`
- Test: `tests/test_transport.py`

**Interfaces:**
- Consumes: `RateLimiter`（T5）、`backoff_delay` / `RETRYABLE_STATUS`（T4）、异常（T1）
- Produces:
  - `class Response(NamedTuple): status: int; headers: dict[str, str]; body: bytes`（headers 键全小写）
  - `@dataclass class StreamBody: factory: Callable[[], BinaryIO]; length: int`
  - `class Transport(host, *, port=None, use_tls=True, user, password, ua=NS_UA, base_path=DEFAULT_BASE, min_gap=DEFAULT_MIN_GAP, max_retries=DEFAULT_MAX_RETRIES, timeout=DEFAULT_TIMEOUT, verbose=False, limiter=None, rand=random.random)`
  - `.request(method, target, *, body: bytes | StreamBody | None = None, headers=None, depth=None) -> Response` —— **`target` 必须是已编码的最终形式**
  - `@contextmanager .stream(method, target, *, headers=None) -> Iterator[Response]` —— body 为 `http.client.HTTPResponse`，供下载分块读
  - `.close()`、`.raise_for_status_or_raise(resp)`

**关键点：**
1. **连接复用**（实测 6 倍速度差）。连接失效时丢弃并重连一次。
2. **预置 Authorization 头**，不走 401 挑战 —— 在 200ms 限流下每次省一个往返。
3. `StreamBody` 的重试必须**重新调用 factory** 打开文件，否则第二次重试发出空 body。

- [ ] **Step 1: 写失败的测试**

`tests/test_transport.py`：

```python
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


def test_connection_is_reused(dav):
    s, base = dav
    t = _transport(base)
    for _ in range(3):
        t.request("PROPFIND", "/dav/", depth="0")
    assert t._conn is not None         # 请求完不关连接


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_transport.py -v`
Expected: FAIL —— `AttributeError: module 'nsdav' has no attribute 'Transport'`

- [ ] **Step 3: 实现**

```python
# ───────────────────────────── Transport ─────────────────────────────

class Response(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass
class StreamBody:
    """可重放的请求体。重试时必须重新调用 factory，否则第二次发出空 body。"""
    factory: Callable[[], BinaryIO]
    length: int


class Transport:
    """HTTP 传输层。不知道 WebDAV 的存在。

    target 参数必须是【已经编码好的最终形式】。本层不做任何路径编码 ——
    用户路径由 WebDAV 层经 enc_path 处理后传入，分页 URL 由 url_to_target
    处理后传入，两者在此汇合。
    """

    def __init__(
        self,
        host: str,
        *,
        port: int | None = None,
        use_tls: bool = True,
        user: str,
        password: str,
        ua: str = NS_UA,
        base_path: str = DEFAULT_BASE,
        min_gap: float = DEFAULT_MIN_GAP,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_TIMEOUT,
        verbose: bool = False,
        limiter: RateLimiter | None = None,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.base_path = base_path.rstrip("/") or ""
        self.ua = ua
        self.max_retries = max_retries
        self.timeout = timeout
        self.verbose = verbose
        self.limiter = limiter or RateLimiter(min_gap)
        self._rand = rand
        self._sleep = time.sleep
        self._conn: http.client.HTTPConnection | None = None
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = f"Basic {token}"

    # ── 连接管理 ──

    def _new_conn(self) -> http.client.HTTPConnection:
        if self.use_tls:
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout)
        return http.client.HTTPConnection(
            self.host, self.port, timeout=self.timeout)

    def _get_conn(self) -> http.client.HTTPConnection:
        if self._conn is None:
            self._conn = self._new_conn()
        return self._conn

    def _drop_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def close(self) -> None:
        self._drop_conn()

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  · {msg}", file=sys.stderr)

    # ── 头部 ──

    def _headers(self, extra, depth) -> dict[str, str]:
        h = {"Authorization": self._auth, "User-Agent": self.ua}
        if depth is not None:
            h["Depth"] = depth
        if extra:
            h.update(extra)
        return h

    # ── 重试循环 ──

    def _attempt(self, method, target, body, headers):
        """发一次请求。返回 (Response, None) 或 (None, 异常)。"""
        try:
            conn = self._get_conn()
            stream = None
            if isinstance(body, StreamBody):
                stream = body.factory()
            try:
                conn.request(method, target, body=stream if stream else body,
                             headers=headers)
                r = conn.getresponse()
                data = r.read()
            finally:
                if stream is not None:
                    stream.close()
            resp = Response(
                r.status,
                {k.lower(): v for k, v in r.getheaders()},
                data,
            )
            return resp, None
        except (http.client.HTTPException, OSError) as e:
            self._drop_conn()
            return None, e

    def _body_headers(self, headers, body):
        h = dict(headers)
        if isinstance(body, StreamBody):
            h["Content-Length"] = str(body.length)
        return h

    def request(self, method, target, *, body=None, headers=None,
                depth=None) -> Response:
        hdrs = self._body_headers(self._headers(headers, depth), body)
        attempt = 0
        while True:
            attempt += 1
            self.limiter.wait()
            resp, exc = self._attempt(method, target, body, hdrs)

            if exc is not None:
                if attempt > self.max_retries:
                    raise NetworkError(
                        f"{method} {target} 失败: {exc}") from exc
                d = backoff_delay(attempt, None, rand=self._rand)
                self._log(f"连接错误({exc})，{d:.1f}s 后重试 "
                          f"{attempt}/{self.max_retries}")
                self._sleep(d)
                continue

            if resp.status in RETRYABLE_STATUS and attempt <= self.max_retries:
                d = backoff_delay(attempt, resp.status, rand=self._rand)
                self._log(f"HTTP {resp.status}，{d:.1f}s 后重试 "
                          f"{attempt}/{self.max_retries}")
                self._sleep(d)
                continue
            return resp

    @contextmanager
    def stream(self, method, target, *, headers=None, depth=None):
        """流式读响应体。不做重试 —— 分块下载由上层负责续传。"""
        self.limiter.wait()
        conn = self._get_conn()
        hdrs = self._headers(headers, depth)
        try:
            conn.request(method, target, headers=hdrs)
            r = conn.getresponse()
        except (http.client.HTTPException, OSError) as e:
            self._drop_conn()
            raise NetworkError(f"{method} {target} 失败: {e}") from e
        try:
            yield Response(r.status,
                           {k.lower(): v for k, v in r.getheaders()},
                           r)          # type: ignore[arg-type]
        finally:
            try:
                r.read()            # 读完，连接才能复用
            except Exception:
                self._drop_conn()

    # ── 状态码归一 ──

    @staticmethod
    def raise_for_status_or_raise(resp: Response) -> Response:
        if resp.status < 400:
            return resp
        snippet = resp.body[:200].decode("utf-8", "replace")
        if resp.status == 401:
            raise AuthError(f"认证失败 (401)。请检查账号与应用密码。{snippet}")
        if resp.status == 403:
            raise NsdavError(f"没有权限 (403)。{snippet}")
        if resp.status in (404, 410):
            raise NotFoundError(f"路径不存在 ({resp.status})。{snippet}")
        if resp.status == 429:
            raise RateLimitError(f"被限流 (429)。调大 --min-gap 重试。{snippet}")
        raise NsdavError(f"HTTP {resp.status}。{snippet}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_transport.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add nsdav.py tests/test_transport.py
git commit -m "feat(core): HTTP 传输层，含连接复用与退避重试"
```

---

### Task 8: WebDAV 操作层（含分页循环）

**Files:**
- Modify: `nsdav.py`
- Test: `tests/test_webdav.py`

**Interfaces:**
- Consumes: `Transport`（T7）、`parse_multistatus`（T3）、`parse_next_link` / `url_to_target`（T2）、`enc_path` / `normalize_remote_path`（T1）
- Produces:
  - `class WebDAV(transport, *, base_path=DEFAULT_BASE)`
  - `.target(rel_path: str) -> str` —— 编码后的最终 target
  - `.propfind(rel_path: str, depth: str = "1") -> list[Entry]` —— **含分页循环**
  - `.stat(rel_path) -> Entry`、`.exists(rel_path) -> bool`、`.listdir(rel_path) -> list[Entry]`
  - `.read(rel_path) -> bytes`、`.put(rel_path, data: bytes | StreamBody)`、`.mkcol(rel_path)`、`.mkdirs(rel_path)`
  - `.delete(rel_path, recursive=False)`、`.move(src, dst)`、`.copy(src, dst)`
  - `.walk(rel_path, max_depth=0) -> Iterator[Entry]`

**关键点：** 分页循环里，第一页用 `self.target()`（要编码），后续页用
`url_to_target()`（**已编码，不得再编码**）。这是最容易写错的地方。

- [ ] **Step 1: 写失败的测试**

`tests/test_webdav.py`：

```python
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
    return nsdav.WebDAV(t)


def test_listdir_excludes_self(dav):
    s, base = dav
    d = _dav(s, base)
    d.mkcol("/a")
    d.put("/a/one.txt", b"1")
    d.put("/a/two.txt", b"22")
    names = sorted(e.name for e in d.listdir("/a"))
    assert names == ["one.txt", "two.txt"]


def test_listdir_follows_pagination():
    s = MockDAV(page_size=3); base = s.start()
    try:
        d = _dav(s, base)
        d.mkcol("/big")
        for i in range(10):
            d.put(f"/big/f{i:02d}.txt", b"x")
        names = sorted(e.name for e in d.listdir("/big"))
        assert len(names) == 10            # 不跟随分页只会拿到 3 个
        assert names[0] == "f00.txt" and names[-1] == "f09.txt"
    finally:
        s.stop()


def test_pagination_does_not_double_encode_special_names():
    """目录名含空格时分页必须仍然正确 —— 这是双编码 bug 的回归测试。

    只断言"拿到 7 个名字"是个**代理**：它确实会被双编码打破（mock 存的路径
    是未编码的，双编码的 marker 解码后变成 `%20` 字面量，按 `%`(0x25) >
    ` `(0x20) 排序，所有条目都落在 marker 之前而被丢掉，于是只剩 3 个），但
    失败信息只说"少了几条"，不说为什么。所以这里直接把**机制**钉住：
    第二次 PROPFIND 的 target 必须单编码。`%20` 与 `%2520` 那两行就是
    红线本身，双编码一出现就以自己的名字失败。
    """
    s = MockDAV(page_size=3); base = s.start()
    try:
        d = _dav(s, base)
        d.mkcol("/my dir")
        for i in range(7):
            d.put(f"/my dir/f{i}.txt", b"x")
        names = sorted(e.name for e in d.listdir("/my dir"))
        assert len(names) == 7

        propfinds = [t for m, t in s.requests if m == "PROPFIND"]
        assert len(propfinds) >= 2, "page_size=3 / 7 个文件，必须真的翻页"
        assert "%20" in propfinds[1], "第二页的 marker 丢了单编码"
        assert "%2520" not in propfinds[1], "marker 被二次编码了"
    finally:
        s.stop()


def test_stat_file_and_dir(dav):
    s, base = dav
    d = _dav(s, base)
    d.put("/x.txt", b"hello")
    e = d.stat("/x.txt")
    assert e.size == 5 and not e.is_dir and e.name == "x.txt"
    assert d.stat("/").is_dir


def test_exists(dav):
    s, base = dav
    d = _dav(s, base)
    assert not d.exists("/nope.txt")
    d.put("/y.txt", b"z")
    assert d.exists("/y.txt")


def test_read(dav):
    s, base = dav
    d = _dav(s, base)
    d.put("/r.txt", b"content here")
    assert d.read("/r.txt") == b"content here"


def test_mkdirs_creates_parents(dav):
    s, base = dav
    d = _dav(s, base)
    d.mkdirs("/p/q/r")
    assert d.exists("/p") and d.exists("/p/q") and d.exists("/p/q/r")


def test_put_creates_parents_on_conflict(dav):
    """PUT 撞 409 之后要**真的**把父目录建出来。

    这条原来只断言 `d.read(...) == b"v"`——而 mock 当初无条件回 201、无条件
    把内容存进 store，所以 `mkdirs` 那段一次都没跑过，这条照样绿。名字里写的
    是"on_conflict"，断言里却没有任何东西依赖那个冲突发生过。必须断言父目录
    **存在**，那才是被检验的行为。

    `assert not d.exists("/deep")` 是前置条件，不是凑数：它保证后面的 409
    确实是撞出来的，而不是这颗树碰巧已经在了。
    """
    s, base = dav
    d = _dav(s, base)
    assert not d.exists("/deep")
    d.put("/deep/nested/f.txt", b"v")
    assert d.read("/deep/nested/f.txt") == b"v"
    assert d.stat("/deep").is_dir
    assert d.stat("/deep/nested").is_dir


def test_delete_recursive(dav):
    s, base = dav
    d = _dav(s, base)
    d.mkcol("/t"); d.put("/t/a.txt", b"1"); d.put("/t/sub/b.txt", b"2")
    d.delete("/t", recursive=True)
    assert not d.exists("/t")


def test_move_and_copy(dav):
    s, base = dav
    d = _dav(s, base)
    d.put("/m.txt", b"data")
    d.move("/m.txt", "/n.txt")
    assert not d.exists("/m.txt") and d.read("/n.txt") == b"data"
    d.copy("/n.txt", "/o.txt")
    assert d.read("/n.txt") == b"data" and d.read("/o.txt") == b"data"


def test_stat_missing_raises(dav):
    s, base = dav
    d = _dav(s, base)
    with pytest.raises(nsdav.NotFoundError):
        d.stat("/absent.txt")


def test_walk_depth_limited(dav):
    s, base = dav
    d = _dav(s, base)
    d.mkcol("/w"); d.mkcol("/w/sub"); d.put("/w/f.txt", b"1")
    d.put("/w/sub/g.txt", b"2")
    shallow = [e.path for e in d.walk("/w", max_depth=1)]
    assert "/w/f.txt" in shallow and "/w/sub/" in shallow
    assert "/w/sub/g.txt" not in shallow
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_webdav.py -v`
Expected: FAIL —— `AttributeError: module 'nsdav' has no attribute 'WebDAV'`

- [ ] **Step 3: 实现**

```python
# ────────────────────────── WebDAV 操作层 ──────────────────────────

class WebDAV:
    """协议层。不知道有命令行这回事。"""

    def __init__(self, transport: Transport,
                 *, base_path: str = DEFAULT_BASE) -> None:
        self.t = transport
        self.base_path = base_path.rstrip("/") or ""

    # ── 路径 ──

    def target(self, rel_path: str) -> str:
        """相对路径 → 已编码的最终 target。用于用户输入的路径。

        只处理用户路径。分页的下一页 URL 不走这里 —— 它走
        url_to_target()，因为那边已经是编码过的。
        """
        rel = normalize_remote_path(rel_path)
        return enc_path(self.base_path + rel)

    def _expect(self, resp) -> Response:
        return self.t.raise_for_status_or_raise(resp)

    # ── 读 ──

    def propfind(self, rel_path: str, depth: str = "1") -> list[Entry]:
        """PROPFIND，跟随 Link 分页到底。

        第一页的 target 由 self.target() 编码；后续页来自 Link 头，
        其 path 与 query 都已编码，必须原样透传，绝不能再过 enc_path。
        """
        entries: list[Entry] = []
        target = self.target(rel_path)
        page = 0
        while True:
            page += 1
            resp = self._expect(self.t.request(
                "PROPFIND", target, body=PROPFIND_BODY,
                headers={"Content-Type": "application/xml"}, depth=depth))
            entries.extend(parse_multistatus(resp.body, self.base_path))

            nxt = parse_next_link(resp.headers.get("link"))
            if not nxt:
                break
            target = url_to_target(nxt)     # 已编码，直接透传
            if page > 10000:
                raise NsdavError("分页层数异常，疑似服务器返回了循环的 Link 头")
        return entries

    def stat(self, rel_path: str) -> Entry:
        entries = self.propfind(rel_path, depth="0")
        if not entries:
            raise NotFoundError(f"路径不存在: {rel_path}")
        return entries[0]

    def exists(self, rel_path: str) -> bool:
        try:
            self.stat(rel_path)
            return True
        except NotFoundError:
            return False

    def listdir(self, rel_path: str) -> list[Entry]:
        want = normalize_remote_path(rel_path).rstrip("/") + "/"
        entries = self.propfind(rel_path, depth="1")
        out = []
        for e in entries:
            if e.path.rstrip("/") + ("/" if e.is_dir else "") == want:
                continue            # 自身
            out.append(e)
        return out

    def read(self, rel_path: str) -> bytes:
        resp = self._expect(self.t.request("GET", self.target(rel_path)))
        return resp.body

    def walk(self, rel_path: str, max_depth: int = 0) -> Iterator[Entry]:
        """广度优先递归。max_depth=0 表示不限深度。"""
        queue = [(normalize_remote_path(rel_path), 1)]
        while queue:
            cur, depth = queue.pop(0)
            for e in self.listdir(cur):
                yield e
                if e.is_dir and (max_depth == 0 or depth < max_depth):
                    queue.append((e.path.rstrip("/"), depth + 1))

    # ── 写 ──

    def put(self, rel_path: str, data: bytes | StreamBody) -> None:
        """写入。父目录不存在时自动补建后重试一次。"""
        target = self.target(rel_path)
        resp = self.t.request("PUT", target, body=data)
        if resp.status == 409:
            parent = normalize_remote_path(rel_path).rsplit("/", 1)[0]
            self.mkdirs(parent)
            resp = self.t.request("PUT", target, body=data)
        self._expect(resp)

    def mkcol(self, rel_path: str) -> None:
        resp = self.t.request("MKCOL", self.target(rel_path))
        if resp.status in (201, 204, 405):      # 405 = 已存在，视为成功
            return
        self._expect(resp)

    def mkdirs(self, rel_path: str) -> None:
        rel = normalize_remote_path(rel_path)
        parts = [p for p in rel.split("/") if p]
        for i in range(1, len(parts) + 1):
            self.mkcol("/" + "/".join(parts[:i]))

    def delete(self, rel_path: str, recursive: bool = False) -> None:
        headers = {"Depth": "infinity"} if recursive else None
        self._expect(self.t.request(
            "DELETE", self.target(rel_path), headers=headers))

    def _destination(self, rel_path: str) -> str:
        scheme = "https" if self.t.use_tls else "http"
        port = f":{self.t.port}" if self.t.port else ""
        return f"{scheme}://{self.t.host}{port}{self.target(rel_path)}"

    def move(self, src: str, dst: str, overwrite: bool = True) -> None:
        self._expect(self.t.request("MOVE", self.target(src), headers={
            "Destination": self._destination(dst),
            "Overwrite": "T" if overwrite else "F",
        }))

    def copy(self, src: str, dst: str, overwrite: bool = True) -> None:
        self._expect(self.t.request("COPY", self.target(src), headers={
            "Destination": self._destination(dst),
            "Overwrite": "T" if overwrite else "F",
        }))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_webdav.py -v`
Expected: 全部 PASS。`test_listdir_follows_pagination` 与
`test_pagination_does_not_double_encode_special_names` 是两条最关键的红线。

- [ ] **Step 5: 提交**

```bash
git add nsdav.py tests/test_webdav.py
git commit -m "feat(webdav): 操作层，含分页跟随与自动建父目录"
```

---

### Task 9: 下载（分块、续传、原子改名）

**Files:**
- Modify: `nsdav.py`
- Test: `tests/test_transfer.py`

**Interfaces:**
- Consumes: `WebDAV`（T8）、`Transport.stream`（T7）
- Produces: `download(dav, remote_path, local_path, *, chunk=DOWNLOAD_CHUNK, progress=None, transport=None) -> tuple[str, int]`

**关键点：**
- 小于一个 chunk 的文件**一次 GET 拉完**，不分块（分块只为大文件省内存）。
- 写 `<目标>.part`，全部到齐后 `os.replace` 原子改名 —— 中途失败不会留下半个正式文件。
- 已有 `.part` 时从断点续；`.part` 比远端大则说明是旧残留，删掉重下。
- 服务端对 Range 请求返回 `200`（而非 `206`）时，说明不支持 Range，必须从头来。

- [ ] **Step 1: 写失败的测试**

`tests/test_transfer.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_transfer.py -v`
Expected: FAIL —— `AttributeError: module 'nsdav' has no attribute 'download'`

- [ ] **Step 3: 实现**

```python
# ─────────────────────────────── 下载 ───────────────────────────────

def download(dav, remote_path, local_path, *, chunk=DOWNLOAD_CHUNK,
             progress=None, transport=None) -> tuple[str, int]:
    """把远端文件下到本地。

    写 <目标>.part，全部就绪后原子改名。已有 .part 时断点续传。
    返回 (本地路径, 字节数)。
    """
    entry = dav.stat(remote_path)
    if entry.is_dir:
        raise NsdavError(f"{remote_path} 是目录，不能下载")
    total = entry.size
    part = local_path + ".part"
    t = transport or dav.t

    def report(done):
        if progress:
            progress(done, total)

    if total == 0:
        with open(part, "wb"):
            pass
        os.replace(part, local_path)
        report(0)
        return local_path, 0

    offset = 0
    if os.path.exists(part):
        have = os.path.getsize(part)
        if have > total:
            os.remove(part)              # 旧残留，重下
        elif have == total:
            os.replace(part, local_path)
            return local_path, total
        else:
            offset = have

    # 小文件一次拉完，分块只为大文件省内存
    step = total if chunk <= 0 or total <= chunk else chunk

    with open(part, "ab" if offset else "wb") as f:
        while offset < total:
            end = min(offset + step - 1, total - 1)
            target = dav.target(remote_path)
            with t.stream("GET", target,
                          headers={"Range": f"bytes={offset}-{end}"}) as resp:
                if resp.status == 200:
                    # 服务端忽略 Range，从头返回：丢掉已有进度重来
                    if offset:
                        f.seek(0)
                        f.truncate(0)
                        offset = 0
                    f.write(resp.body.read())
                    break
                if resp.status == 206:
                    got = 0
                    while True:
                        buf = resp.body.read(65536)
                        if not buf:
                            break
                        f.write(buf)
                        got += len(buf)
                    offset += got
                    report(offset)
                    if got == 0:
                        raise NsdavError(
                            f"下载中断：{remote_path} 在第 {offset} 字节处卡住")
                    continue
                raise NsdavError(
                    f"下载 {remote_path} 失败：HTTP {resp.status}")

    if os.path.getsize(part) != total:
        raise NsdavError(
            f"下载不完整：{remote_path} 期望 {total} 字节，"
            f"实际 {os.path.getsize(part)} 字节。留下 {part} 以便续传。")
    os.replace(part, local_path)
    return local_path, total
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_transfer.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add nsdav.py tests/test_transfer.py
git commit -m "feat(transfer): 分块下载与断点续传"
```

---

### Task 10: 上传（流式 + 校验）

**Files:**
- Modify: `nsdav.py`
- Test: `tests/test_transfer.py`（追加）

**Interfaces:**
- Consumes: `WebDAV.put`（T8）、`StreamBody`（T7）
- Produces: `upload(dav, local_path, remote_path, *, verify="size", progress=None) -> int`

**关键点：**
- 用 `StreamBody` 流式上传，**不把整个文件读进内存**（iSH 内存吃紧）。
- `StreamBody` 的 factory 让重试能重新打开文件（T7 已测）。
- 传完用 **PROPFIND** 核对大小 —— 不是 HEAD（坚果云 HEAD 恒返回 0）。
- `verify="strong"` 时读回末尾 64 字节比对。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_transfer.py`：

```python
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
    s, base = dav
    d, t = _dav(s, base)
    src = tmp_path / "u.txt"
    payload = b"abcdefghij" * 10
    src.write_bytes(payload)

    # 让 PUT 写完就篡改内容，长度不变 —— 只有 strong 校验能发现
    real_put = d.put
    def corrupting_put(p, data):
        real_put(p, data)
        s.store[p] = b"XXXXXXXXXX" + payload[10:]
    d.put = corrupting_put

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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_transfer.py -v -k upload`
Expected: FAIL —— `AttributeError: module 'nsdav' has no attribute 'upload'`

- [ ] **Step 3: 实现**

```python
# ─────────────────────────────── 上传 ───────────────────────────────

VERIFY_TAIL_BYTES = 64


def upload(dav, local_path, remote_path, *, verify="size",
           progress=None) -> int:
    """把本地文件传上去，传完校验。

    verify='size'   核对远端大小（默认，省一次请求）
    verify='strong' 再读回末尾若干字节比对内容
    """
    if not os.path.isfile(local_path):
        raise UsageError(f"本地文件不存在: {local_path}")
    size = os.path.getsize(local_path)

    def factory():
        return open(local_path, "rb")

    dav.put(remote_path, StreamBody(factory, size))

    entry = dav.stat(remote_path)
    if entry.size != size:
        raise NsdavError(
            f"上传后大小不符：{remote_path} 期望 {size}，远端 {entry.size}")

    if verify == "strong" and size > 0:
        tail = min(VERIFY_TAIL_BYTES, size)
        start = size - tail
        with open(local_path, "rb") as f:
            f.seek(start)
            local_tail = f.read()
        target = dav.target(remote_path)
        with dav.t.stream("GET", target,
                          headers={"Range": f"bytes={start}-{size - 1}"}) as r:
            if r.status not in (200, 206):
                raise NsdavError(f"回读校验失败：HTTP {r.status}")
            remote_tail = r.body.read()
        if remote_tail[-tail:] != local_tail:
            raise NsdavError(
                f"上传内容校验失败：{remote_path} 末尾字节与本地不一致")

    if progress:
        progress(size, size)
    return size
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_transfer.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add nsdav.py tests/test_transfer.py
git commit -m "feat(transfer): 流式上传与大小/内容校验"
```

---

### Task 11: 配置解析

**Files:**
- Modify: `nsdav.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 异常（T1）
- Produces:
  - `@dataclass class Config(host, base_path, user, password, min_gap, max_retries, timeout, device_dir=None)`
  - `load_config(args, env: Mapping[str, str] = os.environ) -> Config`
  - `config_file_path() -> str`
  - 环境变量 `NSDAV_WEBDAV_URL` / `NSDAV_WEBDAV_USER` / `NSDAV_WEBDAV_PASSWORD`

**优先级（高 → 低）：** 命令行参数 → 环境变量 → 配置文件 → 内置默认。
缺账号或密码时报 `AuthError`，消息里要说清楚三种设置途径。

- [ ] **Step 1: 写失败的测试**

`tests/test_config.py`：

```python
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
    c = nsdav.load_config(Args(), env={}, config={
        "url": "https://dav.jianguoyun.com/dav",
        "user": "file@x.com", "password": "filepw",
    })
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


def test_min_gap_and_retries_overridable():
    env = {"NSDAV_WEBDAV_USER": "u", "NSDAV_WEBDAV_PASSWORD": "p"}
    c = nsdav.load_config(
        Args(min_gap=1.5, max_retries=9, timeout=300), env=env, config={})
    assert c.min_gap == 1.5 and c.max_retries == 9 and c.timeout == 300
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL —— `AttributeError: module 'nsdav' has no attribute 'load_config'`

- [ ] **Step 3: 实现**

```python
# ─────────────────────────────── 配置 ───────────────────────────────

CONFIG_ENV_URL = "NSDAV_WEBDAV_URL"
CONFIG_ENV_USER = "NSDAV_WEBDAV_USER"
CONFIG_ENV_PASSWORD = "NSDAV_WEBDAV_PASSWORD"


@dataclass
class Config:
    host: str
    base_path: str
    user: str
    password: str
    min_gap: float = DEFAULT_MIN_GAP
    max_retries: int = DEFAULT_MAX_RETRIES
    timeout: float = DEFAULT_TIMEOUT
    port: int | None = None
    use_tls: bool = True


def config_file_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "nsdav", "config.toml")


def _read_config_file(path: str | None = None) -> dict[str, str]:
    path = path or config_file_path()
    if not os.path.isfile(path):
        return {}
    try:
        st = os.stat(path)
        if hasattr(os, "getuid") and st.st_mode & 0o077:
            print(f"警告: {path} 权限过宽，建议 chmod 600", file=sys.stderr)
    except OSError:
        pass
    try:
        with open(path, "rb") as f:
            if sys.version_info >= (3, 11):
                import tomllib
                return {k: str(v) for k, v in tomllib.load(f).items()}
    except Exception as e:
        print(f"警告: 读取 {path} 失败: {e}", file=sys.stderr)
    return {}


def _split_url(url: str) -> tuple[str, int | None, bool, str]:
    sp = urlsplit(url if "://" in url else "https://" + url)
    if not sp.hostname:
        raise UsageError(f"无法解析 WebDAV 地址: {url}")
    return (sp.hostname, sp.port, sp.scheme != "http",
            sp.path.rstrip("/") or DEFAULT_BASE)


def load_config(args, *, env=None, config=None) -> Config:
    env = os.environ if env is None else env
    config = _read_config_file() if config is None else config

    def pick(cli, env_key, cfg_key, default):
        if cli not in (None, ""):
            return cli
        if env.get(env_key):
            return env[env_key]
        if config.get(cfg_key):
            return config[cfg_key]
        return default

    url = pick(getattr(args, "url", None), CONFIG_ENV_URL, "url",
               f"https://{DEFAULT_HOST}{DEFAULT_BASE}")
    host, port, use_tls, base_path = _split_url(url)

    user = pick(getattr(args, "user", None), CONFIG_ENV_USER, "user", None)
    password = pick(getattr(args, "password", None),
                    CONFIG_ENV_PASSWORD, "password", None)
    if not user or not password:
        raise AuthError(
            "缺少账号或密码。三种设置方式：\n"
            f"  1. 环境变量 {CONFIG_ENV_USER} / {CONFIG_ENV_PASSWORD}\n"
            "  2. 命令行参数 --user / --password\n"
            f"  3. 配置文件 {config_file_path()}\n"
            "密码是坚果云「账户信息 → 安全选项 → 添加应用密码」生成的，"
            "不是登录密码。")

    return Config(
        host=host, port=port, use_tls=use_tls, base_path=base_path,
        user=user, password=password,
        min_gap=_number(pick(getattr(args, "min_gap", None), "NSDAV_MIN_GAP",
                             "min_gap", DEFAULT_MIN_GAP), float, "min_gap"),
        max_retries=_number(pick(getattr(args, "max_retries", None),
                                 "NSDAV_MAX_RETRIES", "max_retries",
                                 DEFAULT_MAX_RETRIES), int, "max_retries"),
        timeout=_number(pick(getattr(args, "timeout", None), "NSDAV_TIMEOUT",
                             "timeout", DEFAULT_TIMEOUT), float, "timeout"),
    )
```

`_number` 处理配置文件那边读出来的字符串：

```python
def _number(value, cast, name: str):
    """配置文件里数值也是字符串，统一在这里转，并给出可读的报错。"""
    try:
        return cast(value)
    except (TypeError, ValueError) as e:
        raise UsageError(f"配置项 {name} 不是合法数值: {value!r}") from e
```

在 `tests/test_config.py` 里补一条覆盖配置文件路径的用例：

```python
def test_config_file_numbers_are_coerced():
    env = {"NSDAV_WEBDAV_USER": "u", "NSDAV_WEBDAV_PASSWORD": "p"}
    c = nsdav.load_config(Args(), env=env, config={
        "min_gap": "1.5", "max_retries": "9", "timeout": "300"})
    assert c.min_gap == 1.5 and c.max_retries == 9 and c.timeout == 300


def test_bad_config_number_raises_usage_error():
    env = {"NSDAV_WEBDAV_USER": "u", "NSDAV_WEBDAV_PASSWORD": "p"}
    with pytest.raises(nsdav.UsageError, match="min_gap"):
        nsdav.load_config(Args(), env=env, config={"min_gap": "abc"})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_config.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add nsdav.py tests/test_config.py
git commit -m "feat(config): 参数/环境变量/配置文件三级优先级"
```

---

### Task 12: CLI 命令与 main

**Files:**
- Modify: `nsdav.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: 前面全部
- Produces: `build_parser() -> argparse.ArgumentParser`、`cmd_*` 函数、`main(argv=None) -> int`、`format_size(n) -> str`

**约定：**
- 人类可读输出到 stdout；`--json` 时输出 JSON。
- 进度与日志到 stderr。
- `rm -r` 不给 `-y` 时先列出待删项并要求确认。
- `main()` 捕获 `NsdavError`，打印到 stderr，返回 `exc.exit_code`。

- [ ] **Step 1: 写失败的测试**

`tests/test_cli.py`：

```python
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


def test_mv_and_cp(live_dav, capsys):
    assert run(capsys, "cp", "/d/one.txt", "/d/one-copy.txt")[0] == 0
    assert live_dav.store["/d/one-copy.txt"] == b"1"
    assert run(capsys, "mv", "/d/one-copy.txt", "/d/moved.txt")[0] == 0
    assert "/d/moved.txt" in live_dav.store


def test_rm_recursive_without_yes_asks_and_aborts(live_dav, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    code, out, _ = run(capsys, "rm", "-r", "/d")
    assert code != 0
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL —— `AttributeError: module 'nsdav' has no attribute 'main'`

- [ ] **Step 3: 实现命令行层**

```python
# ────────────────────────────── 命令行层 ──────────────────────────────

def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PiB"


def _entry_dict(e: Entry) -> dict[str, Any]:
    return {"path": e.path, "name": e.name, "is_dir": e.is_dir,
            "size": e.size, "mtime": e.mtime}


def _print_entries(entries, as_json: bool) -> None:
    if as_json:
        print(json.dumps([_entry_dict(e) for e in entries],
                         ensure_ascii=False, indent=2))
        return
    for e in entries:
        if e.is_dir:
            print(f"  {'<dir>':>10}  {e.name}/")
        else:
            print(f"  {format_size(e.size):>10}  {e.name}")


def _make_dav(cfg: Config) -> WebDAV:
    t = Transport(cfg.host, port=cfg.port, use_tls=cfg.use_tls,
                  user=cfg.user, password=cfg.password,
                  base_path=cfg.base_path, min_gap=cfg.min_gap,
                  max_retries=cfg.max_retries, timeout=cfg.timeout,
                  verbose=cfg_verbose())
    return WebDAV(t, base_path=cfg.base_path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nsdav", description="坚果云 WebDAV 命令行工具")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.add_argument("-v", "--verbose", action="store_true", help="打印请求日志")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只显示要做什么")
    p.add_argument("--url")
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--min-gap", type=float)
    p.add_argument("--max-retries", type=int)
    p.add_argument("--timeout", type=float)

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ls").add_argument("path", nargs="?", default="/")
    sub.add_parser("stat").add_argument("path")
    tp = sub.add_parser("tree")
    tp.add_argument("path", nargs="?", default="/")
    tp.add_argument("-d", "--depth", type=int, default=3)
    sub.add_parser("cat").add_argument("path")
    g = sub.add_parser("get")
    g.add_argument("remote"); g.add_argument("local", nargs="?")
    u = sub.add_parser("put")
    u.add_argument("local"); u.add_argument("remote", nargs="?")
    u.add_argument("--verify", choices=("size", "strong"), default="size")
    m = sub.add_parser("mkdir"); m.add_argument("path")
    m.add_argument("-p", "--parents", action="store_true")
    r = sub.add_parser("rm"); r.add_argument("path")
    r.add_argument("-r", "--recursive", action="store_true")
    r.add_argument("-y", "--yes", action="store_true")
    mv = sub.add_parser("mv"); mv.add_argument("src"); mv.add_argument("dst")
    cp = sub.add_parser("cp"); cp.add_argument("src"); cp.add_argument("dst")
    sub.add_parser("quota")
    return p


_VERBOSE = False


def cfg_verbose() -> bool:
    return _VERBOSE
```

命令函数按下列行为实现（每个都在 `main` 的 dispatch 表里注册）：

| 命令 | 行为 |
|---|---|
| `ls [path]` | `dav.listdir(path)` → `_print_entries` |
| `stat path` | `dav.stat(path)` → 一行或 JSON |
| `tree [path] -d N` | `dav.walk(path, max_depth=N)` → 缩进打印 |
| `cat path` | `sys.stdout.buffer.write(dav.read(path))` |
| `get remote [local]` | 无 local 时用 `basename`；调 `download()` |
| `put local [remote]` | 无 remote 时用 `basename(local)`；调 `upload()` |
| `mkdir path [-p]` | `-p` 走 `mkdirs`，否则 `mkcol` |
| `rm path [-r] [-y]` | `-r` 且无 `-y` 时先列目录并 `input()` 确认；`--dry-run` 只打印 |
| `mv src dst` | `dav.move` |
| `cp src dst` | `dav.copy` |
| `quota` | `propfind` 取 `quota-available-bytes`；拿不到就明确报"服务端不支持" |

```python
def main(argv: list[str] | None = None) -> int:
    global _VERBOSE
    args = build_parser().parse_args(argv)
    _VERBOSE = args.verbose and not args.quiet
    try:
        cfg = load_config(args)
        dav = _make_dav(cfg)
        try:
            return _dispatch(args, dav)
        finally:
            dav.t.close()
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except NsdavError as e:
        print(f"错误: {e}", file=sys.stderr)
        return e.exit_code
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_cli.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 全量测试**

Run: `python -m pytest -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add nsdav.py tests/test_cli.py
git commit -m "feat(cli): 命令分发与人类可读/JSON 双输出"
```

---

### Task 13: 实测测试

**Files:**
- Create: `tests/test_live.py`

**Interfaces:**
- Consumes: 全部
- Produces: 无（验证用）

**纪律：** 只在 `NSDAV_WEBDAV_USER` 等环境变量存在时运行；**只在
`/notes/nsdav-test/` 下操作**；测试结束必须清理干净，包括失败路径。
绝不能触碰 `nsdav-test` 之外的任何路径。

- [ ] **Step 1: 写实测测试**

`tests/test_live.py`：

```python
"""对真实坚果云账号的实测。默认跳过（pytest.ini 里 -m "not live"）。

运行方式：
    NSDAV_WEBDAV_USER=... NSDAV_WEBDAV_PASSWORD=... \
    python -m pytest tests/test_live.py -v -m live

只在 /notes/nsdav-test/ 下操作，测试结束自动清理。
"""
import os
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
        "min_gap": None, "max_retries": None, "timeout": None})())
    t = nsdav.Transport(cfg.host, port=cfg.port, use_tls=cfg.use_tls,
                        user=cfg.user, password=cfg.password,
                        base_path=cfg.base_path, min_gap=cfg.min_gap)
    d = nsdav.WebDAV(t, base_path=cfg.base_path)
    d.mkdirs(TEST_DIR)
    yield d
    try:
        d.delete(TEST_DIR, recursive=True)
    except nsdav.NsdavError:
        pass
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
    """
    sub = f"{TEST_DIR}/分页 目录"
    dav.mkdirs(sub)
    for i in range(760):
        dav.put(f"{sub}/p-{i:04d}.txt", b"x")
    names = [e.name for e in dav.listdir(sub)]
    paged = [n for n in names if n.startswith("p-")]
    assert len(paged) == 760


def test_08_mv_cp(dav):
    dav.put(f"{TEST_DIR}/a.txt", b"data")
    dav.move(f"{TEST_DIR}/a.txt", f"{TEST_DIR}/b.txt")
    assert not dav.exists(f"{TEST_DIR}/a.txt")
    dav.copy(f"{TEST_DIR}/b.txt", f"{TEST_DIR}/c.txt")
    assert dav.read(f"{TEST_DIR}/c.txt") == b"data"
```

- [ ] **Step 2: 跑实测（需要凭据）**

```bash
NSDAV_WEBDAV_USER="$NSDAV_WEBDAV_USER" \
NSDAV_WEBDAV_PASSWORD="$NSDAV_WEBDAV_PASSWORD" \
python -m pytest tests/test_live.py -v -m live
```

Expected: 全部 PASS。`test_07` 会跑几分钟（760 次 PUT，受 200ms 间隔限制）。

- [ ] **Step 3: 确认清理干净**

```bash
NSDAV_WEBDAV_USER="$NSDAV_WEBDAV_USER" NSDAV_WEBDAV_PASSWORD="$NSDAV_WEBDAV_PASSWORD" \
python nsdav.py ls /notes
```

Expected: 不应再有 `nsdav-test` 条目。

- [ ] **Step 4: 提交**

```bash
git add tests/test_live.py
git commit -m "test: 对真实坚果云的实测用例"
```

---

### Task 14: 文档与许可证

**Files:**
- Create: `README.md`, `AGENTS.md`, `LICENSE`, `.gitignore`

- [ ] **Step 1: README.md**

需覆盖：这是什么、为什么不用挂载、iSH 上怎么装（三段：`apk add python3`、
把 `nsdav.py` 弄进去、跑 `python3 nsdav.py ls /`）、完整命令表、
配置的三种方式、退出码表、以及**实测发现的服务器怪癖清单**
（HEAD 返回 0、404 而非 410、750 条分页、分页 URL 不可二次编码）。

- [ ] **Step 2: AGENTS.md**

比照常见 CLI 项目 AGENTS.md 的结构：Commands、Environment
variables、Dependencies（明写"仅标准库，不得引入第三方依赖"）、
Architecture（四层依赖关系 + 每层禁止知道什么）、Conventions、
Versioning（版本号单一来源是 `nsdav.py` 的 `__version__`）、
Commit message format（3Cs + Conventional Commits，scope 用 `core`/`webdav`/
`transfer`/`cli`/`config`/`test`/`docs`）。

- [ ] **Step 3: LICENSE**

AGPL-3.0 全文。

- [ ] **Step 4: .gitignore**

```
__pycache__/
*.pyc
.pytest_cache/
*.part
```

- [ ] **Step 5: 提交**

```bash
git add README.md AGENTS.md LICENSE .gitignore
git commit -m "docs: 添加 README、AGENTS.md 与 AGPL-3.0 许可证"
```

- [ ] **Step 6: 打标签**

```bash
git tag v0.1.0
```

---

## 自查记录

**规格覆盖检查：**

| 规格章节 | 对应任务 |
|---|---|
| §2 实测服务器行为（全部 12 条） | T1（编码）、T2（分页）、T3（XML/日期）、T6（mock 固化）、T7（HEAD/404）、T13（实测固化） |
| §3 单文件仅标准库 | 全局约束；T14 的 AGENTS.md 明写 |
| §4 四层架构 | T1–T3 基础、T4/T5 策略、T6 mock、T7 Transport、T8 WebDAV、T12 CLI |
| §5 命令表（11 条） | T12（含 `--dry-run`、`rm -r` 确认） |
| §5 全局参数（9 个） | T11（config）、T12（argparse） |
| §6 可靠性清单（9 条） | 1→T8、2→T5、3→T4+T7、4→T7、5→T13 断言、6→T9、7→T10、8→T12、9→T4/T7 |
| §7 配置三级优先级 | T11 |
| §8 三层测试 | T1–T5（单元）、T6（mock）、T13（实测） |
| §9 AGPL-3.0 | T14 |
| §10 交付物清单 | T14 + 各任务 |

**占位符扫描：** 无 TBD / TODO / "稍后补充"。T9 Step 5 显式要求清理
测试里两处占位断言，已给出替换代码。

**类型一致性：** `Entry`（T1 定义，T3/T8/T12 使用）字段一致；
`Response`（T7 定义，T8 使用）字段一致；`StreamBody`（T7 定义，T10 使用）
字段一致；`Transport.request` 的 `target` 语义（已编码）在 T7 定义、
T8 的两条调用路径（`self.target()` 与 `url_to_target()`）中均遵守。
`Config`（T11）字段与 `_make_dav`（T12）的构造参数逐一对齐。
