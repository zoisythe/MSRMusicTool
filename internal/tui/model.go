package tui

import (
	"context"
	"errors"
	"io"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"charm.land/bubbles/v2/textinput"
	tea "charm.land/bubbletea/v2"
	"github.com/zoisythe/MSRMusicTool/internal/core"
	"github.com/zoisythe/MSRMusicTool/internal/install"
	"github.com/zoisythe/MSRMusicTool/internal/update"
)

type page int

const (
	catalogPage page = iota
	detailPage
	confirmPage
	downloadPage
	resultPage
	settingsPage
	browsePage
	installPage
	overwritePage
	updatePage
	updatingPage
)

var modes = []string{core.ModeAll, core.ModeAudio, core.ModeAudioLyrics}

type Outcome struct {
	Interrupted bool
	Unresolved  []string
	Staged      *update.Staged
}

type catalogMsg struct {
	snapshot core.Snapshot
	note     string
	request  int
	err      error
}
type detailMsg struct {
	detail  core.AlbumDetail
	request int
	err     error
}
type prepareMsg struct {
	prepared core.Prepared
	request  int
}
type searchMsg struct {
	query    string
	sequence int
}
type resultMsg core.BatchResult
type updateMsg struct {
	candidate *update.Candidate
	err       error
	manual    bool
}
type stagedMsg struct {
	staged update.Staged
	err    error
}
type installMsg struct {
	target string
	err    error
}
type installOfferMsg bool
type tickMsg struct{}
type interruptedMsg struct{}
type directoryMsg struct {
	path    string
	entries []string
	request int
	err     error
}

type model struct {
	ctx, signals  context.Context
	site          *core.Site
	config        core.Settings
	version       string
	view          page
	width, height int
	status        string

	catalog       core.Catalog
	snapshotAt    time.Time
	catalogState  string
	refreshID     int
	refreshCancel context.CancelFunc
	deferred      *catalogMsg
	selected      map[string]bool
	rows          []core.Match
	cursor        int
	details       map[string]core.AlbumDetail
	detail        core.AlbumDetail
	detailCursor  int
	pending       string
	request       int
	cancelRequest context.CancelFunc

	search       textinput.Model
	searchOpen   bool
	query        string
	searchSeq    int
	prepared     core.Prepared
	mode, button int
	lastMode     int
	lastConfirm  int
	modeCursor   int
	modeOpen     bool
	flowOffset   int
	tracker      *core.Tracker
	frame        int
	batch        core.BatchResult
	cancelWork   context.CancelFunc
	quitting     bool
	outcome      Outcome

	form            *form
	afterPrompt     page
	formBusy        bool
	offerInstall    bool
	updateCandidate *update.Candidate
	checkingUpdate  bool
	updateProgress  *core.Tracker
}

func Run(signals context.Context, in io.Reader, out io.Writer, site *core.Site, config core.Settings, version string) (Outcome, error) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	m := newModel(ctx, signals, site, config, version)
	p := tea.NewProgram(m, tea.WithInput(in), tea.WithOutput(out), tea.WithoutSignalHandler())
	_, err := p.Run()
	if m.cancelWork != nil {
		m.cancelWork()
	}
	return m.outcome, err
}

func newModel(ctx, signals context.Context, site *core.Site, config core.Settings, version string) *model {
	m := &model{ctx: ctx, signals: signals, site: site, config: config, version: version, width: 80, height: 24,
		selected: make(map[string]bool), details: make(map[string]core.AlbumDetail), search: textinput.New(), catalogState: "加载曲库中…"}
	m.search.Placeholder = "专辑名或歌曲名"
	m.search.CharLimit = 200
	if snapshot, err := core.LoadCatalog(core.CachePath()); err == nil {
		m.catalog, m.snapshotAt = snapshot.Catalog, snapshot.FetchedAt
		m.catalogState = "已加载缓存，正在刷新…"
		m.rebuild()
	}
	return m
}

