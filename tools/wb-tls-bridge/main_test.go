// SPDX-License-Identifier: MIT

package main

import (
	"bufio"
	"crypto/tls"
	"fmt"
	"net"
	"net/http"
	"testing"
	"time"

	utls "github.com/refraction-networking/utls"
)

func TestConnectTargetHost(t *testing.T) {
	for _, tc := range []struct {
		target string
		host   string
	}{
		{"example.com:443", "example.com"},
		{"127.0.0.1:1", "127.0.0.1"},
		{"[::1]:65535", "::1"},
	} {
		t.Run(tc.target, func(t *testing.T) {
			host, err := connectTargetHost(tc.target)
			if err != nil || host != tc.host {
				t.Fatalf("connectTargetHost(%q) = %q, %v; want %q, nil", tc.target, host, err, tc.host)
			}
		})
	}
}

func TestConnectTargetHostRejectsMalformedTargets(t *testing.T) {
	for _, target := range []string{"", "example.com", ":443", " :443", "example.com:0", "example.com:65536", "example.com:-1", "example.com:tls", "::1:443"} {
		t.Run(target, func(t *testing.T) {
			if _, err := connectTargetHost(target); err == nil {
				t.Fatalf("connectTargetHost(%q) unexpectedly succeeded", target)
			}
		})
	}
}

func TestOfficialHelloSpec(t *testing.T) {
	spec := officialHelloSpec()
	if spec.TLSVersMin != tls.VersionTLS12 || spec.TLSVersMax != tls.VersionTLS13 {
		t.Fatalf("TLS version range = %x..%x", spec.TLSVersMin, spec.TLSVersMax)
	}
	if len(spec.CipherSuites) != 18 || len(spec.Extensions) != 10 {
		t.Fatalf("got %d ciphers and %d extensions; want 18 and 10", len(spec.CipherSuites), len(spec.Extensions))
	}
	for _, extension := range spec.Extensions {
		if _, ok := extension.(*utls.ALPNExtension); ok {
			t.Fatal("official profile must not advertise ALPN")
		}
	}
}

func TestServeClientRejectsInvalidRequestsBeforeDial(t *testing.T) {
	const secret = "local-test-only-secret-0123456789abcdef"
	for _, tc := range []struct {
		name    string
		request string
		status  int
	}{
		{"method", "GET / HTTP/1.1\r\nHost: example.com\r\n\r\n", http.StatusMethodNotAllowed},
		{"missing-auth", "CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n", http.StatusForbidden},
		{"wrong-auth", "CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\nX-WB-Bridge-Auth: wrong\r\n\r\n", http.StatusForbidden},
		{"bad-target", fmt.Sprintf("CONNECT example.com:0 HTTP/1.1\r\nHost: example.com:0\r\nX-WB-Bridge-Auth: %s\r\n\r\n", secret), http.StatusBadRequest},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client, server := net.Pipe()
			defer client.Close()
			defer server.Close()
			if err := client.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
				t.Fatal(err)
			}
			go serveClient(server, secret)
			if _, err := fmt.Fprint(client, tc.request); err != nil {
				t.Fatal(err)
			}
			response, err := http.ReadResponse(bufio.NewReader(client), &http.Request{Method: http.MethodConnect})
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			if response.StatusCode != tc.status {
				t.Fatalf("status = %d; want %d", response.StatusCode, tc.status)
			}
		})
	}
}
