package tui

import (
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"

	"charm.land/bubbles/v2/textinput"
	tea "charm.land/bubbletea/v2"
	"github.com/zoisythe/MSRMusicTool/internal/core"
	"github.com/zoisythe/MSRMusicTool/internal/install"
)

type field struct {
	label   string
	input   textinput.Model
	options []string
	value   int
}

func textField(label, value string, mask bool) field {
	input := textinput.New()
	input.SetValue(value)
	input.CharLimit = 2048
	if mask {
		input.EchoMode = textinput.EchoPassword
	}
	return field{label: label, input: input}
}

func choiceField(label string, values []string, current string) field {
	return field{label: label, options: values, value: max(0, slices.Index(values, current))}
}

const (
	dirField = iota
	backendField
	concurrencyField
	endpointField
	secretField
	pollField
	remoteField
	checkField
	themeField
	firstColorField
)

var colorLabels = []string{"背景", "正文", "次要文字", "强调", "选中", "成功", "警告", "错误"}
var themeIDs = []string{"classic", "dark", "light", "contrast"}
var themeLabels = []string{"经典深色", "深色", "浅色", "高对比"}

const installAction = -1

type form struct {
	fields          []field
	index           int
	changed         bool
	browseRequest   int
	directory       string
	directories     []string
	directoryCursor int
}

func newForm(c core.Config) *form {
	update := "开启"
	if !c.Update.CheckOnStartup {
		update = "关闭"
	}
	f := &form{fields: []field{
		textField("下载目录", c.Download.Directory, false),
		choiceField("下载后端", []string{"http", "aria2"}, c.Download.Backend),
		textField("并发数", strconv.Itoa(c.Download.Concurrency), false),
		textField("RPC 地址", c.RPC.Endpoint, false),
		textField("RPC 密钥", c.RPC.Secret, true),
		textField("RPC 轮询秒数", strconv.FormatFloat(c.RPC.PollInterval, 'f', -1, 64), false),
		textField("RPC 共享目录", c.RPC.Directory, false),
		choiceField("启动时检查更新", []string{"开启", "关闭"}, update),
		choiceField("主题预设", themeLabels, themeLabels[max(0, slices.Index(themeIDs, c.TUI.Theme))]),
	}}
	for i, key := range core.ColorKeys {
		f.fields = append(f.fields, textField(colorLabels[i]+"色", c.TUI.Colors[key], false))
		f.fields[len(f.fields)-1].input.Placeholder = "继承预设"
		f.fields[len(f.fields)-1].input.CharLimit = 7
	}
	f.index = f.firstIndex()
	return f
}

func (f *form) firstIndex() int {
	if install.Available() {
		return installAction
	}
	return 0
}

func (f *form) focus() tea.Cmd {
	for i := range f.fields {
		f.fields[i].input.Blur()
	}
	if f.index < 0 || f.index >= len(f.fields) || len(f.fields[f.index].options) > 0 {
		return nil
	}
	return f.fields[f.index].input.Focus()
}

func (f *form) step(change int) tea.Cmd {
	first := f.firstIndex()
	count := len(f.fields) + 1 - first
	f.index = first + (f.index-first+change+count)%count
	return f.focus()
}

func (f *form) colors(base core.TUIConfig, preview bool) core.TUIConfig {
	base.Theme = themeIDs[f.fields[themeField].value]
	base.Colors = maps.Clone(base.Colors)
	if base.Colors == nil {
		base.Colors = make(map[string]string)
	}
	for i, key := range core.ColorKeys {
		value := strings.TrimSpace(f.fields[firstColorField+i].input.Value())
		delete(base.Colors, key)
		if value != "" && (!preview || core.HexColor.MatchString(value)) {
			base.Colors[key] = value
		}
	}
	return base
}

