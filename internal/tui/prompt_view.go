package tui

import (
	"strings"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
	"github.com/zoisythe/MSRMusicTool/internal/install"
)

func (m *model) promptView(p palette, mouse *mouseLayout) tea.View {
	title, question, confirm := "安装到用户目录", "将程序复制到以下位置并加入用户 PATH？", "安装"
	var details []string
	switch m.view {
	case overwritePage:
		title, question, confirm = "覆盖已有程序", "安装目录中已有不同的程序。是否覆盖？", "覆盖"
	case updatePage:
		title, question, confirm = "更新可用", "下载并替换当前程序？", "下载并替换"
		if m.updateCandidate != nil {
			details = append(details, "可更新到 "+m.updateCandidate.Tag, m.updateCandidate.URL)
		}
	}
	if m.view == installPage || m.view == overwritePage {
		target, err := install.Target()
		if err != nil {
			details = append(details, err.Error())
		} else {
			details = append(details, target)
		}
	}
	content := []string{p.style("text", true).Render(question)}
	if len(details) > 0 {
		// A path may occupy several terminal rows, especially during startup resize.
		width := max(1, m.width-8)
		for _, detail := range details {
			for _, line := range strings.Split(ansi.Wrap(safe(detail), width, ""), "\n") {
				content = append(content, p.style("muted", false).Render(line))
			}
		}
	}
	if m.status != "" {
		content = append(content, "", p.style("muted", false).Render(safe(m.status)))
	}
	help := []shortcut{{"left", "← 左"}, {"right", "→ 右"}, {"enter", "Enter 确定"}, {"esc", "Esc 取消"}, {"tab", "Tab 切换"}}
	return m.panelWithActions(p, m.flowHeader(p, title), content, 0, [2]string{"取消", confirm}, help, mouse)
}
