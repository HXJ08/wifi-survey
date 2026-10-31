#pragma once
#include <stddef.h>
#include <stdint.h>
// Declared in a header so Arduino-generated function prototypes can see it.
struct ExportState { size_t length=0; uint32_t crc=0xffffffff; bool send=false; };
