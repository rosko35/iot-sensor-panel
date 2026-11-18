"""Static checks for the deployment files that cannot be exercised without Docker."""

from __future__ import annotations

import json
import re

import pytest
import yaml

PINNED_IMAGE = re.compile(r"^[\w./-]+:\d+\.\d+(\.\d+)?[\w.-]*$")


def load_yaml(path):
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def compose(root):
    return load_yaml(root / "docker-compose.yml")


@pytest.fixture
def dashboard(root):
    with (root / "grafana/dashboards/iot-overview.json").open(encoding="utf-8") as fh:
        return json.load(fh)


def test_compose_has_all_services(compose):
    assert set(compose["services"]) == {"mosquitto", "influxdb", "grafana", "ingest", "simulator"}


def test_images_are_pinned(compose):
    for name, svc in compose["services"].items():
        if "build" in svc:
            continue
        assert PINNED_IMAGE.match(svc["image"]), f"{name} image not pinned: {svc['image']}"
        assert not svc["image"].endswith(":latest")


def test_influxdb_auto_setup(compose):
    env = compose["services"]["influxdb"]["environment"]
    assert env["DOCKER_INFLUXDB_INIT_MODE"] == "setup"
    for key in ("USERNAME", "PASSWORD", "ORG", "BUCKET", "ADMIN_TOKEN"):
        assert f"DOCKER_INFLUXDB_INIT_{key}" in env


def test_compose_bind_mounts_exist(root, compose):
    for name, svc in compose["services"].items():
        for volume in svc.get("volumes", []):
            source = volume.split(":", 1)[0]
            if source.startswith("./"):
                assert (root / source).exists(), f"{name}: missing {source}"


def test_grafana_datasource_provisioning(root):
    data = load_yaml(root / "grafana/provisioning/datasources/influxdb.yml")
    assert data["apiVersion"] == 1
    (ds,) = data["datasources"]
    assert ds["type"] == "influxdb"
    assert ds["uid"] == "influxdb"
    assert ds["jsonData"]["version"] == "Flux"
    assert ds["secureJsonData"]["token"] == "${INFLUX_TOKEN}"


def test_grafana_dashboard_provider(root):
    data = load_yaml(root / "grafana/provisioning/dashboards/dashboards.yml")
    (provider,) = data["providers"]
    assert provider["type"] == "file"
    assert provider["options"]["path"] == "/etc/grafana/dashboards"


def test_dashboard_structure(dashboard):
    assert dashboard["uid"] == "iot-sensor-overview"
    assert dashboard["refresh"] == "5s"
    variables = {v["name"]: v for v in dashboard["templating"]["list"]}
    assert variables["device"]["multi"] and variables["device"]["includeAll"]
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids)), "panel ids must be unique"
    repeated = [p for p in dashboard["panels"] if p.get("repeat") == "device"]
    assert repeated, "expected a row repeated per device"


def test_dashboard_queries_use_provisioned_datasource(dashboard):
    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            continue
        assert panel["datasource"]["uid"] == "influxdb"
        for target in panel["targets"]:
            assert target["datasource"]["uid"] == "influxdb"
            query = target["query"]
            assert query.startswith('from(bucket: "${bucket}")')
            assert query.count("(") == query.count(")"), panel["title"]
            assert query.count("[") == query.count("]"), panel["title"]


def test_dashboard_panels_fit_the_grid(dashboard):
    for panel in dashboard["panels"]:
        gp = panel["gridPos"]
        assert gp["x"] + gp["w"] <= 24, panel["title"]


def test_mosquitto_insecure_config(root):
    text = (root / "mosquitto/config/mosquitto.insecure.conf").read_text()
    directives = {line.split()[0]: line for line in text.splitlines() if line and line[0] != "#"}
    assert directives["listener"].split()[1] == "1883"
    assert directives["allow_anonymous"].split()[1] == "true"


# --------------------------------------------------------------------------- secure profile


class ComposeLoader(yaml.SafeLoader):
    """SafeLoader that understands Compose's merge tags (!override / !reset)."""


