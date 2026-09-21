# nsdav

坚果云（Nutstore）WebDAV 的命令行工具。**唯一运行产物是 `nsdav.py`**：单文件、
仅标准库、无构建步骤。目标环境是 iOS 的 iSH（Alpine/i386 模拟）——那里装不了
第三方包、也挂不了文件系统，所以这个工具是"跑一次命令读写一次云端"的形态，
不是常驻同步进程。

这份文件是给 AI 编码代理和人类协作者的仓库说明。设计规格在
`docs/superpowers/specs/2026-09-21-nutstore-webdav-cli-design.md`，
实现计划在 `docs/superpowers/plans/2026-09-21-nsdav-implementation.md`。
**规格里那些"实测确认的服务器行为"是硬事实的来源**，改协议层之前先看它。

## Commands

```bash
# 用法
python3 nsdav.py <子命令> [参数]          # 全局参数写在子命令前面

# 帮助（离线，随便跑）
python3 nsdav.py --help
python3 nsdav.py <子命令> --help

# 测试：默认排除需要真实账号的 live 层
python -m pytest                          # 离线全量（pytest.ini 的 addopts 已 -m "not live"）
python -m pytest tests/test_webdav.py     # 单个文件
python -m pytest -m live                  # 实测层，需要真实账号，见下
```

没有构建步骤，没有 linter 配置，没有依赖安装。`python3 nsdav.py` 就是全部。

`pytest.ini` 加了 `pythonpath = .`，所以测试直接 `import nsdav`。

跑 live 层需要在环境里给凭据（只读 `NSDAV_WEBDAV_USER` /
`NSDAV_WEBDAV_PASSWORD` 即可；**不要设 `NSDAV_WEBDAV_URL`**，默认值正好把
测试目录落在 `/dav/notes/nsdav-test/`，设了会跑到别的挂载点去）：

```bash
NSDAV_WEBDAV_USER=you@example.com NSDAV_WEBDAV_PASSWORD='应用密码' \
  python -m pytest -m live
```

## Environment variables

三档配置的来源之一（优先级：命令行参数 → 环境变量 → 配置文件）。
命名**不对称**，这是坑：凭据带 `WEBDAV`，数值不带。

| 变量 | 用途 |
|---|---|
| `NSDAV_WEBDAV_URL` | WebDAV 地址，默认 `https://dav.jianguoyun.com/dav` |
| `NSDAV_WEBDAV_USER` | 账号 |
| `NSDAV_WEBDAV_PASSWORD` | **应用密码**，不是登录密码 |
| `NSDAV_MIN_GAP` | 相邻请求最小间隔，默认 `0.2` 秒 |
| `NSDAV_MAX_RETRIES` | 重试上限，默认 `5` |
| `NSDAV_TIMEOUT` | 单请求超时，默认 `120` 秒 |
| `XDG_CONFIG_HOME` | 只影响配置文件位置（见下） |

## Dependencies

**仅标准库。这是硬约束，不得引入任何第三方依赖。** `nsdav.py` 里一行
`import` 第三方库都不许有；`pytest` 只在 `tests/` 里用，是唯一的例外
（测试不参与部署）。理由不是洁癖：iSH（i386 模拟）与 a-Shell 上编译 wheel
基本是灾难，而"单文件 + `apk add python3`"是这个工具能在手机上跑起来的
全部原因。

- 用 `http.client`，**不用 `urllib.request`**：urllib 会在重定向时吃掉非标准
  方法名，也没有干净的流式读写钩子（流式上传和 Range 分块下载都要它）。
- XML 用标准库 `xml.etree.ElementTree`。
- `tomllib` 是标准库，但**只有 3.11+ 有**——见下面 Versioning 旁边的版本闸。
- 测试可以直接依赖 `pytest`；`tests/mock_dav.py` 用标准库 `http.server`
  在进程内起一个**故意做错事**的 mock 服务器。

## Architecture

四层，依赖单向向下。每层都有明确**禁止知道**的东西，这不是风格问题——正是
这些边界让每一层能单独测。

