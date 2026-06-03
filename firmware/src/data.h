#pragma once
#include <Arduino.h>

struct UsageData {
    float session_pct;       // 5-hour window utilization (0-100)
    int session_reset_mins;  // minutes until session resets
    float weekly_pct;        // 7-day window utilization (0-100)
    int weekly_reset_mins;   // minutes until weekly resets
    char status[16];         // "allowed" or "limited"
    char host_name[64];      // sending computer's hostname
    bool ok;                 // data parse succeeded
    bool active;             // true if session utilization rose recently
    bool valid;              // false until first successful parse
};
