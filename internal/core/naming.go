package core

import (
	"mime"
	"net/url"
	"path"
	"regexp"
	"strings"
	"unicode/utf8"

	"golang.org/x/text/unicode/norm"
)

const filePrefix = "塞壬唱片-MSR - "
const maxFilenameBytes = 240

var substitutions = strings.NewReplacer("<", "＜", ">", "＞", ":", "：", "\"", "＂", "/", "／", "\\", "＼", "|", "｜", "?", "？", "*", "＊")
var mediaSuffix = regexp.MustCompile("^\\.[a-zA-Z0-9]{1,8}$")
var commonMIME = map[string]string{
	"audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
	"audio/flac": ".flac", "audio/ogg": ".ogg", "image/jpeg": ".jpg", "image/png": ".png", "text/plain": ".txt",
}

func sanitize(value string) string {
	value = strings.Map(func(r rune) rune {
		if r < 32 {
			return ' '
		}
		return r
	}, norm.NFC.String(value))
	value = strings.TrimRight(strings.TrimSpace(substitutions.Replace(value)), ". ")
	if value == "" {
		return "未命名"
	}
	return value
}

func Extension(address, contentType, fallback string) string {
	if uri, err := url.Parse(address); err == nil {
		extension := strings.ToLower(path.Ext(uri.Path))
		if extension == ".jpeg" {
			return ".jpg"
		}
		if mediaSuffix.MatchString(extension) {
			return extension
		}
	}
	kind := strings.ToLower(strings.TrimSpace(strings.SplitN(contentType, ";", 2)[0]))
	if extension := commonMIME[kind]; extension != "" {
		return extension
	}
	if extensions, _ := mime.ExtensionsByType(kind); len(extensions) > 0 && mediaSuffix.MatchString(extensions[0]) {
		return extensions[0]
	}
	return fallback
}

type Namer struct {
	catalog Catalog
	names   map[string]int
}

func NewNamer(c Catalog) Namer {
	counts := make(map[string]int)
	for _, song := range c.Songs {
		counts[Fold(sanitize(song.Name))]++
	}
	return Namer{c, counts}
}

func (n Namer) Filename(song Song, extension string) string {
	title := sanitize(song.Name)
	conflict := n.names[Fold(title)] > 1
	album := ""
	suffix := ""
	if conflict {
		album = n.catalog.AlbumByID[song.AlbumCID].Name
		if album == "" {
			album = song.AlbumCID
		}
		album = sanitize(album)
		suffix = " [" + album + "-" + song.CID + "]"
	}
	name := filePrefix + title + suffix + extension
	if len(name) <= maxFilenameBytes {
		return name
	}
	if conflict {
		budget := maxFilenameBytes - len(filePrefix+" [-"+song.CID+"]"+extension)
		suffix = " [" + shorten(album, budget/2) + "-" + song.CID + "]"
	} else {
		suffix = " ~" + song.CID
	}
	return filePrefix + shorten(title, maxFilenameBytes-len(filePrefix+suffix+extension)) + suffix + extension
}

func shorten(value string, budget int) string {
	if len(value) <= budget {
		return value
	}
	const ellipsis = "…"
	if budget <= len(ellipsis) {
		return ""
	}
	available := budget - len(ellipsis)
	head, tail := available/2, len(value)-(available-available/2)
	for !utf8.RuneStart(value[head]) {
		head--
	}
	for !utf8.RuneStart(value[tail]) {
		tail++
	}
	return value[:head] + ellipsis + value[tail:]
}
