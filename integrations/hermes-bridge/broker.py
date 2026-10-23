"""Profile-local multi-process ownership. Descriptors are fences, never grants.

SQLite persists work; kernel locks prove process liveness without lease expiry.
Only the elected ingress binds HTTP. Each host claims only its registered origins.
No IPC code execution, fallback destination, timeout takeover or enqueue replay.
"""
import fcntl
import json
import os
from pathlib import Path
import threading
import uuid

try:
    from .bridge import Dispatcher
except ImportError:
    from bridge import Dispatcher


def descriptor(identity):
    try:
        token = identity['conversation_id']
        if not token.startswith('hermes-origin-v1:'):
            return None
        data = json.loads(token[len('hermes-origin-v1:'):])
        if (set(data) != {'host', 'home', 'key', 'anchor', 'generation'}
                or any(not isinstance(v, str) or not v for v in data.values())
                or data['key'] != identity['session_key']):
            return None
        return data
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


class OwnerBroker:
    def __init__(self, store, directory, profile, resolve_origin, *, home=None):
        self.store, self.directory, self.profile = store, Path(directory), profile
        self.home = str(Path(home).resolve()) if home is not None else None
        self.resolve_origin = resolve_origin
        self.observed_sessions = {}  # canonical token -> actual host hook session
        self.owner_id = uuid.uuid4().hex  # process/plugin generation, NOT route generation
        self.closed = False
        self.mutex = threading.RLock()
        self.legacy_file = open(self.directory / 'owner.lock', 'a')
        os.chmod(self.legacy_file.name, 0o600)
        try:
            # New brokers share this migration fence; old exclusive workers
            # must unload before any new broker may start dispatch/recovery.
            fcntl.flock(self.legacy_file, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BaseException:
            self.legacy_file.close()
            raise
        self.file = None
        try:
            self.file = self._lock_file(self.owner_id)
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with store.lock, store.db:
                store.db.execute('CREATE TABLE IF NOT EXISTS delivery_owners (id TEXT PRIMARY KEY, profile TEXT NOT NULL)')
                store.db.execute('CREATE TABLE IF NOT EXISTS origin_owners (token TEXT NOT NULL, owner TEXT NOT NULL, PRIMARY KEY(token, owner))')
                store.db.execute('CREATE TABLE IF NOT EXISTS dispatch_claims (request_id TEXT PRIMARY KEY, owner TEXT NOT NULL)')
                store.db.execute('CREATE TABLE IF NOT EXISTS origin_session_hints (token TEXT PRIMARY KEY, session_id TEXT NOT NULL)')
                store.db.execute('INSERT INTO delivery_owners VALUES (?, ?)', (self.owner_id, profile))
        except BaseException:
            if self.file is not None:
                self.file.close()
            self.legacy_file.close()
            raise

    def _lock_file(self, owner):
        # IDs come from our private DB and are validated before using as paths.
        if len(owner) != 32 or any(c not in '0123456789abcdef' for c in owner):
            raise ValueError('invalid owner generation')
        fd = os.open(self.directory / ('owner-' + owner + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        return os.fdopen(fd, 'a')

    def _valid(self, identity):
        data = descriptor(identity)
        if (not data or identity.get('profile') != self.profile
                or (self.home is not None and data['home'] != self.home)):
            return False
        expected = {k: identity[k] for k in ('profile', 'session_key', 'conversation_id')}
        with self.store.lock:
            hint = self.store.db.execute('SELECT session_id FROM origin_session_hints WHERE token=?',
                                         (identity['conversation_id'],)).fetchone()
        sessions = {data['anchor'], self.observed_sessions.get(identity['conversation_id']),
                    hint[0] if hint else None} - {None}
        for session in sessions:
            try:
                if self.resolve_origin(session) == expected:
                    return True
            except Exception:
                pass
        return False

    def register_origin(self, identity, *, session_id=None):
        with self.mutex:
            if self.closed:
                return False
            if session_id is not None:
                # Called only with host hook/tool metadata, never a model argument.
                try:
                    if self.resolve_origin(session_id) != identity:
                        return False
                except Exception:
                    return False
                self.observed_sessions[identity['conversation_id']] = session_id
            if not self._valid(identity):
                return False
            with self.store.lock, self.store.db:
                if session_id is not None:
                    self.store.db.execute('INSERT OR REPLACE INTO origin_session_hints VALUES (?, ?)',
                                          (identity['conversation_id'], session_id))
                self.store.db.execute('INSERT OR IGNORE INTO origin_owners VALUES (?, ?)',
                                      (identity['conversation_id'], self.owner_id))
            return True

    def refresh(self):
        """Recover only descriptors the actual host still recognises exactly."""
        self.reap()
        with self.mutex:
            with self.store.lock:
                tokens = self.store.db.execute('SELECT token FROM origin_owners WHERE owner=?',
                                               (self.owner_id,)).fetchall()
            for (token,) in tokens:
                try:
                    data = json.loads(token[len('hermes-origin-v1:'):])
                    identity = dict(profile=self.profile, session_key=data['key'], conversation_id=token)
                except (ValueError, KeyError, TypeError):
                    identity = {}
                if not self._valid(identity):
                    with self.store.lock, self.store.db:
                        self.store.db.execute('DELETE FROM origin_owners WHERE token=? AND owner=?',
                                              (token, self.owner_id))
        with self.store.lock:
            rows = self.store.db.execute('SELECT data FROM mappings').fetchall()
        for row in rows:
            self.register_origin(json.loads(row[0]))

    def reap(self):
        # Hold each dead owner's kernel lock through cleanup: never treat a
        # slow live dispatcher as crashed. No timestamp/lease based adoption.
        with self.mutex, self.store.lock, self.store.db:
            self.store.db.execute("UPDATE inbox SET state='uncertain' WHERE state='dispatching' AND request_id NOT IN (SELECT request_id FROM dispatch_claims)")
            owners = self.store.db.execute('SELECT id FROM delivery_owners').fetchall()
            for (owner,) in owners:
                if owner == self.owner_id:
                    continue
                file = self._lock_file(owner)
                try:
                    try:
                        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    self._retire(owner)
                finally:
                    file.close()

    def _retire(self, owner):
        self.store.db.execute("UPDATE inbox SET state='uncertain' WHERE state='dispatching' AND request_id IN (SELECT request_id FROM dispatch_claims WHERE owner=?)", (owner,))
        self.store.db.execute('DELETE FROM origin_owners WHERE owner=?', (owner,))
        self.store.db.execute('DELETE FROM delivery_owners WHERE id=?', (owner,))
        # Claim records deliberately survive; an accepted/uncertain event is never replayed.

    def claim(self, event, mapping):
        with self.mutex:
            if self.closed or not self._valid(mapping):
                return False
            with self.store.lock, self.store.db:
                self.store.db.execute('BEGIN IMMEDIATE')
                owners = self.store.db.execute('SELECT owner FROM origin_owners WHERE token=?',
                                               (mapping['conversation_id'],)).fetchall()
                if owners != [(self.owner_id,)]:
                    return False  # missing or ambiguous ownership: no winner guessed
                changed = self.store.db.execute("UPDATE inbox SET state='dispatching' WHERE request_id=? AND state='blocked_host_identity'", (event['request_id'],)).rowcount
                if changed:
                    self.store.db.execute('INSERT INTO dispatch_claims VALUES (?, ?)',
                                          (event['request_id'], self.owner_id))
                return bool(changed)

    def dispatcher(self, inject):
        return Dispatcher(self.store, self.profile, inject, broker=self)

    def close(self):
        with self.mutex:
            if self.closed:
                return
            try:
                with self.store.lock, self.store.db:
                    self._retire(self.owner_id)
            finally:
                self.closed = True
                self.file.close()
                self.legacy_file.close()


class IngressOwner:
    """Cooperative HTTP election; never attempts binding while another owner lives."""
    def __init__(self, broker, secret, *, port, bind='127.0.0.1'):
        import hashlib
        try:
            from .bridge import validate_bind_address
        except ImportError:
            from bridge import validate_bind_address
        validate_bind_address(bind)
        if not isinstance(port, int) or not 0 <= port <= 65535 or len(secret) < 32:
            raise ValueError('invalid ingress settings')
        self.broker, self.secret, self.port, self.bind = broker, secret, port, bind
        self.listener, self.file = None, None
        self.closed = False
        broker.reap()
        config = json.dumps([bind, port, hashlib.sha256(secret).hexdigest()])
        with broker.store.lock, broker.store.db:
            broker.store.db.execute('BEGIN IMMEDIATE')
            broker.store.db.execute('CREATE TABLE IF NOT EXISTS ingress_settings (id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL)')
            row = broker.store.db.execute('SELECT config FROM ingress_settings WHERE id=1').fetchone()
            owners = broker.store.db.execute('SELECT COUNT(*) FROM delivery_owners').fetchone()[0]
            if row and row[0] != config and owners != 1:
                raise ValueError('active profile ingress configuration mismatch')
            broker.store.db.execute('INSERT OR REPLACE INTO ingress_settings VALUES (1, ?)', (config,))

    def tick(self):
        if self.closed:
            return False
        if self.listener is not None:
            return True
        file = open(self.broker.directory / 'ingress.lock', 'a')
        os.chmod(file.name, 0o600)
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file.close()
            return False
        try:
            try:
                from .bridge import Inbox, Listener
            except ImportError:
                from bridge import Inbox, Listener
            self.listener = Listener(Inbox(self.broker.store, self.secret), self.port, self.bind)
            self.file = file
            return True
        except OSError:
            # Another (non-cooperating) listener may hold the port. Never steal
            # it or stop delivery owners; retry election on a later worker tick.
            file.close()
            return False
        except BaseException:
            file.close()
            raise

    def close(self):
        if self.closed:
            return
        if self.listener is not None:
            self.listener.close()  # socket closed BEFORE election lock release
            self.listener = None
        if self.file is not None:
            self.file.close()
            self.file = None
        self.closed = True


class BridgeRuntime:
    """Enabled lifecycle service; entrypoint retains disabled/config/secret gates."""
    def __init__(self, directory, profile, home, resolve_origin, inject, secret, *,
                 port, bind='127.0.0.1', interval=1):
        try:
            from .bridge import Store, IdentityCapture
        except ImportError:
            from bridge import Store, IdentityCapture
        self.stop = threading.Event()
        self.closed = False
        self.worker = None
        self.store, self.owner, self.ingress = None, None, None
        try:
            self.store = Store(Path(directory) / 'bridge.sqlite3')
            self.owner = OwnerBroker(self.store, directory, profile, resolve_origin, home=home)
            self.capture = IdentityCapture(resolve_origin, profile)
            self.ingress = IngressOwner(self.owner, secret, port=port, bind=bind)
            self.dispatcher = self.owner.dispatcher(inject)
            self.dispatcher.recover()
            self.ingress.tick()
            def run():
                while not self.stop.wait(interval):
                    try:
                        self.ingress.tick()
                        self.dispatcher.drain()
                    except Exception:
                        # No payload/secret logging; durable states retain ambiguity.
                        pass
            self.worker = threading.Thread(target=run, name='pandacode-bridge-owner', daemon=True)
            self.worker.start()
        except BaseException:
            self.close()
            raise

    def observe(self, *, session_id=None, **metadata):
        self.capture.observe(session_id=session_id, **metadata)
        identity = self.capture.resolve(session_id)
        if identity is not None:
            self.owner.register_origin(identity, session_id=session_id)

    def identity(self, session_id):
        identity = self.capture.resolve(session_id)
        if identity is None or not self.owner.register_origin(identity, session_id=session_id):
            raise ValueError('host_origin_capability_unavailable')
        return identity

    def close(self):
        if self.closed:
            return
        self.stop.set()
        if self.worker is not None:
            self.worker.join()  # never close DB during a possibly accepted handoff
        if self.ingress is not None:
            self.ingress.close()
        if self.owner is not None:
            self.owner.close()
        if self.store is not None:
            self.store.close()
        self.closed = True