| 层 | 在哪 | 职责 | **禁止知道** |
|---|---|---|---|
| CLI | `build_parser` / `cmd_*` / `_dispatch` / `main` | argparse 分发、输出格式、退出码 | HTTP、WebDAV 动词、编码规则 |
| WebDAV | `WebDAV` | 协议操作：`propfind`/`stat`/`listdir`/`put`/`mkcol`/`delete`/`move`/`copy`/`walk` | 命令行、stdout、退出码 |
| Model | `Entry` + 纯函数 | 数据形状；`enc_path`、`normalize_remote_path`、`parse_multistatus`、`parse_next_link`、`url_to_target`、`backoff_delay` | **任何 I/O**（这一层要能拿纯数据直接测） |
| Transport | `Transport` | 连接复用、Basic 头、限流、退避重试、超时、UA | **WebDAV 的存在**（它只知道"发请求、拿状态码和字节"） |

`RateLimiter` 与 `backoff_delay` 挂在 Transport 这一层上（被它使用，不是它的
下层）：限流和重试对上面两层完全透明。

**不在四层里的两个函数**：`download` / `upload`（编排 WebDAV 与 Transport，做
`.part` 续传和传后校验）与 `load_config`（命令行/环境变量/配置文件三级解析）。
它们夹在 WebDAV 与 CLI 之间——只读命令行传进来的 `args` 上的属性，不碰
argparse 本身，也不管输出格式。

### 几条必须守住的实现约定

- **`target` 语义**：`Transport.request(target=...)` 拿到的永远是**已编码的
  最终形式**，该层不做任何编码。有两条路汇入它，各走各的，谁都不许跨线：
  - 用户输入的路径 → `WebDAV.target()` → `enc_path()`（逐段 `quote(safe="")`）；
  - 分页 `Link` 里的下一页 URL → `url_to_target()`（**原样透传**，path 与
    query 都已经编码过了）。

  这样"双重编码"在调用图上不可能发生，而不是靠"记得别编 query"这种约定。
  曾经踩过：把 `Link` 里的 `?mk=%2Ffoo%2Fa.txt` 又编一遍，`%2F` 变 `%252F`，
  服务端 400。`enc_path` **不认识 query**——`?` 和 `#` 一样当普通路径字符编码，
  所以文件名里带 `?` 也能访问。
- **取得大小永远不用 `HEAD`**：实测坚果云的 `HEAD` 回 `Content-Length: 0`，
  完全不可用。大小只从 PROPFIND 的 `getcontentlength` 或 Range GET 拿。
- **删除目录必须显式 `recursive=True`**：WebDAV 对集合的 DELETE 缺省就是
  `Depth: infinity`，"不带 Depth 头"不等于"只删空目录"。协议里没有"删空目录"
  这个操作，所以 `WebDAV.delete()` 自己先 `stat()` 挡住目录，逼调用方明确表态。
  递归删整棵树只发**一次** DELETE（`walk` 是 BFS、父在子前，逐个删两层以上必 404）。
- **XML 一律按命名空间全名比对**（`{DAV:}` + local name），**不许字符串匹配**。
  坚果云会带 `xmlns:s="http://ns.jianguoyun.com"` 这类额外命名空间。
- **宁可报错，不要静默给空**：这是本项目反复在防的那一类 bug。响应不是合法
  XML 或根元素不是 `{DAV:}multistatus` 时 `parse_multistatus` **抛异常**而不是
  返回空列表（空列表会被上层当成"空目录"）；配额查询拿不到 RFC 4331 属性时
  `cmd_quota` 明确报"服务端不支持"，而不是把"没问到"当成"配额是 0"。
- **内存界按 64 KiB**：所有流式读都是 `read(65536)` 的循环，**不许整份
  `read()`**，即使已经在分块（`DOWNLOAD_CHUNK = 16 MiB` 只是请求粒度）。
  iSH 上内存比时间金贵。`.part` / 续传存在的理由就是失败路径（ENOSPC、进度
  回调抛异常、Ctrl-C），那些路径上的 `finally` 也必须按块读。
- **进度与日志一律写 stderr**，stdout 只给数据（`--json` 与 `cat` 的字节）。
- **200 是合法的流响应**：服务端可能忽略 `Range` 而整份发回，`download` 与
  `upload(verify="strong")` 两条路都必须同时接受 `200` 和 `206`。

## Conventions

- 退出码集中在 `nsdav.py` 顶部的 `EXIT_*` 常量，异常类各自带 `exit_code`，
  `main()` 统一结算。加新的失败类别时**同时**加常量、异常类和 README 里的表。
