"""The booking webhook's signature and payload reading (F15, ADR-045).

Both pure, and both are where this feature is either safe or not: a public write path whose
signature check is lax is a public write path.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services.booking import payload, signature

SECRET = "a-test-webhook-secret-not-a-real-one"
KEY = "unit-test-credential-key"
ORG = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _headers(body: bytes, *, secret: str = SECRET, timestamp: str = "") -> dict[str, str]:
    headers = {
        "x-cal-signature-256": signature.body_signature(body, secret=secret, timestamp=timestamp)
    }
    if timestamp:
        headers[signature.TIMESTAMP_HEADER] = timestamp
    return headers


class TestTheUrlToken:
    def test_a_token_names_the_workspace_it_was_made_for(self) -> None:
        secret = signature.signing_key(KEY)

        token = signature.sign_token(org_id=ORG, secret=secret)

        assert signature.verify_token(token, secret=secret) == ORG

    def test_an_edited_token_proves_nothing(self) -> None:
        """What must be impossible is editing a token into another workspace's identity. The URL
        is not secret -- it is pasted into a third party's settings page -- so the MAC is the
        only thing standing between that and a cross-tenant write."""
        secret = signature.signing_key(KEY)
        token = signature.sign_token(org_id=ORG, secret=secret)
        payload_part, _, mac = token.partition(".")
        other = signature.sign_token(
            org_id=uuid.UUID("00000000-0000-4000-8000-0000000000bb"), secret=secret
        )

        assert signature.verify_token(f"{other.split('.')[0]}.{mac}", secret=secret) is None
        assert (
            signature.verify_token(f"{payload_part}.AAAAAAAAAAAAAAAAAAAAAA", secret=secret) is None
        )

    def test_a_token_signed_with_another_key_is_refused(self) -> None:
        token = signature.sign_token(org_id=ORG, secret=signature.signing_key("a-different-key"))

        assert signature.verify_token(token, secret=signature.signing_key(KEY)) is None

    @pytest.mark.parametrize("token", ["", ".", "nonsense", "a.b", "!!!.???", "x" * 500, "AAAA"])
    def test_rubbish_returns_none_and_never_raises(self, token: str) -> None:
        """The only caller is a public route anybody may send anything to. A public route that
        can be made to raise is a 500 handled outside the CORS middleware -- the trap that once
        made a duplicate key look like an unreachable backend."""
        assert signature.verify_token(token, secret=signature.signing_key(KEY)) is None

    def test_the_booking_key_is_not_the_unsubscribe_key(self) -> None:
        """Both are derived from `CREDENTIAL_KEY` under different labels, so a token for one can
        never be presented as a token for the other."""
        from app.services import unsubscribe

        assert signature.signing_key(KEY) != unsubscribe.signing_key(KEY)

    def test_the_derived_key_is_not_the_credential_key(self) -> None:
        """`CREDENTIAL_KEY` encrypts mailbox credentials; this signs public URLs. One secret for
        both means a flaw in either reaches the other."""
        assert KEY not in signature.signing_key(KEY)


class TestTheBodySignature:
    def test_a_correctly_signed_body_verifies(self) -> None:
        body = json.dumps({"payload": {"uid": "abc"}}).encode()

        assert signature.verify_body(body, headers=_headers(body), secret=SECRET)

    def test_a_body_changed_by_one_byte_is_refused(self) -> None:
        body = json.dumps({"payload": {"uid": "abc"}}).encode()
        headers = _headers(body)

        assert not signature.verify_body(body + b" ", headers=headers, secret=SECRET)

    def test_a_signature_made_with_another_secret_is_refused(self) -> None:
        body = b'{"payload":{"uid":"abc"}}'

        assert not signature.verify_body(
            body, headers=_headers(body, secret="somebody-elses-secret"), secret=SECRET
        )

    def test_no_signature_header_is_refused(self) -> None:
        """The URL alone books nothing. It ends up in a third party's settings page, their
        delivery logs and anybody's screenshot of either."""
        assert not signature.verify_body(b"{}", headers={}, secret=SECRET)

    def test_the_generic_header_is_accepted_too(self) -> None:
        body = b'{"payload":{"uid":"abc"}}'
        headers = {"X-Booking-Signature": signature.body_signature(body, secret=SECRET)}

        assert signature.verify_body(body, headers=headers, secret=SECRET)

    def test_an_algorithm_prefix_is_accepted(self) -> None:
        """Refusing `sha256=...` would be a refusal nobody could debug from the sender's side."""
        body = b'{"payload":{"uid":"abc"}}'
        headers = {"x-booking-signature": "sha256=" + signature.body_signature(body, secret=SECRET)}

        assert signature.verify_body(body, headers=headers, secret=SECRET)

    def test_a_signature_in_upper_case_hex_is_accepted(self) -> None:
        body = b'{"payload":{"uid":"abc"}}'
        headers = {"x-booking-signature": signature.body_signature(body, secret=SECRET).upper()}

        assert signature.verify_body(body, headers=headers, secret=SECRET)


