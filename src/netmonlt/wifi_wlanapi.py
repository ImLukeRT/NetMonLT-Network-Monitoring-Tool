"""Reads this computer's Wi-Fi state from the Windows WLAN API (wlanapi.dll), with ctypes.

This is the preferred Windows backend: structured values and no dependence on the display
language, unlike `netsh` text. It runs as a small CHILD PROCESS (`python -m
netmonlt.wifi_wlanapi`) that prints one JSON line, so that a hang or a crash in native code
can only ever end that child, never the monitoring app; the parent kills it on a timeout and
validates everything it prints (see wifi_windows.py).

It only calls read-only query functions on interfaces that already exist: WlanOpenHandle,
WlanEnumInterfaces, WlanQueryInterface and WlanFreeMemory. It never scans (no WlanScan, no
WlanGetNetworkBssList), never connects, and never reads the access point's MAC address.

The structure layouts below use fixed-width integers so they are identical on every platform,
which lets the decoding be tested on Linux. Only `CtypesWlanApi` touches the real DLL, and that
part has NOT been run against real hardware by the author.
"""

import ctypes
import json
import sys
from typing import Protocol

from netmonlt.wifi import (
    CATEGORY_ERROR,
    CATEGORY_UNAVAILABLE,
    CATEGORY_UNSUPPORTED_OS,
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_DISCONNECTED,
    STATE_DISCONNECTING,
    STATE_NOT_READY,
    STATE_UNKNOWN,
    WifiError,
)

# WLAN_INTERFACE_STATE
INTERFACE_STATES = {
    0: STATE_NOT_READY,  # wlan_interface_state_not_ready
    1: STATE_CONNECTED,
    2: STATE_UNKNOWN,  # ad hoc network formed: not an infrastructure link
    3: STATE_DISCONNECTING,
    4: STATE_DISCONNECTED,
    5: STATE_CONNECTING,  # associating
    6: STATE_CONNECTING,  # discovering
    7: STATE_CONNECTING,  # authenticating
}
# DOT11_PHY_TYPE
PHY_TYPES = {
    4: "802.11a", 5: "802.11b", 6: "802.11g", 7: "802.11n", 8: "802.11ac", 9: "802.11ad",
    10: "802.11ax", 11: "802.11be",
}
# WLAN_INTF_OPCODE values for WlanQueryInterface
OPCODE_CURRENT_CONNECTION = 7
OPCODE_CHANNEL_NUMBER = 8
OPCODE_RSSI = 0x10000102
RSSI_SOURCE = "Windows WLAN API (driver-reported)"
ERROR_SERVICE_NOT_ACTIVE = 1062
CLIENT_VERSION = 2


class DOT11_SSID(ctypes.Structure):  # noqa: N801 - names follow the Windows headers
    _fields_ = [("uSSIDLength", ctypes.c_uint32), ("ucSSID", ctypes.c_ubyte * 32)]


class WLAN_ASSOCIATION_ATTRIBUTES(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("dot11Ssid", DOT11_SSID),
        ("dot11BssType", ctypes.c_int32),
        ("dot11Bssid", ctypes.c_ubyte * 6),  # the access point's MAC: never read or reported
        ("dot11PhyType", ctypes.c_int32),
        ("uDot11PhyIndex", ctypes.c_uint32),
        ("wlanSignalQuality", ctypes.c_uint32),
        ("ulRxRate", ctypes.c_uint32),  # kbit/s
        ("ulTxRate", ctypes.c_uint32),  # kbit/s
    ]


class WLAN_SECURITY_ATTRIBUTES(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("bSecurityEnabled", ctypes.c_int32), ("bOneXEnabled", ctypes.c_int32),
        ("dot11AuthAlgorithm", ctypes.c_int32), ("dot11CipherAlgorithm", ctypes.c_int32),
    ]


