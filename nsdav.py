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
import math
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timezone
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
    """远端一个文件或目录。path 是相对 WebDAV 根的路径，已解码。

    `size` 为 None 表示**服务端没报大小**（PROPFIND 里缺 getcontentlength，
    或值不是纯数字），这与"大小是 0"是两件事。目录的 size 一律是 0 —— 集合
    的大小没有意义，也从没有哪个调用方读过它。
    """
    path: str
    name: str
    is_dir: bool
    size: int | None
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
            # 钳在 0：多余的 '>' 若让 depth 变负，后面每个逗号都判不出
            # depth == 0，整条头会塌成一段，分页链接就静默丢了
            depth = max(0, depth - 1)
        if ch == "," and depth == 0 and not in_quotes:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def parse_next_link(header: str | None) -> str | None:
    """从 Link 头里取出 rel="next" 的 URL，没有就返回 None。

    rel 的值按 RFC 8288 既可以是 quoted-string，也可以是裸 token
    （rel=next）。两种都必须认：漏掉裸 token 那种就是静默丢分页 ——
    目录超过 750 条时会少列文件，而且没有任何信号告诉用户。

    RFC 8288 §2.1.1 还要求注册关系类型逐字符不区分大小写比较，所以
    rel="Next" 同样算 next；参数名与参数值都不区分大小写。
    """
    if not header:
        return None
    for part in _split_link_values(header):
        m = re.match(r"\s*<([^>]+)>\s*(.*)$", part, re.S)
        if not m:
            continue
        url, params = m.group(1), m.group(2)
        for pm in re.finditer(r'(\w+)\s*=\s*("[^"]*"|[^\s;,"]+)', params):
            value = pm.group(2).strip('"').lower()
            if pm.group(1).lower() == "rel" and "next" in value.split():
                return url
    return None


