// test_eb_serve_host.c -- the Etherbone serve glue (eb_serve.c) end-to-end
// against the mocked ethmac: frame in via udp.c's real RX path, reply frame
// out via the real TX path.
//
// VPLAN: SWEB-7 (any-port magic gate + port mirroring), SWEB-8 (MAC-capture
// unicast reply, no ARP). Build (wired into `make test-c`):
//   cc -Ifirmware/hostmocks -Ilitex/litex/soc/software -DETH_UDP_BROADCAST \
//      -DEB8008_HOST_TEST -o /tmp/t firmware/udp.c firmware/eb8008.c \
//      firmware/eb_serve.c firmware/test_eb_serve_host.c

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <libliteeth/udp.h>

#include "eb_serve.h"

#define CHECK(cond, name) do { \
    tests++; \
    if (cond) { printf("PASS %s\n", name); } \
    else      { printf("FAIL %s (line %d)\n", name, __LINE__); fails++; } \
} while (0)

static int tests, fails;

// ── ethmac model ────────────────────────────────────────────────────────────
uint8_t  mock_ethmac_mem[4 * 2048];
uint8_t  mock_reader_ready = 1;
uint32_t mock_reader_slot, mock_reader_length, mock_reader_starts;
uint8_t  mock_writer_pending;
uint32_t mock_writer_slot, mock_writer_length;

static struct { uint8_t data[2048]; uint32_t len; } tx_log[8];
static int tx_count;

void mock_reader_start_hook(void)
{
    if (tx_count < 8) {
        memcpy(tx_log[tx_count].data,
               mock_ethmac_mem + 2048 * (2 + mock_reader_slot),
               mock_reader_length);
        tx_log[tx_count].len = mock_reader_length;
    }
    tx_count++;
}

// eb8008 bus model (EB8008_HOST_TEST)
static uint32_t bus_mem[64];
uint32_t eb8008_bus_read(uint32_t addr)  { return bus_mem[(addr & 0xff) / 4]; }
void eb8008_bus_write(uint32_t addr, uint32_t val) { bus_mem[(addr & 0xff) / 4] = val; }

// ── frame plumbing ──────────────────────────────────────────────────────────
static const uint8_t MAC_BOARD[6] = {0x10,0xe2,0xd5,0x00,0x00,0x02};
static const uint8_t MAC_PEER[6]  = {0xaa,0xbb,0xcc,0xdd,0xee,0x02};
#define IP_BOARD 0xc0a8012cu
#define IP_PEER  0xc0a80107u

static void put16(uint8_t *p, uint16_t v) { p[0] = v >> 8; p[1] = v; }
static void put32(uint8_t *p, uint32_t v)
{
    p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v;
}

static void inject_udp(uint32_t dst_ip, uint16_t src_port, uint16_t dst_port,
                       const uint8_t *payload, int plen)
{
    static uint8_t f[2048];
    memset(f, 0, sizeof(f));
    if (dst_ip == 0xffffffffu || (dst_ip & 0xff) == 0xff)
        memset(f, 0xff, 6);
    else
        memcpy(f, MAC_BOARD, 6);
    memcpy(f + 6, MAC_PEER, 6);
    put16(f + 12, 0x0800);
    uint8_t *ip = f + 14;
    ip[0] = 0x45;
    put16(ip + 2, (uint16_t)(28 + plen));
    ip[8] = 64; ip[9] = 0x11;
    put32(ip + 12, IP_PEER);
    put32(ip + 16, dst_ip);
    put16(f + 34, src_port);
    put16(f + 36, dst_port);
    put16(f + 38, (uint16_t)(8 + plen));
    memcpy(f + 42, payload, (size_t)plen);
    uint32_t len = (uint32_t)(42 + plen);
    if (len < 60) len = 60;
    memcpy(mock_ethmac_mem, f, len);
    mock_writer_slot = 0;
    mock_writer_length = len;
    mock_writer_pending = 1;
    udp_service();
}

static const uint8_t PROBE[12] = {0x4e,0x6f,0x11,0x44, 0,0,0,0, 0,0,0,0};

