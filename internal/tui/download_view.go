package tui

import (
	"fmt"
	"math"
	"strings"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
	"github.com/zoisythe/MSRMusicTool/internal/core"
)

var modeLabels = []string{"全部：音频、歌词、封面", "仅音频", "音频与歌词"}

func (m *model) downloadFlowView(p palette, mouse *mouseLayout) tea.View {
	title := "确认下载"
	switch m.view {
	case downloadPage:
		title = "正在下载"
	case resultPage:
		title = "下载结果"
	}
	header := m.flowHeader(p, title)
	switch m.view {
	case confirmPage:
		return m.confirmationView(p, header, mouse)
	case resultPage:
		return m.resultView(p, header, mouse)
	default:
		return m.progressView(p, header, mouse)
	}
}

func (m *model) flowHeader(p palette, title string) []string {
	lines := []string{m.titleBar(p, core.AppName+" — 塞壬唱片音乐下载器")}
	bg := p.fallback("section", "chrome")
	if m.height >= 22 {
		lines = append(lines, p.paint("", m.width, bg))
	}
	lines = append(lines, p.paint("  "+p.style("text", true).Render(title), m.width, bg))
	if m.height >= 22 {
		lines = append(lines, p.paint("", m.width, bg), p.paint("", m.width, "background"))
	}
	return lines
}

func (m *model) confirmationContent(p palette) []string {
	content := []string{fmt.Sprintf("共 %d 首歌曲", m.prepared.Count()), ""}
	for i, album := range m.prepared.Albums {
		content = append(content, p.style("text", true).Render(strings.TrimSpace(safe(album.Detail.Name))))
		for _, song := range album.Songs {
			content = append(content, "  • "+safe(song.Name))
		}
		if i+1 < len(m.prepared.Albums) || len(m.prepared.Failures) > 0 {
			content = append(content, "")
		}
	}
	for _, failure := range m.prepared.Failures {
		content = append(content, "  • "+safe(failure.Song.Name))
		for _, reason := range failure.Errors {
			content = append(content, p.style("error", false).Render("    "+safe(reason)))
		}
	}
	return content
}

func (m *model) resultContent(p palette) []string {
	success, skip, failed := 0, 0, 0
	for _, song := range m.batch.Songs {
		switch song.Outcome {
		case "success":
			success++
		case "skipped":
			skip++
		default:
			failed++
		}
	}
	if m.batch.Err != nil {
		failed += max(0, m.prepared.Count()-len(m.batch.Songs))
	}
	content := []string{fmt.Sprintf("成功 %d 首 · 已跳过 %d 首 · 失败 %d 首", success, skip, failed), ""}
	if m.batch.Err != nil {
		content = append(content, p.style("error", false).Render(safe(m.batch.Err.Error())), "")
	}
	for _, song := range m.batch.Songs {
		icon, role := "✗", "error"
		switch song.Outcome {
		case "success":
			icon, role = "✓", "success"
		case "skipped":
			icon, role = "↷", "accent"
		}
		content = append(content, p.style(role, false).Render(icon)+" "+safe(song.Song.Name)+"  "+song.StatusLabel())
		for _, reason := range song.Errors {
			content = append(content, p.style("error", false).Render("    "+safe(reason)))
		}
	}
	if m.batch.Err != nil {
		for _, album := range m.prepared.Albums {
			for _, song := range album.Songs {
				content = append(content, p.style("error", false).Render("✗")+" "+safe(song.Name)+"  失败")
			}
		}
	}
	if len(m.batch.Unresolved) > 0 {
		content = append(content, p.style("warning", false).Render("取消无法确认，保留临时文件 · GID "+safe(strings.Join(m.batch.Unresolved, ", "))))
	}
	if failed > 0 {
		content = append(content, "", "失败歌曲会继续保持选中，返回主界面后可重新确认下载。")
	}
	return content
}

