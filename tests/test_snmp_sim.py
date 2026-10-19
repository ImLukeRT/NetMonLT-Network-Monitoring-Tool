"""The simulated SNMP client, credentials, and the simulator driven through the rate maths."""

import asyncio

import pytest

from helpers import FakeClock
from netmonlt.domain import Device, SimInterface, SimProfile, SnmpConfig
from netmonlt.ifstats import advance_device
from netmonlt.snmp import (
    SimulatedSnmpClient,
    SnmpConfigError,
    SnmpCredentials,
    SnmpError,
    credential_env_names,
    load_credentials,
)

GAP = 1000.0


def device(*interfaces, **profile):
    return Device("sw", "192.0.2.10", sim=SimProfile(interfaces=tuple(interfaces), **profile))


def poll_at(client, clock, dev, t):
    clock.now = t
    return asyncio.run(client.poll(dev))


def setup(dev):
    clock = FakeClock(0.0)
    return SimulatedSnmpClient(clock=clock), clock


# ------------------------------------------------------------------ the simulated client
def test_snapshot_is_deterministic_and_counters_only_increase():
    dev = device(SimInterface("Gi0/1", in_mbps=80, out_mbps=20))
    (a, ca), (b, cb) = setup(dev), setup(dev)
    last = None
    for t in (0, 7, 33, 90, 400):
        first = poll_at(a, ca, dev, t).readings[0]
        assert first == poll_at(b, cb, dev, t).readings[0]  # same inputs, same counters
        if last is not None:
            assert first.in_octets > last.in_octets and first.out_octets > last.out_octets
        last = first


def test_octet_counters_match_the_configured_average_rate_exactly_over_whole_periods():
    dev = device(SimInterface("Gi0/1", in_mbps=80, out_mbps=0))
    client, clock = setup(dev)
    start = poll_at(client, clock, dev, 0).readings[0]
    end = poll_at(client, clock, dev, 120).readings[0]  # two full 60 s waves: they cancel out
    assert end.in_octets - start.in_octets == pytest.approx(80_000_000 / 8 * 120, abs=2)
    assert end.out_octets == start.out_octets


def test_rates_computed_from_the_simulator_equal_the_configured_rate():
    dev = device(SimInterface("Gi0/1", in_mbps=80, out_mbps=20))
    client, clock = setup(dev)
    a = {s.if_index: s for s in advance_device({}, None, poll_at(client, clock, dev, 0), 0.0, GAP)}
    b = advance_device(a, None, poll_at(client, clock, dev, 60), 60.0, GAP)
    assert b[0].in_bps == pytest.approx(80_000_000, abs=5)
    assert b[0].out_bps == pytest.approx(20_000_000, abs=5)
    assert b[0].rate_note is None


def test_errors_and_discards_grow_per_minute():
    dev = device(SimInterface("Gi0/4", in_mbps=1, in_errors_per_min=6, out_discards_per_min=2))
    client, clock = setup(dev)
    r = poll_at(client, clock, dev, 120).readings[0]
    assert (r.in_errors, r.out_errors, r.in_discards, r.out_discards) == (12, 0, 0, 4)


def test_a_down_interface_reports_down_and_carries_nothing():
    dev = device(SimInterface("Gi0/3", status="down", in_mbps=500, in_errors_per_min=9))
    client, clock = setup(dev)
    a = poll_at(client, clock, dev, 0).readings[0]
    b = poll_at(client, clock, dev, 300).readings[0]
    assert a.oper_status == "down" == b.oper_status
    assert (a.in_octets, a.in_errors) == (b.in_octets, b.in_errors) and b.in_errors == 0


def test_a_64_bit_counter_rollover_does_not_disturb_the_rate():
    dev = device(SimInterface("Gi0/1", in_mbps=80, out_mbps=80, wrap_at_s=90))
    client, clock = setup(dev)
    before = poll_at(client, clock, dev, 60).readings[0]
    after = poll_at(client, clock, dev, 120).readings[0]
    assert before.in_octets > 2**63 and after.in_octets < 2**63  # it really rolled over
    first = poll_at(client, clock, dev, 60)
    a = {s.if_index: s for s in advance_device({}, None, first, 60.0, GAP)}
    b = advance_device(a, None, poll_at(client, clock, dev, 120), 120.0, GAP)
    assert b[0].in_bps == pytest.approx(80_000_000, abs=5) and b[0].rate_note is None


def test_a_reboot_restarts_every_counter_and_the_uptime():
    iface = SimInterface("Gi0/1", in_mbps=80, out_mbps=80, in_errors_per_min=30)
    dev = device(iface, reboot_at_s=100)
    client, clock = setup(dev)
    before = poll_at(client, clock, dev, 90)
    after = poll_at(client, clock, dev, 110)
    assert after.uptime_ticks < before.uptime_ticks
    assert after.uptime_ticks == 10 * 100  # ten seconds since the reboot
    assert after.readings[0].in_octets < 10**9 < before.readings[0].in_octets
    assert after.readings[0].in_errors == 5  # 30/min for 10 s
    a = {s.if_index: s for s in advance_device({}, None, before, 90.0, GAP)}
    b = advance_device(a, before.uptime_ticks, after, 110.0, GAP)
    assert b[0].in_bps is None and b[0].rate_note == "counter_reset"  # not a negative rate


