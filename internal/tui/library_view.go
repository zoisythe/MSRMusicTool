package tui

import (
	"fmt"
	"strings"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
	"github.com/zoisythe/MSRMusicTool/internal/core"
)

type libraryLayout struct {
	info, status []string
	listHeight   int
	rowHeight    int
	spaced       bool
}

func (m *model) libraryLayout() libraryLayout {
	layout := libraryLayout{spaced: m.height >= 20}
	if m.view == detailPage {
		layout.info = []string{"专辑：" + safe(m.detail.Name)}
		if m.detail.Intro != "" {
			layout.info = append(layout.info, safe(m.detail.Intro))
		}
		selected := 0
		for _, song := range m.detail.Songs {
			if m.selected[song.CID] {
				selected++
			}
		}
		layout.status = []string{fmt.Sprintf("本专辑已选择 %d/%d 首；Left 返回", selected, len(m.detail.Songs))}
	} else {
		mode := "默认目录"
		if m.config.Override != "" {
			mode = "临时覆盖"
		} else if m.config.Configured() {
			mode = "配置目录"
		}
		layout.info = []string{"配置：" + safe(m.config.Path), "下载：" + safe(m.config.Output) + "（" + mode + "）"}
		selection := fmt.Sprintf("已选择 %d 首歌曲", len(m.selected))
		switch {
		case len(m.rows) == 0 && m.catalog.AlbumByID != nil:
			selection = "没有匹配结果；" + selection
		case m.query != "":
			selection += fmt.Sprintf("；搜索结果 %d 张专辑", len(m.rows))
		}
		status := m.catalogState
		if !m.snapshotAt.IsZero() {
			stamp := "曲库更新于 " + m.snapshotAt.Local().Format("2006-01-02 15:04")
			if status == "曲库已更新" {
				status = stamp
			} else {
				status += " · " + stamp
			}
		}
		layout.status = []string{selection, status, "专辑内按 ← 返回曲库"}
		if m.pending != "" {
			layout.status = append(layout.status, m.pending+"…（Esc 取消）")
		}
		if m.updateCandidate != nil {
			layout.status = append(layout.status, "U 更新至 "+safe(m.updateCandidate.Tag))
		}
	}
	if m.status != "" {
		layout.status = append(layout.status, safe(m.status))
	}
	// Header, footer, divider and the two edges of the list frame.
	reserved := 5 + len(layout.info)
	if m.view == catalogPage && (m.searchOpen || m.query != "") {
		reserved++
	}
	if layout.spaced {
		reserved += 4
	}
	// Keep at least one item visible in short terminals.
	layout.status = layout.status[:min(len(layout.status), max(0, m.height-reserved-1))]
	layout.listHeight = max(1, m.height-reserved-len(layout.status))
	layout.rowHeight = 1
	if layout.listHeight >= 6 {
		layout.rowHeight = 3
	} else if layout.listHeight >= 4 && m.view == catalogPage && m.query != "" {
		layout.rowHeight = 2
	}
	return layout
}

func (m *model) visibleLibraryRows() int {
	layout := m.libraryLayout()
	return layout.listHeight / layout.rowHeight
}

func (m *model) libraryView(p palette, mouse *mouseLayout) tea.View {
	layout := m.libraryLayout()
	lines := []string{m.titleBar(p, core.AppName+" — 塞壬唱片音乐下载器")}
	blank := p.paint("", m.width, "background")
	if layout.spaced {
		lines = append(lines, blank)
	}
	for _, line := range layout.info {
		lines = append(lines, p.paint(p.style("muted", false).Render("  "+line), m.width, "background"))
	}
	if layout.spaced {
		lines = append(lines, blank)
	}
	divider := p.style(p.fallback("divider", "accent"), false)
	lines = append(lines, p.paint(divider.Render(strings.Repeat("─", m.width)), m.width, "background"))
	if layout.spaced {
		lines = append(lines, blank)
	}
	if m.view == catalogPage {
		if m.searchOpen {
			input := m.search
			input.Prompt = "/ "
			input.SetWidth(max(1, m.width-5))
			lines = append(lines, p.paint("  "+input.View(), m.width, "background"))
		} else if m.query != "" {
			mouse.add(2, len(lines), m.width-2, 1, press("/"))
			lines = append(lines, p.paint("  搜索："+safe(m.query), m.width, "background"))
		}
	}
	lines = append(lines, m.libraryPanel(p, layout, mouse, len(lines))...)
	if layout.spaced {
		lines = append(lines, blank)
	}
	for _, line := range layout.status {
		if m.view == catalogPage && m.updateCandidate != nil && strings.HasPrefix(line, "U 更新至 ") {
			mouse.add(2, len(lines), ansi.StringWidth(line), 1, press("u"))
		}
		lines = append(lines, p.paint(p.style("muted", false).Render("  "+line), m.width, "background"))
	}
	help := []shortcut{{"enter", "↵ 下载"}, {"/", "/ 搜索"}, {"space", "Space 选择"}, {"right", "→ 进入专辑"}, {"f2", "F2 设置"}, {"r", "r 刷新"}, {"q", "q 退出"}, {"esc", "Esc 退出"}}
	if m.width < 70 {
		help = []shortcut{{"right", "→ 进入专辑"}, {"f2", "F2 设置"}, {"q", "q 退出"}, {"/", "/ 搜索"}, {"enter", "↵ 下载"}, {"space", "Space 选择"}, {"r", "r 刷新"}, {"esc", "Esc 退出"}}
	}
	if m.view == detailPage {
		help = []shortcut{{"left", "← 返回专辑列表"}, {"space", "Space 选择歌曲"}, {"esc", "Esc 返回"}}
	} else if m.searchOpen {
		help = []shortcut{{"enter", "↵ 应用搜索"}, {"esc", "Esc 关闭搜索"}}
	} else if m.pending != "" {
		help = []shortcut{{"esc", "Esc 取消"}, {"q", "q 退出"}}
	}
	return m.frameView(p, lines, help, mouse)
}