def url_to_target(url: str) -> str:
    """把完整 URL 变成 http.client 用的 target。

    注意：path 和 query 都已经是编码过的，结果必须【直接】交给
    Transport，绝不能再过一次 enc_path。
    """
    sp = urlsplit(url)
    return sp.path + (("?" + sp.query) if sp.query else "")


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

        # 取不到就是 None（"不知道"），不是 0（"空"）。两者混成一个值会让
        # download() 把"问不到大小"当成"远端是空文件"：它会把本地文件截成
        # 0 字节并以成功退出。宁可让调用方看见"不知道"。
        size: int | None = None
        size_el = props.get(f"{DAV}getcontentlength")
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
                # 重试前必须换掉这条连接。服务器可能没读净请求体就回了 503
                # （代理常见做法：读完头就拒绝），剩下的字节留在 socket 里，
                # 下一次请求会被它们的**前面**接上，服务器看到的方法名是
                # `abcdefPUT` 这种垃圾，重试注定失败。异常路径下面已经换连接
                # （_attempt 里 _drop_conn），状态路径当时漏了。
                # 代价是重试时丢掉连接复用——重试本来就罕见，划算。
                self._drop_conn()
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
                # 读干净连接才能复用，但必须按块读：整份 read() 会在**中途失败**
                # 时把剩下的全部字节一次装进内存 —— 磁盘写满（ENOSPC）、进度
                # 回调抛异常、Ctrl-C 都从这条 finally 穿出去，而 .part/续传存在
                # 的理由正是这些场景。成功路径上两条循环都读到了 EOF，drain
                # 读到的是空串，所以这个洞只有失败路径看得见。
                while r.read(65536):
                    pass
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
            # 自身条目一律排除。早先只比 `e.path + ("/" if is_dir)` 与带尾斜杠的
            # want：目录目标能正确排除自己，目标若是**文件**则 depth=1 只返回它
            # 自己、href 不带尾斜杠，于是被当成"自己的子项"返回，`listdir("f")`
            # 得到 `[f]`、`walk("f")` 也把它 yield 出来。去掉两边尾斜杠再比，
            # 两种目标都对：只有目标本身相等，子项一律不同。
            if e.path.rstrip("/") == want.rstrip("/"):
                continue
            out.append(e)
        return out

    def read(self, rel_path: str) -> bytes:
        resp = self._expect(self.t.request("GET", self.target(rel_path)))
        return resp.body

    def walk(self, rel_path: str, max_depth: int = 0) -> Iterator[Entry]:
        """广度优先递归。max_depth=0 表示不限深度。

        seen 记录已经列过的目录。服务端把某个祖先当成自己的子项报回来时
        （同一目录的两种拼写就会这样，`listdir` 的自过滤按规范化路径比，
        拼写一不同就漏过去），没有它这里会无限入队：不报错、不返回，直接把
        进程挂死。实测：把自过滤改成永不命中，整个测试套件就停不下来。
        """
        queue = [(normalize_remote_path(rel_path), 1)]
        seen: set[str] = set()
        while queue:
            cur, depth = queue.pop(0)
            key = cur.rstrip("/") or "/"
            if key in seen:
                continue
            seen.add(key)
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
        """删文件或目录。目标是目录时必须显式 `recursive=True`。

        WebDAV 的 DELETE 对集合**缺省就是 `Depth: infinity`**（RFC 4918
        §9.6.1），所以"不带 Depth 头"并不等于"只删空目录"——服务器照样删整棵
        子树。实测（协议层 + mock 一致）：把下面那行 Depth 头整个去掉，89 条
        用例全绿，而 `/d/a.txt` 连同 `/d/sub` 一起没了。协议里没有"删空目录"
        这个操作，所以这个开关只能在客户端自己兜住：先看清目标是不是目录，
        是就拒绝，让调用方明确说要递归。
        """
        if not recursive and self.stat(rel_path).is_dir:
            raise NsdavError(
                f"{rel_path} 是目录：DELETE 对集合缺省就是递归的，"
                f"要删整棵子树请显式传 recursive=True")
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
    if total is None:
        # 大小未知时**绝不能**当成 0 往下走：total == 0 那条路会把本地文件
        # 换成一个空文件并返回成功。问不到大小就是这个下载做不了的理由，
        # 说清楚比猜一个数好。
        raise NsdavError(
            f"服务端没有返回 {remote_path} 的大小（PROPFIND 缺 "
            f"getcontentlength），无法安全下载：先告诉远端文件多大，"
            f"或者换个能报大小的服务端")
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
            report(total)                # 已经是一整份，也得把进度收尾
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
                    # 服务端忽略 Range，从头返回：丢掉已有进度重来。
                    # 这里同样必须按块读 —— 上面那个分块只约束请求粒度
                    # （chunk 默认 16 MiB），内存界是这里的 64 KiB。整份
                    # read() 会一次性把整个文件读进内存，把"分块只为大文件
                    # 省内存"这个理由作废，而 iSH 上内存比时间金贵。
                    if offset:
                        f.seek(0)
                        f.truncate(0)
                        offset = 0
                    while True:
                        buf = resp.body.read(65536)
                        if not buf:
                            break
                        f.write(buf)
                        offset += len(buf)
                        report(offset)
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
    if entry.size is None:
        # 不能把这个当成"大小不符"报出去 —— 那句话里会印出"远端 None"，
        # 而真正发生的事是校验做不了。
        raise NsdavError(
            f"服务端没有返回 {remote_path} 的大小（PROPFIND 缺 "
            f"getcontentlength），传后校验做不了")
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
            # 200 = 服务端忽略 Range，整个文件都回了过来；206 = 只回我们要的尾巴。
            # 两种都只留最后 tail 字节的滚动窗口 —— 整份 read() 会把远端文件全
            # 读进内存，而这条路本来就承认 200 是合法响应（iSH 上内存比时间金贵，
            # T9 的回退分支踩过同一个坑）。200 那条路上，尾巴在流的末尾。
            remote_tail = b""
            while True:
                buf = r.body.read(65536)
                if not buf:
                    break
                remote_tail = (remote_tail + buf)[-tail:]
        if remote_tail[-tail:] != local_tail:
            raise NsdavError(
                f"上传内容校验失败：{remote_path} 末尾字节与本地不一致")

    if progress:
        progress(size, size)
    return size


