"""The QoS endpoints: current telemetry, unsupported/stale/failed states, and history."""

import pytest
from fastapi.testclient import TestClient

from helpers import FakeClock
from netmonlt.api import create_app
from netmonlt.domain import Device, QosConfig, SimProfile, SimQosClass, SimQosProfile, SnmpConfig
from netmonlt.inventory import Inventory
from netmonlt.probes import SimulatedProber
from netmonlt.qos import QosRow, QosSnapshot, advance_device
from netmonlt.qos_poller import qos_stale_after_s

NOW = 10_000.0
INTERVAL = 5.0
SIM_QOS = SimQosProfile(classes=(SimQosClass("a", 5),))
SECRET = "SECRET-sentinel-value-9f3a"


def sim_devices():
    return [Device("alpha", "192.0.2.1", sim=SimProfile(qos=SIM_QOS)),
            Device("beta", "192.0.2.2")]


def real_devices():
    snmp = SnmpConfig("NETMONLT_SNMP_R1")
    return [Device("alpha", "192.0.2.1", authorized=True, snmp=snmp, qos=QosConfig("diffserv-mib")),
            Device("beta", "192.0.2.2", authorized=True, snmp=snmp)]


def client_for(db, devices, *, mode="simulated", now=NOW):
    inventory = Inventory(mode=mode, poll_interval_s=INTERVAL, devices=tuple(devices),
                          qos_poll_interval_s=INTERVAL)
    app = create_app(inventory, db, SimulatedProber(), start_collector=False, clock=FakeClock(now))
    return TestClient(app)


def row(key="count:1", kind="traffic", pkts=0, octs=0, dpkts=None, docts=None, bits=64):
    return QosRow(key, kind, key.upper(), f"{key} detail", pkts, octs, dpkts, docts, bits)


def store(db, device, snapshots, *, end=NOW, step=INTERVAL, adapter="diffserv-mib",
          source="DIFFSERV-MIB (RFC 3289)"):
    """Poll `snapshots` (lists of rows) every `step` seconds, the last one at `end`."""
    states, uptime = {}, None
    for i, rows in enumerate(snapshots):
        ts = end - (len(snapshots) - 1 - i) * step
        snapshot = QosSnapshot(adapter, source, tuple(rows), 1000 + 100 * i)
        new = advance_device(states, uptime, snapshot, ts, qos_stale_after_s(INTERVAL))
        states = {s.key: s for s in new}
        uptime = snapshot.uptime_ticks
        db.save_qos_success(device, ts, snapshot, new)


def ramp(n=6, pps=10, dpps=0):
    """n polls of one traffic element growing at `pps` packets/s and 100 B per packet."""
    return [[row(pkts=i * pps * 5, octs=i * pps * 500, dpkts=i * dpps * 5, docts=i * dpps * 500)]
            for i in range(n)]


# ------------------------------------------------------------------ enabled or not
def test_with_no_qos_devices_the_api_says_so(db, devices):
    db.sync_devices(devices)
    with client_for(db, devices) as client:  # the fixture devices have no QoS
        body = client.get("/api/qos").json()
    assert body["enabled"] is False and body["devices"] == []


def test_only_devices_with_qos_are_listed_in_both_modes(db):
    for devices, mode in ((sim_devices(), "simulated"), (real_devices(), "real")):
        db.sync_devices(devices)
        with client_for(db, devices, mode=mode) as client:
            body = client.get("/api/qos").json()
        assert body["enabled"] is True and [d["device"] for d in body["devices"]] == ["alpha"]


def test_an_unknown_device_is_404_and_a_device_without_qos_is_an_empty_list(db):
    db.sync_devices(sim_devices())
    with client_for(db, sim_devices()) as client:
        assert client.get("/api/qos?device=ghost").status_code == 404
        assert client.get("/api/qos?device=beta").json()["devices"] == []
        assert client.get("/api/qos/history?device=beta").status_code == 404
        assert client.get("/api/qos/history?device=ghost").status_code == 404


