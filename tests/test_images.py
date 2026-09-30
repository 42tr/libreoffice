import subprocess
import time
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


def pdfinfo_output(sizes, rotations=()):
    lines = [f'Pages:          {len(sizes)}']
    for i, (w, h) in enumerate(sizes, 1):
        lines.append(f'Page {i:4d} size: {w} x {h} pts')
        lines.append(f'Page {i:4d} rot:  {rotations[i - 1] if rotations else 0}')
    return '\n'.join(lines) + '\n'


def fake_tools(sizes, render=None, rotations=()):
    """Answer pdfinfo with the given page sizes and delegate pdftoppm to render."""
    def run(command, deadline=None, env=None):
        if command[0] == 'pdfinfo':
            assert env == {'LC_ALL': 'C'}
            return subprocess.CompletedProcess(command, 0, pdfinfo_output(sizes, rotations), '')
        assert command[0] == 'pdftoppm', command
        return render(command)
    return run


class ImageTests(unittest.TestCase):
    setUp = test_conversion.ConversionTests.setUp

    def test_pdf_bypasses_office_and_preserves_page_order(self):
        source = self.root / 'input.pdf'
        source.write_bytes(make_pdf(12))

        def render(command):
            prefix = Path(command[-1])
            # Deliberately reverse the order and use unpadded numbers.
            for i in reversed(range(1, 13)):
                with Image.new('RGB', (i, 2), (i, 0, 0)) as image:
                    image.save(prefix.with_name(f'page-{i}.png'))
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch.object(client, '_run_command', side_effect=fake_tools([(72, 72)] * 12, render)) as run:
            result = client.convert(source, self.root / 'result.png', 'png')
            self.assertEqual([call.args[0][0] for call in run.call_args_list], ['pdfinfo', 'pdftoppm'])
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
            def render(command, error=error):
                raise error
            with patch.object(client, '_run_command', side_effect=fake_tools([(72, 72)], render)):
                with self.assertRaises(type(error)):
                    client.convert(source, self.root / 'result.png', 'png')
            self.assertEqual(list(self.profiles.iterdir()), [])
            self.assertEqual(client.conversion_slots.qsize(), 2)

    def test_renderer_nonzero_or_missing_output_fails(self):
        source = self.root / 'input.pdf'
        source.write_bytes(make_pdf(1))
        for code in (0, 1):
            def render(command, code=code):
                return subprocess.CompletedProcess(command, code, '', 'error')
            with patch.object(client, '_run_command', side_effect=fake_tools([(72, 72)], render)):
                with self.assertRaisesRegex(RuntimeError, 'pdftoppm'):
                    client.convert(source, self.root / 'result.png', 'png')
            self.assertEqual(list(self.profiles.iterdir()), [])

    def test_oversized_output_rejected_before_rendering(self):
        source = self.root / 'input.pdf'
        source.write_bytes(make_pdf(1))
        for sizes, fmt, dpi in [
            ([(612, 792)] * 200, 'png', None),     # 200 letter pages at 150 DPI
            ([(72, 72), (72, 32000)], 'jpg', None),  # JPEG side limit
            ([(612, 792)] * 8, 'png', 600),         # higher DPI grows the canvas
        ]:
            with self.subTest(pages=len(sizes), fmt=fmt, dpi=dpi):
                with patch.object(client, '_run_command', side_effect=fake_tools(sizes)):
                    with self.assertRaisesRegex(ValueError, 'dpi'):
                        client.convert(source, self.root / f'result.{fmt}', fmt, dpi=dpi)

    def test_page_sizes_follow_rotation(self):
        with patch.object(client, '_run_command',
                          side_effect=fake_tools([(100, 50), (100, 50)], rotations=(90, 180))):
            count, sizes = client._page_sizes(Path('input.pdf'), time.monotonic() + 5)
        self.assertEqual(count, 2)
        self.assertEqual(sizes, {1: (50.0, 100.0), 2: (100.0, 50.0)})

    def test_page_selection_checks_count_and_only_that_page(self):
        source = self.root / 'input.pdf'
        source.write_bytes(make_pdf(1))
        sizes = [(72, 72), (72, 72000)]
        with patch.object(client, '_run_command', side_effect=fake_tools(sizes)):
            with self.assertRaisesRegex(ValueError, '共 2 页'):
                client.convert(source, self.root / 'result.png', 'png', page=3)
            with self.assertRaisesRegex(ValueError, 'JPEG'):
                client.convert(source, self.root / 'result.jpg', 'jpg', page=2)

        def render(command):
            self.assertEqual(command[command.index('-r') + 1], '300')
            self.assertEqual(command[command.index('-f') + 1], '1')
            with Image.new('RGB', (300, 300), 'red') as image:
                image.save(Path(command[-1]).with_name('page-1.png'))
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch.object(client, '_run_command', side_effect=fake_tools(sizes, render)):
            result = client.convert(source, self.root / 'result.jpg', 'jpg', page=1, dpi=300)
        with Image.open(result) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (300, 300))
            self.assertEqual(round(image.info['dpi'][0]), 300)

    def test_invalid_page_rejected_before_launch(self):
        for page, fmt in [(0, 'png'), (-1, 'png'), (1, 'pdf'), ('2', 'png')]:
            with patch.object(client, '_run_command') as run:
                with self.assertRaises(ValueError):
                    client.convert('input.pdf', str(self.root / 'result.png'), fmt, page=page)
                run.assert_not_called()
        for dpi, fmt in [(49, 'png'), (601, 'png'), (True, 'png'), (150, 'docx')]:
            with patch.object(client, '_run_command') as run:
                with self.assertRaises(ValueError):
                    client.convert('input.pdf', str(self.root / 'result.png'), fmt, dpi=dpi)
                run.assert_not_called()

    def test_oversized_stitch_rejected_before_allocation(self):
        path = self.root / 'page.png'
        with Image.new('RGB', (1, 40000)) as image:
            image.save(path)
        with self.assertRaisesRegex(ValueError, 'JPEG'):
            client._stitch_images([path, path], self.root / 'result.jpg', 'jpg')
