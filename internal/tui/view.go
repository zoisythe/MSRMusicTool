package tui

import (
	"fmt"
	"image/color"
	"os"
	"strings"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"
	"github.com/zoisythe/MSRMusicTool/internal/core"
	"github.com/zoisythe/MSRMusicTool/internal/install"
)

type palette map[string]string

var themes = map[string]palette{
	"terminal": {"muted": "8", "accent": "6", "selection": "6", "success": "2", "warning": "3", "error": "1"},
	"dark":     {"background": "#18202E", "text": "#E7EDF5", "muted": "#A6B4C6", "accent": "#80CAFD", "selection": "#A6DCFF", "success": "#74DFA2", "warning": "#FFD078", "error": "#FFA1A7"},
	"light":    {"background": "#F3F6FA", "text": "#182735", "muted": "#4C5B70", "accent": "#004E7A", "selection": "#005275", "success": "#146238", "warning": "#7C4500", "error": "#A82B42"},
	"contrast": {"background": "#101010", "text": "#FFFFFF", "muted": "#DDDDDD", "accent": "#66EBFF", "selection": "#FFF06A", "success": "#76FF76", "warning": "#FFF06A", "error": "#FF8298"},
}

func selectedPalette(config core.TUIConfig) palette {
	p := make(palette)
	if _, noColor := os.LookupEnv("NO_COLOR"); noColor || os.Getenv("TERM") == "dumb" {
		return p
	}
	for k, v := range themes[config.Theme] {
		p[k] = v
	}
	for k, v := range config.Colors {
		if core.HexColor.MatchString(v) {
			p[k] = v
		}
	}
	return p
}

func (p palette) color(role string) color.Color {
	if value := p[role]; value != "" {
		return lipgloss.Color(value)
	}
	return nil
}

func (p palette) style(role string, bold bool) lipgloss.Style {
	style := lipgloss.NewStyle().Bold(bold)
	if c := p.color(role); c != nil {
		style = style.Foreground(c)
	}
	return style
}

