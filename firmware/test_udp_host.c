// test_udp_host.c -- host-compiled unit tests for the firmware/udp.c fork.
//
// VPLAN: SWEB-8 (ARP-free reply addressing), SWEB-10 (broadcast RX
// dispatch), SWEB-11 (min-frame TX padding). The ethmac is modeled through
// the hostmocks/ headers: 4 contiguous 2048-byte slots (rx 0-1, tx 2-3);
// a frame is "received" by writing a slot + raising the writer event, and
// every transmitted frame is captured when the reader is started.
//
// Build & run (wired into `make test-c`):
//   cc -Ifirmware/hostmocks -Ilitex/litex/soc/software \
//      -DETH_UDP_BROADCAST -o /tmp/t firmware/udp.c firmware/test_udp_host.c

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <libliteeth/udp.h>

// fork-only exports (not in the vendored udp.h; declared like main.c does)
extern uint8_t udp_last_src_mac[6];
void udp_set_peer(uint32_t ip, const uint8_t *mac);
void udp_announce_arp(void);
int  udp_arp_refresh(uint32_t ip);

#define CHECK(cond, name) do { \
    tests++; \
    if (cond) { printf("PASS %s\n", name); } \
    else      { printf("FAIL %s (line %d)\n", name, __LINE__); fails++; } \
} while (0)

static int tests, fails;

// ── ethmac model (backs hostmocks/generated/csr.h + mem.h) ─────────────────
uint8_t  mock_ethmac_mem[4 * 2048];
uint8_t  mock_reader_ready = 1;
uint32_t mock_reader_slot, mock_reader_length, mock_reader_starts;
uint8_t  mock_writer_pending;
uint32_t mock_writer_slot, mock_writer_length;

// every transmitted frame, captured at reader-start
#define TX_LOG_MAX 16
static struct { uint8_t data[2048]; uint32_t len; } tx_log[TX_LOG_MAX];
static int tx_count;

// Auto-responder: lets blocking flows (udp_arp_resolve, send_ping) complete
// by answering the just-transmitted frame straight into an rx slot.
enum { RESPOND_NONE, RESPOND_ARP, RESPOND_PONG };
static int responder = RESPOND_NONE;
static void build_and_arm_reply(const uint8_t *tx, uint32_t len);

void mock_reader_start_hook(void)
{
    if (tx_count < TX_LOG_MAX) {
        uint8_t *slot = mock_ethmac_mem + 2048 * (2 + mock_reader_slot);
        memcpy(tx_log[tx_count].data, slot, mock_reader_length);
        tx_log[tx_count].len = mock_reader_length;
        if (responder != RESPOND_NONE)
            build_and_arm_reply(tx_log[tx_count].data, mock_reader_length);
    }
    tx_count++;
}

static void tx_reset(void) { tx_count = 0; }

// deliver a frame into rx slot 0 and run the service loop once
static void rx_inject(const uint8_t *frame, uint32_t len)
{
    memcpy(mock_ethmac_mem, frame, len);
    mock_writer_slot = 0;
    mock_writer_length = len;
    mock_writer_pending = 1;
    udp_service();
}

// ── frame builders ──────────────────────────────────────────────────────────
static const uint8_t MAC_BOARD[6] = {0x10,0xe2,0xd5,0x00,0x00,0x02};
static const uint8_t MAC_PEER[6]  = {0xaa,0xbb,0xcc,0xdd,0xee,0x01};
#define IP_BOARD 0xc0a8012cu  /* 192.168.1.44 */
#define IP_PEER  0xc0a80107u  /* 192.168.1.7  */

static void put16(uint8_t *p, uint16_t v) { p[0] = v >> 8; p[1] = v; }
static void put32(uint8_t *p, uint32_t v)
{
    p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v;
}

// eth(14) + ip(20) + udp(8) + payload, no preamble/crc (HW_PREAMBLE_CRC)
static int build_udp(uint8_t *f, uint32_t dst_ip, uint16_t dst_port,
                     const uint8_t *payload, int plen)
{
    memset(f, 0, 64);
    memcpy(f, MAC_BOARD, 6);          // dst mac (unicast to board)
    memcpy(f + 6, MAC_PEER, 6);       // src mac
    if (dst_ip == 0xffffffffu || (dst_ip & 0xff) == 0xff)
        memset(f, 0xff, 6);           // broadcast frames go to ff:ff:..
    put16(f + 12, 0x0800);            // IPv4
    uint8_t *ip = f + 14;
    ip[0] = 0x45;
    put16(ip + 2, (uint16_t)(20 + 8 + plen));
    ip[8] = 64; ip[9] = 0x11;         // ttl, UDP
    put32(ip + 12, IP_PEER);
    put32(ip + 16, dst_ip);
    uint8_t *udp = f + 34;
    put16(udp, 49152);                // src port
    put16(udp + 2, dst_port);
    put16(udp + 4, (uint16_t)(8 + plen));
    memcpy(f + 42, payload, (size_t)plen);
    int len = 42 + plen;
    return len < 60 ? 60 : len;       // MAC pads runt rx frames on real hw
}

