// test_dhcp_host.c
//
// Host-side unit test for the DHCP packet builder/parser (dhcp8008.c).
// Runs on the dev machine with plain cc -- no LiteX headers, no target
// toolchain:
//
//   cc -o /tmp/t firmware/dhcp8008.c firmware/test_dhcp_host.c && /tmp/t
//
// Exercises the wire format directly (byte offsets / option TLVs) rather
// than trusting the builder's own helpers, so it actually catches framing
// mistakes instead of just echoing them back.

#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "dhcp8008.h"

static const uint8_t chaddr[6] = {0x10, 0xe2, 0xd5, 0x00, 0x00, 0x01};

// Scan the options area of a built/canned packet for `code`. Returns 1 and
// sets *data/*olen on a hit, 0 if not found (including malformed options).
static int find_option(const uint8_t *buf, int len, uint8_t code,
                        const uint8_t **data, int *olen)
{
    int off = DHCP_OPTIONS_OFFSET;
    while (off < len) {
        uint8_t c = buf[off++];
        if (c == DHCP_OPT_END)
            return 0;
        if (c == DHCP_OPT_PAD)
            continue;
        if (off >= len)
            return 0;
        uint8_t l = buf[off++];
        if (off + l > len)
            return 0;
        if (c == code) {
            *data = buf + off;
            *olen = l;
            return 1;
        }
        off += l;
    }
    return 0;
}

static int has_end_option(const uint8_t *buf, int len)
{
    int off = DHCP_OPTIONS_OFFSET;
    while (off < len) {
        uint8_t c = buf[off++];
        if (c == DHCP_OPT_END)
            return 1;
        if (c == DHCP_OPT_PAD)
            continue;
        if (off >= len)
            return 0;
        uint8_t l = buf[off++];
        if (off + l > len)
            return 0;
        off += l;
    }
    return 0;
}

static void test_discover(void)
{
    uint8_t buf[DHCP_MIN_PACKET_LEN + 32];
    uint32_t xid = 0xdeadbeef;

    int n = dhcp_build_discover(buf, chaddr, xid);
    assert(n >= DHCP_OPTIONS_OFFSET);

    assert(buf[0] == DHCP_OP_BOOTREQUEST);
    assert(buf[1] == DHCP_HTYPE_ETHERNET);
    assert(buf[2] == DHCP_HLEN_ETHERNET);

    uint32_t got_xid = ((uint32_t)buf[4] << 24) | ((uint32_t)buf[5] << 16) |
                        ((uint32_t)buf[6] << 8) | buf[7];
    assert(got_xid == xid);

    uint16_t flags = ((uint16_t)buf[10] << 8) | buf[11];
    assert(flags == 0x8000);

    // chaddr field starts at byte 28, first 6 bytes are the hardware address.
    assert(memcmp(buf + 28, chaddr, 6) == 0);

    uint32_t cookie = ((uint32_t)buf[236] << 24) | ((uint32_t)buf[237] << 16) |
                       ((uint32_t)buf[238] << 8) | buf[239];
    assert(cookie == DHCP_MAGIC_COOKIE);

    const uint8_t *data;
    int olen;

    assert(find_option(buf, n, DHCP_OPT_MSG_TYPE, &data, &olen));
    assert(olen == 1 && data[0] == DHCP_MSG_DISCOVER);

    assert(find_option(buf, n, DHCP_OPT_HOSTNAME, &data, &olen));
    assert(olen == 5);
    assert(memcmp(data, "b8008", 5) == 0);

    assert(find_option(buf, n, DHCP_OPT_CLIENT_ID, &data, &olen));
    assert(olen == 7);
    assert(data[0] == DHCP_HTYPE_ETHERNET);
    assert(memcmp(data + 1, chaddr, 6) == 0);

    assert(find_option(buf, n, DHCP_OPT_PARAM_REQ_LIST, &data, &olen));
    assert(olen == 3 && data[0] == 1 && data[1] == 3 && data[2] == 6);

    assert(has_end_option(buf, n));

    printf("test_discover: PASS\n");
}