# ─────────────────────────────── 配置 ───────────────────────────────

CONFIG_ENV_URL = "NSDAV_WEBDAV_URL"
CONFIG_ENV_USER = "NSDAV_WEBDAV_USER"
CONFIG_ENV_PASSWORD = "NSDAV_WEBDAV_PASSWORD"

# 3.11 以下没有 tomllib：那时配置文件被忽略（会告警），只能靠环境变量/命令行。
TOML_AVAILABLE = sys.version_info >= (3, 11)


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
            if not TOML_AVAILABLE:
                print(f"警告: 当前 Python 不支持 TOML（{sys.version.split()[0]}），"
                      f"已忽略配置文件 {path}；请改用环境变量或命令行参数",
                      file=sys.stderr)
                return {}
            import tomllib
            return {k: str(v) for k, v in tomllib.load(f).items()}
    except Exception as e:
        print(f"警告: 读取 {path} 失败: {e}", file=sys.stderr)
    return {}


def _split_url(url: str) -> tuple[str, int | None, bool, str]:
    sp = urlsplit(url if "://" in url else "https://" + url)
    if not sp.hostname:
        raise UsageError(f"无法解析 WebDAV 地址: {url}")
    try:
        port = sp.port
    except ValueError as e:
        raise UsageError(f"WebDAV 地址里的端口不合法: {url}") from e
    return (sp.hostname, port, sp.scheme != "http",
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
                             "min_gap", DEFAULT_MIN_GAP), float, "min_gap",
                        minimum=0.0),
        max_retries=_number(pick(getattr(args, "max_retries", None),
                                 "NSDAV_MAX_RETRIES", "max_retries",
                                 DEFAULT_MAX_RETRIES), int, "max_retries",
                            minimum=0),
        timeout=_number(pick(getattr(args, "timeout", None), "NSDAV_TIMEOUT",
                             "timeout", DEFAULT_TIMEOUT), float, "timeout",
                        minimum=0.0, exclusive=True),
    )


def _number(value, cast, name: str, *, minimum=None, exclusive=False):
    """配置文件里数值也是字符串，统一在这里转，并给出可读的报错。

    `minimum` 给出下限：默认"不能小于"，`exclusive=True` 时"必须大于"。
    inf / nan 这样的非有限值一律拒绝 —— 它们能穿过上下限比较，最后在
    `socket.settimeout` 那里变成 OverflowError / ValueError。
    """
    try:
        n = cast(value)
    except (TypeError, ValueError) as e:
        raise UsageError(f"配置项 {name} 不是合法数值: {value!r}") from e
    if isinstance(n, float) and not math.isfinite(n):
        raise UsageError(f"配置项 {name} 必须是有限数值: {value!r}")
    if minimum is not None and (n <= minimum if exclusive else n < minimum):
        rel = "必须大于" if exclusive else "不能小于"
        raise UsageError(f"配置项 {name} {rel} {minimum}: {value!r}")
    return n


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


def _size_text(e: Entry) -> str:
    """人类可读输出里的大小列。服务端没报大小就写 `?`。

    不能退化写成 `0 B`：`0 B` 是一个确定的事实，而"不知道"不是 —— 这跟
    `Entry.size` 用 None 而不是 0 表示未知是同一条理由。
    """
    return "?" if e.size is None else format_size(e.size)


def _print_entries(entries, as_json: bool) -> None:
    if as_json:
        print(json.dumps([_entry_dict(e) for e in entries],
                         ensure_ascii=False, indent=2))
        return
    for e in entries:
        if e.is_dir:
            print(f"  {'<dir>':>10}  {e.name}/")
        else:
            print(f"  {_size_text(e):>10}  {e.name}")


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


# ── 命令分发 ──

