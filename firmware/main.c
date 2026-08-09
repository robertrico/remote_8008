// main.c -- b8008_net firmware: DHCP client that leases an IP on behalf of
// the Etherbone core and republishes it into the eb_ip CSR.
//
// This replaces the LiteX BIOS in integrated ROM. There is no boot menu, no
// serial console, no bootloader -- just: bring the ethmac up, run DHCP
// against the Etherbone identity, write the leased address into eb_ip, and
// repeat forever (see dhcp8008.c's header comment for what "repeat" means
// and why).
//
// DHCP wire format (BOOTP header + options) lives entirely in dhcp8008.c;
// this file only owns framing (libliteeth's microudp) and policy (retry
// counts, timeouts, the eb_ip_ip_write() handoff).

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <irq.h>
#include <system.h>

#include <generated/csr.h>
#include <generated/soc.h>

#include <libbase/uart.h>
#include <libliteeth/udp.h>
#include <libliteeth/mdio.h>

#include "dhcp8008.h"
#include "eb8008.h"

// The Etherbone core's identity (see caveat 1 in dhcp8008.c): this is what
// goes in the DHCP chaddr/client-id fields, and it's the address the leased
// IP is ultimately *for*.
// Bring-up experiment: Raspberry Pi OUI (B8:27:EB) instead of the project's
// 10:E2:D5 -- testing whether the MR60 drops WiFi->wired unicast based on
// an OUI it doesn't recognize.
static const uint8_t chaddr_etherbone[6] = {0xb8, 0x27, 0xeb, 0x00, 0x80, 0x09};

// The CPU's own ethmac interface: this is what actually goes in the
// Ethernet frames' source MAC field (udp_start()'s macaddr argument).
static const uint8_t mac_ethmac[6] = {0xb8, 0x27, 0xeb, 0x00, 0x80, 0x08};

#define DHCP_CLIENT_PORT 68
#define DHCP_SERVER_PORT 67
#define DHCP_MAX_TRIES   5

#define ETHERBONE_PORT   1234

// udp_service() poll budget per DISCOVER/REQUEST wait. There's no hardware
// timer wired for a real deadline here, so this mirrors the same
// spin-and-count timeout libliteeth's own udp_arp_resolve() uses (100000
// iterations) -- generous enough that on a live network the reply arrives
// almost immediately, and bounded so a silent network doesn't hang forever.
#define DHCP_REPLY_TIMEOUT_LOOPS 200000

static uint32_t next_xid(void)
{
    // No hardware RNG/uptime counter on this SoC (see build notes) -- an
    // incrementing LCG is enough to keep DISCOVER/REQUEST/retry rounds from
    // colliding with each other or with a stale reply from a previous boot.
    static uint32_t x = 0x8008a5a5u;
    x = x * 1103515245u + 12345u;
    return x;
}

static volatile int rx_got;
static uint8_t rx_msgbuf[DHCP_MIN_PACKET_LEN + 64];
static int rx_msglen;

// ── software Etherbone server state ─────────────────────────────────────────
// One request in flight at a time -- litex's CommUDP is strictly
// request/response, and the callback context can't ARP-resolve (that would
// nest udp_service), so the callback just stashes the request and the main
// serve loop replies.
#define EB_BUF_LEN 1500
static volatile int eb_pending;
static uint32_t eb_src_ip;
static uint16_t eb_src_port;
static uint16_t eb_dst_port;
static uint8_t  eb_src_mac[6];
static uint8_t  eb_req[EB_BUF_LEN];
static int      eb_req_len;
static uint8_t  eb_resp[EB_BUF_LEN + 16];

static void dhcp_rx_callback(uint32_t src_ip, uint16_t src_port,
                              uint16_t dst_port, void *data, uint32_t length)
{
    (void)src_ip;
    if (dst_port != DHCP_CLIENT_PORT || src_port != DHCP_SERVER_PORT)
        return;
    if (length > sizeof(rx_msgbuf))
        length = sizeof(rx_msgbuf);
    memcpy(rx_msgbuf, data, length);
    rx_msglen = (int)length;
    rx_got = 1;
}

static volatile uint32_t eb_cb_any, eb_cb_port, eb_served, eb_resolve_fail;

