"""The real SNMPv3 QoS client, tested without a network: the thin pysnmp calls are replaced by
fakes. (No QoS-capable device was available, so the client has NOT been run against any real
equipment; the wrappers around pysnmp were exercised against a real net-snmp agent in Stage 6.)"""

import asyncio
import logging

import pytest

pytest.importorskip("pysnmp")
pytest.importorskip("cryptography")

from pysnmp.proto import rfc1902  # noqa: E402

from netmonlt import snmp_pysnmp  # noqa: E402
from netmonlt.domain import Device, QosConfig, SnmpConfig  # noqa: E402
from netmonlt.qos_adapters import DIFFSERV_COLUMNS, DIFFSERV_MIB  # noqa: E402
from netmonlt.snmp import SnmpConfigError, SnmpError  # noqa: E402
from netmonlt.snmp_pysnmp import (  # noqa: E402
    MAX_QOS_WALK_ROWS,
    OID_SYS_UPTIME,
    PySnmpQosClient,
    convert_oid_value,
    index_tuple,
)

SECRET = "SECRET-sentinel-value-9f3a"
ENV = {
    "NETMONLT_SNMP_R1_USER": "monitor",
    "NETMONLT_SNMP_R1_AUTH_KEY": f"{SECRET}-auth",
    "NETMONLT_SNMP_R1_PRIV_KEY": f"{SECRET}-priv",
}
CONFIG = SnmpConfig("NETMONLT_SNMP_R1", port=1161, timeout_s=3.0, retries=2)
DEVICE = Device("router", "192.0.2.1", authorized=True, snmp=CONFIG,
                qos=QosConfig("diffserv-mib"))
COUNT = "1.3.6.1.2.1.97.1.5.5.1"


# ------------------------------------------------------------------ pure helpers
def test_oid_values_become_dotted_strings_and_other_values_are_unchanged():
    oid = rfc1902.ObjectName("1.3.6.1.2.1.97.1.5.5.1.2.7")
    assert convert_oid_value(oid) == "1.3.6.1.2.1.97.1.5.5.1.2.7"
    assert convert_oid_value(rfc1902.Counter64(2**64 - 1)) == 2**64 - 1
    assert convert_oid_value(rfc1902.Integer32(3)) == 3
    assert convert_oid_value(rfc1902.OctetString("eth0")) == b"eth0"


@pytest.mark.parametrize("oid, column, expected", [
    ("1.2.3.4.7", "1.2.3", (4, 7)), ("1.2.3.9", "1.2.3", (9,)), ("1.2.9.4", "1.2.3", None),
    ("1.2.3.4.x", "1.2.3", None), ("1.2.3", "1.2.3", None), ("1.2.30.4", "1.2.3", None),
])
def test_an_index_is_the_tuple_of_integers_after_the_column(oid, column, expected):
    assert index_tuple(oid, column) == expected


# ------------------------------------------------------------------ the client with fakes
class FakeEngine:
    instances = []

    def __init__(self):
        self.closed = 0
        FakeEngine.instances.append(self)

    def close_dispatcher(self):
        self.closed += 1


class FakeTarget:
    created = []

    @classmethod
    async def create(cls, address, **kwargs):
        cls.created.append((address, kwargs))
        return object()


TABLES = {
    DIFFSERV_COLUMNS["countPkts"]: {(1,): 1000, (2,): 50},
    DIFFSERV_COLUMNS["countOctets"]: {(1,): 1_000_000, (2,): 5000},
    DIFFSERV_COLUMNS["actionInterface"]: {(10,): 3},
    DIFFSERV_COLUMNS["actionSpecific"]: {(10,): f"{COUNT}.3.1"},
    DIFFSERV_COLUMNS["ifName"]: {(3,): b"Gi0/3"},
    DIFFSERV_COLUMNS["ifDiscontinuity"]: {(3,): 700},
    DIFFSERV_COLUMNS["dropType"]: {(1,): 2},
    DIFFSERV_COLUMNS["dropPkts"]: {(1,): 9},
    DIFFSERV_COLUMNS["dropOctets"]: {(1,): 900},
}


@pytest.fixture
def fakes(monkeypatch):
    requested = []
    FakeEngine.instances, FakeTarget.created = [], []

    async def get_uptime(engine, auth, target):
        requested.append(OID_SYS_UPTIME)
        return 424242

    async def walk(engine, auth, target, column):
        requested.append(column)
        return dict(fakes.tables.get(column, {}))

    fakes.tables, fakes.requested = dict(TABLES), requested
    monkeypatch.setattr(snmp_pysnmp, "SnmpEngine", FakeEngine)
    monkeypatch.setattr(snmp_pysnmp, "UdpTransportTarget", FakeTarget)
    monkeypatch.setattr(snmp_pysnmp, "_get_uptime", get_uptime)
    monkeypatch.setattr(snmp_pysnmp, "_walk_table", walk)
    return fakes


