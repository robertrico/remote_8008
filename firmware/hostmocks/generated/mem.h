// hostmock generated/mem.h -- ethmac slot memory lives in a host array.
#ifndef HOSTMOCK_MEM_H
#define HOSTMOCK_MEM_H
#include <stdint.h>
extern uint8_t mock_ethmac_mem[];
#define ETHMAC_BASE ((uintptr_t)mock_ethmac_mem)
#endif
