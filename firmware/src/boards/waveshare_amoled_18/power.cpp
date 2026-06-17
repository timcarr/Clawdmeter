#include "../../hal/power_hal.h"
#include "board.h"
#include "io_expander.h"
#include <Arduino.h>
#include <Wire.h>
#include <XPowersLib.h>

// PWR button via AXP2101 PKEY IRQs (mirrors the 2.16 board approach):
//   SHORT    — quick tap (cycle screens)
//   LONG     — ~1.5s mark, starts the hold-to-pair countdown
//   POSITIVE — release edge, completes/cancels the gesture
//
// The original EXIO4-based approach worked on USB but silently failed on
// battery: XCA9554 input pins are high-impedance (no internal pull resistors),
// so without the USB rail stabilising the line, EXIO4 floats HIGH, locking
// last_pwr_state=true and preventing any edge detection.
//
// VBUS_LOCKOUT_MS: the AXP fires spurious PKEY IRQs when USB is plugged or
// unplugged. Suppress button events for 500ms after any VBUS transition.

#define BATTERY_POLL_MS  2000
#define CHARGING_POLL_MS 500
#define PWR_POLL_MS      50
#define VBUS_LOCKOUT_MS  500

static XPowersPMU pmu;

static int      cached_pct        = -1;
static bool     cached_charging   = false;
static bool     cached_vbus       = false;
static bool     pwr_pressed_flag  = false;
static bool     pwr_long_flag     = false;
static bool     pwr_released_flag = false;
static uint32_t last_battery_ms   = 0;
static uint32_t last_charging_ms  = 0;
static uint32_t last_pwr_ms       = 0;
static uint32_t vbus_changed_ms   = 0;

void power_hal_init(void) {
    if (!pmu.begin(Wire, AXP2101_ADDR, IIC_SDA, IIC_SCL)) {
        Serial.println("AXP2101 init failed");
        return;
    }
    Serial.println("AXP2101 init OK");

    pmu.enableBattDetection();
    pmu.enableBattVoltageMeasure();

    pmu.disableIRQ(XPOWERS_AXP2101_ALL_IRQ);
    pmu.clearIrqStatus();
    pmu.enableIRQ(XPOWERS_AXP2101_PKEY_SHORT_IRQ
                | XPOWERS_AXP2101_PKEY_LONG_IRQ
                | XPOWERS_AXP2101_PKEY_POSITIVE_IRQ);

    pmu.setPowerKeyPressOffTime(XPOWERS_POWEROFF_8S);

    cached_charging = pmu.isCharging();
    cached_vbus     = pmu.isVbusIn();
    cached_pct      = pmu.getBatteryPercent();
}

void power_hal_tick(void) {
    uint32_t now = millis();

    if (now - last_charging_ms >= CHARGING_POLL_MS) {
        last_charging_ms = now;
        cached_charging = pmu.isCharging();
        bool new_vbus = pmu.isVbusIn();
        if (new_vbus != cached_vbus) {
            cached_vbus     = new_vbus;
            vbus_changed_ms = now;
        }
    }
    if (now - last_battery_ms >= BATTERY_POLL_MS) {
        last_battery_ms = now;
        cached_pct = pmu.getBatteryPercent();
    }
    if (now - last_pwr_ms >= PWR_POLL_MS) {
        last_pwr_ms = now;
        pmu.getIrqStatus();
        if (now - vbus_changed_ms >= VBUS_LOCKOUT_MS) {
            if (pmu.isPekeyShortPressIrq())  pwr_pressed_flag  = true;
            if (pmu.isPekeyLongPressIrq())   pwr_long_flag     = true;
            if (pmu.isPekeyPositiveIrq())    pwr_released_flag = true;
        }
        pmu.clearIrqStatus();
    }
}

int  power_hal_battery_pct(void) { return cached_pct; }
bool power_hal_is_charging(void) { return cached_charging; }
bool power_hal_is_vbus_in(void)  { return cached_vbus; }

bool power_hal_pwr_pressed(void) {
    if (pwr_pressed_flag) { pwr_pressed_flag = false; return true; }
    return false;
}

bool power_hal_pwr_long_pressed(void) {
    if (pwr_long_flag) { pwr_long_flag = false; return true; }
    return false;
}

bool power_hal_pwr_released(void) {
    if (pwr_released_flag) { pwr_released_flag = false; return true; }
    return false;
}