func (m *model) Init() tea.Cmd {
	return tea.Batch(m.refresh(), func() tea.Msg {
		select {
		case <-m.signals.Done():
			return interruptedMsg{}
		case <-m.ctx.Done():
			return nil
		}
	}, func() tea.Msg { return installOfferMsg(install.ShouldOffer()) }, m.checkUpdate(false))
}

func (m *model) refresh() tea.Cmd {
	if m.refreshCancel != nil {
		m.refreshCancel()
	}
	ctx, cancel := context.WithCancel(m.ctx)
	m.refreshCancel = cancel
	m.refreshID++
	id := m.refreshID
	m.catalogState = "刷新曲库中…"
	site := m.site
	return func() tea.Msg {
		defer cancel()
		catalog, err := site.Catalog(ctx)
		result := catalogMsg{request: id, err: err}
		if err == nil {
			result.snapshot = core.Snapshot{Schema: 1, FetchedAt: time.Now().UTC(), Catalog: catalog}
		}
		return result
	}
}

func (m *model) rebuild() {
	current := ""
	if m.cursor < len(m.rows) {
		current = m.rows[m.cursor].Album.CID
	}
	m.rows = m.catalog.MatchAlbums(m.query, m.selected)
	m.cursor = min(m.cursor, max(0, len(m.rows)-1))
	if i := slices.IndexFunc(m.rows, func(r core.Match) bool { return r.Album.CID == current }); i >= 0 {
		m.cursor = i
	}
}

func (m *model) applyCatalog(msg catalogMsg) tea.Cmd {
	m.deferred = nil
	m.catalog = msg.snapshot.Catalog
	m.snapshotAt = msg.snapshot.FetchedAt
	m.catalogState = "曲库已更新"
	m.details = make(map[string]core.AlbumDetail)
	for cid := range m.selected {
		if _, found := m.catalog.SongByID[cid]; !found {
			delete(m.selected, cid)
		}
	}
	m.rebuild()
	snapshot := msg.snapshot
	return func() tea.Msg {
		if err := core.SaveCatalog(core.CachePath(), snapshot); err != nil {
			return catalogMsg{request: msg.request, note: "曲库缓存保存失败：" + err.Error()}
		}
		return nil
	}
}

func (m *model) backToCatalog() tea.Cmd {
	m.view = catalogPage
	var command tea.Cmd
	if m.deferred != nil {
		command = m.applyCatalog(*m.deferred)
	}
	if m.offerInstall && m.pending == "" && !m.searchOpen {
		m.offerInstall = false
		m.openPrompt(installPage, catalogPage)
	}
	return command
}

func (m *model) checkUpdate(manual bool) tea.Cmd {
	if m.checkingUpdate || !manual && !m.config.Update.CheckOnStartup {
		return nil
	}
	m.checkingUpdate = true
	ctx, version := m.ctx, m.version
	return func() tea.Msg {
		candidate, err := update.Check(ctx, version)
		return updateMsg{candidate, err, manual}
	}
}

func (m *model) startUpdate() tea.Cmd {
	if m.updateCandidate == nil {
		return nil
	}
	ctx, cancel := context.WithCancel(m.ctx)
	m.cancelWork = cancel
	m.view = updatingPage
	m.updateProgress = &core.Tracker{}
	candidate, tracker := *m.updateCandidate, m.updateProgress
	return tea.Batch(tick(), func() tea.Msg {
		defer cancel()
		staged, err := update.Download(ctx, candidate, func(done, total int64) {
			tracker.Update(core.Progress{Resource: core.Resource{Key: "update", Downloaded: done, Total: total}})
		})
		return stagedMsg{staged, err}
	})
}

func (m *model) requestContext(label string) (context.Context, int) {
	m.request++
	m.pending = label
	var ctx context.Context
	ctx, m.cancelRequest = context.WithCancel(m.ctx)
	return ctx, m.request
}

func (m *model) finishRequest() {
	if m.cancelRequest != nil {
		m.cancelRequest()
	}
	m.cancelRequest = nil
	m.request++
	m.pending = ""
}

