"""PandaCode -> exact host-owned Hermes origin. Disabled on import and by default.

Enabled lifecycle and permission integration tests: tests/test_plugin_enabled.py.
Runtime activation and external delivery require separate rollout receipts.
"""
import json
import os
from pathlib import Path

from . import delegation
from .bridge import Launcher, supports_expected_identity
from .broker import BridgeRuntime


def register(ctx):
    runtime = {'error': 'capture_only_not_enabled', 'service': None, 'store': None}

    def capture(**metadata):
        if runtime['service'] is not None:
            runtime['service'].observe(**metadata)

    def identity(session_id):
        return runtime['service'].identity(session_id)

    def launch(args, *, session_id=None, **metadata):
        if runtime['error']:
            return json.dumps({'error': runtime['error']})
        try:
            task_id = Launcher(runtime['store'], ctx.call_mcp).launch(args, identity(session_id))
            return json.dumps({'taskId': task_id, 'mapping': 'bound'})
        except Exception:
            # Do not leak provider output/secrets or tell the caller to blindly replay.
            return json.dumps({'error': 'launch_failed_or_uncertain',
                               'action': 'Inspect local launch ledger and PandaCode tasks before any retry.'})

    def update_brief(args, *, session_id=None, **metadata):
        if runtime['error']:
            return json.dumps({'error': runtime['error']})
        try:
            if not isinstance(args, dict) or 'taskId' not in args:
                raise ValueError('taskId required')
            runtime['store'].update_brief(args['taskId'], identity(session_id),
                                         **{k: v for k, v in args.items() if k != 'taskId'})
            return json.dumps({'ok': True, 'scope': 'local_context_only'})
        except (ValueError, TypeError):
            return json.dumps({'error': 'brief_update_rejected'})

    def attach(args, *, session_id=None, **metadata):
        if runtime['error']:
            return json.dumps({'error': runtime['error']})
        try:
            return json.dumps(delegation.Attacher(runtime['store'], ctx.call_mcp)
                              .attach(args, identity(session_id)))
        except Exception:
            return json.dumps({'error': 'attach_failed_or_uncertain',
                               'action': 'Read the task with pandacode_read_task before any retry.'})

    def act(args, *, session_id=None, **metadata):
        if runtime['error']:
            return json.dumps({'error': runtime['error']})
        try:
            return json.dumps(delegation.Actor(runtime['store'], ctx.call_mcp).act(args, identity(session_id)))
        except delegation.DelegationInactive as refusal:
            # Local fence: nothing was sent to PandaCode.
            return json.dumps({'error': 'refused_locally', 'reason': str(refusal),
                               'action': 'The delegation is not active for this. Stop; do not retry.'})
        except (ValueError, TypeError):
            return json.dumps({'error': 'action_rejected'})
        except Exception:
            return json.dumps({'error': 'action_failed_or_uncertain',
                               'action': 'Read the task with pandacode_read_task before any retry.'})

    def check(args, **metadata):
        # Read-only and identity-free so background subagents can stop themselves.
        if runtime['error']:
            return json.dumps({'error': runtime['error']})
        try:
            if not isinstance(args, dict) or set(args) - {'taskId', 'epoch'} or 'taskId' not in args:
                raise ValueError('taskId required')
            epoch = args.get('epoch')
            if epoch is not None and delegation.valid_epoch(epoch) is None:
                raise ValueError('invalid epoch')
            data = delegation.assert_active(runtime['store'], args['taskId'], epoch)
            return json.dumps({'active': True, 'level': data['level'], 'epoch': data['epoch']})
        except delegation.DelegationInactive as refusal:
            return json.dumps({'active': False, 'reason': str(refusal), 'action': 'Stop now.'})
        except (ValueError, TypeError):
            return json.dumps({'active': False, 'reason': 'invalid_request', 'action': 'Stop now.'})

    def brief(args, *, session_id=None, **metadata):
        if runtime['error']:
            return json.dumps({'error': runtime['error']})
        try:
            task_id = args['taskId']
            mapping = runtime['store'].mapping(task_id)
            caller = identity(session_id)
            if mapping is None or any(mapping[k] != v for k, v in caller.items()):
                raise ValueError('original host identity required')
            return json.dumps({'brief': delegation.brief(runtime['store'], task_id)})
        except delegation.DelegationInactive as refusal:
            return json.dumps({'error': 'delegation_inactive', 'reason': str(refusal)})
        except (ValueError, TypeError, KeyError):
            return json.dumps({'error': 'brief_rejected'})

    properties = {name: {'type': 'string', 'minLength': 1, 'maxLength': 16384} for name in
                  ('projectId', 'prompt', 'provider', 'model', 'mode', 'constraints', 'authority', 'worktreeBranch')}
    properties['mode']['enum'] = ['plan', 'ask', 'auto']
    ctx.register_tool(name='pandacode_bridge_launch', toolset='pandacode_bridge',
                      schema={'description': 'Launch PandaCode bound to this exact host-owned conversation. '
                              'Never retry an uncertain launch without reconciliation.',
                              'parameters': {'type': 'object', 'properties': properties,
                                             'required': [k for k in properties if k != 'worktreeBranch'],
                                             'additionalProperties': False}}, handler=launch)
    ctx.register_hook('pre_llm_call', capture)

    if ctx.get_config('enabled', False) is not True:
        return
    # This flag acknowledges a REVIEWED HOST CONTRACT, not a substitute for host support.
    if (ctx.get_config('expected_identity_contract', '') != 'host-origin-v1'
            or not supports_expected_identity(ctx.inject_message)
            or not callable(getattr(ctx, 'resolve_origin', None))):
        runtime['error'] = 'host_expected_identity_unavailable'
        return
    secret = os.environ.get('PANDACODE_BRIDGE_WEBHOOK_SECRET', '')
    if len(secret.encode()) < 32:
        runtime['error'] = 'webhook_secret_unavailable'
        return

    resources = []
    def close():
        runtime['error'] = 'bridge_unloaded'
        runtime['service'] = None
        while resources:
            resources.pop()()
    ctx.on_unload(close)
    try:
        # Pin the host home/profile once; background threads never infer an ambient profile.
        from hermes_constants import get_hermes_home
        home, profile = Path(get_hermes_home()).resolve(), ctx.profile_name
        service = BridgeRuntime(home / 'plugin-state' / 'pandacode-bridge', profile, home,
                                ctx.resolve_origin, ctx.inject_message, secret.encode(),
                                port=int(ctx.get_config('port', 8743)),
                                bind=ctx.get_config('bind', '127.0.0.1'))
        resources.append(service.close)
        runtime['store'] = service.store
        runtime['service'] = service
        runtime['error'] = None
        ctx.register_tool(name='pandacode_bridge_update_brief', toolset='pandacode_bridge',
                          schema={'description': 'Update retained task context in its original conversation only. '
                                  'Does not send a PandaCode message or approve commands.',
                                  'parameters': {'type': 'object', 'properties': {
                                      'taskId': {'type': 'string'}, **{k: properties[k] if k in properties else
                                      {'type': 'string'} for k in ('brief', 'constraints', 'authority')}},
                                      'required': ['taskId'], 'additionalProperties': False}},
                          handler=update_brief)
        text = {'type': 'string', 'minLength': 1, 'maxLength': 16384}
        task = {'type': 'string', 'minLength': 1, 'maxLength': 64}
        ctx.register_tool(name='pandacode_bridge_attach', toolset='pandacode_bridge',
                          schema={'description': 'Supervise an existing PandaCode task for this exact conversation, '
                                  'at level watch (read and flag problems only) or manage (guide it and handle '
                                  'routine approvals within the brief). The user stays in charge and can take it '
                                  'over at any time. Never retry an uncertain attach without reading the task.',
                                  'parameters': {'type': 'object', 'properties': {
                                      'taskId': task, 'level': {'type': 'string', 'enum': list(delegation.LEVELS)},
                                      'brief': text, 'constraints': text, 'authority': text},
                                      'required': ['taskId', 'level'], 'additionalProperties': False}},
                          handler=attach)
        ctx.register_tool(name='pandacode_bridge_act', toolset='pandacode_bridge',
                          schema={'description': 'Message, answer, stop or release a task this conversation manages. '
                                  'Refused locally when the delegation is watch-only, taken over, released or '
                                  'stale; pass the epoch you were delegated at (from attach, launch or your brief); a stale one is refused locally with no PandaCode call. '
                                  'release_task takes a handover {changed, checked, open, next}.',
                                  'parameters': {'type': 'object', 'properties': {
                                      'taskId': task, 'action': {'type': 'string', 'enum': list(delegation.ACTIONS)},
                                      'epoch': {'type': 'integer', 'minimum': 0},
                                      'arguments': {'type': 'object'}},
                                      'required': ['taskId', 'action', 'epoch'], 'additionalProperties': False}},
                          handler=act)
        ctx.register_tool(name='pandacode_bridge_check', toolset='pandacode_bridge',
                          schema={'description': 'Whether a PandaCode delegation is still active at this epoch. '
                                  'Call before every action, including from subagents; stop when inactive.',
                                  'parameters': {'type': 'object', 'properties': {
                                      'taskId': task, 'epoch': {'type': 'integer', 'minimum': 0}},
                                      'required': ['taskId'], 'additionalProperties': False}},
                          handler=check)
        ctx.register_tool(name='pandacode_bridge_brief', toolset='pandacode_bridge',
                          schema={'description': 'The delegation brief to hand a background subagent: original '
                                  'brief, level, epoch, task ID and the rules it must follow.',
                                  'parameters': {'type': 'object', 'properties': {'taskId': task},
                                                 'required': ['taskId'], 'additionalProperties': False}},
                          handler=brief)
    except Exception:
        close()
        runtime['error'] = 'bridge_startup_failed'