class WLAN_CONNECTION_ATTRIBUTES(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("isState", ctypes.c_int32),
        ("wlanConnectionMode", ctypes.c_int32),
        ("strProfileName", ctypes.c_uint16 * 256),  # never read: it usually equals the SSID
        ("wlanAssociationAttributes", WLAN_ASSOCIATION_ATTRIBUTES),
        ("wlanSecurityAttributes", WLAN_SECURITY_ATTRIBUTES),
    ]


class WLAN_INTERFACE_INFO(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("InterfaceGuid", ctypes.c_ubyte * 16),
        ("strInterfaceDescription", ctypes.c_uint16 * 256),
        ("isState", ctypes.c_int32),
    ]


class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("dwNumberOfItems", ctypes.c_uint32), ("dwIndex", ctypes.c_uint32),
        ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),  # really dwNumberOfItems entries
    ]


class WlanApi(Protocol):
    """What `collect` needs. CtypesWlanApi implements it; tests use a fake."""

    def open(self): ...
    def close(self, handle) -> None: ...
    def interfaces(self, handle) -> list[tuple[bytes, str, int]]: ...  # guid, description, state
    def query(self, handle, guid: bytes, opcode: int) -> bytes | None: ...


def _utf16_text(units) -> str:
    raw = bytes(b for unit in units for b in (unit & 0xFF, unit >> 8))
    return raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]


def decode_interface_list(buf: bytes) -> list[tuple[bytes, str, int]]:
    """(guid, description, state code) for each interface in a WLAN_INTERFACE_INFO_LIST buffer."""
    header = 8
    if len(buf) < header:
        raise WifiError(CATEGORY_ERROR)
    count = int.from_bytes(buf[0:4], "little")
    size = ctypes.sizeof(WLAN_INTERFACE_INFO)
    if count > 64 or len(buf) < header + count * size:
        raise WifiError(CATEGORY_ERROR)
    result = []
    for i in range(count):
        info = WLAN_INTERFACE_INFO.from_buffer_copy(buf, header + i * size)
        result.append((
            bytes(info.InterfaceGuid), _utf16_text(info.strInterfaceDescription), info.isState,
        ))
    return result


def decode_connection(buf: bytes) -> dict:
    """The link figures in a WLAN_CONNECTION_ATTRIBUTES buffer (SSID, quality, rates, radio)."""
    if len(buf) < ctypes.sizeof(WLAN_CONNECTION_ATTRIBUTES):
        raise WifiError(CATEGORY_ERROR)
    attrs = WLAN_CONNECTION_ATTRIBUTES.from_buffer_copy(buf).wlanAssociationAttributes
    length = min(attrs.dot11Ssid.uSSIDLength, 32)
    ssid = bytes(attrs.dot11Ssid.ucSSID[:length]).decode("utf-8", errors="replace")
    return {
        "ssid": ssid or None,
        "signal_pct": attrs.wlanSignalQuality,
        "rx_mbps": attrs.ulRxRate / 1000 if attrs.ulRxRate else None,
        "tx_mbps": attrs.ulTxRate / 1000 if attrs.ulTxRate else None,
        "radio_type": PHY_TYPES.get(attrs.dot11PhyType),
    }


def collect(api: WlanApi) -> list[dict]:
    """One JSON-ready dict per wireless interface. Raises WifiError(unavailable) if the WLAN
    service cannot be opened."""
    handle = api.open()
    try:
        readings = []
        for guid, description, state_code in api.interfaces(handle):
            state = INTERFACE_STATES.get(state_code, STATE_UNKNOWN)
            reading = {"state": state, "interface": description}
            if state == STATE_CONNECTED:
                connection = api.query(handle, guid, OPCODE_CURRENT_CONNECTION)
                if connection is not None:
                    reading.update(decode_connection(connection))
                rssi = api.query(handle, guid, OPCODE_RSSI)
                if rssi is not None and len(rssi) >= 4:
                    reading["rssi_dbm"] = int.from_bytes(rssi[:4], "little", signed=True)
                    reading["rssi_source"] = RSSI_SOURCE
                channel = api.query(handle, guid, OPCODE_CHANNEL_NUMBER)
                if channel is not None and len(channel) >= 4:
                    reading["channel"] = int.from_bytes(channel[:4], "little")
            readings.append(reading)
        return readings
    finally:
        api.close(handle)


