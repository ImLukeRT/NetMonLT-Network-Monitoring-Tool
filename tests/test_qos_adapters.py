"""The QoS adapter registry and the DIFFSERV-MIB adapter: OIDs, parsing, and honesty rules.

The OIDs asserted here were read from the compiled DIFFSERV-MIB (RFC 3289) and IF-MIB modules,
not typed from memory; this test pins them so an accidental edit is caught.
"""

import pytest

from netmonlt.qos import KIND_DROPS, KIND_TRAFFIC
from netmonlt.qos_adapters import (
    ADAPTERS,
    DIFFSERV_COLUMNS,
    DIFFSERV_MIB,
    build_diffserv,
    pointer_index,
)

COUNT = "1.3.6.1.2.1.97.1.5.5.1"
ACTION = "1.3.6.1.2.1.97.1.5.2.1"
DROP = "1.3.6.1.2.1.97.1.6.2.1"
QUEUE = "1.3.6.1.2.1.97.1.7.2.1"


# ------------------------------------------------------------------ the registry
def test_only_documented_adapters_exist():
    assert set(ADAPTERS) == {"diffserv-mib"}  # vendor adapters are deliberately not guessed
    assert ADAPTERS["diffserv-mib"] is DIFFSERV_MIB
    assert "RFC 3289" in DIFFSERV_MIB.title and "DIFFSERV-MIB" in DIFFSERV_MIB.source_mib
    assert "must implement DIFFSERV-MIB" in DIFFSERV_MIB.requirements


def test_every_oid_matches_the_verified_mib_definition():
    assert DIFFSERV_COLUMNS == {
        "countOctets": f"{COUNT}.2",  # diffServCountActOctets
        "countPkts": f"{COUNT}.3",  # diffServCountActPkts
        "actionInterface": f"{ACTION}.2",  # diffServActionInterface
        "actionSpecific": f"{ACTION}.4",  # diffServActionSpecific
        "dropType": f"{DROP}.2",  # diffServAlgDropType
        "dropQMeasure": f"{DROP}.4",  # diffServAlgDropQMeasure
        "dropOctets": f"{DROP}.7",  # diffServAlgDropOctets
        "dropPkts": f"{DROP}.8",  # diffServAlgDropPkts
        "randomDropOctets": f"{DROP}.9",  # diffServAlgRandomDropOctets
        "randomDropPkts": f"{DROP}.10",  # diffServAlgRandomDropPkts
        "ifName": "1.3.6.1.2.1.31.1.1.1.1",  # IF-MIB ifName
        "ifDiscontinuity": "1.3.6.1.2.1.31.1.1.1.19",  # IF-MIB ifCounterDiscontinuityTime
    }


def test_the_adapter_only_reads_diffserv_and_if_mib_columns():
    allowed = ("1.3.6.1.2.1.97.1.", "1.3.6.1.2.1.31.1.1.1.")
    assert all(oid.startswith(allowed) for oid in DIFFSERV_COLUMNS.values())
    # no write-able configuration column (RowStatus, storage type, row pointers to follow) is read
    assert not any(oid.endswith((".5", ".6")) and oid.startswith(COUNT) for oid in
                   DIFFSERV_COLUMNS.values())


# ------------------------------------------------------------------ row pointers
@pytest.mark.parametrize("pointer, expected", [
    (f"{COUNT}.2.7", 7), (f".{COUNT}.3.12", 12), (f" {COUNT}.2.7 ", 7),
    (f"{COUNT}.7", None),  # no column: not a valid RowPointer
    (f"{COUNT}.2.7.9", None), (f"{COUNT}.0.7", None), (f"{COUNT}.2.0", None),
    ("0.0", None), ("", None), (None, None), (5, None), (b"x", None),
    (f"{ACTION}.2.7", None),  # a pointer into a different table
    (f"{COUNT}.2.x", None),
])
def test_a_row_pointer_is_resolved_only_when_it_is_exactly_one_row_of_that_table(pointer,
                                                                                    expected):
    assert pointer_index(pointer, COUNT) == expected


# ------------------------------------------------------------------ parsing
def tables(**columns):
    return {name: {(i,): v for i, v in values.items()} for name, values in columns.items()}


def test_count_actions_become_traffic_rows_attached_to_their_interface():
    t = tables(
        countPkts={1: 1000, 2: 50}, countOctets={1: 1_000_000, 2: 5000},
        actionInterface={10: 3, 11: 4}, actionSpecific={10: f"{COUNT}.3.1", 11: f"{COUNT}.2.2"},
        ifName={3: b"Gi0/3", 4: b"Gi0/4"}, ifDiscontinuity={3: 700, 4: 800},
    )
    rows = build_diffserv({k: v for k, v in t.items()})
    assert [r.key for r in rows] == ["count:1", "count:2"]
    one, two = rows
    assert (one.kind, one.name, one.detail) == (KIND_TRAFFIC, "Count action 1",
                                                "interface Gi0/3")
    assert (one.packets, one.octets, one.counter_bits, one.discontinuity) == (1000, 1_000_000,
                                                                              64, 700)
    assert two.detail == "interface Gi0/4" and two.discontinuity == 800
    assert one.drop_packets is None and one.drop_octets is None  # it counts no drops