func (m *model) openDetail() tea.Cmd {
	if len(m.rows) == 0 {
		return nil
	}
	cid := m.rows[m.cursor].Album.CID
	if detail, ok := m.details[cid]; ok {
		m.view, m.detail, m.detailCursor = detailPage, detail, 0
		return nil
	}
	ctx, id := m.requestContext("正在加载专辑详情")
	site := m.site
	return func() tea.Msg {
		detail, err := site.Album(ctx, cid)
		return detailMsg{detail, id, err}
	}
}

func (m *model) prepare() tea.Cmd {
	if len(m.selected) == 0 {
		m.status = "请先选择歌曲"
		return nil
	}
	var ids []string
	for _, song := range m.catalog.Songs {
		if m.selected[song.CID] {
			ids = append(ids, song.CID)
		}
	}
	ctx, id := m.requestContext("正在准备下载")
	site, catalog, limit, cached := m.site, m.catalog, m.config.Download.Concurrency, maps.Clone(m.details)
	return func() tea.Msg { return prepareMsg{site.Prepare(ctx, catalog, ids, limit, cached), id} }
}

func (m *model) startDownload() tea.Cmd {
	ctx, cancel := context.WithCancel(m.ctx)
	m.cancelWork = cancel
	m.view = downloadPage
	m.lastMode = m.mode
	m.tracker = &core.Tracker{}
	m.tracker.Update(core.Progress{Completed: len(m.prepared.Failures)})
	m.frame = 0
	site, config, catalog, prepared, mode, tracker := m.site, m.config, m.catalog, m.prepared, modes[m.mode], m.tracker
	return tea.Batch(tick(), func() tea.Msg {
		defer cancel()
		return resultMsg(site.Download(ctx, config, catalog, prepared, mode, tracker.Update))
	})
}

func tick() tea.Cmd {
	return tea.Tick(200*time.Millisecond, func(time.Time) tea.Msg { return tickMsg{} })
}

func (m *model) interrupt() tea.Cmd {
	m.quitting = true
	m.outcome.Interrupted = true
	m.finishRequest()
	if m.cancelWork != nil {
		m.cancelWork()
		m.status = "正在取消并清理…"
		return nil
	}
	if m.formBusy {
		return nil
	}
	return tea.Quit
}

func (m *model) openPrompt(next, back page) { m.view, m.afterPrompt, m.button = next, back, 0 }

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	return m, m.handle(msg)
}

