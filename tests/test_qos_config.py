"""The inventory's QoS settings: opt-in per device, supported adapters only, strict validation."""

import pytest

from helpers import REPO_ROOT
from netmonlt.domain import QosConfig
from netmonlt.inventory import InventoryError, load_inventory, parse_inventory

SNMP = {"enabled": True, "credentials_env": "NETMONLT_SNMP_R1"}


def real(device_extra=None, **top):
    device = {"name": "r1", "address": "127.0.0.1", "authorized": True, "snmp": SNMP,
              **(device_extra or {})}
    return parse_inventory({"mode": "real", "poll_interval_s": 10, "devices": [device], **top})


def simulated(sim=None, **top):
    device = {"name": "r1", "address": "192.0.2.1", **({"sim": sim} if sim else {})}
    return parse_inventory({"devices": [device], **top})


# ------------------------------------------------------------------ real devices
def test_qos_is_off_unless_a_device_enables_it():
    assert real().devices[0].qos is None and real().qos_devices == ()
    assert real({"qos": {"adapter": "diffserv-mib"}}).devices[0].qos is None  # a block alone
    assert real({"qos": {"enabled": False, "adapter": "diffserv-mib"}}).qos_devices == ()
    assert real({"qos": {"enabled": False}}).qos_devices == ()


def test_enabling_qos_selects_one_supported_adapter():
    inv = real({"qos": {"enabled": True, "adapter": "diffserv-mib"}})
    assert inv.devices[0].qos == QosConfig("diffserv-mib")
    assert inv.qos_devices == (inv.devices[0],)


def test_qos_reuses_the_devices_snmp_credentials_so_no_new_secret_keys_exist():
    inv = real({"qos": {"enabled": True, "adapter": "diffserv-mib"}})
    assert inv.devices[0].snmp.credentials_env == "NETMONLT_SNMP_R1"  # the same prefix
    for key in ("community", "user", "password", "auth_key", "priv_key", "credentials_env"):
        with pytest.raises(InventoryError, match="unknown key"):
            real({"qos": {"enabled": True, "adapter": "diffserv-mib", key: "x"}})


@pytest.mark.parametrize("adapter", ["cisco-cbqos", "DIFFSERV-MIB", "diffserv", "", "auto",
                                     "simulated", "../etc", 5, ["diffserv-mib"]])
def test_unsupported_adapters_are_rejected_even_when_not_enabled(adapter):
    for enabled in (True, False):
        with pytest.raises(InventoryError, match="not supported.*diffserv-mib"):
            real({"qos": {"enabled": enabled, "adapter": adapter}})


def test_an_unsupported_adapter_message_says_oids_are_never_guessed():
    with pytest.raises(InventoryError, match="never guesses OIDs"):
        real({"qos": {"enabled": True, "adapter": "vendor-x"}})


def test_enabling_qos_needs_an_adapter():
    with pytest.raises(InventoryError, match="adapter is required"):
        real({"qos": {"enabled": True}})


def test_real_qos_needs_snmp_on_the_same_device():
    raw = {"mode": "real", "poll_interval_s": 10, "devices": [{
        "name": "r1", "address": "127.0.0.1", "authorized": True,
        "qos": {"enabled": True, "adapter": "diffserv-mib"}}]}
    with pytest.raises(InventoryError, match="reuses the device's SNMPv3 credentials"):
        parse_inventory(raw)
    raw["devices"][0]["snmp"] = {"enabled": False, "credentials_env": "NETMONLT_SNMP_R1"}
    with pytest.raises(InventoryError, match="reuses the device's SNMPv3 credentials"):
        parse_inventory(raw)  # an snmp block that is not enabled does not count


def test_real_qos_still_needs_the_device_to_be_authorized_and_a_single_host():
    for extra, message in (({"authorized": False}, "authorized"),
                           ({"address": "10.0.0.0/24"}, "address")):
        with pytest.raises(InventoryError, match=message):
            real({**extra, "qos": {"enabled": True, "adapter": "diffserv-mib"}})


