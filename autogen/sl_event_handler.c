#include "sl_event_handler.h"

#include "sl_board_init.h"
#include "sl_clock_manager.h"
#include "sl_rail_util_compatible_pa.h"
#include "sl_board_control.h"
#include "sl_btctrl_rtos.h"
#include "sl_dma_manager_instances.h"
#include "app.h"
#include "sl_btctrl_linklayer.h"
#include "sl_debug_swo.h"
#include "sl_gpio.h"
#include "sl_mbedtls.h"
#include "sl_uartdrv_instances.h"
#include "sl_btctrl_hci_packet.h"
#include "psa/crypto.h"
#include "sl_se_manager.h"
#include "sli_protocol_crypto.h"
#include "sli_crypto.h"
#include "cmsis_os2.h"
#include "sl_cos.h"

void sli_driver_permanent_allocation(void)
{
}

void sli_service_permanent_allocation(void)
{
}

void sli_stack_permanent_allocation(void)
{
}

void sli_internal_permanent_allocation(void)
{
}

void sl_platform_init(void)
{
  sl_board_preinit();
  sl_clock_manager_runtime_init();
  sl_board_init();
  sl_dma_manager_instances_init();
}

void sli_internal_init_early(void)
{
  app_init_bt();
}

void sl_kernel_start(void)
{
  osKernelStart();
}

void sl_driver_init(void)
{
  sl_debug_swo_init();
  sl_gpio_init();
  sl_uartdrv_init_instances();
  sl_cos_send_config();
}

void sl_service_init(void)
{
  sl_board_configure_vcom();
  sl_mbedtls_init();
  psa_crypto_init();
  sl_se_init();
  sli_protocol_crypto_init();
  sli_crypto_init();
  sli_aes_seed_mask();
}

void sl_stack_init(void)
{
  sl_rail_util_pa_init();
  sl_btctrl_rtos_init();
  sl_btctrl_init();
}

void sl_internal_app_init(void)
{
}

