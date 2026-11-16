// SPDX-License-Identifier: MIT

package main

import (
	"os"
	"strconv"
	"strings"
	"testing"
)

// Pin a security floor even when the bridge's custom ClientHello keeps the
// affected certificate-decompression path disabled. This is a module-policy
// regression, not a reproducer of the advisory's memory-exhaustion input.
func TestSecurityDependencyFloors(t *testing.T) {
	data, err := os.ReadFile("go.mod")
	if err != nil {
		t.Fatal(err)
	}
	versions := make(map[string]string)
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 2 {
			versions[fields[0]] = fields[1]
		}
	}
	for module, floor := range map[string][3]int{
		"github.com/andybalholm/brotli": {1, 2, 7},  // GO-2026-6888
		"golang.org/x/crypto":           {0, 59, 0}, // includes GO-2026-5932 fix
		"golang.org/x/sys":              {0, 44, 0}, // GO-2026-5024
	} {
		t.Run(module, func(t *testing.T) {
			version := versions[module]
			parts := strings.Split(strings.TrimPrefix(version, "v"), ".")
			if len(parts) != 3 {
				t.Fatalf("missing or non-stable module version: %q", version)
			}
			var actual [3]int
			for i, part := range parts {
				actual[i], err = strconv.Atoi(part)
				if err != nil {
					t.Fatalf("non-stable module version: %q", version)
				}
			}
			for i := range floor {
				if actual[i] > floor[i] {
					return
				}
				if actual[i] < floor[i] {
					t.Fatalf("%s is below security floor v%d.%d.%d", version, floor[0], floor[1], floor[2])
				}
			}
		})
	}
}
