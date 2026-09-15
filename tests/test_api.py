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
        cls.server = uvicorn.Server(uvicorn.Config(main.app, log_level='warning'))
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

    def post(self, content=b'hello', fmt='pdf', filename='same.txt', with_headers=False, page=None):
        boundary = 'conversion-test-boundary'
        body = (b'--' + boundary.encode() +
                ('\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n' % filename).encode() +
                b'Content-Type: text/plain\r\n\r\n' + content +
                b'\r\n--' + boundary.encode() + b'--\r\n')
        request = urllib.request.Request(
            self.url + '/convert?target_format=' + fmt + ('' if page is None else '&page=' + str(page)), data=body,
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

        def convert(source, target, fmt, page=None):
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
                self.assertEqual(self.post()[0], status)
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
        self.assertEqual(self.post(fmt='pdf', page=1)[0], 400)
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

        def run(command):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                return real_run(command)
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
        self.assertEqual(list(client.CLI_PROFILE_DIR.iterdir()), [])
        print('Real conversions: 4 successful, peak=%s, elapsed=%.2fs' %
              (peak, time.monotonic() - start), flush=True)


if __name__ == '__main__':
    unittest.main()
