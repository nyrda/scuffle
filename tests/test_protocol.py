"""Check BGS wire bytes, malformed input and notification ordering."""
from collections import deque
import socket
import struct

import pytest

from scuffle.auth import bgs as bgs_module
from scuffle.auth.bgs import BGS, BuildRefused, CredentialRejected, QueueDropped, WOW_TITLE, queued_logon
from scuffle.auth.protobuf import blob, fields, first, integer

# Bytes sent by the launcher version that authenticated against Battle.net.
# A change here alters what the server receives before it consumes a ticket.
CONNECT_REQUEST = bytes.fromhex(
    '000d080010011801283b5d9169446518012a370a0b42475320432b2b2053444b120d0808100118002205'
    '382e312e301a190803101218022211332e31382e322d64643064346463333339')
LOGON_REQUEST = bytes.fromhex(
    '0012080010011802282a5d16822fc06a0343494408d7dedd021204576e36341a04656e55532082a804'
    '52130a065449434b45541a097b22444556223a317d')
GENERATE_REQUEST = bytes.fromhex('001208001003180328055d16822fc06a0343494408d7dedd02')
# Bytes of the browser sign-in that Battle.net accepted.
WEB_LOGON_REQUEST = bytes.fromhex(
    '001208001001180228225d16822fc06a0343494408d7dedd021204576e36341a04656e55532082a804'
    '520b1a097b22444556223a317d')
VERIFY_REQUEST = bytes.fromhex('0012080010021803280b5d16822fc06a034349440a0945552d5449434b4554')
GENERATE_AFTER_VERIFY_REQUEST = bytes.fromhex('001208001003180428055d16822fc06a0343494408d7dedd02')
SIGN_IN_URL = 'https://eu.account.battle.net/login/?externalChallenge=login&app=wow'


class Server:
    """A scripted socket that records what the client sends."""

    def __init__(self, *frames):
        self.incoming = b''.join(frames)
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, size):
        if not self.incoming:
            # A real server keeps the connection open, so the client times out.
            raise socket.timeout
        data, self.incoming = self.incoming[:size], self.incoming[size:]
        return data

    def settimeout(self, _):
        pass


def frame(header, body=b''):
    header += integer(5, len(body))
    return struct.pack('!H', len(header)) + header + body


def reply(token, body=b''):
    return frame(integer(1, 254) + integer(3, token), body)


def logon_complete(status, record=b''):
    body = integer(1, status) + (blob(2, record) if record else b'')
    return frame(integer(1, 0) + integer(2, 1), body)


def challenge(url):
    return frame(integer(1, 0) + integer(2, 4), blob(2, 'web_auth_url') + blob(3, url))


def client(server):
    bgs = BGS.__new__(BGS)
    bgs.socket, bgs.token, bgs.pending, bgs.client_id = server, 0, deque(), None
    return bgs


def test_successful_logon_sends_the_verified_wire_bytes():
    server = Server(reply(1, blob(9, b'CID')), reply(2), logon_complete(0, blob(7, b'TICKET')),
                    reply(3, blob(1, b'NEXT')))
    bgs = client(server)
    bgs.connect()
    bgs.logon(b'TICKET', b'{"DEV":1}', 70658)
    assert bgs.generate_auth_token(WOW_TITLE) == b'NEXT'
    assert server.sent == [CONNECT_REQUEST, LOGON_REQUEST, GENERATE_REQUEST]


def test_rejected_saved_credential_stops_before_issuing_tokens():
    server = Server(reply(1, blob(9, b'CID')), reply(2), logon_complete(3, blob(7, b'x')))
    bgs = client(server)
    bgs.connect()
    with pytest.raises(CredentialRejected, match='logon completed with status 3'):
        bgs.logon(b'TICKET', b'{}', 70658)
    assert len(server.sent) == 2


def refusal(token, status):
    return frame(integer(1, 254) + integer(3, token) + integer(6, status))


@pytest.mark.parametrize('start', [
    lambda bgs: bgs.logon(b'TICKET', b'{}', 70790),
    lambda bgs: bgs.web_challenge(b'{}', 70790),
])
def test_a_build_battlenet_does_not_let_in_is_named_and_stops_the_logon(start):
    server = Server(reply(1, blob(9, b'CID')), refusal(2, 33))
    bgs = client(server)
    bgs.connect()
    with pytest.raises(BuildRefused, match='build 70790'):
        start(bgs)
    assert len(server.sent) == 2


def test_another_refused_logon_is_not_taken_for_a_refused_build():
    server = Server(reply(1, blob(9, b'CID')), refusal(2, 3))
    bgs = client(server)
    bgs.connect()
    with pytest.raises(RuntimeError, match='status 3$') as raised:
        bgs.logon(b'TICKET', b'{}', 70790)
    assert not isinstance(raised.value, BuildRefused)


def queue_update(position, exit_time):
    return frame(integer(1, 0) + integer(2, 2), blob(1, integer(1, position) + integer(2, exit_time) + integer(3, 60)))


QUEUE_END = frame(integer(1, 0) + integer(2, 3))


def test_a_logon_waits_out_the_login_queue_and_reports_its_place():
    # Battle.net sent a queue end and then more updates, so only the logon's own answer ends the wait.
    server = Server(reply(1, blob(9, b'CID')), reply(2), queue_update(746, 1000), QUEUE_END, queue_update(643, 990),
                    logon_complete(0, blob(7, b'TICKET')))
    bgs = client(server)
    seen = []
    bgs.on_queue = lambda position, exit_time: seen.append((position, exit_time))
    bgs.connect()
    assert first(bgs.logon(b'TICKET', b'{}', 70790), 7) == b'TICKET'
    assert seen == [(746, 1000), (643, 990)]


