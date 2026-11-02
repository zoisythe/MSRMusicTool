# TUI 卡顿诊断

诊断日期：2026-11-02，Asia/Shanghai。

这是优化前的诊断记录，基线源码为 `c629269152a7405eb463ca74e87e7cbcf68a3227`。
修复后的实现和验收见 [TUI 优化验收](2026-11-02-tui-optimization.md)。

已复现启动、选择专辑、搜索和返回列表的卡顿。主要原因是列表刷新时销毁并重建全部
`ListItem` / `Label`，触发大量挂载、样式解析和布局；`grey50` 的颜色解析回退、首次重复
构建和按键处理链内的网络等待进一步增加延迟。本次完成诊断和内存中的对照实验，
应用源码及已安装的工具均未修改。

用户通过 `uv tool install .` 安装，再运行 `msr-tool`。已核对该工具环境：MSRMusicTool
0.2.1、Python 3.15.2、Textual 8.2.8、Rich 15.0.0。项目虚拟环境为 Python 3.13.18；
主要 A/B 数据均来自已安装的工具环境。安装目录与仓库的 15 个 Python/TCSS 文件
SHA-256 全部一致，见[环境记录](assets/2026-11-02-tui/environment.json)。

## 同一曲库下的对照

官网索引为 302 张专辑、919 首歌曲，已保存为[固定输入](assets/2026-11-02-tui/catalog.json)。
使用 Textual `run_test` 在 100×32 的界面中发送实际按键；预加载索引和专辑详情，隔离
网络。下表为每种方案独立运行 3 次的中位数，单位 ms。

| 操作 | 原实现 | 仅替换颜色 | 替换颜色并复用列表条目 |
|---|---:|---:|---:|
| 首次列表可用 | 926.77 | 904.86 | 649.55 |
| 空格选中专辑 | 660.23 | 599.51 | 122.31 |
| 空格取消选择 | 550.24 | 439.62 | 107.09 |
| 从详情返回列表 | 728.18 | 774.99 | 170.47 |
| 打开搜索框 | 257.93 | 279.02 | 256.76 |
| 输入 `a` | 347.41 | 303.39 | 289.08 |
| 删除搜索内容、恢复完整列表 | 718.38 | 646.70 | 512.29 |

原始结果：[原实现](assets/2026-11-02-tui/baseline-302.json)、
[颜色替换](assets/2026-11-02-tui/hex-302.json)、
[颜色替换和条目复用](assets/2026-11-02-tui/hex-reuse-302.json)。

这是无终端输出的按键处理与界面收敛耗时，包含 Pilot 等待空闲/动画的开销，不等于
用户终端中精确的输入到显示延迟。对照实验检查了列表 ID、排序及标签文本。复用方案
仅处理结果成员相同的刷新；搜索改变结果集合时仍调用原实现，因此搜索仍有明显开销。
原始 JSON 中的心跳指标覆盖整个诊断会话，包括校验和卸载，用于发现阻塞，不用于
标注单个按键耗时。两张专辑的[同环境对照](assets/2026-11-02-tui/baseline-2.json)中，
选择和返回的刷新函数本身约需 4 ms。

## 原因和证据

