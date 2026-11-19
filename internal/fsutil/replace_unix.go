//go:build !windows

package fsutil

import "os"

func Replace(source, target string) error {
	return os.Rename(source, target)
}
