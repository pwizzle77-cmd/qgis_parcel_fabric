"""AI service checks with synthetic data and a loopback fake server; no paid API calls."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parcel_fabric.core import ai_service as AI
from parcel_fabric.core import deed_ai, ollama

try:
    from qgis.core import QgsApplication
    HAS_QGIS = True
except ImportError:
    HAS_QGIS = False


class Settings:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def value(self, key, default=None, type=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value


class ServiceTests(unittest.TestCase):
    def test_migrate_ollama_and_keep_provider_settings_separate(self):
        settings = Settings({'ParcelFabric/ollama_url': 'http://localhost:12345',
                             'ParcelFabric/ollama_ocr_model': 'existing-vision'})
        old = AI.load(settings)
        self.assertEqual((old['url'], old['ocr_model']), ('http://localhost:12345', 'existing-vision'))
        hosted = dict(provider='openai', url=AI.PROVIDERS['openai'][1],
                      ocr_model='vision-id', llm_model='chat-id', authcfg='abc1234')
        AI.save(settings, hosted)
        self.assertEqual(AI.load(settings), hosted)
        self.assertEqual(AI.load(settings, 'ollama'), old)
        self.assertEqual(settings.values[AI.PREFIX + 'openai/authcfg'], 'abc1234')
        self.assertNotIn('password', json.dumps(settings.values))

    def test_url_validation_and_missing_openai_key(self):
        for url in ('http://example.com/v1', 'file:///tmp', 'https://u:p@example.com/v1',
                    'https://example.com/v1?key=secret', 'https://example.com/#fragment',
                    'https://example.com:999999/v1'):
            with self.subTest(url=url), self.assertRaises(AI.AIError):
                AI.Client('custom', url)
        self.assertEqual(AI.endpoint('https://example.com/api/v1/'), 'https://example.com/api/v1')
        self.assertEqual(AI.endpoint('http://localhost:1234/v1'), 'http://127.0.0.1:1234/v1')
        with self.assertRaises(AI.AIError):
            AI.Client('ollama', 'https://example.com')
        with patch.object(ollama, '_open') as request:
            with self.assertRaisesRegex(AI.AIError, 'API key'):
                AI.Client('openai', AI.PROVIDERS['openai'][1]).list_models()
            request.assert_not_called()

    def test_existing_ollama_calls_keep_their_protocol(self):
        client = AI.Client('ollama', ollama.DEFAULT_URL)
        with patch.object(ollama, 'chat', return_value='text') as chat:
            self.assertEqual(client.chat('vision', 'read', ['image']), 'text')
            chat.assert_called_once_with(ollama.DEFAULT_URL, 'vision', 'read', ['image'], None)
        with patch.object(ollama, 'chat_json', return_value={'calls': []}) as chat:
            self.assertEqual(client.chat_json('chat', 'unread', deed_ai.CALL_SCHEMA), {'calls': []})
            chat.assert_called_once_with(ollama.DEFAULT_URL, 'chat', 'unread', deed_ai.CALL_SCHEMA)

    def test_compatible_http_models_vision_json_errors_and_redirects(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get('Authorization'), None))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'data': [{'id': 'vision-model'}, {'id': 'chat-model'}]}).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, self.headers.get('Authorization'), body))
                if body['model'] == 'redirect':
                    self.send_response(307)
                    self.send_header('Location', 'https://example.com/do-not-send')
                    self.end_headers()
                    return
                if body['model'] == 'unauthorized':
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'do not echo this deed or key')
                    return
                self.send_response(200)
                self.end_headers()
                text = {'vision': 'OCR text', 'json': '```json\n{"calls": []}\n```',
                        'bad-json': 'invented prose', 'bad-shape': '[]', 'bad-calls': '{"calls": ["invented"]}',
                        'bad-fields': '{"calls": [{"quote": "deed", "kind": "line", "bearing": {}}]}',
                        'empty': None}.get(body['model'])
                self.wfile.write(json.dumps({'choices': [{'message': {'content': text}}]}).encode())

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = AI.Client('custom', 'http://127.0.0.1:%d/v1' % server.server_port, 'test-key')
            self.assertEqual(client.list_models(), ['chat-model', 'vision-model'])
            self.assertEqual(client.chat('vision', 'read this page', ['aGk=']), 'OCR text')
            self.assertEqual(client.chat_json('json', 'unread only', deed_ai.CALL_SCHEMA), {'calls': []})
            self.assertEqual(requests[0][:2], ('/v1/models', 'Bearer test-key'))
            vision = requests[1][2]
            self.assertEqual(vision['messages'][0]['content'][1]['image_url']['url'], 'data:image/png;base64,aGk=')
            self.assertFalse(vision['stream'])
            self.assertEqual(requests[2][2]['messages'][-1]['content'], 'unread only')
            for model in ('bad-json', 'bad-shape', 'bad-calls', 'bad-fields'):
                with self.subTest(model=model), self.assertRaises(AI.AIError):
                    client.chat_json(model, 'text', deed_ai.CALL_SCHEMA)
            for model in ('empty', 'redirect', 'unauthorized'):
                with self.subTest(model=model), self.assertRaises(AI.AIError) as error:
                    client.chat(model, 'text')
                self.assertNotIn('do not echo', str(error.exception))
                self.assertNotIn('test-key', str(error.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


@unittest.skipUnless(HAS_QGIS, 'Requires QGIS/Qt')
class SettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qgis.core import QgsApplication
        cls.app = QgsApplication.instance() or QgsApplication([], False)
        cls.app.initQgis()

    def test_authentication_key_is_read_from_qgis_not_preferences(self):
        from qgis.core import QgsApplication
        manager = Mock()

        def fill(authcfg, config, full):
            config.setMethod('Basic')
            config.setConfig('password', 'secret-in-auth-db')
            return True

        manager.loadAuthenticationConfig.side_effect = fill
        with patch.object(QgsApplication, 'authManager', return_value=manager):
            config = dict(provider='openai', url=AI.PROVIDERS['openai'][1], authcfg='abc1234')
            self.assertEqual(AI.client(config).key, 'secret-in-auth-db')
            manager.loadAuthenticationConfig.assert_called_once()
            manager.loadAuthenticationConfig.return_value = False
            manager.loadAuthenticationConfig.side_effect = None
            with self.assertRaises(AI.AIError):
                AI.client(config)

    def test_settings_switch_cancel_save_and_profile_scope(self):
        from parcel_fabric.ui import ai_settings, settings_dialog
        from qgis.PyQt.QtWidgets import QTabWidget
        settings = Settings()
        fabric = SimpleNamespace(name='Test Fabric', settings={}, parcel_types=['Tax'], tolerance=.05,
                                 warn_ratio=10000, packed_dms=False, number_types=[], save_settings=Mock())
        with patch.object(ai_settings, 'QgsSettings', return_value=settings):
            dialog = settings_dialog.SettingsDialog(fabric)
            tabs = dialog.findChild(QTabWidget)
            self.assertIn('AI Services', [tabs.tabText(i) for i in range(tabs.count())])
            widget = dialog.ai
            widget.ocr_model.setCurrentText('my-local-vision')
            widget.provider.setCurrentIndex(widget.provider.findData('openai'))
            self.assertEqual(widget.url.text(), 'https://api.openai.com/v1')
            self.assertIn('may charge', widget.notice.text())
            widget.ocr_model.setCurrentText('my-hosted-vision')
            widget.provider.setCurrentIndex(widget.provider.findData('ollama'))
            self.assertEqual(widget.ocr_model.currentText(), 'my-local-vision')
            self.assertEqual(settings.values, {})
            dialog.reject()
            self.assertEqual(settings.values, {})
            dialog._ok()
            fabric.save_settings.assert_called_once()
            self.assertFalse(any('ai' in key or 'auth' in key for key in fabric.save_settings.call_args.args[0]))
            self.assertEqual(AI.load(settings)['ocr_model'], 'my-local-vision')
            self.assertEqual(AI.load(settings, 'openai')['ocr_model'], 'my-hosted-vision')
            dialog.deleteLater()

    def test_hosted_dialog_startup_does_not_contact_ai_or_unlock_keys(self):
        from parcel_fabric.ui import deed_reader_dialog
        from parcel_fabric.core import units
        settings = Settings()
        config = dict(provider='openai', url=AI.PROVIDERS['openai'][1], authcfg='abc1234',
                      ocr_model='vision', llm_model='chat')
        AI.save(settings, config)
        manager = SimpleNamespace(active=SimpleNamespace(foot_m=units.US_SURVEY_FOOT_M), active_record_id=None)
        with patch.object(deed_reader_dialog, 'QgsSettings', return_value=settings), patch.object(AI, 'client') as client:
            dialog = deed_reader_dialog.DeedReaderDialog(None, manager)
            client.assert_not_called()
            self.assertEqual(dialog.ocr_model.currentText(), 'vision')
            self.assertEqual(dialog.llm_model.currentText(), 'chat')
            self.assertIn('api.openai.com', dialog.ai_notice.text())
            dialog.deleteLater()


if __name__ == '__main__':
    unittest.main()
