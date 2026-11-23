/***************************************************************************//**
 * @file
 * @brief 2.4 GHz channel energy scan for the Bluetooth HCI RCP, controlled
 *        over vendor-specific HCI commands.
 *
 * Measurement: the sl_rail_util_init instance "inst0" runs the BLE 1 Mbps PHY on its own RAIL
 * DMP handle, so RAIL channel k is BLE RF channel k (2402 + 2 * k MHz). Each
 * sample is a hardware RSSI average scheduled below every Bluetooth priority,
 * so link layer traffic always wins the radio. Preempted samples are dropped.
 *
 * Control: VCOM carries HCI, so there is no serial log. The host starts/stops
 * the scan and pulls the latest finished report through the vendor commands
 * in channel_scan.h.
 *
 * Concurrency: the HCI handler may run in task or interrupt context
 * (not documented), so it never blocks. It copies the published report inside
 * a short interrupt-masked section and signals the scan task with a task
 * notification.
 ******************************************************************************/

#include <string.h>
#include "FreeRTOS.h"
#include "task.h"
#include "semphr.h"
#include "sl_rail.h"
#include "sl_rail_util_init.h"
#include "sl_btctrl_hci_handler.h"
#include "app_assert.h"
#include "btl_interface.h"
#include "channel_scan.h"

// RAIL ISR -> scan task notification layout: [31] valid, [23:16] seq, [15:0] RSSI.
#define SAMPLE_VALID_FLAG        0x80000000UL
#define SAMPLE_SEQ_SHIFT         16
#define SAMPLE_SEQ_MASK          0xFFUL
#define SAMPLE_RSSI_MASK         0xFFFFUL

// HCI -> scan task control requests. The task notification value carries RSSI
// samples, so control uses pending bits plus a binary semaphore as wake-up.
#define CTRL_START               (1UL << 0)
#define CTRL_STOP                (1UL << 1)
#define CTRL_BOOTLOADER          (1UL << 2)

// Delay before rebooting into the bootloader, so the Command Complete for
// Enter Bootloader leaves the UART first.
#define BOOTLOADER_REBOOT_DELAY_MS  50u

// HCI status codes.
#define HCI_SUCCESS              0x00
#define HCI_INVALID_PARAMS       0x12
#define HCI_UNSPECIFIED_ERROR    0x1F

// Channels per Read Report response: 6 header bytes + 10 x 4 = 46 bytes,
// below SL_BTCTRL_MAX_HCI_RESPONSE_LENGTH.
#define MAX_CHANNELS_PER_READ    10u
#define READ_REPORT_HEADER_LEN   6u
_Static_assert(READ_REPORT_HEADER_LEN
               + MAX_CHANNELS_PER_READ * sizeof(channel_scan_channel_stats_t)
               < SL_BTCTRL_MAX_HCI_RESPONSE_LENGTH,
               "Read Report response too long");

typedef struct {
  uint16_t passes;
  uint16_t averaging_us;
  uint32_t interval_ms;
  int8_t   busy_threshold_dbm;
} scan_params_t;

// Running statistics of one channel, RSSI in quarter dBm.
typedef struct {
  int16_t  min_q;
  int16_t  max_q;
  int32_t  sum_q;
  uint16_t valid;
  uint16_t busy;
} channel_acc_t;

static StaticTask_t scan_task_tcb;
static StackType_t  scan_task_stack[CHANNEL_SCAN_TASK_STACK_SIZE];
static TaskHandle_t scan_task_handle = NULL;

static sl_rail_handle_t rail_handle = SL_RAIL_EFR32_HANDLE;
static volatile uint8_t sample_seq = 0;

// Written by the HCI handler, read by the scan task at Start.
static volatile scan_params_t requested_params;
static volatile bool running = false;

// Scan task working state.
static scan_params_t params;
static channel_acc_t acc[CHANNEL_SCAN_NUM_CHANNELS];

// Last finished report, read by the HCI handler. Guarded by mask_irq().
static channel_scan_channel_stats_t published[CHANNEL_SCAN_NUM_CHANNELS];
static uint16_t published_passes = 0;
static uint16_t published_seq = 0;

