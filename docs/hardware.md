# Hardware: ESP32 sensor node

The firmware in [`firmware/`](../firmware) is a PlatformIO project (Arduino
framework) that publishes exactly the same JSON payload as the Python
simulator, so a real board shows up in Grafana next to the virtual devices
without any change to the backend.

## Bill of materials

| Part | Notes |
| --- | --- |
| ESP32 DevKit (ESP32-WROOM-32, `esp32dev`) | any classic ESP32 board works; adjust pins for others |
| DHT22 / AM2302 **or** BME280 breakout | DHT22: temperature + humidity. BME280: + pressure, more accurate, I2C |
| HC-SR501 PIR module (optional) | motion; set `PIR_PIN` to `-1` in `config.h` if not fitted |
| 10 kΩ resistor | DHT22 data pull-up (many breakout boards already have one) |
| Breadboard, jumper wires, USB cable | |

## Wiring

All sensors run at **3.3 V logic**. Never feed 5 V into an ESP32 GPIO.

### DHT22 (`env:esp32-dht22`)

| DHT22 pin | ESP32 pin | Notes |
| --- | --- | --- |
| 1 VCC | 3V3 | |
| 2 DATA | GPIO 4 | 10 kΩ pull-up between DATA and 3V3 |
| 3 NC | - | not connected |
| 4 GND | GND | |

### BME280 (`env:esp32-bme280`)

| BME280 pin | ESP32 pin | Notes |
| --- | --- | --- |
| VIN / VCC | 3V3 | use a 3.3 V breakout or one with an on-board regulator |
| GND | GND | |
| SDA | GPIO 21 | default ESP32 I2C SDA |
| SCL | GPIO 22 | default ESP32 I2C SCL |
| SDO | GND or 3V3 | GND -> address `0x76` (default), 3V3 -> `0x77` |
| CSB | 3V3 | selects I2C mode on bare modules |

### HC-SR501 PIR (optional)

| PIR pin | ESP32 pin | Notes |
| --- | --- | --- |
| VCC | 5V / VIN | the module needs 4.5-20 V supply |
| OUT | GPIO 27 | output is 3.3 V, safe for the ESP32 |
| GND | GND | |

Set the PIR jumper to *repeatable trigger* (H) and turn the delay potentiometer
fully counter-clockwise; the firmware latches short pulses between samples.

## Build and flash

```bash
cd firmware
cp include/secrets.example.h include/secrets.h   # then edit it
pio run -e esp32-dht22 -t upload                 # or -e esp32-bme280
pio device monitor
```

`secrets.h` holds the WiFi credentials, `DEVICE_ID`, broker address, MQTT
password and the **CA certificate** (never a private key). It is git-ignored.

### Insecure profile

```c
#define MQTT_USE_TLS 0
#define MQTT_USERNAME ""
```

### Secure profile

1. Generate certificates with the broker's LAN address in the SAN list:
   `python scripts/gen_certs.py --ip 192.168.1.50 --dns my-pc.local`
2. Add the board to the password file:
   `python scripts/make_passwd.py --user esp32-kitchen` (prompts for a password)
3. Paste `mosquitto/certs/ca.crt` into `MQTT_CA_CERT`, set `MQTT_USE_TLS 1`,
   `MQTT_HOST` to an address that appears in the certificate, and the password.

The firmware syncs time over NTP before connecting because certificate
validity periods cannot be checked with a clock that still says 1970.

## Serial output

```
[boot] esp32-kitchen firmware esp32-1.0.0
[wifi] connecting to my-ssid.... ok, ip=192.168.1.73 rssi=-58
[ntp] epoch=...
[mqtt] connecting to 192.168.1.50:8883 (tls=1) as esp32-kitchen
[mqtt] connected
[mqtt] -> sensors/esp32-kitchen/telemetry {"v":1,"device_id":"esp32-kitchen",...}
```

(The lines above illustrate the format; they are not a captured log.)

## Known limitations

- PubSubClient only publishes with QoS 0; a reading lost during a WiFi drop is
  not retransmitted. The ingest service and dashboard tolerate gaps.
- Readings are not buffered while offline.
- The firmware has not been flashed onto hardware as part of this repository's
  CI; it is built against the library versions pinned in `platformio.ini`.
