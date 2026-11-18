# Donanım: ESP32 sensör düğümü

🇬🇧 [English](hardware.md)

[`firmware/`](../firmware) içindeki firmware, Python simülatörüyle birebir aynı
JSON payload'ı yayınlayan bir PlatformIO projesidir (Arduino framework'ü). Bu
sayede gerçek bir kart, backend'de hiçbir değişiklik yapmadan Grafana'da sanal
cihazların yanında görünür.

## Malzeme listesi (BOM)

| Parça | Notlar |
| --- | --- |
| ESP32 DevKit (ESP32-WROOM-32, `esp32dev`) | her klasik ESP32 kartı çalışır; diğerleri için pinleri ayarla |
| DHT22 / AM2302 **veya** BME280 breakout | DHT22: sıcaklık + nem. BME280: + basınç, daha hassas, I2C |
| HC-SR501 PIR modülü (isteğe bağlı) | hareket; takılı değilse `config.h` içinde `PIR_PIN` değerini `-1` yap |
| 10 kΩ direnç | DHT22 data pull-up'ı (birçok breakout kartında zaten var) |
| Breadboard, jumper kablolar, USB kablosu | |

## Bağlantılar

Tüm sensörler **3,3 V lojik** seviyesinde çalışır. Bir ESP32 GPIO'suna asla 5 V
verme.

### DHT22 (`env:esp32-dht22`)

| DHT22 pini | ESP32 pini | Notlar |
| --- | --- | --- |
| 1 VCC | 3V3 | |
| 2 DATA | GPIO 4 | DATA ile 3V3 arasında 10 kΩ pull-up |
| 3 NC | - | bağlı değil |
| 4 GND | GND | |

### BME280 (`env:esp32-bme280`)

| BME280 pini | ESP32 pini | Notlar |
| --- | --- | --- |
| VIN / VCC | 3V3 | 3,3 V'luk bir breakout ya da kart üzerinde regülatörü olan bir model kullan |
| GND | GND | |
| SDA | GPIO 21 | ESP32'nin varsayılan I2C SDA'sı |
| SCL | GPIO 22 | ESP32'nin varsayılan I2C SCL'si |
| SDO | GND veya 3V3 | GND -> adres `0x76` (varsayılan), 3V3 -> `0x77` |
| CSB | 3V3 | çıplak modüllerde I2C modunu seçer |

### HC-SR501 PIR (isteğe bağlı)

| PIR pini | ESP32 pini | Notlar |
| --- | --- | --- |
| VCC | 5V / VIN | modül 4,5-20 V besleme ister |
| OUT | GPIO 27 | çıkış 3,3 V'tur, ESP32 için güvenli |
| GND | GND | |

PIR jumper'ını *repeatable trigger* (H) konumuna getir ve gecikme
potansiyometresini sonuna kadar saat yönünün tersine çevir; firmware örnekler
arasındaki kısa darbeleri yakalayıp tutar (latch).

## Derleme ve yükleme

```bash
cd firmware
cp include/secrets.example.h include/secrets.h   # then edit it
pio run -e esp32-dht22 -t upload                 # or -e esp32-bme280
pio device monitor
```

`secrets.h` WiFi bilgilerini, `DEVICE_ID` değerini, broker adresini, MQTT
parolasını ve **CA sertifikasını** içerir (özel anahtarı (private key) asla).
Git tarafından yok sayılır (git-ignored).

### Insecure profil

```c
#define MQTT_USE_TLS 0
#define MQTT_USERNAME ""
```

### Secure profil

1. Broker'ın LAN adresini SAN listesine ekleyerek sertifikaları üret:
   `python scripts/gen_certs.py --ip 192.168.1.50 --dns my-pc.local`
2. Kartı parola dosyasına ekle:
   `python scripts/make_passwd.py --user esp32-kitchen` (parola sorar)
3. `mosquitto/certs/ca.crt` içeriğini `MQTT_CA_CERT` içine yapıştır,
   `MQTT_USE_TLS 1` yap, `MQTT_HOST` değerini sertifikada geçen bir adrese
   ayarla ve parolayı gir.

Firmware bağlanmadan önce saati NTP ile senkronlar; çünkü saati hâlâ 1970'i
gösteren bir cihazla sertifikanın geçerlilik süresi kontrol edilemez.

## Seri çıktı

```
[boot] esp32-kitchen firmware esp32-1.0.0
[wifi] connecting to my-ssid.... ok, ip=192.168.1.73 rssi=-58
[ntp] epoch=...
[mqtt] connecting to 192.168.1.50:8883 (tls=1) as esp32-kitchen
[mqtt] connected
[mqtt] -> sensors/esp32-kitchen/telemetry {"v":1,"device_id":"esp32-kitchen",...}
```

(Yukarıdaki satırlar biçimi göstermek içindir; yakalanmış gerçek bir log
değildir.)

## Bilinen sınırlamalar

- PubSubClient yalnızca QoS 0 ile publish eder; WiFi koptuğunda kaybolan bir
  okuma yeniden gönderilmez. Ingest servisi ve panel boşlukları tolere eder.
- Okumalar çevrimdışıyken tamponlanmaz.
- Firmware, bu deponun CI'ında gerçek donanıma yüklenmiş değildir; yalnızca
  `platformio.ini` içinde sabitlenmiş kütüphane sürümlerine karşı derlenir.
