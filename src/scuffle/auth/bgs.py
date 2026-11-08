"""Authenticate and renew the native session through BGS.

Credentials stay in child pipes and process memory. Only protocol event names,
status codes, and credential lengths are logged to stderr. No password or token
is printed.
"""
from collections import deque
import socket
import ssl
import struct
import sys
import time
import urllib.parse

from .protobuf import blob, fields, first, integer, varint

PORT = 1119
CONNECTION_SERVICE = 0x65446991
AUTHENTICATION_SERVICE = 0xc02f8216
ACCOUNT_SERVICE = 0x22dc2464         # bnet.protocol.account.v2.client.AccountService
RESPONSE_SERVICE = 254
CONNECT = LOGON = LOGON_COMPLETE = 1
QUEUE_UPDATE = 2
QUEUE_END = 3
VERIFY_AUTH_TOKEN = 2
GENERATE_AUTH_TOKEN = 3
EXTERNAL_CHALLENGE = 4
GET_GAME_ACCOUNT_INFO = 201
WOW_TITLE = 0x576f57
PLATFORM = 'Wn64'
LOCALE = 'enUS'
CHALLENGES = (b'web_auth_url', b'legal_form', b'authenticator')
# Battle.net's answer to a logon from a build it does not let in yet, with or without a login.
ERROR_SERVER_IS_PRIVATE = 33
LOGON_WAIT = 30
# Battle.net sends a queue update about every 25 seconds, a queue end now and then and more updates after it.
QUEUE_SILENCE = 120
# It also closes a queued connection after a minute or two, and the logon starts over on a new one.
QUEUE_RETRY = 3
QUEUE_LIMIT = 30 * 60
# Game account regions as the launch settings name them. Battle.net writes REGION=EU for region 2.
GAME_REGIONS = {1: 'US', 2: 'EU', 3: 'KR', 4: 'TW', 5: 'CN'}

_sdk_version = integer(1, 8) + integer(2, 1) + integer(3, 0) + blob(4, '8.1.0')
_protocol_version = integer(1, 3) + integer(2, 18) + integer(3, 2) + blob(4, '3.18.2-dd0d4dc339')
SDK_INFO = blob(1, 'BGS C++ SDK') + blob(2, _sdk_version) + blob(3, _protocol_version)


class CredentialRejected(RuntimeError):
    """Battle.net refused the presented credential and wants a new sign-in."""


class RpcRejected(RuntimeError):
    def __init__(self, status):
        super().__init__(f'BGS RPC rejected with status {status}')
        self.status = status


class QueueDropped(RuntimeError):
    """The connection ended while Battle.net's login queue held this logon."""


class BuildRefused(RuntimeError):
    """Battle.net refuses every logon from this build for now. The session is not at fault, so it stays."""

    def __init__(self, version):
        super().__init__(f'Battle.net is refusing logons from game build {version} right now, which can '
                         'happen while Blizzard rolls out a patch. Try again later.')


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


def launch_name(name) -> bool:
    """Whether a game account name can go into the launch settings as it is."""
    return (isinstance(name, str) and 0 < len(name) <= 64 and name.strip() == name
            and all(32 <= ord(c) < 127 for c in name))


