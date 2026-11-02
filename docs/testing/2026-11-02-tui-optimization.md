# TUI 响应性与曲库缓存验收

执行日期：2026-11-02，Asia/Shanghai。本机 Windows；项目环境 Python 3.13.18，
已安装工具 Python 3.15.2，均使用 Textual 8.2.8、Rich 15.0.0。

本报告的性能采样保留实现阶段的版本标识 0.2.1；发布准备已将版本标识更新为 0.2.2，
原始性能数据及其校验值保留不变。

已完成列表增量更新、后台详情和确认准备、统一 HTTP 生命周期，以及缓存优先的曲库
加载。已执行 `uv tool install --force . --system-certs` 更新本机 `msr-tool`，
16 个应用 Python/TCSS 文件与安装目录的 SHA-256 全部一致。

## 性能结果

固定输入为 302 张专辑、919 首歌曲，界面 100×32。以下新结果来自**最终安装的工具**，
重复五次取中位数；优化前数据为同一工具环境中三次测量的中位数。

| 操作 | 优化前 / ms | 优化后 / ms |
|---|---:|---:|
| 预加载曲库的目录初始化 | 926.77 | 51.66 |
| 选择专辑 | 660.23 | 73.31 |
| 取消选择 | 550.24 | 73.87 |
| 从详情返回列表 | 728.18 | 79.38 |
| 输入搜索 `a` | 347.41 | 78.46 |
| 清空搜索并恢复全部结果 | 718.38 | 78.22 |

新增磁盘缓存启动指标：从启动 App 到缓存目录可交互，中位数 **88.04 ms**；
交互期间 10 ms 心跳探针的五轮最大延迟为 **93.78 ms**。

| 验收目标 | 已安装工具 | 项目环境 |
|---|---:|---:|
| 缓存目录可交互 ≤300 ms | 88.04 ms | 97.01 ms |
| 选择专辑 ≤200 ms | 73.31 ms | 55.12 ms |
| 返回列表 ≤200 ms | 79.38 ms | 94.51 ms |
| 搜索恢复 ≤200 ms | 78.22 ms | 80.65 ms |
| 交互期间最大停顿 <100 ms | 93.78 ms | 74.05 ms |

每轮记录均确认首次只执行一次目录刷新。选择和返回的刷新函数本身通常约需 2 ms。

计时采用 Textual `run_test` 和 Pilot 实际按键调度，包含等待界面空闲的开销；
搜索包含 60 ms 防抖。缓存启动包含磁盘读取和目录绘制，使用阻塞的模拟网络确认显示
不等待网络；不计解释器、模块导入和缓存准备时间。心跳只覆盖交互阶段，排除断言和
退出卸载。表中数据是本机无终端输出的受控测量，真实终端交互另行验证。

原始数据：[已安装工具](assets/2026-11-02-tui-optimized/installed.json)、
[项目环境](assets/2026-11-02-tui-optimized/after.json)、
[优化前基线](assets/2026-11-02-tui/baseline-302.json)。

## 行为与回归

完整回归 **89 通过，0 失败，0 跳过**，包括官网契约检查，以及本地 HTTP 服务下的
Aria2 1.37.0、Aria2 Next 3.0.8 真实引擎验收。官网部分检查索引、详情和媒体响应；
完整资源下载由本地测试服务提供。见 [JUnit](assets/2026-11-02-tui-optimized/tests.xml)。

- 轻量 `OptionList` 保留 CID、选中排序、当前项和多行匹配提示；未变化的行不重新生成。
  行距直接包含在内容中，避免 Textual 8.2 的纵向组件内边距与可视行高度不一致。
  回归直接检查已绘制的专辑名称和匹配曲目，而不只检查选项数据。
- 搜索合并输入，Esc 立即清除等待中的搜索；搜索快捷键立即转移焦点，连续输入 `/a`
  不丢失首字符，输入框中的 `/` 和 `r` 仍作为文字。