static int build_arp_request(uint8_t *f, uint32_t target_ip)
{
    memset(f, 0, 64);
    memset(f, 0xff, 6);
    memcpy(f + 6, MAC_PEER, 6);
    put16(f + 12, 0x0806);
    uint8_t *a = f + 14;
    put16(a, 1); put16(a + 2, 0x0800); a[4] = 6; a[5] = 4;
    put16(a + 6, 1);                  // request
    memcpy(a + 8, MAC_PEER, 6);
    put32(a + 14, IP_PEER);
    put32(a + 24, target_ip);
    return 60;
}

// ── callback recording ──────────────────────────────────────────────────────
static int cb_hits, bx_hits;
static uint32_t cb_src_ip;
static uint16_t cb_dst_port;

static void cb(uint32_t src_ip, uint16_t src_port, uint16_t dst_port,
               void *data, uint32_t len)
{
    (void)src_port; (void)data; (void)len;
    cb_hits++; cb_src_ip = src_ip; cb_dst_port = dst_port;
}

static void bx(uint32_t src_ip, uint16_t src_port, uint16_t dst_port,
               void *data, uint32_t len)
{
    (void)src_port; (void)data; (void)len;
    bx_hits++; cb_src_ip = src_ip; cb_dst_port = dst_port;
}

static void reset_cbs(void) { cb_hits = bx_hits = 0; cb_src_ip = 0; cb_dst_port = 0; }

// ── auto-responder implementation ───────────────────────────────────────────
static uint16_t csum16(const uint8_t *p, int len)
{
    uint32_t s = 0;
    for (int i = 0; i + 1 < len; i += 2) s += (p[i] << 8) | p[i+1];
    if (len & 1) s += p[len-1] << 8;
    while (s >> 16) s = (s & 0xffff) + (s >> 16);
    return (uint16_t)~s;
}

static void build_and_arm_reply(const uint8_t *tx, uint32_t len)
{
    static uint8_t reply[256];
    (void)len;
    if (responder == RESPOND_ARP && tx[12] == 0x08 && tx[13] == 0x06 &&
        tx[21] == 1 /* request */) {
        memset(reply, 0, sizeof(reply));
        memcpy(reply, tx + 6, 6);         // to the asker
        memcpy(reply + 6, MAC_PEER, 6);
        put16(reply + 12, 0x0806);
        uint8_t *a = reply + 14;
        put16(a, 1); put16(a + 2, 0x0800); a[4] = 6; a[5] = 4;
        put16(a + 6, 2);                  // reply
        memcpy(a + 8, MAC_PEER, 6);
        memcpy(a + 14, tx + 14 + 24, 4);  // sender ip = requested target ip
        memcpy(a + 18, tx + 6, 6);
        memcpy(a + 24, tx + 14 + 14, 4);
        memcpy(mock_ethmac_mem + 2048, reply, 60);  // rx slot 1
        mock_writer_slot = 1;
        mock_writer_length = 60;
        mock_writer_pending = 1;
    }
    if (responder == RESPOND_PONG && tx[12] == 0x08 && tx[13] == 0x00 &&
        tx[23] == 0x01 && tx[34] == 0x08 /* ICMP echo request */) {
        uint32_t ip_total = (tx[16] << 8) | tx[17];
        memset(reply, 0, sizeof(reply));
        memcpy(reply, tx + 6, 6);
        memcpy(reply + 6, MAC_PEER, 6);
        put16(reply + 12, 0x0800);
        memcpy(reply + 14, tx + 14, ip_total);  // copy ip+icmp, then swap
        uint8_t *ip = reply + 14;
        memcpy(ip + 12, tx + 14 + 16, 4);       // src = old dst
        memcpy(ip + 16, tx + 14 + 12, 4);       // dst = old src
        ip[10] = ip[11] = 0;
        put16(ip + 10, csum16(ip, 20));
        uint8_t *icmp = reply + 34;
        icmp[0] = 0x00;                          // echo reply
        icmp[2] = icmp[3] = 0;
        put16(icmp + 2, csum16(icmp, (int)(ip_total - 20)));
        uint32_t flen = 14 + ip_total;
        if (flen < 60) flen = 60;
        memcpy(mock_ethmac_mem + 2048, reply, flen);
        mock_writer_slot = 1;
        mock_writer_length = flen;
        mock_writer_pending = 1;
    }
}

