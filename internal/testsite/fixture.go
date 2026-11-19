// Package testsite serves the public MSR wire format with synthetic media.
package testsite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"time"
)

type Fixture struct {
	Server      *httptest.Server
	mu          sync.Mutex
	calls       map[string]int
	rpc         string
	loseAdd     bool
	failPoll    bool
	failForever bool
}

func New() *Fixture {
	f := &Fixture{calls: make(map[string]int)}
	f.Server = httptest.NewServer(http.HandlerFunc(f.serve))
	return f
}

func (f *Fixture) Close() { f.Server.Close() }

func (f *Fixture) Calls(method, path string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls[method+" "+path]
}

func (f *Fixture) Forward(endpoint string, loseAdd, failPoll, failForever bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.rpc, f.loseAdd, f.failPoll, f.failForever = endpoint, loseAdd, failPoll, failForever
}

func (f *Fixture) count(method, path string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	key := method + " " + path
	f.calls[key]++
	return f.calls[key]
}

func (f *Fixture) serve(w http.ResponseWriter, r *http.Request) {
	count := f.count(r.Method, r.URL.Path)
	if r.URL.Path == "/jsonrpc" {
		f.forwardRPC(w, r)
		return
	}
	ids := []string{"111111", "222222", "333333", "444444", "555555", "666666", "777777"}
	titles := []string{"First", "中文歌曲", "First", "Unknown length", "Retry", "Broken", "Long"}
	api := strings.Split(r.URL.Path, "/")
	if len(api) > 2 && api[1] == "api" {
		album := func(id string) map[string]any {
			return map[string]any{"cid": id, "name": "Album " + id, "artistes": []string{"MSR"}, "coverUrl": f.Server.URL + "/" + id + ".jpg"}
		}
		var songs []map[string]any
		for i, id := range ids {
			cid := "a1"
			if id == "333333" {
				cid = "a2"
			}
			song := map[string]any{"cid": id, "name": titles[i], "albumCid": cid, "artists": []string{"MSR"}, "artistes": []string{"MSR"}, "sourceUrl": f.Server.URL + "/" + id + ".wav"}
			if id == "111111" {
				song["lyricUrl"] = f.Server.URL + "/111111.lrc"
			}
			songs = append(songs, song)
		}
		var data any
		switch api[2] {
		case "albums":
			data = []any{album("a1"), album("a2")}
		case "songs":
			data = map[string]any{"list": songs}
		case "album":
			id := api[3]
			a := album(id)
			a["intro"] = "fallback intro"
			var group []map[string]any
			for _, s := range songs {
				if s["albumCid"] == id {
					group = append(group, s)
				}
			}
			a["songs"] = group
			data = a
		case "song":
			for _, s := range songs {
				if s["cid"] == api[3] {
					data = s
					break
				}
			}
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": data})
		return
	}
	if r.URL.Path == "/666666.wav" {
		http.NotFound(w, r)
		return
	}
	if r.URL.Path == "/555555.wav" && r.Method == http.MethodGet && count == 1 {
		w.Header().Set("Retry-After", "0")
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	contentType, body := "image/jpeg", "synthetic-cover"
	switch {
	case strings.HasSuffix(r.URL.Path, ".lrc"):
		contentType, body = "text/plain", "[00:01] lyric"
	case strings.HasSuffix(r.URL.Path, ".wav"):
		contentType, body = "audio/wav", strings.Repeat("x", 64<<10)
	}
	w.Header().Set("Content-Type", contentType)
	if r.URL.Path == "/777777.wav" && r.Method != http.MethodHead {
		chunk := strings.Repeat("x", 8192)
		w.(http.Flusher).Flush()
		for range 1000 {
			select {
			case <-r.Context().Done():
				return
			case <-time.After(8 * time.Millisecond):
				if _, err := io.WriteString(w, chunk); err != nil {
					return
				}
				w.(http.Flusher).Flush()
			}
		}
		return
	}
	if r.URL.Path == "/444444.wav" {
		w.(http.Flusher).Flush()
	} else {
		w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	}
	io.WriteString(w, body)
}

func (f *Fixture) forwardRPC(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "body", 400)
		return
	}
	var method struct{ Method string }
	if err := json.Unmarshal(body, &method); err != nil {
		http.Error(w, "JSON", 400)
		return
	}
	f.count("RPC", method.Method)
	f.mu.Lock()
	endpoint := f.rpc
	lose := method.Method == "aria2.addUri" && f.loseAdd
	drop := method.Method == "aria2.tellStatus" && (f.failPoll || f.failForever)
	if lose {
		f.loseAdd = false
	}
	if drop {
		f.failPoll = false
	}
	f.mu.Unlock()
	dropConnection := func() {
		connection, _, err := w.(http.Hijacker).Hijack()
		if err == nil {
			connection.Close()
		}
	}
	if drop {
		dropConnection()
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, strings.NewReader(string(body)))
	if err != nil {
		http.Error(w, "endpoint", 502)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		http.Error(w, err.Error(), 502)
		return
	}
	defer resp.Body.Close()
	if lose {
		dropConnection()
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}