func (m *model) confirmationView(p palette, header []string, mouse *mouseLayout) tea.View {
	var controls mouseLayout
	tail := m.confirmationControls(p, &controls)
	lines := append(header, m.flowPanel(p, m.confirmationContent(p), m.flowOffset, m.height-len(header)-len(tail)-1, "background")...)
	mouse.appendAt(controls, len(lines))
	lines = append(lines, tail...)
	help := []shortcut{{"up", "↑ 模式"}, {"down", "↓ 按钮"}, {"left", "← 左"}, {"right", "→ 右"}, {"enter", "Enter 确定"}, {"esc", "Esc 取消"}, {"pgup", "PgUp"}, {"pgdown", "PgDn"}, {"tab", "Tab 切换"}}
	if m.modeOpen {
		help = []shortcut{{"up", "↑ 上一模式"}, {"down", "↓ 下一模式"}, {"enter", "Enter 确定"}, {"esc", "Esc 关闭"}}
	}
	return m.frameView(p, lines, help, mouse)
}

func (m *model) resultView(p palette, header []string, mouse *mouseLayout) tea.View {
	help := []shortcut{{"esc", "Esc 返回主界面"}, {"q", "q 退出"}, {"tab", "Tab 切换"}, {"pgup", "PgUp"}, {"pgdown", "PgDn"}}
	return m.panelWithActions(p, header, m.resultContent(p), m.flowOffset, resultButtons, help, mouse)
}

var resultButtons = [2]string{"返回主界面 (Esc)", "退出 (q)"}

func (m *model) panelWithActions(p palette, header, content []string, offset int, labels [2]string, help []shortcut, mouse *mouseLayout) tea.View {
	var controls mouseLayout
	tail := m.panelActions(p, labels, &controls)
	lines := append(header, m.flowPanel(p, content, offset, m.height-len(header)-len(tail)-1, "background")...)
	mouse.appendAt(controls, len(lines))
	lines = append(lines, tail...)
	return m.frameView(p, lines, help, mouse)
}

func (m *model) panelActions(p palette, labels [2]string, mouse *mouseLayout) []string {
	var tail []string
	blank := p.paint("", m.width, "background")
	if m.height >= 22 {
		tail = append(tail, blank, blank)
	}
	tail = append(tail, m.actionRow(p, mouse, len(tail), m.button, 0, labels[0], labels[1])...)
	if m.height >= 22 {
		tail = append(tail, blank)
	}
	return tail
}

func (m *model) confirmationControls(p palette, mouse *mouseLayout) []string {
	if m.height < 5 {
		return nil
	}
	label := modeLabels[m.mode]
	width := max(1, m.width-4)
	selector := m.control(p, label, width, m.button == 0, m.height >= 22, false)
	tail := make([]string, 0, len(selector)+7)
	blank := p.paint("", m.width, "background")
	if m.height >= 22 {
		tail = append(tail, blank)
	}
	x := (m.width - width) / 2
	mouse.add(x, len(tail), width, len(selector), func(m *model) tea.Cmd {
		m.focusConfirmation(0)
		if m.modeOpen {
			m.modeOpen = false
		} else {
			m.openDownloadMode()
		}
		return nil
	})
	for _, line := range selector {
		tail = append(tail, m.center(p, line))
	}
	if m.modeOpen {
		for i, label := range modeLabels {
			prefix, bg := "    ", p.fallback("control", "chrome")
			if i == m.modeCursor {
				prefix, bg = "  › ", "highlight"
			}
			mouse.add(x, len(tail), width, 1, func(m *model) tea.Cmd {
				m.modeCursor = i
				return m.confirmKeys("enter")
			})
			tail = append(tail, m.center(p, p.paint(prefix+label, width, bg)))
		}
	}
	if m.height >= 22 {
		tail = append(tail, blank)
	}
	tail = append(tail, m.actionRow(p, mouse, len(tail), m.button-1, 1, "取消", "确认下载")...)
	if m.height >= 22 {
		tail = append(tail, blank)
	}
	return tail
}

func (m *model) actionRow(p palette, mouse *mouseLayout, y, active, firstIndex int, first, second string) []string {
	available := max(1, (m.width-3)/2)
	left := m.control(p, first, min(available, max(16, ansi.StringWidth(first)+4)), active == 0, m.height >= 22, true)
	right := m.control(p, second, min(available, max(16, ansi.StringWidth(second)+4)), active == 1, m.height >= 22, true)
	leftWidth, rightWidth := ansi.StringWidth(left[0]), ansi.StringWidth(right[0])
	x := max(0, (m.width-leftWidth-rightWidth-1)/2)
	mouse.add(x, y, leftWidth, len(left), clickButton(firstIndex))
	mouse.add(x+leftWidth+1, y, rightWidth, len(right), clickButton(firstIndex+1))
	lines := make([]string, 0, len(left))
	for i, row := range left {
		lines = append(lines, m.center(p, row+p.paint("", 1, "background")+right[i]))
	}
	return lines
}

