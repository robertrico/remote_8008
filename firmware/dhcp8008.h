// dhcp8008.h
//
// Minimal DHCPv4 (RFC 2131/2132) packet builder/parser for the b8008_net
// firmware. Deliberately dumb: it only touches the BOOTP+options payload
// bytes handed to it -- no Ethernet/IP/UDP framing, no sockets, no state.
// That keeps it host-testable with a plain `cc` (see test_dhcp_host.c) and
// lets main.c own all the framing/timing/retry policy.

#ifndef B8008_NET_DHCP8008_H
#define B8008_NET_DHCP8008_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DHCP_HOSTNAME "b8008"

#define DHCP_MAGIC_COOKIE 0x63825363u

#define DHCP_OP_BOOTREQUEST 1
#define DHCP_OP_BOOTREPLY   2

#define DHCP_HTYPE_ETHERNET 1
#define DHCP_HLEN_ETHERNET  6

#define DHCP_MSG_DISCOVER 1
#define DHCP_MSG_OFFER    2
#define DHCP_MSG_REQUEST  3
#define DHCP_MSG_DECLINE  4
#define DHCP_MSG_ACK      5
#define DHCP_MSG_NAK      6
#define DHCP_MSG_RELEASE  7
#define DHCP_MSG_INFORM   8

#define DHCP_OPT_PAD            0
#define DHCP_OPT_REQUESTED_IP   50
#define DHCP_OPT_LEASE_TIME     51
#define DHCP_OPT_MSG_TYPE       53
#define DHCP_OPT_SERVER_ID      54
#define DHCP_OPT_PARAM_REQ_LIST 55
#define DHCP_OPT_HOSTNAME       12
#define DHCP_OPT_CLIENT_ID      61
#define DHCP_OPT_END            255

// BOOTP fixed part (op..file) is 236 bytes; +4 bytes magic cookie = 240
// before the first option.
#define DHCP_BOOTP_FIXED_LEN 236
#define DHCP_OPTIONS_OFFSET  240

// Conventional minimum DHCP message size (236 + 64 sname/128 file already
// included above; this is the historical BOOTP-relay-friendly padded total,
// same convention dhclient/udhcp use). Not required by RFC 2131 for a
// directly-attached client, but cheap and avoids surprises on older gear.
#define DHCP_MIN_PACKET_LEN 300

// Caller-provided buffer must have room for at least DHCP_MIN_PACKET_LEN
// bytes (the builders pad up to that size).

// Build a DHCPDISCOVER into buf. chaddr is the 6-byte hardware address to
// place in the BOOTP chaddr field and option 61 (client identifier) --
// NOT necessarily the Ethernet frame's source MAC; see the caveat in
// dhcp8008.c. Returns the packet length in bytes.
int dhcp_build_discover(uint8_t *buf, const uint8_t chaddr[6], uint32_t xid);

// Build a DHCPREQUEST (SELECTING state, RFC 2131 4.3.2) into buf, requesting
// requested_ip from server_id (both host byte order, network dotted-quad
// packed MSB-first e.g. 0xC0A80101 == 192.168.1.1). Returns the packet
// length in bytes.
int dhcp_build_request(uint8_t *buf, const uint8_t chaddr[6], uint32_t xid,
                        uint32_t requested_ip, uint32_t server_id);

// Parse a DHCPOFFER. Returns 1 and fills *ip/*server/*lease_secs (host byte
// order) on a valid offer matching xid; returns 0 otherwise (any output
// pointer may be NULL). lease_secs is 0 if the server omitted option 51.
int dhcp_parse_offer(const uint8_t *buf, int len, uint32_t xid,
                      uint32_t *ip, uint32_t *server, uint32_t *lease_secs);

// Parse a DHCPACK. Returns 1 and fills *ip/*lease_secs on a valid ack
// matching xid; returns 0 otherwise (also returns 0 on a DHCPNAK).
int dhcp_parse_ack(const uint8_t *buf, int len, uint32_t xid,
                    uint32_t *ip, uint32_t *lease_secs);

#ifdef __cplusplus
}
#endif

#endif // B8008_NET_DHCP8008_H
