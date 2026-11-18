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
