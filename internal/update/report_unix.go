//go:build !windows

package update

import (
	"fmt"
	"os"
)

func ReportHelperError(err error) { fmt.Fprintln(os.Stderr, err) }