def test_before_the_first_poll_a_device_is_pending_with_no_rows(db):
    db.sync_devices(real_devices())
    with client_for(db, real_devices(), mode="real") as client:
        entry = client.get("/api/qos").json()["devices"][0]
    assert entry["status"]["status"] == "pending" and entry["rows"] == []
    assert entry["adapter"] == "diffserv-mib" and "RFC 3289" in entry["adapter_label"]
    assert "DIFFSERV-MIB" in entry["source_mib"] and entry["requirements"]


# ------------------------------------------------------------------ current state
def test_rows_carry_rates_provenance_counter_type_and_the_polling_time(db):
    db.sync_devices(real_devices())
    store(db, "alpha", ramp(), end=NOW - 2)
    with client_for(db, real_devices(), mode="real") as client:
        body = client.get("/api/qos").json()
    entry = body["devices"][0]
    (r,) = entry["rows"]
    assert (r["key"], r["kind"], r["name"], r["detail"]) == ("count:1", "traffic", "COUNT:1",
                                                            "count:1 detail")
    assert r["pps"] == 10.0 and r["bps"] == 8000.0  # 5000 octets per 5 s = 1000 B/s
    assert r["counter_type"] == "Counter64" and r["counter_bits"] == 64
    assert r["polled_at"] == NOW - 2 and r["age_s"] == pytest.approx(2)
    assert entry["adapter"] == "diffserv-mib" and "RFC 3289" in entry["source_mib"]
    assert body["simulated"] is False and entry["status"]["status"] == "ok"
    assert entry["status"]["stale"] is False and body["stale_after_s"] == 3 * INTERVAL + 30


def test_32_bit_counters_are_labelled_as_such(db):
    db.sync_devices(real_devices())
    store(db, "alpha", [[row(bits=32)], [row(bits=32, pkts=10, octs=100)]])
    with client_for(db, real_devices(), mode="real") as client:
        assert client.get("/api/qos").json()["devices"][0]["rows"][0]["counter_type"] == "Counter32"


def test_simulated_data_is_labelled_simulated_everywhere(db):
    db.sync_devices(sim_devices())
    store(db, "alpha", ramp(), adapter="simulated", source="simulated (no real device)")
    with client_for(db, sim_devices()) as client:
        body = client.get("/api/qos").json()
    entry = body["devices"][0]
    assert body["simulated"] is True and body["mode"] == "simulated"
    assert entry["adapter"] == "simulated" and entry["adapter_label"] == "simulated"
    assert "simulated" in entry["source_mib"] and entry["requirements"] is None


def test_raw_counters_credentials_and_addresses_are_never_exposed(db):
    db.sync_devices(real_devices())
    store(db, "alpha", [[row(pkts=123456789, octs=987654321)],
                        [row(pkts=123456999, octs=987659999)]])
    with client_for(db, real_devices(), mode="real") as client:
        text = client.get("/api/qos").text + client.get("/api/qos/history?device=alpha").text
    for forbidden in ("123456789", "987654321", "credentials", "192.0.2.1", "NETMONLT_SNMP",
                      "password", "auth_key", "priv_key", "community"):
        assert forbidden not in text, forbidden


def test_a_missing_rate_is_null_with_a_reason_never_zero(db):
    db.sync_devices(real_devices())
    store(db, "alpha", [[row(pkts=500, octs=500)]])  # only a first sample
    with client_for(db, real_devices(), mode="real") as client:
        (r,) = client.get("/api/qos").json()["devices"][0]["rows"]
    assert (r["pps"], r["bps"], r["drop_pps"], r["drop_pct"]) == (None, None, None, None)
    assert r["rate_note"] == "first_sample" and r["interval_s"] is None


def test_drops_only_rows_have_no_traffic_rates(db):
    db.sync_devices(real_devices())
    dropper = lambda i: [row("drop:1", "drops", None, None, i * 50, i * 5000)]  # noqa: E731
    store(db, "alpha", [dropper(i) for i in range(4)])
    with client_for(db, real_devices(), mode="real") as client:
        (r,) = client.get("/api/qos").json()["devices"][0]["rows"]
    assert r["kind"] == "drops" and r["drop_pps"] == 10.0
    assert (r["pps"], r["bps"], r["drop_pct"]) == (None, None, None)


