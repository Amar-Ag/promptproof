"""The local telemetry stack, and the switch that routes signals into it.

The deployed environments export to Logfire, which needs an account and a
token. Anyone who clones this repository to look at it has neither, so
"the signals are collected, stored, and viewable in a UI" would have rested on
credentials only the author holds. `make obs-up` is the reproducible path.

Three things have to hold for it to work, and none of them is obvious from
reading any single file:

- the stack must stay behind its profile, or `make up` silently costs four
  more containers;
- the pieces must agree on ports, since a collector exporting to a port Tempo
  is not listening on fails quietly and looks like "no traces yet";
- an explicitly configured endpoint must beat an ambient Logfire token, or the
  stack does nothing for exactly the people who already have a token.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from promptproof_core.metrics import _build_exporter as build_metric_exporter
from promptproof_core.settings import Settings
from promptproof_core.telemetry import _build_exporter as build_span_exporter

ROOT = Path(__file__).resolve().parents[2]
OBS = ROOT / "observability"

TELEMETRY_SERVICES = ("otel-collector", "tempo", "prometheus", "grafana")


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose() -> Any:
    return load_yaml(ROOT / "compose.yaml")


class TestItStaysOptional:
    @pytest.mark.parametrize("service", TELEMETRY_SERVICES)
    def test_every_piece_is_behind_the_telemetry_profile(self, compose: Any, service: str) -> None:
        """`make up` must not quietly become a nine-container stack."""
        assert service in compose["services"], f"{service} is missing from compose"
        profiles = compose["services"][service].get("profiles")
        assert profiles == ["telemetry"], (
            f"{service} has profiles {profiles!r}; it would start with `make up`"
        )

    def test_the_application_services_are_not_behind_a_profile(self, compose: Any) -> None:
        """The guard above is only meaningful if the app itself is unprofiled."""
        for service in ("api", "worker", "db", "web"):
            assert "profiles" not in compose["services"][service], (
                f"{service} became optional, so `make up` no longer starts the app"
            )


class TestThePiecesAgree:
    def test_the_collector_exports_where_tempo_listens(self) -> None:
        """A mismatch here fails silently and reads as "no traces yet"."""
        collector = load_yaml(OBS / "otel-collector.yaml")
        tempo = load_yaml(OBS / "tempo.yaml")

        target = collector["exporters"]["otlp/tempo"]["endpoint"]
        listening = tempo["distributor"]["receivers"]["otlp"]["protocols"]["grpc"]["endpoint"]

        host, _, port = target.rpartition(":")
        assert host == "tempo", f"collector exports to {host!r}, not the tempo service"
        assert listening.endswith(f":{port}"), (
            f"collector sends traces to port {port}, tempo listens on {listening}"
        )

    def test_prometheus_scrapes_the_collector(self) -> None:
        collector = load_yaml(OBS / "otel-collector.yaml")
        prometheus = load_yaml(OBS / "prometheus.yml")

        exposed = collector["exporters"]["prometheus"]["endpoint"]
        _, _, exposed_port = exposed.rpartition(":")
        targets = prometheus["scrape_configs"][0]["static_configs"][0]["targets"]

        assert targets == [f"otel-collector:{exposed_port}"], (
            f"prometheus scrapes {targets}, the collector exposes :{exposed_port}"
        )

    def test_the_collector_keeps_resource_attributes(self) -> None:
        """Without this, every metric series loses service.name and looks as
        though it came from nowhere."""
        collector = load_yaml(OBS / "otel-collector.yaml")
        conversion = collector["exporters"]["prometheus"]["resource_to_telemetry_conversion"]
        assert conversion["enabled"] is True


class TestGrafanaFindsItsData:
    def test_the_dashboard_only_references_provisioned_datasources(self) -> None:
        """A dashboard pointing at a uid Grafana does not have renders as a row
        of broken panels, which is worse than no dashboard."""
        datasources = load_yaml(OBS / "grafana/provisioning/datasources/datasources.yaml")
        known = {d["uid"] for d in datasources["datasources"]}

        dashboard = json.loads(
            (OBS / "grafana/dashboards/scan-journey.json").read_text(encoding="utf-8")
        )
        referenced = {
            panel["datasource"]["uid"]
            for panel in dashboard["panels"]
            if isinstance(panel.get("datasource"), dict)
        }

        assert referenced, "no panel names a datasource"
        assert referenced <= known, f"dashboard references unknown uids: {referenced - known}"

    def test_the_provisioned_path_is_the_mounted_path(self, compose: Any) -> None:
        provider = load_yaml(OBS / "grafana/provisioning/dashboards/dashboards.yaml")
        declared = provider["providers"][0]["options"]["path"]
        mounts = compose["services"]["grafana"]["volumes"]
        assert any(m.split(":")[1] == declared for m in mounts), (
            f"dashboards are provisioned from {declared}, which nothing mounts"
        )

    def test_grafana_needs_no_login(self, compose: Any) -> None:
        """A reviewer with twenty minutes should not meet a password prompt on a
        stack holding nothing but their own test traces."""
        env = compose["services"]["grafana"]["environment"]
        assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "true"
        assert env["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Admin"

    def test_the_dashboard_queries_metrics_this_app_emits(self) -> None:
        """Guards against a dashboard written against invented metric names."""
        source = (ROOT / "packages/core/src/promptproof_core/metrics.py").read_text(
            encoding="utf-8"
        )
        emitted = {
            name.replace("promptproof.", "").replace(".", "_")
            for name in re.findall(r'"(promptproof\.[a-z_.]+)"', source)
        }

        # Only the queries, not the whole file: the text panel mentions source
        # paths like `promptproof_core/alerts.py`, which are not metric names.
        dashboard = json.loads(
            (OBS / "grafana/dashboards/scan-journey.json").read_text(encoding="utf-8")
        )
        expressions = " ".join(
            target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])
        )
        assert expressions, "no panel carries a query"
        queried = set(re.findall(r"promptproof_([a-z_]+)", expressions))

        # A Prometheus series carries the suffixes OTel appends (`_total`,
        # `_bucket`, a unit name), so an emitted name being a prefix is a match.
        unknown = {q for q in queried if not any(q.startswith(n) for n in emitted)}
        assert not unknown, (
            f"dashboard queries metrics that are never emitted: {sorted(unknown)}; "
            f"emitted: {sorted(emitted)}"
        )


class TestExporterPrecedence:
    """An explicit endpoint beats an ambient token.

    The token usually sits in a `.env` from months ago; the endpoint means
    someone has just stood up a collector and pointed this process at it.
    Checking the token first meant `make obs-up` showed nothing at all to
    anyone who already had a token — the one audience the stack is for.
    """

    @staticmethod
    def settings_with(**kwargs: Any) -> Settings:
        # Stated explicitly rather than left to default: Settings reads `.env`,
        # so a developer's real Logfire token would otherwise leak into these
        # cases and "neither configured" would pass or fail by accident.
        base: dict[str, Any] = {
            "groq_api_key": "test-key",
            "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
            "logfire_token": "",
            "otel_exporter_otlp_endpoint": "",
        }
        base.update(kwargs)
        return Settings(**base)

    def test_traces_prefer_the_configured_endpoint(self) -> None:
        exporter = build_span_exporter(
            self.settings_with(
                logfire_token="pylf_v1_token",
                otel_exporter_otlp_endpoint="http://otel-collector:4318",
            )
        )
        assert exporter is not None
        assert "otel-collector" in exporter._endpoint
        assert "logfire" not in exporter._endpoint

    def test_metrics_prefer_the_configured_endpoint(self) -> None:
        exporter = build_metric_exporter(
            self.settings_with(
                logfire_token="pylf_v1_token",
                otel_exporter_otlp_endpoint="http://otel-collector:4318",
            )
        )
        assert exporter is not None
        assert "otel-collector" in exporter._endpoint

    def test_logfire_is_still_used_when_no_endpoint_is_set(self) -> None:
        """The deployed environments rely on this path."""
        exporter = build_span_exporter(self.settings_with(logfire_token="pylf_v1_token"))
        assert exporter is not None
        assert "logfire" in exporter._endpoint

    def test_neither_configured_means_no_exporter(self) -> None:
        """Telemetry is optional; the app must run with none of it."""
        assert build_span_exporter(self.settings_with()) is None
        assert build_metric_exporter(self.settings_with()) is None