1. **每次更新都重建全部控件。**
   [refresh_albums](https://github.com/zoisythe/MSRMusicTool/blob/c629269152a7405eb463ca74e87e7cbcf68a3227/src/msr_music_tool/app.py#L174) 无条件执行 `view.clear()` 和
   `view.extend(...)`。空格选择、搜索输入、关闭空搜索框、从详情或结果返回均进入此路径。
   302 张专辑对应 604 个列表条目及标签控件，即使屏幕只显示少数行也全部挂载。
   条目 `height: auto` 使布局还需测量内容。内存复用实验保留排序和选择语义后，选择的
   刷新函数从数百 ms 降到约 1.5–1.9 ms；包括后续界面处理的整体耗时由 660 ms 降至
   122 ms。`album_matches` 的计算不是主要热点。

   [cProfile 摘要](assets/2026-11-02-tui/profile-summary.txt)显示热点集中在
   `_refresh_layout`、`_get_box_model`、`from_markup` 和控件注册。该次 profile 使用
   项目 Python 3.13.18，仅用于定位调用开销；带 profiler 的绝对耗时未混入上表。

2. **`grey50` 触发重复的颜色纠错计算。**
   [专辑标签](https://github.com/zoisythe/MSRMusicTool/blob/c629269152a7405eb463ca74e87e7cbcf68a3227/src/msr_music_tool/app.py#L628)及
   [歌曲标签](https://github.com/zoisythe/MSRMusicTool/blob/c629269152a7405eb463ca74e87e7cbcf68a3227/src/msr_music_tool/app.py#L642)使用 Rich 色名 `grey50`。
   Textual 8 的 `Color.parse` 不识别此名称，先调用 `get_suggestion` /
   `difflib.get_close_matches`，再抛出解析异常；markup 的标签规范化捕获异常并保留原标签。
   失败结果未进入成功解析缓存，因此反复发生。正常显示并不能消除这段额外计算。

   在已安装环境中解析 604 条标签，`grey50` 需要 **161.93 ms**，触发 1,208 次颜色建议
   查询；等价 RGB 颜色 `#808080` 需要 **13.23 ms**，建议查询为零。
   见[颜色解析对照](assets/2026-11-02-tui/color-markup.json)。完整诊断操作中原实现累计
   触发 4,046 次 `grey50` 建议查询，替换颜色后为零。只替换颜色未消除列表重建和布局，
   因此整体 A/B 的改善较小，部分操作仍有运行波动。

3. **首次进入目录时构建两次列表。**
   [on_mount / on_screen_resume](https://github.com/zoisythe/MSRMusicTool/blob/c629269152a7405eb463ca74e87e7cbcf68a3227/src/msr_music_tool/app.py#L150) 都调用
   `refresh_albums`。采样确认两者在第一次进入时依次执行；即使数据不变也再次清空重建。
   单独跳过首次 resume 的刷新，2 次运行的启动中位数为 **636.22 ms**，原始记录见
   [单次构建对照](assets/2026-11-02-tui/once-302.json)。这里确认的是重复构建，
   不代表两次都完成了一整帧显示。

4. **详情请求占住按键处理链。**
   [action_open_detail](https://github.com/zoisythe/MSRMusicTool/blob/c629269152a7405eb463ca74e87e7cbcf68a3227/src/msr_music_tool/app.py#L233) 直接等待
   `get_album_detail`。使用单张专辑，将请求替换为可控的异步等待，然后发送 Ctrl+C：
   等待约 **156 ms** 后退出操作仍未处理，释放详情请求后才处理，约 **185 ms** 时退出。
   这证明网络等待会使后续输入排队，即使事件循环仍能运行其他协程。
   [确认下载](https://github.com/zoisythe/MSRMusicTool/blob/c629269152a7405eb463ca74e87e7cbcf68a3227/src/msr_music_tool/app.py#L244)同样直接等待准备操作；本轮受控输入实验
   覆盖的是详情路径，确认路径的处理建议来自相同调用结构。

## 启动分解和修改顺序

在工具环境中走正常联网加载路径的一次记录：模块导入 **361.28 ms**；从启动 App 到
目录可用 **1,318.64 ms**，其中首个 HTTP 客户端同步构造 **254.65 ms**，两条并发索引
请求分别约 **119 ms / 270 ms**。首次未缓存详情请求约 **116 ms**。见
[启动及输入记录](assets/2026-11-02-tui/startup-and-input.json)。这些阶段有并发与重叠，
不能把所有分项相加。网络条件会影响这些单次数据；无网络的 A/B 已独立复现主要卡顿。

建议按以下顺序修改：

1. 复用专辑及歌曲行，仅更新变化的标签和必要的排序；去掉首次 resume 和空搜索关闭时
   的无效重建。歌曲条目已有 `set_selected`，当前选择操作仍重建歌曲列表。
2. 将 `grey50` 替换为 Textual 可直接解析的等价颜色。
3. 将详情和确认准备移入 worker，提供加载状态、取消及重复触发保护，让按键处理及时返回。
4. 用虚拟化列表减少完整曲库的布局成本，并合并搜索输入更新。条目复用实验后，打开搜索
   和恢复全部结果仍有约 0.26 s / 0.51 s 开销，这一步仍然必要。
5. 将 HTTP 客户端的同步初始化移出界面事件循环并在应用生命周期内复用；需要进一步
   缩短首开时间时，再增加曲库本地缓存和后台刷新。

相关 `app.py`、`catalog.py`、`service.py` 的 CodeIntel LSP 与 Ruff 检查完成，0 错误、
0 警告。诊断脚本的 Ruff 检查与格式检查通过。本次仅请求索引和专辑元数据，
未变更应用功能，因此未重复执行完整回归套件。

## 复现与证据

以下命令使用已安装工具的解释器，诊断覆盖仅在当前进程内生效。主脚本可改用
`uv run --no-sync python` 在项目虚拟环境运行，但应分别记录解释器版本。
本节脚本配合上面的基线源码使用；修复后的性能复现入口为 `scripts/benchmark_tui.py`。

```powershell
$taskPython = Join-Path (uv tool dir) 'msrmusictool\Scripts\python.exe'
$taskEvidence = 'docs/testing/assets/2026-11-02-tui'
uv run --no-project --python $taskPython python "$taskEvidence/profile_tui.py" --repeat 3
uv run --no-project --python $taskPython python "$taskEvidence/profile_tui.py" --variant hex --repeat 3
uv run --no-project --python $taskPython python "$taskEvidence/profile_tui.py" --variant once --repeat 2
uv run --no-project --python $taskPython python "$taskEvidence/profile_tui.py" --variant hex-reuse --repeat 3
uv run --no-project --python $taskPython python "$taskEvidence/profile_tui.py" --count 2 --repeat 2
uv run --no-project --python $taskPython python "$taskEvidence/color_markup.py"
uv run --no-project --python $taskPython python "$taskEvidence/startup_and_input.py"
```

`profile_tui.py` 默认复用已归档的曲库，不访问网络；`--live` 会重新获取索引。
`startup_and_input.py` 请求官网索引和首张专辑的详情，不下载音频。
重跑会更新同目录的结果 JSON；历史文件及 SHA-256 见
[证据清单](assets/2026-11-02-tui/manifest.json)。
