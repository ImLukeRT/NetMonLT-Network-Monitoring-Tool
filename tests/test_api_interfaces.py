"""GET /api/interfaces: shape, failure states, labelling and read-only behaviour."""

import asyncio

from fastapi.testclient import TestClient

from helpers import FakeClock
from netmonlt.api import create_app
from netmonlt.domain import Device, SimInterface, SimProfile, SnmpConfig
from netmonlt.ifstats import InterfaceReading, InterfaceSnapshot
from netmonlt.inventory import Inventory
from netmonlt.probes import SimulatedProber
from netmonlt.snmp import SnmpError
from netmonlt.snmp_poller import snmp_poll_once, snmp_stale_after_s

SIM_IF = SimProfile(interfaces=(SimInterface("Gi0/1", in_mbps=5),))
REAL_DEVICE = Device("router", "192.0.2.1", authorized=True, snmp=SnmpConfig("NETMONLT_SNMP_R1"))
INTERVAL = 30.0


def sim_inventory(*devices):
    return Inventory("simulated", 5.0, tuple(devices), snmp_poll_interval_s=INTERVAL)


def real_inventory(*devices):
    return Inventory("real", 10.0, tuple(devices), snmp_poll_interval_s=INTERVAL)


def snap(in_octets, out_octets, uptime=1000, *interfaces):
    """A snapshot; `interfaces` are (if_index, name, oper_status), default one up eth0."""
    interfaces = interfaces or ((1, "eth0", "up"),)
    return InterfaceSnapshot(tuple(
        InterfaceReading(index, name, status, in_octets=in_octets, out_octets=out_octets,
                         in_errors=2, out_errors=0, in_discards=1, out_discards=0)
        for index, name, status in interfaces
    ), uptime_ticks=uptime)


class Fixed:
    def __init__(self, *outcomes):
        self.outcomes, self.n = list(outcomes), 0

    async def poll(self, device):
        outcome = self.outcomes[min(self.n, len(self.outcomes) - 1)]
        self.n += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make(db, inventory, clock):
    db.sync_devices(inventory.devices)
    app = create_app(inventory, db, SimulatedProber(), start_collector=False, clock=clock)
    return TestClient(app)


def poll(db, inventory, client_obj, t, clock):
    clock.now = t
    asyncio.run(snmp_poll_once(inventory.snmp_devices, client_obj, db, clock=clock,
                               max_gap_s=snmp_stale_after_s(INTERVAL)))


# ------------------------------------------------------------------ before anything is polled
def test_a_configured_device_that_has_not_been_polled_is_pending(db):
    inv = sim_inventory(Device("sw", "192.0.2.1", sim=SIM_IF))
    with make(db, inv, FakeClock(500.0)) as client:
        body = client.get("/api/interfaces").json()
    assert body["devices"] == [{
        "device": "sw",
        "snmp": {"status": "pending", "message": None, "last_poll_age_s": None,
                 "last_ok_age_s": None, "stale": False, "truncated": 0},
        "interfaces": [],
    }]


def test_without_any_snmp_devices_the_list_is_empty(db):
    inv = sim_inventory(Device("plain", "192.0.2.1"))
    with make(db, inv, FakeClock(0.0)) as client:
        body = client.get("/api/interfaces").json()
    assert body["devices"] == [] and body["server_time"] == 0.0


# ------------------------------------------------------------------ the data
def test_response_shape_and_values_after_two_polls(db):
    inv = sim_inventory(Device("sw", "192.0.2.1", sim=SIM_IF))
    clock = FakeClock(0.0)
    snmp = Fixed(snap(0, 0), snap(37_500_000, 7_500_000))
    with make(db, inv, clock) as client:
        poll(db, inv, snmp, 1000.0, clock)
        first = client.get("/api/interfaces").json()["devices"][0]["interfaces"][0]
        assert (first["in_bps"], first["rate_note"]) == (None, "first_sample")
        poll(db, inv, snmp, 1030.0, clock)
        clock.now = 1035.0
        body = client.get("/api/interfaces").json()
    assert body["mode"] == "simulated" and body["simulated"] is True
    assert body["poll_interval_s"] == INTERVAL
    assert body["stale_after_s"] == snmp_stale_after_s(INTERVAL)
    (entry,) = body["devices"]
    assert entry["snmp"] == {"status": "ok", "message": None, "last_poll_age_s": 5.0,
                             "last_ok_age_s": 5.0, "stale": False, "truncated": 0}
    assert entry["interfaces"] == [{
        "if_index": 1, "name": "eth0", "oper_status": "up", "present": True, "age_s": 5.0,
        "interval_s": 30.0, "in_bps": 10_000_000.0, "out_bps": 2_000_000.0,
        "in_errors_delta": 0, "out_errors_delta": 0, "in_discards_delta": 0,
        "out_discards_delta": 0, "rate_note": None,
    }]


def test_raw_counters_are_not_exposed(db):
    inv = sim_inventory(Device("sw", "192.0.2.1", sim=SIM_IF))
    clock = FakeClock(0.0)
    with make(db, inv, clock) as client:
        poll(db, inv, Fixed(snap(2**64 - 1, 2**64 - 1)), 1000.0, clock)
        text = client.get("/api/interfaces").text
    assert str(2**64 - 1) not in text and "in_octets" not in text and "out_octets" not in text