// ── tests ───────────────────────────────────────────────────────────────────
static void test_rx_dispatch_classes(void)  // SWEB-10
{
    uint8_t f[128];
    const uint8_t pay[4] = {1,2,3,4};

    udp_set_callback(cb);
    udp_set_broadcast_callback(bx);
    udp_set_ip(IP_BOARD);

    reset_cbs();
    rx_inject(f, (uint32_t)build_udp(f, IP_BOARD, 1234, pay, 4));
    CHECK(cb_hits == 1 && bx_hits == 0, "unicast to own IP -> rx callback");
    CHECK(cb_src_ip == IP_PEER && cb_dst_port == 1234, "callback args carry src ip + dst port");

    reset_cbs();
    rx_inject(f, (uint32_t)build_udp(f, 0xffffffffu, 1234, pay, 4));
    CHECK(bx_hits == 1 && cb_hits == 0, "limited broadcast -> bx callback");

    reset_cbs();
    rx_inject(f, (uint32_t)build_udp(f, IP_BOARD | 0xff, 1234, pay, 4));
    CHECK(bx_hits == 1 && cb_hits == 0, "subnet broadcast -> bx callback (fork fix)");

    reset_cbs();
    rx_inject(f, (uint32_t)build_udp(f, IP_PEER, 1234, pay, 4));
    CHECK(cb_hits == 0 && bx_hits == 0, "foreign unicast -> no callback");
}

static void test_src_mac_capture_and_peer_reply(void)  // SWEB-8
{
    uint8_t f[128];
    const uint8_t pay[4] = {9,9,9,9};

    udp_set_callback(cb);
    udp_set_ip(IP_BOARD);
    reset_cbs();
    rx_inject(f, (uint32_t)build_udp(f, IP_BOARD, 1234, pay, 4));
    CHECK(memcmp(udp_last_src_mac, MAC_PEER, 6) == 0,
          "requester MAC captured from frame");

    // reply via udp_set_peer: unicast to the captured MAC, no ARP emitted
    tx_reset();
    udp_set_peer(IP_PEER, udp_last_src_mac);
    memcpy(udp_get_tx_buffer(), pay, 4);
    int sent = udp_send(1234, 49152, 4);
    CHECK(sent != 0, "udp_send accepts the peer");
    CHECK(tx_count == 1, "exactly one frame transmitted (no ARP request)");
    CHECK(memcmp(tx_log[0].data, MAC_PEER, 6) == 0,
          "reply dst MAC is the captured requester MAC");
    CHECK(put16 != NULL && tx_log[0].data[12] == 0x08 && tx_log[0].data[13] == 0x00,
          "reply is an IPv4 frame");
}

static void test_tx_min_frame_padding(void)  // SWEB-11
{
    udp_set_ip(IP_BOARD);
    tx_reset();
    udp_announce_arp();
    CHECK(tx_count == 1, "gratuitous ARP transmitted");
    CHECK(tx_log[0].len == 100, "small TX frame padded to 100 bytes");
    int zeros = 1;
    for (uint32_t i = 60; i < tx_log[0].len; i++)
        if (tx_log[0].data[i]) zeros = 0;
    CHECK(zeros, "padding trailer is zeroed");
}

static void test_arp_reply_padded_and_unicast(void)  // SWEB-10/11 corner
{
    uint8_t f[128];
    udp_set_ip(IP_BOARD);

    tx_reset();
    rx_inject(f, (uint32_t)build_arp_request(f, IP_BOARD));
    CHECK(tx_count == 1, "ARP request for own IP answered");
    CHECK(memcmp(tx_log[0].data, MAC_PEER, 6) == 0, "ARP reply unicast to requester");
    CHECK(tx_log[0].len == 100, "ARP reply padded to 100 bytes");

    tx_reset();
    rx_inject(f, (uint32_t)build_arp_request(f, IP_PEER));
    CHECK(tx_count == 0, "ARP request for another host ignored");
}