def test_a_queue_update_that_arrives_before_the_logon_reply_still_counts():
    server = Server(reply(1, blob(9, b'CID')), queue_update(5, 1000), reply(2), logon_complete(0, blob(7, b'x')))
    bgs = client(server)
    seen = []
    bgs.on_queue = lambda position, exit_time: seen.append(position)
    bgs.connect()
    bgs.logon(b'TICKET', b'{}', 70790)
    assert seen == [5]


class Closing(Server):
    """A real server closes a queued connection by ending the stream."""

    def recv(self, size):
        if not self.incoming:
            return b''
        return super().recv(size)

    def close(self):
        self.closed = True


def queued_connections(monkeypatch, *servers):
    pending = list(servers)
    monkeypatch.setattr(bgs_module, 'BGS', lambda host: client(pending.pop(0)))
    monkeypatch.setattr(bgs_module, 'QUEUE_RETRY', 0)


def test_a_connection_the_queue_drops_starts_the_logon_over_on_a_new_one(monkeypatch):
    dropped = Closing(reply(1, blob(9, b'CID')), reply(2), queue_update(746, 1000))
    through = Closing(reply(1, blob(9, b'CID')), reply(2), logon_complete(0, blob(7, b'x')))
    queued_connections(monkeypatch, dropped, through)
    seen = []
    bgs, record = queued_logon('host', lambda position, exit_time: seen.append(position),
                               lambda bgs: bgs.logon(b'TICKET', b'{}', 70790))
    assert first(record, 7) == b'x' and bgs.socket is through
    assert seen == [746] and dropped.closed and not hasattr(through, 'closed')
    assert len(dropped.sent) == 2 and len(through.sent) == 2


def test_a_queue_that_never_ends_gives_up(monkeypatch):
    queued_connections(monkeypatch, *[Closing(reply(1, blob(9, b'CID')), reply(2), queue_update(9, 1000))
                                      for _ in range(2)])
    monkeypatch.setattr(bgs_module, 'QUEUE_LIMIT', 0)
    with pytest.raises(RuntimeError, match='login queue did not finish'):
        queued_logon('host', None, lambda bgs: bgs.logon(b'TICKET', b'{}', 70790))


def test_a_closed_connection_outside_the_queue_stays_an_error():
    server = Closing(reply(1, blob(9, b'CID')), reply(2))
    bgs = client(server)
    bgs.connect()
    with pytest.raises(RuntimeError, match='closed the BGS connection') as raised:
        bgs.logon(b'TICKET', b'{}', 70790)
    assert not isinstance(raised.value, QueueDropped)


def test_browser_sign_in_sends_the_verified_wire_bytes():
    server = Server(reply(1, blob(9, b'CID')), reply(2), challenge(SIGN_IN_URL), reply(3),
                    logon_complete(0, blob(7, b'x')), reply(4, blob(1, b'NEXT')))
    bgs = client(server)
    bgs.connect()
    assert bgs.web_challenge(b'{"DEV":1}', 70658) == SIGN_IN_URL
    bgs.verify_web_ticket(b'EU-TICKET')
    assert bgs.generate_auth_token(WOW_TITLE) == b'NEXT'
    assert server.sent == [CONNECT_REQUEST, WEB_LOGON_REQUEST, VERIFY_REQUEST,
                           GENERATE_AFTER_VERIFY_REQUEST]


def test_invalid_sign_in_ticket_is_reported_as_rejected():
    # Observed with a fake ticket: the RPC succeeds, then the challenge repeats.
    server = Server(reply(1, blob(9, b'CID')), reply(2), challenge(SIGN_IN_URL), reply(3),
                    challenge(SIGN_IN_URL))
    bgs = client(server)
    bgs.connect()
    bgs.web_challenge(b'{}', 70658)
    with pytest.raises(RuntimeError, match='did not accept the sign-in ticket'):
        bgs.verify_web_ticket(b'EU-FAKE')
    assert len(server.sent) == 3


def test_sign_in_page_outside_battle_net_is_refused():
    server = Server(reply(1, blob(9, b'CID')), reply(2), challenge('https://battle.net.example/login'))
    bgs = client(server)
    bgs.connect()
    with pytest.raises(RuntimeError, match='outside battle.net'):
        bgs.web_challenge(b'{}', 70658)


@pytest.mark.parametrize('wire', [b'\x80', b'\x0a\x05ab', b'\x0d\x00', b'\x09\x00', b'\x00'])
def test_reject_truncated_or_invalid_message(wire):
    with pytest.raises(ValueError):
        fields(wire)


def test_listener_event_is_preserved_when_it_precedes_rpc_reply():
    bgs = BGS.__new__(BGS)
    bgs.pending = deque()
    event = ([(1, 0, 0), (2, 0, 1)], [(1, 0, 0), (2, 2, b'account-record')])
    reply = ([(1, 0, 254), (3, 0, 3)], [(1, 2, b'reply')])
    incoming = iter([event, reply])
    bgs.receive_wire = lambda: next(incoming)
    assert first(bgs.response(3), 1) == b'reply'
    assert bgs.receive() == event


def test_rpc_rejection_is_not_a_successful_logon():
    bgs = BGS.__new__(BGS)
    bgs.pending = deque()
    bgs.receive_wire = lambda: ([(1, 0, 254), (3, 0, 8), (6, 0, 3)], [])
    with pytest.raises(RuntimeError, match='status 3'):
        bgs.response(8)
