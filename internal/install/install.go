package install

import (
	"bytes"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/gofrs/flock"
	"github.com/zoisythe/MSRMusicTool/internal/core"
	"github.com/zoisythe/MSRMusicTool/internal/fsutil"
)

func Available() bool { return runtime.GOOS == "windows" || runtime.GOOS == "linux" }

func Target() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	name := core.AppName
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	return filepath.Join(home, ".local", "bin", name), nil
}

func StatePath() string { return filepath.Join(filepath.Dir(core.ConfigPath()), "install-v1") }

func Remember() error {
	return fsutil.Write(StatePath(), 0600, func(w io.Writer) error { _, err := io.WriteString(w, "prompted\n"); return err })
}

func ShouldOffer() bool {
	if !Available() {
		return false
	}
	if _, err := os.Stat(StatePath()); err == nil {
		return false
	}
	target, err := Target()
	if err != nil {
		return false
	}
	info, err := os.Stat(target)
	if err != nil || !info.Mode().IsRegular() {
		return true
	}
	return !inPath(os.Getenv("PATH"), filepath.Dir(target)) && !persistedPath(filepath.Dir(target))
}

func inPath(list, directory string) bool {
	for _, entry := range filepath.SplitList(list) {
		entry = strings.Trim(strings.TrimSpace(entry), "\"")
		if entry == "" || !filepath.IsAbs(entry) {
			continue
		}
		if samePath(entry, directory) {
			return true
		}
	}
	return false
}

func samePath(a, b string) bool {
	a, b = filepath.Clean(a), filepath.Clean(b)
	return a == b || runtime.GOOS == "windows" && strings.EqualFold(a, b)
}

func digest(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	h := sha256.New()
	_, err = io.Copy(h, f)
	return h.Sum(nil), err
}

var ErrConflict = errors.New("同名程序已存在，需确认覆盖")

func Run(overwrite bool) (string, error) {
	target, err := Target()
	if err != nil {
		return "", err
	}
	dir := filepath.Dir(target)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return target, err
	}
	lock := flock.New(filepath.Join(dir, ".msr-install.lock"))
	acquired, err := lock.TryLock()
	if err != nil {
		return target, err
	}
	if !acquired {
		return target, errors.New("另一实例正在安装")
	}
	defer lock.Close()
	source, err := os.Executable()
	if err != nil {
		return "", err
	}
	if !samePath(source, target) {
		if _, err := os.Lstat(target); err == nil {
			old, oldErr := digest(target)
			current, currentErr := digest(source)
			if oldErr != nil || currentErr != nil || !bytes.Equal(old, current) {
				if !overwrite {
					return target, ErrConflict
				}
				if err := fsutil.Copy(source, target, 0755); err != nil {
					return target, err
				}
			}
		} else if errors.Is(err, os.ErrNotExist) {
			if err := fsutil.Copy(source, target, 0755); err != nil {
				return target, err
			}
		} else {
			return target, err
		}
	}
	if err := addPath(dir); err != nil {
		return target, fmt.Errorf("程序位于 %s，请手动把 %s 加入用户 PATH：%w", target, dir, err)
	}
	if !inPath(os.Getenv("PATH"), dir) {
		value := os.Getenv("PATH")
		if value != "" {
			value += string(os.PathListSeparator)
		}
		if err := os.Setenv("PATH", value+dir); err != nil {
			return target, err
		}
	}
	return target, Remember()
}
