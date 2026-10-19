"""The real SNMPv3 adapter, tested without a network: the thin pysnmp calls are replaced by fakes.

(The pysnmp calls themselves were checked by hand against a real net-snmp agent on loopback;
that is not part of the automated suite, which never needs an SNMP device.)
"""

import asyncio
import logging

import pytest

pytest.importorskip("pysnmp")
pytest.importorskip("cryptography")

from pysnmp.proto import rfc1902  # noqa: E402
from pysnmp.proto import rfc1905 as rfc1905  # noqa: E402

from netmonlt import snmp_pysnmp  # noqa: E402
from netmonlt.domain import (  # noqa: E402
    SNMP_AUTH_PROTOCOLS,
    SNMP_PRIV_PROTOCOLS,
    Device,
    SnmpConfig,
)
from netmonlt.snmp import SnmpConfigError, SnmpCredentials, SnmpError  # noqa: E402
from netmonlt.snmp_pysnmp import (  # noqa: E402
    COLUMNS,
    MAX_ROWS_PER_COLUMN,
    OID_SYS_UPTIME,
    PySnmpClient,
    classify_error,
    convert_value,
    index_of,
    usm_user,
)

SECRET = "SECRET-sentinel-value-9f3a"
ENV = {
    "NETMONLT_SNMP_R1_USER": "monitor",
    "NETMONLT_SNMP_R1_AUTH_KEY": f"{SECRET}-auth",
    "NETMONLT_SNMP_R1_PRIV_KEY": f"{SECRET}-priv",
}
CONFIG = SnmpConfig("NETMONLT_SNMP_R1", port=1161, timeout_s=3.0, retries=2)
DEVICE = Device("router", "192.0.2.1", authorized=True, snmp=CONFIG)


# ------------------------------------------------------------------ pure helpers
@pytest.mark.parametrize("text, category", [
    ("No SNMP response received before timeout", "timeout"),
    ("Request timeout", "timeout"),
    ("Wrong SNMP PDU digest", "auth_failure"),
    ("Unknown USM user", "auth_failure"),
    ("Unsupported security level", "auth_failure"),
    ("authorizationError", "auth_failure"),
    ("noAccess", "auth_failure"),
    ("Ciphering services not available", "config"),
    ("something nobody has seen before", "error"),
    ("", "error"),
])
def test_error_text_maps_to_a_fixed_category(text, category):
    assert classify_error(text) == category


def test_value_conversion_uses_real_pysnmp_types():
    assert convert_value(rfc1902.Counter64(2**64 - 1)) == 2**64 - 1
    assert convert_value(rfc1902.Counter32(7)) == 7
    assert convert_value(rfc1902.Integer32(2)) == 2
    assert convert_value(rfc1902.Gauge32(9)) == 9
    assert convert_value(rfc1902.TimeTicks(3823)) == 3823
    assert convert_value(rfc1902.OctetString("eth0")) == b"eth0"
    assert convert_value(rfc1902.OctetString(b"\xff\xfe")) == b"\xff\xfe"


@pytest.mark.parametrize(
    "marker", [rfc1905.NoSuchObject, rfc1905.NoSuchInstance, rfc1905.EndOfMibView]
)
def test_missing_value_markers_are_none_not_zero_or_text(marker):
    assert convert_value(marker("")) is None


def test_unconvertible_values_are_none():
    assert convert_value(object()) is None
    assert convert_value(None) is None


@pytest.mark.parametrize("oid, expected", [
    ("1.3.6.1.2.1.2.2.1.8.4", 4),
    ("1.3.6.1.2.1.2.2.1.8.1001", 1001),
    ("1.3.6.1.2.1.2.2.1.8", None),  # the column itself
    ("1.3.6.1.2.1.2.2.1.8.4.5", None),  # a longer index: not an ifIndex row
    ("1.3.6.1.2.1.2.2.1.9.4", None),  # a different column
    ("1.3.6.1.2.1.2.2.1.80.4", None),  # a column whose number merely starts the same
    ("1.3.6.1.2.1.2.2.1.8.x", None),
])
def test_ifindex_is_parsed_only_from_rows_of_the_requested_column(oid, expected):
    assert index_of(oid, "1.3.6.1.2.1.2.2.1.8") == expected


