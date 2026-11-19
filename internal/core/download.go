package core

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"time"

	"github.com/zoisythe/MSRMusicTool/internal/fsutil"
)

const (
	ModeAll         = "all"
	ModeAudio       = "audio"
	ModeAudioLyrics = "audio-lyrics"
)

func ValidMode(mode string) bool {
	return mode == ModeAll || mode == ModeAudio || mode == ModeAudioLyrics
}

type SongResult struct {
	Song    Song
	Outcome string
	Errors  []string
}

type BatchResult struct {
	Songs      []SongResult
	Unresolved []string
	Err        error
}

type AlbumDownload struct {
	Detail AlbumDetail
	Songs  []Song
}

type Prepared struct {
	Albums   []AlbumDownload
	Failures []SongResult
}

func (p Prepared) Count() int {
	count := len(p.Failures)
	for _, album := range p.Albums {
		count += len(album.Songs)
	}
	return count
}

type Resource struct {
	Key        string
	Label      string
	Status     string
	Downloaded int64
	Total      int64
	Speed      float64
	Error      string
}

type Progress struct {
	Resource  Resource
	Completed int
	Song      *SongResult
}

func (s *Site) Prepare(ctx context.Context, c Catalog, ids []string, limit int, cached map[string]AlbumDetail) Prepared {
	selected := make(map[string]bool)
	found := make(map[string]bool)
	var result Prepared
	fail := func(song Song, issue string) {
		found[song.CID] = true
		result.Failures = append(result.Failures, SongResult{song, "failed", []string{issue}})
	}
	for _, id := range ids {
		if selected[id] {
			continue
		}
		selected[id] = true
		if _, ok := c.SongByID[id]; !ok {
			fail(Song{CID: id, Name: id}, "歌曲 ID 不存在："+id)
		}
	}
	type albumInfo struct {
		cid    string
		detail AlbumDetail
		err    error
	}
	var details []albumInfo
	for _, album := range c.Albums {
		if slices.ContainsFunc(c.ByAlbum[album.CID], func(song Song) bool { return selected[song.CID] }) {
			details = append(details, albumInfo{cid: album.CID})
		}
	}
	var workers sync.WaitGroup
	slots := make(chan struct{}, limit)
	for i := range details {
		workers.Go(func() {
			item := &details[i]
			if detail, ok := cached[item.cid]; ok {
				item.detail = detail
				return
			}
			if err := acquire(ctx, slots); err != nil {
				item.err = err
				return
			}
			defer func() { <-slots }()
			item.detail, item.err = s.Album(ctx, item.cid)
		})
	}
	workers.Wait()
	for _, item := range details {
		if item.err != nil {
			for _, song := range c.ByAlbum[item.cid] {
				if selected[song.CID] {
					fail(song, "专辑信息加载失败："+item.err.Error())
				}
			}
			continue
		}
		album := AlbumDownload{Detail: item.detail}
		for _, song := range item.detail.Songs {
			if selected[song.CID] && !found[song.CID] {
				album.Songs = append(album.Songs, song)
				found[song.CID] = true
			}
		}
		if len(album.Songs) > 0 {
			result.Albums = append(result.Albums, album)
		}
	}
	for _, id := range ids {
		if !found[id] {
			fail(c.SongByID[id], "官网专辑详情中缺少歌曲，请刷新索引后重试")
		}
	}
	return result
}

func acquire(ctx context.Context, slots chan struct{}) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	case slots <- struct{}{}:
		return nil
	}
}

func randomID() string {
	var data [16]byte
	rand.Read(data[:])
	return hex.EncodeToString(data[:])
}

type fileInfo struct {
	size      int64
	mediaType string
}
type batch struct {
	site      *Site
	namer     Namer
	output    string
	mode      string
	slots     chan struct{}
	rpc       *rpcBackend
	change    func(Progress)
	state     sync.Mutex
	completed int
	temps     map[string]bool
}