@pytest.mark.parametrize("qos, message", [
    ("yes", "must be a mapping"),
    ({"enabled": "true", "adapter": "diffserv-mib"}, "true or false"),
    ({"enabled": 1, "adapter": "diffserv-mib"}, "true or false"),
    ({"enable": True}, "unknown key"),
    ({"poll_interval_s": 5}, "unknown key"),
])
def test_malformed_qos_blocks_are_rejected(qos, message):
    with pytest.raises(InventoryError, match=message):
        real({"qos": qos})


def test_qos_devices_in_real_mode_ignore_a_simulated_policy():
    inv = real({"sim": {"qos": {"classes": [{"name": "a", "offered_mbps": 1}]}},
                "qos": {"enabled": True, "adapter": "diffserv-mib"}})
    assert inv.devices[0].sim.qos is not None  # parsed and validated, but real mode never uses it
    assert inv.qos_devices[0].qos.adapter == "diffserv-mib"
    assert real({"sim": {"qos": {"classes": [{"name": "a", "offered_mbps": 1}]}}}).qos_devices == ()


# ------------------------------------------------------------------ intervals and retention
def test_defaults_and_overrides():
    inv = simulated()
    assert (inv.qos_poll_interval_s, inv.qos_retention_hours) == (30.0, 24.0)
    inv = simulated(qos_poll_interval_s=5, qos_retention_hours=48)
    assert (inv.qos_poll_interval_s, inv.qos_retention_hours) == (5.0, 48.0)


@pytest.mark.parametrize("top, message", [
    ({"qos_poll_interval_s": 0.5}, "qos_poll_interval_s"),
    ({"qos_poll_interval_s": "fast"}, "must be a number"),
    ({"qos_retention_hours": 0}, "qos_retention_hours"),
    ({"qos_retention_hours": 1000}, "qos_retention_hours"),
])
def test_bad_intervals_are_rejected(top, message):
    with pytest.raises(InventoryError, match=message):
        simulated(**top)


def test_real_qos_polling_is_kept_gentle():
    with pytest.raises(InventoryError, match="qos_poll_interval_s"):
        real(qos_poll_interval_s=5)
    assert real(qos_poll_interval_s=10).qos_poll_interval_s == 10.0


# ------------------------------------------------------------------ the simulated policy
POLICY = {"qos": {
    "interface": "wan0", "capacity_mbps": 50,
    "congestion": {"start_s": 10, "duration_s": 20, "surge_factor": 3},
    "classes": [
        {"name": "Voice", "offered_mbps": 5, "priority": True, "avg_packet_bytes": 200},
        {"name": "Bulk", "offered_mbps": 30, "weight": 2.5, "surges": True, "counter_bits": 32,
         "wrap_at_s": 40},
    ]}}


def test_a_simulated_policy_is_parsed():
    inv = simulated(POLICY)
    profile = inv.devices[0].sim.qos
    assert (profile.interface, profile.capacity_mbps) == ("wan0", 50)
    assert profile.congestion_start_s == 10
    assert (profile.congestion_duration_s, profile.surge_factor) == (20, 3)
    voice, bulk = profile.classes
    assert voice.priority and voice.avg_packet_bytes == 200 and voice.counter_bits == 64
    assert (bulk.weight, bulk.surges, bulk.counter_bits, bulk.wrap_at_s) == (2.5, True, 32, 40)
    assert inv.qos_devices == (inv.devices[0],)


def test_in_simulated_mode_only_devices_with_a_policy_have_qos():
    assert simulated().qos_devices == ()
    assert simulated({"rtt_ms": 3}).qos_devices == ()


def test_a_qos_block_in_simulated_mode_is_validated_but_never_enables_real_polling():
    inv = parse_inventory({"devices": [{"name": "a", "address": "x",
                                        "qos": {"enabled": True, "adapter": "diffserv-mib"}}]})
    assert inv.qos_devices == ()  # simulated devices get QoS only from sim.qos


def mutate(**changes):
    policy = {"qos": {**POLICY["qos"], **changes}}
    return policy