static sl_btctrl_command_handler_t hci_handler;
static StaticSemaphore_t ctrl_sem_buf;
static SemaphoreHandle_t ctrl_sem = NULL;
static volatile uint32_t ctrl_pending = 0;

static void scan_task(void *p_arg);
static bool hci_vendor_handler(struct sl_btctrl_hci_message *msg);
static bool measure_channel(uint8_t rf_channel, int16_t *rssi_q);
static void publish_report(uint16_t passes);
static int8_t quarter_dbm_to_dbm(int32_t value_q);

// Interrupt masking usable from both task and ISR context.
static inline UBaseType_t mask_irq(void)
{
  return portSET_INTERRUPT_MASK_FROM_ISR();
}

static inline void unmask_irq(UBaseType_t state)
{
  portCLEAR_INTERRUPT_MASK_FROM_ISR(state);
}

static void notify_scan_task(uint32_t bits)
{
  UBaseType_t s = mask_irq();
  ctrl_pending |= bits;
  unmask_irq(s);
  if (xPortIsInsideInterrupt()) {
    BaseType_t woken = pdFALSE;
    (void)xSemaphoreGiveFromISR(ctrl_sem, &woken);
    portYIELD_FROM_ISR(woken);
  } else {
    (void)xSemaphoreGive(ctrl_sem);
  }
}

static void set_default_params(volatile scan_params_t *p)
{
  p->passes = CHANNEL_SCAN_PASSES_PER_REPORT;
  p->averaging_us = CHANNEL_SCAN_AVERAGING_TIME_US;
  p->interval_ms = CHANNEL_SCAN_REPORT_INTERVAL_MS;
  p->busy_threshold_dbm = CHANNEL_SCAN_BUSY_THRESHOLD_DBM;
}

void channel_scan_init(void)
{
  for (uint8_t ch = 0; ch < CHANNEL_SCAN_NUM_CHANNELS; ch++) {
    published[ch].min_dbm = SL_RAIL_RSSI_INVALID_DBM;
    published[ch].avg_dbm = SL_RAIL_RSSI_INVALID_DBM;
    published[ch].max_dbm = SL_RAIL_RSSI_INVALID_DBM;
    published[ch].busy_pct = 0;
  }
  set_default_params(&requested_params);
  ctrl_sem = xSemaphoreCreateBinaryStatic(&ctrl_sem_buf);
  app_assert(ctrl_sem != NULL, "Channel scan semaphore creation failed.");

  scan_task_handle = xTaskCreateStatic(scan_task,
                                       CHANNEL_SCAN_TASK_NAME,
                                       CHANNEL_SCAN_TASK_STACK_SIZE,
                                       NULL,
                                       CHANNEL_SCAN_TASK_PRIO,
                                       scan_task_stack,
                                       &scan_task_tcb);
  app_assert(scan_task_handle != NULL, "Channel scan task creation failed.");

  sl_btctrl_hci_register_handler(&hci_handler, hci_vendor_handler);

#if CHANNEL_SCAN_AUTOSTART
  running = true;
  notify_scan_task(CTRL_START);
#endif
}

/**************************************************************************//**
 * Vendor-specific HCI command handler. Returns false for opcodes that are not
 * ours so the controller answers "Unknown HCI Command" as usual.
 *****************************************************************************/