static void test_request(void)
{
    uint8_t buf[DHCP_MIN_PACKET_LEN + 32];
    uint32_t xid = 0x12345678;
    uint32_t requested_ip = (192u << 24) | (168u << 16) | (1u << 8) | 50u;
    uint32_t server_id    = (192u << 24) | (168u << 16) | (1u << 8) | 1u;

    int n = dhcp_build_request(buf, chaddr, xid, requested_ip, server_id);
    assert(n >= DHCP_OPTIONS_OFFSET);

    uint16_t flags = ((uint16_t)buf[10] << 8) | buf[11];
    assert(flags == 0x8000);
    assert(memcmp(buf + 28, chaddr, 6) == 0);

    const uint8_t *data;
    int olen;

    assert(find_option(buf, n, DHCP_OPT_MSG_TYPE, &data, &olen));
    assert(olen == 1 && data[0] == DHCP_MSG_REQUEST);

    assert(find_option(buf, n, DHCP_OPT_REQUESTED_IP, &data, &olen));
    assert(olen == 4);
    uint32_t got = ((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) |
                   ((uint32_t)data[2] << 8) | data[3];
    assert(got == requested_ip);

    assert(find_option(buf, n, DHCP_OPT_SERVER_ID, &data, &olen));
    assert(olen == 4);
    got = ((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) |
          ((uint32_t)data[2] << 8) | data[3];
    assert(got == server_id);

    assert(has_end_option(buf, n));

    printf("test_request: PASS\n");
}

// Hand-build a canned DHCPOFFER (server 192.168.1.1 offers 192.168.1.50,
// lease 86400s) with a given xid, mimicking what an off-the-shelf DHCP
// server would send. msg_type_off receives the byte offset of the option-53
// value byte so test_parse_ack can flip DHCPOFFER -> DHCPACK in place.
static void build_canned_offer(uint8_t *buf, int *outlen, uint32_t xid,
                                int *msg_type_off)
{
    memset(buf, 0, DHCP_MIN_PACKET_LEN);
    buf[0] = DHCP_OP_BOOTREPLY;
    buf[1] = DHCP_HTYPE_ETHERNET;
    buf[2] = DHCP_HLEN_ETHERNET;
    buf[3] = 0; // hops
    buf[4] = (uint8_t)(xid >> 24);
    buf[5] = (uint8_t)(xid >> 16);
    buf[6] = (uint8_t)(xid >> 8);
    buf[7] = (uint8_t)xid;
    // secs/flags left 0.
    buf[16] = 192; buf[17] = 168; buf[18] = 1; buf[19] = 50;  // yiaddr
    buf[20] = 192; buf[21] = 168; buf[22] = 1; buf[23] = 1;   // siaddr
    memcpy(buf + 28, chaddr, 6);
    buf[236] = 0x63; buf[237] = 0x82; buf[238] = 0x53; buf[239] = 0x63;

    int off = DHCP_OPTIONS_OFFSET;
    buf[off++] = DHCP_OPT_MSG_TYPE; buf[off++] = 1;
    if (msg_type_off)
        *msg_type_off = off;
    buf[off++] = DHCP_MSG_OFFER;

    buf[off++] = DHCP_OPT_LEASE_TIME; buf[off++] = 4;
    uint32_t lease = 86400;
    buf[off++] = (uint8_t)(lease >> 24);
    buf[off++] = (uint8_t)(lease >> 16);
    buf[off++] = (uint8_t)(lease >> 8);
    buf[off++] = (uint8_t)lease;

    buf[off++] = DHCP_OPT_SERVER_ID; buf[off++] = 4;
    buf[off++] = 192; buf[off++] = 168; buf[off++] = 1; buf[off++] = 1;

    buf[off++] = DHCP_OPT_END;

    *outlen = off;
}

static void test_parse_offer(void)
{
    uint8_t buf[DHCP_MIN_PACKET_LEN];
    int len;
    uint32_t xid = 0xcafef00d;

    build_canned_offer(buf, &len, xid, NULL);

    uint32_t ip = 0, server = 0, lease = 0;
    int ok = dhcp_parse_offer(buf, len, xid, &ip, &server, &lease);
    assert(ok);
    assert(ip == ((192u << 24) | (168u << 16) | (1u << 8) | 50u));
    assert(server == ((192u << 24) | (168u << 16) | (1u << 8) | 1u));
    assert(lease == 86400);

    // Wrong xid must be rejected (stale/foreign reply).
    ok = dhcp_parse_offer(buf, len, xid ^ 1, &ip, &server, &lease);
    assert(!ok);

    // Truncated packet must be rejected, not read out of bounds.
    ok = dhcp_parse_offer(buf, DHCP_OPTIONS_OFFSET - 1, xid, &ip, &server, &lease);
    assert(!ok);

    printf("test_parse_offer: PASS\n");
}

static void test_parse_ack(void)
{
    uint8_t buf[DHCP_MIN_PACKET_LEN];
    int len, msg_type_off;
    uint32_t xid = 0x11223344;

    build_canned_offer(buf, &len, xid, &msg_type_off);
    buf[msg_type_off] = DHCP_MSG_ACK;

    uint32_t ip = 0, lease = 0;
    int ok = dhcp_parse_ack(buf, len, xid, &ip, &lease);
    assert(ok);
    assert(ip == ((192u << 24) | (168u << 16) | (1u << 8) | 50u));
    assert(lease == 86400);

    // An actual OFFER must not parse as an ACK.
    build_canned_offer(buf, &len, xid, NULL);
    ok = dhcp_parse_ack(buf, len, xid, &ip, &lease);
    assert(!ok);

    printf("test_parse_ack: PASS\n");
}

int main(void)
{
    test_discover();
    test_request();
    test_parse_offer();
    test_parse_ack();
    printf("ALL PASS\n");
    return 0;
}
