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

#include "dhcp8008.h"

// The Etherbone core's identity (see caveat 1 in dhcp8008.c): this is what
// goes in the DHCP chaddr/client-id fields, and it's the address the leased
// IP is ultimately *for*.
static const uint8_t chaddr_etherbone[6] = {0x10, 0xe2, 0xd5, 0x00, 0x00, 0x01};

// The CPU's own ethmac interface: this is what actually goes in the
// Ethernet frames' source MAC field (udp_start()'s macaddr argument).
static const uint8_t mac_ethmac[6] = {0x10, 0xe2, 0xd5, 0x00, 0x00, 0x02};

#define DHCP_CLIENT_PORT 68
#define DHCP_SERVER_PORT 67
#define DHCP_MAX_TRIES   5

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

static int wait_for_reply(void)
{
    rx_got = 0;
    for (int i = 0; i < DHCP_REPLY_TIMEOUT_LOOPS; i++) {
        udp_service();
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

    int leased = 0;
    uint32_t ip = 0, lease_secs = 0, elapsed = 0;

    for (;;) {
        if (!leased || elapsed >= lease_secs / 2) {
            printf(leased ? "dhcp: re-acquiring (fresh DISCOVER)...\n"
                           : "dhcp: acquiring...\n");
            if (dhcp_run(&ip, &lease_secs)) {
                eb_ip_ip_write(ip);
                leased  = 1;
                elapsed = 0;
                print_ip("dhcp: leased ", ip);
                printf(", lease %u s\n", (unsigned)lease_secs);
            } else {
                printf("dhcp: no reply after %d tries, retrying in 1s\n",
                       DHCP_MAX_TRIES);
            }
        }
        busy_wait(1000);
        elapsed++;
    }
}