func (s *Site) Download(ctx context.Context, settings Settings, c Catalog, prepared Prepared, mode string, progress func(Progress)) (result BatchResult) {
	result.Songs = append(result.Songs, prepared.Failures...)
	if len(prepared.Albums) == 0 {
		return
	}
	if progress == nil {
		progress = func(Progress) {}
	}
	b := &batch{site: s, namer: NewNamer(c), output: settings.Output, mode: mode, slots: make(chan struct{}, settings.Download.Concurrency), change: progress, completed: len(result.Songs), temps: make(map[string]bool)}
	if err := os.MkdirAll(b.output, 0777); err != nil {
		result.Err = err
		return
	}
	defer func() {
		var unsafe map[string]bool
		if b.rpc != nil {
			result.Unresolved, unsafe = b.rpc.cleanup()
			slices.Sort(result.Unresolved)
		}
		for temp := range b.temps {
			if !unsafe[temp] {
				os.Remove(temp)
				if b.rpc != nil {
					os.Remove(temp + ".aria2")
				}
			}
		}
	}()
	if settings.Download.Backend == "aria2" {
		b.rpc = newRPC(settings.RPC)
		if err := b.rpc.prepare(ctx); err != nil {
			result.Err = err
			return
		}
	}
	var workers sync.WaitGroup
	results := make([]SongResult, prepared.Count()-len(prepared.Failures))
	offset := 0
	for _, album := range prepared.Albums {
		if mode == ModeAll {
			b.progress(album.Detail.CID+":cover", album.Detail.Name+" · 封面")("waiting", 0, -1, 0, "")
		}
		for _, song := range album.Songs {
			b.progress(song.CID+":audio", song.Name+" · 音频")("waiting", 0, -1, 0, "")
			if mode != ModeAudio {
				b.progress(song.CID+":lyrics", song.Name+" · 歌词")("waiting", 0, -1, 0, "")
			}
		}
	}
	for _, album := range prepared.Albums {
		var covers map[string]error
		coverDone := make(chan struct{})
		if mode == ModeAll {
			workers.Go(func() {
				defer close(coverDone)
				covers = b.cover(ctx, album)
			})
		} else {
			close(coverDone)
		}
		for _, song := range album.Songs {
			index := offset
			offset++
			workers.Go(func() {
				r := b.song(ctx, album.Detail, song)
				<-coverDone
				if err, have := covers[song.CID]; have && err != nil {
					r.Outcome = "failed"
					r.Errors = append(r.Errors, err.Error())
				} else if have && r.Outcome == "skipped" {
					r.Outcome = "success"
				}
				results[index] = r
				b.state.Lock()
				b.completed++
				b.change(Progress{Completed: b.completed, Song: &results[index]})
				b.state.Unlock()
			})
		}
	}
	workers.Wait()
	result.Songs = append(result.Songs, results...)
	return
}

func (b *batch) temp() string {
	path := filepath.Join(b.output, ".msr-"+randomID()+".part")
	b.state.Lock()
	b.temps[path] = true
	b.state.Unlock()
	return path
}

func (b *batch) progress(key, label string) func(string, int64, int64, float64, string) {
	return func(status string, downloaded, total int64, speed float64, errorText string) {
		b.state.Lock()
		defer b.state.Unlock()
		b.change(Progress{Resource: Resource{key, label, status, downloaded, total, speed, errorText}, Completed: b.completed})
	}
}

func (b *batch) probe(ctx context.Context, address, method string) fileInfo {
	info := fileInfo{size: -1}
	for attempt := range 4 {
		if acquire(ctx, b.slots) != nil {
			break
		}
		reqCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
		headers := http.Header{}
		if method == http.MethodGet {
			headers.Set("Range", "bytes=0-0")
		}
		r, err := b.site.request(reqCtx, method, address, headers)
		if err == nil {
			err = responseStatus(r)
			if err == nil {
				info = fileInfo{r.ContentLength, r.Header.Get("Content-Type")}
			}
			r.Body.Close()
		}
		cancel()
		<-b.slots
		if err == nil || !retryable(err) || attempt == 3 {
			break
		}
		if wait(ctx, retryDelay(err, attempt)) != nil {
			break
		}
	}
	return info
}