def _progress(done: int, total: int | None) -> None:
    """下载进度写 stderr —— stdout 留给数据与 --json。"""
    if not total:
        return
    sys.stderr.write(f"\r  {format_size(done)} / {format_size(total)}"
                     f"  ({done * 100.0 / total:5.1f}%)")
    if done >= total:
        sys.stderr.write("\n")


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _print_entry(e: Entry, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_entry_dict(e), ensure_ascii=False, indent=2))
    elif e.is_dir:
        print(f"{e.path}/")
    else:
        print(f"{e.path}  {_size_text(e)}")


def cmd_ls(dav, args) -> int:
    _print_entries(dav.listdir(args.path), args.json)
    return EXIT_OK


def cmd_stat(dav, args) -> int:
    _print_entry(dav.stat(args.path), args.json)
    return EXIT_OK


def cmd_tree(dav, args) -> int:
    root = normalize_remote_path(args.path)
    root_segs = len([s for s in root.split("/") if s])
    if args.json:
        _print_entries(list(dav.walk(root, max_depth=args.depth)), True)
        return EXIT_OK
    print(root)
    for e in dav.walk(root, max_depth=args.depth):
        segs = len([s for s in e.path.rstrip("/").split("/") if s])
        indent = "  " * max(segs - root_segs - 1, 0)
        print(f"{indent}{e.name}{'/' if e.is_dir else ''}")
    return EXIT_OK


def cmd_cat(dav, args) -> int:
    sys.stdout.buffer.write(dav.read(args.path))
    sys.stdout.buffer.flush()
    return EXIT_OK


def cmd_get(dav, args) -> int:
    remote = normalize_remote_path(args.remote)
    local = args.local or os.path.basename(remote.rstrip("/")) or "download"
    if args.dry_run:
        print(f"将下载 {remote} → {local}")
        return EXIT_OK
    path, n = download(dav, remote, local, transport=dav.t,
                       progress=None if args.quiet else _progress)
    if not args.quiet:
        print(f"已下载 {format_size(n)} → {path}", file=sys.stderr)
    return EXIT_OK


def cmd_put(dav, args) -> int:
    remote = normalize_remote_path(
        args.remote or "/" + os.path.basename(args.local))
    if args.dry_run:
        print(f"将上传 {args.local} → {remote}")
        return EXIT_OK
    n = upload(dav, args.local, remote, verify=args.verify)
    if not args.quiet:
        print(f"已上传 {format_size(n)} → {remote}", file=sys.stderr)
    return EXIT_OK


def cmd_mkdir(dav, args) -> int:
    if args.dry_run:
        print(f"将创建 {args.path}")
        return EXIT_OK
    if args.parents:
        dav.mkdirs(args.path)
    else:
        dav.mkcol(args.path)
    return EXIT_OK


def cmd_rm(dav, args) -> int:
    target = normalize_remote_path(args.path)
    # 带不带 -r 都要先 stat 一次：判"目标是目录"只此一途。短路掉的话，目录
    # 不带 -r 会落到下面的 delete()，由 T8 的守卫抛 NsdavError 兜住 —— 那是
    # 退出码 1（一般错误），而 README 的退出码表写着 2 = 用法错误，脚本按 2
    # 分类用法错时就会误判。这是 CLI 层判得出来的用法问题，不是服务端的事。
    is_dir = dav.stat(target).is_dir
    if is_dir and not args.recursive:
        # --dry-run 也走这条：那种输入下打印"将删除"是误导 —— 真跑必被拒，
        # 而 dry-run 的承诺是"打印真跑会发生的事"。
        raise UsageError(
            f"{target} 是目录，删除目录需要 -r（协议里没有只删空目录的操作）")
    if not is_dir:
        # 文件：-r 给不给都照删。
        if args.dry_run:
            print(f"将删除 {target}")
            return EXIT_OK
        dav.delete(target)
        return EXIT_OK

    # 递归删整棵树就是**一个** DELETE：RFC 4918 §9.6.1 规定对集合缺省
    # Depth: infinity。别自己拆成逐个删 —— walk 是 BFS、父在子前，删掉父目录
    # 之后子项已不存在，两层以上必然 404（实测：三层时退出码 4，而 T12 原有
    # 用例只嵌一层，看不到）。列出来只为确认提示与 --dry-run。
    doomed = [target] + [e.path for e in dav.walk(target)]
    if args.dry_run:
        for p in doomed:
            print(f"将删除 {p}")
        return EXIT_OK
    if not args.yes:
        for p in doomed:
            print(p)
        if not _confirm(f"以上 {len(doomed)} 项将被递归删除，确认？[y/N] "):
            print("已取消", file=sys.stderr)
            return EXIT_ERROR
    dav.delete(target, recursive=True)
    return EXIT_OK