def test_only_standard_interface_mib_columns_are_ever_requested():
    for column in COLUMNS.values():
        assert column.startswith(("1.3.6.1.2.1.2.2.1.", "1.3.6.1.2.1.31.1.1.1."))
    assert OID_SYS_UPTIME == "1.3.6.1.2.1.1.3.0"
    assert len(COLUMNS) == 9


# ------------------------------------------------------------------ SNMPv3 users
def attr(user, new, old):
    """An attribute under pysnmp's new name if it has one (the old names are deprecated)."""
    return getattr(user, new) if hasattr(user, new) else getattr(user, old)


@pytest.mark.parametrize("auth", SNMP_AUTH_PROTOCOLS)
@pytest.mark.parametrize("priv", SNMP_PRIV_PROTOCOLS)
def test_every_supported_protocol_combination_builds_a_user(auth, priv):
    config = SnmpConfig("X", auth_protocol=auth, priv_protocol=priv)
    creds = SnmpCredentials("u", "auth-key-123", "priv-key-123")
    user = usm_user(creds, config)
    assert attr(user, "authentication_protocol", "authProtocol") == snmp_pysnmp.AUTH_PROTOCOLS[auth]
    assert attr(user, "privacy_protocol", "privProtocol") == snmp_pysnmp.PRIV_PROTOCOLS[priv]


def test_protocol_tables_cover_exactly_what_the_inventory_allows():
    assert set(snmp_pysnmp.AUTH_PROTOCOLS) == set(SNMP_AUTH_PROTOCOLS)
    assert set(snmp_pysnmp.PRIV_PROTOCOLS) == set(SNMP_PRIV_PROTOCOLS)


def test_auth_no_priv_has_no_privacy():
    config = SnmpConfig("X", security_level="authNoPriv")
    user = usm_user(SnmpCredentials("u", "auth-key-123", "ignored-priv-key"), config)
    assert attr(user, "privacy_key", "privKey") is None


# ------------------------------------------------------------------ built from the environment
def test_credentials_for_every_device_are_loaded_at_startup():
    client = PySnmpClient.from_devices([DEVICE, Device("plain", "192.0.2.2")], ENV)
    assert "router" in repr(client) and "plain" not in repr(client)


def test_missing_variables_for_all_devices_are_reported_together_by_name_only():
    other = Device("sw", "192.0.2.3", authorized=True, snmp=SnmpConfig("NETMONLT_SNMP_SW"))
    with pytest.raises(SnmpConfigError) as caught:
        PySnmpClient.from_devices([DEVICE, other], {"NETMONLT_SNMP_R1_USER": "monitor"})
    message = str(caught.value)
    for name in ("NETMONLT_SNMP_R1_AUTH_KEY", "NETMONLT_SNMP_R1_PRIV_KEY", "NETMONLT_SNMP_SW_USER"):
        assert name in message
    assert "monitor" not in message


def test_the_client_never_shows_its_credentials():
    client = PySnmpClient.from_devices([DEVICE], ENV)
    for text in (repr(client), str(client)):
        assert SECRET not in text and "monitor" not in text


# ------------------------------------------------------------------ polling, with fakes for pysnmp
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
    COLUMNS["ifDescr"]: {1: b"Loopback0", 2: b"GigabitEthernet0/1"},
    COLUMNS["ifName"]: {1: b"Lo0", 2: b"Gi0/1"},
    COLUMNS["ifOperStatus"]: {1: 1, 2: 2},
    COLUMNS["ifHCInOctets"]: {1: 100, 2: 2**64 - 1},
    COLUMNS["ifHCOutOctets"]: {1: 200, 2: 300},
    COLUMNS["ifInErrors"]: {1: 0, 2: 5},
    COLUMNS["ifOutErrors"]: {1: 0, 2: 6},
    COLUMNS["ifInDiscards"]: {1: 1, 2: 7},
    COLUMNS["ifOutDiscards"]: {1: 2, 2: 8},
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
    monkeypatch.setattr(snmp_pysnmp, "_walk_column", walk)
    return fakes


