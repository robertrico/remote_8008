// eb_serve.c -- the software Etherbone serve glue between udp.c and
// eb8008.c: stash one request from callback context, answer it from
// main-loop context. Split out of main.c so it is host-testable
// (test_eb_serve_host.c, VPLAN SWEB-7/SWEB-8).
//
// One request in flight at a time: litex's CommUDP is strictly
// request/response, and callback context can't ARP-resolve (that would nest
// udp_service), so the callback only stashes and eb_serve_pending() replies.

#include <stdint.h>
#include <string.h>

#include <libliteeth/udp.h>

#include "eb8008.h"
#include "eb_serve.h"

// fork-only udp.c exports
void udp_set_peer(uint32_t ip, const uint8_t *mac);
extern uint8_t udp_last_src_mac[6];

static volatile int eb_pending;
static uint32_t eb_src_ip;
static uint16_t eb_src_port;
static uint16_t eb_dst_port;
static uint8_t  eb_src_mac[6];
static uint8_t  eb_req[EB_BUF_LEN];
static int      eb_req_len;
static uint8_t  eb_resp[EB_BUF_LEN + 16];

volatile uint32_t eb_cb_any, eb_cb_port, eb_served;

void eb_rx_callback(uint32_t src_ip, uint16_t src_port,
                    uint16_t dst_port, void *data, uint32_t length)
{
    eb_cb_any++;
    // No dst_port filter: mesh routers can rewrite ports in transit
    // (S-WIRE-2b), so the Etherbone magic check below is the real gate.
    if (eb_pending)
        return;
    if (length >= 2 && (((const uint8_t *)data)[0] != 0x4e ||
                        ((const uint8_t *)data)[1] != 0x6f))
        return; // not Etherbone
    if (length > sizeof(eb_req))
        return;
    eb_cb_port++;
    eb_dst_port = dst_port;
    memcpy(eb_src_mac, (const void *)udp_last_src_mac, 6);
    memcpy(eb_req, data, length);
    eb_req_len  = (int)length;
    eb_src_ip   = src_ip;
    eb_src_port = src_port;
    eb_pending  = 1;
}

void eb_serve_pending(void)
{
    if (!eb_pending)
        return;

    int resp_len = eb8008_handle(eb_req, eb_req_len, eb_resp);
    if (resp_len) {
        // Unicast the reply straight to the requester's captured MAC/IP.
        // No ARP resolve: that round-trip would depend on the client->board
        // unicast direction, which the mesh drops (S-WIRE-2b). Board->client
        // unicast is the proven-good direction.
        udp_set_peer(eb_src_ip, eb_src_mac);
        memcpy(udp_get_tx_buffer(), eb_resp, (size_t)resp_len);
        // Mirror the ports the request arrived with -- a NATing mesh maps
        // the reply back to the client only if they match.
        udp_send(eb_dst_port, eb_src_port, (uint32_t)resp_len);
        eb_served++;
    }
    eb_pending = 0;
}
