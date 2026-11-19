package update

import (
	"bytes"
	"crypto/sha256"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/zoisythe/MSRMusicTool/internal/fsutil"
)

func TestPackagedHandoff(t *testing.T) {
	if testing.Short() {
		t.Skip("builds a release executable")
	}
	temp := t.TempDir()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	binary := BinaryName(runtime.GOOS)
	target := filepath.Join(temp, binary)
	if err := fsutil.Copy(executable, target, 0755); err != nil {
		t.Fatal(err)
	}
	built := filepath.Join(t.TempDir(), binary)
	build := exec.Command("go", "build", "-trimpath", "-ldflags", "-X main.version=0.3.1", "-o", built, "./cmd/msr-music-tool")
	build.Dir = "../.."
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v %s", err, output)
	}
	payload, err := os.ReadFile(built)
	if err != nil {
		t.Fatal(err)
	}
	zipped := zipContent(t, binary, string(payload))
	f := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.Write(zipped) }))
	defer f.Close()
	staged, err := download(t.Context(), f.Client(), Asset{URL: f.URL, Size: int64(len(zipped)), Digest: fmt.Sprintf("sha256:%x", sha256.Sum256(zipped))}, target, binary, nil)
	if err != nil {
		t.Fatal(err)
	}

	held := mkLock(target)
	if ok, err := held.TryLock(); err != nil || !ok {
		t.Fatal("could not lock update", err)
	}
	command := childCommand(staged)
	if output, err := command.CombinedOutput(); err == nil || !strings.Contains(string(output), "另一实例正在更新") {
		t.Fatalf("concurrent updater was not rejected: %v %s", err, output)
	}
	held.Close()
	staged, err = download(t.Context(), f.Client(), Asset{URL: f.URL, Size: int64(len(zipped)), Digest: fmt.Sprintf("sha256:%x", sha256.Sum256(zipped))}, target, binary, nil)
	if err != nil {
		t.Fatal(err)
	}
	command = childCommand(staged)
	var output bytes.Buffer
	// Avoid inherited pipes holding the parent open when the Windows helper creates its console.
	command.Stdout, command.Stderr = &output, &output
	command.WaitDelay = 2 * time.Second
	err = command.Run()
	if runtime.GOOS != "windows" && (err == nil || !strings.Contains(output.String(), "未知子命令")) {
		t.Fatalf("new program should exit on the intentionally invalid restart command: %v %s", err, &output)
	}
	if runtime.GOOS == "windows" && err != nil {
		t.Fatalf("Windows handoff: %v %s", err, &output)
	}
	want := sha256.Sum256(payload)
	deadline := time.Now().Add(8 * time.Second)
	for {
		data, err := os.ReadFile(target)
		if err == nil && sha256.Sum256(data) == want {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("replacement did not commit: %v %s", err, &output)
		}
		time.Sleep(40 * time.Millisecond)
	}
	items, err := os.ReadDir(temp)
	if err != nil {
		t.Fatal(err)
	}
	for _, item := range items {
		if item.Name() != binary && !strings.HasSuffix(item.Name(), ".update.lock") {
			t.Fatalf("unexpected update leftover: %s", item.Name())
		}
	}
}

func childCommand(s Staged) *exec.Cmd {
	command := exec.Command(s.Target, "-test.run=^TestUpdateChild$")
	command.Env = append(os.Environ(), "MSR_TEST_REPLACE_PATH="+s.Path, "MSR_TEST_REPLACE_TARGET="+s.Target, "MSR_TEST_REPLACE_STATE="+s.Original)
	return command
}

func TestUpdateChild(t *testing.T) {
	path := os.Getenv("MSR_TEST_REPLACE_PATH")
	if path == "" {
		return
	}
	err := Apply(Staged{Path: path, Target: os.Getenv("MSR_TEST_REPLACE_TARGET"), Original: os.Getenv("MSR_TEST_REPLACE_STATE")}, []string{"intentionally-invalid-command"})
	if err != nil {
		t.Fatal(err)
	}
}
