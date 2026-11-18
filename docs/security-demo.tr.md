# Güvenlik demosu: düz MQTT ve TLS + kimlik doğrulama + ACL

🇬🇧 [English](security-demo.md)

Bu anlatım, aynı ağdaki bir saldırganın varsayılan **insecure** kurulumda neler
gördüğünü ve **secure** override ile neyin değiştiğini bir paket yakalamasıyla
gösterir. README'de referans verilen iki ekran görüntüsünü üretir
(`docs/img/wireshark-insecure.png`, `docs/img/wireshark-secure.png`).

> Ekran görüntüleri henüz depoya eklenmedi - aşağıdaki adımlarla kendi
> bilgisayarında al. Bu belgedeki hiçbir şey yakalanmış bir log değildir;
> "ne görmelisin" bölümleri beklenen protokol davranışını anlatır.

Asıl iddianın otomatik bir sürümü de var: `tests/test_tls_wire.py`, gerçek MQTT
istemci kodu ile sahte bir broker arasına kayıt yapan bir TCP proxy koyar ve
parolanın ile telemetrinin yakalanan baytlarda düz MQTT'de görünür, TLS'de ise
görünmez olduğunu doğrular.

## Neler değişiyor

| | `insecure` (varsayılan) | `secure` (override) |
| --- | --- | --- |
| Broker portu | 1883, düz TCP | 8883, yalnızca TLS 1.2+ (1883 yayınlanmaz) |
| Kimlik doğrulama | anonim | kullanıcı adı + parola (PBKDF2-SHA512 hash'leri) |
| Yetkilendirme | herkes her şeyi publish/subscribe edebilir | ACL: bir cihaz yalnızca `sensors/<kendi id'si>/...` topic'lerine yazabilir |
| Broker sertifikası | - | yerel bir CA tarafından imzalı, host adı istemciler tarafından doğrulanır |
| InfluxDB / Grafana portları | tüm arayüzler | yalnızca `127.0.0.1` |
| Ingest'te payload kontrolü | `device_id` topic ile eşleşmeli | aynı (derinlemesine savunma) |

## Araçlar

- [Wireshark](https://www.wireshark.org/) (GUI) veya `tcpdump` (CLI).
- Mosquitto komut satırı istemcileri. Kurulu değillerse broker image'ından
  çalıştırabilirsin:
  `docker run --rm -it --network iot-sensor-panel_default eclipse-mosquitto:2.0.20 mosquitto_sub ...`

### Nerede yakalanmalı

Container'lar arasındaki trafik (simülatör -> broker) Docker'ın sanal ağından
asla çıkmaz; bu yüzden Wi-Fi adaptöründe yapılan bir yakalamada görünmez.

- **Linux host:** Docker bridge'inde ya da `any` üzerinde yakala:
  `sudo tcpdump -i any -nn -A 'tcp port 1883'`
- **Docker Desktop (Windows/macOS):** broker'ın network namespace'ine bir
  yakalama container'ı bağla ve Wireshark'ta açacağın bir pcap dosyası yaz:

  ```bash
  docker run --rm -it --net container:iot-sensor-panel-mosquitto-1 \
    -v "$PWD:/cap" nicolaka/netshoot:v0.13 \
    tcpdump -i any -nn -w /cap/insecure.pcapng 'tcp port 1883'
  ```

  (`*.pcap`/`*.pcapng` dosyaları git tarafından yok sayılır.)
- **LAN'ındaki gerçek bir ESP32:** yayınlanan porta giden trafiği host'tan
  *geçer*, dolayısıyla host'un LAN arayüzünde normal bir Wireshark yakalaması
  işe yarar.

## Bölüm A - güvensiz: her şey açık metin

1. Varsayılan yığını başlat: `docker compose up --build -d`
2. **Yakalama filtresi** `tcp port 1883` ile bir yakalama başlat
   (Wireshark: *Capture -> Options -> capture filter*, ya da yukarıdaki komutlardan biri).
3. ~30 sn çalışmasına izin ver, sonra durdur.
4. Wireshark'ta **görüntüleme filtresi** olarak `mqtt` uygula. Wireshark, 1883
   portundaki MQTT'yi otomatik olarak çözümler.

Görmen gerekenler:

- Her simüle cihazın client id'sini (`sim-living-room`, ...) ve last-will
  topic'ini `sensors/<id>/status` içeren `Connect Command` paketleri.
- `Publish Message` paketleri: *MQ Telemetry Transport Protocol* kısmını
  genişlettiğinde topic (`sensors/sim-server-room/telemetry`) ve sıcaklık, nem,
  hareket ile RSSI içeren tam JSON payload okunabilir durumdadır.
- *Sağ tık -> Follow -> TCP Stream* JSON'u düz metin olarak gösterir.

### A1. Kimlik bilgileri de aynı derecede görünür

TLS olmadan kimlik doğrulama, dinlemeye (sniffing) karşı işe yaramaz. Yakalama
sürerken insecure broker'a kullanıcı adı ve parola ile bir mesaj gönder:

```bash
mosquitto_pub -h localhost -p 1883 -u sim-garage -P 'hunter2-not-so-secret' \
  -t sensors/sim-garage/telemetry -m '{"device_id":"sim-garage","temperature":12.3}'
```

`Connect Command` paketinde *User Name* ve *Password* alanları olduğu gibi
görünür.

### A2. Herkes veri enjekte edebilir

Bir cihazın kimliğine bürünüp uyarı tetiklemek için kimlik bilgisi gerekmez:

```bash
mosquitto_pub -h localhost -p 1883 -t sensors/sim-server-room/telemetry \
  -m '{"device_id":"sim-server-room","temperature":45.0}'
```

Ingest servisi bunu kabul eder (topic ve payload uyuşuyor), `temp_high` uyarısı
tetiklenir ve Grafana hiç yaşanmamış 45 °C'lik bir sıçrama gösterir.

**Ekran görüntüsü 1 (`docs/img/wireshark-insecure.png`):** `mqtt` ile
filtrelenmiş paket listesi; bir `Publish Message` genişletilmiş olmalı ki topic
ve JSON payload görünsün (isteğe bağlı olarak A1'deki CONNECT paketi, parola
alanıyla birlikte).

## Bölüm B - güvenli: TLS, kimlik doğrulama ve ACL

1. Insecure yığını durdur: `docker compose down`
2. Kimlik bilgilerini ve sertifikaları oluştur (hepsi git tarafından yok sayılır):

   ```bash
   cp .env.example .env          # then replace every change-me-* password
   python scripts/gen_certs.py   # or: scripts/gen_certs.sh
   python scripts/make_passwd.py --from-env .env
   ```

   Linux host'ta broker container içinde uid 1883 olarak çalışır; dosyaları
   okuyamazsa
   `sudo chown 1883:1883 mosquitto/certs/server.key mosquitto/config/passwd`
   komutunu çalıştır.
3. Secure yığını başlat:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.secure.yml up --build -d
   ```

4. `tcp port 8883` yakalama filtresiyle yakala (yukarıdaki komutlarda `1883`
   yerine `8883` yaz, örneğin `-w /cap/secure.pcapng 'tcp port 8883'`).
5. `mqtt` görüntüleme filtresi artık **hiçbir şey** göstermez. Onun yerine
   `tls` kullan.

Görmen gerekenler:

- Her bağlantı için bir TLS handshake: `Client Hello` (SNI uzantısı broker'ın
  host adını, `mosquitto`, içerir), `Server Hello` ve - TLS 1.2 ile - *IoT
  Sensor Panel Local CA* tarafından verilmiş broker sertifikası. TLS 1.3'te
  sertifika zaten şifrelidir.
- Handshake'ten sonra yalnızca `Application Data` kayıtları. *Follow -> TLS
  Stream* rastgele baytlar gösterir; topic, JSON ya da parola yok.
- Hâlâ görünenler: IP adresleri, portlar, zamanlama ve kayıt boyutları. TLS
  içeriği gizler; bir cihazın birkaç saniyede bir broker ile konuştuğu
  gerçeğini gizlemez.

**Ekran görüntüsü 2 (`docs/img/wireshark-secure.png`):** ekran görüntüsü 1 ile
aynı görünüm, `tls` ile filtrelenmiş; handshake'i ve ardından gelen
`Application Data` kayıtlarını gösterir.

### B1. Düz metin ve anonim erişim ortadan kalktı

```bash
# no plain-text listener: connection refused
mosquitto_sub -h localhost -p 1883 -t '#' -v

# TLS but no credentials: "Connection error: Connection Refused: not authorised."
mosquitto_sub -h localhost -p 8883 --cafile mosquitto/certs/ca.crt -t '#' -v
```

Bizim CA'mıza güvenmeyen bir istemci (örneğin `--cafile` yerine
`--capath /etc/ssl/certs` kullanan) TLS handshake'inde sertifika doğrulama
hatası alır; yani kendi sertifikasıyla araya giren bir man-in-the-middle de
reddedilir.

### B2. ACL, ele geçirilmiş bir cihazı sınırlar

Bir cihaz olarak giriş yap ve başka bir cihaz adına publish etmeyi dene:

```bash
mosquitto_pub -h localhost -p 8883 --cafile mosquitto/certs/ca.crt \
  -u sim-garage -P "$SIM_DEVICE_PASSWORD" -V mqttv5 -q 1 -d \
  -t sensors/sim-server-room/telemetry \
  -m '{"device_id":"sim-server-room","temperature":45.0}'
```

MQTT v5 ve QoS 1 ile broker, publish'e reason code'u 135 (*Not authorized*) olan
bir `PUBACK` ile yanıt verir; bu, `-d` debug çıktısında görünür ve broker logu
reddedilen publish'i bildirir. MQTT 3.1.1'de mesaj sessizce düşürülür
(protokolün bunu bildirmenin bir yolu yoktur). Her iki durumda da ingest
servisine hiçbir şey ulaşmaz ve aynı cihaz kendi topic'ine
`sensors/sim-garage/telemetry` yine publish edebilir.

Salt okunur `monitor` hesabı her şeyi izleyebilir ama hiçbir şey publish
edemez:

```bash
mosquitto_sub -h localhost -p 8883 --cafile mosquitto/certs/ca.crt \
  -u monitor -P "$MQTT_MONITOR_PASSWORD" -t 'sensors/#' -t 'alerts/#' -v
```

## Sınırlar ve sonraki adımlar

- **Ortak simülatör parolası.** Tüm simüle cihazlar kolaylık olsun diye
  `SIM_DEVICE_PASSWORD` parolasını paylaşır; her biri kendi kullanıcı adıyla
  giriş yaptığı için ACL yine cihaz başına uygulanır. Gerçek cihazların her
  birine ayrı parola verilmeli (`make_passwd.py --user <id>`).
- **İstemci sertifikaları (mTLS)** cihazlardaki parolaların yerini alır ve flash
  dökümünden kimlik bilgisi çalmayı daha az işe yarar hale getirir; Mosquitto
  bunu `require_certificate true` ve `use_identity_as_username true` ile
  destekler.
- **ESP32'de kimlik bilgisi saklama.** `secrets.h` firmware'e düz metin olarak
  derlenir. ESP32'deki flash encryption ve secure boot asıl çözümdür ve bu
  projenin kapsamı dışındadır.
- **InfluxDB ve Grafana** Docker ağı içinde hâlâ düz HTTP konuşur; secure
  profilde host üzerinde yalnızca `127.0.0.1`'e bağlıdırlar. Dışarı açmadan önce
  önlerine TLS'li bir reverse proxy koy.
- **Sertifika yaşam döngüsü.** Yerel CA 10 yıl, sunucu sertifikası 825 gün
  geçerlidir; yenileme (rotation) ya da iptal (revocation) yoktur. Sunucu
  sertifikasını yeniden vermek için `gen_certs.py` komutunu tekrar çalıştır
  (CA'yı korur).
