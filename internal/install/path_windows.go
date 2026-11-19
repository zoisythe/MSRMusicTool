package install

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

func expandPath(value string) string {
	entries := filepath.SplitList(value)
	for i, entry := range entries {
		expanded, err := registry.ExpandString(entry)
		if err == nil {
			entries[i] = expanded
		}
	}
	return strings.Join(entries, string(os.PathListSeparator))
}

func persistedPath(directory string) bool {
	key, err := registry.OpenKey(registry.CURRENT_USER, "Environment", registry.QUERY_VALUE)
	if err != nil {
		return false
	}
	defer key.Close()
	current, _, err := key.GetStringValue("Path")
	return err == nil && inPath(expandPath(current), directory)
}

func addPath(directory string) error {
	key, _, err := registry.CreateKey(registry.CURRENT_USER, "Environment", registry.QUERY_VALUE|registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer key.Close()
	value, kind, err := key.GetStringValue("Path")
	if err != nil && !errors.Is(err, registry.ErrNotExist) {
		return err
	}
	if inPath(expandPath(value), directory) {
		return nil
	}
	if value != "" && !strings.HasSuffix(value, ";") {
		value += ";"
	}
	value += directory
	if kind == registry.EXPAND_SZ {
		err = key.SetExpandStringValue("Path", value)
	} else {
		err = key.SetStringValue("Path", value)
	}
	if err != nil {
		return err
	}
	proc := windows.NewLazySystemDLL("user32.dll").NewProc("SendMessageTimeoutW")
	environment, _ := windows.UTF16PtrFromString("Environment")
	var result uintptr
	proc.Call(0xffff, 0x001a, 0, uintptr(unsafe.Pointer(environment)), 0x0002, 2000, uintptr(unsafe.Pointer(&result)))
	return nil
}
