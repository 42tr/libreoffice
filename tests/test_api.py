import asyncio
import concurrent.futures
import io
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from PIL import Image

import main
import uvicorn
import libreoffice_client as client


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base_patch = patch.object(main, 'BASE_DIR', cls.temp.name)
        cls.base_patch.start()
        cls.sock = socket.socket()
        cls.sock.bind(('127.0.0.1', 0))
        cls.url = 'http://127.0.0.1:%s' % cls.sock.getsockname()[1]
        # A small outer limit exercises the upload middleware without huge bodies.
        app = main.UploadLimitMiddleware(main.app, max_bytes=64 * 1024)
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={'sockets': [cls.sock]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 30
        while not cls.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError('API server did not start')
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.sock.close()
        cls.base_patch.stop()
        cls.temp.cleanup()

    def post(self, content=b'hello', fmt='pdf', filename='same.txt', with_headers=False, page=None,
             dpi=None):
        boundary = 'conversion-test-boundary'
        body = (b'--' + boundary.encode() +
                ('\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n' % filename).encode() +
                b'Content-Type: text/plain\r\n\r\n' + content +
                b'\r\n--' + boundary.encode() + b'--\r\n')
        query = urllib.parse.urlencode({key: value for key, value in
                                        [('target_format', fmt), ('page', page), ('dpi', dpi)]
                                        if value is not None})
        request = urllib.request.Request(
            self.url + '/convert?' + query, data=body,
            headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                return ((response.status, response.read(), response.headers) if with_headers
                        else (response.status, response.read()))
        except urllib.error.HTTPError as response:
            if with_headers:
                return response.code, response.read(), response.headers
            return response.code, response.read()

    def assert_clean(self):
        deadline = time.monotonic() + 5
        while list(Path(self.temp.name).iterdir()) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_concurrent_requests_keep_event_loop_responsive(self):
        barrier = threading.Barrier(3)
        release = threading.Event()

        def convert(source, target, fmt, page=None, dpi=None):
            barrier.wait(timeout=10)
            if not release.wait(timeout=10):
                raise RuntimeError('conversion was not released')
            Path(target).write_bytes(Path(source).read_bytes())
            return Path(target)

        with patch.object(main, 'convert', side_effect=convert):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.post, str(i).encode()) for i in range(2)]
                try:
                    barrier.wait(timeout=10)
                    with urllib.request.urlopen(self.url + '/openapi.json', timeout=2) as response:
                        self.assertEqual(response.status, 200)
                finally:
                    release.set()
                for i, future in enumerate(futures):
                    self.assertEqual(future.result(), (200, str(i).encode()))
        self.assert_clean()

    def test_error_statuses_and_cleanup(self):
        for error, status in [(ValueError('format'), 400),
                              (client.ConversionBusyError('busy'), 503),
                              (subprocess.TimeoutExpired('soffice', 1), 504),
                              (RuntimeError('failed'), 500)]:
            with self.subTest(status=status), patch.object(main, 'convert', side_effect=error):
                code, body = self.post()
                self.assertEqual(code, status)
                if status == 500:
                    # Internal details stay in the log; the client gets a task id.
                    self.assertNotIn(b'failed', body)
                    self.assertIn('任务 ID'.encode(), body)
                self.assert_clean()

    def test_health(self):
        with urllib.request.urlopen(self.url + '/health', timeout=2) as response:
            self.assertEqual(response.status, 200)

    def test_upload_limit_rejects_declared_length_before_body(self):
        port = int(self.url.rsplit(':', 1)[1])
        with socket.create_connection(('127.0.0.1', port), timeout=5) as conn, \
                patch.object(main, 'convert') as convert:
            # Only the headers are sent, as with curl's Expect: 100-continue.
            conn.sendall(b'POST /convert HTTP/1.1\r\nHost: test\r\n'
                         b'Content-Type: multipart/form-data; boundary=x\r\n'
                         b'Content-Length: 104857600\r\n\r\n')
            self.assertTrue(conn.recv(4096).startswith(b'HTTP/1.1 413 '))
            convert.assert_not_called()
        self.assert_clean()

    def test_upload_limit_rejects_streamed_body(self):
        boundary = b'conversion-test-boundary'
        chunks = [b'--' + boundary + b'\r\nContent-Disposition: form-data; name="file"; '
                  b'filename="big.txt"\r\n\r\n'] + [b'x' * 8192] * 16
        messages = [{'type': 'http.request', 'body': chunk, 'more_body': True} for chunk in chunks]
        sent = []

        async def receive():
            return messages.pop(0) if messages else {'type': 'http.disconnect'}

        async def send(message):
            sent.append(message)

        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': 'POST', 'scheme': 'http', 'path': '/convert', 'raw_path': b'/convert',
                 'query_string': b'target_format=pdf', 'root_path': '',
                 'headers': [(b'content-type', b'multipart/form-data; boundary=' + boundary),
                             (b'transfer-encoding', b'chunked')],
                 'server': ('test', 80), 'client': ('test', 1)}
        with patch.object(main, 'convert') as convert:
            asyncio.run(main.UploadLimitMiddleware(main.app, 64 * 1024)(scope, receive, send))
            convert.assert_not_called()
        self.assertEqual(sent[0]['status'], 413)
        self.assertTrue(messages, 'the body should not be read past the limit')
        self.assert_clean()

    def test_response_media_type_and_filename(self):
        def convert(source, target, fmt, page=None, dpi=None):
            Path(target).write_bytes(b'converted')
            return Path(target)

        with patch.object(main, 'convert', side_effect=convert):
            for fmt, media_type in [('pdf', 'application/pdf'),
                                    ('DOCX', 'application/vnd.openxmlformats-officedocument.'
                                             'wordprocessingml.document')]:
                status, _, headers = self.post(fmt=fmt, filename='C:\\docs\\report.txt',
                                               with_headers=True)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Content-Type'], media_type)
                self.assertIn('filename="report.%s"' % fmt.lower(), headers['Content-Disposition'])
        self.assert_clean()

    @unittest.skipUnless(os.getenv('RUN_IMAGE_TESTS') == '1', 'requires pdftoppm')
    def test_real_pdf_images(self):
        from test_images import make_pdf
        for fmt in ('png', 'jpg', 'jpeg'):
            for count in (1, 2):
                with self.subTest(fmt=fmt, pages=count):
                    status, body, headers = self.post(
                        make_pdf(count), fmt, 'document.pdf', with_headers=True)
                    self.assertEqual(status, 200, body)
                    magic = b'\x89PNG\r\n\x1a\n' if fmt == 'png' else b'\xff\xd8\xff'
                    self.assertTrue(body.startswith(magic))
                    self.assertEqual(headers['Content-Type'],
                                     'image/png' if fmt == 'png' else 'image/jpeg')
                    self.assertIn('document.' + fmt, headers['Content-Disposition'])
                    with Image.open(io.BytesIO(body)) as image:
                        self.assertEqual(image.size, (150, 150 * count))
                        self.assertGreater(image.getpixel((75, 75))[2], 240)
                        if count == 2:
                            self.assertGreater(image.getpixel((75, 225))[0], 240)
                    self.assert_clean()

    @unittest.skipUnless(os.getenv('RUN_IMAGE_TESTS') == '1', 'requires soffice and pdftoppm')
    def test_real_office_to_images(self):
        document = (b'<html><body><p>First page</p>'
                    b'<p style="page-break-before:always">Second page</p></body></html>')
        status, body = self.post(document, 'png', 'document.html')
        self.assertEqual(status, 200, body)
        with Image.open(io.BytesIO(body)) as image:
            self.assertEqual(image.format, 'PNG')
            self.assertGreater(image.height, image.width * 2)
        self.assert_clean()

    def test_invalid_page_parameters(self):
        for page in (0, -1, 'abc', '1.5'):
            self.assertEqual(self.post(fmt='png', page=page)[0], 422)
        for dpi in (49, 601, 'abc'):
            self.assertEqual(self.post(fmt='png', dpi=dpi)[0], 422)
        with patch.object(main, 'convert') as convert:
            self.assertEqual(self.post(fmt='pdf', page=1)[0], 400)
            self.assertEqual(self.post(fmt='pdf', dpi=150)[0], 400)
            self.assertEqual(self.post(fmt='bmp')[0], 400)
            convert.assert_not_called()
        self.assert_clean()

    @unittest.skipUnless(os.getenv('RUN_IMAGE_TESTS') == '1', 'requires pdftoppm')
    def test_real_selected_page(self):
        from test_images import make_pdf
        for fmt in ('png', 'jpg', 'jpeg'):
            for page in (1, 2):
                status, body = self.post(make_pdf(2), fmt, 'document.pdf', page=page)
                self.assertEqual(status, 200, body)
                with Image.open(io.BytesIO(body)) as image:
                    self.assertEqual(image.size, (150, 150))
                    channel = 2 if page == 1 else 0
                    self.assertGreater(image.getpixel((75, 75))[channel], 240)
                self.assert_clean()
        status, body = self.post(make_pdf(2), 'png', 'document.pdf', page=3)
        self.assertEqual(status, 400, body)
        self.assertIn('共 2 页', body.decode())
        self.assert_clean()

    @unittest.skipUnless(os.getenv('RUN_LIBREOFFICE_TESTS') == '1', 'requires soffice')
    def test_real_parallel_conversions(self):
        real_run = client._run_command
        guard = threading.Lock()
        active = peak = 0

        def run(command, *args, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                return real_run(command, *args, **kwargs)
            finally:
                with guard:
                    active -= 1

        start = time.monotonic()
        with patch.object(client, '_run_command', side_effect=run):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(self.post, ('unique-document-%s' % i).encode(),
                                       'docx' if i % 2 else 'pdf') for i in range(4)]
                for i, future in enumerate(futures):
                    status, body = future.result()
                    self.assertEqual(status, 200, body)
                    if i % 2:
                        with zipfile.ZipFile(io.BytesIO(body)) as archive:
                            self.assertIn(('unique-document-%s' % i).encode(),
                                          archive.read('word/document.xml'))
                    else:
                        self.assertTrue(body.startswith(b'%PDF-'), body[:100])
        self.assertEqual(peak, min(4, client.MAX_CONCURRENCY))
        self.assert_clean()
        # Job directories are removed; per-slot profiles stay for reuse.
        self.assertEqual([path.name for path in client.CLI_PROFILE_DIR.iterdir()
                          if not path.name.startswith('profile-')], [])
        print('Real conversions: 4 successful, peak=%s, elapsed=%.2fs' %
              (peak, time.monotonic() - start), flush=True)


if __name__ == '__main__':
    unittest.main()
