# nsdav — 坚果云 WebDAV 命令行工具

`nsdav.py` 是**一个文件、只用标准库**的命令行工具，直接用坚果云的 WebDAV
接口读写云端文件。适合在 iOS 的 iSH、a-Shell 这类装不了第三方包的环境里用。

```sh
python3 nsdav.py ls /
python3 nsdav.py get /notes/todo.md
python3 nsdav.py put ./report.pdf /notes/report.pdf
```

## 为什么不用挂载

在 iOS 上把坚果云挂成文件系统要越狱、要内核扩展、要 root，iSH 里做不到。
但读写云端文件并不需要挂载：WebDAV 就是 HTTP，标准库的 `http.client` 足够。
所以这里的做法是"每次要用的时候跑一下命令"，而不是常驻一个虚拟磁盘。

代价是没有本地缓存、没有双向同步、没有冲突合并——**本工具不做这些**，
它只做"把这一份文件读下来 / 传上去"。

## 在 iSH 上安装

```sh
# 1. 装 python3（iSH 自带 apk，装出来就是 3.11 以上）
apk add python3

# 2. 把 nsdav.py 弄进去：只有一个文件，怎么弄都行。
#    paste 法（把本地文件内容粘进终端，Ctrl-D 结束）：
cat > nsdav.py

#    或者克隆整个仓库（仓库尚未公开，地址待定）：
#    git clone <待定> && cd nutstore-cli

# 3. 先配好凭据（第 4 步就用得上；没配会以退出码 3 报"缺少账号或密码"）。
#    最省事的是环境变量（密码是应用密码，不是登录密码）：
export NSDAV_WEBDAV_USER="you@example.com"
export NSDAV_WEBDAV_PASSWORD="应用密码"

# 4. 跑起来（先看根目录）
python3 nsdav.py ls /
```

密码要到坚果云网页上 **账户信息 → 安全选项 → 添加应用密码** 去生成，
**不是登录密码**。除了环境变量，还可以用命令行参数或配置文件，三种方式
和优先级见下面「配置」一节。

## 命令

11 个子命令，全局参数写在子命令**前面**（`nsdav --json ls /`，不是
`nsdav ls / --json`——后者的 `--json` 会被当成多余参数，退出码 2）。

| 命令 | 做什么 |
|---|---|
| `nsdav ls [path]` | 列目录（Depth 1，自动跟随分页）；`path` 省略即 `/` |
| `nsdav stat <path>` | 单个条目的元信息（Depth 0） |
| `nsdav tree [path] [-d N]` | 递归列出；`path` 省略即 `/`，`-d` 默认 3，`-d 0` 表示**不限深度** |
| `nsdav cat <path>` | 文件内容原样写到 stdout |
| `nsdav get <remote> [local]` | 下载；`local` 省略时取远端文件名 |
| `nsdav put <local> [remote]` | 上传，传完校验；`remote` 省略时是 `/<本地文件名>` |
| `nsdav mkdir [-p] <path>` | 建目录；`-p` 连父目录一起建 |
| `nsdav rm [-r] [-y] <path>` | 删除；目标是目录必须带 `-r` |
| `nsdav mv <src> <dst>` | 移动（MOVE） |
| `nsdav cp <src> <dst>` | 复制（COPY） |
| `nsdav quota` | 查配额（RFC 4331；服务端不支持时明确报错，不会假装是 0） |

各命令自己的选项：

| 命令 | 选项 | 说明 |
|---|---|---|
| `tree` | `-d, --depth N` | 递归深度，默认 3，0 = 不限 |
| `put` | `--verify {size,strong}` | 默认 `size`：传完用 PROPFIND 核对大小；`strong` 再多读回末尾 64 字节（小于 64 字节就整份）比对内容 |
| `mkdir` | `-p, --parents` | 递归建父目录 |
| `rm` | `-r, --recursive` | 允许删目录（递归） |
| `rm` | `-y, --yes` | 跳过确认 |

