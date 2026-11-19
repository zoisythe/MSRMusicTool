# msr-music-tool

`msr-music-tool` 是一个用 Go 编写的塞壬唱片音乐下载工具。它从
[Monster Siren Records](https://monster-siren.hypergryph.com/music) 获取专辑与歌曲索引，
支持终端内搜索、按专辑或单曲选择，以及批量下载音频、LRC 歌词和专辑封面。

## 下载与安装

从 [GitHub Releases](https://github.com/zoisythe/MSRMusicTool/releases) 下载并解压对应的 ZIP。
每个 ZIP 包含一个独立程序。发行说明列出 ZIP 的 SHA-256。

| 系统 | 可选架构 | 运行方式 |
| --- | --- | --- |
| Windows | `amd64`、`arm64`、`i686` | 双击 `msr-music-tool.exe` 打开终端 TUI，或从终端运行 |
| Linux | `amd64`、`arm64`、`armv7`、`i686` | 在终端运行 `./msr-music-tool` |
| macOS | `universal`（Intel 和 Apple silicon） | 在终端运行 `./msr-music-tool` |

Linux 和 macOS 如果解压后没有执行权限，可运行 `chmod +x msr-music-tool`。
macOS 产物使用 ad-hoc 签名，首次运行可能需要在系统安全设置中确认。

Windows 和 Linux 第一次打开 TUI 时，可将程序复制到 `~/.local/bin` 并加入当前用户 PATH。
确认后重新打开终端即可按命令名运行。也可以选择便携运行，之后在 TUI 设置中再次安装。
从 Python 版升级时，请手动下载 Go 版。新命令为 `msr-music-tool`，现有配置和曲库缓存可继续使用。

## CLI

无参数启动 TUI。子命令直接输出结果，适合脚本使用。

```console
msr-music-tool search "月明人间" "其他歌曲" "https://monster-siren.hypergryph.com/music/953935"
msr-music-tool download 953935 --mode audio-lyrics --config ./config.toml --output ./Music
```

搜索接受多个名称（忽略大小写的子串）、六位歌曲 ID、官网桌面或移动歌曲链接，按输入顺序
合并并去重。下载接受六位 ASCII 数字 ID。模式为 `all`（音频、歌词、封面）、
`audio`（音频）和 `audio-lyrics`（音频和歌词），默认 `all`。TUI 确认页也可以选择模式。

搜索结果写入 stdout，下载状态和诊断写入 stderr。退出码包括成功 0、无匹配或下载失败 1、
参数或配置错误 2、中断 130。CLI 和 TUI 显示资源字节进度、下载速度和歌曲完成数。
长度未知时显示 `?`，重定向到文件时使用普通文本。

## TUI 与配置

TUI 优先显示本机的 `catalog-v1.json`，随后后台刷新官网数据。离线时可浏览缓存，详情和下载
需要网络。刷新会保留仍有效的选择，查看详情、确认或下载期间收到的新曲库会在回到列表后应用。
损坏的缓存会触发重新读取官网。

| 页面 | 按键 | 操作 |
| --- | --- | --- |
| 专辑列表 | `/`、`Space`、`Right`、`Enter` | 搜索、选择整张专辑、查看详情、确认下载 |
| 专辑列表 | `r`、`F2`、`u` | 刷新曲库、打开设置、查看可用更新 |
| 专辑列表 | `Esc` | 取消详情或下载准备、退出搜索或退出程序 |
| 专辑详情 | `Space`、`Left` | 选择单曲、返回专辑列表 |
| 确认页 | `Tab` / `Shift+Tab`、`Enter`、`Esc` | 切换选项、确认、取消 |
| 结果页 | `Tab`、`Enter`、`Esc`、`q` | 切换按钮、执行按钮、返回、退出 |
| 设置 | `Ctrl+S`、`Esc` | 保存或放弃修改 |
| 设置 | `F3`、`F4`、`F5`、`F6` | 检查或查看更新、安装到用户目录、恢复预设色、浏览下载目录 |
| 任意页面 | `Ctrl+C` | 退出，并尽力取消本批次下载 |

专辑以全选 `●`、部分选中 `◐`、未选 `○` 的顺序排列，同一状态内沿用官网顺序。

可在设置页修改下载目录、后端、并发数和 RPC 参数，也可直接编辑 [config.example.toml](config.example.toml)
中的字段。用 `--help` 查看系统默认配置路径。显式指定的 `--config` 必须存在。相对配置目录以
TOML 文件所在目录为基准，支持 `~`。没有配置时下载到启动目录下的 `MonsterSirenRecording`。
`--output` 临时覆盖目录，其相对路径以启动目录为基准。设置会保存配置值，覆盖只影响本次运行。

TUI 可选择终端默认、深色、浅色和高对比主题。每个颜色项接受 `#RRGGBB`，留空时继承预设。
编辑即时预览，保存后生效。RPC 密钥在表单中遮蔽，配置文件仍以 TOML 保存。

启动时默认后台检查更新，可在设置中关闭。确认下载后，程序校验 GitHub 发布资产的 SHA-256，
解压并替换原路径的可执行文件，再尝试重启。替换完成后只保留新版。正式版接收正式更新，
预览版也可接收更高的预览版本。

## 下载规则

- 文件基本名为 `塞壬唱片-MSR - 歌曲名`，音频保持远端格式。歌词保存为同名 `.lrc`，官网没有歌词时
  使用专辑简介原文，简介为空时创建空文件。
- 封面使用同名文件。一个批次里每张专辑只传输一次封面，再复制给需要的歌曲。
- 文件平铺输出。重名曲目追加 `[专辑名-歌曲CID]`，Windows 禁用字符统一替换为相近的全角字符。
- 已存在且长度匹配的资源会跳过。下载先写入临时文件，完整后再替换目标。TUI 保留下载失败的歌曲供重试。
- 默认使用 HTTP，不需要另外启动下载器。Aria2 RPC 由用户配置并启动。使用远端目录映射时，
  引擎和工具须能访问同一共享目录，`--output` 必须与配置目录一致。工具只管理本批次的任务。
  Ctrl+C 最多等待五秒取消，无法确认取消时报告 GID 并保留该任务的临时文件。

## 开发与测试

```console
go run ./cmd/msr-music-tool
go test ./...
go vet ./...
go build ./cmd/msr-music-tool
```

`MSR_RUN_LIVE_TESTS=1` 启用只读的官网契约检查。设置 `MSR_ARIA2_BINARY` 和 `MSR_ARIA_NEXT_BINARY`
可用真实引擎执行 RPC 测试，包括断连恢复、提交去重和取消隔离。常规测试使用本地合成媒体。
GitHub Actions 在版本标签推送时构建八个 ZIP，从各 ZIP 解压执行冒烟测试，再创建 Release。
仓库不包含音乐文件。