def test_an_interface_can_disappear_without_renumbering_the_others():
    dev = device(SimInterface("a", in_mbps=1), SimInterface("b", in_mbps=1, disappears_at_s=50),
                 SimInterface("c", in_mbps=1))
    client, clock = setup(dev)
    assert [(r.if_index, r.name) for r in poll_at(client, clock, dev, 10).readings] == [
        (1, "a"), (2, "b"), (3, "c")]
    assert [(r.if_index, r.name) for r in poll_at(client, clock, dev, 60).readings] == [
        (1, "a"), (3, "c")]
    early = poll_at(client, clock, dev, 10)
    a = {s.if_index: s for s in advance_device({}, None, early, 10.0, GAP)}
    late = poll_at(client, clock, dev, 60)
    b = {s.if_index: s for s in advance_device(a, None, late, 60.0, GAP)}
    assert (b[2].present, b[2].rate_note) == (False, "absent")


def test_the_device_outage_makes_snmp_time_out_too():
    dev = device(SimInterface("Gi0/1", in_mbps=1), outage_start_s=30, outage_duration_s=30)
    client, clock = setup(dev)
    poll_at(client, clock, dev, 29)
    for t in (30, 45, 59.9):
        with pytest.raises(SnmpError) as caught:
            poll_at(client, clock, dev, t)
        assert caught.value.category == "timeout"
    poll_at(client, clock, dev, 60)


def test_a_device_with_no_simulated_interfaces_is_unsupported():
    dev = Device("plain", "192.0.2.1")
    client, clock = setup(dev)
    with pytest.raises(SnmpError) as caught:
        poll_at(client, clock, dev, 0)
    assert caught.value.category == "unsupported"


# ------------------------------------------------------------------ errors
def test_snmp_errors_carry_a_fixed_safe_message():
    err = SnmpError("auth_failure")
    assert err.category == "auth_failure" and "authentication failed" in err.message
    assert SnmpError("something-new").category == "something-new"
    assert "unexpected" in SnmpError("something-new").message  # unknown codes stay generic


# ------------------------------------------------------------------ credentials
CONFIG = SnmpConfig(credentials_env="NETMONLT_SNMP_R1")
SECRETS = {
    "NETMONLT_SNMP_R1_USER": "monitor-user",
    "NETMONLT_SNMP_R1_AUTH_KEY": "SECRET-auth-value-123",
    "NETMONLT_SNMP_R1_PRIV_KEY": "SECRET-priv-value-456",
}


def test_credentials_are_read_from_the_environment():
    creds = load_credentials(CONFIG, SECRETS)
    assert (creds.user, creds.auth_key, creds.priv_key) == (
        "monitor-user", "SECRET-auth-value-123", "SECRET-priv-value-456")


def test_environment_variable_names_follow_the_prefix():
    assert credential_env_names(CONFIG) == [
        "NETMONLT_SNMP_R1_USER", "NETMONLT_SNMP_R1_AUTH_KEY", "NETMONLT_SNMP_R1_PRIV_KEY"]
    no_priv = SnmpConfig(credentials_env="X", security_level="authNoPriv")
    assert credential_env_names(no_priv) == ["X_USER", "X_AUTH_KEY"]


def test_missing_variables_are_listed_by_name_only():
    env = {"NETMONLT_SNMP_R1_USER": "monitor-user", "NETMONLT_SNMP_R1_AUTH_KEY": ""}
    with pytest.raises(SnmpConfigError) as caught:
        load_credentials(CONFIG, env)
    message = str(caught.value)
    assert "NETMONLT_SNMP_R1_AUTH_KEY" in message and "NETMONLT_SNMP_R1_PRIV_KEY" in message
    assert "monitor-user" not in message and "NETMONLT_SNMP_R1_USER" not in message


def test_auth_no_priv_does_not_need_or_use_a_privacy_key():
    cfg = SnmpConfig(credentials_env="NETMONLT_SNMP_R1", security_level="authNoPriv")
    creds = load_credentials(cfg, SECRETS)
    assert creds.priv_key is None  # even though one is in the environment


def test_short_keys_are_rejected_without_revealing_them():
    env = {**SECRETS, "NETMONLT_SNMP_R1_AUTH_KEY": "short"}
    with pytest.raises(SnmpConfigError) as caught:
        load_credentials(CONFIG, env)
    message = str(caught.value)
    assert "NETMONLT_SNMP_R1_AUTH_KEY" in message and "too short" in message
    assert '"short"' not in message and "='short'" not in message  # the value itself isn't shown


def test_credentials_never_appear_in_repr_or_str():
    creds = load_credentials(CONFIG, SECRETS)
    for text in (repr(creds), str(creds), f"{creds}", f"{[creds]}"):
        assert "SECRET" not in text and "monitor-user" not in text
    assert isinstance(creds, SnmpCredentials)


def test_the_process_environment_is_used_by_default(monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    assert load_credentials(CONFIG).user == "monitor-user"