func (f *form) config(base core.Config) (core.Config, error) {
	concurrency, err := strconv.Atoi(strings.TrimSpace(f.fields[concurrencyField].input.Value()))
	if err != nil {
		return base, fmt.Errorf("并发数必须为整数")
	}
	interval, err := strconv.ParseFloat(strings.TrimSpace(f.fields[pollField].input.Value()), 64)
	if err != nil {
		return base, fmt.Errorf("RPC 轮询间隔必须为数字")
	}
	base.Download = core.DownloadConfig{
		Directory:   f.fields[dirField].input.Value(),
		Backend:     f.fields[backendField].options[f.fields[backendField].value],
		Concurrency: concurrency,
	}
	base.RPC = core.RPCConfig{
		Endpoint:     strings.TrimSpace(f.fields[endpointField].input.Value()),
		Secret:       f.fields[secretField].input.Value(),
		PollInterval: interval,
		Directory:    strings.TrimSpace(f.fields[remoteField].input.Value()),
	}
	base.Update.CheckOnStartup = f.fields[checkField].value == 0
	base.TUI = f.colors(base.TUI, false)
	return base, nil
}

type settingsSavedMsg struct {
	config core.Settings
	err    error
}

func (m *model) saveSettings() tea.Cmd {
	config, err := m.form.config(m.config.Config)
	if err != nil {
		m.status = err.Error()
		return nil
	}
	current := m.config
	m.formBusy = true
	return func() tea.Msg { latest, err := current.Save(config); return settingsSavedMsg{latest, err} }
}

func (m *model) settingsKeys(msg tea.KeyPressMsg) tea.Cmd {
	f := m.form
	switch msg.String() {
	case "esc":
		m.form = nil
		m.status = "已关闭设置"
		return m.backToCatalog()
	case "ctrl+s":
		return m.saveSettings()
	case "tab", "down":
		return f.step(1)
	case "shift+tab", "up":
		return f.step(-1)
	case "f3":
		if f.changed {
			m.status = "请先保存或退出设置"
			return nil
		}
		if m.updateCandidate != nil {
			m.openPrompt(updatePage, settingsPage)
			return nil
		}
		return m.checkUpdate(true)
	case "f4":
		if install.Available() {
			m.openPrompt(installPage, settingsPage)
		}
		return nil
	case "f5":
		for i := firstColorField; i < len(f.fields); i++ {
			f.fields[i].input.SetValue("")
		}
		f.changed = true
		return nil
	case "f6":
		path := strings.TrimSpace(f.fields[dirField].input.Value())
		if path == "~" || strings.HasPrefix(path, "~/") || strings.HasPrefix(path, "~\\") {
			if home, err := os.UserHomeDir(); err == nil {
				path = filepath.Join(home, strings.TrimLeft(path[1:], "/\\"))
			}
		}
		if !filepath.IsAbs(path) {
			path = filepath.Join(filepath.Dir(m.config.Path), path)
		}
		for {
			info, err := os.Stat(path)
			if err == nil && info.IsDir() {
				break
			}
			parent := filepath.Dir(path)
			if parent == path {
				if cwd, err := os.Getwd(); err == nil {
					path = cwd
				}
				break
			}
			path = parent
		}
		m.view = browsePage
		return m.loadDirectory(path)
	case "enter":
		if f.index == installAction {
			m.openPrompt(installPage, settingsPage)
			return nil
		}
		if f.index == len(f.fields) {
			return m.saveSettings()
		}
		if len(f.fields[f.index].options) == 0 {
			return f.step(1)
		}
	}
	if f.index >= 0 && f.index < len(f.fields) {
		field := &f.fields[f.index]
		if len(field.options) > 0 {
			delta := 0
			switch msg.String() {
			case "left":
				delta = -1
			case "right", "enter", "space", " ":
				delta = 1
			}
			if delta != 0 {
				field.value = (field.value + len(field.options) + delta) % len(field.options)
				f.changed = true
			}
			return nil
		}
	}
	return m.editInput(msg)
}

func (m *model) editInput(msg tea.Msg) tea.Cmd {
	if m.form == nil || m.form.index < 0 || m.form.index >= len(m.form.fields) || m.formBusy {
		return nil
	}
	field := &m.form.fields[m.form.index]
	if len(field.options) > 0 {
		return nil
	}
	before := field.input.Value()
	input, cmd := field.input.Update(msg)
	field.input = input
	if before != input.Value() {
		m.form.changed = true
	}
	return cmd
}
