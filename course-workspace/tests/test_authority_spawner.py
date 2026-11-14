from pathlib import Path
import sys
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hub'))
from authority_spawner import AuthoritySpawner, AuthorityError


class SpawnerTests(unittest.IsolatedAsyncioTestCase):
    def spawner(self):
        value = AuthoritySpawner(user=SimpleNamespace(name='alice'), worker_id='worker-one', course_id='course-main')
        value.get_env = lambda: {'JUPYTERHUB_USER': 'alice', 'JUPYTERHUB_API_TOKEN': 'scoped-token', 'DOCKER_HOST': 'must-not-forward', 'WORKSPACE_AUTHORITY_KEY': 'must-not-forward'}
        return value

    async def test_start_fresh_epoch_and_filters_nonlearner_environment(self):
        value = self.spawner()
        view = {'authorized': True, 'epoch': 2, 'worker_id': 'worker-one', 'course_id': 'course-main'}
        response = {'server_id': 'a' * 64, 'epoch': 2, 'address': '172.20.0.2', 'port': 8888}
        value.call = AsyncMock(side_effect=[view, response])
        self.assertEqual(await value.start(), ('172.20.0.2', 8888))
        payload = value.call.call_args_list[1].args[1]
        self.assertEqual(payload['expected_epoch'], 2)
        self.assertEqual(set(payload['environment']), {'JUPYTERHUB_USER', 'JUPYTERHUB_API_TOKEN'})
        self.assertEqual(value.get_state()['server_epoch'], 2)

    async def test_persisted_old_epoch_stop_does_not_refresh_or_adopt(self):
        value = self.spawner(); value.load_state({'server_id': 'a' * 64, 'server_epoch': 1})
        value.call = AsyncMock(return_value={'stopped': True})
        await value.stop()
        value.call.assert_awaited_once_with('hub.stop', {'subject': 'alice', 'server_id': 'a' * 64, 'epoch': 1})
        self.assertIsNone(value.server_id)

    async def test_unavailable_poll_is_not_running_and_keeps_identity_for_exact_stop(self):
        value = self.spawner(); value.load_state({'server_id': 'a' * 64, 'server_epoch': 1})
        value.call = AsyncMock(side_effect=ConnectionError())
        self.assertEqual(await value.poll(), 1)
        self.assertEqual(value.server_epoch, 1)

    async def test_poll_diagnostic_is_allowlisted_without_error_message_or_retry(self):
        for error, code in ((AuthorityError('authority_operation_refused'), 'authority_operation_refused'),
                            (RuntimeError('private-message'), 'other')):
            value = self.spawner(); value.load_state({'server_id': 'a' * 64, 'server_epoch': 1})
            value.call = AsyncMock(side_effect=error)
            value.log = Mock(spec=logging.Logger)
            self.assertEqual(await value.poll(), 1)
            value.call.assert_awaited_once()
            value.log.warning.assert_called_once_with('Authority notebook poll refused: %s', code)
            self.assertEqual(value.server_id, 'a' * 64)

    async def test_later_epoch_response_not_adopted(self):
        value = self.spawner()
        value.call = AsyncMock(side_effect=[{'authorized': True, 'epoch': 1, 'worker_id': 'worker-one', 'course_id': 'course-main'},
            {'server_id': 'a' * 64, 'epoch': 2, 'address': '172.20.0.2', 'port': 8888}])
        with self.assertRaises(ValueError): await value.start()
        self.assertIsNone(value.server_id)

    def test_invalid_cached_state_refused(self):
        value = self.spawner()
        for state in ({'server_id': 'a' * 64, 'server_epoch': True}, {'server_id': 'peer', 'server_epoch': 1}, {'server_id': 'a' * 64}):
            with self.assertRaises(ValueError): value.load_state(state)


if __name__ == '__main__': unittest.main()