def test_a_count_action_that_no_action_points_at_has_no_interface_and_no_discontinuity():
    rows = build_diffserv(tables(countPkts={5: 10}, countOctets={5: 20},
                                 ifName={1: b"x"}, ifDiscontinuity={1: 5}))
    (row,) = rows
    assert row.detail is None and row.discontinuity is None


def test_two_actions_that_disagree_about_the_interface_leave_it_unresolved():
    rows = build_diffserv(tables(
        countPkts={1: 10}, countOctets={1: 20}, actionInterface={10: 3, 11: 4},
        actionSpecific={10: f"{COUNT}.3.1", 11: f"{COUNT}.3.1"},
        ifName={3: b"Gi0/3", 4: b"Gi0/4"}, ifDiscontinuity={3: 1, 4: 2},
    ))
    assert rows[0].detail is None and rows[0].discontinuity is None  # not guessed


def test_an_interface_index_of_zero_means_indeterminate():
    rows = build_diffserv(tables(
        countPkts={1: 10}, countOctets={1: 20}, actionInterface={10: 0},
        actionSpecific={10: f"{COUNT}.3.1"}, ifName={0: b"x"},
    ))
    assert rows[0].detail is None


def test_an_unnamed_interface_falls_back_to_its_index():
    rows = build_diffserv(tables(countPkts={1: 10}, countOctets={1: 20}, actionInterface={10: 7},
                                 actionSpecific={10: f"{COUNT}.3.1"}))
    assert rows[0].detail == "interface if7"


def test_droppers_become_drops_rows_with_their_type_and_queue():
    rows = build_diffserv(tables(
        dropType={1: 2, 2: 4, 3: 9}, dropQMeasure={1: f"{QUEUE}.1.5", 2: "0.0"},
        dropPkts={1: 100, 2: 7, 3: 1}, dropOctets={1: 150_000, 2: 700, 3: 1},
    ))
    assert [r.key for r in rows] == ["drop:1", "drop:2", "drop:3"]
    a, b, c = rows
    assert (a.kind, a.name, a.detail) == (KIND_DROPS, "Dropper 1", "tail drop, monitors queue 5")
    assert (a.drop_packets, a.drop_octets, a.packets, a.octets) == (100, 150_000, None, None)
    assert b.detail == "random drop" and c.detail == "unknown type"


def test_random_and_deterministic_drops_are_added_together():
    rows = build_diffserv(tables(dropPkts={1: 100}, dropOctets={1: 1000},
                                 randomDropPkts={1: 25}, randomDropOctets={1: 250}))
    assert (rows[0].drop_packets, rows[0].drop_octets) == (125, 1250)


def test_a_dropper_reporting_only_random_drops_is_still_listed():
    rows = build_diffserv(tables(randomDropPkts={4: 25}))
    assert (rows[0].drop_packets, rows[0].drop_octets) == (25, None)


@pytest.mark.parametrize("bad", [-1, 2**64, True, "7", None, b"7", 1.5])
def test_invalid_counter_values_are_dropped_not_repaired(bad):
    rows = build_diffserv(tables(countPkts={1: bad}, countOctets={1: bad}))
    assert rows == []  # a row with no valid counter at all is not listed
    rows = build_diffserv(tables(countPkts={1: bad}, countOctets={1: 20}))
    assert (rows[0].packets, rows[0].octets) == (None, 20)


def test_a_sum_that_overflows_64_bits_is_unknown():
    rows = build_diffserv(tables(dropPkts={1: 2**64 - 1}, randomDropPkts={1: 5},
                                 dropOctets={1: 10}))
    assert (rows[0].drop_packets, rows[0].drop_octets) == (None, 10)


def test_interface_names_are_cleaned():
    rows = build_diffserv(tables(countPkts={1: 1}, countOctets={1: 1}, actionInterface={10: 3},
                                 actionSpecific={10: f"{COUNT}.3.1"},
                                 ifName={3: b"Gi\x001\x1b[2J" + b"x" * 200}))
    assert "\x00" not in rows[0].detail and "\x1b" not in rows[0].detail
    assert len(rows[0].detail) <= len("interface ") + 64


# ------------------------------------------------------------------ unsupported devices
@pytest.mark.parametrize("empty", [{}, {"countPkts": {}, "dropPkts": {}},
                                   {"ifName": {(1,): b"eth0"}, "ifDiscontinuity": {(1,): 5}}])
def test_a_device_without_diffserv_data_yields_no_rows_at_all(empty):
    assert build_diffserv(empty) == []  # nothing is fabricated from interface names alone


def test_configuration_tables_alone_produce_no_telemetry():
    # a device that has the classifier/action configuration but no counters reports nothing
    assert build_diffserv(tables(actionInterface={1: 3}, actionSpecific={1: f"{COUNT}.3.1"},
                                 dropType={1: 2}, dropQMeasure={1: f"{QUEUE}.1.5"})) == []


def test_parsing_does_not_mutate_its_input():
    t = tables(countPkts={1: 5}, countOctets={1: 6})
    before = {k: dict(v) for k, v in t.items()}
    build_diffserv(t)
    assert t == before