func (m *model) control(p palette, label string, width int, active, tall, centered bool) []string {
	bg := p.fallback("control", "chrome")
	if active {
		bg = "panel"
	}
	margin := "  "
	if centered {
		margin = strings.Repeat(" ", max(0, (width-ansi.StringWidth(label))/2))
	}
	if active {
		label = p.style("selection", true).Render(label)
	}
	if tall && width > 4 {
		edge := p.style("muted", false).Render
		if !centered {
			if active {
				edge = p.style("accent", false).Render
			}
			label = ansi.Truncate(label, width-7, "…")
			gap := strings.Repeat(" ", max(0, width-5-ansi.StringWidth(margin+label)))
			return []string{
				p.paint(edge("▊"+strings.Repeat("▔", width-2)+"▎"), width, bg),
				p.paint(edge("▊")+margin+label+gap+"▼  "+edge("▎"), width, bg),
				p.paint(edge("▊"+strings.Repeat("▁", width-2)+"▎"), width, bg),
			}
		}
		return []string{
			p.paint(edge(strings.Repeat("▔", width)), width, bg),
			p.paint(margin+label, width, bg),
			p.paint(edge(strings.Repeat("▁", width)), width, bg),
		}
	}
	if active {
		margin = "› "
	}
	if !centered {
		label += "  ▼"
	}
	return []string{p.paint(margin+label, width, bg)}
}

func (m *model) center(p palette, line string) string {
	line = ansi.Truncate(line, m.width, "")
	gap := max(0, m.width-ansi.StringWidth(line))
	return p.paint("", gap/2, "background") + line + p.paint("", gap-gap/2, "background")
}

// flowPanel uses the same two-column gutter and rounded border as the catalog.
func (m *model) flowPanel(p palette, content []string, offset, height int, bg string) []string {
	if height < 1 {
		return nil
	}
	margin := 0
	if m.width >= 16 {
		margin = 2
	}
	gutter := p.paint("", margin, "background")
	width := m.width - 2*margin
	if height < 3 || width < 4 {
		lines := make([]string, 0, height)
		for _, content := range content[:min(height, len(content))] {
			lines = append(lines, gutter+p.paint(content, width, bg)+gutter)
		}
		return lines
	}
	inner := width - 2
	visible := flowPanelRows(height)
	padding := (height - 2 - visible) / 2
	offset = min(max(0, offset), max(0, len(content)-visible))
	edge := p.style("accent", false).Render
	border := func(left, right string) string {
		return gutter + p.paint(edge(left+strings.Repeat("─", inner)+right), width, bg) + gutter
	}
	side := p.paint(edge("│"), 1, bg)
	lines := []string{border("╭", "╮")}
	for i := range height - 2 {
		line := ""
		if index := offset + i - padding; i >= padding && i < padding+visible && index < len(content) {
			line = "  " + content[index]
		}
		lines = append(lines, gutter+side+p.paint(line, inner, bg)+side+gutter)
	}
	return append(lines, border("╰", "╯"))
}

func flowPanelRows(height int) int {
	switch {
	case height >= 6:
		return height - 4
	case height >= 3:
		return height - 2
	default:
		return max(0, height)
	}
}

func (m *model) scrollDownloadPanel(key string) {
	var content []string
	var controls int
	if m.view == confirmPage {
		content = m.confirmationContent(nil)
		controls = len(m.confirmationControls(nil, nil))
	} else {
		content = m.resultContent(nil)
		controls = len(m.panelActions(nil, resultButtons, nil))
	}
	height := m.height - len(m.flowHeader(nil, "")) - controls - 1
	visible := max(1, flowPanelRows(height))
	last := max(0, len(content)-visible)
	m.flowOffset = move(m.flowOffset, last+1, visible, key)
}