static bool hci_vendor_handler(struct sl_btctrl_hci_message *msg)
{
  uint16_t opcode;
  size_t len = 0;
  uint8_t in[16];
  uint8_t out[SL_BTCTRL_MAX_HCI_RESPONSE_LENGTH];

  if (sl_btctrl_hci_message_get_opcode(msg, &opcode) != SL_STATUS_OK) {
    return false;
  }
  if (opcode < CHANNEL_SCAN_OPCODE_GET_INFO || opcode > CHANNEL_SCAN_OPCODE_ENTER_BOOTLOADER) {
    return false;
  }
  if (sl_btctrl_hci_message_get_length(msg, &len) != SL_STATUS_OK || len > sizeof(in)) {
    sl_btctrl_hci_message_set_response(msg, HCI_INVALID_PARAMS, NULL, 0);
    return true;
  }
  if (len > 0 && sl_btctrl_hci_message_get_parameters(msg, in, len) != SL_STATUS_OK) {
    sl_btctrl_hci_message_set_response(msg, HCI_UNSPECIFIED_ERROR, NULL, 0);
    return true;
  }

  switch (opcode) {
    case CHANNEL_SCAN_OPCODE_GET_INFO: {
      UBaseType_t s = mask_irq();
      uint16_t seq = published_seq;
      unmask_irq(s);
      out[0] = CHANNEL_SCAN_REPORT_FORMAT_VERSION;
      out[1] = CHANNEL_SCAN_NUM_CHANNELS;
      out[2] = running ? 1 : 0;
      out[3] = (uint8_t)seq;
      out[4] = (uint8_t)(seq >> 8);
      out[5] = MAX_CHANNELS_PER_READ;
      sl_btctrl_hci_message_set_response(msg, HCI_SUCCESS, out, 6);
      return true;
    }

    case CHANNEL_SCAN_OPCODE_START: {
      scan_params_t p;
      set_default_params(&p);
      if (len == 9) {
        p.passes = (uint16_t)(in[0] | (in[1] << 8));
        p.averaging_us = (uint16_t)(in[2] | (in[3] << 8));
        p.interval_ms = (uint32_t)in[4] | ((uint32_t)in[5] << 8)
                        | ((uint32_t)in[6] << 16) | ((uint32_t)in[7] << 24);
        p.busy_threshold_dbm = (int8_t)in[8];
      } else if (len != 0) {
        sl_btctrl_hci_message_set_response(msg, HCI_INVALID_PARAMS, NULL, 0);
        return true;
      }
      if (p.passes == 0 || p.averaging_us == 0
          || p.averaging_us > CHANNEL_SCAN_AVERAGING_TIME_MAX_US) {
        sl_btctrl_hci_message_set_response(msg, HCI_INVALID_PARAMS, NULL, 0);
        return true;
      }
      UBaseType_t s = mask_irq();
      requested_params.passes = p.passes;
      requested_params.averaging_us = p.averaging_us;
      requested_params.interval_ms = p.interval_ms;
      requested_params.busy_threshold_dbm = p.busy_threshold_dbm;
      running = true;
      unmask_irq(s);
      notify_scan_task(CTRL_START);
      sl_btctrl_hci_message_set_response(msg, HCI_SUCCESS, NULL, 0);
      return true;
    }

    case CHANNEL_SCAN_OPCODE_STOP:
      running = false;
      notify_scan_task(CTRL_STOP);
      sl_btctrl_hci_message_set_response(msg, HCI_SUCCESS, NULL, 0);
      return true;

    case CHANNEL_SCAN_OPCODE_READ_REPORT: {
      if (len != 2) {
        sl_btctrl_hci_message_set_response(msg, HCI_INVALID_PARAMS, NULL, 0);
        return true;
      }
      uint8_t first = in[0];
      uint8_t count = in[1];
      if (first >= CHANNEL_SCAN_NUM_CHANNELS || count == 0) {
        sl_btctrl_hci_message_set_response(msg, HCI_INVALID_PARAMS, NULL, 0);
        return true;
      }
      if (count > MAX_CHANNELS_PER_READ) {
        count = MAX_CHANNELS_PER_READ;
      }
      if (count > CHANNEL_SCAN_NUM_CHANNELS - first) {
        count = (uint8_t)(CHANNEL_SCAN_NUM_CHANNELS - first);
      }
      UBaseType_t s = mask_irq();
      out[0] = (uint8_t)published_seq;
      out[1] = (uint8_t)(published_seq >> 8);
      out[2] = (uint8_t)published_passes;
      out[3] = (uint8_t)(published_passes >> 8);
      memcpy(&out[READ_REPORT_HEADER_LEN], &published[first],
             count * sizeof(channel_scan_channel_stats_t));
      unmask_irq(s);
      out[4] = first;
      out[5] = count;
      sl_btctrl_hci_message_set_response(msg, HCI_SUCCESS, out,
                                         READ_REPORT_HEADER_LEN
                                         + count * sizeof(channel_scan_channel_stats_t));
      return true;
    }

    case CHANNEL_SCAN_OPCODE_ENTER_BOOTLOADER:
      running = false;
      notify_scan_task(CTRL_BOOTLOADER);
      sl_btctrl_hci_message_set_response(msg, HCI_SUCCESS, NULL, 0);
      return true;

    default:
      return false;
  }
}