def client_for(device=DEVICE):
    return PySnmpClient.from_devices([device], ENV)


def poll(device=DEVICE, client=None):
    return asyncio.run((client or client_for(device)).poll(device))


def test_a_poll_merges_the_columns_into_interface_readings(fakes):
    snapshot = poll()
    assert snapshot.uptime_ticks == 424242
    lo, gi = snapshot.readings
    assert (lo.if_index, lo.name, lo.oper_status, lo.in_octets, lo.out_octets) == (
        1, "Lo0", "up", 100, 200)
    assert (gi.name, gi.oper_status, gi.in_octets, gi.in_errors, gi.out_discards) == (
        "Gi0/1", "down", 2**64 - 1, 5, 8)


def test_only_the_named_columns_and_sysuptime_are_requested(fakes):
    poll()
    assert sorted(fakes.requested) == sorted([OID_SYS_UPTIME, *COLUMNS.values()])


def test_the_target_uses_the_devices_own_address_port_timeout_and_retries(fakes):
    poll()
    assert FakeTarget.created == [(("192.0.2.1", 1161), {"timeout": 3.0, "retries": 2})]


def test_the_engine_is_always_closed(fakes, monkeypatch):
    poll()
    assert [e.closed for e in FakeEngine.instances] == [1]

    async def boom(*args):
        raise SnmpError("timeout")

    monkeypatch.setattr(snmp_pysnmp, "_walk_column", boom)
    with pytest.raises(SnmpError):
        poll()
    assert [e.closed for e in FakeEngine.instances] == [1, 1]


def test_a_device_without_interface_data_is_unsupported(fakes):
    fakes.tables[COLUMNS["ifOperStatus"]] = {}
    with pytest.raises(SnmpError) as caught:
        poll()
    assert caught.value.category == "unsupported"


def test_missing_high_capacity_counters_leave_octets_empty(fakes):
    fakes.tables.pop(COLUMNS["ifHCInOctets"])
    fakes.tables.pop(COLUMNS["ifHCOutOctets"])
    lo, _ = poll().readings
    assert lo.in_octets is None and lo.out_octets is None and lo.in_errors == 0


def test_a_failure_category_from_the_wire_is_preserved(fakes, monkeypatch):
    async def denied(*args):
        raise SnmpError("auth_failure")

    monkeypatch.setattr(snmp_pysnmp, "_get_uptime", denied)
    with pytest.raises(SnmpError) as caught:
        poll()
    assert caught.value.category == "auth_failure"


def test_unexpected_library_errors_become_a_generic_error_without_their_text(
    fakes, monkeypatch, caplog
):
    async def leaky(*args):
        raise RuntimeError(f"pysnmp says the key was {SECRET}")

    monkeypatch.setattr(snmp_pysnmp, "_walk_column", leaky)
    with caplog.at_level(logging.DEBUG), pytest.raises(SnmpError) as caught:
        poll()
    assert caught.value.category == "error"
    assert SECRET not in str(caught.value) and SECRET not in caplog.text
    assert caught.value.__cause__ is None  # the original text isn't chained either