`rm -r` 会先把将要删除的每一行打印出来，再问一次 `[y/N]`；回答 no 会以
退出码 1 结束（不是 0）——脚本里想无人值守就加 `-y`。**`rm -r` 只发一次
DELETE**：WebDAV 规定对集合的 DELETE 缺省就是 `Depth: infinity`，服务端
一次删完整棵子树，客户端不逐个子删。反过来说，**目标是目录时 `rm` 不带
`-r` 一律被拒绝（空的也一样）**，因为协议里根本没有"只删空目录"这个操作，
所以客户端不敢替你猜——它没法保证只删掉空的那一层。这种调用**以退出码 2
（用法错误）结束**，在发 DELETE 之前就判掉（`--dry-run` 也一样，不会先打出
一行"将删除"再拒绝）。

`get` 和 `put` 各有一个值得知道的行为：

- **`get` 会先写 `<目标>.part`，全部到齐才原子改名成目标文件。** 中断之后
  再跑同一条命令，它会拿现有 `.part` 的大小当起点**接着下**，不用从头再来。
  所以看到目录里有个 `.part` 是正常的中间状态，不是垃圾。
- **`put` 不用先 `mkdir`。** 父目录不存在时客户端会自动逐级建出来再重试一次
  PUT（服务端回 `409` 就是这个信号）。

## 全局参数

| 参数 | 作用 |
|---|---|
| `--version` | 打印版本号后退出 |
| `--json` | 以 JSON 输出（`ls` / `stat` / `tree` / `quota` 支持） |
| `-v, --verbose` | 把每个请求写到 stderr |
| `-q, --quiet` | 不打印进度和完成行 |
| `--dry-run` | 只打印要做什么，不发写请求（`get`/`put`/`mkdir`/`rm`/`mv`/`cp`） |
| `--url URL` | 覆盖 WebDAV 地址 |
| `--user USER` | 覆盖账号 |
| `--password PASSWORD` | 覆盖应用密码 |
| `--min-gap SECONDS` | 相邻请求的最小间隔，默认 `0.2` |
| `--max-retries N` | 失败重试上限，默认 `5` |
| `--timeout SECONDS` | 单次请求超时，默认 `120` |

`--dry-run` 对只读命令（`ls`/`stat`/`tree`/`cat`/`quota`）没有意义，它们本来
就不写。`--json` 对 `cat` 无效——`cat` 往 stdout 写的是文件的原始字节，不做
任何包装。

## 配置

三种方式，**优先级从高到低：命令行参数 → 环境变量 → 配置文件**。

**一、命令行参数**（见上表，`--url` / `--user` / `--password` 等）。

**二、环境变量**

| 变量 | 对应 |
|---|---|
| `NSDAV_WEBDAV_URL` | WebDAV 地址，默认 `https://dav.jianguoyun.com/dav` |
| `NSDAV_WEBDAV_USER` | 账号 |
| `NSDAV_WEBDAV_PASSWORD` | 应用密码 |
| `NSDAV_MIN_GAP` | 最小间隔 |
| `NSDAV_MAX_RETRIES` | 重试上限 |
| `NSDAV_TIMEOUT` | 超时 |

注意命名不对称：**凭据那三个带 `WEBDAV`**（`NSDAV_WEBDAV_USER`），**数值
那三个不带**（`NSDAV_MIN_GAP`、`NSDAV_MAX_RETRIES`、`NSDAV_TIMEOUT`）。
写错了不会报错，只会被无声忽略。

**三、配置文件** `~/.config/nsdav/config.toml`（认 `XDG_CONFIG_HOME`，设了
就用 `$XDG_CONFIG_HOME/nsdav/config.toml`）。文件权限若是组/其他人可读，
会在 stderr 给一条告警，建议 `chmod 600`。

最小可用的配置文件长这样——**平铺的顶层键，没有段落头**：

```toml
url = "https://dav.jianguoyun.com/dav"
user = "you@example.com"
password = "这里填应用密码"
timeout = 30
min_gap = 0.2
max_retries = 5
```

