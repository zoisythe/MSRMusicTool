package main

import (
	"context"
	"net/url"
	"os"
	"os/signal"
	"runtime/debug"
	"strings"
	"syscall"

	"github.com/zoisythe/MSRMusicTool/internal/command"
	"github.com/zoisythe/MSRMusicTool/internal/core"
	"golang.org/x/term"
)

var version = "dev"

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if version == "dev" {
		if info, ok := debug.ReadBuildInfo(); ok && strings.HasPrefix(info.Main.Version, "v") {
			version = strings.TrimPrefix(info.Main.Version, "v")
		}
	}
	site := core.NewSite(version)
	// The release smoke harness serves synthetic media over loopback.
	if value := os.Getenv("MSR_FIXTURE_URL"); value != "" {
		if u, err := url.Parse(value); err == nil && u.Scheme == "http" && u.Hostname() == "127.0.0.1" && u.User == nil && u.Path == "" && u.RawQuery == "" && u.Fragment == "" {
			site.Base = u.String()
		}
	}
	interactive := term.IsTerminal(int(os.Stdin.Fd())) && term.IsTerminal(int(os.Stdout.Fd()))
	stderrTTY := term.IsTerminal(int(os.Stderr.Fd())) && os.Getenv("TERM") != "dumb" && os.Getenv("NO_COLOR") == ""
	os.Exit(command.Run(ctx, os.Args[1:], command.IO{In: os.Stdin, Out: os.Stdout, Err: os.Stderr, Interactive: interactive, ProgressTerminal: stderrTTY}, site, version))
}
