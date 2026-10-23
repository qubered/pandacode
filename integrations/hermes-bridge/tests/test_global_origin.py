"""New origin tests, independent of the approval-pending entrypoint slice."""
import unittest
import bridge


class GlobalOriginTests(unittest.TestCase):
    def test_host_owned_capability_not_platform_or_hook_target_selects_origin(self):
        origins = {name: dict(profile='work', session_key='route:' + name,
                             conversation_id='hermes-origin-v1:' + name)
                   for name in ('photon-dm', 'group-thread', 'desktop', 'tui', 'cli', 'future-platform')}
        capture = bridge.IdentityCapture(origins.get, 'work')
        for name, identity in origins.items():
            capture.observe(session_id=name, platform='not-an-allowlist', session_key='attacker')
            self.assertEqual(capture.resolve(name), identity)
        capture.observe(session_id='unowned-child', parent_session_id='photon-dm')
        self.assertIsNone(capture.resolve('unowned-child'))
        origins['wrong-profile'] = dict(profile='other', session_key='route', conversation_id='hermes-origin-v1:x')
        capture.observe(session_id='wrong-profile')
        self.assertIsNone(capture.resolve('wrong-profile'))
        self.assertIsNone(capture.resolve('not-observed'))


if __name__ == '__main__':
    unittest.main()
