package core

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"
)

type rpcBackend struct {
	config RPCConfig
	client *http.Client
	mu     sync.Mutex
	active map[string]string
	broken bool
}

type rpcFault struct {
	Code    int
	Message string
}

func (f *rpcFault) Error() string { return f.Message }

func newRPC(config RPCConfig) *rpcBackend {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &rpcBackend{config: config, active: make(map[string]string), client: &http.Client{
		Transport: transport, Timeout: 10 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

func (rpc *rpcBackend) redact(text string) string {
	if rpc.config.Secret != "" {
		return strings.ReplaceAll(text, rpc.config.Secret, "[REDACTED]")
	}
	return text
}

func (rpc *rpcBackend) call(ctx context.Context, method string, result any, inputs ...any) error {
	params := make([]any, 0, len(inputs)+1)
	if rpc.config.Secret != "" {
		params = append(params, "token:"+rpc.config.Secret)
	}
	params = append(params, inputs...)
	id := randomID()
	body, err := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params})
	if err != nil {
		return &rpcFault{-1, err.Error()}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, rpc.config.Endpoint, bytes.NewReader(body))
	if err != nil {
		return errors.New(rpc.redact(err.Error()))
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := rpc.client.Do(req)
	if err != nil {
		return fmt.Errorf("RPC 通信失败：%s", rpc.redact(err.Error()))
	}
	defer resp.Body.Close()
	if err := responseStatus(resp); err != nil {
		return err
	}
	var reply struct {
		ID     string
		Error  *rpcFault
		Result json.RawMessage
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&reply); err != nil {
		return fmt.Errorf("RPC 通信失败：%s", rpc.redact(err.Error()))
	}
	if reply.ID != id {
		// A mismatched ID does not establish whether a mutation was accepted.
		return errors.New("RPC 响应 ID 不匹配")
	}
	if reply.Error != nil {
		reply.Error.Message = rpc.redact(reply.Error.Message)
		return reply.Error
	}
	if len(reply.Result) == 0 || json.Unmarshal(reply.Result, result) != nil {
		return errors.New("RPC 响应缺少有效的结果")
	}
	return nil
}

func (rpc *rpcBackend) read(ctx context.Context, method string, result any, retry func(int), params ...any) error {
	for attempt := range 4 {
		err := rpc.call(ctx, method, result, params...)
		var fault *rpcFault
		if err == nil || attempt == 3 || errors.As(err, &fault) {
			return err
		}
		if retry != nil {
			retry(attempt + 1)
		}
		if err := wait(ctx, retryDelay(err, attempt)); err != nil {
			return err
		}
	}
	return errors.New("RPC 重试耗尽")
}

func (rpc *rpcBackend) prepare(ctx context.Context) error {
	var version struct{ Version string }
	if err := rpc.read(ctx, "aria2.getVersion", &version, nil); err != nil {
		return err
	}
	if version.Version == "" {
		return errors.New("引擎未返回有效版本信息")
	}
	return nil
}

type rpcStatus struct {
	GID          string
	Status       string
	Total        string `json:"totalLength"`
	Completed    string `json:"completedLength"`
	Speed        string `json:"downloadSpeed"`
	ErrorCode    string
	ErrorMessage string
}

var rpcFields = []string{"gid", "status", "totalLength", "completedLength", "downloadSpeed", "errorCode", "errorMessage"}

func (rpc *rpcBackend) forget(gid string) {
	rpc.mu.Lock()
	defer rpc.mu.Unlock()
	delete(rpc.active, gid)
}

func (rpc *rpcBackend) transfer(ctx context.Context, address, temp string, emit func(string, int64, int64, float64, string)) (int64, error) {
	gid := randomID()[:16]
	rpc.mu.Lock()
	if rpc.broken || ctx.Err() != nil {
		rpc.mu.Unlock()
		if ctx.Err() != nil {
			return -1, ctx.Err()
		}
		return -1, errors.New("RPC 连接状态不确定，已停止提交后续任务")
	}
	rpc.active[gid] = temp
	rpc.mu.Unlock()
	defer func() {
		rpc.mu.Lock()
		if _, unresolved := rpc.active[gid]; unresolved {
			rpc.broken = true
		}
		rpc.mu.Unlock()
	}()
	dir := rpc.config.Directory
	if dir == "" {
		dir = filepath.Dir(temp)
	}
	options := map[string]string{"gid": gid, "dir": dir, "out": filepath.Base(temp), "auto-file-renaming": "false", "allow-overwrite": "true"}
	retry := func(int) { emit("retrying", 0, -1, 0, "RPC 连接中断，正在重新查询原任务") }
	var submitted string
	err := rpc.call(ctx, "aria2.addUri", &submitted, []string{address}, options)
	if err == nil && submitted != gid {
		return -1, errors.New("引擎未采用预分配 GID")
	}
	var fault *rpcFault
	if errors.As(err, &fault) {
		rpc.forget(gid)
		return -1, err
	}
	// A failed response may follow a successful addUri. Poll the owned GID, never resubmit.
	for {
		var status rpcStatus
		if err := rpc.read(ctx, "aria2.tellStatus", &status, retry, gid, rpcFields); err != nil {
			return -1, err
		}
		if status.GID != gid {
			return -1, errors.New("引擎返回了不匹配的 GID")
		}
		total, _ := strconv.ParseInt(status.Total, 10, 64)
		done, _ := strconv.ParseInt(status.Completed, 10, 64)
		speed, _ := strconv.ParseFloat(status.Speed, 64)
		if total <= 0 {
			total = -1
		}
		switch status.Status {
		case "complete":
			rpc.forget(gid)
			info, err := os.Stat(temp)
			if err != nil {
				return -1, fmt.Errorf("无法读取引擎文件，请检查共享目录映射：%w", err)
			}
			if info.Size() != done || (total >= 0 && info.Size() != total) {
				return -1, errors.New("引擎文件长度不符，请检查 rpc.directory 共享目录映射")
			}
			emit("transferred", done, total, speed, "")
			return done, nil
		case "error", "removed":
			rpc.forget(gid)
			return -1, errors.New(rpc.redact(fmt.Sprintf("引擎下载失败 %s: %s", status.ErrorCode, status.ErrorMessage)))
		case "active", "waiting", "paused":
			emit(status.Status, done, total, speed, "")
		default:
			return -1, errors.New("未知的引擎任务状态")
		}
		if err := wait(ctx, time.Duration(rpc.config.PollInterval*float64(time.Second))); err != nil {
			return -1, err
		}
	}
}

func (rpc *rpcBackend) cleanup() (unresolved []string, unsafe map[string]bool) {
	defer rpc.client.CloseIdleConnections()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var workers sync.WaitGroup
	for _, gid := range slices.Collect(maps.Keys(rpc.active)) {
		workers.Go(func() {
			var status rpcStatus
			removed := false
			for {
				if err := rpc.call(ctx, "aria2.tellStatus", &status, gid, []string{"gid", "status"}); err != nil || status.GID != gid {
					return
				}
				switch status.Status {
				case "complete", "error", "removed":
					rpc.forget(gid)
					return
				case "active", "waiting", "paused":
				default:
					return
				}
				if !removed {
					var confirmation string
					if err := rpc.call(ctx, "aria2.forceRemove", &confirmation, gid); err != nil {
						return
					}
					removed = true
				}
				if wait(ctx, 50*time.Millisecond) != nil {
					return
				}
			}
		})
	}
	workers.Wait()
	unsafe = make(map[string]bool)
	for gid, path := range rpc.active {
		unresolved = append(unresolved, gid)
		unsafe[path] = true
	}
	return
}
