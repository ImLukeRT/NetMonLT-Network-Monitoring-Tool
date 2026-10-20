"""Wi-Fi core: reading validation, SSID privacy, netsh parsing, the simulated source, low-signal
warnings and the history series. Pure logic: no adapter, no Windows, no network."""

import asyncio

import pytest

from helpers import FakeClock
from netmonlt.domain import WifiConfig, WifiSimProfile
from netmonlt.wifi import (
    CATEGORY_NO_ADAPTER,
    CATEGORY_PARSE,
    CATEGORY_UNAVAILABLE,
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    STATE_NOT_READY,
    SimulatedWifiSource,
    WifiError,
    WifiReading,
    WifiSample,
    apply_ssid_privacy,
    choose_reading,
    field_provenance,
    low_signal_warning,
    mask_ssid,
    normalize_reading,
    parse_netsh_interfaces,
    series,
)


def norm(state=STATE_CONNECTED, **fields):
    return normalize_reading(state, source="wlanapi", **fields)


# ------------------------------------------------------------------ validation
def test_a_connected_reading_keeps_valid_values():
    r = norm(ssid="Home", interface="Wi-Fi", signal_pct=72, rssi_dbm=-58, rssi_source="driver",
             rx_mbps=866.7, tx_mbps=433.3, radio_type="802.11ac", channel=36)
    assert (r.signal_pct, r.rssi_dbm, r.rssi_source) == (72, -58, "driver")
    assert (r.rx_mbps, r.tx_mbps, r.radio_type, r.channel) == (866.7, 433.3, "802.11ac", 36)


@pytest.mark.parametrize("field, bad", [
    ("signal_pct", 101), ("signal_pct", -1), ("signal_pct", "80"), ("signal_pct", 55.5),
    ("signal_pct", True), ("signal_pct", float("nan")),
    ("rssi_dbm", 5), ("rssi_dbm", -500), ("rssi_dbm", "x"),
    ("rx_mbps", 0), ("rx_mbps", -3), ("rx_mbps", 10**9), ("rx_mbps", float("inf")),
    ("tx_mbps", "fast"), ("channel", 0), ("channel", 5000), ("channel", 36.5),
    ("radio_type", "802.11ax; DROP TABLE"), ("radio_type", ""), ("radio_type", 11),
])
def test_out_of_range_or_malformed_values_become_unavailable_never_clamped(field, bad):
    r = norm(**{field: bad, "rssi_source": "driver"})
    assert getattr(r, field) is None


def test_an_rssi_source_is_dropped_when_there_is_no_rssi():
    assert norm(rssi_dbm=None, rssi_source="driver").rssi_source is None
    assert norm(rssi_dbm=7, rssi_source="driver").rssi_source is None  # implausible value


def test_signal_percentage_is_never_turned_into_dbm():
    r = norm(signal_pct=80)
    assert r.rssi_dbm is None and r.rssi_source is None


@pytest.mark.parametrize("state", ["disconnected", "connecting", "disconnecting", "not_ready",
                                   "unknown"])
def test_only_a_connected_adapter_carries_link_figures(state):
    r = normalize_reading(state, source="x", ssid="Home", signal_pct=50, rssi_dbm=-60,
                          rssi_source="d", rx_mbps=10, tx_mbps=10, radio_type="802.11n",
                          channel=6, interface="Wi-Fi")
    assert r.state == state and r.interface == "Wi-Fi"
    assert (r.ssid, r.signal_pct, r.rssi_dbm, r.rx_mbps, r.tx_mbps) == (None,) * 5
    assert (r.radio_type, r.channel) == (None, None)


def test_an_unrecognised_state_is_unknown():
    assert normalize_reading("flying", source="x").state == "unknown"
    assert normalize_reading(None, source="x").state == "unknown"


def test_text_is_cleaned_and_bounded():
    r = norm(ssid="Ho\x00me\x1b[31m\n", interface="W" * 200)
    assert r.ssid == "Home[31m" and len(r.interface) == 64
    assert norm(ssid="").ssid is None and norm(ssid=42).ssid is None


def test_a_zero_signal_is_a_real_value_not_missing():
    assert norm(signal_pct=0).signal_pct == 0


# ------------------------------------------------------------------ SSID privacy
@pytest.mark.parametrize("ssid, expected", [
    ("a", "a"), ("ab", "a*"), ("HomeWiFi", "H*******"), ("x" * 40, "x" + "*" * 11),
    ("Café-5G", "C******"),
])
def test_masking_keeps_the_first_character_and_hides_the_rest(ssid, expected):
    assert mask_ssid(ssid) == expected


