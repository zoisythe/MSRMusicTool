package core

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"math"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"

	"github.com/gofrs/flock"
	"github.com/pelletier/go-toml/v2"
	"github.com/zoisythe/MSRMusicTool/internal/fsutil"
)

const AppName = "msr-music-tool"
const legacyName = "MSRMusicTool"

type DownloadConfig struct {
	Directory   string `toml:"directory"`
	Backend     string `toml:"backend"`
	Concurrency int    `toml:"max_concurrent_downloads"`
}

type RPCConfig struct {
	Endpoint     string  `toml:"endpoint"`
	Secret       string  `toml:"secret"`
	PollInterval float64 `toml:"poll_interval"`
	Directory    string  `toml:"directory,omitempty"`
}

type TUIConfig struct {
	Theme  string            `toml:"theme"`
	Colors map[string]string `toml:"colors,omitempty"`
}

type UpdateConfig struct {
	CheckOnStartup bool `toml:"check_on_startup"`
}

type Config struct {
	Download DownloadConfig `toml:"download"`
	RPC      RPCConfig      `toml:"rpc"`
	TUI      TUIConfig      `toml:"tui"`
	Update   UpdateConfig   `toml:"update"`
}

type Settings struct {
	Config
	Path     string
	Output   string
	Override string
	raw      []byte
}

func (s Settings) Configured() bool { return s.raw != nil }

var ColorKeys = []string{"background", "text", "muted", "accent", "selection", "success", "warning", "error"}
var HexColor = regexp.MustCompile("^#[0-9a-fA-F]{6}$")
var windowsAbs = regexp.MustCompile("^(?:[A-Za-z]:[/\\\\]|[/\\\\]{2}[^/\\\\]+[/\\\\][^/\\\\]+)")

func ConfigPath() string {
	base, _ := os.UserConfigDir()
	if runtime.GOOS == "windows" {
		// platformdirs uses the non-roaming profile for this project.
		base, _ = os.UserCacheDir()
	}
	return filepath.Join(base, legacyName, "config.toml")
}

func CachePath() string {
	base, _ := os.UserCacheDir()
	base = filepath.Join(base, legacyName)
	if runtime.GOOS == "windows" {
		base = filepath.Join(base, "Cache")
	}
	return filepath.Join(base, "catalog-v1.json")
}

func defaults() Config {
	return Config{
		Download: DownloadConfig{Backend: "http", Concurrency: 4},
		RPC:      RPCConfig{Endpoint: "http://127.0.0.1:6800/jsonrpc", PollInterval: 0.5},
		TUI:      TUIConfig{Theme: "classic"},
		Update:   UpdateConfig{CheckOnStartup: true},
	}
}

func absolute(path, base string) (string, error) {
	if path == "~" || strings.HasPrefix(path, "~/") || (runtime.GOOS == "windows" && strings.HasPrefix(path, "~\\")) {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		path = filepath.Join(home, strings.TrimLeft(path[1:], "/\\"))
	}
	if !filepath.IsAbs(path) {
		path = filepath.Join(base, path)
	}
	return filepath.Abs(path)
}

func LoadSettings(path, override string) (Settings, error) {
	cwd, err := os.Getwd()
	if err != nil {
		return Settings{}, err
	}
	explicit := path != ""
	if !explicit {
		path = ConfigPath()
	}
	path, err = absolute(path, cwd)
	if err != nil {
		return Settings{}, err
	}
	if override != "" {
		override, err = absolute(override, cwd)
		if err != nil {
			return Settings{}, err
		}
	}
	s := Settings{Config: defaults(), Path: path, Override: override}
	s.raw, err = os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) && !explicit {
		s.Download.Directory = filepath.Join(cwd, "MonsterSirenRecording")
	} else if err != nil {
		return Settings{}, fmt.Errorf("%s: %w", path, err)
	} else if err := toml.Unmarshal(s.raw, &s.Config); err != nil {
		return Settings{}, fmt.Errorf("%s: %w", path, err)
	} else {
		// Explicit empty RPC directory is invalid. An omitted directory selects local paths.
		var raw map[string]any
		if err := toml.Unmarshal(s.raw, &raw); err != nil {
			return Settings{}, err
		}
		if rpc, ok := raw["rpc"].(map[string]any); ok && rpc["directory"] == "" {
			return Settings{}, fmt.Errorf("%s: rpc.directory 必须为引擎侧绝对路径", path)
		}
	}
	if s.TUI.Theme == "terminal" {
		s.TUI.Theme = "classic"
	}
	s.Output, err = s.Validate(s.Config)
	return s, err
}

