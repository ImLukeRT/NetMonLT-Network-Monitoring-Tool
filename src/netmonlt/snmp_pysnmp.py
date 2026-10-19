"""Real SNMPv3 interface polling with pysnmp. Needs the optional `snmp` extra.

    pip install 'netmonlt[snmp]'      (or: uv sync --extra snmp)

Why pysnmp: it is the maintained, pure-Python SNMP library (7.x, asyncio API) that implements the
SNMPv3 user-based security model, including SHA-2 authentication and AES privacy. `cryptography`
is installed alongside it because pysnmp needs it for AES: without it every authPriv request
fails with "Ciphering services not available".

What this client does, and does not do:
  * it only ever polls a device that is explicitly configured, authorised, and has its
    credentials loaded at startup; it never discovers or scans anything;
  * per poll it reads sysUpTime and walks just nine named IF-MIB / IF-X-MIB columns of that one
    device (not the whole MIB tree);
  * credentials are read from environment variables, kept only in memory, and never logged,
    stored or returned; pysnmp error text is mapped to a fixed category and discarded.
"""

import contextlib
import logging
from collections.abc import Mapping, Sequence
from typing import Any

import cryptography  # noqa: F401  (pysnmp needs it for AES privacy; fail at import if missing)
from pysnmp.hlapi.v3arch.asyncio import (
    USM_AUTH_HMAC96_SHA,
    USM_AUTH_HMAC128_SHA224,
    USM_AUTH_HMAC192_SHA256,
    USM_AUTH_HMAC256_SHA384,
    USM_AUTH_HMAC384_SHA512,
    USM_PRIV_CFB128_AES,
    USM_PRIV_CFB192_AES,
    USM_PRIV_CFB256_AES,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    UsmUserData,
    bulk_walk_cmd,
    get_cmd,
)

from netmonlt.domain import Device, SnmpConfig
from netmonlt.ifstats import MAX_INTERFACES, InterfaceSnapshot, build_snapshot
from netmonlt.probes.icmp import TargetError, validate_target
from netmonlt.snmp import (
    CATEGORY_AUTH,
    CATEGORY_CONFIG,
    CATEGORY_ERROR,
    CATEGORY_TIMEOUT,
    CATEGORY_UNSUPPORTED,
    SnmpConfigError,
    SnmpCredentials,
    SnmpError,
    load_credentials,
)

log = logging.getLogger(__name__)

AUTH_PROTOCOLS = {
    "SHA": USM_AUTH_HMAC96_SHA,  # SHA-1: weak-ish, but the only SHA many older devices have
    "SHA224": USM_AUTH_HMAC128_SHA224,
    "SHA256": USM_AUTH_HMAC192_SHA256,
    "SHA384": USM_AUTH_HMAC256_SHA384,
    "SHA512": USM_AUTH_HMAC384_SHA512,
}
PRIV_PROTOCOLS = {
    "AES128": USM_PRIV_CFB128_AES,
    "AES192": USM_PRIV_CFB192_AES,
    "AES256": USM_PRIV_CFB256_AES,
}

OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
# The only things ever requested from a device: IF-MIB (ifTable) and IF-X-MIB (ifXTable) columns.
COLUMNS = {
    "ifDescr": "1.3.6.1.2.1.2.2.1.2",
    "ifOperStatus": "1.3.6.1.2.1.2.2.1.8",
    "ifInDiscards": "1.3.6.1.2.1.2.2.1.13",
    "ifInErrors": "1.3.6.1.2.1.2.2.1.14",
    "ifOutDiscards": "1.3.6.1.2.1.2.2.1.19",
    "ifOutErrors": "1.3.6.1.2.1.2.2.1.20",
    "ifName": "1.3.6.1.2.1.31.1.1.1.1",
    "ifHCInOctets": "1.3.6.1.2.1.31.1.1.1.6",
    "ifHCOutOctets": "1.3.6.1.2.1.31.1.1.1.10",
}
MAX_ROWS_PER_COLUMN = 4 * MAX_INTERFACES  # a runaway device can't make us read forever
BULK_REPETITIONS = 20
_MISSING_VALUE_TYPES = {"NoSuchObject", "NoSuchInstance", "EndOfMibView"}

# Error text (from pysnmp) -> our category. Matched case-insensitively as substrings.
_AUTH_HINTS = (
    "wrong snmp pdu digest", "unknown usm user", "unsupported security level",
    "unknown snmp engine", "not in time window", "decryption error", "wrong encryption",
    "authorizationerror", "noaccess", "authentication",
)


def classify_error(text: str) -> str:
    """Map a pysnmp error indication / status to one of our categories (text is then discarded)."""
    lowered = text.lower()
    if "ciphering services not available" in lowered:
        return CATEGORY_CONFIG
    if "timeout" in lowered or "no snmp response" in lowered:
        return CATEGORY_TIMEOUT
    if any(hint in lowered for hint in _AUTH_HINTS):
        return CATEGORY_AUTH
    return CATEGORY_ERROR