**这里有个坑，请务必看清**：加载器只读 TOML 的**顶层键**。所以

```toml
[nsdav]          # ← 加了段落头
timeout = 30
```

里面的 `timeout` 会被**无声忽略**——不报错、不警告，行为跟没写一样。
同理，键名拼错（`passwd`、`NSDAV_TIMEOUT` 这种把环境变量名当键名写的）
也一样安静地失效。

判断配置到底有没有被读进去，最快的办法是**只往配置文件里放凭据**：
如果认证仍然失败（退出码 3，报"缺少账号或密码"），那说明这个文件根本没
被加载——先去查路径和权限，别去怀疑密码。

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 一般错误（含上传后校验不符、`rm -r` 时回答 no） |
| `2` | 用法/参数错误（argparse 报的也是这个；`rm` 目录不带 `-r` 也是这个） |
| `3` | 认证失败（HTTP 401，或压根没找到账号密码） |
| `4` | 路径不存在 |
| `5` | 被限流（HTTP 429） |
| `6` | 网络错误（连不上、超时、重试次数耗尽） |
| `130` | 被 Ctrl-C 中断 |

特例：`nsdav cat 大文件 | head` 这种下游提前关闭管道的情况算成功，退出码 0。

## 实测发现的服务器行为

下面这些是 2026-09-21 拿真实账号对着 `dav.jianguoyun.com` 一条条测出来的，
不是照协议文档推测的。它们是这个工具真正在防的东西。

- **`HEAD` 完全不可用**：`HEAD` 问一个 16 字节的文件，`Content-Length` 回的
  是 `0`。所以客户端一次 `HEAD` 都不发——**大小一律来自 PROPFIND 的
  `getcontentlength`**（也就是每次先 `stat()`），没有第二个来源。`Range`
  GET 只用来搬内容与断点续传，不参与确定大小。
- **缺路径是 `404`，不是 `410`**：挂载点 `/dav/` **内部**的缺失路径一律 404。
  只有在 `/dav/` 之外才会看到 410，那说的是挂载点不存在。（早期实测在这里
  走过弯路，见设计稿 §2。）
- **目录满 750 条触发分页**：第一页回 750 条 + 自身，响应头带
  `Link: <...?mk=xxx>; rel="next"`。marker 式分页，不是 offset。客户端逐页
  跟到底，**不跟就会静默少列文件**。
- **`Link` 的值不能二次编码**：里面的 path 和 query 都已经编码过了，必须
  原样重发。客户端只在用户输入的路径上编码，`Link` 走另一条路原样透传——
  两条路在传输层汇合，编码不会重复发生。
- **目录的 `getcontentlength` 是 `0`**：判目录要看 `resourcetype` 里的
  `collection`，别看大小。
- **`getlastmodified` 是 RFC 1123**（`Mon, 21 Sep 2026 08:03:53 GMT`）。
- **XML 带额外命名空间**：`<d:multistatus xmlns:d="DAV:"
  xmlns:s="http://ns.jianguoyun.com">`，解析必须按命名空间，不能拿字符串凑。
- **Range GET 回 `206`**，断点续传可用。客户端同时接受 `200`：服务端一旦
  忽略 `Range`、整份发回，也是合法响应，下载逻辑会丢掉已有进度从头重写，
  而不是把两种响应混在一起拼出一个坏文件。
- **DELETE 对集合缺省就是 `Depth: infinity`**：见上面 `rm` 那一段。

## 已知限制：目录名需要转义且条目超过 750 条时，列不全

这是一个**服务端缺陷**，客户端没有兜底，这里如实写出来。

触发条件是两个同时成立：

1. 目录名本身含有需要百分号编码的字符（比如中文或空格）；
2. 该目录里条目超过 750 条，于是服务端发分页 `Link`。

这时服务端发出来的 `Link`，其 **path 被编码了两次**，而同一个 `Link` 的
`?mk=` 查询参数只编码一次。实测（目录名 `分页 目录`，760 个文件）：