class TestReplay:
    def test_a_stale_signed_delivery_is_refused(self) -> None:
        """A signed body is valid for ever otherwise, so a copy lifted from a provider's log
        could be replayed a year later."""
        old = str(int((NOW - timedelta(hours=2)).timestamp()))
        body = b'{"payload":{"uid":"abc"}}'

        assert not signature.verify_body(
            body, headers=_headers(body, timestamp=old), secret=SECRET, now=NOW
        )

    def test_a_fresh_signed_delivery_is_accepted(self) -> None:
        recent = str(int((NOW - timedelta(minutes=2)).timestamp()))
        body = b'{"payload":{"uid":"abc"}}'

        assert signature.verify_body(
            body, headers=_headers(body, timestamp=recent), secret=SECRET, now=NOW
        )

    def test_the_timestamp_cannot_be_moved_forward(self) -> None:
        """It is part of the signed payload, so editing it invalidates the signature rather than
        refreshing the delivery."""
        old = str(int((NOW - timedelta(hours=2)).timestamp()))
        body = b'{"payload":{"uid":"abc"}}'
        headers = _headers(body, timestamp=old)
        headers[signature.TIMESTAMP_HEADER] = str(int(NOW.timestamp()))

        assert not signature.verify_body(body, headers=headers, secret=SECRET, now=NOW)

    def test_an_unparseable_timestamp_is_refused_rather_than_ignored(self) -> None:
        body = b'{"payload":{"uid":"abc"}}'
        headers = _headers(body, timestamp="not-a-time")

        assert not signature.verify_body(body, headers=headers, secret=SECRET, now=NOW)

    def test_a_delivery_with_no_timestamp_is_still_accepted(self) -> None:
        """Named as a compromise rather than left implicit: Cal.com signs the body alone, so
        requiring a timestamp would mean refusing the tool this is built for. What such a
        delivery loses is the replay bound, not the secret."""
        assert signature.fresh_enough("", now=NOW)


class TestReadingAPayload:
    def test_a_cal_com_shaped_payload(self) -> None:
        data = {
            "triggerEvent": "BOOKING_CREATED",
            "payload": {
                "uid": "cal-abc123",
                "startTime": "2026-11-10T09:30:00Z",
                "attendees": [{"email": "Ben@Example.com", "name": "Ben Shah"}],
            },
        }

        booking = payload.read(data, provider_hint="cal")

        assert booking.external_id == "cal-abc123"
        assert booking.kind == "booked"
        assert booking.invitee_email == "ben@example.com"
        assert booking.invitee_name == "Ben Shah"
        assert booking.scheduled_at == datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
        assert booking.provider == "cal"

    def test_a_calendly_shaped_payload(self) -> None:
        """A different tool nests everything under `data` and names the event differently. A
        parser written against one shape meets the acceptance criterion and fails for everybody
        else -- which is exactly how the Lever feed's epoch milliseconds were found."""
        data = {
            "event": "invitee.created",
            "payload": {},
            "data": {
                "uuid": "cl-999",
                "scheduled_event": {"start_time": "2026-11-11T14:00:00+05:30"},
                "email": "ben@example.com",
                "name": "Ben Shah",
            },
        }

        booking = payload.read(data)

        assert booking.external_id == "cl-999"
        assert booking.kind == "booked"
        assert booking.invitee_email == "ben@example.com"
        assert booking.scheduled_at is not None

    def test_epoch_milliseconds_are_read_as_a_time(self) -> None:
        data = {"uid": "x1", "startTime": 1_793_000_000_000, "email": "ben@example.com"}

        booking = payload.read(data)

        assert booking.scheduled_at is not None
        assert booking.scheduled_at.year >= 2026

    def test_a_payload_with_no_booking_id_is_an_error_with_a_sentence(self) -> None:
        """Never an empty booking. The job-post intake taught this expensively: an unknown source
        with a plausible payload returned zero postings, which reads as an empty board. Here the
        equivalent is "nobody booked a meeting" about a meeting somebody will attend."""
        with pytest.raises(payload.BookingUnreadable) as caught:
            payload.read({"payload": {"attendees": [{"email": "ben@example.com"}]}})

        assert "booking id" in str(caught.value)

    def test_a_body_that_is_not_an_object_is_an_error(self) -> None:
        with pytest.raises(payload.BookingUnreadable):
            payload.read([1, 2, 3])

    def test_a_booking_with_no_email_is_readable_and_will_match_nobody(self) -> None:
        """Kept rather than refused: the booking happened, and a workspace should be able to see
        one it could not attribute."""
        booking = payload.read({"uid": "x2", "triggerEvent": "BOOKING_CREATED"})

        assert booking.invitee_email is None

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ("BOOKING_CREATED", "booked"),
            ("booking.created", "booked"),
            ("invitee.created", "booked"),
            ("BOOKING_CANCELLED", "cancelled"),
            ("invitee.canceled", "cancelled"),
            ("BOOKING_RESCHEDULED", "rescheduled"),
            ("", "booked"),
            ("something.new", "booked"),
        ],
    )
    def test_an_event_name_is_read_as_a_kind(self, event: str, expected: str) -> None:
        assert payload.kind_of(event) == expected

    def test_a_cancellation_is_never_read_as_a_booking(self) -> None:
        """`cancel` is matched before `book`, because `BOOKING_CANCELLED` contains both."""
        assert payload.kind_of("BOOKING_CANCELLED") == "cancelled"
