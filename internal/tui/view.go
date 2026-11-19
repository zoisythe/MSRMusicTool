package tui

import (
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
	"classic": {
		"background": "#121212", "text": "#E0E0E0", "muted": "#A0A0A0", "accent": "#0178D4", "selection": "#FFFFFF",
		"success": "#008000", "warning": "#FFFF00", "error": "#FF6767", "chrome": "#242F38", "panel": "#272727",
		"highlight": "#5C4628", "shortcut": "#FFA624", "divider": "#0053AA", "unselected": "#808080",
		"scrollTrack": "#000000", "scrollThumb": "#003054", "section": "#3A3A3A", "control": "#1E1E1E",
	},
	"dark": {
		"background": "#18202E", "text": "#E7EDF5", "muted": "#A6B4C6", "accent": "#80CAFD", "selection": "#A6DCFF",
		"success": "#74DFA2", "warning": "#FFD078", "error": "#FFA1A7", "chrome": "#263447", "panel": "#202C3C",
		"highlight": "#354C63", "scrollTrack": "#18202E", "scrollThumb": "#537898",
	},
	"light": {
		"background": "#F3F6FA", "text": "#182735", "muted": "#4C5B70", "accent": "#004E7A", "selection": "#005275",
		"success": "#146238", "warning": "#7C4500", "error": "#A82B42", "chrome": "#DAE5EF", "panel": "#E4EBF2",
		"highlight": "#BDD8EC", "scrollTrack": "#D4DEE8", "scrollThumb": "#739AB6",
	},
	"contrast": {
		"background": "#101010", "text": "#FFFFFF", "muted": "#DDDDDD", "accent": "#66EBFF", "selection": "#FFF06A",
		"success": "#76FF76", "warning": "#FFF06A", "error": "#FF8298", "chrome": "#252525", "panel": "#1A1A1A",
		"highlight": "#3D3600", "scrollTrack": "#000000", "scrollThumb": "#66EBFF",
	},
}

func selectedPalette(config core.TUIConfig) palette {
	p := make(palette)
	if _, noColor := os.LookupEnv("NO_COLOR"); noColor || os.Getenv("TERM") == "dumb" {
		return p
	}
	base := themes[config.Theme]
	if base == nil {
		base = themes["classic"]
	}
	for k, v := range base {
		p[k] = v
	}
	for k, v := range config.Colors {
		if core.HexColor.MatchString(v) {
			p[k] = v
		}
	}
	return p
}

func (p palette) paint(value string, width int, background string) string {
	if width <= 0 {
		return ""
	}
	value = ansi.Truncate(value, width, "…")
	value += strings.Repeat(" ", max(0, width-ansi.StringWidth(value)))
	bg := p.color(background)
	fg := p.color("text")
	if bg == nil && fg == nil {
		return value
	}
	var style ansi.Style
	if bg != nil {
		style = style.BackgroundColor(bg)
	}
	if fg != nil {
		style = style.ForegroundColor(fg)
	}
	// Styled labels reset their colors at the end of a span. Reapply the
	// cell background before rendering the rest of the line and its padding.
	prefix := style.String()
	return prefix + strings.ReplaceAll(value, ansi.ResetStyle, ansi.ResetStyle+prefix) + ansi.ResetStyle
}

