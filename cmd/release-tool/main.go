// Release-tool packages and exercises the same binary that will be published.
package main

import (
	"archive/zip"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/zoisythe/MSRMusicTool/internal/testsite"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 2 && args[0] == "commit-message" {
		return conventionalCommit(args[1])
	}
	if len(args) == 3 && args[0] == "pack" {
		return pack(args[1], args[2])
	}
	if len(args) >= 3 && args[0] == "smoke" {
		return smoke(args[1], args[2], args[3:])
	}
	return errors.New("usage: release-tool pack BINARY ZIP | release-tool smoke ZIP TAG [WRAPPER...]")
}

var conventional = regexp.MustCompile(`^(build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)(\([A-Za-z0-9._/-]+\))?!?: \S.*$`)

func conventionalCommit(file string) error {
	data, err := os.ReadFile(file)
	if err != nil {
		return err
	}
	line, _, _ := strings.Cut(strings.TrimPrefix(string(data), "\ufeff"), "\n")
	line = strings.TrimSpace(line)
	for _, prefix := range []string{"fixup! ", "squash! "} {
		if rest, ok := strings.CutPrefix(line, prefix); ok {
			line = rest
			break
		}
	}
	if !conventional.MatchString(line) {
		return errors.New("提交信息必须符合 Conventional Commits，例如 feat: add album search")
	}
	return nil
}

func pack(binary, archive string) error {
	data, err := os.Open(binary)
	if err != nil {
		return err
	}
	defer data.Close()
	info, err := data.Stat()
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return errors.New("expected executable file")
	}
	if err := os.MkdirAll(filepath.Dir(archive), 0755); err != nil {
		return err
	}
	f, err := os.Create(archive)
	if err != nil {
		return err
	}
	defer f.Close()
	w := zip.NewWriter(f)
	header := &zip.FileHeader{Name: filepath.Base(binary), Method: zip.Deflate}
	header.SetMode(0755)
	header.SetModTime(time.Date(2020, time.January, 1, 0, 0, 0, 0, time.UTC))
	content, err := w.CreateHeader(header)
	if err != nil {
		return err
	}
	if _, err := io.Copy(content, data); err != nil {
		return err
	}
	return w.Close()
}

func smoke(filename, tag string, wrapper []string) error {
	archive, err := zip.OpenReader(filename)
	if err != nil {
		return err
	}
	defer archive.Close()
	expected := "msr-music-tool"
	if strings.HasPrefix(filepath.Base(filename), "msr-music-tool-windows-") {
		expected += ".exe"
	}
	if len(archive.File) != 1 || archive.File[0].Name != expected || !archive.File[0].Mode().IsRegular() || archive.File[0].Mode().Perm() != 0755 {
		return fmt.Errorf("invalid single-file ZIP %s", filename)
	}
	dir, err := os.MkdirTemp("", "msr-release-smoke-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(dir)
	binary := filepath.Join(dir, expected)
	extracted, err := archive.File[0].Open()
	if err != nil {
		return err
	}
	defer extracted.Close()
	f, err := os.OpenFile(binary, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0755)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(f, extracted)
	if err := errors.Join(copyErr, f.Close()); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	fixture := testsite.New()
	defer fixture.Close()
	run := func(args ...string) (string, error) {
		items := append(append(append([]string(nil), wrapper...), binary), args...)
		command := exec.CommandContext(ctx, items[0], items[1:]...)
		command.Dir = dir
		command.Env = append(os.Environ(), "MSR_FIXTURE_URL="+fixture.Server.URL)
		output, err := command.CombinedOutput()
		if err != nil {
			return string(output), fmt.Errorf("%s: %w: %s", args[0], err, output)
		}
		return string(output), nil
	}
	output, err := run("--version")
	if err != nil {
		return err
	}
	if strings.TrimSpace(output) != "msr-music-tool "+strings.TrimPrefix(tag, "v") {
		return fmt.Errorf("invalid version: %s", output)
	}
	if output, err = run("search", "中文歌曲"); err != nil || !strings.Contains(output, "222222\t中文歌曲") {
		return fmt.Errorf("search: %s %w", output, err)
	}
	destination := filepath.Join(dir, "music")
	if _, err := run("download", "111111", "222222", "--output", destination); err != nil {
		return err
	}
	if fixture.Calls("GET", "/a1.jpg") != 1 {
		return errors.New("expected one cover download")
	}
	items, err := os.ReadDir(destination)
	if err != nil {
		return err
	}
	if len(items) != 6 {
		return fmt.Errorf("expected six output files: %v", items)
	}
	for _, item := range items {
		ext := path.Ext(item.Name())
		if ext != ".wav" && ext != ".lrc" && ext != ".jpg" {
			return fmt.Errorf("unexpected file %s", item.Name())
		}
	}
	if output, err := run("download", "111111", "222222", "--output", destination); err != nil || strings.Count(output, "skipped") != 2 {
		return fmt.Errorf("repeat download: %s %w", output, err)
	}
	fmt.Println(filepath.Base(filename), strings.TrimSpace(tag), "verified")
	return nil
}
