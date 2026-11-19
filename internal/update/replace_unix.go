//go:build !windows

package update

import (
	"errors"
	"fmt"
	"os"
	"syscall"

	"github.com/zoisythe/MSRMusicTool/internal/fsutil"
)

func Apply(s Staged, args []string) error {
	defer os.Remove(s.Path)
	lock := mkLock(s.Target)
	defer lock.Close()
	acquired, err := lock.TryLock()
	if err != nil {
		return err
	}
	if !acquired {
		return errors.New("另一实例正在更新")
	}
	if err := checkOriginal(s); err != nil {
		return err
	}
	if err := fsutil.Replace(s.Path, s.Target); err != nil {
		return err
	}
	lock.Close()
	if err := syscall.Exec(s.Target, append([]string{s.Target}, args...), os.Environ()); err != nil {
		return fmt.Errorf("已更新，无法重新启动，请手动运行 %s：%w", s.Target, err)
	}
	return nil
}

func RunHelper([]string) error { return errors.New("无效的内部命令") }
