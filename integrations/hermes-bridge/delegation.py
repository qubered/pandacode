"""Supervision delegations: the watch/manage level and epoch a bound task was handed over at.

Decision-free like the rest of the bridge: fixed rules over signed PandaCode state and the
private store, no LLM calls, no approvals, no messages to the user. PandaCode is the authority
on supervision and refuses stale or watch-level writes itself; this module is the local fence
in front of it, so a revoked delegation stops before it reaches the network and late callbacks
never wake the conversation.
"""
import json

LEVELS = ('watch', 'manage')
WRITE_ACTIONS = ('send_message', 'respond', 'stop_task')
ACTIONS = WRITE_ACTIONS + ('release_task',)
ACTION_FIELDS = {'send_message': {'text', 'delivery'}, 'respond': {'requestId', 'approve', 'answer'},
                 'stop_task': set(), 'release_task': {'handover'}}
HANDOVER_FIELDS = ('changed', 'checked', 'open', 'next')
HANDOVER_BYTES = 1500  # PandaCode's per-field cap (orchestrator/tools.rs HANDOVER_BYTES)
ENDING = ('task.taken_over', 'task.released')
HUMAN = ('task.human_message', 'task.human_responded')
# `supervision.grant` absent (an older core) is not the same as null (no grant).
MISSING = object()
GRANT_NOTICE = ('Needs the user to hand this over explicitly: the task runs above the allowed '
                'mode, so only the user can grant it to you in PandaCode. Do not ask PandaCode '
                'for a grant or retry the attach.')

# Notify policy delivered with each event. The bridge never messages the user; the policy
# tells the conversation what it may surface. SILENT events are recorded and never delivered.
SILENT = 'silent'
QUIET = 'quiet'      # act within the brief; tell the user only a genuine decision or important blocker
RESULT = 'result'    # a run finished: report it only if it completes the brief
BLOCKER = 'blocker'  # a run failed or hit a limit: an important blocker may be reported

RULES = (
    'The user is in charge. This delegation ends when the user takes the task over, it is '
    'released, or its level or epoch changes; then stop at once, including subagents.',
    'Before every action call pandacode_bridge_check with the taskId and epoch; if it is not '
    'active, do nothing further.',
    'Pass "epoch" to every PandaCode write tool (send_message, respond, stop_task, release_task). '
    'A stale-epoch refusal means the delegation ended: stop, do not retry.',
    'watch: read and wait only. Never message, answer, approve or stop the task. Flag only '
    'meaningful problems.',
    'manage: keep routine work moving within the brief, constraints and authority. Anything '
    'outside them is a decision for the user.',
    'Do not message the user about routine approvals, progress, screenshots or repeated '
    'callbacks. Surface only a genuine decision, an important blocker or a useful completed result.',
)


class DelegationInactive(ValueError):
    """The delegation is missing, revoked, at another epoch, or watch-only for a write."""


