"""Small, testable Bloomberg historical-data client.

The Bloomberg dependency is intentionally imported only when a fetch is made.
That keeps the portable dashboard/build tooling usable on machines without a
Bloomberg installation while allowing the authoring pipeline to run against a
licensed Desktop API session on ``localhost:8194``.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import importlib
import math
import time
from typing import Any


REFDATA_SERVICE = "//blp/refdata"
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8194


class BloombergClientError(RuntimeError):
    """Base class for Bloomberg client failures."""


class BloombergDependencyError(BloombergClientError):
    """Raised when the optional Bloomberg Python package is unavailable."""


class BloombergSessionError(BloombergClientError):
    """Raised when the Bloomberg session or reference-data service fails."""


class BloombergRequestError(BloombergClientError):
    """Raised when Bloomberg rejects a historical-data request."""

    def __init__(self, message: str, details: Sequence[str] = ()) -> None:
        self.details = tuple(str(detail) for detail in details if str(detail))
        suffix = f" ({'; '.join(self.details)})" if self.details else ""
        super().__init__(f"{message}{suffix}")


class BloombergTimeoutError(BloombergClientError, TimeoutError):
    """Raised when a pull exceeds its hard wall-clock timeout."""


@dataclass(frozen=True, slots=True)
class HistoricalRequest:
    """One Bloomberg security and the inclusive history window to request."""

    security: str
    start_date: date
    end_date: date
    extra_fields: tuple[str, ...] = ()
    overrides: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        security = str(self.security or "").strip()
        start_date = _coerce_date(self.start_date, "start_date")
        end_date = _coerce_date(self.end_date, "end_date")
        extra_fields = tuple(
            dict.fromkeys(
                str(field or "").strip().upper()
                for field in self.extra_fields
                if str(field or "").strip()
            )
        )
        overrides = tuple(
            dict.fromkeys(
                (
                    str(field or "").strip().upper(),
                    str(value if value is not None else "").strip(),
                )
                for field, value in self.overrides
                if str(field or "").strip()
            )
        )
        if not security:
            raise ValueError("HistoricalRequest.security must not be empty")
        if end_date < start_date:
            raise ValueError("HistoricalRequest.end_date must be on or after start_date")
        object.__setattr__(self, "security", security)
        object.__setattr__(self, "start_date", start_date)
        object.__setattr__(self, "end_date", end_date)
        object.__setattr__(self, "extra_fields", extra_fields)
        object.__setattr__(self, "overrides", overrides)


@dataclass(frozen=True, slots=True)
class BloombergPullResult:
    """Normalized historical rows and non-fatal Bloomberg warnings."""

    rows: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]


def _coerce_date(value: object, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError(f"HistoricalRequest.{label} must be a datetime.date")


def _load_blpapi() -> Any:
    try:
        return importlib.import_module("blpapi")
    except (ImportError, OSError) as exc:
        raise BloombergDependencyError(
            "Bloomberg Python API is not installed or could not be loaded. "
            "Run INSTALL_BLOOMBERG.bat to install blpapi on the licensed "
            "Bloomberg workstation."
        ) from exc


def is_bloomberg_available(blpapi_module: Any | None = None) -> bool:
    """Return whether the optional Bloomberg Python API can be imported.

    This checks the Python dependency, not terminal login, entitlements, or the
    reachability of the Desktop API service. ``blpapi_module`` is accepted so a
    lightweight fake can be used in tests without installing Bloomberg.
    """

    if blpapi_module is not None:
        return True
    try:
        _load_blpapi()
    except BloombergDependencyError:
        return False
    return True


class BloombergClient:
    """Fetch Bloomberg history through one Desktop API session.

    ``blpapi_module``, ``session_factory``, and ``monotonic`` are injectable so
    event handling can be unit-tested on machines that do not have Bloomberg.
    A session factory receives the configured ``SessionOptions`` object. The
    timeout is applied independently to session setup and to every Bloomberg
    batch, so a large update does not consume one shared wall-clock budget.
    """

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        service: str = REFDATA_SERVICE,
        blpapi_module: Any | None = None,
        session_factory: Callable[[Any], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host = str(host or DEFAULT_HOST)
        self.port = int(port)
        self.service = str(service or REFDATA_SERVICE).strip()
        if not self.service.startswith("//"):
            raise ValueError("service must be a Bloomberg service path beginning with //")
        self._blpapi_module = blpapi_module
        self._session_factory = session_factory
        self._monotonic = monotonic

    def fetch(
        self,
        requests: Sequence[HistoricalRequest],
        fields: Sequence[str],
        *,
        batch_size: int = 50,
        timeout_seconds: int = 120,
    ) -> BloombergPullResult:
        """Fetch and normalize Bloomberg history for the requested securities.

        Requests with identical date ranges are combined into Bloomberg batches.
        Security errors, field exceptions, and empty securities are returned as
        warnings. Session, response, request-status, and timeout failures raise a
        ``BloombergClientError`` subclass and never return a partial success.
        ``timeout_seconds`` is a hard limit for session setup and, separately,
        for each batch's ``sendRequest`` plus response-event cycle.
        """

        normalized_requests = _normalize_requests(requests)
        normalized_fields = _normalize_fields(fields)
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not normalized_requests:
            return BloombergPullResult(rows=(), warnings=())

        api = self._blpapi_module or _load_blpapi()
        session = self._create_session(api)
        setup_deadline = self._monotonic() + float(timeout_seconds)
        all_rows: list[dict[str, Any]] = []
        warnings: list[str] = []

        try:
            try:
                started = session.start()
            except Exception as exc:
                raise BloombergSessionError(
                    f"Could not start Bloomberg session at {self.host}:{self.port}: {exc}"
                ) from exc
            if not started:
                raise BloombergSessionError(
                    f"Could not start Bloomberg session at {self.host}:{self.port}"
                )
            self._check_deadline(
                setup_deadline, timeout_seconds, "Bloomberg session setup"
            )

            try:
                opened = session.openService(self.service)
            except Exception as exc:
                raise BloombergSessionError(
                    f"Could not open Bloomberg service {self.service}: {exc}"
                ) from exc
            if not opened:
                raise BloombergSessionError(
                    f"Could not open Bloomberg service {self.service}"
                )
            self._check_deadline(
                setup_deadline, timeout_seconds, "Bloomberg session setup"
            )

            try:
                service = session.getService(self.service)
            except Exception as exc:
                raise BloombergSessionError(
                    f"Could not access Bloomberg service {self.service}: {exc}"
                ) from exc
            if service is None:
                raise BloombergSessionError(
                    f"Could not access Bloomberg service {self.service}"
                )
            self._check_deadline(
                setup_deadline, timeout_seconds, "Bloomberg session setup"
            )

            for start_date, end_date, extra_fields, overrides, securities in _group_requests(
                normalized_requests
            ):
                request_fields = tuple(dict.fromkeys((*normalized_fields, *extra_fields)))
                for batch in _batched(securities, batch_size):
                    request = _build_historical_request(
                        service,
                        batch,
                        request_fields,
                        start_date,
                        end_date,
                        overrides,
                    )
                    # Each batch gets a fresh budget covering send plus response.
                    request_deadline = self._monotonic() + float(timeout_seconds)
                    try:
                        session.sendRequest(request)
                    except Exception as exc:
                        joined = ", ".join(batch)
                        raise BloombergRequestError(
                            f"Could not send Bloomberg historical request for {joined}",
                            (str(exc),),
                        ) from exc
                    self._check_deadline(
                        request_deadline,
                        timeout_seconds,
                        "Bloomberg historical request",
                    )

                    batch_rows, batch_warnings = self._read_response(
                        api=api,
                        session=session,
                        securities=batch,
                        fields=request_fields,
                        deadline=request_deadline,
                        timeout_seconds=timeout_seconds,
                    )
                    all_rows.extend(batch_rows)
                    warnings.extend(batch_warnings)
        finally:
            try:
                session.stop()
            except Exception:
                # A stop failure must not mask a request/session/timeout failure.
                pass

        security_order = {
            request.security: index for index, request in enumerate(normalized_requests)
        }
        all_rows.sort(
            key=lambda row: (
                security_order.get(str(row.get("security", "")), len(security_order)),
                str(row.get("date", "")),
            )
        )
        return BloombergPullResult(
            rows=tuple(all_rows),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _create_session(self, api: Any) -> Any:
        try:
            options = api.SessionOptions()
            options.setServerHost(self.host)
            options.setServerPort(self.port)
            factory = self._session_factory or api.Session
            return factory(options)
        except Exception as exc:
            raise BloombergSessionError(
                f"Could not configure Bloomberg session at {self.host}:{self.port}: {exc}"
            ) from exc

    def _check_deadline(
        self,
        deadline: float,
        timeout_seconds: int,
        operation: str,
    ) -> None:
        if self._monotonic() >= deadline:
            raise BloombergTimeoutError(
                f"{operation} exceeded hard timeout of {timeout_seconds} seconds"
            )

    def _read_response(
        self,
        *,
        api: Any,
        session: Any,
        securities: Sequence[str],
        fields: Sequence[str],
        deadline: float,
        timeout_seconds: int,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        seen_securities: set[str] = set()

        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise BloombergTimeoutError(
                    "Bloomberg historical request exceeded hard timeout of "
                    f"{timeout_seconds} seconds"
                )
            timeout_ms = max(1, min(math.ceil(remaining * 1000), 2_147_483_647))
            try:
                event = session.nextEvent(timeout_ms)
            except Exception as exc:
                raise BloombergSessionError(
                    f"Bloomberg session failed while waiting for a response: {exc}"
                ) from exc
            self._check_deadline(
                deadline,
                timeout_seconds,
                "Bloomberg historical request",
            )

            event_type = event.eventType()
            if _event_is(api, event_type, "TIMEOUT"):
                raise BloombergTimeoutError(
                    "Bloomberg historical request exceeded hard timeout of "
                    f"{timeout_seconds} seconds"
                )

            if _event_is(api, event_type, "REQUEST_STATUS"):
                details = tuple(_status_message_text(message) for message in _iter_messages(event))
                raise BloombergRequestError(
                    "Bloomberg reported a historical request failure",
                    tuple(detail for detail in details if detail),
                )

            if _event_is(api, event_type, "SESSION_STATUS") or _event_is(
                api, event_type, "SERVICE_STATUS"
            ):
                failures = [
                    text
                    for message in _iter_messages(event)
                    if (text := _status_failure_text(message))
                ]
                if failures:
                    raise BloombergSessionError(
                        "Bloomberg session/service failure: " + "; ".join(failures)
                    )
                continue

            is_partial = _event_is(api, event_type, "PARTIAL_RESPONSE")
            is_response = _event_is(api, event_type, "RESPONSE")
            if not (is_partial or is_response):
                continue

            for message in _iter_messages(event):
                response_error = _get_named(message, "responseError")
                if response_error is not None:
                    detail = _format_error_info(response_error)
                    raise BloombergRequestError(
                        "Bloomberg rejected the historical request",
                        (detail,) if detail else (),
                    )

                security_data = _get_named(message, "securityData")
                if security_data is None:
                    continue
                for block in _iter_complex_values(security_data):
                    block_rows, block_warnings, security = _parse_security_data(
                        block, fields
                    )
                    if security:
                        seen_securities.add(security)
                    rows.extend(block_rows)
                    warnings.extend(block_warnings)

            if is_response:
                break

        for security in securities:
            if security not in seen_securities:
                warnings.append(
                    f"security {security}: Bloomberg returned no security response"
                )
        return rows, warnings


def _normalize_requests(
    requests: Sequence[HistoricalRequest],
) -> tuple[HistoricalRequest, ...]:
    normalized: list[HistoricalRequest] = []
    seen: set[
        tuple[
            str,
            date,
            date,
            tuple[str, ...],
            tuple[tuple[str, str], ...],
        ]
    ] = set()
    for item in requests:
        if not isinstance(item, HistoricalRequest):
            raise TypeError("requests must contain HistoricalRequest values")
        key = (
            item.security,
            item.start_date,
            item.end_date,
            item.extra_fields,
            item.overrides,
        )
        if key not in seen:
            normalized.append(item)
            seen.add(key)
    return tuple(normalized)


def _normalize_fields(fields: Sequence[str]) -> tuple[str, ...]:
    if isinstance(fields, (str, bytes)):
        raise TypeError("fields must be a sequence of Bloomberg field names")
    normalized = tuple(
        dict.fromkeys(str(field or "").strip().upper() for field in fields if str(field or "").strip())
    )
    if not normalized:
        raise ValueError("fields must contain at least one Bloomberg field name")
    return normalized


def _group_requests(
    requests: Sequence[HistoricalRequest],
) -> Iterable[
    tuple[
        date,
        date,
        tuple[str, ...],
        tuple[tuple[str, str], ...],
        tuple[str, ...],
    ]
]:
    grouped: OrderedDict[
        tuple[date, date, tuple[str, ...], tuple[tuple[str, str], ...]],
        list[str],
    ] = OrderedDict()
    for request in requests:
        key = (
            request.start_date,
            request.end_date,
            request.extra_fields,
            request.overrides,
        )
        securities = grouped.setdefault(key, [])
        if request.security not in securities:
            securities.append(request.security)
    for (start_date, end_date, extra_fields, overrides), securities in grouped.items():
        yield start_date, end_date, extra_fields, overrides, tuple(securities)


def _batched(values: Sequence[str], size: int) -> Iterable[tuple[str, ...]]:
    for index in range(0, len(values), size):
        yield tuple(values[index : index + size])


def _build_historical_request(
    service: Any,
    securities: Sequence[str],
    fields: Sequence[str],
    start_date: date,
    end_date: date,
    overrides: Sequence[tuple[str, str]] = (),
) -> Any:
    try:
        request = service.createRequest("HistoricalDataRequest")
        security_element = request.getElement("securities")
        for security in securities:
            security_element.appendValue(security)
        field_element = request.getElement("fields")
        for field in fields:
            field_element.appendValue(field)
        if overrides:
            override_element = request.getElement("overrides")
            for field, value in overrides:
                override = override_element.appendElement()
                override.setElement("fieldId", field)
                override.setElement("value", value)
        _set_request_value(request, "startDate", start_date.strftime("%Y%m%d"))
        _set_request_value(request, "endDate", end_date.strftime("%Y%m%d"))
        _set_request_value(request, "periodicitySelection", "DAILY")
        return request
    except BloombergClientError:
        raise
    except Exception as exc:
        raise BloombergRequestError(
            "Could not build Bloomberg HistoricalDataRequest", (str(exc),)
        ) from exc


def _set_request_value(request: Any, name: str, value: object) -> None:
    setter = getattr(request, "set", None)
    if callable(setter):
        setter(name, value)
        return
    setter = getattr(request, "setElement", None)
    if callable(setter):
        setter(name, value)
        return
    raise AttributeError("Bloomberg request does not expose set/setElement")


def _event_is(api: Any, event_type: object, name: str) -> bool:
    event_class = getattr(api, "Event", None)
    expected = getattr(event_class, name, None) if event_class is not None else None
    if expected is not None and event_type == expected:
        return True
    candidate = getattr(event_type, "name", event_type)
    normalized = str(candidate).upper().replace(" ", "_")
    return normalized == name or normalized.endswith(f".{name}")


def _iter_messages(event: Any) -> Iterable[Any]:
    try:
        yield from iter(event)
        return
    except TypeError:
        pass
    messages = getattr(event, "messages", None)
    if callable(messages):
        yield from messages()


def _get_named(container: Any, name: str) -> Any | None:
    if container is None:
        return None
    if isinstance(container, Mapping):
        if name in container:
            return container[name]
        lower_name = name.lower()
        for key, value in container.items():
            if str(key).lower() == lower_name:
                return value
        return None
    has_element = getattr(container, "hasElement", None)
    if callable(has_element):
        try:
            if not has_element(name):
                return None
        except TypeError:
            if not has_element(name, True):
                return None
    get_element = getattr(container, "getElement", None)
    if callable(get_element):
        try:
            return get_element(name)
        except Exception:
            return None
    return getattr(container, name, None)


def _iter_complex_values(element: Any) -> Iterable[Any]:
    if element is None:
        return
    if isinstance(element, Mapping):
        yield element
        return
    if isinstance(element, (list, tuple)):
        yield from element
        return
    is_array = getattr(element, "isArray", None)
    if callable(is_array) and is_array():
        count = int(element.numValues())
        for index in range(count):
            getter = getattr(element, "getValueAsElement", None)
            yield getter(index) if callable(getter) else element.getValue(index)
        return
    yield element


def _iter_row_items(row: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(row, Mapping):
        for name, value in row.items():
            yield str(name), _normalize_value(value)
        return
    count_method = getattr(row, "numElements", None)
    get_element = getattr(row, "getElement", None)
    if callable(count_method) and callable(get_element):
        for index in range(int(count_method())):
            child = get_element(index)
            child_name = getattr(child, "name", None)
            name = child_name() if callable(child_name) else child_name
            yield str(name), _element_value(child)


def _element_value(element: Any) -> Any:
    if element is None:
        return None
    if isinstance(element, (str, int, float, bool, date, datetime)):
        return element
    is_null = getattr(element, "isNull", None)
    if callable(is_null) and is_null():
        return None
    getter = getattr(element, "getValue", None)
    if callable(getter):
        try:
            return _normalize_value(getter())
        except Exception:
            pass
    string_getter = getattr(element, "getValueAsString", None)
    if callable(string_getter):
        try:
            return string_getter()
        except Exception:
            pass
    return _normalize_value(element)


def _normalize_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, date, datetime)):
        return value
    to_python = getattr(value, "toPython", None)
    if callable(to_python):
        try:
            return to_python()
        except Exception:
            pass

    def component(name: str) -> int | None:
        candidate = getattr(value, name, None)
        candidate = candidate() if callable(candidate) else candidate
        try:
            return int(candidate) if candidate is not None else None
        except (TypeError, ValueError):
            return None

    year, month, day = component("year"), component("month"), component("day")
    if year is not None and month is not None and day is not None:
        hour = component("hour")
        minute = component("minute")
        second = component("second")
        if hour is not None:
            return datetime(year, month, day, hour, minute or 0, second or 0)
        return date(year, month, day)
    return value


def _parse_security_data(
    security_data: Any,
    fields: Sequence[str],
) -> tuple[list[dict[str, Any]], list[str], str]:
    security_value = _get_named(security_data, "security")
    security = str(_element_value(security_value) or "").strip()
    warnings: list[str] = []

    security_error = _get_named(security_data, "securityError")
    if security_error is not None:
        detail = _format_error_info(security_error)
        warnings.append(
            f"security {security or '<unknown>'}: {detail or 'Bloomberg security error'}"
        )
        return [], warnings, security

    errored_fields: set[str] = set()
    field_exceptions = _get_named(security_data, "fieldExceptions")
    if field_exceptions is not None:
        for exception in _iter_complex_values(field_exceptions):
            field_value = _get_named(exception, "fieldId")
            field = str(_element_value(field_value) or "<unknown>").strip().upper()
            error_info = _get_named(exception, "errorInfo")
            if error_info is None:
                error_info = exception
            detail = _format_error_info(error_info)
            warnings.append(
                f"security {security or '<unknown>'} field {field}: "
                f"{detail or 'Bloomberg field error'}"
            )
            errored_fields.add(field)

    field_data = _get_named(security_data, "fieldData")
    data_rows = list(_iter_complex_values(field_data)) if field_data is not None else []
    if not data_rows:
        warnings.append(
            f"security {security or '<unknown>'}: Bloomberg returned no historical rows"
        )
        return [], warnings, security

    requested_by_upper = {field.upper(): field for field in fields}
    rows: list[dict[str, Any]] = []
    fields_with_values: set[str] = set()
    for data_row in data_rows:
        values = {name.upper(): value for name, value in _iter_row_items(data_row)}
        row_date = values.get("DATE")
        if row_date is None:
            warnings.append(
                f"security {security or '<unknown>'}: historical row without a date was skipped"
            )
            continue
        record: dict[str, Any] = {"security": security, "date": row_date}
        for upper_name, output_name in requested_by_upper.items():
            value = values.get(upper_name)
            record[output_name] = value
            if value is not None:
                fields_with_values.add(upper_name)
        rows.append(record)

    if not rows:
        warnings.append(
            f"security {security or '<unknown>'}: Bloomberg returned no usable historical rows"
        )
    else:
        for field in fields:
            upper_field = field.upper()
            if upper_field not in fields_with_values and upper_field not in errored_fields:
                warnings.append(
                    f"security {security or '<unknown>'} field {field}: "
                    "Bloomberg returned no values"
                )
    return rows, warnings, security


def _format_error_info(error_info: Any) -> str:
    parts: list[str] = []
    for name in ("source", "code", "category", "subcategory", "message"):
        raw_value = _get_named(error_info, name)
        value = _element_value(raw_value)
        if value not in (None, ""):
            parts.append(f"{name}={value}")
    if parts:
        return ", ".join(parts)
    text = "" if error_info is None else str(error_info).strip()
    return text if text and not text.startswith("<") else ""


def _message_type(message: Any) -> str:
    value = getattr(message, "messageType", None)
    value = value() if callable(value) else value
    if value is None and isinstance(message, Mapping):
        value = message.get("messageType") or message.get("type")
    return str(value or "").strip()


def _status_message_text(message: Any) -> str:
    message_type = _message_type(message) or "RequestStatus"
    reason = _get_named(message, "reason")
    if reason is None:
        reason = _get_named(message, "responseError")
    detail = _format_error_info(reason) if reason is not None else ""
    if detail:
        return f"{message_type}: {detail}"
    to_string = getattr(message, "toString", None)
    if callable(to_string):
        try:
            rendered = str(to_string()).strip()
            if rendered:
                return f"{message_type}: {rendered}"
        except Exception:
            pass
    return message_type


def _status_failure_text(message: Any) -> str:
    message_type = _message_type(message)
    normalized = message_type.upper()
    failure_tokens = ("FAIL", "TERMINAT", "DOWN", "STOPPED", "CONNECTIONLOST")
    if not any(token in normalized for token in failure_tokens):
        return ""
    return _status_message_text(message)


__all__ = [
    "BloombergClient",
    "BloombergClientError",
    "BloombergDependencyError",
    "BloombergPullResult",
    "BloombergRequestError",
    "BloombergSessionError",
    "BloombergTimeoutError",
    "HistoricalRequest",
    "is_bloomberg_available",
]
