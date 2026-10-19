"""Inventory rules for SNMP: opt-in, SNMPv3 only, no secrets in the file, simulated interfaces."""

import pytest

from netmonlt.domain import SimInterface, SnmpConfig
from netmonlt.inventory import InventoryError, parse_inventory


def snmp(**over):
    return {"enabled": True, "credentials_env": "NETMONLT_SNMP_R1", **over}


def real(device=None, **top):
    dev = {"name": "r1", "address": "192.0.2.1", "authorized": True, **(device or {})}
    return {"mode": "real", "poll_interval_s": 10, "devices": [dev], **top}


def sim(**device):
    return {"devices": [{"name": "r1", "address": "x", **device}]}


# ------------------------------------------------------------------ opt-in
def test_snmp_is_off_by_default():
    inv = parse_inventory(real())
    assert inv.devices[0].snmp is None and inv.snmp_devices == ()


@pytest.mark.parametrize("block", [
    {},
    {"enabled": False, "credentials_env": "NETMONLT_SNMP_R1"},
    {"credentials_env": "NETMONLT_SNMP_R1"},
])
def test_an_snmp_block_alone_never_turns_polling_on(block):
    inv = parse_inventory(real({"snmp": block}))
    assert inv.devices[0].snmp is None and inv.snmp_devices == ()


def test_enabling_snmp_uses_safe_defaults():
    inv = parse_inventory(real({"snmp": snmp()}))
    assert inv.devices[0].snmp == SnmpConfig(
        credentials_env="NETMONLT_SNMP_R1", security_level="authPriv", auth_protocol="SHA256",
        priv_protocol="AES128", port=161, timeout_s=5.0, retries=1)
    assert inv.snmp_devices == (inv.devices[0],)


def test_only_the_devices_that_enable_snmp_are_polled():
    raw = real()
    raw["devices"].append({"name": "r2", "address": "192.0.2.2", "authorized": True})
    raw["devices"][0]["snmp"] = snmp()
    assert [d.name for d in parse_inventory(raw).snmp_devices] == ["r1"]


def test_real_snmp_still_requires_an_authorized_single_host():
    with pytest.raises(InventoryError, match="authorized: true"):
        parse_inventory(real({"authorized": False, "snmp": snmp()}))
    with pytest.raises(InventoryError, match="address"):
        parse_inventory(real({"address": "10.0.0.0/24", "snmp": snmp()}))


def test_enabled_must_be_a_boolean():
    with pytest.raises(InventoryError, match="enabled must be true or false"):
        parse_inventory(real({"snmp": snmp(enabled="yes please")}))


# ------------------------------------------------------------------ no secrets in the file
@pytest.mark.parametrize("key", [
    "community", "version", "user", "username", "password", "auth_key", "priv_key", "passphrase",
])
def test_secrets_and_snmpv2c_cannot_be_written_in_the_inventory(key):
    with pytest.raises(InventoryError) as caught:
        parse_inventory(real({"snmp": snmp(**{key: "SECRET-VALUE-123"})}))
    message = str(caught.value)
    assert key in message and "environment variable" in message
    assert "SECRET-VALUE-123" not in message  # the value is never echoed


@pytest.mark.parametrize("prefix", [
    None, "", "lower_case", "1STARTS_WITH_DIGIT", "HAS SPACE", "A" * 61, 5,
])
def test_credentials_env_must_be_a_safe_variable_prefix(prefix):
    block = snmp()
    if prefix is None:
        del block["credentials_env"]
    else:
        block["credentials_env"] = prefix
    with pytest.raises(InventoryError, match="credentials_env"):
        parse_inventory(real({"snmp": block}))


# ------------------------------------------------------------------ protocols and limits
@pytest.mark.parametrize("auth", ["SHA", "SHA224", "SHA256", "SHA384", "SHA512"])
def test_supported_authentication_protocols(auth):
    parsed = parse_inventory(real({"snmp": snmp(auth_protocol=auth)}))
    assert parsed.devices[0].snmp.auth_protocol == auth