- 用户可见文案是**中文**，代码注释与 docstring 也是中文。报错要说清"怎么办"
  （例如认证失败要提"应用密码"和三种配置方式）。
- 路径参数一律先过 `normalize_remote_path()`：折叠重复斜杠、解析 `.` / `..`、
  不许越过根。
- 新增协议行为前先去 `docs/superpowers/specs/` 的"实测确认的服务器行为"一节
  对一遍；规格与实测冲突时，**以实测为准并且回头改规格**。
- 配置文件是**平铺的顶层键**（`url` / `user` / `password` / `min_gap` /
  `max_retries` / `timeout`），`_read_config_file()` 只取 `tomllib.load()` 的
  顶层项。**不要给它加 `[段落]`**——段落下的键会被无声忽略。未知/拼错的键
  同样无声忽略（这是刻意的：配置文件不因为多一个键就整个拒收），所以改这里
  要留意"安静的失效"。
- 测试分三层，别混：纯函数与解析（单元，不碰网络）→ `tests/mock_dav.py`
  进程内 mock（协议层回归网，改 Transport 必跑）→ `-m live` 打真实服务器
  （默认跳过，**绝不在测试目录之外的路径上写任何东西**）。
- 实测用例的目录固定为 `/dav/notes/nsdav-test/`，用例自己建、自己清理。

## Versioning

版本号的**唯一来源是 `nsdav.py` 里的 `__version__`**（当前 `0.1.0`），
`--version` 直接读它。没有 `setup.py`、没有 `pyproject.toml`、没有
`__init__.py`——不要凭空造第二个版本号。

格式 `MAJOR.MINOR.PATCH`：

| 段 | 含义 | 例子 |
|---|---|---|
| MAJOR | 破坏性变更 / 大重构 | 改 CLI 形态、改 `target` 语义 |
| MINOR | 新功能、新子命令 | 加 `mkdir -p` |
| PATCH | 同一版本内的修复 | 退避上限调准 |

规则：

- 功能合入就提版本号，并且**只在最终合并的那次提交上打 tag**（当前是
  `v0.1.0`）。改版本号就是改 `__version__` 这一行，没有第二处要同步。
- 一个特性分支有多次提交时，只在最后那次提交上打 tag。
- 版本闸：`tomllib` 是 3.11 才有的，`TOML_AVAILABLE = sys.version_info >= (3, 11)`。
  3.10 及以下会忽略配置文件并告警。设计稿里提过的 `key=value` 回退解析器
  **刻意没有实现**（见设计稿 §11），别去补它。

## Commit message format

[Conventional Commits](https://www.conventionalcommits.org/) + 3Cs 正文。

```
<type>(<scope>): <subject>

Changes:
- <做了什么>

Context:
- <为什么，问题是什么，之前是什么样>

Considerations:
- <边界情况、兼容性、版本影响、遗留问题>
```

| type | 用途 |
|---|---|
| `feat` | 新功能 / 新命令 |
| `fix` | 修 bug |
| `refactor` | 重构（行为不变） |
| `docs` | 纯文档 |
| `test` | 纯测试 |
| `chore` | 其它（脚本、仓库杂务） |

**scope** 指明动的是哪一层：

| scope | 对应 |
|---|---|
| `core` | `Entry`、`enc_path`、`normalize_remote_path`、`parse_*` 等纯函数 |
| `webdav` | `WebDAV` 协议层 |
| `transfer` | `download` / `upload` |
| `cli` | argparse 与 `cmd_*` |
| `config` | `load_config` 与配置来源 |
| `test` | `tests/**` |
| `docs` | `README.md` / `AGENTS.md` / `docs/**` |

约束：

- 标题 ≤ 60 字符，不以 `.!?;` 结尾；标题与正文之间空一行。
- 正文字每行 ≤ 72 字符（中文按字符数算，一行别塞太满）。
- 正文必须含 `Changes:` / `Context:` / `Considerations:` 三段。**Context 是
  重点**：说清"之前什么样、为什么改"，而 `git diff` 已经能说明"改了什么"。
- `Considerations` 里写实测数据最有价值——例如"把 DELETE 的
  `Depth: infinity` 那一行整个去掉，89 条用例全绿，而子树照样被服务端删掉
  了"。本仓库的历史提交里有大量这种记录，照这个标准写。
