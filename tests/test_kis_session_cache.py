from tempfile import TemporaryDirectory
from unittest.mock import patch,Mock
import unittest
from industry_flow import Client

class KisSessionCacheTests(unittest.TestCase):
    def test_authentication_reused_after_python_process_state_is_reset(self):
        with TemporaryDirectory() as folder,patch.dict('os.environ',dict(KIS_APP_KEY='test-key',KIS_APP_SECRET='test-secret',RUNNER_TEMP=folder)),patch('industry_flow.requests.post') as post:
            prior=dict(Client._sessions);Client._sessions.clear()
            try:
                post.return_value=Mock(status_code=200)
                post.return_value.json.return_value=dict(access_token='test-access-token-never-real')
                first=Client();Client._sessions.clear();second=Client()
                self.assertEqual(first.headers,second.headers)
                self.assertEqual(post.call_count,1)
            finally:Client._sessions.clear();Client._sessions.update(prior)

if __name__=='__main__':unittest.main()
