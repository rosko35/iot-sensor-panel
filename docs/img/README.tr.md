# Görseller

🇬🇧 [English](README.md)

Ana README'den referans verilen ekran görüntüleri ve kayıtlar. Çalışan bir
yığından elle yakalanırlar; hiçbiri üretilmiş ya da uydurulmuş değildir.

| Dosya | Ne gösteriyor | Nasıl yakalanır |
| --- | --- | --- |
| `dashboard-demo.gif` | Canlı güncellenen Grafana paneli, bir uyarıyı tetikleyip çözen sıcaklık sıçraması | `docker compose up`, "IoT Sensor Panel" panelinin ~30 sn'lik kaydı |
| `dashboard.webp` | Tam panel, cihaz filosuna genel bakış + bir cihaz satırı | aynı panelin ekran görüntüsü |
| `telegram-alert.png` | Telegram'da tetiklenen ve çözülen bir bildirim | `.env` içinde `TELEGRAM_*` ayarlarını yap |
| `wireshark-insecure.png` | Düz MQTT: okunabilir topic, JSON payload (ve parola) | [güvenlik demosu, Bölüm A](../security-demo.tr.md#bölüm-a---güvensiz-her-şey-açık-metin) |
| `wireshark-secure.png` | TLS handshake'i, ardından yalnızca `Application Data` | [güvenlik demosu, Bölüm B](../security-demo.tr.md#bölüm-b---güvenli-tls-kimlik-doğrulama-ve-acl) |
| `hardware.jpg` | Breadboard üzerinde ESP32 + DHT22/BME280 + PIR | fotoğraf |