@pytest.mark.parametrize("auth", ["MD5", "md5", "sha256", "SHA-256", "", None, 3])
def test_weak_or_unknown_authentication_protocols_are_rejected(auth):
    with pytest.raises(InventoryError, match="auth_protocol"):
        parse_inventory(real({"snmp": snmp(auth_protocol=auth)}))


@pytest.mark.parametrize("priv", ["AES128", "AES192", "AES256"])
def test_supported_privacy_protocols(priv):
    parsed = parse_inventory(real({"snmp": snmp(priv_protocol=priv)}))
    assert parsed.devices[0].snmp.priv_protocol == priv


@pytest.mark.parametrize("priv", ["DES", "3DES", "AES", "aes128", None])
def test_weak_or_unknown_privacy_protocols_are_rejected(priv):
    with pytest.raises(InventoryError, match="priv_protocol"):
        parse_inventory(real({"snmp": snmp(priv_protocol=priv)}))


def test_security_level_cannot_be_no_auth_no_priv():
    parsed = parse_inventory(real({"snmp": snmp(security_level="authNoPriv")}))
    assert parsed.devices[0].snmp.security_level == "authNoPriv"
    for level in ("noAuthNoPriv", "authpriv", None):
        with pytest.raises(InventoryError, match="security_level"):
            parse_inventory(real({"snmp": snmp(security_level=level)}))


@pytest.mark.parametrize("key, bad", [
    ("port", 0), ("port", 65536), ("port", 161.5), ("port", "161"),
    ("timeout_s", 0.5), ("timeout_s", 31), ("retries", 4), ("retries", -1), ("retries", 1.5),
])
def test_numeric_snmp_settings_are_range_checked(key, bad):
    with pytest.raises(InventoryError, match=key):
        parse_inventory(real({"snmp": snmp(**{key: bad})}))


def test_unknown_snmp_keys_are_rejected():
    with pytest.raises(InventoryError, match="unknown key"):
        parse_inventory(real({"snmp": snmp(walk_everything=True)}))


def test_snmp_must_be_a_mapping():
    with pytest.raises(InventoryError, match="'snmp' must be a mapping"):
        parse_inventory(real({"snmp": ["enabled"]}))


# ------------------------------------------------------------------ the polling interval
def test_snmp_interval_defaults_and_minimums():
    assert parse_inventory(real()).snmp_poll_interval_s == 30
    assert parse_inventory(real(snmp_poll_interval_s=10)).snmp_poll_interval_s == 10
    with pytest.raises(InventoryError, match="snmp_poll_interval_s"):
        parse_inventory(real(snmp_poll_interval_s=9))  # real devices are polled gently
    assert parse_inventory({**sim(), "snmp_poll_interval_s": 1}).snmp_poll_interval_s == 1
    with pytest.raises(InventoryError, match="snmp_poll_interval_s"):
        parse_inventory({**sim(), "snmp_poll_interval_s": 0.5})


# ------------------------------------------------------------------ simulated vs real never mix
def test_simulated_mode_never_polls_a_real_device_even_if_snmp_is_configured():
    inv = parse_inventory(sim(snmp=snmp()))
    assert inv.devices[0].snmp is not None  # parsed and validated ...
    assert inv.snmp_devices == ()  # ... but never used outside real mode


def test_real_mode_ignores_simulated_interfaces():
    raw = real({"sim": {"interfaces": [{"name": "Gi0/1"}]}})
    assert parse_inventory(raw).snmp_devices == ()


def test_simulated_interfaces_make_a_device_a_simulated_snmp_device():
    inv = parse_inventory(sim(sim={"interfaces": [{"name": "Gi0/1", "in_mbps": 5}]}))
    assert inv.snmp_devices == (inv.devices[0],)


# ------------------------------------------------------------------ sim.interfaces
def test_simulated_interface_fields_and_defaults():
    inv = parse_inventory(sim(sim={"reboot_at_s": 90, "interfaces": [
        {"name": "Gi0/1", "in_mbps": 120, "out_mbps": 45.5, "status": "down",
         "in_errors_per_min": 3, "out_errors_per_min": 1, "in_discards_per_min": 2,
         "out_discards_per_min": 4, "wrap_at_s": 60, "disappears_at_s": 120},
        {"name": "Gi0/2"},
    ]}))
    profile = inv.devices[0].sim
    assert profile.reboot_at_s == 90
    assert profile.interfaces[0] == SimInterface("Gi0/1", 120, 45.5, "down", 3, 1, 2, 4, 60, 120)
    assert profile.interfaces[1] == SimInterface("Gi0/2")