func (m *model) libraryPanel(p palette, layout libraryLayout, mouse *mouseLayout, y int) []string {
	cursor, count := m.cursor, len(m.rows)
	if m.view == detailPage {
		cursor, count = m.detailCursor, len(m.detail.Songs)
	}
	margin := 0
	if m.width >= 16 {
		margin = 2
	}
	width := m.width - 2*margin
	inner := max(0, width-2)
	edge := p.style("accent", false).Render
	gutter := p.paint("", margin, "background")
	if width <= 2 {
		return []string{p.paint("", m.width, "panel")}
	}
	lines := []string{gutter + p.paint(edge("╭"+strings.Repeat("─", inner)+"╮"), width, "panel") + gutter}
	visible := layout.listHeight / layout.rowHeight
	start, end := window(cursor, count, visible)
	scroll := count > visible && inner >= 3
	thumbHeight, thumbStart := 0, 0
	if scroll {
		thumbHeight = max(1, layout.listHeight*visible/count)
		thumbStart = (layout.listHeight - thumbHeight) * start / (count - visible)
		inner--
	}
	for i := range layout.listHeight {
		index, offset := start+i/layout.rowHeight, i%layout.rowHeight
		content, background := "", "panel"
		if index < end {
			if offset == 0 {
				x, row := margin+1, y+i+1
				if m.view == detailPage {
					mouse.add(x, row, inner, layout.rowHeight, clickSong(m.detail.CID, m.detail.Songs[index].CID))
				} else {
					cid := m.rows[index].Album.CID
					mouse.add(x, row, min(inner, 5), layout.rowHeight, clickAlbum(cid, "space"))
					mouse.add(x+5, row, inner-5, layout.rowHeight, clickAlbum(cid, "right"))
				}
			}
			selected := index == cursor
			if selected {
				background = "highlight"
			}
			if offset == (layout.rowHeight-1)/2 {
				content = m.libraryItem(p, index, selected)
			} else if offset == layout.rowHeight-1 && m.view == catalogPage {
				content = m.matchingSongs(p, index)
			}
		}
		bar := ""
		if scroll {
			position := i * (count - 1) / max(1, layout.listHeight-1)
			mouse.add(margin+1+inner, y+i+1, 1, 1, func(m *model) tea.Cmd {
				if m.view == detailPage {
					m.detailCursor = min(position, max(0, len(m.detail.Songs)-1))
				} else {
					m.cursor = min(position, max(0, len(m.rows)-1))
				}
				return nil
			})
			role := "scrollTrack"
			glyph := " "
			if i >= thumbStart && i < thumbStart+thumbHeight {
				role = "scrollThumb"
				if p.color(role) == nil {
					glyph = "▪"
				}
			}
			bar = p.paint(glyph, 1, role)
		}
		lines = append(lines, gutter+p.paint(edge("│"), 1, "panel")+p.paint(content, inner, background)+bar+p.paint(edge("│"), 1, "panel")+gutter)
	}
	lines = append(lines, gutter+p.paint(edge("╰"+strings.Repeat("─", width-2)+"╯"), width, "panel")+gutter)
	return lines
}

func (m *model) libraryItem(p palette, index int, active bool) string {
	symbol, role := "○", p.fallback("unselected", "muted")
	name, artists := "", ""
	if m.view == detailPage {
		song := m.detail.Songs[index]
		name, artists = song.Name, strings.Join(song.Artists, " / ")
		if m.selected[song.CID] {
			symbol, role = "●", "success"
		}
	} else {
		match := m.rows[index]
		name = match.Album.Name
		switch match.State() {
		case 1:
			symbol, role = "◐", "warning"
		case 2:
			symbol, role = "●", "success"
		}
	}
	label := strings.TrimSpace(safe(name))
	if active {
		label = p.style("selection", true).Render(label)
	}
	if artists != "" {
		label += p.style("muted", false).Render("  " + safe(artists))
	}
	return "  " + p.style(role, false).Render(symbol) + "  " + label
}

func (m *model) matchingSongs(p palette, index int) string {
	songs := m.rows[index].Songs
	if len(songs) == 0 {
		return ""
	}
	names := make([]string, 0, min(len(songs), 3))
	for _, song := range songs[:min(len(songs), 3)] {
		names = append(names, safe(song.Name))
	}
	label := "     匹配曲目：" + strings.Join(names, "、")
	if len(songs) > len(names) {
		label += fmt.Sprintf(" 等 %d 首", len(songs))
	}
	return p.style("muted", false).Render(label)
}
