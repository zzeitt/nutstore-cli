# nsdav — 坚果云 WebDAV 命令行工具 · 设计规格

日期：2026-09-21
状态：已通过设计评审，待实现
版本：v0.1.0

## 1. 目标

做一个单文件 Python 脚本，用坚果云自己的 WebDAV 协议直接读写云端文件。
不挂载磁盘、不装依赖、不需要 root。

主要使用场景是 **iOS 上的 iSH**（Alpine Linux i386 模拟）。同一个文件在 macOS、
Linux、Windows、a-Shell、Termux 上不做任何修改也能跑。

### 非目标

- 不做双向同步。不做本地索引、不做三方比对、不做冲突合并。
- 不碰 Obsidian 的仓库结构、`.obsidian` 配置目录、忽略规则那一套。
- 不做 OAuth 登录。只用 WebDAV 账号 + 应用密码。

说清楚原因：插件里真正做同步的那部分（`src/sync/**`）有约 5000 行，是它最重的
组件。本次只提炼出它踩过的**协议层坑**，这部分才是"可靠"二字的来源。

## 2. 实测确认的服务器行为

以下全部是在 2026-09-21 用真实账号对 `dav.jianguoyun.com` 实测得到的结果，
不是推测。测试在 `/dav/notes/nsdav-test/` 下进行，测试数据已清理。

| 行为 | 实测结果 | 影响 |
|---|---|---|
| Basic 认证 | PROPFIND 返回 207 | 认证走标准 Basic |
| `HEAD` 问大小 | `Content-Length: 0`，实际 16 字节 | **HEAD 完全不可用**，必须改用 PROPFIND 或 Range GET |
| PROPFIND 问大小 | 返回正确的 16 | 取大小的正道 |
| Range GET | `206` + `Content-Range: bytes 6-8/16` | **断点续传可用**，大文件下载能续 |
| 路径不存在 | `404`（GET 和 PROPFIND 都是） | 按标准 404 处理即可 |
| `MKCOL` | 201 | 标准 |
| `PUT` | 201（新建） | 标准 |
| `MOVE` | 201 | 标准 |
| `DELETE` + `Depth: infinity` | 204，1201 个文件耗时 2.8 秒 | 递归删除服务端一次做完，不用客户端遍历 |
| 目录列表分页 | **满 750 条触发**。第一页返回 750 条 + 自身 = 751，响应头带 `Link: <...?mk=xxx>; rel="next"` | **不跟随分页就会静默丢文件** |
| 分页机制 | marker 式：`?mk=<最后一项的路径>`，不是 offset | `mk` 值在 Link 里**已经是编码过的**，必须原样透传 |
| 连接复用 | 630 ms/次 → **101 ms/次**（快 6 倍） | 必须保持长连接 |
| XML 命名空间 | `<d:multistatus xmlns:d="DAV:" xmlns:s="http://ns.jianguoyun.com">` | 解析必须按命名空间，不能用字符串匹配 |
| 目录的 `getcontentlength` | `0` | 目录大小无意义，用 `resourcetype/collection` 判目录 |
| `getlastmodified` 格式 | `Mon, 21 Sep 2026 08:03:53 GMT` | RFC 1123，用 `email.utils.parsedate_to_datetime` 解析 |

### 一个曾经搞错的点

早期探测时看到 `PROPFIND https://dav.jianguoyun.com/nonexistent-xyz` 返回 **410**，
据此以为"坚果云用 410 表示不存在"。**这是错的**——那个路径在 `/dav/` 挂载点之外，
410 说的是挂载点不存在。挂载点**内部**的缺失路径一律是 404。按 404 处理。

## 3. 部署形态

单个 `nsdav.py`，只用标准库。这一点是刻意的：

- iSH 里 `apk add python3` 就有，不需要 pip、不需要编译 wheel（i386 模拟下编译
  第三方包基本是灾难）。
- a-Shell 内置 python3，但装不了 pip 包。纯标准库才跑得起来。
- 往手机里部署就是"弄进去一个文件"：`cat > nsdav.py` 粘贴，或者从
  GitHub raw 拉一次。没有 venv、没有依赖树。

因此**不使用 `requests`**，用 `http.client`。选 `http.client` 而不是
`urllib.request` 有两个具体原因：urllib 在重定向时会吃掉非标准方法名；
以及 urllib 没有干净的流式读写钩子，而我们要做流式上传和 Range 分块下载。

## 4. 架构