/**************************************************************************//**
 * RAIL event callback for the sl_rail_util_init instances (interrupt context).
 *****************************************************************************/
void sl_rail_util_on_event(sl_rail_handle_t handle, sl_rail_events_t events)
{
  uint32_t value = 0;

  if (handle != rail_handle || scan_task_handle == NULL) {
    return;
  }

  if (events & SL_RAIL_EVENT_RSSI_AVERAGE_DONE) {
    int16_t rssi_q = sl_rail_get_average_rssi(handle);
    if (rssi_q != SL_RAIL_RSSI_INVALID) {
      value = SAMPLE_VALID_FLAG | ((uint32_t)(uint16_t)rssi_q & SAMPLE_RSSI_MASK);
    }
  } else if (!(events & SL_RAIL_EVENT_SCHEDULER_STATUS)) {
    return;
  }

  // Both a finished average and a scheduler failure end the operation.
  (void)sl_rail_yield_radio(handle);

  value |= ((uint32_t)sample_seq & SAMPLE_SEQ_MASK) << SAMPLE_SEQ_SHIFT;
  BaseType_t woken = pdFALSE;
  (void)xTaskNotifyFromISR(scan_task_handle, value, eSetValueWithOverwrite, &woken);
  portYIELD_FROM_ISR(woken);
}

/**************************************************************************//**
 * Wait up to `ticks` for a control request. Returns true if a (re)start was
 * requested and the scan is still meant to run.
 *****************************************************************************/
static bool wait_control(TickType_t ticks)
{
  if (xSemaphoreTake(ctrl_sem, ticks) != pdTRUE) {
    return false;
  }
  UBaseType_t s = mask_irq();
  uint32_t bits = ctrl_pending;
  ctrl_pending = 0;
  unmask_irq(s);
  if (bits & CTRL_BOOTLOADER) {
    vTaskDelay(pdMS_TO_TICKS(BOOTLOADER_REBOOT_DELAY_MS));
    bootloader_rebootAndInstall();   // Does not return.
  }
  return (bits & CTRL_START) != 0 && running;
}

static void load_params(void)
{
  UBaseType_t s = mask_irq();
  params.passes = requested_params.passes;
  params.averaging_us = requested_params.averaging_us;
  params.interval_ms = requested_params.interval_ms;
  params.busy_threshold_dbm = requested_params.busy_threshold_dbm;
  unmask_irq(s);
}

static void scan_task(void *p_arg)
{
  (void)p_arg;
  sl_rail_status_t status;

  rail_handle = sl_rail_util_get_handle(SL_RAIL_UTIL_HANDLE_INST0);
  app_assert(rail_handle != SL_RAIL_EFR32_HANDLE && rail_handle != NULL,
             "RAIL scan handle not initialized.");

  status = sl_rail_config_events(rail_handle,
                                 SL_RAIL_EVENTS_ALL,
                                 SL_RAIL_EVENT_RSSI_AVERAGE_DONE
                                 | SL_RAIL_EVENT_SCHEDULER_STATUS);
  app_assert(status == SL_RAIL_STATUS_NO_ERROR,
             "[E: 0x%04x] Failed to configure RAIL events", (int)status);

  for (;; ) {
    // Idle: the radio is free and the device can sleep until the host starts us.
    while (!wait_control(portMAX_DELAY)) {
    }
    load_params();

    while (running) {
      bool restart = false;
      memset(acc, 0, sizeof(acc));

      for (uint16_t pass = 0; pass < params.passes && running && !restart; pass++) {
        for (uint8_t ch = 0; ch < CHANNEL_SCAN_NUM_CHANNELS && running; ch++) {
          int16_t rssi_q;
          if (wait_control(0)) {
            restart = true;   // New Start while running: apply new parameters.
            break;
          }
          if (!measure_channel(ch, &rssi_q)) {
            continue;
          }
          channel_acc_t *a = &acc[ch];
          if (a->valid == 0 || rssi_q < a->min_q) {
            a->min_q = rssi_q;
          }
          if (a->valid == 0 || rssi_q > a->max_q) {
            a->max_q = rssi_q;
          }
          a->sum_q += rssi_q;
          a->valid++;
          if (rssi_q > (int16_t)params.busy_threshold_dbm * 4) {
            a->busy++;
          }
        }
      }

      if (restart) {
        load_params();
        continue;
      }
      if (!running) {
        break;   // Stopped mid-report: keep the previous report.
      }
      publish_report(params.passes);

      if (params.interval_ms > 0) {
        // LOW POWER: the device sleeps here. A Start or Stop wakes us early.
        if (wait_control(pdMS_TO_TICKS(params.interval_ms))) {
          load_params();
        }
      } else {
        taskYIELD();
      }
    }
  }
}