func (s Settings) Validate(c Config) (string, error) {
	field := func(message string) (string, error) { return "", fmt.Errorf("%s: %s", s.Path, message) }
	if strings.TrimSpace(c.Download.Directory) == "" {
		return field("download.directory 必须是非空字符串")
	}
	if c.Download.Backend != "http" && c.Download.Backend != "aria2" {
		return field("download.backend 必须为 http 或 aria2")
	}
	if c.Download.Concurrency < 1 || c.Download.Concurrency > 32 {
		return field("download.max_concurrent_downloads 必须为 1–32 的整数")
	}
	endpoint, err := url.Parse(c.RPC.Endpoint)
	if err != nil || endpoint.Hostname() == "" || (endpoint.Scheme != "http" && endpoint.Scheme != "https") || endpoint.User != nil || endpoint.RawQuery != "" || endpoint.ForceQuery || strings.Contains(c.RPC.Endpoint, "#") || endpoint.Port() == "0" {
		return field("rpc.endpoint 必须为不含凭据的 HTTP(S) URL")
	}
	if port := endpoint.Port(); port != "" {
		var n int
		if _, err := fmt.Sscanf(port, "%d", &n); err != nil || n < 1 || n > 65535 {
			return field("rpc.endpoint 端口无效")
		}
	}
	if math.IsNaN(c.RPC.PollInterval) || c.RPC.PollInterval < 0.1 || c.RPC.PollInterval > 60 {
		return field("rpc.poll_interval 必须介于 0.1–60 秒")
	}
	if c.RPC.Directory != "" && !strings.HasPrefix(c.RPC.Directory, "/") && !windowsAbs.MatchString(c.RPC.Directory) {
		return field("rpc.directory 必须为引擎侧绝对路径")
	}
	switch c.TUI.Theme {
	case "classic", "dark", "light", "contrast":
	default:
		return field("tui.theme 预设无效")
	}
	for key, value := range c.TUI.Colors {
		for _, role := range ColorKeys {
			if key == role && !HexColor.MatchString(value) {
				return field("tui.colors." + key + " 必须为 #RRGGBB")
			}
		}
	}
	output, err := absolute(strings.TrimSpace(c.Download.Directory), filepath.Dir(s.Path))
	if err != nil {
		return "", err
	}
	if s.Override != "" {
		equal := s.Override == output || (runtime.GOOS == "windows" && strings.EqualFold(s.Override, output))
		if c.Download.Backend == "aria2" && c.RPC.Directory != "" && !equal {
			return field("使用 rpc.directory 映射时，--output 必须与 download.directory 一致")
		}
		output = s.Override
	}
	return output, nil
}

func (s Settings) Save(c Config) (Settings, error) {
	output, err := s.Validate(c)
	if err != nil {
		return s, err
	}
	if err := os.MkdirAll(filepath.Dir(s.Path), 0700); err != nil {
		return s, err
	}
	lock := flock.New(s.Path + ".lock")
	acquired, err := lock.TryLock()
	if err != nil {
		return s, err
	}
	if !acquired {
		return s, errors.New("配置文件正在写入")
	}
	defer lock.Close()
	current, err := os.ReadFile(s.Path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return s, err
	}
	if !bytes.Equal(current, s.raw) || (current == nil) != (s.raw == nil) {
		return s, errors.New("配置文件已被修改，请重启后重新编辑")
	}
	raw := map[string]any{}
	if current != nil {
		if err := toml.Unmarshal(current, &raw); err != nil {
			return s, err
		}
	}
	encoded, err := toml.Marshal(c)
	if err != nil {
		return s, err
	}
	update := map[string]any{}
	if err := toml.Unmarshal(encoded, &update); err != nil {
		return s, err
	}
	if rpc, ok := raw["rpc"].(map[string]any); ok {
		delete(rpc, "directory")
	}
	if tui, ok := raw["tui"].(map[string]any); ok {
		if colors, ok := tui["colors"].(map[string]any); ok {
			for _, role := range ColorKeys {
				delete(colors, role)
			}
		}
	}
	mergeTables(raw, update)
	encoded, err = toml.Marshal(raw)
	if err != nil {
		return s, err
	}
	if err := fsutil.Write(s.Path, 0600, func(w io.Writer) error { _, err := w.Write(encoded); return err }); err != nil {
		return s, err
	}
	s.Config, s.Output, s.raw = c, output, encoded
	return s, nil
}

func mergeTables(dst, src map[string]any) {
	for key, item := range src {
		target, have := dst[key].(map[string]any)
		update, table := item.(map[string]any)
		if have && table {
			mergeTables(target, update)
		} else {
			dst[key] = item
		}
	}
}
