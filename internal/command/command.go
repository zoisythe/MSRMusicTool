package command

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/zoisythe/MSRMusicTool/internal/core"
	"github.com/zoisythe/MSRMusicTool/internal/tui"
	"github.com/zoisythe/MSRMusicTool/internal/update"
)

type Options struct {
	Action string
	Mode   string
	Config string
	Output string
	Items  []string
}

func Parse(args []string) (Options, error) {
	o := Options{Mode: core.ModeAll}
	positional := false
	modeOption := false
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if !positional {
			if arg == "--" {
				positional = true
				continue
			}
			key, value, inline := strings.Cut(arg, "=")
			switch key {
			case "--help", "-h", "--version":
				if inline {
					return o, fmt.Errorf("不接受参数：%s", arg)
				}
				if key == "--version" {
					o.Action = "version"
				} else {
					if o.Action == "" {
						o.Action = "help"
					} else {
						o.Action = "help-" + o.Action
					}
				}
				return o, nil
			case "--config", "--output", "--mode":
				if !inline {
					if i+1 >= len(args) || strings.HasPrefix(args[i+1], "-") {
						return o, fmt.Errorf("%s 缺少值", key)
					}
					i++
					value = args[i]
				}
				if value == "" {
					return o, fmt.Errorf("%s 缺少值", key)
				}
				switch key {
				case "--config":
					o.Config = value
				case "--output":
					o.Output = value
				case "--mode":
					o.Mode = value
					modeOption = true
				}
				continue
			default:
				if strings.HasPrefix(arg, "-") {
					return o, fmt.Errorf("未知参数：%s", arg)
				}
			}
		}
		if o.Action == "" {
			if arg != "search" && arg != "download" {
				return o, fmt.Errorf("未知子命令：%s", arg)
			}
			o.Action = arg
		} else {
			o.Items = append(o.Items, arg)
		}
	}
	if modeOption && o.Action != "download" {
		return o, errors.New("--mode 仅用于 download")
	}
	if o.Action != "" && len(o.Items) == 0 {
		return o, errors.New("至少需要一个搜索词或歌曲 ID")
	}
	switch o.Action {
	case "download":
		if !core.ValidMode(o.Mode) {
			return o, fmt.Errorf("无效的下载模式：%s", o.Mode)
		}
		for _, item := range o.Items {
			if !core.SongID.MatchString(item) {
				return o, fmt.Errorf("download 仅接受六位 ASCII 数字歌曲 ID：%s", item)
			}
		}
	case "search":
		for _, item := range o.Items {
			if strings.TrimSpace(item) == "" {
				return o, errors.New("搜索内容不能为空")
			}
			if _, err := core.QueryID(item); err != nil {
				return o, err
			}
		}
	}
	return o, nil
}

func usage(action string) string {
	switch action {
	case "help-search":
		return "用法：" + core.AppName + " search [--config PATH] [--output DIR] <名称|ID|官网链接>...\n"
	case "help-download":
		return "用法：" + core.AppName + " download [--config PATH] [--output DIR] [--mode all|audio|audio-lyrics] <ID>...\n"
	default:
		return "用法：" + core.AppName + " [--config PATH] [--output DIR] [search|download]\n\n" +
			"  无子命令    打开 TUI\n  search      搜索歌曲名、ID 或官网链接\n  download    按六位歌曲 ID 下载\n  --version   显示版本\n" +
			"\n默认配置：" + core.ConfigPath() + "\n"
	}
}

type IO struct {
	In               io.Reader
	Out, Err         io.Writer
	Interactive      bool
	ProgressTerminal bool
}