func (b *batch) ext(ctx context.Context, address string, probe fileInfo, fallback string) string {
	extension := Extension(address, probe.mediaType, "")
	if extension == "" && probe.mediaType == "" {
		extension = Extension(address, b.probe(ctx, address, http.MethodGet).mediaType, "")
	}
	if extension == "" {
		return fallback
	}
	return extension
}

func matches(path string, size int64) bool {
	info, err := os.Stat(path)
	return size >= 0 && err == nil && info.Mode().IsRegular() && info.Size() == size
}

func (b *batch) transfer(ctx context.Context, address, temp string, expected int64, progress func(string, int64, int64, float64, string)) (int64, error) {
	if !validMedia(address) {
		return -1, errors.New("媒体 URL 无效")
	}
	if b.rpc != nil {
		if err := acquire(ctx, b.slots); err != nil {
			return -1, err
		}
		defer func() { <-b.slots }()
		size, err := b.rpc.transfer(ctx, address, temp, progress)
		if err == nil && expected >= 0 && size != expected {
			err = fmt.Errorf("下载长度不符：预期 %d，实际 %d", expected, size)
		}
		return size, err
	}
	var last error
	for attempt := range 4 {
		if err := acquire(ctx, b.slots); err != nil {
			return -1, err
		}
		progress("waiting", 0, -1, 0, "")
		size, err := b.httpTransfer(ctx, address, temp, expected, progress)
		<-b.slots
		if err == nil {
			return size, nil
		}
		last = err
		if !retryable(err) || attempt == 3 {
			break
		}
		progress("retrying", 0, -1, 0, err.Error())
		if err := wait(ctx, retryDelay(err, attempt)); err != nil {
			return -1, err
		}
	}
	return -1, last
}

func (b *batch) httpTransfer(ctx context.Context, address, temp string, expected int64, progress func(string, int64, int64, float64, string)) (int64, error) {
	readCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	idle := time.AfterFunc(60*time.Second, cancel)
	defer idle.Stop()
	response, err := b.site.request(readCtx, http.MethodGet, address, nil)
	if err != nil {
		if ctx.Err() == nil && readCtx.Err() != nil {
			return -1, os.ErrDeadlineExceeded
		}
		return -1, err
	}
	defer response.Body.Close()
	if err := responseStatus(response); err != nil {
		return -1, err
	}
	f, err := os.OpenFile(temp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0600)
	if err != nil {
		return -1, err
	}
	defer f.Close()
	start := time.Now()
	last := start
	var done int64
	total := response.ContentLength
	progress("active", 0, total, 0, "")
	buf := make([]byte, 64<<10)
	for {
		n, readErr := response.Body.Read(buf)
		if n > 0 {
			idle.Reset(60 * time.Second)
			if _, err := f.Write(buf[:n]); err != nil {
				return -1, err
			}
			done += int64(n)
			if time.Since(last) >= 100*time.Millisecond {
				progress("active", done, total, float64(done)/time.Since(start).Seconds(), "")
				last = time.Now()
			}
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			if ctx.Err() == nil && readCtx.Err() != nil {
				return -1, os.ErrDeadlineExceeded
			}
			return -1, readErr
		}
	}
	if err := ctx.Err(); err != nil {
		return -1, err
	}
	if total >= 0 && done != total || expected >= 0 && done != expected {
		return -1, fmt.Errorf("下载长度不符（实际 %d 字节）：%w", done, io.ErrUnexpectedEOF)
	}
	if err := f.Close(); err != nil {
		return -1, err
	}
	return done, nil
}

func (b *batch) remote(ctx context.Context, address, target string, info fileInfo, progress func(string, int64, int64, float64, string)) (bool, error) {
	if matches(target, info.size) {
		progress("skipped", info.size, info.size, 0, "")
		return false, nil
	}
	temp := b.temp()
	size, err := b.transfer(ctx, address, temp, info.size, progress)
	if err == nil {
		err = ctx.Err()
	}
	if err == nil {
		err = fsutil.Replace(temp, target)
	}
	if err != nil {
		progress("failed", 0, -1, 0, err.Error())
		return false, err
	}
	progress("complete", size, size, 0, "")
	return true, nil
}