@pytest.mark.parametrize("interfaces, message", [
    ("Gi0/1", "must be a list"),
    ([["Gi0/1"]], "must be a mapping"),
    ([{}], "name"),
    ([{"name": ""}], "name"),
    ([{"name": "bad name"}], "name"),
    ([{"name": "Gi0/1"}, {"name": "Gi0/1"}], "duplicate interface"),
    ([{"name": "a", "status": "sideways"}], "status"),
    ([{"name": "a", "in_mbps": -1}], "in_mbps"),
    ([{"name": "a", "in_mbps": float("nan")}], "in_mbps"),
    ([{"name": "a", "in_mbps": True}], "in_mbps"),
    ([{"name": "a", "wrap_at_s": -5}], "wrap_at_s"),
    ([{"name": "a", "speed": 100}], "unknown key"),
    ([{"name": f"if{i}"} for i in range(65)], "at most 64"),
])
def test_invalid_simulated_interfaces_are_rejected(interfaces, message):
    with pytest.raises(InventoryError, match=message):
        parse_inventory(sim(sim={"interfaces": interfaces}))


# ------------------------------------------------------------------ the shipped examples stay valid
def example_text(name):
    from helpers import REPO_ROOT

    return (REPO_ROOT / "config" / name).read_text(encoding="utf-8")


def test_the_demo_inventory_has_simulated_interfaces_and_a_polling_interval():
    from helpers import REPO_ROOT
    from netmonlt.inventory import load_inventory

    inv = load_inventory(REPO_ROOT / "config" / "devices.sim.yaml")
    assert inv.snmp_poll_interval_s == 10
    names = [d.name for d in inv.snmp_devices]
    assert {"core-router", "access-switch", "edge-firewall"} <= set(names)
    interfaces = {i.name: i for d in inv.snmp_devices for i in d.sim.interfaces}
    assert interfaces["Gi0/1"].wrap_at_s is not None  # the rollover demo
    assert any(d.sim.reboot_at_s is not None for d in inv.snmp_devices)  # the restart demo
    assert any(i.disappears_at_s is not None for i in interfaces.values())  # the vanishing demo
    assert interfaces["Gi0/3"].status == "down"


def test_the_real_example_does_not_enable_snmp():
    from helpers import REPO_ROOT
    from netmonlt.inventory import load_inventory

    inv = load_inventory(REPO_ROOT / "config" / "devices.real.example.yaml")
    assert inv.snmp_devices == () and all(d.snmp is None for d in inv.devices)


def test_the_commented_snmp_example_in_the_real_inventory_is_valid_when_uncommented():
    lines = example_text("devices.real.example.yaml").splitlines()
    start = max(i for i, line in enumerate(lines) if line.startswith("  # - name: home-router"))
    block = []
    for line in lines[start:]:
        if not line.startswith("  #"):
            break
        block.append(line[4:])  # drop the "  # " comment prefix
    devices = pytest.importorskip("yaml").safe_load("\n".join(block))
    inv = parse_inventory({"mode": "real", "poll_interval_s": 10, "devices": devices})
    snmp_config = inv.devices[0].snmp
    assert snmp_config is not None and snmp_config.credentials_env == "NETMONLT_SNMP_HOME_ROUTER"
    assert (snmp_config.security_level, snmp_config.auth_protocol) == ("authPriv", "SHA256")


def test_the_credentials_template_matches_the_documented_prefix_and_holds_no_real_secrets():
    from netmonlt.snmp import credential_env_names, load_credentials

    env = {}
    for line in example_text("snmp.env.example").splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            env[key] = value
    config = SnmpConfig("NETMONLT_SNMP_HOME_ROUTER")
    assert sorted(env) == sorted(credential_env_names(config))
    assert all(v.startswith("replace-with-") for v in env.values())
    assert load_credentials(config, env).user.startswith("replace-with-")  # long enough to load
