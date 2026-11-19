package core

import (
	"context"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/zoisythe/MSRMusicTool/internal/testsite"
)

func TestRealEngines(t *testing.T) {
	for _, key := range []string{"MSR_ARIA2_BINARY", "MSR_ARIA_NEXT_BINARY"} {
		t.Run(key, func(t *testing.T) {
			binary := os.Getenv(key)
			if binary == "" {
				t.Skip("set " + key + " to run real-engine acceptance")
			}
			f := testsite.New()
			defer f.Close()
			rpc := launch(t, binary)
			f.Forward(rpc.config.Endpoint, true, true, false)
			s := NewSite("test")
			s.Base = f.Server.URL
			c, err := s.Catalog(t.Context())
			if err != nil {
				t.Fatal(err)
			}
			settings := Settings{Config: defaults(), Output: t.TempDir()}
			settings.Download.Backend = "aria2"
			settings.RPC = rpc.config
			settings.RPC.Endpoint = f.Server.URL + "/jsonrpc"
			batch := s.Prepare(t.Context(), c, []string{"111111", "222222"}, 4, nil)
			result := s.Download(t.Context(), settings, c, batch, ModeAll, nil)
			if result.Err != nil || len(result.Unresolved) != 0 || len(result.Songs) != 2 ||
				result.Songs[0].Outcome != "success" || result.Songs[1].Outcome != "success" {
				t.Fatalf("RPC downloads: %+v", result)
			}
			if f.Calls("RPC", "aria2.addUri") != 4 || f.Calls("GET", "/a1.jpg") != 1 {
				t.Fatal("recovery resubmitted a task or downloaded the cover twice")
			}
			files, _ := os.ReadDir(settings.Output)
			if len(files) != 6 {
				t.Fatalf("output got %v", files)
			}

			var outside string
			err = rpc.call(t.Context(), "aria2.addUri", &outside, []string{f.Server.URL + "/777777.wav"}, map[string]string{
				"dir": t.TempDir(), "out": "outside.wav", "pause": "true",
			})
			if err != nil {
				t.Fatal(err)
			}
			settings.Output = t.TempDir()
			batch = s.Prepare(t.Context(), c, []string{"777777"}, 4, nil)
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			defer cancel()
			several := 0
			result = s.Download(ctx, settings, c, batch, ModeAudio, func(progress Progress) {
				if progress.Resource.Downloaded > 0 && progress.Resource.Status == "active" {
					several++
				}
				if several == 2 {
					cancel()
				}
			})
			if several < 2 || len(result.Unresolved) != 0 {
				t.Fatalf("RPC cancel: events=%d %+v", several, result)
			}
			files, _ = os.ReadDir(settings.Output)
			if len(files) != 0 {
				t.Fatalf("temporary files not removed: %v", files)
			}
			var status rpcStatus
			err = rpc.call(t.Context(), "aria2.tellStatus", &status, outside, rpcFields)
			if err != nil || status.Status != "paused" {
				t.Fatalf("unowned GID changed: %+v %v", status, err)
			}

			// Only one resource may enter while status is unknown; its GID and temporary output are preserved.
			f.Forward(rpc.config.Endpoint, false, false, true)
			settings.Output = t.TempDir()
			settings.Download.Concurrency = 1
			batch = s.Prepare(t.Context(), c, []string{"777777", "555555"}, 1, nil)
			before := f.Calls("RPC", "aria2.addUri")
			result = s.Download(t.Context(), settings, c, batch, ModeAudio, nil)
			if len(result.Unresolved) != 1 || f.Calls("RPC", "aria2.addUri") != before+1 {
				t.Fatalf("uncertain connection: %+v", result)
			}
			if len(result.Songs) != 2 || result.Songs[0].Outcome != "failed" || result.Songs[1].Outcome != "failed" {
				t.Fatalf("expected failed songs: %+v", result.Songs)
			}
			files, _ = os.ReadDir(settings.Output)
			if len(files) == 0 || !strings.Contains(files[0].Name(), ".part") {
				t.Fatal("missing uncertain transfer file")
			}
		})
	}
}

func launch(t *testing.T, binary string) *rpcBackend {
	t.Helper()
	socket, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := strings.Split(socket.Addr().String(), ":")[1]
	socket.Close()
	dir := t.TempDir()
	log, err := os.Create(filepath.Join(dir, "engine.log"))
	if err != nil {
		t.Fatal(err)
	}
	defer log.Close()
	command := exec.Command(binary, "--enable-rpc=true", "--rpc-listen-all=false", "--rpc-listen-port="+port,
		"--rpc-secret=acceptance-secret", "--dir="+dir, "--console-log-level=warn")
	command.Stdout, command.Stderr = log, log
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { command.Process.Kill(); command.Wait() })
	rpc := newRPC(RPCConfig{Endpoint: "http://127.0.0.1:" + port + "/jsonrpc", Secret: "acceptance-secret", PollInterval: 0.1})
	t.Cleanup(rpc.client.CloseIdleConnections)
	ctx, cancel := context.WithTimeout(t.Context(), 8*time.Second)
	defer cancel()
	for {
		var result struct{ Version string }
		if err := rpc.call(ctx, "aria2.getVersion", &result); err == nil && result.Version != "" {
			t.Log("engine version", result.Version)
			return rpc
		}
		if err := wait(ctx, 50*time.Millisecond); err != nil {
			output, _ := os.ReadFile(log.Name())
			t.Fatalf("starting engine: %v\n%s", err, output)
		}
	}
}
