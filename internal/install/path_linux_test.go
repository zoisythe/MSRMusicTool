package install

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBashProfileInstallation(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("SHELL", "/bin/bash")
	dir := filepath.Join(home, ".local", "bin")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	profile := filepath.Join(home, ".bash_profile")
	if err := os.WriteFile(profile, []byte("export EXISTING=1\n"), 0640); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := addPath(dir); err != nil {
			t.Fatal(err)
		}
	}
	if !persistedPath(dir) {
		t.Fatal("expected persisted PATH")
	}
	content, err := os.ReadFile(profile)
	if err != nil || !strings.Contains(string(content), "export EXISTING=1") ||
		strings.Count(string(content), marker) != 1 {
		t.Fatalf("profile changed incorrectly: %q %v", content, err)
	}
}