```
Transport   http.client 长连接 + 预置 Basic 头
            ├── RateLimiter   串行 + 最小间隔（默认 200ms）
            ├── RetryPolicy   指数退避 + 抖动，可中断
            └── 超时、User-Agent、连接失效重连
    │
WebDAV      协议操作
            ├── propfind(path, depth)   含 Link 分页循环
            ├── get(path, range)  put  mkcol  delete  move  copy
            └── enc_path()              只编码路径段，query 原样保留
    │
Model       Entry(path, name, is_dir, size, mtime)
    │
CLI         argparse 分发
```

### 各层职责边界

- **Transport** 不懂 WebDAV。它只知道"发一个请求，拿到状态码和字节"。
  限流、重试、超时、UA 全在这一层，对上层透明。
- **WebDAV** 不懂命令行。它只做协议，不知道有 `ls` 这个命令。
- **CLI** 不懂 HTTP。它只调用 WebDAV 层的方法，负责参数解析和输出格式。

这样分层是为了能单独测：Transport 可以对着一个故意捣乱的 mock 服务器测，
WebDAV 层可以对着假 Transport 测，CLI 层几乎不用测。

### `enc_path` 的实现要点

分页踩过的坑就在这里。正确做法是**把路径和 query 分开处理**：

```python
def enc_path(p):
    path, _, query = p.partition("?")
    encoded = "/".join(quote(seg, safe="") for seg in path.split("/"))
    return encoded + ("?" + query if query else "")
```

路径段逐个 `quote(safe="")`，保证空格变 `%20`、`+` 变 `%2B`、中文和 emoji 正确。
而 query 部分**原样透传**，因为 Link 头里给出的 `mk` 值已经是编码过的，
再编码一次就会变成 `%252F`，服务端返回 400。这个错误在 spike 阶段真实发生过。

**由此推出一条必须遵守的规则**：`enc_path` 只能用于**用户输入的原始路径**。
从 `Link` 头拿到的下一页 URL，其 path 和 query **都已经是编码过的**，
必须原样送给 `http.client`，**不能再过一次 `enc_path`**。

否则会踩一个很隐蔽的坑：如果目录名里有空格，Link 里给的是
`/dav/notes/my%20notes?mk=...`，再过一次 `enc_path` 会把它变成
`my%2520notes`，服务端去找一个名字里真带 `%20` 的目录，然后 404——
而且只在"目录名含特殊字符 **且** 条目超过 750 条"时才出现。

实现上用一个显式的标记区分：Transport 层接收的 target 分两种，
一种是"未编码的原始路径"，一种"已经完全成形的 URL"。
不要靠猜，靠显式参数或类型区分。

## 5. 命令

```
nsdav ls [path]               列目录（Depth-1，自动跟随分页）
nsdav stat <path>             单个条目元信息（Depth-0）
nsdav tree [path] [-d N]      递归列出（-d 默认 3，-d 0 表示不限深度）
nsdav cat <path>              文件内容打到 stdout
nsdav get <remote> [local]    下载（Range 分块，可续传）
nsdav put <local> [remote]    上传（流式，传完校验）
nsdav mkdir [-p] <path>       建目录（-p 递归建父目录）
nsdav rm [-r] [-y] <path>     删除（-r 递归）
nsdav mv <src> <dst>          移动
nsdav cp <src> <dst>          复制
nsdav quota                   查配额（RFC 4331，失败则明确报不支持）
```

全局参数：

| 参数 | 作用 |
|---|---|
| `--json` | 输出 JSON，便于脚本处理 |
| `-v` / `-q` | 详细 / 安静 |
| `--dry-run` | 只打印要做什么，不真做（`rm -r`、`mv` 等破坏性操作） |
| `--url` `--user` `--password` | 覆盖配置 |
| `--min-gap` | 请求最小间隔，默认 0.2 秒 |
| `--max-retries` | 重试上限，默认 5 |
| `--timeout` | 单请求超时，默认 120 秒 |

### 破坏性操作的防护

`rm -r` 会先列出将要删除的内容并**要求确认**，除非给 `-y`。
这是针对"单文件脚本 + 手机上手滑"这个组合的。

## 6. 可靠性清单

这些是工具的真正价值所在，逐条对应上面实测到的事实。

1. **PROPFIND 分页循环**：每次看响应头 `Link`，正则取 `rel="next"`，跟随直到没有。
   这是防静默丢文件的唯一手段。