static void test_getters_and_broadcast_mode(void)
{
    udp_set_ip(IP_BOARD);
    CHECK(udp_get_ip() == IP_BOARD, "udp_get_ip returns what was set");

    tx_reset();
    udp_set_broadcast();
    memcpy(udp_get_tx_buffer(), "abc", 3);      // odd length -> pad branch
    CHECK(udp_send(1, 2, 3) != 0, "broadcast send accepted");
    CHECK(tx_log[0].data[0] == 0xff && tx_log[0].data[5] == 0xff,
          "udp_set_broadcast targets ff:ff:ff:ff:ff:ff");
}

static void test_arp_resolve_roundtrip_and_cache(void)
{
    udp_set_ip(IP_BOARD);
    responder = RESPOND_ARP;
    tx_reset();
    CHECK(udp_arp_refresh(0xc0a80101) == 1, "forced ARP refresh resolves via reply");
    CHECK(tx_count >= 1 && tx_log[0].data[12] == 0x08 && tx_log[0].data[13] == 0x06,
          "refresh emitted an ARP request");

    tx_reset();
    CHECK(udp_arp_resolve(0xc0a80101) == 1, "cached resolve is a no-op hit");
    CHECK(tx_count == 0, "cached resolve emits nothing");

    responder = RESPOND_NONE;
    CHECK(udp_arp_refresh(0xc0a80163) == 0, "unanswered resolve times out with 0");
}

static void test_ping_flow(void)
{
    udp_set_ip(IP_BOARD);
    responder = RESPOND_PONG;
    // resolve target first via ARP responder so send_ping's resolve succeeds
    responder = RESPOND_ARP;
    CHECK(udp_arp_refresh(IP_PEER) == 1, "ping target resolves");
    responder = RESPOND_PONG;
    tx_reset();
    CHECK(send_ping(IP_PEER, 16) == 0, "ping answered by pong");
    responder = RESPOND_NONE;
    CHECK(send_ping(IP_PEER, 16) == -2, "unanswered ping times out");
    CHECK(send_ping(IP_PEER, 60000) == -1, "oversized ping payload rejected");
}

static void test_icmp_echo_served(void)
{
    uint8_t f[256];
    udp_set_ip(IP_BOARD);
    // ICMP echo request to the board
    memset(f, 0, sizeof(f));
    memcpy(f, MAC_BOARD, 6);
    memcpy(f + 6, MAC_PEER, 6);
    put16(f + 12, 0x0800);
    uint8_t *ip = f + 14;
    ip[0] = 0x45;
    put16(ip + 2, 20 + 8 + 4);
    ip[8] = 64; ip[9] = 0x01;
    put32(ip + 12, IP_PEER);
    put32(ip + 16, IP_BOARD);
    uint8_t *icmp = f + 34;
    icmp[0] = 0x08;                     // echo request
    tx_reset();
    rx_inject(f, 60);
    CHECK(tx_count == 1, "ICMP echo request answered");
    CHECK(tx_log[0].data[34] == 0x00, "reply is echo-reply type");
    CHECK(memcmp(tx_log[0].data, MAC_PEER, 6) == 0, "echo reply unicast to pinger");
}

static void test_frame_guards(void)
{
    uint8_t f[128];
    udp_set_ip(IP_BOARD);
    udp_set_callback(cb);
    reset_cbs();

    // oversized rx length claim must be discarded before parsing
    memset(f, 0, 64);
    memcpy(mock_ethmac_mem, f, 64);
    mock_writer_slot = 0;
    mock_writer_length = 4000;          // > ETHMAC_SLOT_SIZE
    mock_writer_pending = 1;
    udp_service();
    CHECK(cb_hits == 0, "oversized frame discarded");

    // malformed ARP (wrong hwtype) ignored
    int len = build_arp_request(f, IP_BOARD);
    f[14] = 0xff;                        // corrupt hwtype
    tx_reset();
    rx_inject(f, (uint32_t)len);
    CHECK(tx_count == 0, "malformed ARP ignored");

    // unicast-to-me with only the broadcast callback installed falls back
    const uint8_t pay[2] = {7, 7};
    udp_set_callback((udp_callback)0);
    udp_set_broadcast_callback(bx);
    reset_cbs();
    rx_inject(f, (uint32_t)build_udp(f, IP_BOARD, 99, pay, 2));
    CHECK(bx_hits == 1, "unicast falls back to bx callback when rx unset");
    udp_set_broadcast_callback((udp_callback)0);
}

