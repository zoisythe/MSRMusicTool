package core

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

type Site struct {
	Base   string
	Client *http.Client
	Agent  string
}

func NewSite(version string) *Site {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DialContext = (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext
	transport.ResponseHeaderTimeout = 60 * time.Second
	transport.DisableCompression = true
	return &Site{SiteURL, &http.Client{Transport: transport}, AppName + "/" + version + " (+" + SiteURL + "/music)"}
}

func (s *Site) request(ctx context.Context, method, address string, headers http.Header) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, method, address, nil)
	if err != nil {
		return nil, err
	}
	if headers != nil {
		req.Header = headers.Clone()
	}
	req.Header.Set("User-Agent", s.Agent)
	req.Header.Set("Accept-Encoding", "identity")
	return s.Client.Do(req)
}

type responseError struct {
	status int
	after  string
}

func (e responseError) Error() string { return fmt.Sprintf("HTTP %d", e.status) }

func responseStatus(r *http.Response) error {
	if r.StatusCode >= 200 && r.StatusCode < 300 {
		return nil
	}
	return responseError{r.StatusCode, r.Header.Get("Retry-After")}
}

func retryable(err error) bool {
	var status responseError
	if errors.As(err, &status) {
		switch status.status {
		case 408, 429, 500, 502, 503, 504:
			return true
		}
		return false
	}
	var network net.Error
	return errors.As(err, &network) || errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, os.ErrDeadlineExceeded)
}

func wait(ctx context.Context, delay time.Duration) error {
	t := time.NewTimer(delay)
	defer t.Stop()
	select {
	case <-t.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func retryDelay(err error, attempt int) time.Duration {
	delay := 250 * time.Millisecond << attempt
	var status responseError
	if errors.As(err, &status) && status.after != "" {
		if seconds, parseErr := strconv.ParseFloat(status.after, 64); parseErr == nil && seconds >= 0 && seconds <= 86400 {
			delay = time.Duration(seconds * float64(time.Second))
		} else if at, parseErr := http.ParseTime(status.after); parseErr == nil {
			delay = max(0, time.Until(at))
		}
	}
	return min(delay, 60*time.Second)
}

func (s *Site) json(ctx context.Context, path string, output any) error {
	var last error
	for attempt := range 4 {
		requestCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
		r, err := s.request(requestCtx, http.MethodGet, s.Base+path, nil)
		if err == nil {
			err = responseStatus(r)
			if err == nil {
				var envelope struct {
					Code *int
					Msg  json.RawMessage
					Data json.RawMessage
				}
				err = json.NewDecoder(io.LimitReader(r.Body, 16<<20)).Decode(&envelope)
				if err == nil {
					r.Body.Close()
					cancel()
					if envelope.Code == nil || *envelope.Code != 0 {
						return fmt.Errorf("%s 返回错误：%s", path, envelope.Msg)
					}
					if len(envelope.Data) == 0 || string(envelope.Data) == "null" {
						return fmt.Errorf("%s 返回空数据", path)
					}
					if err := json.Unmarshal(envelope.Data, output); err != nil {
						return fmt.Errorf("%s 数据格式无效：%w", path, err)
					}
					return nil
				}
				if errors.Is(err, io.EOF) {
					err = io.ErrUnexpectedEOF
				}
			}
			r.Body.Close()
		}
		cancel()
		last = err
		var syntax *json.SyntaxError
		if attempt == 3 || (!retryable(err) && !errors.As(err, &syntax)) {
			break
		}
		if err := wait(ctx, retryDelay(err, attempt)); err != nil {
			return err
		}
	}
	return fmt.Errorf("请求 %s 失败：%w", path, last)
}

type apiSong struct {
	CID      string
	Name     string
	AlbumCID string
	Artists  []string
	Artistes []string
	Source   string `json:"sourceUrl"`
	Lyric    string `json:"lyricUrl"`
}

func (v apiSong) summary(album string) (Song, error) {
	artists := v.Artists
	if album != "" {
		artists = v.Artistes
	} else {
		album = v.AlbumCID
	}
	if v.CID == "" || v.Name == "" || album == "" || artists == nil {
		return Song{}, errors.New("官网歌曲数据格式无效")
	}
	return Song{v.CID, v.Name, album, artists}, nil
}

type apiAlbum struct {
	CID      string
	Name     string
	Cover    string `json:"coverUrl"`
	Artistes []string
	Intro    string
	Songs    []apiSong
}

func (v apiAlbum) summary(rank int) (Album, error) {
	if v.CID == "" || v.Name == "" || v.Cover == "" {
		return Album{}, errors.New("官网专辑数据格式无效")
	}
	return Album{v.CID, v.Name, v.Cover, v.Artistes, rank}, nil
}

func (s *Site) Catalog(ctx context.Context) (Catalog, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	var albums []apiAlbum
	var songs struct{ List []apiSong }
	done := make(chan error, 2)
	go func() { done <- s.json(ctx, "/api/albums", &albums) }()
	go func() { done <- s.json(ctx, "/api/songs", &songs) }()
	for range 2 {
		if err := <-done; err != nil {
			return Catalog{}, err
		}
	}
	if albums == nil || songs.List == nil {
		return Catalog{}, errors.New("官网曲库格式无效")
	}
	albumList := make([]Album, 0, len(albums))
	songList := make([]Song, 0, len(songs.List))
	for i, value := range albums {
		a, err := value.summary(i)
		if err != nil {
			return Catalog{}, err
		}
		if a.Artists == nil {
			return Catalog{}, errors.New("官网专辑缺少艺人列表")
		}
		albumList = append(albumList, a)
	}
	for _, value := range songs.List {
		song, err := value.summary("")
		if err != nil {
			return Catalog{}, err
		}
		songList = append(songList, song)
	}
	catalog := NewCatalog(albumList, songList)
	if len(catalog.AlbumByID) != len(albumList) || len(catalog.SongByID) != len(songList) {
		return Catalog{}, errors.New("官网曲库有重复 CID")
	}
	return catalog, nil
}

func (s *Site) Album(ctx context.Context, cid string) (AlbumDetail, error) {
	var value apiAlbum
	if err := s.json(ctx, "/api/album/"+url.PathEscape(cid)+"/detail", &value); err != nil {
		return AlbumDetail{}, err
	}
	album, err := value.summary(0)
	if err != nil {
		return AlbumDetail{}, err
	}
	if value.Songs == nil || album.CID != cid {
		return AlbumDetail{}, errors.New("官网专辑详情格式无效")
	}
	detail := AlbumDetail{Album: album, Intro: value.Intro}
	for _, item := range value.Songs {
		song, err := item.summary(album.CID)
		if err != nil {
			return AlbumDetail{}, err
		}
		detail.Songs = append(detail.Songs, song)
	}
	return detail, nil
}

func (s *Site) Song(ctx context.Context, cid string) (SongDetail, error) {
	var value apiSong
	if err := s.json(ctx, "/api/song/"+url.PathEscape(cid), &value); err != nil {
		return SongDetail{}, err
	}
	song, err := value.summary("")
	if err != nil {
		return SongDetail{}, err
	}
	if value.Source == "" || song.CID != cid {
		return SongDetail{}, errors.New("官网歌曲详情格式无效")
	}
	return SongDetail{song, value.Source, value.Lyric}, nil
}

func validMedia(address string) bool {
	u, err := url.Parse(address)
	return err == nil && (u.Scheme == "https" || u.Scheme == "http") && u.Host != "" && u.User == nil && !strings.ContainsAny(address, "\r\n")
}
