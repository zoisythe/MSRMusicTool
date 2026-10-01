# MSRMusicTool

MSRMusicTool 是一个基于 Python 和 Textual 的塞壬唱片音乐下载工具。它从
[Monster Siren Records](https://monster-siren.hypergryph.com/music) 获取专辑与歌曲索引，
支持终端内搜索、按专辑或单曲选择，以及批量下载音频、LRC 歌词和专辑封面。

## 下载独立版本

可以从 [Releases](https://github.com/zoisythe/MSRMusicTool/releases/latest) 下载对应系统的
`msr-tool-*` 单文件程序。独立版本已经包含 Python 和全部依赖，无需另外安装运行环境。

- Windows：下载 `.exe` 文件后在终端中运行。
- Linux、macOS：下载后先执行 `chmod +x msr-tool-*`，再从终端启动。
- macOS 构建未签名；首次运行时可能需要在系统安全设置中确认。

`SHA256SUMS` 提供所有 Release 文件的 SHA-256 校验值。

## 安装与运行

需要 Python 3.11 或更高版本。使用 [uv](https://docs.astral.sh/uv/) 安装：

```console
uv tool install .
msr-tool
```

在源码仓库中也可以直接运行：

```console
uv sync --dev
uv run msr-tool
```

`python -m msr_music_tool` 提供相同入口。`msr-tool --help` 会显示当前系统实际使用的
配置文件路径。

## 配置

### 0.2.0 CLI 与下载后端

无参数启动 TUI；CLI 不打开交互界面：

```console
msr-tool search "月明人间" "其他歌曲" "https://monster-siren.hypergryph.com/music/953935"
msr-tool download 953935 --mode audio-lyrics --config ./config.toml --output ./Music
```

搜索接受多个名称（忽略大小写的子串）、六位歌曲 ID、官网桌面或移动歌曲链接；按输入顺序
合并并去重。下载仅接受六位 ASCII 数字 ID。模式为 `all`（音频、歌词、封面）、`audio`
（音频）、`audio-lyrics`（音频和歌词），默认 `all`；TUI 确认页也可以选择模式。

```toml
[download]
directory = "D:/Music"
backend = "http" # http 或 aria2
max_concurrent_downloads = 4 # 同时传输的资源数，1–32
progress_style = "=>" # =>、#、ILoveCandy

[rpc]
endpoint = "http://127.0.0.1:6800/jsonrpc"
secret = ""
poll_interval = 0.5 # 秒，0.1–60
# directory = "/shared/music" # 引擎侧映射目录，必须是绝对路径
```

`--config` 选择 TOML 文件，显式指定的文件不存在会报错；`--output` 临时覆盖目录，其相对
路径以启动目录为基准。两者也可用于无子命令的 TUI 启动。并发数、风格、RPC 参数只在
TOML 中配置；旧的仅含 `download.directory` 的配置仍然有效。启用 `rpc.directory` 映射
时，不允许 `--output` 指向其他目录。

CLI 搜索结果写入 stdout，下载状态和诊断写入 stderr；终端每秒最多刷新五次，重定向时
输出普通文本。退出码为成功 0、无匹配或下载失败 1、参数或配置错误 2、中断 130。
未知长度显示已下载字节和速度，不显示虚假的百分比。

RPC 连接由用户启动的 Aria2 兼容引擎，不自动启动或回退后端。音频、远端歌词和封面由
引擎下载；简介歌词与封面复制在本机处理。引擎目录必须可由本工具访问（本机或共享
目录）；完成后检查文件并原子替换。工具只限制自己的资源任务数，不修改引擎全局
设置。Ctrl+C 最多等待五秒取消本次任务；取消无法确认时会报告 GID 并保留临时文件，
不影响引擎内其他任务。暂不支持无共享磁盘的远端输出、跨进程恢复或提交后退出。

HTTP 使用异步网络并发和受限磁盘线程；CLI/TUI 实时展示资源字节、速度、ETA 和歌曲
完成数。同专辑封面只传输一次。传输重试会重置当前资源字节计数。

配置文件名为 `config.toml`，位于操作系统的用户配置目录下。文件内容如下：

```toml
[download]
directory = "D:/Music"
```

- 相对路径以 `config.toml` 所在目录为基准，支持 `~`。
- 未创建配置文件时，程序下载到启动目录下的 `MonsterSirenRecording`。
- 配置目录后，文件直接写入该目录，不再创建 `MonsterSirenRecording` 子目录。
- 配置文件存在但格式错误时，程序会报告路径和错误字段，不会使用默认值继续运行。

## 快捷键

| 页面 | 按键 | 操作 |
| --- | --- | --- |
| 专辑列表 | `/` | 搜索专辑名和歌曲名 |
| 专辑列表 | `Space` | 选择或取消整张专辑 |
| 专辑列表 | `Right` | 进入专辑详情 |
| 专辑列表 | `Enter` | 查看下载确认页 |
| 专辑列表 | `Esc` | 关闭搜索；未搜索时退出程序 |
| 专辑详情 | `Space` | 选择或取消单首歌曲 |
| 专辑详情 | `Left` | 返回专辑列表 |
| 确认页 | `Tab` / `Shift+Tab` | 在模式选择、“取消”和“确认下载”之间切换 |
| 确认页模式选择 | `Enter` / `Up` / `Down` | 打开模式列表并选择下载资源 |
| 确认页按钮 | `Left` / `Up` / `Right` / `Down` | 在“取消”和“确认下载”之间切换 |
| 确认页 | `Enter` | 执行当前选中的按钮 |
| 确认页 | `Esc` | 取消并保留已有选择 |
| 结果页 | `Left` / `Up` / `Right` / `Down` / `Tab` | 在“返回主界面”和“退出”之间切换 |
| 结果页 | `Enter` | 执行当前选中的按钮 |
| 结果页 | `Esc` | 返回专辑列表 |
| 结果页 | `q` | 退出程序；`q` 在其他页面不会退出 |
| 任意页面 | `Ctrl+C` | 退出程序并尽力取消本批次下载 |

专辑状态使用以下标记，并按该顺序排列：

- `●` 绿色：全选
- `◐` 黄色：部分选中
- `○` 灰色：未选择

同一状态内采用官网返回的专辑顺序。官网接口没有单独的发行时间字段，因此程序不会显示
从封面地址推测的日期。

## 下载规则

- 文件基本名为 `塞壬唱片-MSR - 歌曲名`，音频保持远端格式，不进行转码。
- 歌词保存为同名 `.lrc`。官网没有歌词时，使用专辑简介原文；简介为空时创建空文件。
- 封面保存为同名 `.jpg` 或 `.png`。同一批次内每张专辑只下载一次封面，再复制给各歌曲。
- 全部文件平铺在下载目录。官网存在重名曲目时，冲突项追加 `[专辑名-歌曲CID]`。
- Windows 禁用字符会替换为视觉相近的全角字符，以保证三个操作系统得到一致文件名。
- 已存在且长度与远端一致的资源会跳过；不完整文件会通过临时文件重新下载并原子替换。
- 一首歌的音频、歌词或封面任一失败时，该歌曲会继续保持选中，返回后可再次下载。

## 开发与测试

```console
uv sync --dev
uv run pre-commit install
uv run pre-commit run --all-files
uv run pytest
uv build
```

pre-commit 会运行 Ruff 检查和格式化，并通过 `commit-msg` hook 要求提交信息符合
[Conventional Commits](https://www.conventionalcommits.org/)，例如 `feat: add album search`。

构建当前系统的独立单文件程序：

```console
uv sync --group standalone
uv run --no-sync python scripts/build_standalone.py
uv run --no-sync python scripts/verify_standalone.py
```

完整发布验收（歌曲仅保存在临时目录，退出后清理）：

```console
uv run --no-sync python scripts/verify_standalone.py --live --report build/standalone-acceptance.json
uv run --no-sync python scripts/verify_standalone.py --live --wheel dist/msrmusictool-0.2.0-py3-none-any.whl --report build/wheel-acceptance.json
```

`tests/test_e2e.py` 使用实际本地 HTTP 服务及限速合成媒体验证 CLI/TUI 的完整下载和
实时进度。设置 `MSR_ARIA2_BINARY`、`MSR_ARIA_NEXT_BINARY` 为引擎可执行文件路径后，
完整测试还会启动真实引擎验证 RPC 下载、断连恢复、提交去重及取消隔离。未设置时
对应引擎验收明确跳过。CI 的 Windows RPC 作业使用固定的 Aria2 1.37.0 和 Aria2 Next
2.8.3；Release 构建执行独立程序的 `--live` 验收。

构建结果写入 `release/`。推送 `v*` 标签时，GitHub Actions 会分别构建 Windows、Linux、
macOS 独立程序，并自动创建 GitHub Release。

常规测试完全离线。设置 `MSR_RUN_LIVE_TESTS=1` 后可运行只读取索引和媒体 HEAD 的官网契约测试：

```console
MSR_RUN_LIVE_TESTS=1 uv run pytest -m integration
```

项目仅访问官网公开接口，仓库不包含音乐文件。

0.2.0 的实际测试环境、结果和证据见 [验收记录](docs/testing/0.2.0-acceptance.md)。
