import subprocess
from PIL import Image
from pathlib import Path
from unittest.mock import patch

import unittest
import test_conversion
import libreoffice_client as client


def make_pdf(pages):
    """Small valid PDF with a different solid colour on each page."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'']
    kids = []
    for i in range(pages):
        page_id = len(objects) + 1
        kids.append(f'{page_id} 0 R')
        stream = f'{i % 2} 0 {1 - i % 2} rg 0 0 72 72 re f'.encode()
        objects.append((f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] '
                        f'/Resources << >> /Contents {page_id + 1} 0 R >>').encode())
        objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
    objects[1] = f'<< /Type /Pages /Count {pages} /Kids [{" ".join(kids)}] >>'.encode()
    data = b'%PDF-1.4\n'
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(data)
    data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    data += (f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n'
             f'startxref\n{xref}\n%%EOF\n').encode()
    return data


class ImageTests(unittest.TestCase):
    setUp = test_conversion.ConversionTests.setUp

    def test_pdf_bypasses_office_and_preserves_page_order(self):
        source = self.root / 'input.pdf'
        source.write_bytes(make_pdf(12))

        def render(command):
            self.assertEqual(command[0], 'pdftoppm')
            prefix = Path(command[-1])
            # Deliberately reverse the order and use unpadded numbers.
            for i in reversed(range(1, 13)):
                with Image.new('RGB', (i, 2), (i, 0, 0)) as image:
                    image.save(prefix.with_name(f'page-{i}.png'))
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch.object(client, '_run_command', side_effect=render) as run:
            result = client.convert(source, self.root / 'result.png', 'png')
            self.assertEqual(run.call_count, 1)
        self.assertEqual(result.suffix, '.png')
        with Image.open(result) as image:
            self.assertEqual(image.size, (12, 24))
            for i in range(1, 13):
                self.assertEqual(image.getpixel((0, (i - 1) * 2)), (i, 0, 0))
            self.assertEqual(image.getpixel((11, 0)), (255, 255, 255))
        self.assertEqual(list(self.profiles.iterdir()), [])

    def test_renderer_failure_and_timeout_release_slot(self):
        source = self.root / 'input.pdf'
        source.write_bytes(make_pdf(1))
        for error in [RuntimeError('render failed'), subprocess.TimeoutExpired('pdftoppm', 1)]:
            with patch.object(client, '_run_command', side_effect=error):
                with self.assertRaises(type(error)):
                    client.convert(source, self.root / 'result.png', 'png')
            self.assertEqual(list(self.profiles.iterdir()), [])
            self.assertTrue(client.conversion_slots.acquire(blocking=False))
            self.assertTrue(client.conversion_slots.acquire(blocking=False))
            client.conversion_slots.release()
            client.conversion_slots.release()

    def test_renderer_nonzero_or_missing_output_fails(self):
        source = self.root / 'input.pdf'
        source.write_bytes(make_pdf(1))
        for code in (0, 1):
            with patch.object(client, '_run_command', return_value=subprocess.CompletedProcess([], code, '', 'error')):
                with self.assertRaises(RuntimeError):
                    client.convert(source, self.root / 'result.png', 'png')
            self.assertEqual(list(self.profiles.iterdir()), [])

    def test_invalid_page_rejected_before_launch(self):
        for page, fmt in [(0, 'png'), (-1, 'png'), (1, 'pdf'), ('2', 'png')]:
            with patch.object(client, '_run_command') as run:
                with self.assertRaises(ValueError):
                    client.convert('input.pdf', str(self.root / 'result.png'), fmt, page=page)
                run.assert_not_called()

    def test_oversized_stitch_rejected_before_allocation(self):
        path = self.root / 'page.png'
        with Image.new('RGB', (1, 40000)) as image:
            image.save(path)
        with self.assertRaisesRegex(ValueError, 'JPEG'):
            client._stitch_images([path, path], self.root / 'result.jpg', 'jpg')
