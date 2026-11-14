"""Secure kernel probe URLs must not downgrade or rewrite the route."""
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import smoke


class KernelTransportTests(unittest.TestCase):
    def test_transport_preserves_secure_origin_ipv6_and_route(self):
        for origin, expected in [('https://course.example:8443', 'wss://course.example:8443'),
                                 ('http://127.0.0.1:8000', 'ws://127.0.0.1:8000'),
                                 ('https://[::1]:8443', 'wss://[::1]:8443')]:
            route = '/user/alice/api/kernels/test/channels?session_id=synthetic'
            with self.subTest(origin=origin):
                self.assertEqual(smoke.websocket_url(origin + route), expected + route)

    def test_rejects_invalid_origin_without_exposing_url(self):
        for value in ['file:///private-canary', 'https:///private-canary',
                      'https://private-canary@course.example/path', 'https://course.example/#private-canary']:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '^kernel_websocket_url_invalid$'):
                smoke.websocket_url(value)

    def test_real_kernel_caller_uses_wss_without_retry_on_handshake_failure(self):
        session = Mock()
        session.prepare_request.return_value.headers = {'Cookie': 'synthetic-cookie'}
        response = Mock()
        response.json.return_value = {'id': 'synthetic-id'}
        failure = smoke.websocket.WebSocketBadStatusException('synthetic refusal', status_code=503)
        with patch.object(smoke, 'change', return_value=response) as change, \
             patch.object(smoke.websocket, 'create_connection', side_effect=failure) as connect:
            with self.assertRaises(smoke.websocket.WebSocketBadStatusException):
                smoke.kernel(session, 'https://course.example', 'alice', 'ir', "cat('CONTINUITY_OK\\n')")
        self.assertEqual(change.call_count, 1)  # Never repeat an uncertain kernel POST.
        connect.assert_called_once_with('wss://course.example/user/alice/api/kernels/synthetic-id/channels',
                                        cookie='synthetic-cookie', origin='https://course.example', timeout=30)


if __name__ == '__main__':
    unittest.main()
