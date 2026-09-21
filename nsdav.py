#!/usr/bin/env python3
"""nsdav — 坚果云 WebDAV 命令行工具。

单文件，仅标准库。为 iOS iSH 等受限环境设计。
"""
from __future__ import annotations

import email.utils
import random
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Callable
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