func (b *batch) local(ctx context.Context, content []byte, target string, progress func(string, int64, int64, float64, string)) (bool, error) {
	size := int64(len(content))
	if matches(target, size) {
		if current, err := os.ReadFile(target); err == nil && bytes.Equal(current, content) {
			progress("skipped", size, size, 0, "")
			return false, nil
		}
	}
	err := fsutil.Write(target, 0600, func(w io.Writer) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		_, err := w.Write(content)
		return err
	})
	if err != nil {
		progress("failed", 0, -1, 0, err.Error())
		return false, err
	}
	progress("complete", size, size, 0, "")
	return true, nil
}

func (b *batch) song(ctx context.Context, album AlbumDetail, song Song) SongResult {
	r := SongResult{Song: song, Outcome: "skipped"}
	if err := acquire(ctx, b.slots); err != nil {
		return SongResult{song, "failed", []string{err.Error()}}
	}
	detail, err := b.site.Song(ctx, song.CID)
	<-b.slots
	if err != nil {
		return SongResult{song, "failed", []string{"读取歌曲信息失败: " + err.Error()}}
	}
	type result struct {
		kind    string
		changed bool
		err     error
	}
	assets := make(chan result, 2)
	go func() {
		info := b.probe(ctx, detail.Source, http.MethodHead)
		target := filepath.Join(b.output, b.namer.Filename(song, b.ext(ctx, detail.Source, info, ".bin")))
		changed, err := b.remote(ctx, detail.Source, target, info, b.progress(song.CID+":audio", song.Name+" · 音频"))
		assets <- result{"音频", changed, err}
	}()
	count := 1
	if b.mode != ModeAudio {
		count++
		go func() {
			target := filepath.Join(b.output, b.namer.Filename(song, ".lrc"))
			report := b.progress(song.CID+":lyrics", song.Name+" · 歌词")
			var changed bool
			var err error
			if detail.Lyric != "" {
				changed, err = b.remote(ctx, detail.Lyric, target, b.probe(ctx, detail.Lyric, http.MethodHead), report)
			} else {
				changed, err = b.local(ctx, []byte(album.Intro), target, report)
			}
			assets <- result{"歌词", changed, err}
		}()
	}
	for range count {
		a := <-assets
		if a.err != nil {
			r.Outcome = "failed"
			r.Errors = append(r.Errors, a.kind+"："+a.err.Error())
		} else if a.changed && r.Outcome != "failed" {
			r.Outcome = "success"
		}
	}
	return r
}

func (b *batch) cover(ctx context.Context, album AlbumDownload) map[string]error {
	info := b.probe(ctx, album.Detail.Cover, http.MethodHead)
	ext := b.ext(ctx, album.Detail.Cover, info, ".img")
	pending := make(map[string]string)
	report := b.progress(album.Detail.CID+":cover", album.Detail.Name+" · 封面")
	for _, song := range album.Songs {
		path := filepath.Join(b.output, b.namer.Filename(song, ext))
		if !matches(path, info.size) {
			pending[song.CID] = path
		}
	}
	result := make(map[string]error)
	if len(pending) == 0 {
		report("skipped", info.size, info.size, 0, "")
		return result
	}
	temp := b.temp()
	size, err := b.transfer(ctx, album.Detail.Cover, temp, info.size, report)
	if err == nil {
		err = ctx.Err()
	}
	var copyErr error
	for cid, path := range pending {
		if err != nil {
			result[cid] = fmt.Errorf("封面：%w", err)
			continue
		}
		result[cid] = fsutil.Copy(temp, path, 0600)
		if result[cid] != nil {
			copyErr = result[cid]
		}
	}
	if err == nil {
		err = copyErr
	}
	if err != nil {
		report("failed", 0, -1, 0, err.Error())
	} else {
		report("complete", size, size, 0, "")
	}
	return result
}

func (s *Site) CheckMedia(ctx context.Context, address string) (string, error) {
	b := batch{site: s, slots: make(chan struct{}, 1)}
	info := b.probe(ctx, address, http.MethodHead)
	if info.mediaType == "" {
		info = b.probe(ctx, address, http.MethodGet)
	}
	if info.mediaType == "" {
		return "", errors.New("媒体响应缺少 Content-Type")
	}
	return info.mediaType, nil
}