# ------------------------------------------------------------------ only authorised devices
@pytest.mark.parametrize("device", [
    Device("router", "192.0.2.1", authorized=False, snmp=CONFIG),
    Device("router", "192.0.2.1", authorized=True),
    Device("router", "10.0.0.0/24", authorized=True, snmp=CONFIG),
    Device("router", "224.0.0.1", authorized=True, snmp=CONFIG),
    Device("router", "-bad", authorized=True, snmp=CONFIG),
    Device("someone-else", "192.0.2.9", authorized=True, snmp=CONFIG),
])
def test_unconfigured_or_unauthorised_targets_are_refused_before_any_traffic(fakes, device):
    client = client_for(DEVICE)
    with pytest.raises(SnmpError) as caught:
        asyncio.run(client.poll(device))
    assert caught.value.category == "config"
    assert fakes.requested == [] and FakeTarget.created == [] and FakeEngine.instances == []


# ------------------------------------------------------------------ the wrappers around pysnmp
class FakeStatus:
    def __init__(self, text):
        self.text = text

    def prettyPrint(self):  # noqa: N802 - pysnmp's name
        return self.text

    def __bool__(self):
        return True


def walker(*pages):
    """A fake bulk_walk_cmd yielding the given pages [(error, status, binds), ...]."""
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


COL = "1.3.6.1.2.1.2.2.1.8"


def walk(fake, monkeypatch):
    monkeypatch.setattr(snmp_pysnmp, "bulk_walk_cmd", fake)
    return asyncio.run(snmp_pysnmp._walk_column(None, None, None, COL))


def test_a_walk_collects_rows_and_ignores_other_oids_and_missing_markers(monkeypatch):
    fake = walker((None, 0, [
        (f"{COL}.1", rfc1902.Integer32(1)),
        (f"{COL}.2", rfc1905.NoSuchInstance("")),  # not a value
        ("1.3.6.1.2.1.2.2.1.9.3", rfc1902.Integer32(5)),  # a different column
        (f"{COL}.4", rfc1902.Integer32(2)),
    ]), (None, 0, [(f"{COL}.5", rfc1902.Integer32(7))]))
    assert walk(fake, monkeypatch) == {1: 1, 4: 2, 5: 7}
    assert fake.closed == [True]


def test_a_walk_error_indication_becomes_a_category(monkeypatch):
    with pytest.raises(SnmpError) as caught:
        walk(walker(("Wrong SNMP PDU digest", 0, [])), monkeypatch)
    assert caught.value.category == "auth_failure"
    with pytest.raises(SnmpError) as caught:
        walk(walker((None, FakeStatus("noAccess"), [])), monkeypatch)
    assert caught.value.category == "auth_failure"


def test_a_runaway_device_cannot_make_a_walk_endless(monkeypatch):
    pages = [(None, 0, [(f"{COL}.{i}", rfc1902.Integer32(1)) for i in range(start, start + 20)])
             for start in range(1, MAX_ROWS_PER_COLUMN * 3, 20)]
    fake = walker(*pages)
    rows = walk(fake, monkeypatch)
    assert MAX_ROWS_PER_COLUMN <= len(rows) < MAX_ROWS_PER_COLUMN + 20
    assert fake.closed == [True]  # the generator was closed, not abandoned


def test_the_uptime_request_handles_values_and_errors(monkeypatch):
    async def ok(*args, **kwargs):
        return None, 0, 0, [(OID_SYS_UPTIME, rfc1902.TimeTicks(3823))]

    monkeypatch.setattr(snmp_pysnmp, "get_cmd", ok)
    assert asyncio.run(snmp_pysnmp._get_uptime(None, None, None)) == 3823

    async def missing(*args, **kwargs):
        return None, 0, 0, [(OID_SYS_UPTIME, rfc1905.NoSuchObject(""))]

    monkeypatch.setattr(snmp_pysnmp, "get_cmd", missing)
    assert asyncio.run(snmp_pysnmp._get_uptime(None, None, None)) is None  # optional, not fatal

    async def timed_out(*args, **kwargs):
        return "No SNMP response received before timeout", 0, 0, []

    monkeypatch.setattr(snmp_pysnmp, "get_cmd", timed_out)
    with pytest.raises(SnmpError) as caught:
        asyncio.run(snmp_pysnmp._get_uptime(None, None, None))
    assert caught.value.category == "timeout"
