package update

import (
	"archive/zip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/gofrs/flock"
	"github.com/zoisythe/MSRMusicTool/internal/core"
	"golang.org/x/mod/semver"
)

const releaseAPI = "https://api.github.com/repos/zoisythe/MSRMusicTool/releases"
const maxArchive = 100 << 20
const maxExecutable = 100 << 20

type Asset struct {
	Name   string
	URL    string `json:"browser_download_url"`
	Digest string
	Size   int64
}

type Release struct {
	Tag        string `json:"tag_name"`
	URL        string `json:"html_url"`
	Draft      bool
	Prerelease bool
	Assets     []Asset
}

type Candidate struct {
	Tag   string
	URL   string
	Asset Asset
}

func AssetName(goos, goarch string) string {
	switch goos {
	case "darwin":
		if goarch != "amd64" && goarch != "arm64" {
			return ""
		}
		goos, goarch = "macos", "universal"
	case "linux", "windows":
		switch goarch {
		case "386":
			goarch = "i686"
		case "arm":
			if goos != "linux" {
				return ""
			}
			goarch = "armv7"
		case "amd64", "arm64":
		default:
			return ""
		}
	default:
		return ""
	}
	return core.AppName + "-" + goos + "-" + goarch + ".zip"
}

func BinaryName(goos string) string {
	if goos == "windows" {
		return core.AppName + ".exe"
	}
	return core.AppName
}

func Select(releases []Release, version, assetName string) *Candidate {
	current := "v" + strings.TrimPrefix(version, "v")
	if assetName == "" || !semver.IsValid(current) {
		return nil
	}
	preview := semver.Prerelease(current) != ""
	var latest *Candidate
	for _, release := range releases {
		if release.Draft || !semver.IsValid(release.Tag) || semver.Compare(release.Tag, current) <= 0 ||
			(!preview && (release.Prerelease || semver.Prerelease(release.Tag) != "")) {
			continue
		}
		if latest != nil && semver.Compare(latest.Tag, release.Tag) >= 0 {
			continue
		}
		count := 0
		var asset Asset
		for _, entry := range release.Assets {
			if entry.Name == assetName {
				count++
				asset = entry
			}
		}
		if count == 1 {
			latest = &Candidate{release.Tag, release.URL, asset}
		}
	}
	return latest
}

func trusted(address *url.URL, assets bool) bool {
	if address == nil || address.Scheme != "https" || address.User != nil || address.Port() != "" {
		return false
	}
	if !assets {
		return address.Hostname() == "api.github.com"
	}
	return slices.Contains([]string{"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}, address.Hostname())
}

func githubClient(assets bool) *http.Client {
	return &http.Client{Timeout: 2 * time.Minute, CheckRedirect: func(req *http.Request, previous []*http.Request) error {
		if len(previous) >= 6 || !trusted(req.URL, assets) {
			return errors.New("GitHub 重定向目标无效")
		}
		return nil
	}}
}

func Check(ctx context.Context, version string) (*Candidate, error) {
	if !semver.IsValid("v" + strings.TrimPrefix(version, "v")) {
		return nil, nil
	}
	assetName := AssetName(runtime.GOOS, runtime.GOARCH)
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	client := githubClient(false)
	defer client.CloseIdleConnections()
	var releases []Release
	for page := 1; page <= 3; page++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, releaseAPI+"?per_page=30&page="+strconv.Itoa(page), nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set("Accept", "application/vnd.github+json")
		req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
		resp, err := client.Do(req)
		if err != nil {
			return nil, err
		}
		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			return nil, fmt.Errorf("GitHub Releases 返回 HTTP %d", resp.StatusCode)
		}
		var chunk []Release
		err = json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&chunk)
		resp.Body.Close()
		if err != nil {
			return nil, err
		}
		releases = append(releases, chunk...)
		if len(chunk) < 30 {
			break
		}
	}
	candidate := Select(releases, version, assetName)
	if candidate != nil {
		if _, err := digestBytes(candidate.Asset.Digest); err != nil {
			return nil, err
		}
	}
	return candidate, nil
}

func digestBytes(value string) ([]byte, error) {
	if !strings.HasPrefix(value, "sha256:") {
		return nil, errors.New("更新资产缺少 SHA-256 摘要")
	}
	digest, err := hex.DecodeString(strings.TrimPrefix(value, "sha256:"))
	if err != nil || len(digest) != sha256.Size {
		return nil, errors.New("更新资产的 SHA-256 摘要无效")
	}
	return digest, nil
}

func state(info os.FileInfo) string {
	return fmt.Sprintf("%d:%d", info.Size(), info.ModTime().UnixNano())
}

func mkLock(target string) *flock.Flock {
	return flock.New(filepath.Join(filepath.Dir(target), "."+filepath.Base(target)+".update.lock"))
}

type Staged struct {
	Target   string
	Path     string
	Original string
}