func (m *model) handle(msg tea.Msg) tea.Cmd {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = max(1, msg.Width), max(1, msg.Height)
		return nil
	case interruptedMsg:
		return m.interrupt()
	case installOfferMsg:
		m.offerInstall = bool(msg)
		if m.offerInstall && m.view == catalogPage && m.pending == "" && !m.searchOpen {
			return m.backToCatalog()
		}
	case catalogMsg:
		if m.refreshID != msg.request {
			return nil
		}
		if msg.note != "" {
			m.status = msg.note
			return nil
		}
		if msg.err != nil {
			if m.catalog.AlbumByID != nil {
				m.catalogState = "离线 · 使用缓存"
			} else {
				m.catalogState = "无法加载曲库 · 按 r 重试"
			}
			m.status = msg.err.Error()
		} else if m.view != catalogPage || m.pending != "" {
			m.deferred = &msg
			m.catalogState = "有新曲库 · 返回列表后应用"
		} else {
			return m.applyCatalog(msg)
		}
	case detailMsg:
		if m.request != msg.request {
			return nil
		}
		m.finishRequest()
		if msg.err != nil {
			m.status = "专辑详情加载失败：" + msg.err.Error()
			return m.backToCatalog()
		}
		m.details[msg.detail.CID] = msg.detail
		m.view, m.detail, m.detailCursor = detailPage, msg.detail, 0
	case prepareMsg:
		if m.request != msg.request {
			return nil
		}
		m.finishRequest()
		m.prepared = msg.prepared
		m.view, m.button, m.mode, m.modeOpen = confirmPage, 2, m.lastMode, false
		m.lastConfirm = 2
		m.flowOffset = 0
	case searchMsg:
		if msg.sequence == m.searchSeq && m.searchOpen {
			m.query = msg.query
			m.rebuild()
		}
	case resultMsg:
		m.cancelWork = nil
		m.batch = core.BatchResult(msg)
		m.outcome.Unresolved = append(m.outcome.Unresolved, m.batch.Unresolved...)
		for _, song := range m.batch.Songs {
			if song.Outcome != "failed" {
				delete(m.selected, song.Song.CID)
			}
		}
		m.rebuild()
		if m.quitting {
			return tea.Quit
		}
		m.view, m.button = resultPage, 0
		m.flowOffset = 0
	case updateMsg:
		m.checkingUpdate = false
		if msg.err != nil {
			if msg.manual {
				m.status = "更新检查失败：" + msg.err.Error()
			}
		} else {
			m.updateCandidate = msg.candidate
			if msg.candidate == nil && msg.manual {
				m.status = "未发现可用更新"
			}
		}
	case stagedMsg:
		m.cancelWork = nil
		if msg.err == nil && m.quitting {
			os.Remove(msg.staged.Path)
		}
		if m.quitting {
			return tea.Quit
		}
		if msg.err != nil {
			m.status = "更新未应用：" + msg.err.Error()
			m.view = m.afterPrompt
		} else {
			m.outcome.Staged = &msg.staged
			return tea.Quit
		}
	case settingsSavedMsg:
		m.formBusy = false
		if m.quitting {
			return tea.Quit
		}
		if msg.err != nil {
			m.status = msg.err.Error()
			return nil
		}
		m.config = msg.config
		m.form.changed = false
		m.status = "设置已保存"
	case installMsg:
		m.formBusy = false
		if m.quitting {
			return tea.Quit
		}
		if errors.Is(msg.err, install.ErrConflict) {
			m.view, m.button = overwritePage, 0
		} else {
			if msg.err != nil {
				m.status = msg.err.Error()
			} else {
				m.status = "安装完成：" + msg.target + " · 请重新打开终端"
			}
			return m.closePrompt()
		}
	case directoryMsg:
		if m.view == browsePage && m.form != nil && msg.request == m.form.browseRequest {
			if msg.err != nil {
				m.status = msg.err.Error()
			} else {
				m.form.directory, m.form.directories, m.form.directoryCursor = msg.path, msg.entries, 0
			}
		}
	case tickMsg:
		if m.cancelWork != nil {
			m.frame++
			return tick()
		}
	case clickMsg:
		if !m.quitting && !m.formBusy && m.view == msg.page && (m.view != confirmPage || m.modeOpen == msg.modeOpen) {
			return msg.action(m)
		}
	case tea.KeyPressMsg:
		if msg.String() == "ctrl+c" {
			return m.interrupt()
		}
		return m.keys(msg)
	case tea.PasteMsg:
		if m.view == catalogPage && m.searchOpen {
			return m.searchInput(msg)
		}
		if m.view == settingsPage {
			return m.editInput(msg)
		}
	}
	return nil
}

func (m *model) keys(key tea.KeyPressMsg) tea.Cmd {
	name := key.String()
	if m.quitting || m.formBusy {
		return nil
	}
	switch m.view {
	case catalogPage:
		return m.catalogKeys(key)
	case detailPage:
		switch name {
		case "esc", "left":
			return m.backToCatalog()
		case "up", "down", "pgup", "pgdown", "home", "end":
			m.detailCursor = move(m.detailCursor, len(m.detail.Songs), m.visibleLibraryRows(), name)
		case "space", " ":
			if m.detailCursor < len(m.detail.Songs) {
				cid := m.detail.Songs[m.detailCursor].CID
				if m.selected[cid] {
					delete(m.selected, cid)
				} else if _, ok := m.catalog.SongByID[cid]; ok {
					m.selected[cid] = true
				}
				m.rebuild()
			}
		}
	case confirmPage:
		return m.confirmKeys(name)
	case resultPage:
		switch name {
		case "pgup", "pgdown", "home", "end":
			m.scrollDownloadPanel(name)
		case "q":
			return tea.Quit
		case "esc":
			return m.backToCatalog()
		case "up", "down", "left", "right", "tab", "shift+tab":
			m.button = 1 - m.button
		case "enter":
			if m.button == 1 {
				return tea.Quit
			}
			return m.backToCatalog()
		}
	case settingsPage:
		return m.settingsKeys(key)
	case browsePage:
		return m.browseKeys(name)
	case installPage, overwritePage, updatePage:
		return m.promptKeys(name)
	case updatingPage:
		if name == "esc" && m.cancelWork != nil {
			m.cancelWork()
		}
	}
	return nil
}

