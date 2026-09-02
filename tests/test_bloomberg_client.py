from __future__ import annotations

from datetime import date
import unittest

from app.bloomberg_client import (
    BloombergClient,
    BloombergRequestError,
    BloombergTimeoutError,
    HistoricalRequest,
)


class FakeEventType:
    PARTIAL_RESPONSE = "PARTIAL_RESPONSE"
    RESPONSE = "RESPONSE"
    REQUEST_STATUS = "REQUEST_STATUS"
    SESSION_STATUS = "SESSION_STATUS"
    SERVICE_STATUS = "SERVICE_STATUS"
    TIMEOUT = "TIMEOUT"


class FakeOptions:
    def setServerHost(self, host):
        self.host = host

    def setServerPort(self, port):
        self.port = port


class FakeApi:
    Event = FakeEventType
    SessionOptions = FakeOptions


class FakeElement:
    def __init__(self):
        self.values = []

    def appendValue(self, value):
        self.values.append(value)


class FakeOverride:
    def __init__(self):
        self.values = {}

    def setElement(self, name, value):
        self.values[name] = value


class FakeOverridesElement:
    def __init__(self):
        self.values = []

    def appendElement(self):
        override = FakeOverride()
        self.values.append(override)
        return override


class FakeRequest:
    def __init__(self):
        self.elements = {
            "securities": FakeElement(),
            "fields": FakeElement(),
            "overrides": FakeOverridesElement(),
        }
        self.settings = {}

    def getElement(self, name):
        return self.elements[name]

    def set(self, name, value):
        self.settings[name] = value


class FakeService:
    def __init__(self):
        self.requests = []

    def createRequest(self, request_type):
        self.requests.append((request_type, FakeRequest()))
        return self.requests[-1][1]


class FakeEvent:
    def __init__(self, event_type, messages=()):
        self._event_type = event_type
        self._messages = tuple(messages)

    def eventType(self):
        return self._event_type

    def __iter__(self):
        return iter(self._messages)


class FakeSession:
    def __init__(self, events):
        self.events = list(events)
        self.service = FakeService()
        self.stopped = False
        self.sent = []

    def start(self):
        return True

    def openService(self, service):
        return True

    def getService(self, service):
        return self.service

    def sendRequest(self, request):
        self.sent.append(request)

    def nextEvent(self, timeout_ms):
        return self.events.pop(0)

    def stop(self):
        self.stopped = True


def security_message(security, rows=(), *, security_error=None, field_exceptions=()):
    block = {
        "security": security,
        "fieldData": list(rows),
        "fieldExceptions": list(field_exceptions),
    }
    if security_error is not None:
        block["securityError"] = security_error
    return {"securityData": block}