2. **请求串行 + 最小间隔**：默认 200ms。不并发，避免被限流。
3. **重试**：503 / 429 / 连接错误走指数退避 + 抖动（1s 起，封顶 60s）。
   与插件不同——插件是**死等 60 秒**，而且不可中断，本项目要可被 Ctrl-C 打断。
4. **连接复用**：保持一条 `HTTPSConnection`。实测 6 倍速度差。
   连接失效（`RemoteDisconnected`、`BrokenPipe`）时自动重建并重试一次。
5. **大小只从 PROPFIND 或 Range GET 取**，**永远不用 HEAD**。
6. **下载**：文件小于一个块（默认 16 MiB）时**一次 GET 拉完**，不分块——
   分块是为大文件省内存，小文件分块只会白白多花几次往返。
   大于一块时按 16 MiB 切 Range 块，写 `<目标>.part`，
   每块按 `Content-Range` 校验字节数，全部到齐后 `os.replace` 原子改名。
   已有 `.part` 且服务端支持 Range 时**从断点继续**（用 `.part` 的当前大小作为
   起始 offset）；若 `.part` 比远端目标还大，说明本地残留是旧的，删掉重下。
7. **上传**：从磁盘流式读，设置 `Content-Length`，超时给足（默认 120s，
   不是某些客户端默认的 60s）。传完用 PROPFIND 核对大小。
8. **退出码**区分：0 成功、1 一般错误、2 参数错、3 认证失败、4 未找到、
   5 限流、6 网络错误。
9. **可中断**：所有 `sleep` 走同一个可打断的等待函数，Ctrl-C 立即退出。
   （插件专门修过"大量任务并发时闪退"的问题，这里从一开始就做对。）

## 7. 配置

优先级从高到低：命令行参数 → 环境变量 → 配置文件。

环境变量：`NSDAV_WEBDAV_URL`、`NSDAV_WEBDAV_USER`、`NSDAV_WEBDAV_PASSWORD`。

配置文件：`~/.config/nsdav/config.toml`（权限须为 600，否则告警）。
只用标准库 `tomllib`（Python 3.11+）解析；3.10 及以下降级为 `key=value` 格式。

## 8. 测试策略

分三层。

**第一层：单元测试（pytest，不碰网络）**
- `enc_path` 往返：空格、`+`、中文、emoji、字面 `%`、连续斜杠
- `Link` 头解析：正常、无、多个、格式错、`rel` 顺序不同
- 退避序列：503 / 429 / 500 / 401 分别应重试几次（401 不该重试）
- PROPFIND XML 解析：多条目、单条目、空、缺字段、目录 vs 文件
- `.part` 续传逻辑：文件不存在 / 已完整 / 部分 / 比远端大

**第二层：mock 服务器（`tests/mock_dav.py`）**

一个进程内的 `http.server`，**故意做错事**：满 N 条就分页、第一次 503 第二次
成功、返回 404、连接中途断开。这是回归网，改 Transport 层时必须全绿。

**第三层：实测（`@pytest.mark.live`，默认跳过）**

对 `/dav/notes/nsdav-test/` 跑一遍完整流程：mkdir → put → stat → ls →
get → range get → mv → rm，结束后自己清理。跑之前先建好一个满了 750 条的
目录验证分页。**只在 `nsdav-test/` 下做任何写入。**

## 9. 许可证

**AGPL-3.0**。

理由要说清楚：本项目的可靠性知识来自阅读 AGPL-3.0 的
`nutstore/obsidian-nutstore-sync` 源码。协议事实本身（分页、404、XML 形状）
不受版权保护，代码也是从零用 Python 重写的——但既然读过源码，沿用同一许可证
是最干净、最没有争议的选择。

如果将来需要 MIT，正确做法是**不看源码**、只依据公开协议文档与实测行为重写一遍。
现在不做这件事。

## 10. 交付物

```
nutstore-cli/
├── AGENTS.md            给 AI 和协作者的仓库说明
├── README.md            用法
├── LICENSE              AGPL-3.0
├── nsdav.py             单文件 CLI（唯一的运行产物）
├── pytest.ini
├── tests/
│   ├── mock_dav.py      进程内 mock WebDAV 服务器
│   ├── test_paths.py
│   ├── test_paginate.py
│   ├── test_retry.py
│   ├── test_propfind.py
│   ├── test_download.py
│   └── test_live.py     实测，默认跳过
└── docs/superpowers/specs/   本文件
```