def client_for(device=DEVICE):
    return PySnmpQosClient.from_devices([device], ENV)


def poll(device=DEVICE, client=None):
    return asyncio.run((client or client_for(device)).poll(device))


def test_a_poll_turns_the_walks_into_a_qos_snapshot(fakes):
    snapshot = poll()
    assert snapshot.adapter == "diffserv-mib" and snapshot.uptime_ticks == 424242
    assert "RFC 3289" in snapshot.source_mib and snapshot.truncated == 0
    assert [r.key for r in snapshot.rows] == ["count:1", "count:2", "drop:1"]
    one = snapshot.rows[0]
    assert (one.packets, one.octets, one.detail, one.discontinuity) == (
        1000, 1_000_000, "interface Gi0/3", 700)
    assert snapshot.rows[2].drop_packets == 9 and snapshot.rows[2].detail == "tail drop"


def test_only_the_adapters_columns_and_sysuptime_are_requested_all_read_only(fakes):
    poll()
    assert sorted(fakes.requested) == sorted([OID_SYS_UPTIME, *DIFFSERV_MIB.columns.values()])
    # every request is a GET or a GETBULK walk: the module has no set-request call at all
    assert not hasattr(snmp_pysnmp, "set_cmd") and "set_cmd" not in vars(snmp_pysnmp)


def test_the_target_uses_the_devices_own_address_port_timeout_and_retries(fakes):
    poll()
    assert FakeTarget.created == [(("192.0.2.1", 1161), {"timeout": 3.0, "retries": 2})]


def test_the_engine_is_always_closed(fakes):
    poll()
    assert [e.closed for e in FakeEngine.instances] == [1]
    fakes.tables = {}
    with pytest.raises(SnmpError):
        poll()
    assert [e.closed for e in FakeEngine.instances] == [1, 1]


def test_a_device_without_diffserv_data_is_unsupported_and_returns_no_rows(fakes):
    fakes.tables = {DIFFSERV_COLUMNS["ifName"]: {(1,): b"eth0"}}  # only interface names
    with pytest.raises(SnmpError) as caught:
        poll()
    assert caught.value.category == "unsupported"


def test_rows_beyond_the_cap_are_counted_not_silently_dropped(fakes):
    fakes.tables = {
        DIFFSERV_COLUMNS["countPkts"]: {(i,): i for i in range(1, 91)},
        DIFFSERV_COLUMNS["countOctets"]: {(i,): i for i in range(1, 91)},
    }
    snapshot = poll()
    assert len(snapshot.rows) == 64 and snapshot.truncated == 26


def test_a_walk_row_limit_exists():
    assert MAX_QOS_WALK_ROWS == 4 * 64


# ------------------------------------------------------------------ credentials
def test_credentials_are_loaded_from_the_environment_and_never_shown():
    client = client_for()
    assert SECRET not in repr(client) and "<hidden>" in repr(client)
    assert "router" in repr(client)


def test_missing_credentials_are_reported_by_variable_name_only():
    env = {"NETMONLT_SNMP_R1_USER": "monitor"}
    with pytest.raises(SnmpConfigError) as caught:
        PySnmpQosClient.from_devices([DEVICE], env)
    message = str(caught.value)
    assert "NETMONLT_SNMP_R1_AUTH_KEY" in message and "NETMONLT_SNMP_R1_PRIV_KEY" in message
    assert "monitor" not in message


def test_only_qos_devices_need_credentials():
    plain = Device("plain", "192.0.2.2", authorized=True, snmp=CONFIG)  # SNMP but no QoS
    client = PySnmpQosClient.from_devices([plain], {})  # no variables needed for it
    assert "plain" not in repr(client)


