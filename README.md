# MSRMusicTool

MSRMusicTool 是一个基于 Python 和 Textual 的塞壬唱片音乐下载工具。它从
[Monster Siren Records](https://monster-siren.hypergryph.com/music) 获取专辑与歌曲索引，
支持终端内搜索、按专辑或单曲选择，以及批量下载音频、LRC 歌词和专辑封面。

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
| 确认页 | `Left` / `Up` / `Right` / `Down` / `Tab` | 在“取消”和“确认下载”之间切换 |
| 确认页 | `Enter` | 执行当前选中的按钮 |
| 确认页 | `Esc` | 取消并保留已有选择 |
| 结果页 | `Left` / `Up` / `Right` / `Down` / `Tab` | 在“返回主界面”和“退出”之间切换 |
| 结果页 | `Enter` | 执行当前选中的按钮 |
| 结果页 | `Esc` | 返回专辑列表 |
| 结果页 | `q` | 退出程序；`q` 在其他页面不会退出 |
| 任意页面 | `Ctrl+C` | 强制退出程序 |

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
uv run ruff check src tests
uv run pytest
uv build
```

常规测试完全离线。设置 `MSR_RUN_LIVE_TESTS=1` 后可运行只读取索引和媒体 HEAD 的官网契约测试：

```console
MSR_RUN_LIVE_TESTS=1 uv run pytest -m integration
```

项目仅访问官网公开接口，仓库不包含音乐文件。