func Run(ctx context.Context, args []string, streams IO, site *core.Site, version string) int {
	if len(args) > 0 && args[0] == "--internal-update" {
		if err := update.RunHelper(args[1:]); err != nil {
			update.ReportHelperError(err)
			return 1
		}
		return 0
	}
	opts, err := Parse(args)
	if err != nil {
		fmt.Fprint(streams.Err, usage(""))
		fmt.Fprintln(streams.Err, err)
		return 2
	}
	if opts.Action == "version" {
		fmt.Fprintf(streams.Out, "%s %s\n", core.AppName, version)
		return 0
	}
	if strings.HasPrefix(opts.Action, "help") {
		fmt.Fprint(streams.Out, usage(opts.Action))
		return 0
	}
	settings, err := core.LoadSettings(opts.Config, opts.Output)
	if err != nil {
		fmt.Fprintln(streams.Err, "配置错误：", err)
		return 2
	}
	if opts.Action == "" {
		if !streams.Interactive {
			fmt.Fprintln(streams.Err, "请在终端启动 TUI，或使用 search / download")
			return 2
		}
		result, err := tui.Run(ctx, streams.In, streams.Out, site, settings, version)
		for _, gid := range result.Unresolved {
			printGID(streams.Err, gid)
		}
		if result.Interrupted {
			return 130
		}
		if err == nil && result.Staged != nil {
			err = update.Apply(*result.Staged, args)
		}
		if err != nil {
			fmt.Fprintln(streams.Err, "操作失败：", err)
			return 1
		}
		return 0
	}
	code := cli(ctx, opts, settings, streams, site)
	if ctx.Err() != nil {
		fmt.Fprintln(streams.Err, "已中断")
		return 130
	}
	return code
}

func cli(ctx context.Context, opts Options, settings core.Settings, streams IO, site *core.Site) int {
	catalog, err := site.Catalog(ctx)
	if err != nil {
		fmt.Fprintln(streams.Err, "操作失败：", err)
		return 1
	}
	if opts.Action == "search" {
		results, missing := catalog.Search(opts.Items)
		if len(results) > 0 {
			fmt.Fprintln(streams.Out, "ID\t歌曲\t专辑\t艺人\t链接")
		}
		for _, song := range results {
			album := catalog.AlbumByID[song.AlbumCID].Name
			if album == "" {
				album = song.AlbumCID
			}
			fields := []string{song.CID, song.Name, album, strings.Join(song.Artists, " / "), core.SiteURL + "/music/" + song.CID}
			for i := range fields {
				fields[i] = strings.NewReplacer("\t", " ", "\n", " ", "\r", " ").Replace(fields[i])
			}
			fmt.Fprintln(streams.Out, strings.Join(fields, "\t"))
		}
		for _, item := range missing {
			fmt.Fprintln(streams.Err, "无匹配："+item)
		}
		if len(missing) > 0 {
			return 1
		}
		return 0
	}
	prepared := site.Prepare(ctx, catalog, opts.Items, settings.Download.Concurrency, nil)
	count := prepared.Count()
	tracker := &core.Tracker{}
	tracker.Update(core.Progress{Completed: len(prepared.Failures)})
	last := time.Now()
	frame := 0
	render := func() {
		snapshot := tracker.Snapshot()
		if streams.ProgressTerminal {
			bar := snapshot.Bar(20, frame, func(s string) string { return "\x1b[32m" + s + "\x1b[0m" })
			fmt.Fprintf(streams.Err, "\r\x1b[K%s  %s", bar, snapshot.Text(count))
			frame++
		} else {
			fmt.Fprintln(streams.Err, snapshot.Text(count))
		}
	}
	if len(prepared.Albums) > 0 {
		render()
	}
	result := site.Download(ctx, settings, catalog, prepared, opts.Mode, func(event core.Progress) {
		tracker.Update(event)
		if time.Since(last) >= 200*time.Millisecond {
			render()
			last = time.Now()
		}
		if !streams.ProgressTerminal && (event.Resource.Status == "complete" || event.Resource.Status == "skipped" || event.Resource.Status == "failed") {
			fmt.Fprintf(streams.Err, "%s：%s\n", event.Resource.Label, core.StatusLabel(event.Resource.Status))
		}
	})
	if len(prepared.Albums) > 0 {
		render()
		if streams.ProgressTerminal {
			fmt.Fprintln(streams.Err)
		}
	}
	failed := result.Err != nil
	if result.Err != nil {
		fmt.Fprintln(streams.Err, "操作失败：", result.Err)
	}
	for _, gid := range result.Unresolved {
		printGID(streams.Err, gid)
	}
	for _, song := range result.Songs {
		fmt.Fprintf(streams.Err, "%s %s：%s\n", song.Song.CID, song.Song.Name, song.Outcome)
		if song.Outcome == "failed" {
			failed = true
		}
		for _, issue := range song.Errors {
			fmt.Fprintln(streams.Err, " ", issue)
		}
	}
	if failed {
		return 1
	}
	return 0
}

func printGID(w io.Writer, gid string) {
	fmt.Fprintln(w, "RPC 取消无法确认，保留临时文件，GID："+gid)
}
