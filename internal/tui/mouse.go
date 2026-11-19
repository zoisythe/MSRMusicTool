package tui

import (
	"path/filepath"
	"slices"
	"strings"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
	"github.com/zoisythe/MSRMusicTool/internal/core"
)

type clickAction func(*model) tea.Cmd

type mouseArea struct {
	x, y, width, height int
	action              clickAction
}

type mouseLayout []mouseArea

func (l *mouseLayout) add(x, y, width, height int, action clickAction) {
	if l != nil && width > 0 && height > 0 {
		*l = append(*l, mouseArea{x, y, width, height, action})
	}
}

func (l *mouseLayout) appendAt(other mouseLayout, y int) {
	for _, area := range other {
		l.add(area.x, y+area.y, area.width, area.height, area.action)
	}
}

func (l *mouseLayout) clipBody(rows int) {
	visible := (*l)[:0]
	for _, area := range *l {
		if area.y < rows {
			area.height = min(area.height, rows-area.y)
			visible = append(visible, area)
		}
	}
	*l = visible
}

type clickMsg struct {
	page     page
	modeOpen bool
	action   clickAction
}

type shortcut struct{ key, label string }

func (l *mouseLayout) shortcuts(p palette, width, y int, bindings []shortcut) string {
	var parts []string
	x := 1
	keyStyle := p.style(p.fallback("shortcut", "accent"), true)
	for _, binding := range bindings {
		columns := ansi.StringWidth(binding.label)
		if x+columns > width {
			break
		}
		l.add(x, y, columns, 1, press(binding.key))
		hint, description, _ := strings.Cut(binding.label, " ")
		label := keyStyle.Render(hint)
		if description != "" {
			label += " " + description
		}
		parts = append(parts, label)
		x += columns + 2
	}
	if len(parts) == 0 {
		return ""
	}
	return " " + strings.Join(parts, "  ")
}

var shortcutKeys = map[string]tea.Key{
	"esc": {Code: tea.KeyEscape}, "enter": {Code: tea.KeyEnter},
	"space": {Code: tea.KeySpace, Text: " "}, "tab": {Code: tea.KeyTab},
	"shift+tab": {Code: tea.KeyTab, Mod: tea.ModShift},
	"left":      {Code: tea.KeyLeft}, "right": {Code: tea.KeyRight},
	"up": {Code: tea.KeyUp}, "down": {Code: tea.KeyDown},
	"pgup": {Code: tea.KeyPgUp}, "pgdown": {Code: tea.KeyPgDown},
	"f2": {Code: tea.KeyF2}, "f3": {Code: tea.KeyF3}, "f5": {Code: tea.KeyF5}, "f6": {Code: tea.KeyF6},
	"ctrl+s": {Code: 's', Mod: tea.ModCtrl}, "ctrl+c": {Code: 'c', Mod: tea.ModCtrl},
}

func press(name string) clickAction {
	return func(m *model) tea.Cmd {
		key, ok := shortcutKeys[name]
		if !ok {
			if r := []rune(name); len(r) == 1 {
				key = tea.Key{Code: r[0], Text: name}
			}
		}
		return m.handle(tea.KeyPressMsg(key))
	}
}

func (l mouseLayout) bind(v *tea.View, current page, modeOpen bool, width, height int) {
	v.MouseMode = tea.MouseModeCellMotion
	v.OnMouse = func(event tea.MouseMsg) tea.Cmd {
		mouse := event.Mouse()
		if mouse.X < 0 || mouse.X >= width || mouse.Y < 0 || mouse.Y >= height {
			return nil
		}
		switch event.(type) {
		case tea.MouseClickMsg:
			if mouse.Button != tea.MouseLeft {
				return nil
			}
			for _, area := range l {
				if mouse.X >= area.x && mouse.X < area.x+area.width && mouse.Y >= area.y && mouse.Y < area.y+area.height {
					return func() tea.Msg { return clickMsg{current, modeOpen, area.action} }
				}
			}
		case tea.MouseWheelMsg:
			key := ""
			switch mouse.Button {
			case tea.MouseWheelUp:
				key = "up"
			case tea.MouseWheelDown:
				key = "down"
			}
			if key != "" {
				return func() tea.Msg {
					return clickMsg{current, modeOpen, func(m *model) tea.Cmd { return m.wheel(key) }}
				}
			}
		}
		return nil
	}
}

func (m *model) wheel(key string) tea.Cmd {
	if (m.view == confirmPage && !m.modeOpen) || m.view == resultPage {
		m.scrollDownloadPanel(key)
		return nil
	}
	switch m.view {
	case catalogPage, detailPage, browsePage, settingsPage, confirmPage:
		return press(key)(m)
	}
	return nil
}

func clickButton(index int) clickAction {
	return func(m *model) tea.Cmd {
		if m.view == confirmPage {
			m.modeOpen = false
			m.focusConfirmation(index)
		} else {
			m.button = index
		}
		return press("enter")(m)
	}
}

func clickSetting(index int, key string) clickAction {
	return func(m *model) tea.Cmd {
		m.form.index = index
		if key == "" {
			return m.form.focus()
		}
		return tea.Batch(m.form.focus(), press(key)(m))
	}
}

func clickDirectory(parent, name string) clickAction {
	return func(m *model) tea.Cmd {
		if m.form.directory != parent || !slices.Contains(m.form.directories, name) {
			return nil
		}
		return m.loadDirectory(filepath.Join(parent, name))
	}
}

func clickAlbum(cid, key string) clickAction {
	return func(m *model) tea.Cmd {
		if m.pending != "" {
			return nil
		}
		index := slices.IndexFunc(m.rows, func(row core.Match) bool { return row.Album.CID == cid })
		if index < 0 {
			return nil
		}
		m.cursor = index
		if m.searchOpen {
			m.searchOpen = false
			m.search.Blur()
			m.query = m.search.Value()
			m.rebuild()
			if m.cursor >= len(m.rows) || m.rows[m.cursor].Album.CID != cid {
				return nil
			}
		}
		return press(key)(m)
	}
}

func clickSong(album, cid string) clickAction {
	return func(m *model) tea.Cmd {
		if m.detail.CID != album {
			return nil
		}
		index := slices.IndexFunc(m.detail.Songs, func(song core.Song) bool { return song.CID == cid })
		if index < 0 {
			return nil
		}
		m.detailCursor = index
		return press("space")(m)
	}
}
