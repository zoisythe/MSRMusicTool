package core

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestSettingsRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "legacy.toml")
	old := "[download]\ndirectory = 'Music'\nprogress_style = 'legacy'\n[other]\nvalue = 99\n"
	if err := os.WriteFile(path, []byte(old), 0600); err != nil {
		t.Fatal(err)
	}
	override := filepath.Join(t.TempDir(), "temporary")
	settings, err := LoadSettings(path, override)
	if err != nil {
		t.Fatal(err)
	}
	if settings.Output != override || settings.Download.Concurrency != 4 || !settings.Update.CheckOnStartup {
		t.Fatalf("settings: %+v", settings)
	}
	draft := settings.Config
	draft.Download.Concurrency = 3
	draft.RPC.Secret = "secret"
	draft.TUI = TUIConfig{"dark", map[string]string{"accent": "#1234AB"}}
	draft.Update.CheckOnStartup = false
	saved, err := settings.Save(draft)
	if err != nil {
		t.Fatal(err)
	}
	if saved.Output != override {
		t.Fatal("override changed")
	}
	reloaded, err := LoadSettings(path, "")
	if err != nil {
		t.Fatal(err)
	}
	if want := filepath.Join(filepath.Dir(path), "Music"); reloaded.Output != want {
		t.Fatalf("directory: %s want %s", reloaded.Output, want)
	}
	if reloaded.TUI.Theme != "dark" || reloaded.TUI.Colors["accent"] != "#1234AB" || reloaded.RPC.Secret != "secret" || reloaded.Update.CheckOnStartup {
		t.Fatal("settings lost on reload")
	}
	data, _ := os.ReadFile(path)
	if !strings.Contains(string(data), "progress_style") || !strings.Contains(string(data), "value = 99") {
		t.Fatalf("unknown keys lost: %s", data)
	}
	if _, err := settings.Save(draft); err == nil {
		t.Fatal("accepted stale settings write")
	}
	draft.TUI.Colors["accent"] = "red"
	if _, err := saved.Save(draft); err == nil {
		t.Fatal("accepted invalid color")
	}
	after, _ := os.ReadFile(path)
	if string(after) != string(data) {
		t.Fatal("invalid save modified config")
	}
}

func TestInvalidConfig(t *testing.T) {
	for name, body := range map[string]string{
		"missing download":    "[rpc]\nsecret = 'hidden'",
		"invalid count":       "[download]\ndirectory = '.'\nmax_concurrent_downloads = 33",
		"bad poll":            "[download]\ndirectory = '.'\n[rpc]\npoll_interval = 0.01",
		"relative engine dir": "[download]\ndirectory = '.'\n[rpc]\ndirectory = 'relative'",
	} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "invalid.toml")
			if err := os.WriteFile(path, []byte(body), 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := LoadSettings(path, ""); err == nil {
				t.Fatal("accepted invalid configuration")
			}
		})
	}
}

func TestLegacyCache(t *testing.T) {
	path := filepath.Join(t.TempDir(), "catalog-v1.json")
	body := `{"schema_version":1,"fetched_at":"2026-11-02T08:30:03.123456+00:00","albums":[{"cid":"a1","name":"专辑","cover_url":"https://example.com/a.jpg","artists":["MSR"],"release_rank":0}],"songs":[{"cid":"111111","name":"歌曲","album_cid":"a1","artists":[]}]}`
	if err := os.WriteFile(path, []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
	s, err := LoadCatalog(path)
	if err != nil || s.SongByID["111111"].Name != "歌曲" {
		t.Fatalf("loading legacy cache: %v", err)
	}
	s.FetchedAt = s.FetchedAt.Add(time.Minute)
	if err := SaveCatalog(path, s); err != nil {
		t.Fatal(err)
	}
	actual, err := LoadCatalog(path)
	if err != nil || !actual.FetchedAt.Equal(s.FetchedAt) {
		t.Fatalf("cache roundtrip: %v", err)
	}
	if err := os.WriteFile(path, []byte("bad JSON"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadCatalog(path); err == nil {
		t.Fatal("corrupt cache accepted")
	}
}