func (m *model) catalogKeys(key tea.KeyPressMsg) tea.Cmd {
	name := key.String()
	if name == "q" && !m.searchOpen {
		return tea.Quit
	}
	if m.pending != "" {
		if name == "esc" {
			m.finishRequest()
			m.status = "已取消"
			return m.backToCatalog()
		}
		return nil
	}
	if m.searchOpen {
		switch name {
		case "esc":
			m.searchOpen = false
			m.search.Blur()
			m.search.SetValue("")
			m.query = ""
			m.searchSeq++
			m.rebuild()
			return m.backToCatalog()
		case "enter":
			m.searchOpen = false
			m.search.Blur()
			m.query = m.search.Value()
			m.rebuild()
			return m.backToCatalog()
		case "up", "down", "pgup", "pgdown":
			m.cursor = move(m.cursor, len(m.rows), m.visibleLibraryRows(), name)
		default:
			return m.searchInput(key)
		}
		return nil
	}
	switch name {
	case "esc":
		if m.query != "" {
			m.query = ""
			m.search.SetValue("")
			m.rebuild()
			return nil
		}
		return tea.Quit
	case "/":
		m.searchOpen = true
		m.search.SetWidth(max(8, m.width-12))
		return m.search.Focus()
	case "up", "down", "pgup", "pgdown", "home", "end":
		m.cursor = move(m.cursor, len(m.rows), m.visibleLibraryRows(), name)
	case "space", " ":
		if m.cursor < len(m.rows) {
			row := m.rows[m.cursor]
			for _, song := range m.catalog.ByAlbum[row.Album.CID] {
				if row.State() == 2 {
					delete(m.selected, song.CID)
				} else {
					m.selected[song.CID] = true
				}
			}
			m.rebuild()
		}
	case "right":
		return m.openDetail()
	case "enter":
		return m.prepare()
	case "r":
		return m.refresh()
	case "f2":
		m.form = newForm(m.config.Config)
		m.view = settingsPage
		return m.form.focus()
	case "u":
		if m.updateCandidate != nil {
			m.openPrompt(updatePage, catalogPage)
		}
	}
	return nil
}

func (m *model) searchInput(msg tea.Msg) tea.Cmd {
	old := m.search.Value()
	input, cmd := m.search.Update(msg)
	m.search = input
	if input.Value() == old {
		return cmd
	}
	m.searchSeq++
	id, query := m.searchSeq, input.Value()
	return tea.Batch(cmd, tea.Tick(60*time.Millisecond, func(time.Time) tea.Msg { return searchMsg{query, id} }))
}

func move(index, count, visible int, key string) int {
	switch key {
	case "up":
		index--
	case "down":
		index++
	case "pgup":
		index -= visible
	case "pgdown":
		index += visible
	case "home":
		index = 0
	case "end":
		index = count - 1
	}
	return max(0, min(index, count-1))
}

func (m *model) confirmKeys(name string) tea.Cmd {
	if name == "esc" {
		if m.modeOpen {
			m.modeOpen = false
			return nil
		}
		return m.backToCatalog()
	}
	if m.modeOpen {
		switch name {
		case "up":
			m.modeCursor = (m.modeCursor + len(modes) - 1) % len(modes)
		case "down":
			m.modeCursor = (m.modeCursor + 1) % len(modes)
		case "enter":
			m.mode = m.modeCursor
			m.modeOpen = false
		}
		return nil
	}
	switch name {
	case "pgup", "pgdown", "home", "end":
		m.scrollDownloadPanel(name)
	case "tab":
		m.focusConfirmation((m.button + 1) % 3)
	case "shift+tab":
		m.focusConfirmation((m.button + 2) % 3)
	case "up":
		m.focusConfirmation(0)
	case "down":
		if m.button == 0 {
			m.focusConfirmation(max(1, m.lastConfirm))
		}
	case "left":
		m.focusConfirmation(1)
	case "right":
		m.focusConfirmation(2)
	case "space", " ":
		if m.button == 0 {
			m.openDownloadMode()
		}
	case "enter":
		switch m.button {
		case 0:
			m.openDownloadMode()
		case 1:
			return m.backToCatalog()
		case 2:
			return m.startDownload()
		}
	}
	return nil
}

