# Security demo: plain MQTT vs. TLS + auth + ACL

🇹🇷 [Türkçe](security-demo.tr.md)

This walkthrough shows, with a packet capture, what an attacker on the same
network sees in the default **insecure** setup and what changes with the
**secure** override. It produces the two screenshots referenced in the README
(`docs/img/wireshark-insecure.png`, `docs/img/wireshark-secure.png`).

> The screenshots are not checked in yet - take them on your own machine with
> the steps below. Nothing in this document is a captured log; the "what you
> should see" sections describe the expected protocol behaviour.

There is also an automated version of the core claim: `tests/test_tls_wire.py`
puts a recording TCP proxy between the real MQTT client code and a fake broker
and asserts that the password and telemetry are visible in the captured bytes
over plain MQTT and absent over TLS.

## What changes

| | `insecure` (default) | `secure` (override) |
| --- | --- | --- |
| Broker port | 1883, plain TCP | 8883, TLS 1.2+ only (1883 not published) |
| Authentication | anonymous | username + password (PBKDF2-SHA512 hashes) |
| Authorization | anyone can publish/subscribe anything | ACL: a device can only write `sensors/<its id>/...` |
| Broker certificate | - | signed by a local CA, host name verified by clients |
| InfluxDB / Grafana ports | all interfaces | `127.0.0.1` only |
| Payload check in ingest | `device_id` must match topic | same (defence in depth) |

## Tools

