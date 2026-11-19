package core

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/gofrs/flock"
	"github.com/zoisythe/MSRMusicTool/internal/fsutil"
	"golang.org/x/text/cases"
)

const SiteURL = "https://monster-siren.hypergryph.com"

type Album struct {
	CID     string   `json:"cid"`
	Name    string   `json:"name"`
	Cover   string   `json:"cover_url"`
	Artists []string `json:"artists"`
	Rank    int      `json:"release_rank"`
}

type Song struct {
	CID      string   `json:"cid"`
	Name     string   `json:"name"`
	AlbumCID string   `json:"album_cid"`
	Artists  []string `json:"artists"`
}

type AlbumDetail struct {
	Album
	Intro string
	Songs []Song
}

type SongDetail struct {
	Song
	Source string
	Lyric  string
}

type Catalog struct {
	Albums    []Album           `json:"albums"`
	Songs     []Song            `json:"songs"`
	AlbumByID map[string]Album  `json:"-"`
	SongByID  map[string]Song   `json:"-"`
	ByAlbum   map[string][]Song `json:"-"`
}

func NewCatalog(albums []Album, songs []Song) Catalog {
	c := Catalog{albums, songs, map[string]Album{}, map[string]Song{}, map[string][]Song{}}
	for _, album := range albums {
		c.AlbumByID[album.CID] = album
	}
	for _, song := range songs {
		c.SongByID[song.CID] = song
		c.ByAlbum[song.AlbumCID] = append(c.ByAlbum[song.AlbumCID], song)
	}
	return c
}

type Snapshot struct {
	Schema    int       `json:"schema_version"`
	FetchedAt time.Time `json:"fetched_at"`
	Catalog
}

func LoadCatalog(path string) (Snapshot, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Snapshot{}, err
	}
	var s Snapshot
	if err := json.Unmarshal(data, &s); err != nil {
		return Snapshot{}, err
	}
	if s.Schema != 1 || s.FetchedAt.IsZero() || s.Albums == nil || s.Songs == nil {
		return Snapshot{}, errors.New("无效的曲库缓存")
	}
	for _, album := range s.Albums {
		if album.CID == "" || album.Artists == nil {
			return Snapshot{}, errors.New("无效的专辑缓存")
		}
	}
	for _, song := range s.Songs {
		if song.CID == "" || song.Artists == nil {
			return Snapshot{}, errors.New("无效的歌曲缓存")
		}
	}
	s.Catalog = NewCatalog(s.Albums, s.Songs)
	if len(s.AlbumByID) != len(s.Albums) || len(s.SongByID) != len(s.Songs) {
		return Snapshot{}, errors.New("曲库缓存有重复 CID")
	}
	return s, nil
}

func SaveCatalog(path string, s Snapshot) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	lock := flock.New(path + ".lock")
	defer lock.Close()
	if err := lock.Lock(); err != nil {
		return err
	}
	if current, err := LoadCatalog(path); err == nil && current.FetchedAt.After(s.FetchedAt) {
		return nil
	}
	s.Schema = 1
	return fsutil.Write(path, 0600, func(w io.Writer) error { return json.NewEncoder(w).Encode(s) })
}

var SongID = regexp.MustCompile("^[0-9]{6}$")
var songPath = regexp.MustCompile("^/(?:m/)?music/([0-9]{6})/?$")

func QueryID(query string) (string, error) {
	query = strings.TrimSpace(query)
	if SongID.MatchString(query) {
		return query, nil
	}
	if !strings.Contains(query, "://") {
		return "", nil
	}
	u, err := url.Parse(query)
	if err == nil && (u.Scheme == "http" || u.Scheme == "https") && strings.EqualFold(u.Hostname(), "monster-siren.hypergryph.com") && u.User == nil && (u.Port() == "" || u.Port() == "80" || u.Port() == "443") {
		if match := songPath.FindStringSubmatch(u.EscapedPath()); match != nil {
			return match[1], nil
		}
	}
	return "", fmt.Errorf("无效的官网歌曲链接：%s", query)
}

func Fold(value string) string { return cases.Fold().String(value) }

func (c Catalog) Search(queries []string) (results []Song, missing []string) {
	seen := make(map[string]bool)
	for _, query := range queries {
		id, _ := QueryID(query)
		needle := Fold(strings.TrimSpace(query))
		found := false
		for _, song := range c.Songs {
			if id != "" && song.CID != id || id == "" && !strings.Contains(Fold(song.Name), needle) {
				continue
			}
			found = true
			if !seen[song.CID] {
				results = append(results, song)
				seen[song.CID] = true
			}
		}
		if !found {
			missing = append(missing, query)
		}
	}
	return results, missing
}

type Match struct {
	Album Album
	Count int
	Total int
	Songs []Song
}

func (m Match) State() int {
	if m.Count == 0 {
		return 0
	}
	if m.Count == m.Total {
		return 2
	}
	return 1
}

func (c Catalog) MatchAlbums(query string, selected map[string]bool) []Match {
	needle := Fold(strings.TrimSpace(query))
	var matches []Match
	for _, album := range c.Albums {
		group := c.ByAlbum[album.CID]
		m := Match{Album: album, Total: len(group)}
		for _, song := range group {
			if selected[song.CID] {
				m.Count++
			}
			if needle != "" && strings.Contains(Fold(song.Name), needle) {
				m.Songs = append(m.Songs, song)
			}
		}
		if needle == "" || strings.Contains(Fold(album.Name), needle) || len(m.Songs) > 0 {
			matches = append(matches, m)
		}
	}
	slices.SortStableFunc(matches, func(a, b Match) int { return b.State() - a.State() })
	return matches
}
