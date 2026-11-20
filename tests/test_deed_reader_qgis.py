"""Read Deed integration checks without running a language model or editing real records."""
import base64
import json
from pathlib import Path
import shutil
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parcel_fabric.core import cogo as C, deed_ai as A, ollama as O

try:
    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QRectF
    from qgis.PyQt.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPdfWriter
    HAS_QGIS = True
except ImportError:
    HAS_QGIS = False


DESCRIPTION = ('Beginning at a monument; thence North 0 East, 100 feet; '
               'thence East, 200 feet; thence South, 100 feet; '
               'thence West, 200 feet to the Point of Beginning.')


class LocalOllamaTests(unittest.TestCase):
    def test_only_local_urls_are_accepted(self):
        for base in ('http://localhost:11434', 'http://127.0.0.1:12345', 'http://[::1]:11434'):
            self.assertTrue(O._url(base, '/api/chat').endswith('/api/chat'))
        for base in ('https://ollama.com', 'http://192.168.1.5:11434', 'file:///tmp/test',
                     'http://localhost.example.com', 'http://localhost@evil.example',
                     'http://localhost:999999', 'http://localhost/?remote=true', 'http://localhost/#test'):
            with self.subTest(base=base), self.assertRaises(O.OllamaError):
                O._url(base, '/api/chat')

    def test_cloud_named_models_are_rejected_before_any_request(self):
        with patch.object(O, '_post') as post:
            for model in ('gemma4:cloud', 'model:123b-cloud', 'model-cloud'):
                with self.assertRaises(O.OllamaError):
                    O.chat(O.DEFAULT_URL, model, 'private text')
            post.assert_not_called()

    def test_local_http_client_quotes_json_and_redirect_protection(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.path == '/redirect/api/tags':
                    self.send_response(302)
                    self.send_header('Location', 'https://example.com/')
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'models': [{'name': 'deepseek-ocr:latest'}]}).encode())
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                content = json.dumps({'calls': []}) if body.get('format') else 'OCR TEXT'
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'message': {'content': content}}).encode())

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            self.assertEqual(O.list_models(url), ['deepseek-ocr:latest'])
            self.assertEqual(O.chat(url, 'deepseek-ocr:latest', 'Free OCR.', ['aGk=']), 'OCR TEXT')
            self.assertEqual(O.chat_json(url, 'qwen2.5vl:7b', 'one unread clause', A.CALL_SCHEMA), {'calls': []})
            self.assertEqual(requests[0]['messages'][0]['images'], ['aGk='])
            self.assertEqual(requests[1]['messages'][0]['content'], 'one unread clause')
            req = O.urllib.request.Request(url + '/redirect/api/tags')
            with self.assertRaisesRegex(O.OllamaError, 'redirect'):
                O._open(req, 5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_description_and_quoted_call_checks(self):
        deed = 'Warranty deed. ' + DESCRIPTION + ' Signed by grantor.'
        self.assertEqual(A.find_description(deed), DESCRIPTION)
        quote = 'thence North 0 East, 100 feet'
        call = {'kind': 'line', 'quote': quote, 'bearing': 'N0E', 'distance': 100}
        self.assertEqual(A.check_call(call, deed).distance, 100)
        for change in ({'quote': 'not in the deed'}, {'distance': 101}, {'distance': 100.004}, {'bearing': 'N1E'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                A.check_call(dict(call, **change), deed)
        curve = 'curve right, radius 100 feet, arc length 157.08 feet, chord bearing N45E'
        result = A.check_call({'kind': 'curve', 'quote': curve, 'bearing': 'N45E', 'radius': 100,
                               'arc_length': 157.08, 'side': 'right', 'bearing_is': 'chord'}, curve)
        self.assertEqual((result.kind, result.radius, result.arc_length), (C.CURVE, 100, 157.08))


@unittest.skipUnless(HAS_QGIS, 'Requires QGIS/Qt')
class DeedReaderQgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QgsApplication.instance() or QgsApplication([], False)
        cls.app.initQgis()
        font = Path('C:/Windows/Fonts/arial.ttf')
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        root = Path(__file__).resolve().parents[2]
        self.tmp = root / '_scratch' / ('deed-reader-' + uuid4().hex)
        self.tmp.mkdir(parents=True)

    def tearDown(self):
        self.assertEqual(self.tmp.parent, Path(__file__).resolve().parents[2] / '_scratch')
        self.assertTrue(self.tmp.name.startswith('deed-reader-'))
        shutil.rmtree(self.tmp)

    def pdf(self, scanned=False):
        path = self.tmp / ('scan.pdf' if scanned else 'text.pdf')
        writer = QPdfWriter(str(path))
        writer.setResolution(72)
        painter = QPainter(writer)
        painter.setFont(QFont('Arial', 10))
        if scanned:
            image = QImage(600, 200, QImage.Format.Format_RGB32)
            image.fill(QColor('white'))
            ip = QPainter(image)
            ip.drawText(20, 50, 'North 45 East, 100 feet')
            ip.end()
            painter.drawImage(QRectF(40, 40, 500, 200), image)
        else:
            painter.drawText(40, 80, 'Begin at a monument; thence North 45 East, 100 feet.')
        painter.end()
        del painter, writer
        return path

    def test_qgis4_text_pdf_is_read_directly_without_ocr(self):
        from parcel_fabric.core import deed_pages as pages
        path = self.pdf()
        with patch.object(pages, 'pdf_pages', side_effect=AssertionError('OCR must not be used')):
            images, text = pages.load(str(path))
        self.assertEqual(images, [])
        self.assertIn('North 45 East', '\n'.join(text))

    def test_scanned_pdf_uses_real_qt_renderer(self):
        from parcel_fabric.core import deed_pages as pages
        images, text = pages.load(str(self.pdf(True)))
        self.assertEqual(text, [])
        self.assertEqual(len(images), 1)
        self.assertFalse(QImage.fromData(base64.b64decode(images[0])).isNull())
        with self.assertRaises(pages.DeedReadError):
            pages._to_b64(QImage())

    def test_png_and_multi_page_tiff(self):
        from PIL import Image
        from parcel_fabric.core import deed_pages as pages
        first, second = Image.new('RGB', (100, 100), 'white'), Image.new('RGB', (100, 100), 'black')
        path = self.tmp / 'pages.tif'
        first.save(path, save_all=True, append_images=[second])
        images = pages.image_pages(str(path))
        self.assertEqual(len(images), 2)
        self.assertEqual(QImage.fromData(base64.b64decode(images[1])).pixelColor(1, 1), QColor('black'))
        path = self.tmp / 'page.png'
        first.save(path)
        self.assertEqual(len(pages.image_pages(str(path))), 1)

    def dialog(self):
        from parcel_fabric.ui.deed_reader_dialog import DeedReaderDialog
        from parcel_fabric.core import units
        manager = SimpleNamespace(active=SimpleNamespace(foot_m=units.US_SURVEY_FOOT_M), active_record_id=None)
        with patch.object(DeedReaderDialog, '_load_models'):
            dialog = DeedReaderDialog(None, manager)
        dialog.ai_config = dict(provider='ollama', url=O.DEFAULT_URL, ocr_model='deepseek-ocr',
                                llm_model='qwen2.5vl', authcfg='')
        dialog.ocr_model.setCurrentText('deepseek-ocr')
        dialog.llm_model.setCurrentText('qwen2.5vl')
        return dialog

    def test_parser_closure_review_gate_and_ai_color(self):
        from parcel_fabric.ui import deed_reader_dialog as module
        dialog = self.dialog()
        dialog.desc.setPlainText(DESCRIPTION)
        dialog._extract()
        self.assertEqual(len(dialog.courses), 4)
        self.assertIn('area', dialog.closure.text())
        self.assertFalse(dialog.load_btn.isEnabled())
        with patch.object(module.QMessageBox, 'information'), patch.object(dialog, 'accept') as accept:
            dialog._accept()
            accept.assert_not_called()
        dialog.reviewed.setChecked(True)
        self.assertTrue(dialog.load_btn.isEnabled())
        index = next(i for i, row in enumerate(dialog.rows) if row['course'])
        dialog.rows[index]['by'] = 'AI'
        dialog._show_rows()
        self.assertEqual(dialog.table.item(index, 3).foreground().color(), module.ORANGE)
        dialog.desc.setPlainText(DESCRIPTION + ' changed')
        self.assertFalse(dialog.reviewed.isChecked())
        self.assertFalse(dialog.load_btn.isEnabled())
        dialog.deleteLater()

    def test_ai_find_rejects_an_invented_tail_after_a_real_prefix(self):
        from parcel_fabric.ui import deed_reader_dialog as module
        dialog = self.dialog()
        deed = 'Begin at a monument. ' + ('real deed words ' * 30) + 'Point of Beginning.'
        dialog.deed_text.setPlainText(deed)
        dialog.desc.setPlainText('keep this description')
        answer = deed[:220] + ' invented extra description'
        with patch.object(module.OL, 'chat', return_value=answer):
            dialog._run = lambda label, work, done: done(work(None))
            dialog._find_ai()
        self.assertEqual(dialog.desc.toPlainText(), 'keep this description')
        dialog.deleteLater()

    def test_ai_receives_only_the_unread_clause(self):
        from parcel_fabric.ui import deed_reader_dialog as module
        dialog = self.dialog()
        dialog.desc.setPlainText('Beginning at a monument; thence North 0 East 100 feet; '
            'thence North 45 East along the property line 50 feet.')
        dialog._extract()
        unread = next(row['source'] for row in dialog.rows if row['phase'] == 'Boundary' and row['note'])
        answer = {'calls': [{'kind': 'line', 'quote': unread, 'bearing': 'N45E', 'distance': 50}]}
        task = SimpleNamespace(isCanceled=lambda: False, setProgress=lambda value: None)
        with patch.object(module.OL, 'chat_json', return_value=answer) as chat:
            dialog._run = lambda label, work, done: done(work(task))
            dialog._ask_ai()
        prompt = chat.call_args.args[2]
        self.assertIn(unread, prompt)
        self.assertNotIn('North 0 East 100 feet', prompt)
        self.assertEqual(len(dialog.courses), 2)
        self.assertTrue(any(row['by'] == 'AI' for row in dialog.rows))
        self.assertFalse(dialog.load_btn.isEnabled())
        dialog.deleteLater()

    def test_reviewed_deed_can_be_attached_without_overwriting_existing_document(self):
        from unittest.mock import Mock
        dialog = self.dialog()
        dialog.desc.setPlainText(DESCRIPTION)
        dialog._extract()
        dialog.path.setText(str(self.pdf()))
        dialog.manager.active_record_id = 'test-record'
        record = {'QPF_DocumentURL': ''}
        dialog.fabric.settings = {}
        dialog.fabric.record_by_id = lambda gid: record
        dialog.fabric.update_record = Mock()
        dialog.reviewed.setChecked(True)
        dialog._accept()
        dialog.fabric.update_record.assert_called_once_with('test-record', doc_url=dialog.path.text())
        dialog.fabric.update_record.reset_mock()
        record['QPF_DocumentURL'] = 'existing.pdf'
        dialog._attach_to_record()
        dialog.fabric.update_record.assert_not_called()
        dialog.deleteLater()

    def test_hosted_settings_do_not_require_authentication_to_read_text_pdf(self):
        dialog = self.dialog()
        dialog.ai_config.update(provider='openai', url='https://api.openai.com/v1', authcfg='locked-key')
        dialog.path.setText(str(self.pdf()))
        with patch.object(dialog, '_client', side_effect=AssertionError('A text PDF needs no AI key')):
            dialog._run = lambda label, work, done: done(work(None))
            dialog._read()
        self.assertIn('North 45 East', dialog.deed_text.toPlainText())
        dialog.deleteLater()

    def test_hosted_scan_uses_vision_model_and_preserves_review_gate(self):
        from unittest.mock import Mock
        dialog = self.dialog()
        dialog.ai_config.update(provider='custom', url='https://example.com/v1')
        dialog.ocr_model.setCurrentText('configured-vision')
        dialog.path.setText(str(self.pdf(True)))
        client = Mock()
        client.chat.return_value = DESCRIPTION
        task = SimpleNamespace(isCanceled=lambda: False, setProgress=lambda value: None)
        with patch.object(dialog, '_client', return_value=client):
            dialog._run = lambda label, work, done: done(work(task))
            dialog._read()
        self.assertEqual(client.chat.call_args.args[0], 'configured-vision')
        self.assertEqual(len(client.chat.call_args.args[2]), 1)
        self.assertEqual(len(dialog.courses), 4)
        self.assertFalse(dialog.load_btn.isEnabled())
        dialog.deleteLater()


if __name__ == '__main__':
    unittest.main()