func (m *model) focusConfirmation(index int) {
	m.button = index
	if index != 0 {
		m.lastConfirm = index
	}
}

func (m *model) openDownloadMode() {
	m.modeCursor = m.mode
	m.modeOpen = true
}

func (m *model) closePrompt() tea.Cmd {
	if m.afterPrompt == settingsPage && m.form != nil {
		m.view = settingsPage
		return m.form.focus()
	}
	return m.backToCatalog()
}

func (m *model) promptKeys(name string) tea.Cmd {
	switch name {
	case "tab", "shift+tab", "up", "down":
		m.button = 1 - m.button
	case "left":
		m.button = 0
	case "right":
		m.button = 1
	case "esc":
		if m.view == installPage {
			return m.answerInstall(false, false)
		}
		return m.closePrompt()
	case "enter":
		switch m.view {
		case installPage:
			return m.answerInstall(m.button == 1, false)
		case overwritePage:
			if m.button == 1 {
				return m.answerInstall(true, true)
			}
			return m.closePrompt()
		case updatePage:
			if m.button == 1 {
				return m.startUpdate()
			}
			return m.closePrompt()
		}
	}
	return nil
}

func (m *model) answerInstall(accept, overwrite bool) tea.Cmd {
	m.formBusy = true
	return func() tea.Msg {
		if !accept {
			if err := install.Remember(); err != nil {
				return installMsg{"", err}
			}
			return installMsg{"", errors.New("已选择便携运行")}
		}
		target, err := install.Run(overwrite)
		return installMsg{target, err}
	}
}

func (m *model) listSize() int { return max(1, m.height-8) }

func (m *model) loadDirectory(path string) tea.Cmd {
	m.form.browseRequest++
	id := m.form.browseRequest
	return func() tea.Msg {
		entries, err := os.ReadDir(path)
		if err != nil {
			return directoryMsg{request: id, err: err}
		}
		var dirs []string
		for _, entry := range entries {
			if entry.IsDir() {
				dirs = append(dirs, entry.Name())
			} else if entry.Type()&os.ModeSymlink != 0 {
				if info, err := os.Stat(filepath.Join(path, entry.Name())); err == nil && info.IsDir() {
					dirs = append(dirs, entry.Name())
				}
			}
		}
		return directoryMsg{path: path, entries: dirs, request: id}
	}
}

func (m *model) browseKeys(name string) tea.Cmd {
	f := m.form
	switch name {
	case "esc":
		m.view = settingsPage
		return f.focus()
	case "enter":
		if f.directory == "" {
			return nil
		}
		f.fields[0].input.SetValue(f.directory)
		f.changed = true
		m.view = settingsPage
		return f.focus()
	case "left", "backspace":
		if f.directory != "" {
			return m.loadDirectory(filepath.Dir(f.directory))
		}
	case "right":
		if f.directoryCursor < len(f.directories) {
			return m.loadDirectory(filepath.Join(f.directory, f.directories[f.directoryCursor]))
		}
	case "up", "down", "pgup", "pgdown", "home", "end":
		f.directoryCursor = move(f.directoryCursor, len(f.directories), m.listSize(), name)
	}
	return nil
}

func safe(s string) string {
	// Titles and errors can come from the network. Keep control characters out of the terminal.
	return strings.Map(func(r rune) rune {
		if r < 32 || r >= 127 && r <= 159 {
			return ' '
		}
		return r
	}, s)
}