for _tag in ("!override", "!reset"):
    ComposeLoader.add_constructor(
        _tag,
        lambda loader, node: (
            loader.construct_sequence(node)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_mapping(node)
            if isinstance(node, yaml.MappingNode)
            else loader.construct_scalar(node)
        ),
    )


@pytest.fixture
def secure(root):
    with (root / "docker-compose.secure.yml").open(encoding="utf-8") as fh:
        return yaml.load(fh, Loader=ComposeLoader)


def conf_directives(path):
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition(" ")
            out.setdefault(key, []).append(value.strip())
    return out


def test_secure_override_only_exposes_tls_port(secure, root):
    raw = (root / "docker-compose.secure.yml").read_text(encoding="utf-8")
    assert "ports: !override" in raw
    assert secure["services"]["mosquitto"]["ports"] == ["8883:8883"]
    for name in ("influxdb", "grafana"):
        assert all(p.startswith("127.0.0.1:") for p in secure["services"][name]["ports"])


def test_secure_override_mounts_config_over_insecure_one(secure, compose):
    base_targets = {v.split(":")[1] for v in compose["services"]["mosquitto"]["volumes"]}
    mounts = secure["services"]["mosquitto"]["volumes"]
    targets = {m["target"]: m for m in mounts}
    assert "/mosquitto/config/mosquitto.conf" in base_targets & set(targets)
    assert targets["/mosquitto/config/mosquitto.conf"]["source"].endswith("mosquitto.secure.conf")
    for mount in mounts:
        assert mount["read_only"] is True
        assert mount["bind"]["create_host_path"] is False


def test_secure_clients_use_tls_and_credentials(secure):
    for name in ("ingest", "simulator"):
        env = secure["services"][name]["environment"]
        assert env["MQTT_TLS"] == "true"
        assert env["MQTT_PORT"] == "8883"
        assert env["MQTT_CA_CERT"] == "/certs/ca.crt"
    assert secure["services"]["ingest"]["environment"]["MQTT_PASSWORD"].startswith(
        "${MQTT_INGEST_PASSWORD:?"
    )
    assert secure["services"]["simulator"]["environment"]["SIM_PER_DEVICE_AUTH"] == "true"


def test_mosquitto_secure_config(root):
    d = conf_directives(root / "mosquitto/config/mosquitto.secure.conf")
    assert d["listener"] == ["8883 0.0.0.0"], "only the TLS listener may exist"
    assert d["allow_anonymous"] == ["false"]
    assert d["password_file"] == ["/mosquitto/config/passwd"]
    assert d["acl_file"] == ["/mosquitto/config/acl"]
    assert d["tls_version"] == ["tlsv1.2"]
    for key in ("cafile", "certfile", "keyfile"):
        assert d[key][0].startswith("/mosquitto/certs/")


def test_acl_restricts_devices_to_their_own_topics(root):
    lines = [
        line.strip()
        for line in (root / "mosquitto/config/acl").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    patterns = [line for line in lines if line.startswith("pattern")]
    assert patterns == ["pattern write sensors/%u/telemetry", "pattern write sensors/%u/status"]
    # no blanket write access to the sensor tree for anyone
    assert not any(re.match(r"topic (write|readwrite) sensors/(#|\+)", line) for line in lines)
    ingest = lines[lines.index("user ingest") + 1 : lines.index("user monitor")]
    assert "topic readwrite alerts/#" in ingest
    assert all("sensors" not in rule or rule.startswith("topic read ") for rule in ingest)


def test_generated_secrets_are_git_ignored(root):
    import shutil
    import subprocess

    if not shutil.which("git") or not (root / ".git").exists():
        pytest.skip("not a git checkout")
    paths = [
        ".env",
        "mosquitto/certs/ca.key",
        "mosquitto/certs/server.key",
        "mosquitto/certs/server.crt",
        "mosquitto/config/passwd",
        "firmware/include/secrets.h",
        "capture.pcapng",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", *paths],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert sorted(result.stdout.split()) == sorted(paths)
    tracked = subprocess.run(
        ["git", "ls-files", "mosquitto/certs"], cwd=root, capture_output=True, text=True
    ).stdout.split()
    assert tracked in ([], ["mosquitto/certs/.gitkeep"])
