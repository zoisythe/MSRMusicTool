package tui

import (
	"fmt"
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
	"github.com/zoisythe/MSRMusicTool/internal/core"
)

func TestDownloadFlowPanels(t *testing.T) {
	m := testModel(t)
	m.width, m.height = 100, 30
	first := core.Song{CID: "111111", Name: "第一首歌"}
	second := core.Song{CID: "222222", Name: "第二首歌"}
	m.handle(prepareMsg{request: m.request, prepared: core.Prepared{Albums: []core.AlbumDownload{{
		Detail: core.AlbumDetail{Album: core.Album{Name: "待下载专辑"}},
		Songs:  []core.Song{first, second},
	}}}})
	if m.view != confirmPage || m.button != 2 {
		t.Fatal("confirmation must initially focus the confirm button")
	}
	assertFlowText(t, m, "确认下载", "╭", "共 2 首歌曲", "待下载专辑", "• 第一首歌", "• 第二首歌", "╰", "全部：音频、歌词、封面", "取消", "确认下载")
	m.confirmKeys("tab")
	m.confirmKeys("enter")
	m.confirmKeys("down")
	if m.mode != 0 || !m.modeOpen {
		t.Fatal("mode navigation must keep the previous value until accepted")
	}
	m.confirmKeys("enter")
	if m.mode != 1 || m.modeOpen {
		t.Fatal("mode selection was not applied")
	}

	m.view = downloadPage
	m.tracker = &core.Tracker{}
	m.tracker.Update(core.Progress{Completed: 1, Resource: core.Resource{Key: "first", Label: "第一首歌 · 音频", Status: "active", Downloaded: 400, Total: 800, Speed: 120}})
	m.tracker.Update(core.Progress{Completed: 1, Resource: core.Resource{Key: "second", Label: "第二首歌 · 音频", Status: "waiting", Total: -1}})
	m.tracker.Update(core.Progress{Completed: 1, Resource: core.Resource{Key: "old", Label: "旧歌词", Status: "skipped", Downloaded: 10, Total: 10}})
	m.tracker.Update(core.Progress{Completed: 1, Song: &core.SongResult{Song: first, Outcome: "success"}})
	assertFlowText(t, m, "正在下载", "歌曲 1/2", "第一首歌 · 音频", "第二首歌 · 音频 · 排队", "╭", "旧歌词：跳过", "第一首歌：成功", "╰")

	m.handle(resultMsg(core.BatchResult{Songs: []core.SongResult{
		{Song: first, Outcome: "success"}, {Song: second, Outcome: "failed", Errors: []string{"错误细节一", "错误细节二"}},
	}}))
	assertFlowText(t, m, "下载结果", "╭", "成功 1 首 · 已跳过 0 首 · 失败 1 首", "✓ 第一首歌", "✗ 第二首歌", "错误细节一", "错误细节二", "失败歌曲会继续保持选中", "╰", "返回主界面 (Esc)", "退出 (q)")
}

func TestConfirmationCanScrollToLastSong(t *testing.T) {
	m := testModel(t)
	m.width, m.height = 60, 18
	album := core.AlbumDownload{Detail: core.AlbumDetail{Album: core.Album{Name: "专辑"}}}
	for i := range 50 {
		album.Songs = append(album.Songs, core.Song{Name: fmt.Sprintf("测试歌曲 %02d", i)})
	}
	m.handle(prepareMsg{request: m.request, prepared: core.Prepared{Albums: []core.AlbumDownload{album}}})
	m.handle(key(tea.KeyEnd, ""))
	plain := ansi.Strip(m.View().Content)
	if !strings.Contains(plain, "测试歌曲 49") || !strings.Contains(plain, "确认下载") {
		t.Fatal(plain)
	}
}

func assertFlowText(t *testing.T, m *model, expected ...string) {
	t.Helper()
	view := m.View()
	plain := ansi.Strip(view.Content)
	lines := strings.Split(plain, "\n")
	if len(lines) != m.height {
		t.Fatalf("height = %d, want %d", len(lines), m.height)
	}
	for i, line := range lines {
		if width := ansi.StringWidth(line); width != m.width {
			t.Errorf("line %d has width %d, want %d", i, width, m.width)
		}
	}
	for _, item := range expected {
		index := strings.Index(plain, item)
		if index < 0 {
			t.Fatalf("missing or out of order %q in:\n%s", item, ansi.Strip(view.Content))
		}
		plain = plain[index+len(item):]
	}
}
