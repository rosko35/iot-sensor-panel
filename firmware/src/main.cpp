// ESP32 sensor node: DHT22 or BME280 (+ optional PIR) -> MQTT (optionally TLS).
//
// Publishes the same JSON schema as the Python simulator to
// sensors/<DEVICE_ID>/telemetry and a retained online/offline status (with a
// last will) to sensors/<DEVICE_ID>/status.

#include <Arduino.h>
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <math.h>
#include <time.h>

#include "config.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Copy include/secrets.example.h to include/secrets.h and fill in WiFi/MQTT settings."
#endif

#if defined(SENSOR_DHT22)
#include <DHT.h>
static DHT dht(DHT_PIN, DHT22);
#elif defined(SENSOR_BME280)
#include <Adafruit_BME280.h>
#include <Adafruit_Sensor.h>
#include <Wire.h>
static Adafruit_BME280 bme;
#else
#error "Build with -DSENSOR_DHT22 or -DSENSOR_BME280 (see platformio.ini)."
#endif

#if MQTT_USE_TLS
static WiFiClientSecure net;
#else
static WiFiClient net;
#endif
static PubSubClient mqtt(net);

static char topicTelemetry[64];
static char topicStatus[64];
static uint32_t seq = 0;
static uint32_t lastPublishMs = 0;
static uint32_t lastMqttAttemptMs = 0;
static bool sensorOk = false;

#if PIR_PIN >= 0
// Latched by the interrupt so short motion pulses between samples are not lost.
static volatile bool motionLatched = false;
static void IRAM_ATTR onMotion() { motionLatched = true; }
#endif

struct Reading {
  float temperature = NAN;
  float humidity = NAN;
  float pressure = NAN;  // hPa, BME280 only
};

static float round2(float v) { return roundf(v * 100.0f) / 100.0f; }

// --------------------------------------------------------------------------- sensors

static bool initSensor() {
#if defined(SENSOR_DHT22)
  dht.begin();
  return true;
#else
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  if (!bme.begin(BME280_I2C_ADDR, &Wire)) {
    Serial.println(F("[sensor] BME280 not found - check wiring and I2C address"));
    return false;
  }
  // Weather-station settings from the datasheet: forced mode, 1x oversampling.
  bme.setSampling(Adafruit_BME280::MODE_FORCED, Adafruit_BME280::SAMPLING_X1,
                  Adafruit_BME280::SAMPLING_X1, Adafruit_BME280::SAMPLING_X1,
                  Adafruit_BME280::FILTER_OFF);
  return true;
#endif
}

static Reading readSensor() {
  Reading r;
#if defined(SENSOR_DHT22)
  r.temperature = dht.readTemperature();
  r.humidity = dht.readHumidity();
#else
  if (sensorOk && bme.takeForcedMeasurement()) {
    r.temperature = bme.readTemperature();
    r.humidity = bme.readHumidity();
    r.pressure = bme.readPressure() / 100.0f;
  }
#endif
  return r;
}

static bool readMotion() {
#if PIR_PIN >= 0
  bool motion = motionLatched || digitalRead(PIR_PIN) == HIGH;
  motionLatched = false;
  return motion;
#else
  return false;
#endif
}

// --------------------------------------------------------------------------- network

static void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("[wifi] connecting to %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(DEVICE_ID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  const uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < WIFI_CONNECT_TIMEOUT_MS) {
    delay(250);
    Serial.print('.');
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf(" ok, ip=%s rssi=%d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    Serial.println(F(" failed, will retry"));
  }
}

// TLS certificate validation needs a correct clock (validity period), and the
// payload timestamp comes from it too.
static void syncTime() {
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  const uint32_t started = millis();
  while (time(nullptr) < (time_t)MIN_VALID_EPOCH && millis() - started < NTP_SYNC_TIMEOUT_MS) {
    delay(200);
  }
  Serial.printf("[ntp] epoch=%ld\n", (long)time(nullptr));
}