def test_the_response_explains_what_qos_telemetry_is_and_is_not(db):
    db.sync_devices(real_devices())
    with client_for(db, real_devices(), mode="real") as client:
        body = client.get("/api/qos").json()
    d = body["definitions"]
    assert "interface discards" in d["not_interface_discards"]
    assert "DSCP" in d["not_dscp"] and "never captures" in d["not_dscp"]
    assert "ICMP" in d["not_icmp_quality"] and "not prove" in d["not_proof"]
    assert "do not prove that QoS is enforced" in body["scope"]


# ------------------------------------------------------------------ unsupported, failed, stale
def test_an_unsupported_device_shows_the_state_and_no_fabricated_rows(db):
    db.sync_devices(real_devices())
    db.save_qos_failure("alpha", NOW - 2, "unsupported", "no QoS data is shown")
    with client_for(db, real_devices(), mode="real") as client:
        entry = client.get("/api/qos").json()["devices"][0]
    assert entry["status"]["status"] == "unsupported" and entry["rows"] == []
    assert "no QoS data is shown" in entry["status"]["message"]
    assert entry["requirements"] and entry["status"]["last_ok_age_s"] is None


@pytest.mark.parametrize("category", ["timeout", "auth_failure", "config", "error"])
def test_failures_are_reported_with_their_category(db, category):
    db.sync_devices(real_devices())
    db.save_qos_failure("alpha", NOW - 1, category, "fixed message")
    with client_for(db, real_devices(), mode="real") as client:
        entry = client.get("/api/qos").json()["devices"][0]
    assert entry["status"]["status"] == category and entry["status"]["message"] == "fixed message"
    assert entry["status"]["stale"] is False  # never succeeded: nothing to call stale


def test_after_a_failure_the_last_good_data_is_kept_and_marked_stale(db):
    db.sync_devices(real_devices())
    store(db, "alpha", ramp(), end=NOW - 300)
    db.save_qos_failure("alpha", NOW - 1, "timeout", "no response")
    with client_for(db, real_devices(), mode="real") as client:
        entry = client.get("/api/qos").json()["devices"][0]
    assert entry["rows"][0]["pps"] == 10.0  # the last good rates remain visible
    assert entry["status"]["status"] == "timeout" and entry["status"]["stale"] is True
    assert entry["status"]["last_ok_age_s"] == pytest.approx(300)
    assert entry["rows"][0]["age_s"] == pytest.approx(300)


def test_a_restart_gives_one_sample_with_no_rate(db):
    db.sync_devices(real_devices())
    snaps = ramp(4)
    snaps.append([row(pkts=7, octs=70)])  # counters fell and uptime (set by store) kept rising
    store(db, "alpha", snaps, end=NOW - 1)
    with client_for(db, real_devices(), mode="real") as client:
        (r,) = client.get("/api/qos").json()["devices"][0]["rows"]
    assert r["rate_note"] == "counter_reset" and r["pps"] is None


def test_an_element_that_disappears_is_marked_absent(db):
    db.sync_devices(real_devices())
    store(db, "alpha", [[row("count:1", pkts=0, octs=0), row("count:2", pkts=0, octs=0)],
                        [row("count:1", pkts=50, octs=500)]], end=NOW - 1)
    with client_for(db, real_devices(), mode="real") as client:
        rows = {r["key"]: r for r in client.get("/api/qos").json()["devices"][0]["rows"]}
    assert rows["count:2"]["present"] is False and rows["count:2"]["rate_note"] == "absent"
    assert rows["count:2"]["pps"] is None and rows["count:1"]["pps"] == 10.0