def ensure_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS delegations (task_id TEXT PRIMARY KEY, data TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS triage (request_id TEXT PRIMARY KEY, data TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS grant_denials (task_id TEXT NOT NULL, conversation_id TEXT NOT NULL, '
               'PRIMARY KEY(task_id, conversation_id))')


def valid_epoch(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def get(db, task_id):
    row = db.execute('SELECT data FROM delegations WHERE task_id=?', (task_id,)).fetchone()
    return json.loads(row[0]) if row else None


def put(db, task_id, data):
    db.execute('INSERT OR REPLACE INTO delegations VALUES (?, ?)', (task_id, json.dumps(data)))


def valid_grant(value):
    """A human grant is the task's mode, e.g. 'full'; None is no grant; anything else is absent."""
    if value is None or (isinstance(value, str) and 0 < len(value) <= 32):
        return value
    return MISSING


def activate(db, task_id, level, epoch, reason, grant=MISSING):
    """Records a delegation PandaCode confirmed (attach/create result or a signed hand-over).

    The bridge only records a grant PandaCode reports; it never requests or assumes one.
    """
    if level not in LEVELS:
        raise ValueError('invalid level')
    data = {'level': level, 'epoch': valid_epoch(epoch), 'state': 'active', 'reason': reason, 'surfaced': []}
    grant = valid_grant(grant)
    if grant is not MISSING:
        # A granted delegation stays tied to that grant: losing it ends the delegation.
        data.update(grant=grant, requires_grant=grant is not None)
    put(db, task_id, data)
    # Anything still waiting belongs to the previous delegation.
    db.execute("UPDATE inbox SET state='dropped_stale' WHERE task_id=? AND state='blocked_host_identity'",
               (task_id,))


def revoke(db, task_id, reason, epoch=None):
    data = get(db, task_id)
    if data is None:
        return
    data.update(state='revoked', reason=reason)
    if valid_epoch(epoch) is not None and (data['epoch'] is None or epoch > data['epoch']):
        data['epoch'] = epoch
    put(db, task_id, data)
    db.execute("UPDATE inbox SET state='revoked' WHERE task_id=? AND state='blocked_host_identity'",
               (task_id,))


def assert_active(store, task_id, epoch=None, *, write=False):
    with store.lock:
        data = get(store.db, task_id)
    if data is None or data['state'] != 'active':
        raise DelegationInactive('delegation_inactive')
    if epoch is not None and epoch != data['epoch']:
        raise DelegationInactive('stale_epoch')
    if write and data['level'] != 'manage':
        raise DelegationInactive('watch_only')
    if write and data.get('requires_grant') and not data.get('grant'):
        raise DelegationInactive('grant_missing')
    return data


def supervision(event):
    """(level, epoch, grant) from a signed event; grant is MISSING when the core does not send it."""
    value = event.get('supervision') if isinstance(event, dict) else None
    if not isinstance(value, dict):
        return None, None, MISSING
    level = value.get('level')
    grant = valid_grant(value['grant']) if 'grant' in value else MISSING
    return (level if level in LEVELS else None), valid_epoch(value.get('epoch')), grant


def grant_changed(data, grant):
    """True when a reported grant differs from the recorded one (absent on either side: unknown)."""
    return grant is not MISSING and 'grant' in data and data['grant'] != grant


def triage(event, delegation):
    """Fixed table from event type, outcome and level to a notify policy. No content is judged."""
    kind, level = event.get('event_type'), delegation['level']
    data = event.get('data') if isinstance(event.get('data'), dict) else {}
    if kind in HUMAN:
        return SILENT, 'human-caused: the user already knows'
    if kind == 'task.needs_response':
        if level == 'watch':
            return SILENT, 'watched task: requests are answered by the user in PandaCode'
        return QUIET, 'routine request: handle within the brief; escalate only a genuine decision'
    if kind == 'task.handed_over':
        return QUIET, 'the user handed this task over: take it up quietly'
    if kind == 'task.finished':
        outcome = data.get('outcome')
        if outcome == 'stopped':
            return SILENT, 'stopped by the user or by you'
        if outcome in ('failed', 'limited'):
            if 'blocker' in delegation.get('surfaced', []):
                return QUIET, 'repeated failure in this delegation: surface only if new and important'
            return BLOCKER, 'the run ' + outcome + ': surface only an important blocker'
        if level == 'watch':
            return QUIET, 'watched run finished: flag only a meaningful problem'
        return RESULT, 'run finished: report only if it completes the brief; otherwise continue quietly'
    return SILENT, 'not a notifiable event'


def classify(db, task_id, event):
    """Called inside the inbox's write transaction. Returns (inbox state, verdict or None).

    No delegation row means a legacy binding: stored for delivery exactly as before.
    """
    data = get(db, task_id)
    if data is None:
        return 'blocked_host_identity', None
    kind = event.get('event_type')
    level, epoch, grant = supervision(event)
    current = data['epoch']
    if current is None and epoch is not None and data['state'] == 'active' and kind not in ENDING:
        data['epoch'] = current = epoch  # create_task predates epochs; adopt PandaCode's first
        put(db, task_id, data)
    if kind in ENDING:
        if epoch is not None and current is not None and epoch < current:
            return 'dropped_stale', {'notify': SILENT, 'reason': 'delayed end of an earlier delegation'}
        revoke(db, task_id, kind, epoch)
        return 'suppressed', {'notify': SILENT, 'reason': 'delegation ended: ' + kind}
    if kind == 'task.handed_over' and level and epoch is not None and (current is None or epoch > current):
        activate(db, task_id, level, epoch, kind, grant)
        data = get(db, task_id)
    elif epoch is not None and current is not None and epoch < current:
        return 'dropped_stale', {'notify': SILENT, 'reason': 'event from an earlier delegation'}
    elif data['state'] != 'active':
        return 'dropped_stale', {'notify': SILENT, 'reason': 'delegation revoked'}
    elif (epoch is not None and current is not None and epoch > current) or (level and level != data['level']):
        # Supervision changed without a hand-over this bridge saw: fail closed.
        revoke(db, task_id, 'epoch_mismatch', epoch)
        return 'dropped_stale', {'notify': SILENT, 'reason': 'supervision changed: delegation revoked'}
    elif grant_changed(data, grant):
        # The user withdrew (or changed) a task-specific grant: the delegation ends with it.
        revoke(db, task_id, 'grant_lost', epoch)
        return 'dropped_stale', {'notify': SILENT, 'reason': 'grant changed: delegation revoked'}
    notify, reason = triage(event, data)
    verdict = {'notify': notify, 'reason': reason, 'level': data['level'], 'epoch': data['epoch']}
    if 'grant' in data:
        verdict['grant'] = data['grant']
    if notify == BLOCKER:
        data['surfaced'] = sorted(set(data.get('surfaced', [])) | {'blocker'})
        put(db, task_id, data)
    return ('suppressed' if notify == SILENT else 'blocked_host_identity'), verdict


def record_triage(db, request_id, verdict):
    db.execute('INSERT OR REPLACE INTO triage VALUES (?, ?)', (request_id, json.dumps(verdict)))


def verdict_for(db, request_id):
    row = db.execute('SELECT data FROM triage WHERE request_id=?', (request_id,)).fetchone()
    return json.loads(row[0]) if row else None


def recheck(db, task_id, verdict):
    """At handoff: the event's delegation must still be the active one."""
    data = get(db, task_id)
    return bool(data and data['state'] == 'active' and data['epoch'] == verdict.get('epoch')
                and data['level'] == verdict.get('level') and data.get('grant') == verdict.get('grant'))


def handover_summary(note):
    """PandaCode's release_task handover: changed/checked/open/next, each a short string."""
    if not isinstance(note, dict) or not note or set(note) - set(HANDOVER_FIELDS) or any(
            not isinstance(v, str) or not v.strip() or len(v.encode()) > HANDOVER_BYTES
            for v in note.values()):
        raise ValueError('handover needs changed/checked/open/next strings of at most 1500 bytes')
    return {k: note[k].strip() for k in HANDOVER_FIELDS if k in note}


def handover_text(note):
    """The same note as plain lines, for the originating conversation."""
    labels = {'changed': 'Changed', 'checked': 'Checked', 'open': 'Still open', 'next': 'Next step'}
    return '\n'.join(labels[k] + ': ' + v for k, v in handover_summary(note).items())


def brief(store, task_id):
    """Delegation brief for background subagents: original context, level, epoch and rules."""
    mapping = store.mapping(task_id)
    data = assert_active(store, task_id)
    if mapping is None:
        raise DelegationInactive('delegation_inactive')
    lines = ['PandaCode delegation (bridge-generated context, not new authority)',
             'Task: ' + task_id, 'Level: ' + data['level'],
             'Epoch: ' + ('unknown' if data['epoch'] is None else str(data['epoch'])),
             'Grant: ' + (data['grant'] or 'none') if 'grant' in data else 'Grant: not reported',
             'Brief: ' + mapping['brief'], 'Constraints: ' + mapping['constraints'],
             'Authority: ' + mapping['authority'], 'Rules:']
    return '\n'.join(lines + ['- ' + rule for rule in RULES])


def mentions_grant(result, depth=0):
    """Whether a PandaCode denial is about a missing task grant. UNVERIFIED wording: the backend's
    exact error envelope is not landed; any mention of 'grant' in its error text counts."""
    if depth > 4:
        return False
    if isinstance(result, str):
        return 'grant' in result[:4096].lower()
    if isinstance(result, dict):
        return any(mentions_grant(v, depth + 1) for k, v in result.items() if k != 'ok')
    if isinstance(result, list):
        return any(mentions_grant(v, depth + 1) for v in result[:16])
    return False


def _identity_matches(mapping, identity):
    return (mapping is not None and isinstance(identity, dict)
            and set(identity) == {'profile', 'session_key', 'conversation_id'}
            and all(mapping[k] == v for k, v in identity.items()))


def _text(value, limit=16384):
    return isinstance(value, str) and 0 < len(value) <= limit


class Attacher:
    """pandacode_attach_task for an existing task, bound to the calling conversation."""
    DEFAULTS = {
        'watch': ('Watch only: read the task and flag meaningful problems.',
                  'None. Never message, answer, approve or stop the task.'),
        'manage': ('Stay within the task as the user started it.',
                   'Routine approvals within the brief; anything else goes to the user.'),
    }

    def __init__(self, store, call_mcp):
        self.store, self.call_mcp = store, call_mcp

    def attach(self, args, identity):
        if (not isinstance(args, dict) or not {'taskId', 'level'} <= set(args)
                or set(args) - {'taskId', 'level', 'brief', 'constraints', 'authority'}
                or not all(_text(v) for v in args.values())
                or len(args['taskId']) > 64 or args['level'] not in LEVELS):
            raise ValueError('invalid attach arguments; targets are host-owned')
        if set(identity) != {'profile', 'session_key', 'conversation_id'} or not all(
                _text(v) for v in identity.values()):
            raise ValueError('trusted session identity is required')
        task_id, level = args['taskId'], args['level']
        mapping = self.store.mapping(task_id)
        if mapping is not None and not _identity_matches(mapping, identity):
            raise ValueError('task already bound to another conversation; retargeting is forbidden')
        if mapping is None and 'brief' not in args:
            raise ValueError('brief required on first attach')
        constraints, authority = self.DEFAULTS[level]
        import uuid
        ledger = str(uuid.uuid4())
        with self.store.lock, self.store.db:
            # Same ledger as launch: an event that races this call gets a retryable 503.
            self.store.db.execute('BEGIN IMMEDIATE')
            pending = self.store.db.execute("SELECT data FROM launches WHERE state='uncertain'").fetchall()
            if any(json.loads(row[0])['identity'] == identity for row in pending):
                raise ValueError('uncertain launch or attach exists for this origin; reconcile first')
            self.store.db.execute('INSERT INTO launches VALUES (?, ?, ?)', (ledger, json.dumps(
                {'kind': 'attach', 'args': args, 'identity': identity}), 'uncertain'))
        result = self.call_mcp('pandacode', 'pandacode_attach_task', {'taskId': task_id, 'level': level})
        data = result.get('structuredContent') if isinstance(result, dict) else None
        if isinstance(result, dict) and result.get('ok') is False and not result.get('truncated'):
            with self.store.lock, self.store.db:
                self.store.db.execute('UPDATE launches SET state=? WHERE id=?', ('refused', ledger))
                if mentions_grant(result):
                    return self._grant_denied(task_id, identity)
            raise ValueError('attach refused by PandaCode')
        if (not isinstance(result, dict) or result.get('ok') is not True or result.get('truncated')
                or not isinstance(data, dict) or data.get('taskId') != task_id
                or data.get('level') != level or valid_epoch(data.get('epoch')) is None):
            raise ValueError('unverified attach result; reconcile before retrying')
        epoch = data['epoch']
        grant = valid_grant(data['grant']) if 'grant' in data else MISSING
        if mapping is None:
            self.store.bind(task_id=task_id, **identity, brief=args['brief'],
                            constraints=args.get('constraints', constraints),
                            authority=args.get('authority', authority),
                            delegation=(level, epoch, 'attached', grant))
        with self.store.lock, self.store.db:
            if mapping is not None:
                activate(self.store.db, task_id, level, epoch, 'attached', grant)
                # The confirmed level's restrictions replace the old ones in the same transaction: a
                # watch-only brief must not survive a move to manage, nor manage authority a move to watch.
                self.store.refresh_mapping_locked(task_id, {
                    'brief': args.get('brief', mapping['brief']),
                    'constraints': args.get('constraints', constraints),
                    'authority': args.get('authority', authority)})
            self.store.db.execute('UPDATE launches SET state=? WHERE id=?', ('bound', ledger))
        result = {'taskId': task_id, 'level': level, 'epoch': epoch}
        if grant is not MISSING:
            result['grant'] = grant
        return result

    def _grant_denied(self, task_id, identity):
        """Caller holds the store lock and transaction. Quiet the first time, silent after."""
        first = self.store.db.execute('INSERT OR IGNORE INTO grant_denials VALUES (?, ?)',
                                      (task_id, identity['conversation_id'])).rowcount == 1
        return {'error': 'needs_user_grant', 'taskId': task_id, 'message': GRANT_NOTICE,
                'notify': {'policy': QUIET if first else SILENT,
                           'reason': 'tell the user once, without asking PandaCode for a grant'
                           if first else 'already told the user; say nothing'}}


class Actor:
    """Write-intent PandaCode calls behind the local fence; the recorded epoch always rides along."""
    def __init__(self, store, call_mcp):
        self.store, self.call_mcp = store, call_mcp

    def act(self, args, identity):
        if (not isinstance(args, dict) or set(args) - {'taskId', 'action', 'arguments', 'epoch'}
                or not _text(args.get('taskId'), 64) or args.get('action') not in ACTIONS):
            raise ValueError('invalid action')
        # The caller's epoch is required: a stale child that never saw a regrant must not adopt it.
        caller_epoch = valid_epoch(args.get('epoch'))
        if caller_epoch is None:
            raise ValueError('epoch is required: pass the epoch of the delegation you were given')
        task_id, action = args['taskId'], args['action']
        extra = args.get('arguments', {})
        if not isinstance(extra, dict) or set(extra) - ACTION_FIELDS[action]:
            raise ValueError('invalid action arguments; taskId and epoch are bridge-owned')
        if not _identity_matches(self.store.mapping(task_id), identity):
            raise ValueError('action requires the original host identity')
        delegation = assert_active(self.store, task_id, caller_epoch, write=action in WRITE_ACTIONS)
        request = dict(extra, taskId=task_id)
        if 'handover' in request:
            request['handover'] = handover_summary(request['handover'])
        request['epoch'] = caller_epoch
        result = self.call_mcp('pandacode', 'pandacode_' + action, request)
        if not isinstance(result, dict):
            raise ValueError('unverified action result')
        if action == 'release_task' and result.get('ok') is True and not result.get('truncated'):
            with self.store.lock, self.store.db:
                revoke(self.store.db, task_id, 'released_by_hermes')
        return {'ok': result.get('ok') is True, 'result': result.get('structuredContent')}