@pytest.mark.parametrize("sim, message", [
    ({"qos": "x"}, "must be a mapping"),
    ({"qos": {"classes": []}}, "non-empty list"),
    ({"qos": {}}, "non-empty list"),
    (mutate(colour="red"), "unknown key"),
    (mutate(interface="bad name"), "interface"),
    (mutate(capacity_mbps=0), "capacity_mbps"),
    (mutate(congestion={"start_s": 5}), "start_s and duration_s"),
    (mutate(congestion={"start_s": 5, "duration_s": 5, "surge_factor": 0.5}), "surge_factor"),
    (mutate(congestion={"start_s": -1, "duration_s": 5}), "start_s"),
    (mutate(congestion="soon"), "congestion must be a mapping"),
    (mutate(classes=[{"name": "a", "offered_mbps": 1}] * 2), "duplicate class name"),
    (mutate(classes=[{"name": "a"}]), "offered_mbps"),
    (mutate(classes=[{"offered_mbps": 1}]), "name"),
    (mutate(classes=[{"name": "x" * 41, "offered_mbps": 1}]), "name"),
    (mutate(classes=[{"name": "a", "offered_mbps": -1}]), "offered_mbps"),
    (mutate(classes=[{"name": "a", "offered_mbps": 1, "weight": 0}]), "weight"),
    (mutate(classes=[{"name": "a", "offered_mbps": 1, "counter_bits": 16}]), "32 or 64"),
    (mutate(classes=[{"name": "a", "offered_mbps": 1, "priority": "yes"}]), "true or false"),
    (mutate(classes=[{"name": "a", "offered_mbps": 1, "avg_packet_bytes": 10}]),
     "avg_packet_bytes"),
    (mutate(classes=[{"name": "a", "offered_mbps": 1, "avg_packet_bytes": 100.5}]),
     "whole number"),
    (mutate(classes=[{"name": "a", "offered_mbps": 1, "wrap_at_s": 10**7}]), "wrap_at_s"),
    (mutate(classes=[{"name": "a", "offered_mbps": 1, "dscp": 46}]), "unknown key"),
    (mutate(classes=[{"name": "a", "offered_mbps": 1}] * 17), "more than 16"),
    (mutate(classes="all"), "non-empty list"),
])
def test_bad_simulated_policies_are_rejected(sim, message):
    with pytest.raises(InventoryError, match=message):
        simulated(sim)


def test_class_names_cannot_claim_to_be_observed_dscp_values_through_a_dscp_key():
    # a class has no DSCP field: names are labels only, and nothing here infers a marking
    with pytest.raises(InventoryError, match="unknown key"):
        simulated(mutate(classes=[{"name": "Voice", "offered_mbps": 1, "dscp": "EF"}]))


# ------------------------------------------------------------------ shipped examples
def test_the_shipped_demo_has_one_simulated_qos_device_with_the_scripted_story():
    inv = load_inventory(REPO_ROOT / "config" / "devices.sim.yaml")
    (dev,) = inv.qos_devices
    assert dev.name == "edge-firewall" and dev.qos is None  # simulated: from sim.qos only
    profile = dev.sim.qos
    assert len(profile.classes) >= 4 and any(c.priority for c in profile.classes)
    assert any(c.counter_bits == 32 and c.wrap_at_s for c in profile.classes)
    assert any(c.surges for c in profile.classes) and profile.congestion_start_s is not None
    assert inv.qos_poll_interval_s == 5


def test_the_real_example_keeps_qos_off_and_documents_how_to_enable_it():
    path = REPO_ROOT / "config" / "devices.real.example.yaml"
    inv = load_inventory(path)
    assert inv.qos_devices == () and all(d.qos is None for d in inv.devices)
    text = path.read_text(encoding="utf-8")
    commented = [line for line in text.splitlines() if line.lstrip().startswith("#")]
    assert any(line.lstrip("# ").startswith("qos:") for line in commented)  # only as a comment
    assert "diffserv-mib" in text and "read-only" in text and "OFF unless enabled" in text