# ------------------------------------------------------------------ history
def test_history_has_one_series_per_element_with_buckets_and_units(db):
    db.sync_devices(real_devices())
    store(db, "alpha", [[row("count:1", pkts=i * 50, octs=i * 5000),
                         row("count:2", pkts=i * 10, octs=i * 800)] for i in range(8)])
    with client_for(db, real_devices(), mode="real") as client:
        body = client.get("/api/qos/history?device=alpha&window_s=900").json()
    assert body["device"] == "alpha" and body["window_s"] == 900 and body["end"] == NOW
    assert [s["key"] for s in body["series"]] == ["count:1", "count:2"]
    first = body["series"][0]
    assert first["kind"] == "traffic" and first["name"] == "COUNT:1"
    valid = [p for p in first["points"] if p["pps"] is not None]
    assert valid and all(p["pps"] == pytest.approx(10.0) for p in valid)
    assert body["bucket_s"] >= 2 * INTERVAL and body["gaps"] == []
    assert body["simulated"] is False and body["series_truncated"] == 0


def test_history_never_shows_a_zero_for_buckets_with_no_valid_rate(db):
    db.sync_devices(real_devices())
    store(db, "alpha", [[row(pkts=500, octs=500)]])  # one first sample: no rate yet
    with client_for(db, real_devices(), mode="real") as client:
        (series,) = client.get("/api/qos/history?device=alpha").json()["series"]
    (point,) = series["points"]
    assert point["pps"] is None and point["bps"] is None and point["drop_pct"] is None
    assert point["samples"] == 1 and point["no_rate"] == 1


def test_polling_gaps_are_gaps_not_zero_traffic(db):
    db.sync_devices(real_devices())
    store(db, "alpha", ramp(8), end=NOW - 500)
    store(db, "alpha", ramp(8, pps=20), end=NOW)
    with client_for(db, real_devices(), mode="real") as client:
        body = client.get("/api/qos/history?device=alpha").json()
    assert len(body["gaps"]) == 1 and body["gaps"][0]["end"] - body["gaps"][0]["start"] > 400
    points = body["series"][0]["points"]
    assert all(not (body["gaps"][0]["start"] + 40 < p["start"] and p["end"] < body["gaps"][0]["end"]
                    - 40) for p in points)  # nothing invented inside the hole


def test_a_counter_reset_shows_as_a_no_rate_sample_in_history(db):
    db.sync_devices(real_devices())
    snaps = ramp(6)
    snaps.insert(3, [row(pkts=5, octs=5)])  # a reset in the middle
    store(db, "alpha", snaps)
    with client_for(db, real_devices(), mode="real") as client:
        (series,) = client.get("/api/qos/history?device=alpha&max_points=500").json()["series"]
    assert sum(p["no_rate"] for p in series["points"]) >= 2  # the first sample and the reset


def test_history_before_any_poll_is_empty_not_an_error(db):
    db.sync_devices(real_devices())
    with client_for(db, real_devices(), mode="real") as client:
        body = client.get("/api/qos/history?device=alpha").json()
    assert body["series"] == [] and body["first_ts"] is None and body["gaps"] == []


def test_history_limits_the_number_of_series(db):
    db.sync_devices(real_devices())
    rows = lambda i: [row(f"count:{n}", pkts=i * 10, octs=i * 10) for n in range(1, 21)]  # noqa: E731
    store(db, "alpha", [rows(i) for i in range(4)])
    with client_for(db, real_devices(), mode="real") as client:
        body = client.get("/api/qos/history?device=alpha").json()
    assert len(body["series"]) == 16 and body["series_truncated"] == 4


def test_history_parameters_are_validated(db):
    db.sync_devices(real_devices())
    with client_for(db, real_devices(), mode="real") as client:
        assert client.get("/api/qos/history").status_code == 422  # a device is required
        for bad in ("window_s=10", "window_s=99999999", "max_points=1", "max_points=9999"):
            assert client.get(f"/api/qos/history?device=alpha&{bad}").status_code == 422


def test_the_endpoints_are_read_only(db):
    db.sync_devices(real_devices())
    with client_for(db, real_devices(), mode="real") as client:
        for path in ("/api/qos", "/api/qos/history?device=alpha"):
            assert client.post(path).status_code == 405
            assert client.delete(path).status_code == 405


def test_the_dashboard_page_still_serves_and_names_the_qos_section(db):
    db.sync_devices(real_devices())
    with client_for(db, real_devices(), mode="real") as client:
        page = client.get("/")
    assert page.status_code == 200 and "QoS telemetry (SNMP)" in page.text
