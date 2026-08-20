"""UDS (ISO 14229) decoding, with FieldDeck permission classification.

Two jobs here.

**Decode.** Turn a reassembled ISO-TP payload into a readable request or
response, including negative responses and their NRC, so a capture explains
itself instead of being a wall of hex.

**Classify.** Every UDS service carries the FieldDeck permission its *use*
would require.  That matters because "UDS diagnostics" spans reading a VIN
and erasing an ECU's flash with the same transport and a one-byte difference.
Service 0x22 is a read; service 0x34 starts a firmware download; service 0x11
resets a running controller that may be steering something.  Lumping them
together as "diagnostics" is how a diagnostic session bricks a vehicle.

Decoding a *captured* message is always PASSIVE — the bytes already happened.
The permission on a service describes what transmitting it would cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fielddeck.common.models import PermissionLevel

__all__ = ["UdsService", "decode_message", "service_catalogue"]

#: Positive responses are the request SID with bit 6 set.
_POSITIVE_RESPONSE_OFFSET = 0x40
_NEGATIVE_RESPONSE = 0x7F


@dataclass(frozen=True, slots=True)
class UdsService:
    sid: int
    name: str
    #: What FieldDeck would require to *transmit* this service.
    permission: PermissionLevel
    description: str

    @property
    def response_sid(self) -> int:
        return self.sid + _POSITIVE_RESPONSE_OFFSET


#: The services worth naming.  Permission levels are the point of this table.
SERVICES: dict[int, UdsService] = {
    service.sid: service
    for service in (
        UdsService(
            0x10,
            "DiagnosticSessionControl",
            PermissionLevel.CONTROL,
            "changes the ECU's session; can unlock destructive services",
        ),
        UdsService(0x11, "ECUReset", PermissionLevel.CONTROL, "resets a running controller"),
        UdsService(
            0x14,
            "ClearDiagnosticInformation",
            PermissionLevel.CONTROL,
            "erases stored fault codes and freeze frames — evidence is lost",
        ),
        UdsService(0x19, "ReadDTCInformation", PermissionLevel.QUERY, "reads stored fault codes"),
        UdsService(
            0x22,
            "ReadDataByIdentifier",
            PermissionLevel.QUERY,
            "reads a data identifier such as VIN or software version",
        ),
        UdsService(0x23, "ReadMemoryByAddress", PermissionLevel.QUERY, "reads raw ECU memory"),
        UdsService(
            0x24,
            "ReadScalingDataByIdentifier",
            PermissionLevel.QUERY,
            "reads scaling information for a data identifier",
        ),
        UdsService(
            0x27,
            "SecurityAccess",
            PermissionLevel.CONTROL,
            "seed/key exchange that unlocks protected services",
        ),
        UdsService(
            0x28,
            "CommunicationControl",
            PermissionLevel.CONTROL,
            "enables or disables normal bus messages — can silence a live network",
        ),
        UdsService(
            0x29, "Authentication", PermissionLevel.CONTROL, "certificate-based authentication"
        ),
        UdsService(
            0x2A,
            "ReadDataByPeriodicIdentifier",
            PermissionLevel.CONTROL,
            "asks the ECU to transmit periodically, adding bus load",
        ),
        UdsService(
            0x2C,
            "DynamicallyDefineDataIdentifier",
            PermissionLevel.CONTROL,
            "defines a new data identifier in the ECU",
        ),
        UdsService(
            0x2E,
            "WriteDataByIdentifier",
            PermissionLevel.CONTROL,
            "writes a data identifier; may change calibration",
        ),
        UdsService(
            0x2F,
            "InputOutputControlByIdentifier",
            PermissionLevel.CONTROL,
            "takes direct control of an ECU input or output — can move actuators",
        ),
        UdsService(
            0x31,
            "RoutineControl",
            PermissionLevel.CONTROL,
            "starts an ECU routine; some routines erase memory",
        ),
        UdsService(
            0x34, "RequestDownload", PermissionLevel.FLASH, "begins a firmware download to the ECU"
        ),
        UdsService(
            0x35, "RequestUpload", PermissionLevel.QUERY, "begins reading firmware out of the ECU"
        ),
        UdsService(0x36, "TransferData", PermissionLevel.FLASH, "transfers a firmware block"),
        UdsService(0x37, "RequestTransferExit", PermissionLevel.FLASH, "ends a firmware transfer"),
        UdsService(
            0x38,
            "RequestFileTransfer",
            PermissionLevel.FLASH,
            "file-based transfer to or from the ECU",
        ),
        UdsService(
            0x3D, "WriteMemoryByAddress", PermissionLevel.FLASH, "writes directly to ECU memory"
        ),
        UdsService(
            0x3E,
            "TesterPresent",
            PermissionLevel.CONTROL,
            "keeps a non-default session alive; transmitted onto the bus",
        ),
        UdsService(
            0x83, "AccessTimingParameter", PermissionLevel.CONTROL, "changes protocol timing"
        ),
        UdsService(
            0x84, "SecuredDataTransmission", PermissionLevel.CONTROL, "encrypted service wrapper"
        ),
        UdsService(
            0x85,
            "ControlDTCSetting",
            PermissionLevel.CONTROL,
            "stops the ECU recording faults — suppresses evidence",
        ),
        UdsService(
            0x86, "ResponseOnEvent", PermissionLevel.CONTROL, "configures event-triggered responses"
        ),
        UdsService(
            0x87,
            "LinkControl",
            PermissionLevel.CONTROL,
            "changes the bus bitrate — can drop every other node off the network",
        ),
    )
}

#: Negative response codes, ISO 14229-1 Annex A.
NRC: dict[int, str] = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x34: "authenticationRequired",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
    0x81: "rpmTooHigh",
    0x82: "rpmTooLow",
    0x83: "engineIsRunning",
    0x84: "engineIsNotRunning",
    0x87: "shifterLeverNotInPark",
    0x92: "voltageTooHigh",
    0x93: "voltageTooLow",
}

#: Data identifiers standardised across manufacturers.  Anything else is
#: vendor-specific and is reported as unknown rather than guessed at.
DATA_IDENTIFIERS: dict[int, str] = {
    0xF180: "bootSoftwareIdentification",
    0xF181: "applicationSoftwareIdentification",
    0xF182: "applicationDataIdentification",
    0xF183: "bootSoftwareFingerprint",
    0xF184: "applicationSoftwareFingerprint",
    0xF186: "activeDiagnosticSession",
    0xF187: "vehicleManufacturerSparePartNumber",
    0xF188: "vehicleManufacturerECUSoftwareNumber",
    0xF189: "vehicleManufacturerECUSoftwareVersionNumber",
    0xF18A: "systemSupplierIdentifier",
    0xF18C: "ECUSerialNumber",
    0xF190: "VIN",
    0xF191: "vehicleManufacturerECUHardwareNumber",
    0xF192: "systemSupplierECUHardwareNumber",
    0xF194: "systemSupplierECUSoftwareNumber",
    0xF195: "systemSupplierECUSoftwareVersionNumber",
    0xF197: "systemNameOrEngineType",
    0xF19E: "ODXFile",
}

_SESSION_TYPES = {
    0x01: "default",
    0x02: "programming",
    0x03: "extendedDiagnostic",
    0x04: "safetySystemDiagnostic",
}
_RESET_TYPES = {
    0x01: "hardReset",
    0x02: "keyOffOnReset",
    0x03: "softReset",
    0x04: "enableRapidPowerShutDown",
    0x05: "disableRapidPowerShutDown",
}


def decode_message(payload: bytes, *, is_response: bool | None = None) -> dict[str, Any]:
    """Decode one reassembled UDS message.

    ``is_response`` may be left as None: a positive response is recognisable
    from its SID, and a negative response from its 0x7F prefix.
    """
    if not payload:
        return {"kind": "empty", "hex": "", "note": "no payload"}

    first = payload[0]

    if first == _NEGATIVE_RESPONSE:
        request_sid = payload[1] if len(payload) > 1 else None
        code = payload[2] if len(payload) > 2 else None
        service = SERVICES.get(request_sid) if request_sid is not None else None
        return {
            "kind": "negative_response",
            "hex": payload.hex().upper(),
            "service": service.name if service else _unknown_sid(request_sid),
            "service_id": f"0x{request_sid:02X}" if request_sid is not None else None,
            "nrc": f"0x{code:02X}" if code is not None else None,
            "nrc_name": NRC.get(code, "unknown") if code is not None else None,
            "pending": code == 0x78,
        }

    is_positive = (
        first >= _POSITIVE_RESPONSE_OFFSET and (first - _POSITIVE_RESPONSE_OFFSET) in SERVICES
    )
    sid = first - _POSITIVE_RESPONSE_OFFSET if is_positive else first
    service = SERVICES.get(sid)
    body = payload[1:]

    decoded: dict[str, Any] = {
        "kind": "response" if is_positive else "request",
        "hex": payload.hex().upper(),
        "service_id": f"0x{sid:02X}",
        "service": service.name if service else _unknown_sid(sid),
        "permission_to_transmit": str(service.permission) if service else "unknown",
        "risk": service.description if service else "unrecognised service; treat as unsafe",
        "data_hex": body.hex().upper(),
    }

    if sid == 0x22 and len(body) >= 2:  # ReadDataByIdentifier
        did = int.from_bytes(body[:2], "big")
        decoded["did"] = f"0x{did:04X}"
        decoded["did_name"] = DATA_IDENTIFIERS.get(did, "vendor-specific")
        if is_positive:
            value = body[2:]
            decoded["value_hex"] = value.hex().upper()
            decoded["value_ascii"] = _printable(value)
    elif sid == 0x10 and body:  # DiagnosticSessionControl
        decoded["session"] = _SESSION_TYPES.get(body[0] & 0x7F, f"0x{body[0]:02X}")
    elif sid == 0x11 and body:  # ECUReset
        decoded["reset_type"] = _RESET_TYPES.get(body[0] & 0x7F, f"0x{body[0]:02X}")
    elif sid == 0x27 and body:  # SecurityAccess
        level = body[0]
        decoded["security_level"] = f"0x{level:02X}"
        decoded["phase"] = "requestSeed" if level % 2 == 1 else "sendKey"
    elif sid == 0x31 and len(body) >= 3:  # RoutineControl
        decoded["routine_control"] = {1: "start", 2: "stop", 3: "requestResults"}.get(
            body[0], f"0x{body[0]:02X}"
        )
        decoded["routine_id"] = f"0x{int.from_bytes(body[1:3], 'big'):04X}"
    elif sid == 0x34 and len(body) >= 2:  # RequestDownload
        decoded["data_format"] = f"0x{body[0]:02X}"
        decoded["address_and_length_format"] = f"0x{body[1]:02X}"

    return decoded


def _unknown_sid(sid: int | None) -> str:
    if sid is None:
        return "unknown"
    # 0xB0-0xBE and similar ranges are reserved for manufacturers; say so
    # rather than inventing a name.
    if 0xA0 <= sid <= 0xBE:
        return f"vendor-specific (0x{sid:02X})"
    return f"unknown (0x{sid:02X})"


def _printable(data: bytes) -> str:
    return "".join(chr(byte) if 0x20 <= byte < 0x7F else "." for byte in data)


def service_catalogue() -> list[dict[str, Any]]:
    """Every known service with the permission transmitting it would need."""
    return [
        {
            "sid": f"0x{service.sid:02X}",
            "name": service.name,
            "permission": str(service.permission),
            "description": service.description,
        }
        for service in sorted(SERVICES.values(), key=lambda entry: entry.sid)
    ]


def summarize_exchange(messages: list[Any]) -> dict[str, Any]:
    """Summarise a decoded UDS conversation.

    Reports the highest permission any observed service would have required,
    which is the number an operator wants before replaying a capture.
    """
    decoded = []
    highest = PermissionLevel.PASSIVE
    for message in messages:
        payload = message.data if hasattr(message, "data") else bytes(message)
        entry = decode_message(payload)
        decoded.append(entry)
        sid_text = entry.get("service_id")
        if sid_text:
            service = SERVICES.get(int(sid_text, 16))
            if service and service.permission.rank > highest.rank:
                highest = service.permission
    return {
        "messages": decoded,
        "count": len(decoded),
        "highest_permission_observed": str(highest),
        "note": (
            "decoding a capture is PASSIVE; the permission above is what "
            "transmitting these services would require"
        ),
    }