static void test_remaining_guards(void)
{
    uint8_t f[128];
    udp_set_ip(IP_BOARD);

    // ARP reply whose sender is not the address being resolved: ignored.
    responder = RESPOND_NONE;
    udp_arp_refresh(0xc0a801fe);        // times out; cache = fe, mac zeroed
    memset(f, 0, 64);
    memcpy(f, MAC_BOARD, 6);
    memcpy(f + 6, MAC_PEER, 6);
    put16(f + 12, 0x0806);
    uint8_t *a = f + 14;
    put16(a, 1); put16(a + 2, 0x0800); a[4] = 6; a[5] = 4;
    put16(a + 6, 2);                    // reply
    memcpy(a + 8, MAC_PEER, 6);
    put32(a + 14, 0xc0a801aa);          // sender != cached_ip
    tx_reset();
    rx_inject(f, 60);
    CHECK(tx_count == 0, "foreign ARP reply ignored");

    // udp_send with an unresolved (zero-MAC) peer must refuse.
    CHECK(udp_send(1, 2, 4) == 0, "send with unresolved peer refused");

    // udp_send beyond max payload must refuse.
    responder = RESPOND_ARP;
    CHECK(udp_arp_refresh(IP_PEER) == 1, "re-resolve peer");
    responder = RESPOND_NONE;
    CHECK(udp_send(1, 2, 2100) == 0, "oversized udp_send refused");

    // resolve same IP again while its MAC is zeroed: cache-hit path falls
    // through to a fresh request (then times out).
    udp_arp_refresh(0xc0a801fe);
    CHECK(udp_arp_resolve(0xc0a801fe) == 0, "cached-ip zero-mac resolve re-asks");

    // send_ping to an unresolvable address: ARP-failed branch.
    CHECK(send_ping(0xc0a801fd, 8) == -1, "ping ARP failure returns -1");

    // ICMP guards: runt frame, bad total_length, total_length > rxlen.
    memset(f, 0, 64);
    memcpy(f, MAC_BOARD, 6);
    memcpy(f + 6, MAC_PEER, 6);
    put16(f + 12, 0x0800);
    uint8_t *ip = f + 14;
    ip[0] = 0x45; ip[9] = 0x01;
    put32(ip + 12, IP_PEER); put32(ip + 16, IP_BOARD);
    tx_reset();
    put16(ip + 2, 20);                  // total_length < sizeof(icmp_frame)
    rx_inject(f, 60);
    put16(ip + 2, 200);                 // total_length > rxlen
    rx_inject(f, 60);
    rx_inject(f, 40);                   // rxlen below eth+icmp minimum
    CHECK(tx_count == 0, "malformed ICMP frames all discarded");

    // unsolicited pongs: wrong sequence, then right sequence wrong id.
    // udp.c writes/compares these fields RAW (no htons), so the "right"
    // values are host-order bytes: seq == ping_seq_number (2 pings built).
    put16(ip + 2, 20 + 8);
    f[34] = 0x00;                       // echo reply
    put16(f + 34 + 6, 0x7777);          // bogus sequence
    rx_inject(f, 60);
    CHECK(tx_count == 0, "pong with wrong sequence ignored");
    f[34 + 6] = 2; f[34 + 7] = 0;       // seq = 2, raw host order
    f[34 + 4] = 0x99; f[34 + 5] = 0x99; // bogus identifier
    rx_inject(f, 60);
    CHECK(tx_count == 0, "pong with wrong identifier ignored");

    // gratuitous announce refuses to fire without a real address
    tx_reset();
    udp_set_ip(0);
    udp_announce_arp();
    udp_set_ip(0xffffffffu);
    udp_announce_arp();
    CHECK(tx_count == 0, "announce suppressed while unleased");
    udp_set_ip(IP_BOARD);

    // non-IPv4 version byte dropped before proto dispatch
    udp_set_callback(cb);
    reset_cbs();
    int len = build_udp(f, IP_BOARD, 5, (const uint8_t *)"x", 1);
    f[14] = 0x55;                       // version nibble wrong
    rx_inject(f, (uint32_t)len);
    CHECK(cb_hits == 0, "non-IPv4 frame dropped");
}

int main(void)
{
    udp_start(MAC_BOARD, 0);
    eth_init();                          // banner + no-PHY-reset path

    test_rx_dispatch_classes();
    test_src_mac_capture_and_peer_reply();
    test_tx_min_frame_padding();
    test_arp_reply_padded_and_unicast();
    test_getters_and_broadcast_mode();
    test_arp_resolve_roundtrip_and_cache();
    test_ping_flow();
    test_icmp_echo_served();
    test_frame_guards();
    test_remaining_guards();

    printf("%s: %d tests, %d failures\n", fails ? "FAIL" : "ALL PASS", tests, fails);
    return fails ? 1 : 0;
}
