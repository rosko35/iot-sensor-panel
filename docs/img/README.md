# Images

Screenshots and recordings referenced from the main README. They are captured
by hand from a running stack; none are generated or mocked.

| File | What it shows | How to capture |
| --- | --- | --- |
| `dashboard-demo.gif` | Grafana dashboard updating live, a temperature spike raising and clearing an alert | `docker compose up`, record ~30 s of the "IoT Sensor Panel" dashboard |
| `dashboard.png` | Full dashboard, fleet overview + one device row | screenshot of the same dashboard |
| `telegram-alert.png` | A raised and a resolved notification in Telegram | configure `TELEGRAM_*` in `.env` |
| `wireshark-insecure.png` | Plain MQTT: readable topic, JSON payload (and password) | [security demo, part A](../security-demo.md#part-a---insecure-everything-in-the-clear) |
| `wireshark-secure.png` | TLS handshake followed by `Application Data` only | [security demo, part B](../security-demo.md#part-b---secure-tls-authentication-and-acl) |
| `hardware.jpg` | ESP32 + DHT22/BME280 + PIR on a breadboard | photo |