class CtypesWlanApi:
    """The real wlanapi.dll. Windows only; the one part here that is not testable elsewhere."""

    def __init__(self):
        if sys.platform != "win32":
            raise WifiError(CATEGORY_UNSUPPORTED_OS)
        try:
            self._dll = ctypes.WinDLL("wlanapi.dll")  # type: ignore[attr-defined]
        except OSError:
            raise WifiError(CATEGORY_UNAVAILABLE) from None
        c = ctypes
        self._dll.WlanOpenHandle.argtypes = [
            c.c_uint32, c.c_void_p, c.POINTER(c.c_uint32), c.POINTER(c.c_void_p)]
        self._dll.WlanOpenHandle.restype = c.c_uint32
        self._dll.WlanCloseHandle.argtypes = [c.c_void_p, c.c_void_p]
        self._dll.WlanCloseHandle.restype = c.c_uint32
        self._dll.WlanEnumInterfaces.argtypes = [
            c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)]
        self._dll.WlanEnumInterfaces.restype = c.c_uint32
        self._dll.WlanQueryInterface.argtypes = [
            c.c_void_p, c.POINTER(c.c_ubyte * 16), c.c_uint32, c.c_void_p,
            c.POINTER(c.c_uint32), c.POINTER(c.c_void_p), c.c_void_p]
        self._dll.WlanQueryInterface.restype = c.c_uint32
        self._dll.WlanFreeMemory.argtypes = [c.c_void_p]
        self._dll.WlanFreeMemory.restype = None

    def open(self):
        negotiated = ctypes.c_uint32()
        handle = ctypes.c_void_p()
        code = self._dll.WlanOpenHandle(
            CLIENT_VERSION, None, ctypes.byref(negotiated), ctypes.byref(handle))
        if code == ERROR_SERVICE_NOT_ACTIVE:
            raise WifiError(CATEGORY_UNAVAILABLE)
        if code != 0:
            raise WifiError(CATEGORY_ERROR)
        return handle

    def close(self, handle) -> None:
        self._dll.WlanCloseHandle(handle, None)

    def interfaces(self, handle) -> list[tuple[bytes, str, int]]:
        pointer = ctypes.c_void_p()
        if self._dll.WlanEnumInterfaces(handle, None, ctypes.byref(pointer)) != 0:
            raise WifiError(CATEGORY_ERROR)
        try:
            count = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0]
            size = 8 + min(count, 64) * ctypes.sizeof(WLAN_INTERFACE_INFO)
            return decode_interface_list(ctypes.string_at(pointer, size))
        finally:
            self._dll.WlanFreeMemory(pointer)

    def query(self, handle, guid: bytes, opcode: int) -> bytes | None:
        size = ctypes.c_uint32()
        data = ctypes.c_void_p()
        key = (ctypes.c_ubyte * 16).from_buffer_copy(guid)
        code = self._dll.WlanQueryInterface(
            handle, ctypes.byref(key), opcode, None, ctypes.byref(size), ctypes.byref(data), None)
        if code != 0 or not data.value:
            return None  # not supported by this adapter or driver: unavailable, not an error
        try:
            return ctypes.string_at(data, size.value)
        finally:
            self._dll.WlanFreeMemory(data)


def main() -> int:
    """Print one JSON line: {"ok": true, "interfaces": [...]} or {"ok": false, "category": ...}."""
    try:
        result = {"ok": True, "interfaces": collect(CtypesWlanApi())}
    except WifiError as exc:
        result = {"ok": False, "category": exc.category}
    except Exception:  # noqa: BLE001 - never let a traceback (and any path in it) escape
        result = {"ok": False, "category": CATEGORY_ERROR}
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
