import unittest
import tempfile
import time
from pathlib import Path
from http.client import IncompleteRead
from unittest.mock import Mock, patch
from srv.oscquery import VRChatOSCQueryClient, OSCQueryAvatarSnapshot


class QueryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = VRChatOSCQueryClient(Mock(), lambda _: None)
        self.parameters = {'FULL_PATH': '/avatar/parameters', 'CONTENTS': {
            'P': {'FULL_PATH': '/avatar/parameters/OGB/Pen/Test/PenSelf', 'TYPE': 'f', 'ACCESS': 1, 'VALUE': [0.6]}}}

    def test_truncated_avatar_uses_split_nodes(self):
        with patch.object(self.client, '_read_json', side_effect=[IncompleteRead(b'{'), {'VALUE': ['avtr_a']}, self.parameters, {'VALUE': ['avtr_a']}]) as read:
            snapshot = self.client._fetch('127.0.0.1', 12345)
        self.assertEqual(snapshot.avatar_id, 'avtr_a')
        self.assertEqual(snapshot.parameter_values[0][1], 0.6)
        self.assertEqual(read.call_count, 4)

    def test_split_read_retries_changed_identity(self):
        self.client.split_endpoints.add('http://127.0.0.1:12345')
        with patch.object(self.client, '_read_json', side_effect=[{'VALUE': ['avtr_a']}, self.parameters, {'VALUE': ['avtr_b']}, {'VALUE': ['avtr_b']}, self.parameters, {'VALUE': ['avtr_b']}]):
            self.assertEqual(self.client._fetch('127.0.0.1', 12345).avatar_id, 'avtr_b')

    def test_hidden_identity_does_not_block_split_parameters(self):
        with patch.object(self.client, '_read_json', side_effect=[ValueError('bad json'), {}, self.parameters, {}]):
            snapshot = self.client._fetch('127.0.0.1', 12345)
        self.assertEqual(snapshot.avatar_id, '')
        self.assertTrue(snapshot.parameter_values)

    def test_cached_endpoint_skips_repeated_mdns_resolution(self):
        self.client.cached_endpoint = ('127.0.0.1', 12345)
        expected = OSCQueryAvatarSnapshot('', (), 'http://127.0.0.1:12345')
        with patch.object(self.client, '_fetch', return_value=expected):
            self.assertEqual(self.client._query_services(), expected)
        self.client.zeroconf.get_service_info.assert_not_called()

    def test_log_fallback_requires_vrchat_host_info(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / 'AppData/LocalLow/VRChat/VRChat'
            folder.mkdir(parents=True)
            (folder / 'output_log_test.txt').write_text('service of type OSCQuery on 12345\n')
            with patch.dict('os.environ', {'USERPROFILE': root}), patch.object(self.client, '_read_json', return_value={'NAME': 'VRChat-Client-test'}):
                self.assertEqual(self.client._endpoint_from_game_log(), ('127.0.0.1', 12345))
            with patch.dict('os.environ', {'USERPROFILE': root}), patch.object(self.client, '_read_json', return_value={'NAME': 'OtherApp'}):
                self.assertIsNone(self.client._endpoint_from_game_log())
