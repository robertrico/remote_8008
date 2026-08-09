// test_eb8008_host.c -- host-compiled unit tests for eb8008.c, the software
// Etherbone server. Style follows test_dhcp_host.c: no framework, assert-ish
// macros, exit code is the verdict.
//
// Build & run (also wired into `make test-c`):
//   cc -DEB8008_HOST_TEST -o /tmp/t firmware/eb8008.c firmware/test_eb8008_host.c && /tmp/t
//
// The bus is modeled as a sparse 64 KiB window at 0xf0000000 (CSR-like) so
// 32-bit wire addresses resolve without touching host memory. Every access
// is logged so tests can assert exactly which addresses were touched -- a
// wrong-address write is as much a failure as a wrong value.
//
// VPLAN: SW-EB-1..SW-EB-6 (see docs/VPLAN.md "Software Etherbone" family).

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "eb8008.h"

#define CHECK(cond, name) do { \
    tests++; \
    if (cond) { printf("PASS %s\n", name); } \
    else      { printf("FAIL %s (line %d)\n", name, __LINE__); fails++; } \
} while (0)

static int tests, fails;

// ── bus model ───────────────────────────────────────────────────────────────
#define BUS_BASE 0xf0000000u
#define BUS_SIZE 0x10000u
static uint32_t bus_mem[BUS_SIZE / 4];
static uint32_t bus_reads, bus_writes;
static uint32_t last_write_addr, last_read_addr;

uint32_t eb8008_bus_read(uint32_t addr)
{
    bus_reads++;
    last_read_addr = addr;
    if (addr - BUS_BASE < BUS_SIZE)
        return bus_mem[(addr - BUS_BASE) / 4];
    return 0xdeadbeef; // out-of-window reads yield a recognizable poison
}

void eb8008_bus_write(uint32_t addr, uint32_t val)
{
    bus_writes++;
    last_write_addr = addr;
    if (addr - BUS_BASE < BUS_SIZE)
        bus_mem[(addr - BUS_BASE) / 4] = val;
}

static void bus_reset(void)
{
    memset(bus_mem, 0, sizeof(bus_mem));
    bus_reads = bus_writes = 0;
}

// ── packet builders (mirror litex etherbone.py encoding) ────────────────────
static void put32(uint8_t *p, uint32_t v)
{
    p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v;
}

static int hdr(uint8_t *p, int pf)
{
    p[0] = 0x4e; p[1] = 0x6f;
    p[2] = (uint8_t)(0x10 | (pf ? 0x01 : 0));
    p[3] = 0x44;
    p[4] = p[5] = p[6] = p[7] = 0;
    return 8;
}

static int record(uint8_t *p, int wcount, uint32_t wbase, const uint32_t *wdata,
                  int rcount, uint32_t rbase, const uint32_t *raddrs)
{
    int n = 0;
    p[0] = 0; p[1] = 0x0f; p[2] = (uint8_t)wcount; p[3] = (uint8_t)rcount;
    n = 4;
    if (wcount) {
        put32(p + n, wbase); n += 4;
        for (int i = 0; i < wcount; i++) { put32(p + n, wdata[i]); n += 4; }
    }
    if (rcount) {
        put32(p + n, rbase); n += 4;
        for (int i = 0; i < rcount; i++) { put32(p + n, raddrs[i]); n += 4; }
    }
    return n;
}

static uint32_t get32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8)  |  (uint32_t)p[3];
}

// Reply buffer sized to the documented contract; a canary tail catches any
// write past resp_len as well as past the contract bound.
#define REQ_MAX 1500
#define CANARY  0xC5
static uint8_t req[REQ_MAX];
static uint8_t resp[REQ_MAX + 16 + 64];

static int handle(int req_len)
{
    memset(resp, CANARY, sizeof(resp));
    int n = eb8008_handle(req, req_len, resp);
    // contract from eb8008.h: replies never exceed req_len + 16
    if (n > req_len + 16) {
        printf("FAIL contract: resp %d > req %d + 16 (line %d)\n", n, req_len, __LINE__);
        fails++; tests++;
    }
    for (int i = n; i < (int)sizeof(resp); i++) {
        if (resp[i] != CANARY) {
            printf("FAIL canary: resp[%d] clobbered beyond resp_len=%d\n", i, n);
            fails++; tests++;
            break;
        }
    }
    return n;
}

