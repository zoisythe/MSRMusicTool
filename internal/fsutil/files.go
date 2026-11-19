package fsutil

import (
	"io"
	"os"
	"path/filepath"
)

// Write stages data on the same filesystem and publishes it only after a complete write.
func Write(path string, permissions os.FileMode, write func(io.Writer) error) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".msr-write-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	err = write(f)
	if err == nil {
		err = f.Chmod(permissions)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	return Replace(f.Name(), path)
}

func Copy(source, target string, permissions os.FileMode) error {
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	return Write(target, permissions, func(w io.Writer) error {
		_, err := io.Copy(w, input)
		return err
	})
}