def test_privacy_modes():
    r = WifiReading(STATE_CONNECTED, ssid="HomeWiFi", signal_pct=70)
    assert apply_ssid_privacy(r, "full").ssid == "HomeWiFi"
    assert apply_ssid_privacy(r, "masked").ssid == "H*******"
    assert apply_ssid_privacy(r, "hidden").ssid is None
    assert apply_ssid_privacy(r, "masked").signal_pct == 70  # nothing else changes
    assert apply_ssid_privacy(WifiReading(STATE_DISCONNECTED), "masked").ssid is None


def test_masking_twice_changes_nothing():
    r = apply_ssid_privacy(WifiReading(STATE_CONNECTED, ssid="HomeWiFi"), "masked")
    assert apply_ssid_privacy(r, "masked").ssid == r.ssid


# ------------------------------------------------------------------ netsh parsing (English)
CONNECTED = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : 11111111-2222-3333-4444-555555555555
    Physical address       : aa:bb:cc:dd:ee:ff
    State                  : connected
    SSID                   : HomeWiFi
    BSSID                  : 11:22:33:44:55:66
    Network type           : Infrastructure
    Radio type             : 802.11ax
    Authentication         : WPA2-Personal
    Cipher                 : CCMP
    Connection mode        : Auto Connect
    Band                   : 5 GHz
    Channel                : 36
    Receive rate (Mbps)    : 866.7
    Transmit rate (Mbps)   : 780
    Signal                 : 99%
    Profile                : HomeWiFi

    Hosted network status  : Not available
"""

DISCONNECTED = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : 11111111-2222-3333-4444-555555555555
    Physical address       : aa:bb:cc:dd:ee:ff
    State                  : disconnected
    Hosted network status  : Not available
"""


def test_a_connected_interface_is_parsed():
    (r,) = parse_netsh_interfaces(CONNECTED)
    assert r.state == "connected" and r.ssid == "HomeWiFi" and r.interface == "Wi-Fi"
    assert (r.signal_pct, r.rx_mbps, r.tx_mbps) == (99, 866.7, 780.0)
    assert (r.radio_type, r.channel, r.source) == ("802.11ax", 36, "netsh")
    assert r.rssi_dbm is None and r.rssi_source is None  # netsh gave no RSSI: none invented


def test_bssid_mac_guid_and_profile_are_never_kept():
    (r,) = parse_netsh_interfaces(CONNECTED)
    everything = repr(r).lower()
    for secret in ("11:22:33:44:55:66", "aa:bb:cc:dd:ee:ff", "11111111-2222", "wpa2", "intel"):
        assert secret not in everything


def test_a_disconnected_interface_has_a_state_and_nothing_else():
    (r,) = parse_netsh_interfaces(DISCONNECTED)
    assert r.state == "disconnected" and r.interface == "Wi-Fi"
    assert r.ssid is None and r.signal_pct is None and r.rx_mbps is None


def test_a_reported_rssi_line_is_used_and_labelled_with_its_source():
    text = CONNECTED.replace("    Signal                 : 99%",
                             "    Signal                 : 99%\n    Rssi                   : -47")
    (r,) = parse_netsh_interfaces(text)
    assert r.rssi_dbm == -47 and r.rssi_source == "netsh (Rssi field)"


def test_an_implausible_rssi_line_is_discarded():
    text = CONNECTED.replace("    Signal                 : 99%",
                             "    Signal                 : 99%\n    Rssi                   : 47")
    assert parse_netsh_interfaces(text)[0].rssi_dbm is None


def test_decimal_commas_crlf_and_extra_blank_lines_are_tolerated():
    text = CONNECTED.replace("866.7", "866,7").replace("\n", "\r\n\r\n")
    (r,) = parse_netsh_interfaces(text)
    assert r.rx_mbps == 866.7 and r.signal_pct == 99


def test_an_ssid_containing_a_colon_survives():
    text = CONNECTED.replace(": HomeWiFi\n    BSSID", ": Cafe : 5G\n    BSSID")
    assert parse_netsh_interfaces(text)[0].ssid == "Cafe : 5G"