// Bring-up counters + gratuitous-ARP announce from the local udp.c fork.
extern uint32_t dbg_rx_frames, dbg_rx_arp, dbg_rx_ip, dbg_rx_short;
extern uint32_t dbg_ip_tome, dbg_ip_bcast, dbg_ip_other;
void udp_announce_arp(void);
int udp_arp_refresh(uint32_t ip);
void udp_set_peer(uint32_t ip, const uint8_t *mac);
extern uint8_t udp_last_src_mac[6];

static void eb_rx_callback(uint32_t src_ip, uint16_t src_port,
                            uint16_t dst_port, void *data, uint32_t length)
{
    eb_cb_any++;
    // No dst_port filter: mesh routers can rewrite ports in transit, so the
    // Etherbone magic check below is the real gate.
    if (eb_pending)
        return;
    if (length >= 2 && (((const uint8_t *)data)[0] != 0x4e ||
                        ((const uint8_t *)data)[1] != 0x6f))
        return; // not Etherbone
    eb_cb_port++;
    eb_dst_port = dst_port;
    memcpy(eb_src_mac, (const void *)udp_last_src_mac, 6);
    if (length > sizeof(eb_req))
        return;
    memcpy(eb_req, data, length);
    eb_req_len  = (int)length;
    eb_src_ip   = src_ip;
    eb_src_port = src_port;
    eb_pending  = 1;
}

// Reply to the stashed Etherbone request (main-loop context: ARP resolve of
// the requester is safe here). The single-entry ARP cache in libliteeth's
// udp.c persists across requests, so the resolve only round-trips when the
// requester changes.
static void eb_serve_pending(void)
{
    static uint32_t eb_resolved_ip;

    if (!eb_pending)
        return;

    int resp_len = eb8008_handle(eb_req, eb_req_len, eb_resp);
    if (resp_len) {
        // Unicast the reply straight to the requester's captured MAC/IP.
        // No ARP resolve: that round-trip would depend on the client->board
        // unicast direction, which the mesh drops. Board->client unicast is
        // the proven-good direction.
        (void)eb_resolved_ip;
        udp_set_peer(eb_src_ip, eb_src_mac);
        memcpy(udp_get_tx_buffer(), eb_resp, (size_t)resp_len);
        // Mirror the ports the request arrived with -- a NATing mesh maps
        // the reply back to the client only if they match.
        udp_send(eb_dst_port, eb_src_port, (uint32_t)resp_len);
        eb_served++;
    }
    eb_pending = 0;
}

// Total frames seen at the MAC's RX slot interface (approximate: sampled as
// writer ev_pending just before udp_service consumes it). Distinguishes
// "RX path dead" from "frames arrive, none is our DHCP reply".
static unsigned long rx_frames;

static void service_and_count(void)
{
    if (ethmac_sram_writer_ev_pending_read())
        rx_frames++;
    udp_service();
}

static int wait_for_reply(void)
{
    rx_got = 0;
    for (int i = 0; i < DHCP_REPLY_TIMEOUT_LOOPS; i++) {
        service_and_count();
        if (rx_got)
            return 1;
    }
    return 0;
}

// Runs one full DISCOVER->OFFER->REQUEST->ACK cycle, retrying up to
// DHCP_MAX_TRIES times. Returns 1 and fills *out_ip/*out_lease_secs on
// success.
static int dhcp_run(uint32_t *out_ip, uint32_t *out_lease_secs)
{
    udp_set_callback(dhcp_rx_callback);

    for (int attempt = 0; attempt < DHCP_MAX_TRIES; attempt++) {
        uint32_t xid = next_xid();
        uint32_t offered_ip, offered_server, offered_lease;
        uint32_t acked_ip, acked_lease;
        int len;

        // Send with the spec-correct unleased-client source address
        // (0.0.0.0), then switch the RX filter to accept the server's
        // broadcast reply (destination 255.255.255.255): libliteeth's
        // udp.c (see firmware/Makefile for why it's built locally with
        // ETH_UDP_BROADCAST) filters incoming frames strictly on
        // dst_ip == the address passed to udp_set_ip() -- its broadcast
        // handling is TX-side only (udp_set_broadcast() picks the
        // destination MAC/IP for udp_send()), the RX path has no broadcast
        // exception at all, so the filter has to be driven by hand to match
        // whichever address the packet in flight actually carries.
        udp_set_ip(0);
        len = dhcp_build_discover(udp_get_tx_buffer(), chaddr_etherbone, xid);
        if (!udp_send(DHCP_CLIENT_PORT, DHCP_SERVER_PORT, (uint32_t)len))
            continue;
        udp_set_ip(0xFFFFFFFFu);

        if (!wait_for_reply())
            continue;
        if (!dhcp_parse_offer(rx_msgbuf, rx_msglen, xid, &offered_ip,
                               &offered_server, &offered_lease))
            continue;

        udp_set_ip(0);
        len = dhcp_build_request(udp_get_tx_buffer(), chaddr_etherbone, xid,
                                  offered_ip, offered_server);
        if (!udp_send(DHCP_CLIENT_PORT, DHCP_SERVER_PORT, (uint32_t)len))
            continue;
        udp_set_ip(0xFFFFFFFFu);

        if (!wait_for_reply())
            continue;
        if (!dhcp_parse_ack(rx_msgbuf, rx_msglen, xid, &acked_ip, &acked_lease))
            continue;
        if (acked_ip != offered_ip)
            continue; // NAK'd or reoffered a different address; retry clean

        *out_ip = acked_ip;
        *out_lease_secs = acked_lease ? acked_lease : 3600;
        udp_set_callback((udp_callback)0);
        return 1;
    }

    udp_set_callback((udp_callback)0);
    return 0;
}