- 详情和确认准备使用独立的后台操作任务，支持 Esc 取消、重复确认保护和过期结果丢弃；
  等待请求时可缩放或 Ctrl+C 退出。晚到的详情不会重新填充已失效的缓存。
- HTTP 客户端在线程构造并在 TUI 内复用。调用方取消不取消初始化；普通退出和 Ctrl+C
  均等待任务清理后关闭客户端，重复清理安全。
- 首次无缓存和缓存损坏时读取官网；旧缓存离线仍可浏览并显示更新时间。后台更新保留
  有效选择，移除失效歌曲并提示，详情及确认/下载期间延后应用。最新刷新覆盖旧的
  待应用快照；缓存保存失败不影响浏览，写入采用临时文件和原子替换。
- 显式注入曲库时跳过磁盘缓存和自动刷新，测试使用隔离缓存位置。

CodeIntel 项目 ty/Ruff 检查完成，**0 错误、0 警告**；Ruff 格式检查及离线锁文件检查
通过，见 [检查记录](assets/2026-11-02-tui-optimized/checks.json)。

实际运行 Windows PowerShell PTY 中的 `msr-tool`，确认缓存曲库和文字显示、选择、
详情、返回、连续 `/a` 搜索及 Esc 退出均正常，退出码为 0。
见 [终端检查](assets/2026-11-02-tui-optimized/terminal.json)。

## 复现

```powershell
uv sync --locked --group standalone
uv run --no-sync python scripts/benchmark_tui.py --repeat 5

$taskPython = Join-Path (uv tool dir) 'msrmusictool\Scripts\python.exe'
uv run --no-project --python $taskPython python scripts/benchmark_tui.py --repeat 5 `
  --output build/tui-performance/installed.json

$env:MSR_ARIA2_BINARY = '本机 aria2c.exe 的绝对路径'
$env:MSR_ARIA_NEXT_BINARY = '本机 aria2-next.exe 的绝对路径'
$env:MSR_RUN_LIVE_TESTS = '1'
uv run --locked --no-sync pytest -q --junitxml=build/tui-performance/tests.xml
uv run --no-sync ruff check src scripts tests
uv run --no-sync ruff format --check src scripts tests
uv lock --check --offline
```

基准脚本默认读取已归档的固定曲库，不下载音乐；临时缓存位于测试临时目录。
脚本快照、安装依赖、引擎版本和应用校验值分别保存在
[脚本](assets/2026-11-02-tui-optimized/benchmark_tui.py)和
[环境记录](assets/2026-11-02-tui-optimized/environment.json)中。
全部证据的 SHA-256 见 [清单](assets/2026-11-02-tui-optimized/manifest.json)。

## 0.2.2 发布前验证（2026-11-03，Asia/Shanghai）

- 更新版本标识后，完整回归再次通过：89 通过、0 失败、0 跳过，包含官网契约及两种真实
  引擎。见 [发布前 JUnit](assets/0.2.2/tests.xml)。
- `uv build --offline` 构建 wheel 和源码包成功；独立安装 0.2.2 wheel 的 7 项检查通过，
  包含 CLI 参数、官网搜索、完整资源下载和仅音频重复下载跳过。
  见 [wheel 验收](assets/0.2.2/wheel.json)。下载内容在临时目录中验收后清理，保留文件名、
  大小和校验值。
- 全仓 Ruff 检查和格式检查、CodeIntel ty/Ruff 项目检查、锁文件检查与 pre-commit 均通过。
  发布前证据的 SHA-256 见 [清单](assets/0.2.2/manifest.json)。
- 加载页的隐藏按钮不自动获得焦点；错误页、确认页和结果页同步切换输入焦点。
  键盘回归显式等待后台失败状态就绪，并检查按钮操作返回时的输入目标，避免调度时序干扰。
- 异步切页测试在新页面完成挂载及待处理消息后再退出，避免测试卸载与 Header 的挂载回调竞态。
  补充验证的 11 项异步和缓存回归通过，见 [JUnit](assets/0.2.2/async-regressions.xml)。
