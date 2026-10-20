"""The QoS telemetry adapters NetMonLT supports, and nothing else.

An adapter is a small, explicit description of one documented MIB: which columns to read (with
their OIDs) and how to turn the values into QosRows. There is NO guessing and NO discovery: a
device uses an adapter only if the inventory names it, and a device that does not implement the
MIB simply returns nothing, which is reported as "unsupported", never as made-up QoS data.

Every OID below was read from the compiled DIFFSERV-MIB module (RFC 3289), not typed from
memory, and the IF-MIB ones from the compiled IF-MIB. This module has no SNMP library
dependency, so the parsing is tested with plain dictionaries.

SUPPORTED ADAPTERS
  diffserv-mib   RFC 3289 (IETF standards track, 2002). Reads the count-action and
                 algorithmic-dropper counters. Counters are Counter64. Implemented by few
                 devices; NOT verified against any real device by the author.

NOT YET SUPPORTED (deliberately): vendor MIBs such as Cisco CISCO-CLASS-BASED-QOS-MIB, Juniper
JUNIPER-COS-MIB or MikroTik's queue tables. They are the usual sources of QoS counters on real
equipment, but their OIDs could not be verified from a trustworthy copy of the MIB, so no adapter
exists for them. Adding one means adding one QosAdapter here (columns + a pure `build` function)
once its definitions are verified; nothing else changes.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from netmonlt.qos import KIND_DROPS, KIND_TRAFFIC, QosRow, clean_label

Tables = Mapping[str, Mapping[tuple[int, ...], Any]]  # {column name: {index tuple: value}}

# ---- DIFFSERV-MIB (RFC 3289), 1.3.6.1.2.1.97.1
_COUNT_ENTRY = "1.3.6.1.2.1.97.1.5.5.1"  # diffServCountActEntry
_ACTION_ENTRY = "1.3.6.1.2.1.97.1.5.2.1"  # diffServActionEntry
_DROP_ENTRY = "1.3.6.1.2.1.97.1.6.2.1"  # diffServAlgDropEntry
_Q_ENTRY = "1.3.6.1.2.1.97.1.7.2.1"  # diffServQEntry
_IF_X_ENTRY = "1.3.6.1.2.1.31.1.1.1"  # IF-MIB ifXEntry

DIFFSERV_COLUMNS = {
    "countOctets": f"{_COUNT_ENTRY}.2",  # diffServCountActOctets, Counter64
    "countPkts": f"{_COUNT_ENTRY}.3",  # diffServCountActPkts, Counter64
    "actionInterface": f"{_ACTION_ENTRY}.2",  # diffServActionInterface, ifIndex or 0
    "actionSpecific": f"{_ACTION_ENTRY}.4",  # diffServActionSpecific, RowPointer
    "dropType": f"{_DROP_ENTRY}.2",  # diffServAlgDropType
    "dropQMeasure": f"{_DROP_ENTRY}.4",  # diffServAlgDropQMeasure, RowPointer to a queue
    "dropOctets": f"{_DROP_ENTRY}.7",  # diffServAlgDropOctets, Counter64 (deterministic drops)
    "dropPkts": f"{_DROP_ENTRY}.8",  # diffServAlgDropPkts, Counter64
    "randomDropOctets": f"{_DROP_ENTRY}.9",  # diffServAlgRandomDropOctets, Counter64
    "randomDropPkts": f"{_DROP_ENTRY}.10",  # diffServAlgRandomDropPkts, Counter64
    "ifName": f"{_IF_X_ENTRY}.1",  # ifName
    "ifDiscontinuity": f"{_IF_X_ENTRY}.19",  # ifCounterDiscontinuityTime, TimeStamp (ticks)
}
DROP_TYPES = {1: "other", 2: "tail drop", 3: "head drop", 4: "random drop", 5: "always drop"}
COUNTER_BITS = 64


@dataclass(frozen=True)
class QosAdapter:
    name: str  # the value used in the inventory
    title: str  # shown on the dashboard
    source_mib: str  # the MIB (and standard) the counters come from
    columns: Mapping[str, str]  # column name -> OID (all read-only GETBULK walks)
    build: Callable[[Tables], list[QosRow]]
    requirements: str  # what a device must support, for the documentation and error messages


def _counter(value: Any, bits: int = COUNTER_BITS) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value < (1 << bits) else None


def pointer_index(pointer: Any, entry_prefix: str) -> int | None:
    """The row index in a RowPointer to `entry_prefix`.<column>.<index>, or None.

    A RowPointer names one column instance of the row it points to (SNMPv2-TC), so a valid
    pointer into this table is exactly the entry OID, a column number and the index. Anything
    else (zeroDotZero, a pointer into another table, a malformed value) is not resolved.
    """
    if not isinstance(pointer, str):
        return None
    oid = pointer.strip().lstrip(".")
    prefix = entry_prefix + "."
    if not oid.startswith(prefix):
        return None
    rest = oid[len(prefix):].split(".")
    if len(rest) != 2 or not all(part.isdigit() for part in rest):
        return None
    column, index = int(rest[0]), int(rest[1])
    return index if column >= 1 and index >= 1 else None


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _single(table: Mapping[tuple[int, ...], Any], index: int) -> Any:
    return table.get((index,))


def build_diffserv(tables: Tables) -> list[QosRow]:
    """QosRows from DIFFSERV-MIB walks: one traffic row per count action and one drops row per
    algorithmic dropper. Elements are labelled by their table index because the MIB gives them no
    name; a count action is attached to an interface only if an action row points at it
    unambiguously."""
    interface_of: dict[int, int | None] = {}
    action_if = tables.get("actionInterface", {})
    for (action_id,), pointer in tables.get("actionSpecific", {}).items():
        count_id = pointer_index(pointer, _COUNT_ENTRY)
        if count_id is None:
            continue
        if_index = _positive_int(_single(action_if, action_id))
        if count_id in interface_of and interface_of[count_id] != if_index:
            interface_of[count_id] = None  # two actions disagree: do not guess
        else:
            interface_of[count_id] = if_index

    names, discontinuities = tables.get("ifName", {}), tables.get("ifDiscontinuity", {})
    packets, octets = tables.get("countPkts", {}), tables.get("countOctets", {})
    rows: list[QosRow] = []
    for count_id in sorted({k[0] for k in packets} | {k[0] for k in octets}):
        pkts, octs = _counter(_single(packets, count_id)), _counter(_single(octets, count_id))
        if pkts is None and octs is None:
            continue
        if_index = interface_of.get(count_id)
        detail = discontinuity = None
        if if_index is not None:
            detail = "interface " + clean_label(_single(names, if_index), f"if{if_index}")
            ticks = _single(discontinuities, if_index)
            discontinuity = _counter(ticks, 32)
        rows.append(QosRow(
            key=f"count:{count_id}", kind=KIND_TRAFFIC, name=f"Count action {count_id}",
            detail=detail, packets=pkts, octets=octs, counter_bits=COUNTER_BITS,
            discontinuity=discontinuity,
        ))

    det_pkts, det_octs = tables.get("dropPkts", {}), tables.get("dropOctets", {})
    rnd_pkts, rnd_octs = tables.get("randomDropPkts", {}), tables.get("randomDropOctets", {})
    ids = {k[0] for t in (det_pkts, det_octs, rnd_pkts, rnd_octs) for k in t}

    def total(*parts: Any) -> int | None:
        """Deterministic plus random drops, from whichever of the two the device reports."""
        valid = [p for p in (_counter(x) for x in parts) if p is not None]
        if not valid:
            return None
        value = sum(valid)
        return value if value < (1 << COUNTER_BITS) else None

    for drop_id in sorted(ids):
        dpkts = total(_single(det_pkts, drop_id), _single(rnd_pkts, drop_id))
        docts = total(_single(det_octs, drop_id), _single(rnd_octs, drop_id))
        if dpkts is None and docts is None:
            continue
        kind_code = _single(tables.get("dropType", {}), drop_id)
        parts = [DROP_TYPES.get(kind_code, "unknown type") if isinstance(kind_code, int)
                 else "unknown type"]
        queue = pointer_index(_single(tables.get("dropQMeasure", {}), drop_id), _Q_ENTRY)
        if queue is not None:
            parts.append(f"monitors queue {queue}")
        rows.append(QosRow(
            key=f"drop:{drop_id}", kind=KIND_DROPS, name=f"Dropper {drop_id}",
            detail=", ".join(parts), drop_packets=dpkts, drop_octets=docts,
            counter_bits=COUNTER_BITS,
        ))
    return rows


DIFFSERV_MIB = QosAdapter(
    name="diffserv-mib",
    title="DIFFSERV-MIB (RFC 3289)",
    source_mib="DIFFSERV-MIB (RFC 3289): diffServCountActTable and diffServAlgDropTable",
    columns=DIFFSERV_COLUMNS,
    build=build_diffserv,
    requirements=(
        "the device must implement DIFFSERV-MIB (1.3.6.1.2.1.97) with count actions or "
        "algorithmic droppers configured, and allow your SNMPv3 user to read it"
    ),
)

ADAPTERS: dict[str, QosAdapter] = {DIFFSERV_MIB.name: DIFFSERV_MIB}