static bool connectMqtt() {
  if (mqtt.connected()) return true;
  if (WiFi.status() != WL_CONNECTED) return false;
  if (millis() - lastMqttAttemptMs < MQTT_RECONNECT_INTERVAL_MS && lastMqttAttemptMs != 0) {
    return false;
  }
  lastMqttAttemptMs = millis();

  const char *user = strlen(MQTT_USERNAME) ? MQTT_USERNAME : nullptr;
  const char *pass = user ? MQTT_PASSWORD : nullptr;
  Serial.printf("[mqtt] connecting to %s:%d (tls=%d) as %s\n", MQTT_HOST, MQTT_PORT,
                MQTT_USE_TLS, user ? user : "<anonymous>");
  // Last will: the broker publishes "offline" (retained) if we drop off.
  if (!mqtt.connect(DEVICE_ID, user, pass, topicStatus, 1, true, "offline")) {
    Serial.printf("[mqtt] failed, state=%d\n", mqtt.state());
    return false;
  }
  mqtt.publish(topicStatus, "online", true);
  Serial.println(F("[mqtt] connected"));
  return true;
}

// --------------------------------------------------------------------------- publish

static void publishReading() {
  const Reading r = readSensor();
  const bool motion = readMotion();

  JsonDocument doc;
  doc["v"] = 1;
  doc["device_id"] = DEVICE_ID;
  const time_t now = time(nullptr);
  if (now >= (time_t)MIN_VALID_EPOCH) {
    doc["ts"] = (uint32_t)now;  // otherwise the ingest service uses receive time
  }
  doc["seq"] = ++seq;
  if (!isnan(r.temperature)) doc["temperature"] = round2(r.temperature);
  if (!isnan(r.humidity)) doc["humidity"] = round2(r.humidity);
  if (!isnan(r.pressure)) doc["pressure"] = round2(r.pressure);
#if PIR_PIN >= 0
  doc["motion"] = motion;
#else
  (void)motion;
#endif
  doc["rssi"] = WiFi.RSSI();
  doc["fw"] = FW_VERSION;

  if (isnan(r.temperature) && isnan(r.humidity)) {
    Serial.println(F("[sensor] read failed, publishing motion/rssi only"));
  }

  char buffer[MQTT_BUFFER_SIZE];
  const size_t len = serializeJson(doc, buffer, sizeof(buffer));
  const bool ok = mqtt.publish(topicTelemetry, reinterpret_cast<const uint8_t *>(buffer), len,
                               false);
  Serial.printf("[mqtt] %s %s %s\n", ok ? "->" : "!!", topicTelemetry, buffer);
  digitalWrite(STATUS_LED_PIN, ok ? HIGH : LOW);
}

// --------------------------------------------------------------------------- Arduino

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("\n[boot] %s firmware %s\n", DEVICE_ID, FW_VERSION);

  pinMode(STATUS_LED_PIN, OUTPUT);
#if PIR_PIN >= 0
  pinMode(PIR_PIN, INPUT);
  attachInterrupt(digitalPinToInterrupt(PIR_PIN), onMotion, RISING);
#endif

  snprintf(topicTelemetry, sizeof(topicTelemetry), "sensors/%s/telemetry", DEVICE_ID);
  snprintf(topicStatus, sizeof(topicStatus), "sensors/%s/status", DEVICE_ID);

  sensorOk = initSensor();
  connectWifi();
  syncTime();

#if MQTT_USE_TLS
  // Verify the broker against our own CA; the host name is checked against
  // the certificate's subjectAltName entries.
  net.setCACert(MQTT_CA_CERT);
#endif
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(MQTT_KEEPALIVE_S);
  mqtt.setBufferSize(MQTT_BUFFER_SIZE);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWifi();
  }
  if (connectMqtt()) {
    mqtt.loop();
    if (lastPublishMs == 0 || millis() - lastPublishMs >= PUBLISH_INTERVAL_MS) {
      lastPublishMs = millis();
      publishReading();
    }
  }
  delay(10);
}