class BGS:
    on_queue = staticmethod(lambda position, exit_time: None)
    queued = False

    def __init__(self, host):
        context = ssl.create_default_context()
        connection = socket.create_connection((host, PORT), timeout=10)
        try:
            self.socket = context.wrap_socket(connection, server_hostname=host)
        except BaseException:
            connection.close()
            raise
        self.socket.settimeout(15)
        self.token = 0
        self.pending = deque()
        self.client_id = None

    def exact(self, size):
        result = bytearray()
        while len(result) < size:
            data = self.socket.recv(size - len(result))
            if not data:
                raise RuntimeError('Server closed the BGS connection')
            result.extend(data)
        return bytes(result)

    def send(self, service, method, body):
        self.token += 1
        header = (integer(1, 0) + integer(2, method) + integer(3, self.token) +
                  integer(5, len(body)) + varint(11 << 3 | 5) + struct.pack('<I', service))
        if self.client_id:
            header += blob(13, self.client_id)
        self.socket.sendall(struct.pack('!H', len(header)) + header + body)
        return self.token

    def receive_wire(self):
        size = struct.unpack('!H', self.exact(2))[0]
        if size > 8192:
            raise RuntimeError('Unexpected BGS header size')
        header = fields(self.exact(size))
        body_size = first(header, 5, 0)
        if body_size > 1024 * 1024:
            raise RuntimeError('Unexpected BGS payload size')
        return header, fields(self.exact(body_size))

    def receive(self):
        return self.pending.popleft() if self.pending else self.receive_wire()

    def response(self, token):
        for _ in range(32):
            header, body = self.receive_wire()
            service = first(header, 1, 0)
            if service == RESPONSE_SERVICE and first(header, 3) == token:
                status = first(header, 6, 0)
                if status:
                    raise RpcRejected(status)
                return body
            if service != RESPONSE_SERVICE:
                # AuthenticationListener methods return NO_RESPONSE. Preserve
                # them for the login loop when they arrive before the RPC reply.
                self.pending.append((header, body))
            log('unsolicited_packet', 'service', service, 'method', first(header, 2, 0))
        raise RuntimeError('BGS response did not arrive')

    def call(self, service, method, body):
        return self.response(self.send(service, method, body))

    def connect(self):
        connected = self.call(CONNECTION_SERVICE, CONNECT, integer(3, 1) + blob(5, SDK_INFO))
        self.client_id = first(connected, 9)
        log('bgs_connected', 'connection_client_id_present', bool(self.client_id))

    def request_logon(self, device, version, credential=None):
        options = (blob(1, credential) if credential else b'') + blob(3, device)
        request = (integer(1, WOW_TITLE) + blob(2, PLATFORM) + blob(3, LOCALE) +
                   integer(4, version) + blob(10, options))
        try:
            self.call(AUTHENTICATION_SERVICE, LOGON, request)
        except RpcRejected as error:
            if error.status == ERROR_SERVER_IS_PRIVATE:
                raise BuildRefused(version) from error
            raise
        log('logon_request_accepted', 'with_credential', bool(credential))

    def notifications(self):
        """The logon's notifications. Battle.net's login queue is waited out here and has no time limit of its own."""
        deadline = time.monotonic() + LOGON_WAIT
        while time.monotonic() < deadline:
            try:
                header, body = self.receive()
            except (RuntimeError, OSError) as error:
                if self.queued:
                    raise QueueDropped(str(error)) from error
                raise
            method = first(header, 2, 0)
            if method not in (QUEUE_UPDATE, QUEUE_END):
                yield header, body
                continue
            if not self.queued:
                self.queued = True
                self.socket.settimeout(QUEUE_SILENCE)
            deadline = time.monotonic() + QUEUE_SILENCE
            if method == QUEUE_UPDATE:
                state = fields(first(body, 1, b''))
                log('login_queue', 'position', first(state, 1, 0))
                self.on_queue(first(state, 1, 0), first(state, 2, 0))

    def logon(self, credential, device, version):
        self.request_logon(device, version, credential)
        return self.await_logon(credential)

    def web_challenge(self, device, version):
        """Start a logon without a credential and return the browser sign-in URL."""
        self.request_logon(device, version)
        for header, body in self.notifications():
            method = first(header, 2, 0)
            if method == EXTERNAL_CHALLENGE and first(body, 2) == b'web_auth_url':
                url = first(body, 3, b'').decode(errors='replace')
                parts = urllib.parse.urlsplit(url)
                if parts.scheme != 'https' or not (parts.hostname or '').endswith('.battle.net'):
                    raise RuntimeError('BGS offered a sign-in page outside battle.net')
                return url
            if method == LOGON_COMPLETE and isinstance(first(body, 1), int) and first(body, 1):
                raise RuntimeError(f'BGS logon completed with status {first(body, 1)}')
            log('login_notification', 'method', method)
        raise RuntimeError('BGS offered no browser sign-in')

    def verify_web_ticket(self, ticket):
        """Complete a pending web challenge with the ticket from the sign-in page."""
        self.call(AUTHENTICATION_SERVICE, VERIFY_AUTH_TOKEN, blob(1, ticket))
        log('web_ticket_submitted')
        # The RPC succeeds even for an invalid ticket. The server then answers
        # with a new challenge or a failed OnLogonComplete instead.
        try:
            return self.await_logon(ticket)
        except CredentialRejected as error:
            raise RuntimeError(f'Battle.net did not accept the sign-in ticket ({error}). '
                               'Sign in again, since each ticket works only once') from None

    def await_logon(self, credential):
        for header, body in self.notifications():
            method = first(header, 2, 0)
            if method == LOGON_COMPLETE and isinstance(first(body, 1, 0), int) and first(body, 2):
                status = first(body, 1, 0)
                log('logon_complete', 'status', status)
                if status:
                    raise CredentialRejected(f'BGS logon completed with status {status}')
                record = fields(first(body, 2))
                log('login_ticket_present', bool(first(record, 7)),
                    'login_ticket_echoes_input', first(record, 7) == credential)
                return record
            if isinstance(first(body, 2), bytes) and first(body, 2) in CHALLENGES:
                self.reject_challenge()
            if method == LOGON_COMPLETE and isinstance(first(body, 1), int) and first(body, 1):
                raise CredentialRejected(f'BGS login notification has status {first(body, 1)}')
            log('login_notification', 'method', method)
        raise RuntimeError('No successful BGS logon notification')

    def reject_challenge(self):
        # An invalid remembered ticket can produce both a web URL and an
        # OnLogonComplete error. Report that rejection before treating the URL
        # as evidence of a valid pending challenge.
        self.socket.settimeout(2)
        try:
            for _ in range(8):
                header, body = self.receive()
                if first(header, 2) == LOGON_COMPLETE and isinstance(first(body, 1), int):
                    status = first(body, 1)
                    log('logon_complete_after_challenge', 'status', status)
                    if status:
                        raise CredentialRejected(f'BGS rejected the saved credential with status {status}')
                    self.pending.appendleft((header, body))
                    break
        except socket.timeout:
            pass
        finally:
            self.socket.settimeout(15)
        raise CredentialRejected('BGS requires an interactive authentication challenge')

    def game_accounts(self, record):
        """The WoW accounts under this Battle.net login, named as the game knows them.

        LogonRecord lists their handles. GetGameAccountInfo names each one, as the game's own
        account choice does. None means the names could not be read, which is not an empty list.
        """
        accounts = []
        try:
            for handle in (value for number, _, value in record if number == 2):
                items = fields(handle)
                if first(items, 2) != WOW_TITLE:
                    continue
                info = fields(first(self.call(ACCOUNT_SERVICE, GET_GAME_ACCOUNT_INFO, blob(1, handle)), 1, b''))
                name = first(info, 2, b'').decode(errors='replace')
                identifier, region = first(items, 1), GAME_REGIONS.get(first(items, 3))
                if not isinstance(identifier, int) or region is None or not launch_name(name):
                    raise ValueError('unusable game account')
                accounts.append({'id': identifier, 'name': name, 'region': region})
        except (RuntimeError, ValueError, OSError) as error:
            log('game_accounts_unavailable', type(error).__name__)
            return None
        log('game_accounts', 'count', len(accounts))
        return accounts

    def generate_auth_token(self, title):
        token = first(self.call(AUTHENTICATION_SERVICE, GENERATE_AUTH_TOKEN, integer(1, title)), 1)
        if not isinstance(token, bytes) or not token:
            raise RuntimeError('GenerateAuthToken returned no credential')
        return token