/**************************************************************************//**
 * Measure one channel. Returns false if the sample was preempted or failed.
 *****************************************************************************/
static bool measure_channel(uint8_t rf_channel, int16_t *rssi_q)
{
  const sl_rail_scheduler_info_t scheduler_info = {
    .priority = CHANNEL_SCAN_RAIL_PRIORITY,
    .slip_time = CHANNEL_SCAN_SLIP_TIME_US,
    .transaction_time = params.averaging_us,
  };
  uint32_t value;
  uint8_t seq = ++sample_seq;

  (void)xTaskNotifyStateClear(NULL);

  sl_rail_status_t status = sl_rail_start_average_rssi(rail_handle,
                                                       rf_channel,
                                                       params.averaging_us,
                                                       &scheduler_info);
  if (status != SL_RAIL_STATUS_NO_ERROR) {
    vTaskDelay(1);
    return false;
  }

  TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(CHANNEL_SCAN_SAMPLE_TIMEOUT_MS);
  for (;; ) {
    TickType_t now = xTaskGetTickCount();
    TickType_t wait = (deadline > now) ? (deadline - now) : 0;
    if (xTaskNotifyWait(0, UINT32_MAX, &value, wait) != pdTRUE) {
      (void)sl_rail_idle(rail_handle, SL_RAIL_IDLE_ABORT, true);
      (void)sl_rail_yield_radio(rail_handle);
      return false;
    }
    if (((value >> SAMPLE_SEQ_SHIFT) & SAMPLE_SEQ_MASK) == seq) {
      break;
    }
  }

  if (!(value & SAMPLE_VALID_FLAG)) {
    return false;
  }
  *rssi_q = (int16_t)(uint16_t)(value & SAMPLE_RSSI_MASK);
  return true;
}

static int8_t quarter_dbm_to_dbm(int32_t value_q)
{
  int32_t dbm = (value_q >= 0) ? ((value_q + 2) / 4) : ((value_q - 2) / 4);
  if (dbm < SL_RAIL_RSSI_INVALID_DBM) {
    dbm = SL_RAIL_RSSI_INVALID_DBM;
  } else if (dbm > INT8_MAX) {
    dbm = INT8_MAX;
  }
  return (int8_t)dbm;
}

static void publish_report(uint16_t passes)
{
  channel_scan_channel_stats_t stats[CHANNEL_SCAN_NUM_CHANNELS];

  for (uint8_t ch = 0; ch < CHANNEL_SCAN_NUM_CHANNELS; ch++) {
    const channel_acc_t *a = &acc[ch];
    channel_scan_channel_stats_t *s = &stats[ch];
    if (a->valid == 0) {
      s->min_dbm = SL_RAIL_RSSI_INVALID_DBM;
      s->avg_dbm = SL_RAIL_RSSI_INVALID_DBM;
      s->max_dbm = SL_RAIL_RSSI_INVALID_DBM;
      s->busy_pct = 0;
      continue;
    }
    s->min_dbm = quarter_dbm_to_dbm(a->min_q);
    s->avg_dbm = quarter_dbm_to_dbm(a->sum_q / (int32_t)a->valid);
    s->max_dbm = quarter_dbm_to_dbm(a->max_q);
    s->busy_pct = (uint8_t)(((uint32_t)a->busy * 100u) / a->valid);
  }

  UBaseType_t s = mask_irq();
  memcpy(published, stats, sizeof(published));
  published_passes = passes;
  published_seq++;
  if (published_seq == 0) {
    published_seq = 1;   // 0 means "no report yet".
  }
  unmask_irq(s);
}
