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
	order     []string
	logs      []string
	completed int
}

func (t *Tracker) Update(p Progress) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.completed = p.Completed
	if p.Resource.Key != "" {
		if t.resources == nil {
			t.resources = make(map[string]Resource)
		}
		r := p.Resource
		previous, exists := t.resources[r.Key]
		if !exists {
			t.order = append(t.order, r.Key)
		}
		if (r.finished() || r.Status == "retrying") && (!exists || previous.Status != r.Status || previous.Error != r.Error) {
			log := r.Label + "：" + StatusLabel(r.Status)
			if r.Error != "" {
				log += " · " + r.Error
			}
			t.logs = append(t.logs, log)
		}
		t.resources[r.Key] = r
	}
	if p.Song != nil {
		t.logs = append(t.logs, p.Song.Song.Name+"："+p.Song.StatusLabel())
	}
}

func (s SongResult) StatusLabel() string {
	switch s.Outcome {
	case "success":
		return "成功"
	case "skipped":
		return "已存在，跳过"
	default:
		return "失败"
	}
}

func (r Resource) finished() bool {
	switch r.Status {
	case "complete", "skipped", "failed", "error", "removed":
		return true
	}
	return false
}

type ProgressSnapshot struct {
	Done, Total int64
	Speed       float64
	Completed   int
}

func (t *Tracker) Snapshot() ProgressSnapshot {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.snapshot()
}

// Activity captures the summary, queued or running resources, and log in one read.
func (t *Tracker) Activity(logLimit int) (ProgressSnapshot, []Resource, []string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	var pending []Resource
	for _, key := range t.order {
		if r := t.resources[key]; !r.finished() {
			pending = append(pending, r)
		}
	}
	recent := t.logs[max(0, len(t.logs)-logLimit):]
	return t.snapshot(), pending, append([]string(nil), recent...)
}

func (t *Tracker) snapshot() ProgressSnapshot {
	p := ProgressSnapshot{Completed: t.completed}
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
