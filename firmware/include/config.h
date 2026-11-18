// Non-secret build configuration. Secrets live in secrets.h (git-ignored).
#pragma once

#ifndef FW_VERSION
#define FW_VERSION "esp32-1.0.0"
#endif

// How often a reading is published. DHT22 needs at least 2 s between reads.
#ifndef PUBLISH_INTERVAL_MS
#define PUBLISH_INTERVAL_MS 10000UL
#endif

// --- Pins (see docs/hardware.md for the wiring table) ----------------------
#define DHT_PIN 4         // DHT22 data line (10k pull-up to 3V3)
#define I2C_SDA_PIN 21    // BME280 SDA
#define I2C_SCL_PIN 22    // BME280 SCL
#define BME280_I2C_ADDR 0x76  // 0x77 if SDO is tied high
#define PIR_PIN 27        // HC-SR501 OUT; set to -1 when no PIR is fitted
#define STATUS_LED_PIN 2  // on-board LED on most ESP32 DevKit boards

// --- Network behaviour --------------------------------------------------------
#define WIFI_CONNECT_TIMEOUT_MS 20000UL
#define NTP_SYNC_TIMEOUT_MS 15000UL
#define MQTT_RECONNECT_INTERVAL_MS 5000UL
#define MQTT_KEEPALIVE_S 30
#define MQTT_BUFFER_SIZE 512
// Any Unix time before this means the clock has not been synced yet.
#define MIN_VALID_EPOCH 1700000000UL
