// eb_serve.h -- software Etherbone serve glue (see eb_serve.c).
#ifndef EB_SERVE_H
#define EB_SERVE_H

#include <stdint.h>

#define EB_BUF_LEN 1500

// Install as both udp_set_callback and udp_set_broadcast_callback.
void eb_rx_callback(uint32_t src_ip, uint16_t src_port,
                    uint16_t dst_port, void *data, uint32_t length);

// Call from the serve loop after each udp_service().
void eb_serve_pending(void);

// Serve counters (diagnostics).
extern volatile uint32_t eb_cb_any, eb_cb_port, eb_served;

#endif
