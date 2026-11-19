package install

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/zoisythe/MSRMusicTool/internal/fsutil"
)

const marker = "# msr-music-tool PATH"
const posixEntry = "# msr-music-tool PATH\ncase \":${PATH-}:\" in\n  *\":$HOME/.local/bin:\"*) ;;\n  *) export PATH=\"${PATH:+$PATH:}$HOME/.local/bin\" ;;\nesac\n"
const fishEntry = "# msr-music-tool PATH\nif not contains -- \"$HOME/.local/bin\" $PATH\n    set -gx PATH $PATH \"$HOME/.local/bin\"\nend\n"

func shellProfiles() ([]string, string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, "", err
	}
	shell := filepath.Base(os.Getenv("SHELL"))
	switch shell {
	case "bash":
		login := filepath.Join(home, ".profile")
		for _, name := range []string{".bash_profile", ".bash_login"} {
			path := filepath.Join(home, name)
			if _, err := os.Stat(path); err == nil {
				login = path
				break
			}
		}
		return []string{filepath.Join(home, ".bashrc"), login}, posixEntry, nil
	case "zsh":
		if path := os.Getenv("ZDOTDIR"); path != "" {
			home = path
		}
		return []string{filepath.Join(home, ".zshrc"), filepath.Join(home, ".zprofile")}, posixEntry, nil
	case "fish":
		config, err := os.UserConfigDir()
		if err != nil {
			return nil, "", err
		}
		return []string{filepath.Join(config, "fish", "conf.d", "msr-music-tool.fish")}, fishEntry, nil
	case "sh", "dash", "ash", "ksh":
		return []string{filepath.Join(home, ".profile")}, posixEntry, nil
	default:
		return nil, "", fmt.Errorf("无法识别用户 shell %q", shell)
	}
}

func persistedPath(_ string) bool {
	paths, _, err := shellProfiles()
	if err != nil {
		return false
	}
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil || !strings.Contains(string(data), marker) {
			return false
		}
	}
	return true
}

func addPath(directory string) error {
	paths, entry, err := shellProfiles()
	if err != nil {
		if inPath(os.Getenv("PATH"), directory) {
			return nil
		}
		return err
	}
	for _, path := range paths {
		if err := addProfile(path, entry); err != nil {
			return err
		}
	}
	return nil
}

func addProfile(path, entry string) error {
	if real, err := filepath.EvalSymlinks(path); err == nil {
		path = real
	} else if info, statErr := os.Lstat(path); statErr == nil && info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("无法解析 %s：%w", path, err)
	}
	info, err := os.Stat(path)
	mode := os.FileMode(0600)
	if err == nil {
		if !info.Mode().IsRegular() {
			return fmt.Errorf("%s 不是普通文件", path)
		}
		mode = info.Mode().Perm()
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	data, err := os.ReadFile(path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if strings.Contains(string(data), marker) {
		return nil
	}
	return fsutil.Write(path, mode, func(w io.Writer) error {
		if _, err := w.Write(data); err != nil {
			return err
		}
		if len(data) > 0 {
			if _, err := io.WriteString(w, "\n"); err != nil {
				return err
			}
		}
		_, err := io.WriteString(w, entry)
		return err
	})
}
