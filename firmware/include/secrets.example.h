// Copy this file to secrets.h (same folder) and fill in your values.
// secrets.h is git-ignored - never commit real credentials.
#pragma once

// --- WiFi ---------------------------------------------------------------------
#define WIFI_SSID "your-ssid"
#define WIFI_PASSWORD "your-wifi-password"

// --- Device identity ------------------------------------------------------------
// Must match [A-Za-z0-9_-]{1,32}. In the secure setup this is also the MQTT
// username, and the broker ACL only lets it publish to sensors/<DEVICE_ID>/...
#define DEVICE_ID "esp32-kitchen"

// --- MQTT broker ----------------------------------------------------------------
// With TLS the host name must match a subjectAltName of the broker certificate
// (add it with: python scripts/gen_certs.py --dns my-pc.local --ip 192.168.1.50).
#define MQTT_HOST "192.168.1.50"

// 0 = insecure profile (plain MQTT on 1883, anonymous)
// 1 = secure profile (TLS on 8883 + username/password)
#define MQTT_USE_TLS 1

#if MQTT_USE_TLS
#define MQTT_PORT 8883
#else
#define MQTT_PORT 1883
#endif

// Leave the username empty ("") for anonymous access in the insecure profile.
#define MQTT_USERNAME DEVICE_ID
#define MQTT_PASSWORD "the-password-from-make_passwd"

// PEM of the local CA (mosquitto/certs/ca.crt produced by scripts/gen_certs.*).
// Only the CA certificate goes here - never the CA or server private key.
static const char MQTT_CA_CERT[] = R"PEM(
-----BEGIN CERTIFICATE-----
...paste the contents of mosquitto/certs/ca.crt here...
-----END CERTIFICATE-----
)PEM";