# ------------------------------------------------------------------ refusals (defence in depth)
@pytest.mark.parametrize("device", [
    Device("a", "192.0.2.1", authorized=True, snmp=CONFIG),  # no qos block
    Device("a", "192.0.2.1", authorized=True, qos=QosConfig("diffserv-mib")),  # no snmp
    Device("a", "192.0.2.1", authorized=False, snmp=CONFIG, qos=QosConfig("diffserv-mib")),
    Device("a", "192.0.2.1", authorized=True, snmp=CONFIG, qos=QosConfig("cisco-guess")),
    Device("a", "10.0.0.0/24", authorized=True, snmp=CONFIG, qos=QosConfig("diffserv-mib")),
    Device("a", "-oX", authorized=True, snmp=CONFIG, qos=QosConfig("diffserv-mib")),
])
def test_a_device_that_is_not_fully_configured_is_never_contacted(fakes, device):
    client = PySnmpQosClient({"a": object()})
    with pytest.raises(SnmpError) as caught:
        asyncio.run(client.poll(device))
    assert caught.value.category == "config"
    assert fakes.requested == [] and FakeTarget.created == [] and FakeEngine.instances == []


def test_a_device_whose_credentials_were_not_loaded_is_not_contacted(fakes):
    with pytest.raises(SnmpError) as caught:
        asyncio.run(PySnmpQosClient({}).poll(DEVICE))
    assert caught.value.category == "config" and FakeTarget.created == []


# ------------------------------------------------------------------ errors
def test_snmp_errors_keep_their_category(fakes, monkeypatch):
    async def denied(engine, auth, target, column):
        raise SnmpError("auth_failure")

    monkeypatch.setattr(snmp_pysnmp, "_walk_table", denied)
    with pytest.raises(SnmpError) as caught:
        poll()
    assert caught.value.category == "auth_failure"


def test_unexpected_errors_never_leak_their_text(fakes, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    async def leaky(engine, auth, target, column):
        raise RuntimeError(f"failed with key {SECRET}")

    monkeypatch.setattr(snmp_pysnmp, "_walk_table", leaky)
    with pytest.raises(SnmpError) as caught:
        poll()
    assert caught.value.category == "error" and SECRET not in str(caught.value)
    assert SECRET not in caplog.text and "RuntimeError" in caplog.text


# ------------------------------------------------------------------ the walk wrapper
class FakeStatus:
    def __init__(self, text):
        self.text = text

    def prettyPrint(self):  # noqa: N802 - pysnmp's name
        return self.text

    def __bool__(self):
        return True


def walker(*pages):
    closed = []

    async def fake(engine, auth, target, context, nonrep, maxrep, objtype, **options):
        assert options == {"lexicographicMode": False}  # never walks past the column's subtree
        try:
            for error, status, binds in pages:
                yield error, status, 0, binds
        finally:
            closed.append(True)

    fake.closed = closed
    return fake


COL = "1.3.6.1.2.1.97.1.6.2.1.8"


def walk(fake, monkeypatch):
    monkeypatch.setattr(snmp_pysnmp, "bulk_walk_cmd", fake)
    return asyncio.run(snmp_pysnmp._walk_table(None, None, None, COL))


def test_a_table_walk_collects_tuple_indexed_rows_and_pointer_values(monkeypatch):
    fake = walker((None, 0, [
        (f"{COL}.1", rfc1902.Counter64(5)),
        (f"{COL}.2.7", rfc1902.Counter64(6)),  # a two-part index
        ("1.3.6.1.9.9.9.1", rfc1902.Counter64(9)),  # not in this column: ignored
        (f"{COL}.3", rfc1902.ObjectName("1.3.6.1.2.1.97.1.7.2.1.1.5")),
    ]))
    assert walk(fake, monkeypatch) == {
        (1,): 5, (2, 7): 6, (3,): "1.3.6.1.2.1.97.1.7.2.1.1.5"}
    assert fake.closed == [True]


def test_a_walk_stops_at_the_row_limit(monkeypatch):
    binds = [(f"{COL}.{i}", rfc1902.Counter64(i)) for i in range(1, MAX_QOS_WALK_ROWS + 50)]
    rows = walk(walker((None, 0, binds), (None, 0, [(f"{COL}.9999", rfc1902.Counter64(1))])),
                monkeypatch)
    assert MAX_QOS_WALK_ROWS <= len(rows) < MAX_QOS_WALK_ROWS + 50 + 1


@pytest.mark.parametrize("error, status, category", [
    ("No SNMP response received before timeout", 0, "timeout"),
    ("Wrong SNMP PDU digest", 0, "auth_failure"),
    (None, FakeStatus("noAccess"), "auth_failure"),
    (None, FakeStatus("something unseen"), "error"),
])
def test_walk_errors_map_to_fixed_categories(monkeypatch, error, status, category):
    with pytest.raises(SnmpError) as caught:
        walk(walker((error, status, [])), monkeypatch)
    assert caught.value.category == category
