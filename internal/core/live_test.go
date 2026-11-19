package core

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

func TestLiveSite(t *testing.T) {
	if os.Getenv("MSR_RUN_LIVE_TESTS") != "1" {
		t.Skip("set MSR_RUN_LIVE_TESTS=1")
	}
	ctx, cancel := context.WithTimeout(t.Context(), 75*time.Second)
	defer cancel()
	site := NewSite("test")
	c, err := site.Catalog(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Albums) == 0 || len(c.Songs) == 0 {
		t.Fatal("empty catalog")
	}
	a, err := site.Album(ctx, c.Albums[0].CID)
	if err != nil {
		t.Fatal(err)
	}
	if len(a.Songs) == 0 {
		t.Fatal("empty album")
	}
	s, err := site.Song(ctx, a.Songs[0].CID)
	if err != nil {
		t.Fatal(err)
	}
	cover, err := site.CheckMedia(ctx, a.Cover)
	if err != nil || !strings.HasPrefix(cover, "image/") {
		t.Fatalf("cover: %q %v", cover, err)
	}
	audio, err := site.CheckMedia(ctx, s.Source)
	if err != nil || (!strings.HasPrefix(audio, "audio/") && !strings.HasPrefix(audio, "application/")) {
		t.Fatalf("audio: %q %v", audio, err)
	}
}