- 把 `Link` 原样重发（这是 HTTP 客户端唯一正确的做法）→ `404 ObjectNotFound`；
- 把 path 手动 unquote 一次、query 原样 → `207`，剩下的 10 条全回来；
- 同样 760 条的**纯 ASCII 目录名**分页完全正常。

对用户的影响：**用 `ls`/`tree` 列一个"名字含中文且条目超过 750"的目录，
会以 404 失败（退出码 4），而不是给出完整列表。**

客户端**刻意不做补偿**。把 path 再 unquote 一次看着能"修好"这一个场景，
但那是对服务端返回值的猜测式改写，会在别的路径上改坏真实数据——一个为了
糊住 A 场景而破坏 B 场景的补丁，比一个说得清楚的失败更糟。

这一条被实测用例 `tests/test_live.py::test_07_...` 钉住了，而且是
`xfail(strict=True, raises=nsdav.NotFoundError)`：现在它"预期失败"，
所以整轮是绿的；哪一天服务端改好了，它会变成 **XPASS** 并且**判红**，
提醒我们回来把这条限制删掉。

## 输出：stdout 和 stderr 是分开的

- **stdout**：`ls` / `stat` / `tree` / `quota` 的结果，`--json` 的 JSON，
  `cat` 的原始字节，**以及 `--dry-run` 的"将…"行、`rm -r` 的待删清单和
  `[y/N]` 确认提示**。
- **stderr**：进度条、`-v` 的请求日志、`get`/`put` 的完成行
  （`已下载 …` / `已上传 …`）、`rm -r` 回答 no 时的 `已取消`，以及所有
  告警和错误。

**注意下面这条：stdout 并不总是"干净的数据"。** `--dry-run` 的"将删除 …"
清单、`rm -r` 在删除前列出的每一项、以及 `以上 N 项将被递归删除，确认？[y/N]`
这个提示，走的都是 stdout（实测 `--dry-run rm -r` 退出码 0，stderr 全空；
`rm -r` 回答 no 退出码 1，`已取消` 才写 stderr）。所以：

```sh
# 待删清单会进 out.txt，不是进终端
nsdav --dry-run rm -r /x > out.txt
```

**脚本里别把 stdout 当纯数据流。** 要机器可读的输出就只用 `--json`
（`ls` / `stat` / `tree` / `quota`）；`--dry-run` 与 `rm -r` 的提示是给人看的
文本，别当输入。

另一半仍然成立：`nsdav get big.iso > log.txt` **不会**把"已下载 …"写进
`log.txt`；要连日志一起收，得写 `nsdav get big.iso > log.txt 2>> log.txt`。
反过来，`nsdav ls / > list.txt` 拿到的是一份干净的列表。

## Python 版本要求

**硬性要求 Python 3.11 或以上。** 原因只有一个：配置文件用标准库的
`tomllib` 解析，而它是 3.11 才加进来的。

设计稿里曾经写过"3.10 及以下降级为 `key=value` 格式"，那个回退解析器
**刻意没有实现**（见设计稿 §11）：iSH 和 a-Shell 上 `apk add python3` /
自带的 python3 都已经是 3.11+，为旧版本多维护一套解析器属于净负担。

在 3.10 及以下会怎样：配置文件被忽略，stderr 上给一条明确的告警，只能靠
环境变量或命令行参数。**用户该做的就是升级 Python**——iSH 里
`apk add python3` 装出来就是当前的 3.11+。

## Windows / Git Bash

在 MSYS 系的外壳（Windows 上的 Git Bash）里，像 `/notes` 这样的绝对远端
路径参数会在程序看到它之前被改写成 `C:/Program Files/Git/notes`，服务端
于是回 `409 AncestorsNotFound`。加一个环境变量即可：

```sh
MSYS_NO_PATHCONV=1 python nsdav.py ls /notes
```

这是外壳的路径转换行为，不是客户端的问题。

## 许可证

AGPL-3.0，见 [LICENSE](LICENSE)。