// ── PHY diagnostics (bring-up) ──────────────────────────────────────────────
// The RGMII gateware is gigabit-only: a link that negotiated 10/100 passes
// frames through a 125 MHz MAC that mangles them, which looks exactly like
// "dhcp: no reply" with a dead tcpdump. Scan the MDIO bus once for the PHY,
// then report link/speed from the Marvell copper-status register each retry.
static int phy_addr = -1;

// Paged MDIO read (Marvell): page select lives in reg 22.
static int mdio_read_paged(int addr, int page, int reg)
{
    int val;
    mdio_write(addr, 22, page);
    val = mdio_read(addr, reg);
    mdio_write(addr, 22, 0);
    return val;
}

static void phy_scan(void)
{
    for (int a = 0; a < 32; a++) {
        int id1 = mdio_read(a, 2); // PHY identifier 1
        if (id1 != 0xffff && id1 != 0x0000) {
            printf("phy: addr %d id %04x:%04x\n", a, id1, mdio_read(a, 3));
            if (phy_addr < 0)
                phy_addr = a;
        }
    }
    if (phy_addr < 0) {
        printf("phy: no PHY found on MDIO bus!\n");
        return;
    }
    // 88E1512 MAC-specific control 2 (page 2, reg 21): bit 5 = RGMII RX
    // internal delay, bit 4 = RGMII TX internal delay. Tells us which side of
    // the 2 ns skew the PHY already provides vs. what the FPGA must add.
    int mscr2 = mdio_read_paged(phy_addr, 2, 21);
    printf("phy: mscr2 %04x rgmii-rx-dly %d rgmii-tx-dly %d\n",
           mscr2, !!(mscr2 & 0x0020), !!(mscr2 & 0x0010));
}

static void phy_report(void)
{
    static const char *speeds[4] = {"10", "100", "1000", "?"};
    if (phy_addr < 0)
        return;
    int bmsr = mdio_read(phy_addr, 1);  // latched link (bit 2), aneg done (bit 5)
    int stat = mdio_read(phy_addr, 17); // Marvell copper specific status
    printf("phy: bmsr %04x link %d aneg %d | speed %s duplex %s rt-link %d\n",
           bmsr, !!(bmsr & 0x0004), !!(bmsr & 0x0020),
           speeds[(stat >> 14) & 3], (stat & 0x2000) ? "full" : "half",
           !!(stat & 0x0400));
    // RX-path truth: broadcast chatter (ARP etc.) hits the MAC constantly on
    // any live LAN, so preamble/crc counters moving = frames reach the MAC;
    // crc rising = they arrive corrupt (RGMII RX timing); all-zero = RX dead.
    printf("mac: rx frames %lu preamble-err %lu crc-err %lu writer-err %lu\n",
           rx_frames,
           (unsigned long)ethmac_rx_datapath_preamble_errors_read(),
           (unsigned long)ethmac_rx_datapath_crc_errors_read(),
           (unsigned long)ethmac_sram_writer_errors_read());
    // TX side: reader ready=1 + level=0 after sends means the MAC accepted
    // and drained every TX frame (frames left the MAC; problem is at/after
    // RGMII). ready=0 or level piling up = TX datapath wedged in the FPGA.
    printf("mac: tx ready %d level %lu done-pending %d\n",
           (int)ethmac_sram_reader_ready_read(),
           (unsigned long)ethmac_sram_reader_level_read(),
           (int)ethmac_sram_reader_ev_pending_read());
}