def test_real_mode_is_not_labelled_simulated(db):
    inv = real_inventory(REAL_DEVICE)
    with make(db, inv, FakeClock(0.0)) as client:
        body = client.get("/api/interfaces").json()
    assert body["mode"] == "real" and body["simulated"] is False


def test_down_and_absent_interfaces_and_their_unknown_rates(db):
    inv = sim_inventory(Device("sw", "192.0.2.1", sim=SIM_IF))
    clock = FakeClock(0.0)
    snmp = Fixed(
        snap(0, 0, 1000, (1, "a", "up"), (2, "b", "down")),
        snap(0, 0, 1100, (2, "b", "down")),
    )
    with make(db, inv, clock) as client:
        poll(db, inv, snmp, 1000.0, clock)
        poll(db, inv, snmp, 1010.0, clock)
        by_name = {i["name"]: i for i in
                   client.get("/api/interfaces").json()["devices"][0]["interfaces"]}
    assert by_name["b"]["oper_status"] == "down" and by_name["b"]["in_bps"] == 0.0
    gone = by_name["a"]
    assert (gone["present"], gone["oper_status"], gone["rate_note"]) == (False, "absent", "absent")
    assert gone["in_bps"] is None and gone["age_s"] >= 10.0  # last seen before the second poll


# ------------------------------------------------------------------ failures and staleness
def test_a_failed_poll_reports_a_safe_message_and_keeps_the_last_data(db):
    inv = sim_inventory(Device("sw", "192.0.2.1", sim=SIM_IF))
    clock = FakeClock(0.0)
    with make(db, inv, clock) as client:
        poll(db, inv, Fixed(snap(0, 0)), 1000.0, clock)
        poll(db, inv, Fixed(SnmpError("auth_failure")), 1030.0, clock)
        clock.now = 1040.0
        entry = client.get("/api/interfaces").json()["devices"][0]
    assert entry["snmp"]["status"] == "auth_failure"
    assert "authentication failed" in entry["snmp"]["message"]
    assert entry["snmp"]["last_poll_age_s"] == 10.0 and entry["snmp"]["last_ok_age_s"] == 40.0
    assert [i["name"] for i in entry["interfaces"]] == ["eth0"]  # last known, not wiped


def test_data_becomes_stale_after_the_limit(db):
    inv = sim_inventory(Device("sw", "192.0.2.1", sim=SIM_IF))
    clock = FakeClock(0.0)
    limit = snmp_stale_after_s(INTERVAL)
    with make(db, inv, clock) as client:
        poll(db, inv, Fixed(snap(0, 0)), 1000.0, clock)
        clock.now = 1000.0 + limit
        assert client.get("/api/interfaces").json()["devices"][0]["snmp"]["stale"] is False
        clock.now = 1000.0 + limit + 1
        assert client.get("/api/interfaces").json()["devices"][0]["snmp"]["stale"] is True


def test_a_device_that_never_worked_is_not_called_stale(db):
    inv = sim_inventory(Device("sw", "192.0.2.1", sim=SIM_IF))
    clock = FakeClock(0.0)
    with make(db, inv, clock) as client:
        poll(db, inv, Fixed(SnmpError("timeout")), 1000.0, clock)
        clock.now = 9999.0
        entry = client.get("/api/interfaces").json()["devices"][0]
    assert entry["snmp"]["status"] == "timeout" and entry["snmp"]["stale"] is False
    assert entry["snmp"]["last_ok_age_s"] is None and entry["interfaces"] == []


# ------------------------------------------------------------------ filtering and safety
def test_device_filter_and_unknown_device(db):
    a, b = Device("a", "192.0.2.1", sim=SIM_IF), Device("b", "192.0.2.2", sim=SIM_IF)
    plain = Device("plain", "192.0.2.3")
    inv = sim_inventory(a, b, plain)
    with make(db, inv, FakeClock(0.0)) as client:
        assert [d["device"] for d in client.get("/api/interfaces").json()["devices"]] == ["a", "b"]
        only_b = client.get("/api/interfaces?device=b").json()["devices"]
        assert [d["device"] for d in only_b] == ["b"]
        assert client.get("/api/interfaces?device=plain").json()["devices"] == []
        assert client.get("/api/interfaces?device=nope").status_code == 404
        assert client.get("/api/interfaces?device=" + "x" * 65).status_code == 422


def test_leftover_rows_for_a_device_that_no_longer_uses_snmp_are_hidden(db):
    with_snmp = Device("sw", "192.0.2.1", sim=SIM_IF)
    clock = FakeClock(0.0)
    inv = sim_inventory(with_snmp)
    db.sync_devices(inv.devices)
    poll(db, inv, Fixed(snap(0, 0)), 1000.0, clock)
    changed = sim_inventory(Device("sw", "192.0.2.1"))  # SNMP removed from the inventory
    with make(db, changed, clock) as client:
        assert client.get("/api/interfaces").json()["devices"] == []


def test_the_endpoint_is_read_only(db):
    inv = sim_inventory(Device("sw", "192.0.2.1", sim=SIM_IF))
    with make(db, inv, FakeClock(0.0)) as client:
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)("/api/interfaces").status_code == 405


def test_other_endpoints_are_unchanged(db):
    inv = sim_inventory(Device("sw", "192.0.2.1", sim=SIM_IF))
    with make(db, inv, FakeClock(0.0)) as client:
        assert client.get("/api/devices").status_code == 200
        assert client.get("/api/alerts").status_code == 200
        assert client.get("/healthz").json() == {"ok": True}
        assert client.get("/").status_code == 200