def logon_battle_tag(record):
    """The BattleTag the LogonRecord names, as the only text field with its form, or None."""
    from .vault import battle_tag
    for _, _, value in record:
        if isinstance(value, bytes):
            try:
                tag = battle_tag(value.decode())
            except UnicodeDecodeError:
                continue
            if tag:
                return tag
    return None


def queued_logon(host, queue, start):
    """Connect and run start(bgs), again on a new connection whenever the login queue drops it.

    Returns the open connection and start's result. A failure closes the connection.
    """
    give_up = time.monotonic() + QUEUE_LIMIT
    while True:
        bgs = BGS(host)
        if queue:
            bgs.on_queue = queue
        try:
            bgs.connect()
            return bgs, start(bgs)
        except QueueDropped as error:
            bgs.socket.close()
            if time.monotonic() >= give_up:
                raise RuntimeError("Battle.net's login queue did not finish in 30 minutes. Try again later.") from error
            log('login_queue_dropped')
            time.sleep(QUEUE_RETRY)
        except BaseException:
            bgs.socket.close()
            raise


def login(vault, host, version, sign_in, queue=None):
    """Sign in through the browser and save a fresh broker credential for the account signed in to.

    sign_in receives the battle.net URL and returns the ticket from its redirect.
    No connection stays open while the user signs in. Returns the login the session was saved to.
    """
    device = vault.device_id()
    bgs, url = queued_logon(host, queue, lambda bgs: bgs.web_challenge(device, version))
    bgs.socket.close()
    ticket = sign_in(url)
    bgs, _ = queued_logon(host, queue, lambda bgs: bgs.web_challenge(device, version))
    try:
        record = bgs.verify_web_ticket(ticket)
        # Keyed by the account id the logon names, so the session is saved as that account only.
        with vault.adding(first(record, 1)) as added:
            added.save_session(bgs.generate_auth_token(WOW_TITLE))
            # What a sign-in hands back, when it is not just the page's one-time ticket again.
            login_ticket = first(record, 7)
            if isinstance(login_ticket, bytes) and login_ticket and login_ticket != ticket:
                added.save_login_ticket(login_ticket)
            log('unused_broker_credential_saved_protected')
            added.save_battle_tag(logon_battle_tag(record))
            # After the credential is saved, so a failed lookup never costs the sign-in.
            added.save_accounts(first(record, 1), bgs.game_accounts(record))
        return added
    finally:
        bgs.socket.close()


def renew(login, host, version, queue=None, credential=None):
    """Consume a login's broker credential, then save its successor and a game credential.

    credential is the login ticket when the saved session no longer works.
    """
    credential = login.load_session() if credential is None else credential
    log('credential_loaded', 'bytes', len(credential))
    device = login.device_id()
    log('logon_device_id', 'bytes', len(device))
    bgs, record = queued_logon(host, queue, lambda bgs: bgs.logon(credential, device, version))
    try:
        # Before anything is saved, so one account's session is never renewed as another's.
        if not login.claim(first(record, 1)):
            raise CredentialRejected('The saved session signed in to another Battle.net account')
        # Keep a distinct, unused SSO ticket for the next broker connection.
        # LogonRecord.login_ticket can echo the already consumed input.
        login.save_session(bgs.generate_auth_token(WOW_TITLE))
        log('unused_broker_credential_saved_protected')
        token = bgs.generate_auth_token(WOW_TITLE)
        log('wow_auth_token_generated', 'bytes', len(token))
        login.save_game_token(token)
        log('game_credential_saved_protected')
        login.save_battle_tag(logon_battle_tag(record))
        login.save_accounts(first(record, 1), bgs.game_accounts(record))
    finally:
        bgs.socket.close()
