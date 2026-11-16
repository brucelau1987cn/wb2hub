// SPDX-License-Identifier: BSD-3-Clause

package main

import (
	"testing"

	"github.com/klauspost/compress/fse"
)

// Regression input from klauspost/compress's BSD-licensed security fix:
// Copyright (c) 2012 The Go Authors. All rights reserved.
// Copyright (c) 2019 Klaus Post. All rights reserved.
// See licenses/compress.txt for the retained license and disclaimer.
// https://github.com/klauspost/compress/commit/7f5d8528720c895a6106f201719f87eb030a565d
// Exercise the dependency directly, not an alleged bridge exploit: the bridge's
// custom ClientHello does not advertise certificate compression.
func TestCompressionRejectsCountOverflowAfterScratchReuse(t *testing.T) {
	var scratch fse.Scratch
	scratch.DecompressLimit = 255
	seed := []byte{0x1a, 0x00, 0x00, 0x00, 0x90, 0xaf, 0xfa, 0xff, 0x03, 0x00, 0x80, 0xff, 0x7f}
	if _, err := fse.Decompress(seed, &scratch); err != nil {
		t.Fatal(err)
	}
	malformed := []byte{0x1a, 0x00, 0xe0, 0xff, 0x1f, 0x0f, 0xb1, 0x9c, 0x5a, 0xcf, 0x4d, 0xe4, 0x1d, 0x7b, 0xab, 0x99, 0x5e, 0x01, 0x51, 0xa4, 0x73, 0xd8, 0xb1, 0xf3, 0x39, 0x4b, 0x67, 0xf6}
	if _, err := fse.Decompress(malformed, &scratch); err == nil {
		t.Fatal("malformed normalized count accepted")
	}
}
