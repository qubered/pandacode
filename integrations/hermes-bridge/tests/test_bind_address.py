"""Independent bind-address configuration checks (no listener opened)."""
import unittest
import bridge


class BindAddressTests(unittest.TestCase):
    def test_only_loopback_or_explicit_tailnet_ip_is_allowed(self):
        self.assertTrue(hasattr(bridge, 'validate_bind_address'), 'bind validator missing')
        for address in ('127.0.0.1', '100.64.1.2', 'fd7a:115c:a1e0::12'):
            self.assertEqual(bridge.validate_bind_address(address), address)
        for address in ('0.0.0.0', '::', '192.168.1.1', '8.8.8.8', 'example.com', '100.128.1.2'):
            with self.assertRaises(ValueError):
                bridge.validate_bind_address(address)