def cmd_mv(dav, args) -> int:
    if args.dry_run:
        print(f"将移动 {args.src} → {args.dst}")
        return EXIT_OK
    dav.move(args.src, args.dst)
    return EXIT_OK


def cmd_cp(dav, args) -> int:
    if args.dry_run:
        print(f"将复制 {args.src} → {args.dst}")
        return EXIT_OK
    dav.copy(args.src, args.dst)
    return EXIT_OK


_QUOTA_BODY = (b'<?xml version="1.0" encoding="utf-8"?>'
               b'<D:propfind xmlns:D="DAV:"><D:prop>'
               b'<D:quota-available-bytes/><D:quota-used-bytes/>'
               b'</D:prop></D:propfind>')


def _quota_numbers(resp) -> tuple[int | None, int | None]:
    avail = used = None
    try:
        root = ET.fromstring(resp.body)
    except ET.ParseError as e:
        raise NsdavError(f"配额响应不是合法 XML: {e}")
    if root.tag != f"{DAV}multistatus":
        # 与 parse_multistatus 同一道把关：外层不是 multistatus 就不是配额
        # 响应。回 (None, None)，交给 cmd_quota 那条"服务端不支持配额查询"
        # 的报错路径 —— 这里静默返回就等于"配额为 0"，正是本文件反复在防的
        # 那种静默失败。
        return None, None
    # 属性按**全名**比对（DAV 常量 + local name），不按 local name：`DAV:`
    # 之外命名空间里同名的属性不算数。全局约束要求 XML 一律按命名空间解析，
    # 不得用字符串匹配，本文件其它地方（parse_multistatus）就是这么做的。
    for el in root.iter():
        if not el.text or not el.text.strip().isdigit():
            continue
        if el.tag == f"{DAV}quota-available-bytes":
            avail = int(el.text)
        elif el.tag == f"{DAV}quota-used-bytes":
            used = int(el.text)
    return avail, used


def cmd_quota(dav, args) -> int:
    # propfind() 只回 Entry（不含 RFC 4331 的配额属性），这里要原始响应。
    resp = dav._expect(dav.t.request(
        "PROPFIND", dav.target("/"), body=_QUOTA_BODY,
        headers={"Content-Type": 'application/xml; charset="utf-8"',
                 "Depth": "0"}))
    avail, used = _quota_numbers(resp)
    if avail is None:
        raise NsdavError(
            "服务端不支持配额查询（PROPFIND 未返回 RFC 4331 的 "
            "quota-available-bytes）")
    if args.json:
        print(json.dumps({"available": avail, "used": used},
                         ensure_ascii=False, indent=2))
    elif used is None:
        print(f"可用 {format_size(avail)}")
    else:
        print(f"可用 {format_size(avail)}，已用 {format_size(used)}，"
              f"合计 {format_size(avail + used)}")
    return EXIT_OK


_COMMANDS = {
    "ls": cmd_ls, "stat": cmd_stat, "tree": cmd_tree, "cat": cmd_cat,
    "get": cmd_get, "put": cmd_put, "mkdir": cmd_mkdir, "rm": cmd_rm,
    "mv": cmd_mv, "cp": cmd_cp, "quota": cmd_quota,
}


def _dispatch(args, dav) -> int:
    return _COMMANDS[args.cmd](dav, args)


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