// ── tests ───────────────────────────────────────────────────────────────────
static void test_probe(void)
{
    int n = hdr(req, 1);
    put32(req + n, 0); n += 4; // CommUDP.probe pads 4 bytes
    int rn = handle(n);
    CHECK(rn == 12, "probe: reply length 12");
    CHECK(resp[0] == 0x4e && resp[1] == 0x6f, "probe: reply magic");
    CHECK(resp[2] == 0x12, "probe: version=1 + pr set, pf clear");
    CHECK(resp[3] == 0x44, "probe: addr/port size 4/4");
}

static void test_bad_magic_and_short(void)
{
    memset(req, 0, 16);
    CHECK(handle(16) == 0, "bad magic: no reply");
    hdr(req, 0);
    CHECK(handle(7) == 0, "short packet: no reply");
}

static void test_write_record(void)
{
    bus_reset();
    uint32_t data[3] = { 0x11111111, 0x22222222, 0x33333333 };
    int n = hdr(req, 0);
    n += record(req + n, 3, BUS_BASE + 0x100, data, 0, 0, NULL);
    int rn = handle(n);
    CHECK(rn == 0, "write: no reply (fire-and-forget)");
    CHECK(bus_writes == 3, "write: exactly 3 bus writes");
    CHECK(bus_mem[0x100/4] == 0x11111111 &&
          bus_mem[0x104/4] == 0x22222222 &&
          bus_mem[0x108/4] == 0x33333333, "write: values at incrementing addrs");
}

static void test_read_record(void)
{
    bus_reset();
    bus_mem[0x200/4] = 0xAAAA5555;
    bus_mem[0x208/4] = 0x0BADF00D;
    uint32_t addrs[2] = { BUS_BASE + 0x200, BUS_BASE + 0x208 };
    int n = hdr(req, 0);
    n += record(req + n, 0, 0, NULL, 2, 0xCAFED00D, addrs);
    int rn = handle(n);
    CHECK(rn == 8 + 8 + 8, "read: reply = header + record hdr/base + 2 words");
    CHECK(resp[2] == 0x10, "read: reply pr/pf clear");
    CHECK(resp[8+2] == 2 && resp[8+3] == 0, "read: reply wcount=rcount, rcount=0");
    CHECK(get32(resp + 12) == 0xCAFED00D, "read: base_ret_addr echoed (correlation id)");
    CHECK(get32(resp + 16) == 0xAAAA5555 && get32(resp + 20) == 0x0BADF00D,
          "read: values in order");
    CHECK(bus_reads == 2, "read: exactly 2 bus reads");
}

static void test_write_then_read_same_record(void)
{
    bus_reset();
    uint32_t data[1]  = { 0x600D600D };
    uint32_t addrs[1] = { BUS_BASE + 0x300 };
    int n = hdr(req, 0);
    n += record(req + n, 1, BUS_BASE + 0x300, data, 1, 7, addrs);
    int rn = handle(n);
    CHECK(rn == 8 + 8 + 4, "w+r record: reply sized for the read half");
    CHECK(get32(resp + 16) == 0x600D600D, "w+r record: read sees the write");
}

static void test_multi_record(void)
{
    bus_reset();
    bus_mem[0x400/4] = 1; bus_mem[0x404/4] = 2;
    uint32_t a1[1] = { BUS_BASE + 0x400 };
    uint32_t a2[1] = { BUS_BASE + 0x404 };
    int n = hdr(req, 0);
    n += record(req + n, 0, 0, NULL, 1, 100, a1);
    n += record(req + n, 0, 0, NULL, 1, 200, a2);
    int rn = handle(n);
    // one reply packet, two records appended
    CHECK(rn == 8 + (8+4) + (8+4), "multi-record: both answered in one reply");
    CHECK(get32(resp + 12) == 100 && get32(resp + 24) == 200,
          "multi-record: correlation ids in order");
}

