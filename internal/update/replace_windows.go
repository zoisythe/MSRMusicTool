package update

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/zoisythe/MSRMusicTool/internal/fsutil"
	"golang.org/x/sys/windows"
)

func Apply(s Staged, args []string) error {
	lock := mkLock(s.Target)
	defer lock.Close()
	acquired, err := lock.TryLock()
	if err != nil {
		os.Remove(s.Path)
		return err
	}
	if !acquired {
		os.Remove(s.Path)
		return errors.New("另一实例正在更新")
	}
	if err := checkOriginal(s); err != nil {
		os.Remove(s.Path)
		return err
	}
	helperArgs := append([]string{"--internal-update", s.Target, s.Original, strconv.Itoa(os.Getpid())}, args...)
	command := exec.Command(s.Path, helperArgs...)
	command.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NO_WINDOW, HideWindow: true}
	out, err := command.StdoutPipe()
	if err != nil {
		os.Remove(s.Path)
		return err
	}
	if err := command.Start(); err != nil {
		out.Close()
		os.Remove(s.Path)
		return err
	}
	ready := make(chan error, 1)
	go func() {
		reader := bufio.NewReader(out)
		line, err := reader.ReadString('\n')
		line = strings.TrimSpace(line)
		if reason, ok := strings.CutPrefix(line, "error: "); ok {
			ready <- errors.New(reason)
			return
		}
		if err != nil || line != "ready" {
			ready <- errors.New("更新进程在完成握手前退出")
			return
		}
		ready <- nil
	}()
	timeout := time.NewTimer(20 * time.Second)
	defer timeout.Stop()
	var handoffErr error
	select {
	case handoffErr = <-ready:
		if handoffErr == nil {
			out.Close()
			return nil
		}
	case <-timeout.C:
		handoffErr = errors.New("等待更新进程超时")
	}
	command.Process.Kill()
	command.Wait()
	os.Remove(s.Path)
	return fmt.Errorf("无法启动 Windows 更新进程：%w", handoffErr)
}

type beforeHandoff struct{ error }

func RunHelper(args []string) (err error) {
	ready := false
	defer func() {
		if err != nil && !ready {
			fmt.Fprintln(os.Stdout, "error:", err)
			err = beforeHandoff{err}
		}
	}()
	if len(args) < 3 {
		return errors.New("无效的更新交接")
	}
	stage, err := executable()
	if err != nil {
		return err
	}
	target := args[0]
	if !filepath.IsAbs(target) || !sameFile(filepath.Dir(stage), filepath.Dir(target)) ||
		!strings.HasPrefix(filepath.Base(stage), ".msr-update-") || !strings.HasSuffix(stage, ".exe") ||
		strings.EqualFold(stage, target) {
		return errors.New("无效的更新文件位置")
	}
	pid, err := strconv.ParseUint(args[2], 10, 32)
	if err != nil {
		return err
	}
	parent, err := windows.OpenProcess(windows.SYNCHRONIZE|windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return err
	}
	defer windows.CloseHandle(parent)
	buf := make([]uint16, 32768)
	length := uint32(len(buf))
	if err := windows.QueryFullProcessImageName(parent, 0, &buf[0], &length); err != nil {
		return err
	}
	imagePath := windows.UTF16ToString(buf[:length])
	if !sameFile(imagePath, target) {
		return errors.New("更新目标与父进程不符")
	}
	if _, err := fmt.Fprintln(os.Stdout, "ready"); err != nil {
		return err
	}
	ready = true
	status, err := windows.WaitForSingleObject(parent, 30000)
	if err != nil || status != windows.WAIT_OBJECT_0 {
		return errors.New("旧程序未退出，更新未应用")
	}
	lock := mkLock(target)
	defer lock.Close()
	acquired, err := lock.TryLock()
	if err != nil {
		return err
	}
	if !acquired {
		return errors.New("另一实例正在更新")
	}
	if err := checkOriginal(Staged{Target: target, Path: stage, Original: args[1]}); err != nil {
		return err
	}
	var replaceErr error
	for range 12 {
		replaceErr = fsutil.Replace(stage, target)
		if replaceErr == nil {
			break
		}
		if !errors.Is(replaceErr, windows.ERROR_SHARING_VIOLATION) && !errors.Is(replaceErr, windows.ERROR_ACCESS_DENIED) {
			break
		}
		time.Sleep(250 * time.Millisecond)
	}
	if replaceErr != nil {
		return replaceErr
	}
	lock.Close()
	command := exec.Command(target, args[3:]...)
	command.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NEW_CONSOLE}
	if err := command.Start(); err != nil {
		return fmt.Errorf("已更新，请手动运行 %s：%w", target, err)
	}
	return command.Process.Release()
}

func sameFile(a, b string) bool {
	first, err := os.Stat(a)
	if err != nil {
		return false
	}
	second, err := os.Stat(b)
	return err == nil && os.SameFile(first, second)
}

func ReportHelperError(err error) {
	var early beforeHandoff
	if errors.As(err, &early) {
		return
	}
	body, _ := windows.UTF16PtrFromString(err.Error())
	title, _ := windows.UTF16PtrFromString("msr-music-tool 更新")
	windows.MessageBox(0, body, title, windows.MB_ICONERROR)
}