def test_two_interfaces_are_both_parsed_and_the_connected_one_is_chosen():
    second = DISCONNECTED.replace("There is 1 interface on the system:", "")
    readings = parse_netsh_interfaces(second + CONNECTED)
    assert [r.state for r in readings] == ["disconnected", "connected"]
    assert choose_reading(readings).state == "connected"
    assert choose_reading(readings[:1]).state == "disconnected"


def test_signal_with_a_space_before_the_percent_sign_is_accepted():
    text = CONNECTED.replace("99%", "58 %")
    assert parse_netsh_interfaces(text)[0].signal_pct == 58


def test_no_wireless_interface_is_reported_as_no_adapter():
    text = "There is no wireless interface on the system.\n"
    with pytest.raises(WifiError) as caught:
        parse_netsh_interfaces(text)
    assert caught.value.category == CATEGORY_NO_ADAPTER


def test_a_stopped_wlan_service_is_reported_as_unavailable():
    text = "The Wireless AutoConfig Service (wlansvc) is not running.\n"
    with pytest.raises(WifiError) as caught:
        parse_netsh_interfaces(text)
    assert caught.value.category == CATEGORY_UNAVAILABLE


def test_a_powered_down_adapter_is_a_not_ready_state_not_an_error():
    text = ("The wireless local area network interface is powered down and doesn't support "
            "the requested operation.\n")
    (r,) = parse_netsh_interfaces(text)
    assert r.state == STATE_NOT_READY and r.signal_pct is None


@pytest.mark.parametrize("text", [
    "", "   \n\n", "garbage with no structure", "Name : Wi-Fi\n",  # no recognisable state
    # a German-language interface block: the labels are translated, so it is not recognised
    "    Name                   : WLAN\n    Status                 : Verbunden\n"
    "    SSID                   : Heim\n    Signal                 : 80%\n",
])
def test_unrecognised_output_is_a_parse_error_never_a_guess(text):
    with pytest.raises(WifiError) as caught:
        parse_netsh_interfaces(text)
    assert caught.value.category == CATEGORY_PARSE


def test_choosing_from_nothing_is_no_adapter():
    with pytest.raises(WifiError) as caught:
        choose_reading([])
    assert caught.value.category == CATEGORY_NO_ADAPTER


# ------------------------------------------------------------------ simulated source
def read_at(profile, seconds):
    clock = FakeClock(0.0)
    source = SimulatedWifiSource(profile, clock)
    clock.now = seconds
    return asyncio.run(source.read())


def test_the_simulation_is_deterministic():
    profile = WifiSimProfile()
    first = [read_at(profile, t) for t in range(0, 200, 7)]
    second = [read_at(profile, t) for t in range(0, 200, 7)]
    assert first == second


def test_the_simulated_script_connects_dips_disconnects_and_recovers():
    p = WifiSimProfile()
    steady, weak = read_at(p, 10), read_at(p, 70)
    gone, back = read_at(p, 135), read_at(p, 160)
    assert steady.state == "connected" and 60 <= steady.signal_pct <= 100
    assert weak.state == "connected" and weak.signal_pct < 40  # below the default threshold
    assert gone.state == "disconnected" and gone.signal_pct is None and gone.ssid is None
    assert back.state == "connected" and back.signal_pct > 60


def test_everything_the_simulation_reports_is_labelled_simulated():
    r = read_at(WifiSimProfile(), 10)
    assert r.source == "simulated" and r.rssi_source == "simulated"
    assert r.interface == "Wi-Fi (simulated)"
    assert set(field_provenance(r).values()) == {"simulated"}
    assert 1 <= r.signal_pct <= 100 and r.channel == 36 and r.radio_type == "802.11ax"


def test_the_simulation_respects_the_configured_script():
    quiet = WifiSimProfile(dip_at_s=None, disconnect_at_s=None, base_signal_pct=90)
    signals = [read_at(quiet, t).signal_pct for t in range(0, 400, 5)]
    assert min(signals) > 70 and all(read_at(quiet, t).state == "connected" for t in (70, 135))
    custom = WifiSimProfile(ssid="Lab", disconnect_at_s=5, disconnect_duration_s=5)
    assert read_at(custom, 6).state == "disconnected" and read_at(custom, 11).ssid == "Lab"


def test_the_simulation_never_reads_the_system():
    # it needs nothing but a clock: no adapter, no OS call, so it runs anywhere
    assert asyncio.run(SimulatedWifiSource().read()).state in {"connected", "disconnected"}