func (p palette) fallback(role, other string) string {
	if p[role] == "" {
		return other
	}
	return role
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
	var mouse mouseLayout
	if m.view == catalogPage || m.view == detailPage {
		return m.libraryView(p, &mouse)
	}
	if m.view == confirmPage || m.view == downloadPage || m.view == resultPage {
		return m.downloadFlowView(p, &mouse)
	}
	if m.view == installPage || m.view == overwritePage || m.view == updatePage {
		return m.promptView(p, &mouse)
	}
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
	title := map[page]string{settingsPage: "设置", browsePage: "选择下载目录", updatingPage: "下载更新"}
	line(m.titleBar(p, core.AppName+"  ·  "+title[m.view]))
	line("")
	var help []shortcut
	switch m.view {
	case settingsPage:
		line(p.style("muted", false).Render(safe(m.config.Path)))
		if m.config.Override != "" {
			line(p.style("warning", false).Render("本次输出覆盖：" + safe(m.config.Override)))
		}
		f := m.form
		if install.Available() {
			mouse.add(0, len(lines), width, 1, clickSetting(installAction, "enter"))
			row(installAction, f.index, "安装并加入 PATH  [Enter] · ~/.local/bin")
		}
		start, end := window(max(0, f.index), len(f.fields)+1, max(1, m.height-len(lines)-4))
		for i := start; i < end; i++ {
			if i == len(f.fields) {
				mouse.add(0, len(lines), width, 1, clickSetting(i, "enter"))
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
				arrow := ansi.StringWidth(prefix + caption)
				mouse.add(arrow, len(lines), 1, 1, clickSetting(i, "left"))
				mouse.add(0, len(lines), width, 1, clickSetting(i, "right"))
				line(prefix + caption + "〈 " + field.options[field.value] + " 〉")
			} else {
				mouse.add(0, len(lines), width, 1, clickSetting(i, ""))
				input := field.input
				input.SetWidth(max(1, width-ansi.StringWidth(prefix+caption+input.Prompt)-1))
				line(prefix + caption + input.View())
			}
		}
		line(mouse.shortcuts(p, width, len(lines), []shortcut{{"f3", "F3 更新"}, {"f5", "F5 恢复预设色"}, {"f6", "F6 浏览目录"}}))
		help = []shortcut{{"ctrl+s", "Ctrl+S 保存"}, {"esc", "Esc 取消"}, {"left", "← 上一选项"}, {"right", "→ 下一选项"}, {"tab", "Tab 移动"}}
	case browsePage:
		f := m.form
		line(safe(f.directory))
		start, end := window(f.directoryCursor, len(f.directories), max(1, m.height-len(lines)-3))
		for i := start; i < end; i++ {
			mouse.add(0, len(lines), width, 1, clickDirectory(f.directory, f.directories[i]))
			row(i, f.directoryCursor, f.directories[i]+"/")
		}
		help = []shortcut{{"left", "← 上一级"}, {"right", "→ 打开目录"}, {"enter", "Enter 使用当前目录"}, {"esc", "Esc 取消"}}
	case updatingPage:
		current := m.updateProgress.Snapshot()
		line(core.Bytes(float64(current.Done)) + " / " + core.Bytes(float64(current.Total)))
		line(current.Bar(min(48, width), m.frame, fillBar))
		help = []shortcut{{"esc", "Esc 取消下载"}}
	}
	if m.status != "" {
		line(p.style("muted", false).Render(safe(m.status)))
	}
	for i := range lines {
		if i > 0 {
			lines[i] = p.paint(lines[i], m.width, "background")
		}
	}
	return m.frameView(p, lines, help, &mouse)
}

func (m *model) titleBar(p palette, title string) string {
	gap := max(0, (m.width-ansi.StringWidth(title))/2)
	return p.paint(strings.Repeat(" ", gap)+title, m.width, "chrome")
}

func (m *model) frameView(p palette, lines []string, help []shortcut, mouse *mouseLayout) tea.View {
	count := max(0, m.height-1)
	lines = lines[:min(len(lines), count)]
	blank := p.paint("", m.width, "background")
	for len(lines) < count {
		lines = append(lines, blank)
	}
	footer := "chrome"
	if len(help) == 0 {
		footer = "background"
	}
	mouse.clipBody(count)
	lines = append(lines, p.paint(mouse.shortcuts(p, m.width, count, help), m.width, footer))
	v := tea.NewView(strings.Join(lines, "\n"))
	v.AltScreen = true
	v.WindowTitle = core.AppName
	v.BackgroundColor, v.ForegroundColor = p.color("background"), p.color("text")
	mouse.bind(&v, m.view, m.modeOpen, m.width, m.height)
	return v
}

func window(cursor, count, visible int) (int, int) {
	start := max(0, cursor-visible+1)
	return start, min(count, start+visible)
}
