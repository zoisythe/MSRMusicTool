package tui

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"
	"github.com/zoisythe/MSRMusicTool/internal/core"
)

func testModel(t *testing.T) *model {
	t.Helper()
	path := filepath.Join(t.TempDir(), "test.toml")
	source := "[download]\ndirectory = 'Music'\n[rpc]\nsecret = 'sensitive'\n"
	if err := os.WriteFile(path, []byte(source), 0600); err != nil {
		t.Fatal(err)
	}
	settings, err := core.LoadSettings(path, "")
	if err != nil {
		t.Fatal(err)
	}
	m := newModel(t.Context(), t.Context(), core.NewSite("dev"), settings, "dev")
	m.catalog = core.NewCatalog([]core.Album{{CID: "a1", Name: "中文专辑"}}, []core.Song{{CID: "111111", Name: "中文歌曲", AlbumCID: "a1"}})
	m.rebuild()
	return m
}

func key(code rune, text string) tea.KeyPressMsg { return tea.KeyPressMsg{Code: code, Text: text} }

func TestSettingsPreviewCancelSave(t *testing.T) {
	m := testModel(t)
	m.handle(key(tea.KeyF2, ""))
	if m.view != settingsPage || strings.Contains(m.View().Content, "sensitive") {
		t.Fatal("settings page or secret masking")
	}
	m.form.fields[themeField].value = 1
	m.form.fields[firstColorField+1].input.SetValue("#123456")
	preview := m.form.colors(m.config.TUI, true)
	if preview.Theme != "dark" || preview.Colors["text"] != "#123456" || m.config.TUI.Theme != "terminal" {
		t.Fatal("theme preview is not isolated")
	}
	m.handle(key(tea.KeyEscape, ""))
	if m.form != nil || m.config.TUI.Theme != "terminal" {
		t.Fatal("cancel kept draft")
	}
	m.handle(key(tea.KeyF2, ""))
	m.form.fields[themeField].value = 3
	command := m.saveSettings()
	m.handle(command())
	if m.config.TUI.Theme != "contrast" {
		t.Fatal("theme not saved")
	}
	got, err := core.LoadSettings(m.config.Path, "")
	if err != nil || got.TUI.Theme != "contrast" {
		t.Fatalf("theme did not persist: %v", err)
	}
}

func TestCancelledDetailAndDeferredCatalog(t *testing.T) {
	m := testModel(t)
	m.openDetail()
	old := m.request
	m.handle(key(tea.KeyEscape, ""))
	m.handle(detailMsg{detail: core.AlbumDetail{Album: core.Album{CID: "a1"}}, request: old})
	if m.view != catalogPage || len(m.details) != 0 {
		t.Fatal("cancelled detail changed model")
	}
	m.handle(key(' ', " "))
	if !m.selected["111111"] {
		t.Fatal("space did not select")
	}
	m.view = detailPage
	newCatalog := core.NewCatalog([]core.Album{{CID: "a1", Name: "更新曲库"}}, nil)
	m.handle(catalogMsg{snapshot: core.Snapshot{Schema: 1, FetchedAt: time.Now(), Catalog: newCatalog}, request: m.refreshID})
	if m.deferred == nil || !m.selected["111111"] {
		t.Fatal("catalog changed while browsing details")
	}
	m.handle(key(tea.KeyEscape, ""))
	if m.deferred != nil || m.selected["111111"] || m.catalog.Albums[0].Name != "更新曲库" {
		t.Fatal("catalog was not adopted on return")
	}
}

func TestSearchKeepsNewestInput(t *testing.T) {
	m := testModel(t)
	m.handle(key('/', "/"))
	m.handle(tea.PasteMsg{Content: "中文"})
	m.handle(searchMsg{query: "expired", sequence: m.searchSeq - 1})
	if m.query != "" {
		t.Fatal("stale search applied")
	}
	m.handle(searchMsg{query: "中文", sequence: m.searchSeq})
	if m.query != "中文" || len(m.rows) != 1 || len(m.rows[0].Songs) != 1 {
		t.Fatal("current search was not applied")
	}
	m.handle(key(tea.KeyEscape, ""))
	if m.query != "" || m.searchOpen {
		t.Fatal("escape did not exit search")
	}
}
