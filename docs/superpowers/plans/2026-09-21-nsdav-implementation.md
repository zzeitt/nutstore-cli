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
    ("/q/a?b.txt", "/q/a%3Fb.txt"),          # 未配对 '?' 属于路径
])
def test_enc_path_encodes_segments(raw, expected):
    assert nsdav.enc_path(raw) == expected


def test_enc_path_passes_query_through_untouched():
    # 分页 marker 已经是编码过的，再编一次会变成 %252F -> 服务端 400
    raw = "/dav/notes?mk=%2Fnotes%2Fa.txt"
    assert nsdav.enc_path(raw) == "/dav/notes?mk=%2Fnotes%2Fa.txt"


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
    """把未编码的路径编成可上线的形式。

    只处理路径部分；'?' 之后的 query 原样保留 —— 分页 marker 本身
    已经是编码过的，再编一次会变成 %252F，服务端会返回 400。
    """
    path, sep, query = raw.partition("?")
    encoded = "/".join(quote(seg, safe="") for seg in path.split("/"))
    return encoded + (sep + query if sep else "")


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
    assert e.name == "b.txt"


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
    """
    root = ET.fromstring(xml_bytes)
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

        if base and href.startswith(base):
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
与插件量级相当但能快速失败。

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


def test_backoff_is_capped():
    assert nsdav.backoff_delay(20, 503, rand=_no_jitter) == 60.0


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
    """第 attempt 次重试前该等多久（attempt 从 1 开始）。"""
    base = _BACKOFF_BASE_HARD if status in (429, 503) else _BACKOFF_BASE_SOFT
    raw = min(base * (2 ** (attempt - 1)), _BACKOFF_CAP)
    return raw * (1.0 + jitter * (2.0 * rand() - 1.0))
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
    c = FakeClock()
    rl = nsdav.RateLimiter(0.2, clock=c.now, sleep=c.sleep)
    rl.wait()
    c.t += 0.05
    rl.wait()
    assert c.slept == [0.2 - 0.05]


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
        self.fail_first_n = fail_first_n
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
                if not outer._check_auth(self.headers.get("Authorization")):
                    self._simple(401)
                    return
                if outer._failures_left > 0:
                    outer._failures_left -= 1
                    self._simple(outer.fail_status)
                    return
                getattr(self, f"_do_{method.lower()}", self._unsupported)()

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
            def _do_get(self):
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

            def _do_head(self):
                # 刻意模仿坚果云：永远返回 Content-Length: 0
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _do_put(self):
                rel = self._rel()
                body = self._read_body()
                existed = rel in outer.store
                outer.store[rel] = body
                self._simple(204 if existed else 201)

            def _do_mkcol(self):
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

            def _do_delete(self):
                rel = self._rel()
                depth = (self.headers.get("Depth") or "infinity").lower()
                if rel in outer.store:
                    del outer.store[rel]
                    self._simple(204)
                    return
                target = rel.rstrip("/")
                if target in outer.dirs:
                    if depth != "infinity":
                        self._simple(400)
                        return
                    for k in [k for k in outer.store
                              if k == target or k.startswith(target + "/")]:
                        del outer.store[k]
                    for d in [d for d in outer.dirs
                              if d == target or d.startswith(target + "/")]:
                        outer.dirs.discard(d)
                    self._simple(204)
                    return
                self._simple(404)

            def _do_move(self):
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

            def _do_copy(self):
                src = self._rel()
                dest = unquote(urlsplit(self.headers.get("Destination", "")).path)
                dst = dest[len(BASE_PATH):] or "/"
                if src not in outer.store:
                    self._simple(404)
                    return
                outer.store[dst] = outer.store[src]
                self._simple(201)

            def _do_propfind(self):
                rel = self._rel()
                depth = (self.headers.get("Depth") or "1").lower()
                if rel in outer.store:
                    items = [(rel, False)]
                elif rel.rstrip("/") in outer.dirs:
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
                    link = (f"<http://{self.headers.get('Host', '127.0.0.1')}"
                            f"{BASE_PATH}{rel}?mk=" + quote(last, safe="") +
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

            def _unsupported(self):
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
  - `.close()`、`.raise_for_status(resp)`

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


def _transport(base, **kw):
    from urllib.parse import urlsplit
    u = urlsplit(base)
    return nsdav.Transport(
        u.hostname, port=u.port, use_tls=False,
        user="u", password="p",
        min_gap=0, **kw,
    )


def test_sends_preemptive_basic_auth(dav):
    s, base = dav
    t = _transport(base)
    r = t.request("PROPFIND", "/dav/", depth="0")
    assert r.status == 207
    assert s.requests == [("PROPFIND", "/dav/")]


def test_bad_credentials_raise_auth_error(dav):
    s, base = dav
    t = _transport(base)
    t.password = "wrong"
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
    t = _transport(base, max_retries=5)
    t.password = "wrong"
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
    """目录名含空格时分页必须仍然正确 —— 这是双编码 bug 的回归测试。"""
    s = MockDAV(page_size=3); base = s.start()
    try:
        d = _dav(s, base)
        d.mkcol("/my dir")
        for i in range(7):
            d.put(f"/my dir/f{i}.txt", b"x")
        names = sorted(e.name for e in d.listdir("/my dir"))
        assert len(names) == 7
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
    s, base = dav
    d = _dav(s, base)
    d.put("/deep/nested/f.txt", b"v")     # 父目录不存在，应自动建
    assert d.read("/deep/nested/f.txt") == b"v"


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

    def target(self, rel_path: str, query: str | None = None) -> str:
        """相对路径 → 已编码的最终 target。用于用户输入的路径。"""
        rel = normalize_remote_path(rel_path)
        return enc_path(self.base_path + rel) + (query or "")

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
    s, base = dav
    d, t = _dav(s, base)
    payload = bytes(range(256)) * 200        # 51200 字节
    s.add_file("/big.bin", payload)
    dest = tmp_path / "big.bin"
    nsdav.download(d, "/big.bin", str(dest), chunk=1024, transport=t)
    assert dest.read_bytes() == payload


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
    from urllib.parse import urlsplit

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
    nsdav.upload(d, str(src), "/u.txt", verify="strong")

    s.store["/u.txt"] = b"XXXXXXXXXX" + payload[10:]   # 篡改开头
    with pytest.raises(nsdav.NsdavError):
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


def test_07_pagination_over_750(dav):
    """建 760 个文件触发分页，确认不丢条目。这条最慢，放最后。"""
    for i in range(760):
        dav.put(f"{TEST_DIR}/p-{i:04d}.txt", b"x")
    names = [e.name for e in dav.listdir(TEST_DIR)]
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