def usm_user(credentials: SnmpCredentials, config: SnmpConfig) -> UsmUserData:
    """The pysnmp SNMPv3 user for a device (authPriv or authNoPriv)."""
    if config.security_level == "authPriv":
        return UsmUserData(
            credentials.user,
            authKey=credentials.auth_key,
            privKey=credentials.priv_key,
            authProtocol=AUTH_PROTOCOLS[config.auth_protocol],
            privProtocol=PRIV_PROTOCOLS[config.priv_protocol],
        )
    return UsmUserData(
        credentials.user,
        authKey=credentials.auth_key,
        authProtocol=AUTH_PROTOCOLS[config.auth_protocol],
    )


def convert_value(value: Any) -> int | bytes | None:
    """A pysnmp value as a plain int (numbers, counters, timeticks) or bytes (strings).

    Returns None for "no such object/instance/end of MIB" markers and anything unrecognised.
    """
    if type(value).__name__ in _MISSING_VALUE_TYPES:
        return None
    if hasattr(value, "asOctets"):
        return bytes(value.asOctets())
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def index_of(oid: str, column: str) -> int | None:
    """The ifIndex at the end of `oid` if it is a row of `column` (e.g. column.4 -> 4)."""
    prefix = column + "."
    if not oid.startswith(prefix):
        return None
    suffix = oid[len(prefix):]
    return int(suffix) if suffix.isdigit() else None


# ---- the only functions that talk to pysnmp (replaced by fakes in the unit tests)
async def _get_uptime(engine, auth, target) -> int | None:
    error, status, _, binds = await get_cmd(
        engine, auth, target, ContextData(), ObjectType(ObjectIdentity(OID_SYS_UPTIME))
    )
    if error:
        raise SnmpError(classify_error(str(error)))
    if status:
        raise SnmpError(classify_error(status.prettyPrint()))
    for _, value in binds:
        converted = convert_value(value)
        if isinstance(converted, int):
            return converted
    return None


async def _walk_column(engine, auth, target, column: str) -> dict[int, int | bytes]:
    rows: dict[int, int | bytes] = {}
    walk = bulk_walk_cmd(
        engine, auth, target, ContextData(), 0, BULK_REPETITIONS,
        ObjectType(ObjectIdentity(column)), lexicographicMode=False,
    )
    async with contextlib.aclosing(walk):
        async for error, status, _, binds in walk:
            if error:
                raise SnmpError(classify_error(str(error)))
            if status:
                raise SnmpError(classify_error(status.prettyPrint()))
            for oid, value in binds:
                index = index_of(str(oid), column)
                converted = convert_value(value)
                if index is not None and converted is not None:
                    rows[index] = converted
            if len(rows) >= MAX_ROWS_PER_COLUMN:
                break
    return rows


class PySnmpClient:
    """Polls interface statistics from real devices over SNMPv3 (see the module docstring)."""

    def __init__(self, credentials: Mapping[str, SnmpCredentials]):
        self._credentials = dict(credentials)

    def __repr__(self) -> str:
        return f"PySnmpClient(devices={sorted(self._credentials)}, credentials=<hidden>)"

    @classmethod
    def from_devices(
        cls, devices: Sequence[Device], environ: Mapping[str, str] | None = None
    ) -> "PySnmpClient":
        """Load every device's credentials from the environment, or say which variables are
        missing (by name only). Done at startup so a typo fails fast, not at the first poll."""
        credentials: dict[str, SnmpCredentials] = {}
        problems = []
        for device in devices:
            if device.snmp is None:
                continue
            try:
                credentials[device.name] = load_credentials(device.snmp, environ)
            except SnmpConfigError as exc:
                problems.append(f"{device.name}: {exc}")
        if problems:
            raise SnmpConfigError("; ".join(problems))
        return cls(credentials)

    async def poll(self, device: Device) -> InterfaceSnapshot:
        config = device.snmp
        # Defence in depth: only a configured, authorised, single-host device is ever contacted.
        if config is None or not device.authorized or device.name not in self._credentials:
            raise SnmpError(CATEGORY_CONFIG)
        try:
            validate_target(device.address)
        except TargetError:
            raise SnmpError(CATEGORY_CONFIG) from None

        engine = SnmpEngine()
        try:
            auth = usm_user(self._credentials[device.name], config)
            target = await UdpTransportTarget.create(
                (device.address, config.port), timeout=config.timeout_s, retries=config.retries
            )
            uptime = await _get_uptime(engine, auth, target)
            tables = {}
            for name, column in COLUMNS.items():
                tables[name] = await _walk_column(engine, auth, target, column)
            if not tables["ifOperStatus"]:
                raise SnmpError(CATEGORY_UNSUPPORTED)
            return build_snapshot(tables, uptime)
        except SnmpError:
            raise
        except Exception as exc:  # pysnmp error text may echo parameters: keep only the type
            log.debug("pysnmp raised %s while polling %s", type(exc).__name__, device.name)
            raise SnmpError(CATEGORY_ERROR) from None
        finally:
            engine.close_dispatcher()
