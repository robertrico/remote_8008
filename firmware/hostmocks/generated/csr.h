// hostmock generated/csr.h -- ethmac CSR accessors backed by the test model.
#ifndef HOSTMOCK_CSR_H
#define HOSTMOCK_CSR_H
#include <stdint.h>

// model state, defined by the test harness
extern uint8_t  mock_reader_ready;
extern uint32_t mock_reader_slot, mock_reader_length, mock_reader_starts;
extern uint8_t  mock_writer_pending;
extern uint32_t mock_writer_slot, mock_writer_length;

void mock_reader_start_hook(void);

static inline uint8_t  ethmac_sram_reader_ready_read(void)    { return mock_reader_ready; }
static inline void ethmac_sram_reader_slot_write(uint32_t v)  { mock_reader_slot = v; }
static inline void ethmac_sram_reader_length_write(uint32_t v){ mock_reader_length = v; }
static inline void ethmac_sram_reader_start_write(uint32_t v) { (void)v; mock_reader_starts++; mock_reader_start_hook(); }
static inline void ethmac_sram_reader_ev_pending_write(uint32_t v) { (void)v; }

static inline uint8_t  ethmac_sram_writer_ev_pending_read(void) { return mock_writer_pending; }
static inline void ethmac_sram_writer_ev_pending_write(uint32_t v) { (void)v; mock_writer_pending = 0; }
static inline uint32_t ethmac_sram_writer_slot_read(void)     { return mock_writer_slot; }
static inline uint32_t ethmac_sram_writer_length_read(void)   { return mock_writer_length; }

#endif
#define CSR_ETHMAC_BASE 0xf0001800L
#define CSR_ETHMAC_PREAMBLE_CRC_ADDR 0xf0001838L
