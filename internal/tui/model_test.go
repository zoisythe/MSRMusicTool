package tui

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
	"github.com/zoisythe/MSRMusicTool/internal/core"
	"github.com/zoisythe/MSRMusicTool/internal/install"
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

func TestClassicCatalogFrame(t *testing.T) {
	t.Setenv("TERM", "xterm-256color")
	if value, ok := os.LookupEnv("NO_COLOR"); ok {
		if err := os.Unsetenv("NO_COLOR"); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { os.Setenv("NO_COLOR", value) })
	}
	m := testModel(t)
	v := m.View()
	plain := ansi.Strip(v.Content)
	title, _, _ := strings.Cut(plain, "\n")
	if strings.TrimSpace(title) != core.AppName+" — 塞壬唱片音乐下载器" {
		t.Errorf("unexpected catalog title bar: %q", title)
	}
	for _, want := range []string{"配置：", "下载：", "╭", "中文专辑", "已选择 0 首歌曲", "F2 设置", "q 退出", "→ 进入专辑", "专辑内按 ← 返回曲库"} {
		if !strings.Contains(plain, want) {
			t.Errorf("catalog is missing %q", want)
		}
	}
	if v.BackgroundColor == nil || !strings.Contains(v.Content, "\x1b[48;") {
		t.Error("the default theme must paint cell backgrounds explicitly")
	}
	lines := strings.Split(v.Content, "\n")
	if len(lines) != m.height || !strings.Contains(ansi.Strip(lines[len(lines)-1]), "F2 设置") {
		t.Fatal("shortcut bar must be docked to the last terminal row")
	}
	for _, line := range lines {
		if width := ansi.StringWidth(line); width != m.width {
			t.Fatalf("line width = %d, want %d", width, m.width)
		}
	}
}

func TestCatalogQuitShortcut(t *testing.T) {
	m := testModel(t)
	m.handle(key('/', "/"))
	m.handle(key('q', "q"))
	if !m.searchOpen || m.search.Value() != "q" {
		t.Fatal("q must be accepted as text while editing a search")
	}
	m.handle(key(tea.KeyEnter, ""))
	command := m.handle(key('q', "q"))
	if command == nil {
		t.Fatal("q did not request exit from the catalog")
	}
	if _, ok := command().(tea.QuitMsg); !ok {
		t.Fatal("q did not quit after the search was submitted")
	}
}

func TestInstallActionAfterDecliningStartupOffer(t *testing.T) {
	if !install.Available() {
		t.Skip("PATH installation is available on Windows and Linux")
	}
	dir := t.TempDir()
	t.Setenv("LOCALAPPDATA", dir)
	t.Setenv("XDG_CONFIG_HOME", dir)
	m := testModel(t)
	m.width, m.height = 100, 30
	m.handle(installOfferMsg(true))
	assertFlowText(t, m, "msr-music-tool — 塞壬唱片音乐下载器", "安装到用户目录", "╭", "加入用户 PATH", "╰", "取消", "安装", "Enter 确定")
	decline := clickLabel(t, m, "取消", false)
	m.handle(decline())
	if m.view != catalogPage || install.ShouldOffer() {
		t.Fatal("startup decline was not remembered")
	}
	m.handle(key(tea.KeyF2, ""))
	if !strings.Contains(ansi.Strip(m.View().Content), "安装并加入 PATH") {
		t.Error("settings lacks a visible PATH installation action")
	}
	m.handle(key(tea.KeyEnter, ""))
	if m.view != installPage || m.afterPrompt != settingsPage {
		t.Fatal("Enter on the settings action did not reopen the installation prompt")
	}
	m.handle(tea.WindowSizeMsg{Width: 64, Height: 14})
	assertFlowText(t, m, "安装到用户目录", "╭", "加入用户 PATH", "╰", "取消", "安装")
	m.handle(key(tea.KeyEscape, ""))
	m.handle(key(tea.KeyEnter, ""))
	if m.view != installPage {
		t.Fatal("the action should remain available after another decline")
	}
}

func TestSettingsPreviewCancelSave(t *testing.T) {
	m := testModel(t)
	m.handle(key(tea.KeyF2, ""))
	if m.view != settingsPage || strings.Contains(m.View().Content, "sensitive") {
		t.Fatal("settings page or secret masking")
	}
	m.form.fields[themeField].value = 1
	m.form.fields[firstColorField+1].input.SetValue("#123456")
	preview := m.form.colors(m.config.TUI, true)
	if preview.Theme != "dark" || preview.Colors["text"] != "#123456" || m.config.TUI.Theme != "classic" {
		t.Fatal("theme preview is not isolated")
	}
	m.handle(key(tea.KeyEscape, ""))
	if m.form != nil || m.config.TUI.Theme != "classic" {
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
