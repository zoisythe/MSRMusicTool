package tui

import (
	"fmt"
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
	"github.com/zoisythe/MSRMusicTool/internal/core"
)

func TestConfirmationArrowFocus(t *testing.T) {
	m := testModel(t)
	m.handle(prepareMsg{request: m.request})
	for _, key := range []struct {
		input string
		focus int
	}{{"left", 1}, {"up", 0}, {"up", 0}, {"down", 1}, {"right", 2}, {"up", 0}} {
		m.confirmKeys(key.input)
		if m.button != key.focus || m.modeOpen {
			t.Fatalf("%s: focus %d, mode open %v", key.input, m.button, m.modeOpen)
		}
	}
	m.confirmKeys("enter")
	m.confirmKeys("down")
	if !m.modeOpen || m.mode != 0 || m.modeCursor != 1 {
		t.Fatal("mode preview changed the selected mode")
	}
	m.confirmKeys("enter")
	if m.modeOpen || m.view != confirmPage || m.button != 0 || m.mode != 1 {
		t.Fatal("mode selection did not return to confirmation")
	}
	m.confirmKeys("down")
	if m.button != 2 {
		t.Fatal("down should return to the last focused button")
	}
}

func TestMouseConfirmationAndResult(t *testing.T) {
	for _, size := range []struct{ width, height int }{{100, 30}, {64, 18}} {
		t.Run(fmt.Sprintf("%dx%d", size.width, size.height), func(t *testing.T) {
			m := testModel(t)
			m.width, m.height = size.width, size.height
			m.handle(prepareMsg{request: m.request})
			clickLabel(t, m, "全部：音频、歌词、封面", false)
			if !m.modeOpen {
				t.Fatal("click did not open the mode menu")
			}
			clickLabel(t, m, "仅音频", false)
			if m.modeOpen || m.mode != 1 || m.view != confirmPage {
				t.Fatal("click did not apply the mode")
			}
			clickLabel(t, m, "取消", false)
			if m.view != catalogPage {
				t.Fatal("click did not cancel")
			}
			m.handle(prepareMsg{request: m.request})
			clickLabel(t, m, "确认下载", true)
			if m.view != downloadPage {
				t.Fatal("click did not start the download")
			}
			m.handle(resultMsg(core.BatchResult{}))
			clickLabel(t, m, "返回主界面 (Esc)", false)
			if m.view != catalogPage {
				t.Fatal("click did not return to the catalog")
			}
		})
	}
}

func TestMouseCatalogAndSettings(t *testing.T) {
	m := testModel(t)
	clickLabel(t, m, "○  中文专辑", false)
	if !m.selected["111111"] {
		t.Fatal("click did not select the album")
	}
	m.details["a1"] = core.AlbumDetail{
		Album: m.catalog.Albums[0], Songs: m.catalog.Songs,
	}
	clickLabel(t, m, "中文专辑", false)
	if m.view != detailPage {
		t.Fatal("click did not open the album")
	}
	clickLabel(t, m, "中文歌曲", false)
	if m.selected["111111"] {
		t.Fatal("click did not deselect the song")
	}
	clickLabel(t, m, "← 返回专辑列表", false)
	if m.view != catalogPage {
		t.Fatal("the footer did not return to the catalog")
	}
	clickLabel(t, m, "F2 设置", false)
	if m.view != settingsPage {
		t.Fatal("the footer did not open settings")
	}
	clickLabel(t, m, "下载后端", false)
	if m.form.fields[backendField].value != 1 {
		t.Fatal("click did not cycle the setting")
	}
	clickLabel(t, m, "Esc 取消", false)
	if m.view != catalogPage {
		t.Fatal("the footer did not close settings")
	}
}

func clickLabel(t *testing.T, m *model, label string, last bool) tea.Cmd {
	t.Helper()
	v := m.View()
	if v.MouseMode != tea.MouseModeCellMotion || v.OnMouse == nil {
		t.Fatal("mouse reporting is disabled")
	}
	x, y := 0, -1
	for i, line := range strings.Split(ansi.Strip(v.Content), "\n") {
		if index := strings.Index(line, label); index >= 0 {
			x, y = ansi.StringWidth(line[:index]), i
			if !last {
				break
			}
		}
	}
	if y < 0 {
		t.Fatalf("missing label %q", label)
	}
	event := v.OnMouse(tea.MouseClickMsg{X: x, Y: y, Button: tea.MouseLeft})
	if event == nil {
		t.Fatalf("missing hit area for %q at (%d,%d)", label, x, y)
	}
	return m.handle(event())
}
