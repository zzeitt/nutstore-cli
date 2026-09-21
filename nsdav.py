#!/usr/bin/env python3
"""nsdav — 坚果云 WebDAV 命令行工具。

单文件，仅标准库。为 iOS iSH 等受限环境设计。
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

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
