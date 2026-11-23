/***************************************************************************//**
 * @file
 * @brief 2.4 GHz channel energy scan configuration (RCP build).
 *
 * In the RCP build the host controls the scan through vendor-specific HCI
 * commands (see channel_scan.h and CHANNEL_SCAN_RCP.md). The values below are
 * the defaults used when the host sends "Start" with no parameters.
 ******************************************************************************/

#ifndef CHANNEL_SCAN_CONFIG_H
#define CHANNEL_SCAN_CONFIG_H

// Number of BLE RF channels scanned (RF channel k = 2402 + 2 * k MHz).
#define CHANNEL_SCAN_NUM_CHANNELS            40u

// Default duration of each hardware RSSI average, in microseconds.
#define CHANNEL_SCAN_AVERAGING_TIME_US       128u
#define CHANNEL_SCAN_AVERAGING_TIME_MAX_US   10000u

// Default number of full sweeps over all channels per report.
#define CHANNEL_SCAN_PASSES_PER_REPORT       20u

// Default idle time between reports, in ms. 0 = scan continuously.
// LOW POWER: a long interval lets the device sleep in EM2 between reports.
#define CHANNEL_SCAN_REPORT_INTERVAL_MS      0u

// Default "busy" threshold. -71 dBm matches the Bluetooth AFH default.
#define CHANNEL_SCAN_BUSY_THRESHOLD_DBM      (-71)

// RAIL scheduler priority of each measurement. Keep this numerically above
// every Bluetooth controller priority so the scan always yields to Bluetooth.
#define CHANNEL_SCAN_RAIL_PRIORITY           250u

// How long a measurement may wait for the radio before it is dropped.
#define CHANNEL_SCAN_SLIP_TIME_US            20000u

// Fallback timeout for a single measurement, in milliseconds.
#define CHANNEL_SCAN_SAMPLE_TIMEOUT_MS       50u

// Start scanning at boot with the defaults, without waiting for the host.
#define CHANNEL_SCAN_AUTOSTART               0

// Scan task settings.
#define CHANNEL_SCAN_TASK_NAME               "chscan_task"
#define CHANNEL_SCAN_TASK_STACK_SIZE         256u   // in words
#define CHANNEL_SCAN_TASK_PRIO               20u

#endif // CHANNEL_SCAN_CONFIG_H
