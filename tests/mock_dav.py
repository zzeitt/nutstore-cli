"""进程内假 WebDAV 服务器，用于制造分页、503、404、Range 等场景。"""
from __future__ import annotations

import base64
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

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
