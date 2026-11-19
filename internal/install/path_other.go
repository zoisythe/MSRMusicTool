//go:build !linux && !windows

package install

import "errors"

func persistedPath(string) bool { return false }
func addPath(string) error      { return errors.New("此系统使用便携运行") }