func (m *model) View() tea.View {
	config := m.config.TUI
	if m.form != nil {
		config = m.form.colors(config, true)
	}
	p := selectedPalette(config)
	fillBar := func(value string) string { return p.style("success", false).Render(value) }
	width := max(1, m.width)
	trunc := func(s string) string { return ansi.Truncate(s, width, "…") }
	var lines []string
	line := func(value string) { lines = append(lines, trunc(value)) }
	row := func(index, active int, label string) {
		if index == active {
			line(p.style("selection", true).Render("› " + safe(label)))
		} else {
			line("  " + safe(label))
		}
	}
	title := map[page]string{catalogPage: "曲库", detailPage: "专辑详情", confirmPage: "确认下载", downloadPage: "正在下载", resultPage: "下载结果", settingsPage: "设置", browsePage: "选择下载目录", installPage: "安装到用户目录", overwritePage: "覆盖已有程序", updatePage: "更新可用", updatingPage: "下载更新"}
	line(p.style("accent", true).Render(core.AppName + "  ·  " + title[m.view]))
	line("")
	var help string
	switch m.view {
	case catalogPage:
		status := m.catalogState
		if !m.snapshotAt.IsZero() {
			status += " · " + m.snapshotAt.Local().Format("01-02 15:04")
		}
		line(p.style("muted", false).Render(m.debugCount() + "  |  " + status))
		if m.searchOpen {
			input := m.search
			input.SetWidth(max(1, width-4))
			line("/ " + input.View())
		} else if m.query != "" {
			line("搜索：" + safe(m.query))
		}
		if m.pending != "" {
			line(p.style("warning", true).Render(m.pending + "… · Esc 取消"))
		}
		if len(m.rows) == 0 && m.catalog.AlbumByID != nil {
			line("没有匹配的专辑")
		}
		if m.updateCandidate != nil {
			line(p.style("accent", true).Render("U 更新至 " + safe(m.updateCandidate.Tag)))
		}
		start, end := window(m.cursor, len(m.rows), max(1, m.height-len(lines)-3))
		for i := start; i < end; i++ {
			value := m.rows[i]
			symbol, role := "○", "muted"
			switch value.State() {
			case 2:
				symbol, role = "●", "success"
			case 1:
				symbol, role = "◐", "warning"
			}
			title := safe(value.Album.Name)
			if len(value.Songs) > 0 {
				var songs []string
				for _, song := range value.Songs[:min(2, len(value.Songs))] {
					songs = append(songs, safe(song.Name))
				}
				title += " · " + strings.Join(songs, " / ")
			}
			prefix := "  "
			if i == m.cursor {
				prefix = p.style("selection", true).Render("› ")
			}
			line(prefix + p.style(role, false).Render(symbol) + " " + title + fmt.Sprintf("  %d/%d", value.Count, value.Total))
		}
		help = "/ 搜索  ↑↓ 移动  Space 选择  → 详情  Enter 下载  r 刷新  F2 设置  Esc 返回"
	case detailPage:
		line(p.style("accent", true).Render(safe(m.detail.Name)))
		if m.detail.Intro != "" {
			line(p.style("muted", false).Render(safe(m.detail.Intro)))
		}
		start, end := window(m.detailCursor, len(m.detail.Songs), max(1, m.height-len(lines)-3))
		for i := start; i < end; i++ {
			song := m.detail.Songs[i]
			symbol := "○"
			if m.selected[song.CID] {
				symbol = "●"
			}
			row(i, m.detailCursor, symbol+" "+song.Name)
		}
		help = "↑↓ 移动  Space 选择  ← / Esc 返回"
	case confirmPage:
		line(fmt.Sprintf("共 %d 首歌 · %s", m.prepared.Count(), safe(m.config.Output)))
		line("模式：" + modes[m.mode])
		if m.modeOpen {
			for i, mode := range modes {
				row(i, m.mode, mode)
			}
			help = "↑↓ 选择模式  Enter 确认  Esc 返回"
		} else {
			line(buttons(p, m.button, "模式", "取消", "确认下载"))
			remaining := max(0, m.height-len(lines)-3)
			for _, album := range m.prepared.Albums[:min(remaining, len(m.prepared.Albums))] {
				line(fmt.Sprintf("  %s · %d 首", safe(album.Detail.Name), len(album.Songs)))
			}
			help = "Tab / Shift+Tab 切换  Enter 确认  Esc 返回"
		}
	case downloadPage:
		current := m.tracker.Snapshot()
		line(current.Text(m.prepared.Count()))
		line(current.Bar(min(48, width), m.frame, fillBar))
		if current.Latest.Label != "" {
			line(safe(current.Latest.Label) + " · " + core.StatusLabel(current.Latest.Status))
		}
		if current.Latest.Error != "" {
			line(p.style("warning", false).Render(safe(current.Latest.Error)))
		}
		help = "Ctrl+C 取消本批并退出"
	case resultPage:
		line(buttons(p, m.button, "返回曲库", "退出"))
		if m.batch.Err != nil {
			line(p.style("error", false).Render(safe(m.batch.Err.Error())))
		}
		if len(m.batch.Unresolved) > 0 {
			line(p.style("warning", false).Render("取消无法确认，保留临时文件 · GID " + strings.Join(m.batch.Unresolved, ", ")))
		}
		succeeded, skipped, failed := 0, 0, 0
		for _, song := range m.batch.Songs {
			switch song.Outcome {
			case "success":
				succeeded++
			case "skipped":
				skipped++
			default:
				failed++
			}
		}
		line(fmt.Sprintf("完成 %d · 跳过 %d · 失败 %d", succeeded, skipped, failed))
		for _, song := range m.batch.Songs {
			if len(lines) >= m.height-3 {
				break
			}
			text, role := "已完成", "success"
			switch song.Outcome {
			case "skipped":
				text, role = "已跳过", "muted"
			case "failed":
				text, role = "失败", "error"
				if len(song.Errors) > 0 {
					text += " · " + safe(song.Errors[0])
				}
			}
			line(safe(song.Song.Name) + "  " + p.style(role, false).Render(text))
		}
		help = "Tab 切换  Enter 确认  Esc 返回  q 退出"
	case settingsPage:
		line(p.style("muted", false).Render(safe(m.config.Path)))
		if m.config.Override != "" {
			line(p.style("warning", false).Render("本次输出覆盖：" + safe(m.config.Override)))
		}
		f := m.form
		start, end := window(f.index, len(f.fields)+1, max(1, m.height-len(lines)-4))
		for i := start; i < end; i++ {
			if i == len(f.fields) {
				row(i, f.index, "保存设置")
				continue
			}
			field := f.fields[i]
			caption := field.label + "  "
			prefix := "  "
			if i == f.index {
				prefix = p.style("selection", true).Render("› ")
			}
			if len(field.options) > 0 {
				line(prefix + caption + "〈 " + field.options[field.value] + " 〉")
			} else {
				input := field.input
				input.SetWidth(max(1, width-lipgloss.Width(caption)-4))
				line(prefix + caption + input.View())
			}
		}
		line(p.style("muted", false).Render("F3 更新  F4 安装  F5 恢复预设色  F6 浏览目录"))
		help = "Tab 移动  ←→ 切换选项  Ctrl+S 保存  Esc 取消"
	case browsePage:
		f := m.form
		line(safe(f.directory))
		start, end := window(f.directoryCursor, len(f.directories), max(1, m.height-len(lines)-3))
		for i := start; i < end; i++ {
			row(i, f.directoryCursor, f.directories[i]+"/")
		}
		help = "← 上一级  → 打开目录  Enter 使用当前目录  Esc 取消"
	case installPage, overwritePage:
		target, _ := install.Target()
		if m.view == installPage {
			line("将程序复制到以下位置并加入用户 PATH？")
		} else {
			line("此处已有不同的程序。是否覆盖？")
		}
		line(safe(target))
		line(buttons(p, m.button, "取消", "确认"))
		help = "Tab 切换  Enter 确认  Esc 取消"
	case updatePage:
		if m.updateCandidate != nil {
			line("可更新到 " + safe(m.updateCandidate.Tag))
			line(safe(m.updateCandidate.URL))
		}
		line("下载并替换当前程序？")
		line(buttons(p, m.button, "取消", "下载并替换"))
		help = "Tab 切换  Enter 确认  Esc 取消"
	case updatingPage:
		current := m.updateProgress.Snapshot()
		line(core.Bytes(float64(current.Done)) + " / " + core.Bytes(float64(current.Total)))
		line(current.Bar(min(48, width), m.frame, fillBar))
		help = "Esc 取消下载"
	}
	if m.status != "" {
		line(p.style("muted", false).Render(safe(m.status)))
	}
	line(p.style("muted", false).Render(help))
	v := tea.NewView(strings.Join(lines, "\n"))
	v.AltScreen = true
	v.WindowTitle = core.AppName
	v.BackgroundColor, v.ForegroundColor = p.color("background"), p.color("text")
	return v
}

func buttons(p palette, current int, labels ...string) string {
	parts := make([]string, len(labels))
	for i, label := range labels {
		parts[i] = "[ " + label + " ]"
		if i == current {
			parts[i] = p.style("selection", true).Render("› " + parts[i])
		}
	}
	return strings.Join(parts, "  ")
}

func window(cursor, count, visible int) (int, int) {
	start := max(0, cursor-visible+1)
	return start, min(count, start+visible)
}
