package core

import (
	"fmt"
	"math"
	"strings"
	"sync"
)

type Tracker struct {
	mu        sync.Mutex
	resources map[string]Resource
	completed int
	latest    Resource
}

func (t *Tracker) Update(p Progress) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.completed = p.Completed
	if p.Resource.Key != "" {
		if t.resources == nil {
			t.resources = make(map[string]Resource)
		}
		t.resources[p.Resource.Key], t.latest = p.Resource, p.Resource
	}
}

type ProgressSnapshot struct {
	Done, Total int64
	Speed       float64
	Completed   int
	Latest      Resource
}

func (t *Tracker) Snapshot() ProgressSnapshot {
	t.mu.Lock()
	defer t.mu.Unlock()
	p := ProgressSnapshot{Completed: t.completed, Latest: t.latest}
	for _, r := range t.resources {
		p.Done += r.Downloaded
		p.Speed += r.Speed
		if r.Total < 0 {
			p.Total = -1
		} else if p.Total >= 0 {
			p.Total += r.Total
		}
	}
	if len(t.resources) == 0 {
		p.Total = -1
	}
	return p
}

func Bytes(value float64) string {
	if value < 0 {
		return "?"
	}
	units := []string{"B", "KiB", "MiB", "GiB", "TiB"}
	unit := 0
	for value >= 1024 && unit < len(units)-1 {
		value /= 1024
		unit++
	}
	return fmt.Sprintf("%.2f %s", value, units[unit])
}

func (p ProgressSnapshot) Text(count int) string {
	return fmt.Sprintf("歌曲 %d/%d  %s/%s  %s/s", p.Completed, count, Bytes(float64(p.Done)), Bytes(float64(p.Total)), Bytes(p.Speed))
}

func (p ProgressSnapshot) Bar(width, frame int, fill func(string) string) string {
	if width < 1 {
		return ""
	}
	if p.Total < 0 {
		position := frame % width
		return strings.Repeat("─", position) + fill("━") + strings.Repeat("─", width-position-1)
	}
	complete := 0
	if p.Total > 0 {
		complete = int(math.Round(float64(p.Done) / float64(p.Total) * float64(width)))
	}
	complete = max(0, min(width, complete))
	return fill(strings.Repeat("━", complete)) + strings.Repeat("─", width-complete)
}

func StatusLabel(status string) string {
	translated := map[string]string{"waiting": "等待", "active": "下载中", "retrying": "重试", "complete": "完成", "skipped": "跳过", "failed": "失败", "paused": "暂停", "transferred": "已传输"}
	if value, ok := translated[status]; ok {
		return value
	}
	return status
}