class BloombergClientTests(unittest.TestCase):
    def client_for(self, session):
        return BloombergClient(
            blpapi_module=FakeApi,
            session_factory=lambda options: session,
        )

    def test_partial_and_final_responses_are_combined_and_session_stops(self):
        session = FakeSession(
            (
                FakeEvent(
                    FakeEventType.PARTIAL_RESPONSE,
                    (security_message("WUF26 Comdty", ({"date": date(2025, 1, 2), "PX_LAST": 200.1},)),),
                ),
                FakeEvent(
                    FakeEventType.RESPONSE,
                    (security_message("HOF26 Comdty", ({"date": date(2025, 1, 2), "PX_LAST": 201.2},)),),
                ),
            )
        )
        window = (date(2025, 1, 1), date(2025, 1, 3))
        result = self.client_for(session).fetch(
            (
                HistoricalRequest("WUF26 Comdty", *window),
                HistoricalRequest("HOF26 Comdty", *window),
            ),
            ("PX_LAST",),
            batch_size=25,
            timeout_seconds=30,
        )
        self.assertEqual(len(result.rows), 2)
        self.assertEqual([row["security"] for row in result.rows], ["WUF26 Comdty", "HOF26 Comdty"])
        self.assertFalse(result.warnings)
        self.assertTrue(session.stopped)
        request = session.sent[0]
        self.assertEqual(request.elements["securities"].values, ["WUF26 Comdty", "HOF26 Comdty"])
        self.assertEqual(request.settings["periodicitySelection"], "DAILY")

    def test_security_and_field_errors_are_actionable_warnings(self):
        field_error = {
            "fieldId": "PX_SETTLE",
            "errorInfo": {"category": "BAD_FLD", "message": "Not applicable"},
        }
        session = FakeSession(
            (
                FakeEvent(
                    FakeEventType.RESPONSE,
                    (
                        security_message(
                            "WUF26 Comdty",
                            ({"date": date(2025, 1, 2), "PX_LAST": 200.1},),
                            field_exceptions=(field_error,),
                        ),
                        security_message(
                            "BADF26 Comdty",
                            security_error={"category": "BAD_SEC", "message": "Unknown security"},
                        ),
                    ),
                ),
            )
        )
        result = self.client_for(session).fetch(
            (
                HistoricalRequest("WUF26 Comdty", date(2025, 1, 1), date(2025, 1, 3)),
                HistoricalRequest("BADF26 Comdty", date(2025, 1, 1), date(2025, 1, 3)),
            ),
            ("PX_LAST", "PX_SETTLE"),
            batch_size=25,
            timeout_seconds=30,
        )
        self.assertEqual(len(result.rows), 1)
        self.assertTrue(any("PX_SETTLE" in warning and "BAD_FLD" in warning for warning in result.warnings))
        self.assertTrue(any("BAD_SEC" in warning for warning in result.warnings))
        self.assertTrue(session.stopped)

    def test_request_specific_fields_and_overrides_are_batched_separately(self):
        session = FakeSession(
            (
                FakeEvent(
                    FakeEventType.RESPONSE,
                    (
                        security_message(
                            "QSF26 Comdty",
                            (
                                {
                                    "date": date(2025, 1, 2),
                                    "PX_LAST": 700.0,
                                    "FAIR_VALUE_SNAPSHOT_AT_1630": 701.0,
                                },
                            ),
                        ),
                    ),
                ),
                FakeEvent(
                    FakeEventType.RESPONSE,
                    (
                        security_message(
                            "HOF26 Comdty",
                            ({"date": date(2025, 1, 2), "PX_LAST": 200.1},),
                        ),
                    ),
                ),
            )
        )
        window = (date(2025, 1, 1), date(2025, 1, 3))

        result = self.client_for(session).fetch(
            (
                HistoricalRequest(
                    "QSF26 Comdty",
                    *window,
                    extra_fields=("FAIR_VALUE_SNAPSHOT_AT_1630",),
                    overrides=(("TIME_ZONE_OVERRIDE", "13"),),
                ),
                HistoricalRequest("HOF26 Comdty", *window),
            ),
            ("PX_LAST", "PX_SETTLE"),
            batch_size=25,
            timeout_seconds=30,
        )

        self.assertEqual(len(result.rows), 2)
        self.assertEqual(len(session.sent), 2)
        fair_request = session.sent[0]
        self.assertEqual(fair_request.elements["securities"].values, ["QSF26 Comdty"])
        self.assertEqual(
            fair_request.elements["fields"].values,
            ["PX_LAST", "PX_SETTLE", "FAIR_VALUE_SNAPSHOT_AT_1630"],
        )
        self.assertEqual(
            [override.values for override in fair_request.elements["overrides"].values],
            [{"fieldId": "TIME_ZONE_OVERRIDE", "value": "13"}],
        )
        self.assertEqual(session.sent[1].elements["securities"].values, ["HOF26 Comdty"])
        self.assertFalse(session.sent[1].elements["overrides"].values)

    def test_request_status_raises_and_still_stops_session(self):
        session = FakeSession(
            (
                FakeEvent(
                    FakeEventType.REQUEST_STATUS,
                    ({"messageType": "RequestFailure", "reason": {"message": "Not entitled"}},),
                ),
            )
        )
        with self.assertRaises(BloombergRequestError):
            self.client_for(session).fetch(
                (HistoricalRequest("WUF26 Comdty", date(2025, 1, 1), date(2025, 1, 3)),),
                ("PX_LAST",),
                batch_size=25,
                timeout_seconds=30,
            )
        self.assertTrue(session.stopped)

    def test_timeout_raises_and_still_stops_session(self):
        session = FakeSession((FakeEvent(FakeEventType.TIMEOUT),))
        with self.assertRaises(BloombergTimeoutError):
            self.client_for(session).fetch(
                (HistoricalRequest("WUF26 Comdty", date(2025, 1, 1), date(2025, 1, 3)),),
                ("PX_LAST",),
                batch_size=25,
                timeout_seconds=30,
            )
        self.assertTrue(session.stopped)


if __name__ == "__main__":
    unittest.main()
