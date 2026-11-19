package command

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/zoisythe/MSRMusicTool/internal/core"
	"github.com/zoisythe/MSRMusicTool/internal/testsite"
)

func run(t *testing.T, fixture *testsite.Fixture, args ...string) (int, string, string) {
	t.Helper()
	var out, stderr bytes.Buffer
	site := core.NewSite("test")
	site.Base = fixture.Server.URL
	code := Run(t.Context(), args, IO{Out: &out, Err: &stderr}, site, "test")
	return code, out.String(), stderr.String()
}

func TestCLI(t *testing.T) {
	f := testsite.New()
	defer f.Close()
	config := filepath.Join(t.TempDir(), "config.toml")
	if err := os.WriteFile(config, []byte("[download]\ndirectory = 'music'"), 0600); err != nil {
		t.Fatal(err)
	}
	code, out, errText := run(t, f, "--config", config, "search", "First", "中文", "https://monster-siren.hypergryph.com/m/music/111111", "nonexistent")
	if code != 1 || !strings.HasPrefix(out, "ID\t歌曲\t专辑\t艺人\t链接\n") || strings.Count(out, "111111\t") != 1 ||
		!strings.Contains(out, "222222\t中文歌曲") || !strings.Contains(errText, "无匹配：nonexistent") {
		t.Fatalf("%d\n%s\n%s", code, out, errText)
	}
	calls := f.Calls("GET", "/api/albums")
	for _, args := range [][]string{{"download", "１２３４５６"}, {"search", "https://example.org/music/111111"}, {"download", "111111", "--mode", "bad"}, {"search", "First", "--config", config + ".missing"}} {
		code, _, _ := run(t, f, args...)
		if code != 2 || f.Calls("GET", "/api/albums") != calls {
			t.Errorf("validation used network or gave wrong exit code: %v %d", args, code)
		}
	}
}

func TestCLIHTTPTransfers(t *testing.T) {
	for mode, count := range map[string]int{core.ModeAll: 6, core.ModeAudio: 2, core.ModeAudioLyrics: 4} {
		t.Run(mode, func(t *testing.T) {
			f := testsite.New()
			defer f.Close()
			out := t.TempDir()
			code, stdout, stderr := run(t, f, "download", "111111", "222222", "--output", out, "--mode", mode)
			files, _ := os.ReadDir(out)
			if code != 0 || stdout != "" || len(files) != count || !strings.Contains(stderr, "歌曲 0/2") || !strings.Contains(stderr, "歌曲 2/2") {
				t.Fatalf("%d %s files=%v", code, stderr, files)
			}
			if mode == core.ModeAll && f.Calls("GET", "/a1.jpg") != 1 {
				t.Fatal("cover downloaded more than once")
			}
			audio, err := os.ReadFile(filepath.Join(out, "塞壬唱片-MSR - First [Album a1-111111].wav"))
			if err != nil || len(audio) != 64<<10 {
				t.Fatalf("unexpected audio %d %v", len(audio), err)
			}
			if mode != core.ModeAudio {
				lyrics, err := os.ReadFile(filepath.Join(out, "塞壬唱片-MSR - 中文歌曲.lrc"))
				if err != nil || string(lyrics) != "fallback intro" {
					t.Fatalf("lyrics %q %v", lyrics, err)
				}
			}
			code, _, stderr = run(t, f, "download", "111111", "222222", "--output", out, "--mode", mode)
			if code != 0 || !strings.Contains(stderr, "111111 First：skipped") || f.Calls("GET", "/111111.wav") != 1 {
				t.Fatalf("skip failed: %d %s", code, stderr)
			}
		})
	}
	f := testsite.New()
	defer f.Close()
	dir := t.TempDir()
	code, _, stderr := run(t, f, "download", "444444", "555555", "666666", "--output", dir, "--mode", "audio")
	if code != 1 || f.Calls("GET", "/555555.wav") != 2 || !strings.Contains(stderr, "666666 Broken：failed") {
		t.Fatalf("unknown/retry/failure: %d %s", code, stderr)
	}
	files, _ := os.ReadDir(dir)
	if len(files) != 2 {
		t.Fatal(fmt.Sprint(files))
	}
}