# ------------------------------------------------------------------ low-signal warning
CFG = WifiConfig(low_signal_pct=40, warn_after_polls=3)
STALE = 35.0


def samples(*values, step=5.0, end=1000.0, state=STATE_CONNECTED):
    """Newest first: values are given oldest first."""
    out = []
    for i, v in enumerate(reversed(values)):
        st = state if v is not None else STATE_DISCONNECTED
        out.append(WifiSample(end - i * step, st, v, None, None, None))
    return out


def warn(recent, now=1001.0, cfg=CFG):
    return low_signal_warning(recent, cfg, now=now, stale_after_s=STALE)


def test_no_warning_while_the_signal_is_fine():
    assert warn(samples(80, 75, 78))["low_signal"] is False


def test_the_warning_needs_the_configured_number_of_consecutive_low_readings():
    assert warn(samples(80, 30, 20))["low_signal"] is False  # only two in a row
    w = warn(samples(80, 30, 20, 25))
    assert w["low_signal"] is True and w["consecutive_low"] == 3 and w["since"] == 990.0


def test_the_threshold_is_inclusive_and_configurable():
    assert warn(samples(40, 40, 40))["low_signal"] is True
    assert warn(samples(41, 41, 41))["low_signal"] is False
    assert warn(samples(60, 60, 60), cfg=WifiConfig(low_signal_pct=60))["low_signal"] is True
    assert warn(samples(30, 30), cfg=WifiConfig(low_signal_pct=40, warn_after_polls=2))[
        "low_signal"] is True


def test_one_good_reading_ends_the_warning():
    assert warn(samples(20, 20, 20, 70))["low_signal"] is False


def test_a_disconnection_is_not_a_low_signal_warning():
    recent = samples(20, 20, None)  # newest reading: disconnected
    assert warn(recent)["low_signal"] is False


def test_stale_data_gives_no_warning():
    assert warn(samples(20, 20, 20), now=1000.0 + STALE + 1)["low_signal"] is False
    assert warn(samples(20, 20, 20), now=1000.0 + STALE)["low_signal"] is True


def test_no_data_means_no_warning():
    assert warn([])["low_signal"] is False


def test_a_zero_signal_counts_as_low_but_a_missing_one_does_not():
    assert warn(samples(0, 0, 0))["low_signal"] is True
    recent = [WifiSample(1000 - 5 * i, STATE_CONNECTED, None, None, None, None) for i in range(3)]
    assert warn(recent)["low_signal"] is False


# ------------------------------------------------------------------ history series
def test_series_buckets_connected_signal_and_never_averages_in_a_disconnection():
    obs = [
        WifiSample(100, STATE_CONNECTED, 80, -55, 100.0, 50.0),
        WifiSample(105, STATE_CONNECTED, 60, -65, 80.0, 40.0),
        WifiSample(110, STATE_DISCONNECTED, None, None, None, None),
    ]
    (p,), bucket_s = series(obs, start=100, end=130, first_ts=100, poll_interval_s=5,
                            max_points=10)
    assert bucket_s == 15  # three polling intervals at least
    assert (p["observations"], p["connected"], p["not_connected"]) == (3, 2, 1)
    assert p["signal_mean"] == 70.0 and (p["signal_min"], p["signal_max"]) == (60, 80)
    assert p["rssi_mean"] == -60.0 and p["rx_mean"] == 90.0 and p["tx_mean"] == 45.0


def test_a_bucket_with_only_disconnected_observations_has_no_signal_not_zero():
    obs = [WifiSample(t, STATE_DISCONNECTED, None, None, None, None) for t in (100, 105, 110)]
    (p,), _ = series(obs, start=100, end=130, first_ts=100, poll_interval_s=5, max_points=10)
    assert p["signal_mean"] is None and p["signal_min"] is None and p["connected"] == 0
    assert p["rssi_mean"] is None


def test_series_omits_empty_buckets_and_missing_rssi_stays_missing():
    obs = [WifiSample(100, STATE_CONNECTED, 70, None, None, None),
           WifiSample(400, STATE_CONNECTED, 71, None, None, None)]
    points, _ = series(obs, start=0, end=500, first_ts=100, poll_interval_s=5, max_points=50)
    assert len(points) == 2 and all(p["rssi_mean"] is None for p in points)


def test_series_of_nothing_is_empty():
    assert series([], start=0, end=100, first_ts=None, poll_interval_s=5, max_points=10)[0] == []