func executable() (string, error) {
	current, err := os.Executable()
	if err != nil {
		return "", err
	}
	return filepath.EvalSymlinks(current)
}

func Download(ctx context.Context, candidate Candidate, progress func(int64, int64)) (Staged, error) {
	current, err := executable()
	if err != nil {
		return Staged{}, err
	}
	u, err := url.Parse(candidate.Asset.URL)
	expectedPath := "/zoisythe/MSRMusicTool/releases/download/" + candidate.Tag + "/" + candidate.Asset.Name
	if err != nil || !trusted(u, true) || u.Hostname() != "github.com" || u.Path != expectedPath ||
		candidate.Asset.Name != AssetName(runtime.GOOS, runtime.GOARCH) {
		return Staged{}, errors.New("更新资产地址或架构无效")
	}
	client := githubClient(true)
	defer client.CloseIdleConnections()
	return download(ctx, client, candidate.Asset, current, BinaryName(runtime.GOOS), progress)
}

func download(ctx context.Context, client *http.Client, asset Asset, target, binary string, progress func(int64, int64)) (Staged, error) {
	digest, err := digestBytes(asset.Digest)
	if err != nil {
		return Staged{}, err
	}
	if asset.Size <= 0 || asset.Size > maxArchive {
		return Staged{}, errors.New("更新 ZIP 大小无效")
	}
	info, err := os.Stat(target)
	if err != nil {
		return Staged{}, err
	}
	if !info.Mode().IsRegular() {
		return Staged{}, errors.New("更新目标不是普通文件")
	}
	dir := filepath.Dir(target)
	archive, err := os.CreateTemp(dir, ".msr-archive-*.zip")
	if err != nil {
		return Staged{}, err
	}
	defer os.Remove(archive.Name())
	defer archive.Close()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, asset.URL, nil)
	if err != nil {
		return Staged{}, err
	}
	req.Header.Set("Accept-Encoding", "identity")
	resp, err := client.Do(req)
	if err != nil {
		return Staged{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return Staged{}, fmt.Errorf("下载更新返回 HTTP %d", resp.StatusCode)
	}
	h := sha256.New()
	reader := io.LimitReader(resp.Body, asset.Size+1)
	w := io.MultiWriter(h, archive)
	if progress != nil {
		w = io.MultiWriter(w, &counter{callback: progress, total: asset.Size})
	}
	size, err := io.Copy(w, reader)
	if err != nil {
		return Staged{}, err
	}
	if size != asset.Size || !slices.Equal(h.Sum(nil), digest) {
		return Staged{}, errors.New("更新 ZIP 的 SHA-256 或大小不符")
	}
	if err := archive.Close(); err != nil {
		return Staged{}, err
	}
	path, err := unpack(archive.Name(), dir, binary)
	if err != nil {
		return Staged{}, err
	}
	return Staged{target, path, state(info)}, nil
}

type counter struct {
	callback    func(int64, int64)
	total, done int64
	last        time.Time
}

func (c *counter) Write(data []byte) (int, error) {
	c.done += int64(len(data))
	if time.Since(c.last) >= 200*time.Millisecond || c.done == c.total {
		c.callback(c.done, c.total)
		c.last = time.Now()
	}
	return len(data), nil
}

func unpack(archivePath, dir, binary string) (string, error) {
	a, err := zip.OpenReader(archivePath)
	if err != nil {
		return "", err
	}
	defer a.Close()
	if len(a.File) != 1 || a.File[0].Name != binary || !a.File[0].Mode().IsRegular() {
		return "", errors.New("更新 ZIP 必须只包含一个名称匹配的普通程序文件")
	}
	entry := a.File[0]
	if entry.UncompressedSize64 == 0 || entry.UncompressedSize64 > maxExecutable {
		return "", errors.New("更新程序大小无效")
	}
	reader, err := entry.Open()
	if err != nil {
		return "", err
	}
	defer reader.Close()
	pattern := ".msr-update-*"
	if strings.HasSuffix(binary, ".exe") {
		pattern += ".exe"
	}
	f, err := os.CreateTemp(dir, pattern)
	if err != nil {
		return "", err
	}
	var complete bool
	defer func() {
		f.Close()
		if !complete {
			os.Remove(f.Name())
		}
	}()
	count, err := io.Copy(f, io.LimitReader(reader, maxExecutable+1))
	if err != nil {
		return "", err
	}
	if uint64(count) != entry.UncompressedSize64 {
		return "", errors.New("更新程序未完整解压")
	}
	if err := f.Chmod(0755); err != nil {
		return "", err
	}
	if err := f.Sync(); err != nil {
		return "", err
	}
	if err := f.Close(); err != nil {
		return "", err
	}
	complete = true
	return f.Name(), nil
}

func checkOriginal(s Staged) error {
	info, err := os.Stat(s.Target)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || state(info) != s.Original {
		return errors.New("更新期间原程序已改变，请重新启动")
	}
	return nil
}
