package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"log"
	"net"
	"os"
	"time"
)

const (
	ListenAddr = ":9000"
	MsgLen     = 32
)

func tag32(secret []byte, msg []byte) uint32 {
	mac := hmac.New(sha256.New, secret)
	mac.Write(msg[:28]) // tag covers first 28 bytes; last 4 bytes are the tag itself
	sum := mac.Sum(nil)
	return binary.BigEndian.Uint32(sum[:4])
}

func decodeHexIfValid(s string) ([]byte, bool) {
	if len(s)%2 != 0 || len(s) == 0 {
		return nil, false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F') {
			continue
		}
		return nil, false
	}
	out := make([]byte, len(s)/2)
	for i := 0; i < len(out); i++ {
		hi := s[i*2]
		lo := s[i*2+1]
		out[i] = (hexNibble(hi) << 4) | hexNibble(lo)
	}
	return out, true
}

func hexNibble(c byte) byte {
	switch {
	case c >= '0' && c <= '9':
		return c - '0'
	case c >= 'a' && c <= 'f':
		return c - 'a' + 10
	case c >= 'A' && c <= 'F':
		return c - 'A' + 10
	default:
		return 0
	}
}

func main() {
	secretEnv := os.Getenv("LATTICE_SECRET")
	if secretEnv == "" {
		secretEnv = os.Getenv("LATTICE_SECRET_HEX")
	}
	secret, ok := decodeHexIfValid(secretEnv)
	if !ok {
		secret = []byte(secretEnv)
	}
	if len(secret) < 16 {
		log.Fatal("Set LATTICE_SECRET (raw) or LATTICE_SECRET_HEX (hex) env var (>=16 bytes recommended)")
	}

	udpAddr, err := net.ResolveUDPAddr("udp", ListenAddr)
	if err != nil {
		log.Fatal(err)
	}

	pc, err := net.ListenUDP("udp", udpAddr)
	if err != nil {
		log.Fatal(err)
	}
	defer pc.Close()

	log.Printf("LATTICE UDP echo listening on %s/udp", ListenAddr)

	_ = pc.SetReadBuffer(1 << 20)
	_ = pc.SetWriteBuffer(1 << 20)

	buf := make([]byte, MsgLen)

	// Lightweight per-source token bucket (also firewall allowlist in production!)
	type bucket struct {
		tokens   int
		last     time.Time
		lastSeen time.Time
	}
	limits := make(map[string]*bucket)

	const (
		maxTokens   = 60              // burst capacity
		refillPerS  = 30              // tokens per second
		cost        = 1
		bucketTTL   = 2 * time.Minute
		sweepEvery  = 30 * time.Second
	)

	lastSweep := time.Now()

	for {
		n, addr, err := pc.ReadFromUDP(buf)
		if err != nil {
			continue
		}
		if n != MsgLen {
			continue
		}

		// Rate limit by source IP (not ip:port)
		key := addr.IP.String()
		now := time.Now()
		b, ok := limits[key]
		if !ok {
			b = &bucket{tokens: maxTokens, last: now, lastSeen: now}
			limits[key] = b
		}
		elapsed := now.Sub(b.last).Seconds()
		if elapsed > 0 {
			b.tokens += int(elapsed * refillPerS)
			if b.tokens > maxTokens {
				b.tokens = maxTokens
			}
			b.last = now
		}
		b.lastSeen = now
		if b.tokens < cost {
			continue
		}
		b.tokens -= cost

		msg := buf[:MsgLen]
		if msg[0] != 'L' || msg[1] != 'A' || msg[2] != 'T' || msg[3] != 'O' {
			continue
		}

		want := tag32(secret, msg)
		got := binary.BigEndian.Uint32(msg[28:32])
		if want != got {
			continue
		}

		_, _ = pc.WriteToUDP(msg, addr) // echo 1:1 (not an amplifier)

		// Periodic cleanup of idle buckets.
		if now.Sub(lastSweep) >= sweepEvery && len(limits) > 0 {
			for k, v := range limits {
				if now.Sub(v.lastSeen) > bucketTTL {
					delete(limits, k)
				}
			}
			lastSweep = now
		}
	}
}