static void print_ip(const char *prefix, uint32_t ip)
{
    printf("%s%d.%d.%d.%d", prefix, (int)((ip >> 24) & 0xff),
           (int)((ip >> 16) & 0xff), (int)((ip >> 8) & 0xff), (int)(ip & 0xff));
}

int main(void)
{
    irq_setmask(0);
    irq_setie(1);
#ifdef CSR_UART_BASE
    uart_init();
#endif

    printf("\nb8008_net firmware: DHCP client (hostname \"" DHCP_HOSTNAME "\")\n");

    eth_init();
    udp_start(mac_ethmac, 0);
    udp_set_broadcast();

    phy_scan();
    phy_report();

    // Bring-up TX self-test: PHY internal loopback (reg 0 bit 14, speed
    // forced to 1000/full, autoneg off). TX frames fold back into RX inside
    // the 88E1512, so "sent N, got back M" isolates the FPGA->PHY TX leg
    // from everything beyond the PHY.
    if (phy_addr >= 0) {
        unsigned long before = rx_frames;
        mdio_write(phy_addr, 0, 0x4140); // loopback | 1000 | full, aneg off
        busy_wait(500);
        for (int i = 0; i < 3; i++) {
            int len = dhcp_build_discover(udp_get_tx_buffer(),
                                          chaddr_etherbone, next_xid());
            udp_set_ip(0);
            udp_send(DHCP_CLIENT_PORT, DHCP_SERVER_PORT, (uint32_t)len);
            for (int j = 0; j < 20000; j++)
                service_and_count();
        }
        printf("loopback: sent 3, saw %lu frames back\n", rx_frames - before);
        mdio_write(phy_addr, 0, 0x1340); // aneg on + restart
    }

    uint32_t ip = 0, lease_secs = 0;

    for (;;) {
        // Acquire (or re-acquire) a lease. dhcp_run drives the RX filter
        // itself, but the *remote* endpoint may still point at the last
        // Etherbone client after a serve period -- force broadcast back on
        // for the DISCOVER/REQUEST exchange.
        udp_set_broadcast();
        printf("dhcp: acquiring...\n");
        while (!dhcp_run(&ip, &lease_secs)) {
            printf("dhcp: no reply after %d tries, retrying in 1s\n",
                   DHCP_MAX_TRIES);
            phy_report();
            busy_wait(1000);
        }
        eb_ip_ip_write(ip);
        print_ip("dhcp: leased ", ip);
        printf(", lease %u s\n", (unsigned)lease_secs);

        // Serve software Etherbone on the leased address until the lease is
        // half way through, then re-acquire (spec-ish renewal without a
        // wall clock). EB_SERVE_LOOPS_PER_SEC is a rough calibration of the
        // service loop -- renewal timing only needs to be right within a
        // factor of a few on an 86400 s lease.
        #define EB_SERVE_LOOPS_PER_SEC 400000u
        udp_set_ip(ip);
        udp_set_callback(eb_rx_callback);
        // Broadcast requests land here too: the client broadcasts because
        // the mesh drops client->board unicast, and broadcast is the proven
        // delivery path in that direction.
        udp_set_broadcast_callback(eb_rx_callback);
        udp_announce_arp(); // repopulate the LAN's ARP tables immediately
        printf("etherbone: serving on UDP %d\n", ETHERBONE_PORT);
        for (uint32_t s = 0; s < lease_secs / 2; s++) {
            if ((s % 30) == 0)
                udp_announce_arp(); // periodic refresh
            // ARP the gateway every ~10 ticks: the request repopulates the
            // router's table with our mapping (see udp_arp_refresh) after
            // its negative-cache backoff. Assumes .1 on our /24.
            if ((s % 10) == 1) {
                uint32_t gw = (ip & 0xffffff00u) | 1u;
                if (!udp_arp_refresh(gw))
                    printf("arp: gateway refresh timeout\n");
            }
            for (uint32_t i = 0; i < EB_SERVE_LOOPS_PER_SEC; i++) {
                service_and_count(); // udp_service + rx_frames sampling
                eb_serve_pending();
            }
        }
        udp_set_callback((udp_callback)0);
        printf("dhcp: lease half-life reached, renewing\n");
    }
}