- [Wireshark](https://www.wireshark.org/) (GUI) or `tcpdump` (CLI).
- Mosquitto command-line clients. If you do not have them installed, run them
  from the broker image:
  `docker run --rm -it --network iot-sensor-panel_default eclipse-mosquitto:2.0.20 mosquitto_sub ...`

### Where to capture

Traffic between containers (simulator -> broker) never leaves Docker's virtual
network, so a capture on your Wi-Fi adapter will not show it.

- **Linux host:** capture on the Docker bridge or `any`:
  `sudo tcpdump -i any -nn -A 'tcp port 1883'`
- **Docker Desktop (Windows/macOS):** attach a capture container to the
  broker's network namespace and write a pcap you then open in Wireshark:

  ```bash
  docker run --rm -it --net container:iot-sensor-panel-mosquitto-1 \
    -v "$PWD:/cap" nicolaka/netshoot:v0.13 \
    tcpdump -i any -nn -w /cap/insecure.pcapng 'tcp port 1883'
  ```

  (`*.pcap`/`*.pcapng` files are git-ignored.)
- **Real ESP32 on your LAN:** its traffic to the published port *does* cross
  the host, so a normal Wireshark capture on the host's LAN interface works.

## Part A - insecure: everything in the clear

1. Start the default stack: `docker compose up --build -d`
2. Start a capture with the **capture filter** `tcp port 1883`
   (Wireshark: *Capture -> Options -> capture filter*, or one of the commands above).
3. Let it run for ~30 s, then stop it.
4. In Wireshark apply the **display filter** `mqtt`. Wireshark dissects MQTT on
   port 1883 automatically.

What you should see:

- `Connect Command` packets with the client id of every simulated device
  (`sim-living-room`, ...) and the last-will topic `sensors/<id>/status`.
- `Publish Message` packets: expand *MQ Telemetry Transport Protocol* and the
  topic (`sensors/sim-server-room/telemetry`) and the full JSON payload with
  temperature, humidity, motion and RSSI are readable.
- *Right click -> Follow -> TCP Stream* shows the JSON as plain text.

### A1. Credentials are just as visible

Authentication without TLS does not help against sniffing. Send a message with
a username and password to the insecure broker while capturing:

```bash
mosquitto_pub -h localhost -p 1883 -u sim-garage -P 'hunter2-not-so-secret' \
  -t sensors/sim-garage/telemetry -m '{"device_id":"sim-garage","temperature":12.3}'
```

In the `Connect Command` packet, the *User Name* and *Password* fields are shown
verbatim.

### A2. Anyone can inject data

No credentials are needed to spoof a device and trigger an alert:

```bash
mosquitto_pub -h localhost -p 1883 -t sensors/sim-server-room/telemetry \
  -m '{"device_id":"sim-server-room","temperature":45.0}'
```

The ingest service accepts it (topic and payload agree), the `temp_high` alert
fires and Grafana shows a 45 °C spike that never happened.

**Screenshot 1 (`docs/img/wireshark-insecure.png`):** the packet list filtered by
`mqtt` with one `Publish Message` expanded so topic and JSON payload are visible
(optionally the CONNECT from A1 with the password field).

## Part B - secure: TLS, authentication and ACL

1. Stop the insecure stack: `docker compose down`
2. Create credentials and certificates (all git-ignored):

   ```bash
   cp .env.example .env          # then replace every change-me-* password
   python scripts/gen_certs.py   # or: scripts/gen_certs.sh
   python scripts/make_passwd.py --from-env .env
   ```

   On a Linux host the broker runs as uid 1883 inside the container; if it
   cannot read the files, run
   `sudo chown 1883:1883 mosquitto/certs/server.key mosquitto/config/passwd`.
3. Start the secure stack:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.secure.yml up --build -d
   ```

4. Capture with the capture filter `tcp port 8883` (change `1883` to `8883` in
   the commands above, e.g. `-w /cap/secure.pcapng 'tcp port 8883'`).
5. Display filter `mqtt` now shows **nothing**. Use `tls` instead.

What you should see:

- A TLS handshake per connection: `Client Hello` (the SNI extension contains
  the broker host name, `mosquitto`), `Server Hello`, and - with TLS 1.2 - the
  broker certificate issued by *IoT Sensor Panel Local CA*. With TLS 1.3 the
  certificate is already encrypted.
- After the handshake only `Application Data` records. *Follow -> TLS Stream*
  shows random bytes; no topic, no JSON, no password.
- What is still visible: IP addresses, ports, timing and record sizes. TLS
  hides content, not the fact that a device talks to the broker every few
  seconds.

**Screenshot 2 (`docs/img/wireshark-secure.png`):** the same view as screenshot 1,
filtered by `tls`, showing the handshake followed by `Application Data`.

### B1. Plain-text and anonymous access are gone

```bash
# no plain-text listener: connection refused
mosquitto_sub -h localhost -p 1883 -t '#' -v

# TLS but no credentials: "Connection error: Connection Refused: not authorised."
mosquitto_sub -h localhost -p 8883 --cafile mosquitto/certs/ca.crt -t '#' -v
```

A client that does not trust our CA (for example `--capath /etc/ssl/certs`
instead of `--cafile`) fails the TLS handshake with a certificate
verification error, so a man-in-the-middle with its own certificate is
rejected as well.

### B2. The ACL contains a compromised device

Log in as one device and try to publish for another:

```bash
mosquitto_pub -h localhost -p 8883 --cafile mosquitto/certs/ca.crt \
  -u sim-garage -P "$SIM_DEVICE_PASSWORD" -V mqttv5 -q 1 -d \
  -t sensors/sim-server-room/telemetry \
  -m '{"device_id":"sim-server-room","temperature":45.0}'
```

With MQTT v5 and QoS 1 the broker answers the publish with a `PUBACK` whose
reason code is 135 (*Not authorized*), visible in the `-d` debug output; the
broker log reports the denied publish. With MQTT 3.1.1 the message is silently
dropped (the protocol has no way to report it). Either way nothing reaches the
ingest service, and the same device can still publish to its own topic
`sensors/sim-garage/telemetry`.

The read-only `monitor` account can watch everything but publish nothing:

```bash
mosquitto_sub -h localhost -p 8883 --cafile mosquitto/certs/ca.crt \
  -u monitor -P "$MQTT_MONITOR_PASSWORD" -t 'sensors/#' -t 'alerts/#' -v
```

## Limits and next steps

- **Shared simulator password.** All simulated devices share
  `SIM_DEVICE_PASSWORD` for convenience; the ACL still applies per device
  because each logs in with its own username. Real devices should each get
  their own password (`make_passwd.py --user <id>`).
- **Client certificates (mTLS)** would replace passwords on devices and make
  credential theft from a flash dump less useful; Mosquitto supports this with
  `require_certificate true` and `use_identity_as_username true`.
- **Credential storage on the ESP32.** `secrets.h` is compiled into the
  firmware in plain text. Flash encryption and secure boot on the ESP32 are the
  proper fix and are out of scope here.
- **InfluxDB and Grafana** still speak plain HTTP inside the Docker network; in
  the secure profile they are only bound to `127.0.0.1` on the host. Put a
  reverse proxy with TLS in front of them before exposing them.
- **Certificate lifecycle.** The local CA is valid for 10 years and the server
  certificate for 825 days; there is no rotation or revocation. Re-run
  `gen_certs.py` (it keeps the CA) to re-issue the server certificate.