func (m *model) progressView(p palette, header []string, mouse *mouseLayout) tea.View {
	summary, resources, logs := m.tracker.Activity(m.height)
	rows := []progressRow{{fmt.Sprintf("歌曲 %d/%d", summary.Completed, m.prepared.Count()), summary.Done, summary.Total, summary.Speed}}
	for _, r := range resources {
		label := safe(r.Label)
		switch r.Status {
		case "active", "":
		case "waiting":
			label += " · 排队"
		case "transferred":
			label += " · 整理"
		default:
			label += " · " + core.StatusLabel(r.Status)
		}
		rows = append(rows, progressRow{label, r.Downloaded, r.Total, r.Speed})
	}
	width := max(1, m.width-4)
	blank := p.paint("", m.width, "background")
	space := max(1, m.height-len(header)-1)
	logHeight := max(3, (space-5)/2)
	rowLimit := max(1, space-logHeight-4)
	rendered := renderProgress(p, rows[:min(rowLimit, len(rows))], width, m.frame)
	lines := append(header, m.center(p, p.paint(rendered[0], width, "background")))
	if space >= 9 {
		lines = append(lines, blank)
	}
	for _, line := range rendered[1:] {
		lines = append(lines, m.center(p, p.paint(line, width, "background")))
	}
	if len(resources) == 0 && len(logs) == 0 {
		lines = append(lines, p.paint("  正在准备资源……", m.width, "background"))
	}
	if m.status != "" {
		lines = append(lines, p.paint("  "+safe(m.status), m.width, "background"))
	}
	// Keep the bordered log in the lower half while resources come and go.
	for len(lines) < m.height-logHeight-3 {
		lines = append(lines, blank)
	}
	var logRows []string
	for _, entry := range logs[max(0, len(logs)-logHeight):] {
		logRows = append(logRows, strings.Split(ansi.Wrap(safe(entry), max(1, m.width-8), ""), "\n")...)
	}
	lines = append(lines, m.flowPanel(p, logRows, len(logRows), logHeight, p.fallback("control", "panel"))...)
	return m.frameView(p, lines, []shortcut{{"ctrl+c", "Ctrl+C 取消本批并退出"}}, mouse)
}

type progressRow struct {
	label       string
	done, total int64
	speed       float64
}

func renderProgress(p palette, rows []progressRow, width, frame int) []string {
	var rendered []string
	for _, row := range rows {
		amount := core.Bytes(float64(row.done)) + "/" + core.Bytes(float64(row.total))
		speed := core.Bytes(row.speed) + "/s"
		if width < 35 {
			rendered = append(rendered, row.label+"  "+amount+"  "+speed)
			continue
		}
		amountWidth := max(19, len(amount))
		speedWidth := max(12, len(speed))
		nameWidth := max(1, min(24, width-amountWidth-speedWidth-8))
		barWidth := max(1, width-nameWidth-amountWidth-speedWidth-6)
		label := ansi.Truncate(row.label, nameWidth, "…")
		label += strings.Repeat(" ", nameWidth-ansi.StringWidth(label))
		done := core.ProgressSnapshot{Done: row.done, Total: row.total}
		bar := progressBar(p, done, barWidth, frame)
		rendered = append(rendered, p.style("muted", false).Render(label)+"  "+bar+"  "+
			fmt.Sprintf("%*s  %*s", amountWidth, amount, speedWidth, speed))
	}
	return rendered
}

func progressBar(p palette, progress core.ProgressSnapshot, width, frame int) string {
	fill := p.style("success", false).Render
	track := p.style(p.fallback("unselected", "muted"), false).Render
	if progress.Total < 0 {
		pulse := min(3, width)
		start := frame % (width - pulse + 1)
		return track(strings.Repeat("━", start)) + fill(strings.Repeat("━", pulse)) + track(strings.Repeat("━", width-start-pulse))
	}
	if progress.Total == 0 {
		return fill(strings.Repeat("━", width))
	}
	scaled := float64(width) * min(1, max(0, float64(progress.Done)/float64(progress.Total)))
	complete := int(scaled)
	partial := int(math.Ceil(scaled)) - complete
	return fill(strings.Repeat("━", complete)+strings.Repeat("╸", partial)) + track(strings.Repeat("━", width-complete-partial))
}