static void test_truncated_records(void)
{
    // wcount promises 4 words but the packet ends after 1: must not read
    // past the buffer or write more than the bytes actually present allow.
    bus_reset();
    int n = hdr(req, 0);
    req[n] = 0; req[n+1] = 0x0f; req[n+2] = 4; req[n+3] = 0; // wcount=4
    put32(req + n + 4, BUS_BASE + 0x500);
    put32(req + n + 8, 0x77777777);
    int rn = handle(n + 12); // record truncated mid-payload
    CHECK(rn == 0, "truncated write: no reply");
    CHECK(bus_writes == 0, "truncated write: no bus writes at all");

    bus_reset();
    n = hdr(req, 0);
    req[n] = 0; req[n+1] = 0x0f; req[n+2] = 0; req[n+3] = 8; // rcount=8
    put32(req + n + 4, 0x1234);
    rn = handle(n + 8); // no addresses present
    CHECK(rn == 0, "truncated read: no reply");
    CHECK(bus_reads == 0, "truncated read: no bus reads");
}

static void test_255_word_burst(void)
{
    // SPEC S-WIRE-5 heritage: the hardware core needed buffer_depth=255 for
    // 255-word bursts; the software server must handle the same burst.
    bus_reset();
    uint32_t data[255];
    for (int i = 0; i < 255; i++) data[i] = 0x1000 + i;
    int n = hdr(req, 0);
    n += record(req + n, 255, BUS_BASE, data, 0, 0, NULL);
    handle(n);
    int ok = (bus_writes == 255);
    for (int i = 0; i < 255 && ok; i++)
        if (bus_mem[i] != (uint32_t)(0x1000 + i)) ok = 0;
    CHECK(ok, "255-word write burst lands intact");

    bus_reset();
    uint32_t addrs[255];
    for (int i = 0; i < 255; i++) addrs[i] = BUS_BASE + 4*i;
    n = hdr(req, 0);
    n += record(req + n, 0, 0, NULL, 255, 42, addrs);
    int rn = handle(n);
    CHECK(rn == 8 + 8 + 4*255, "255-word read burst reply size");
}

static void test_bounds_sweep(void)
{
    // Adversarial: every (wcount, rcount) x truncation point over a small
    // window. The CHECKs inside handle() enforce the size contract and the
    // canary on every single call; this sweep just drives the space.
    int calls = 0, effect_violations = 0;
    for (int wc = 0; wc <= 6; wc++) {
        for (int rc = 0; rc <= 6; rc++) {
            uint32_t data[6], addrs[6];
            for (int i = 0; i < 6; i++) { data[i] = i; addrs[i] = BUS_BASE + 4*i; }
            int full = hdr(req, 0);
            full += record(req + full, wc, BUS_BASE, wc ? data : NULL,
                           rc, 9, rc ? addrs : NULL);
            // byte offsets at which each section is completely present
            int write_done = 8 + 4 + (wc ? 4 * (1 + wc) : 0);
            int read_done  = write_done + (rc ? 4 * (1 + rc) : 0);
            for (int cut = 0; cut <= full; cut++) {
                bus_reset();
                handle(cut);
                calls++;
                // Exact bus-effect law: a section executes iff every one of
                // its bytes arrived. Anything else is the parser trusting a
                // count it could not verify.
                uint32_t want_w = (wc && cut >= write_done) ? (uint32_t)wc : 0;
                uint32_t want_r = (rc && cut >= read_done)  ? (uint32_t)rc : 0;
                if (bus_writes != want_w || bus_reads != want_r)
                    effect_violations++;
            }
        }
    }
    printf("bounds sweep: %d adversarial calls, %d bus-effect violations\n",
           calls, effect_violations);
    CHECK(effect_violations == 0, "bounds sweep: exact bus-effect law held");
}

int main(void)
{
    test_probe();
    test_bad_magic_and_short();
    test_write_record();
    test_read_record();
    test_write_then_read_same_record();
    test_multi_record();
    test_truncated_records();
    test_255_word_burst();
    test_bounds_sweep();

    printf("%s: %d tests, %d failures\n", fails ? "FAIL" : "ALL PASS", tests, fails);
    return fails ? 1 : 0;
}