// ── tests ───────────────────────────────────────────────────────────────────
static void test_probe_on_rewritten_port_mirrored(void)  // SWEB-7
{
    tx_count = 0;
    inject_udp(IP_BOARD, 31337, 777, PROBE, sizeof(PROBE));  // NAT-mangled port
    eb_serve_pending();
    CHECK(eb_served == 1, "request on non-1234 port served (magic gate)");
    CHECK(tx_count == 1, "one reply frame");
    const uint8_t *r = tx_log[0].data;
    CHECK((r[34] << 8 | r[35]) == 777 && (r[36] << 8 | r[37]) == 31337,
          "reply mirrors the request's dst/src ports");
    CHECK(memcmp(r, MAC_PEER, 6) == 0, "reply unicast to captured MAC");  // SWEB-8
    CHECK(r[42] == 0x4e && r[43] == 0x6f && (r[44] & 0x02),
          "reply payload is a probe reply");
}

static void test_broadcast_request_served(void)  // SWEB-7 via bx path
{
    tx_count = 0;
    uint32_t before = eb_served;
    inject_udp(IP_BOARD | 0xff, 40000, 1234, PROBE, sizeof(PROBE));
    eb_serve_pending();
    CHECK(eb_served == before + 1, "subnet-broadcast request served");
    CHECK(memcmp(tx_log[0].data, MAC_PEER, 6) == 0,
          "broadcast request still answered unicast");
}

static void test_non_etherbone_ignored(void)
{
    uint32_t before_any = eb_cb_any, before_port = eb_cb_port;
    tx_count = 0;
    const uint8_t junk[8] = {1,2,3,4,5,6,7,8};
    inject_udp(IP_BOARD, 5353, 5353, junk, sizeof(junk));
    eb_serve_pending();
    CHECK(eb_cb_any == before_any + 1, "callback saw the frame");
    CHECK(eb_cb_port == before_port, "non-magic payload not stashed");
    CHECK(tx_count == 0, "no reply to non-Etherbone traffic");
}

static void test_write_request_no_reply(void)
{
    // header + record with wcount=1: base addr + one word
    uint8_t req[8 + 4 + 8];
    memcpy(req, PROBE, 4);
    req[2] = 0x10;                      // clear pf
    memset(req + 4, 0, 4);
    req[8] = 0; req[9] = 0x0f; req[10] = 1; req[11] = 0;
    put32(req + 12, 0x10);              // bus addr (model window)
    put32(req + 16, 0xfeedface);
    tx_count = 0;
    inject_udp(IP_BOARD, 50000, 1234, req, sizeof(req));
    eb_serve_pending();
    CHECK(tx_count == 0, "write is fire-and-forget");
    CHECK(bus_mem[0x10 / 4] == 0xfeedface, "write landed on the bus model");
}

static void test_busy_and_oversize_guards(void)
{
    // second request while one is pending: dropped, first one still served
    uint32_t before = eb_served;
    tx_count = 0;
    inject_udp(IP_BOARD, 1111, 1234, PROBE, sizeof(PROBE));   // stashed
    inject_udp(IP_BOARD, 2222, 1234, PROBE, sizeof(PROBE));   // busy -> dropped
    eb_serve_pending();
    eb_serve_pending();                                        // nothing left
    CHECK(eb_served == before + 1, "busy guard: only the first request served");
    CHECK((tx_log[0].data[36] << 8 | tx_log[0].data[37]) == 1111,
          "the served one was the first");

    // oversized Etherbone payload: magic OK but bigger than EB_BUF_LEN
    static uint8_t big[1600];
    memcpy(big, PROBE, 4);
    before = eb_served;
    inject_udp(IP_BOARD, 3333, 1234, big, (int)sizeof(big));
    eb_serve_pending();
    CHECK(eb_served == before, "oversized request not stashed");
}

int main(void)
{
    udp_start(MAC_BOARD, 0);
    udp_set_ip(IP_BOARD);
    udp_set_callback(eb_rx_callback);
    udp_set_broadcast_callback(eb_rx_callback);

    test_probe_on_rewritten_port_mirrored();
    test_broadcast_request_served();
    test_non_etherbone_ignored();
    test_write_request_no_reply();
    test_busy_and_oversize_guards();

    printf("%s: %d tests, %d failures\n", fails ? "FAIL" : "ALL PASS", tests, fails);
    return fails ? 1 : 0;
}
