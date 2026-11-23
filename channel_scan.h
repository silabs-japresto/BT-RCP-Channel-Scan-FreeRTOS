/***************************************************************************//**
 * @file
 * @brief 2.4 GHz channel energy scan controlled over vendor-specific HCI.
 *
 * Vendor commands (OGF 0x3F). All multi-byte fields are little endian.
 * Every response starts with the HCI status byte added by the controller.
 *
 * 0xFF00 Get Info        params: none
 *   rsp: format_version u8, num_channels u8, state u8 (0 idle, 1 running),
 *        report_seq u16, max_channels_per_read u8
 *
 * 0xFF01 Start           params: none (use defaults), or
 *        passes u16, averaging_us u16, interval_ms u32, busy_threshold_dbm i8
 *   rsp: none. Status 0x12 (invalid params) on bad lengths or values.
 *
 * 0xFF02 Stop            params: none     rsp: none
 *
 * 0xFF03 Read Report     params: first_channel u8, count u8
 *   rsp: report_seq u16, passes u16, first_channel u8, count u8,
 *        count x { min_dbm i8, avg_dbm i8, max_dbm i8, busy_pct u8 }
 *   report_seq is 0 until the first report completes. A host that reads a
 *   full report in several pages should check that report_seq is the same
 *   on every page, and read again otherwise.
 *
 * 0xFF04 Enter Bootloader params: none     rsp: none
 *   About 50 ms after the Command Complete, the device reboots into the UART
 *   XMODEM bootloader, which waits for a GBL upload at 115200 8N1.
 ******************************************************************************/

#ifndef CHANNEL_SCAN_H
#define CHANNEL_SCAN_H

#include <stdint.h>
#include "channel_scan_config.h"

#define CHANNEL_SCAN_REPORT_FORMAT_VERSION   1u

#define CHANNEL_SCAN_OPCODE_GET_INFO         0xFF00u
#define CHANNEL_SCAN_OPCODE_START            0xFF01u
#define CHANNEL_SCAN_OPCODE_STOP             0xFF02u
#define CHANNEL_SCAN_OPCODE_READ_REPORT      0xFF03u
#define CHANNEL_SCAN_OPCODE_ENTER_BOOTLOADER 0xFF04u

// Statistics of one RF channel over one report.
typedef struct {
  int8_t  min_dbm;    // Lowest sample: approximate noise floor.
  int8_t  avg_dbm;    // Mean of the samples, in dBm.
  int8_t  max_dbm;    // Highest sample: strongest burst seen.
  uint8_t busy_pct;   // Share of samples above the busy threshold.
} __attribute__((packed)) channel_scan_channel_stats_t;

/**************************************************************************//**
 * Create the scan task and register the vendor HCI command handler.
 * Call once during init.
 *****************************************************************************/
void channel_scan_init(void);

#endif // CHANNEL_SCAN_H
