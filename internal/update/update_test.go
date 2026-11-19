package update

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/zoisythe/MSRMusicTool/internal/fsutil"
)

func TestAssetSelection(t *testing.T) {
	for _, target := range []struct{ os, arch, want string }{
		{"linux", "386", "linux-i686"}, {"linux", "arm", "linux-armv7"}, {"linux", "amd64", "linux-amd64"}, {"linux", "arm64", "linux-arm64"},
		{"windows", "386", "windows-i686"}, {"windows", "amd64", "windows-amd64"}, {"windows", "arm64", "windows-arm64"},
		{"darwin", "amd64", "macos-universal"}, {"darwin", "arm64", "macos-universal"},
	} {
		if got := AssetName(target.os, target.arch); got != "msr-music-tool-"+target.want+".zip" {
			t.Error(got)
		}
	}
	name := AssetName(runtime.GOOS, runtime.GOARCH)
	releases := []Release{
		{Tag: "v0.4.0-rc.2", Prerelease: true, Assets: []Asset{{Name: name}}},
		{Tag: "v0.4.0", Draft: true, Assets: []Asset{{Name: name}}},
		{Tag: "v0.3.1", Assets: []Asset{{Name: name}}},
	}
	if got := Select(releases, "0.3.0", name); got == nil || got.Tag != "v0.3.1" {
		t.Fatalf("stable: %+v", got)
	}
	if got := Select(releases, "0.3.0-rc.1", name); got == nil || got.Tag != "v0.4.0-rc.2" {
		t.Fatalf("prerelease: %+v", got)
	}
	if Select(releases, "dev", name) != nil || Select(releases, "0.4.0", name) != nil {
		t.Fatal("unexpected update")
	}
}

func zipContent(t *testing.T, entry, text string) []byte {
	t.Helper()
	var buffer bytes.Buffer
	writer := zip.NewWriter(&buffer)
	f, err := writer.Create(entry)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Write([]byte(text)); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return buffer.Bytes()
}

func TestStaging(t *testing.T) {
	dir := t.TempDir()
	binary := BinaryName(runtime.GOOS)
	target := filepath.Join(dir, binary)
	if err := os.WriteFile(target, []byte("original"), 0755); err != nil {
		t.Fatal(err)
	}
	for _, scenario := range []struct {
		entry   string
		corrupt bool
		pass    bool
	}{
		{binary, false, true}, {"../" + binary, false, false}, {binary, true, false},
	} {
		content := zipContent(t, scenario.entry, "new version")
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write(content) }))
		digest := fmt.Sprintf("sha256:%x", sha256.Sum256(content))
		if scenario.corrupt {
			digest = fmt.Sprintf("sha256:%064x", 8)
		}
		staged, err := download(t.Context(), server.Client(), Asset{Name: "release.zip", URL: server.URL, Digest: digest, Size: int64(len(content))}, target, binary, nil)
		server.Close()
		if scenario.pass != (err == nil) {
			t.Fatalf("staging: %+v %v", scenario, err)
		}
		if actual, _ := os.ReadFile(target); string(actual) != "original" {
			t.Fatal("staging mutated installed binary")
		}
		if scenario.pass {
			if err := checkOriginal(staged); err != nil {
				t.Fatal(err)
			}
			if data, _ := os.ReadFile(staged.Path); string(data) != "new version" {
				t.Fatal("bad staged binary")
			}
			if err := fsutil.Replace(staged.Path, target); err != nil {
				t.Fatal(err)
			}
			if data, _ := os.ReadFile(target); string(data) != "new version" {
				t.Fatal("bad replacement")
			}
			files, _ := os.ReadDir(dir)
			if len(files) != 1 || files[0].Name() != binary {
				t.Fatalf("unexpected update leftovers %v", files)
			}
			if err := os.WriteFile(target, []byte("original"), 0755); err != nil {
				t.Fatal(err)
			}
		}
	}
}
