import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pymupdf

SCRIPT = Path(__file__).resolve().parents[1] / 'paper-download/scripts/download_papers.py'
spec = importlib.util.spec_from_file_location('download_papers', SCRIPT)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


def pdf_bytes(title, pages=2, blank_body=False, metadata_title=None):
    with pymupdf.open() as document:
        for number in range(pages):
            page = document.new_page()
            if number == 0:
                page.insert_text((40, 60), title, fontsize=16)
            if number == 0 or not blank_body:
                page.insert_textbox(pymupdf.Rect(40, 100, 550, 700),
                                    ('Research methods and experimental results. ' * 40), fontsize=11)
        document.set_metadata({'title': metadata_title or title})
        return document.tobytes()


class PaperTests(unittest.TestCase):
    def test_export_and_duplicate_dois(self):
        rows = app.load_entries({'entries': [
            {'title': 'Paper One', 'doi': 'https://doi.org/10.1234/ABC'},
            {'title': 'Paper One', 'doi': '10.1234/abc'},
            {'title': 'Paper Two', 'pdf_urls': ['https://example.org/two.pdf']},
        ]})
        plan = app.plan_papers(rows)
        self.assertEqual(len(plan), 2)
        self.assertEqual(len(plan[0]['members']), 2)
        self.assertEqual(plan[0]['doi'], '10.1234/abc')

    def test_title_collisions_do_not_merge_different_dois(self):
        plan = app.plan_papers(app.load_entries([
            {'title': 'A: Paper', 'doi': '10.1234/one'},
            {'title': 'A/ Paper', 'doi': '10.1234/two'},
            {'title': '测' * 250},
        ]))
        self.assertEqual(len(plan), 3)
        self.assertEqual(len({p['filename'].casefold() for p in plan}), 3)
        self.assertTrue(all(len(p['filename'].encode()) <= 255 for p in plan))
        self.assertTrue(all('/' not in p['filename'] for p in plan))

    def test_invalid_entries_fail_before_download(self):
        for value in [{'entries': {}}, [{'doi': '10.1234/one'}], [{'title': 'T', 'pdf_urls': 'https://example.org'}]]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.load_entries(value)

    def test_arxiv_doi_produces_direct_source(self):
        row = app.plan_papers(app.load_entries([{'title': 'Example', 'doi': '10.48550/arXiv.2001.00001'}]))[0]
        self.assertIn('https://arxiv.org/pdf/2001.00001', app.direct_urls(row))

    def test_subset_merge_keeps_other_duplicate_members(self):
        previous = [dict(title='A Real Paper', doi='10.1234/one', index=index, position=position,
                         key='10.1234/one', filename='A Real Paper.pdf')
                    for position, index in enumerate(('A', 'B'), 1)]
        incoming = app.load_entries([dict(title='A Real Paper', doi='10.1234/one', index='B')])
        papers, _, _ = app.merge_plan(incoming, previous)
        self.assertEqual([m['index'] for m in papers[0]['members']], ['A', 'B'])


class ValidationTests(unittest.TestCase):
    def test_full_pdf_and_mismatched_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'paper.pdf'
            path.write_bytes(pdf_bytes('A Real Paper'))
            self.assertTrue(app.validate_pdf(path, 'A Real Paper')['valid'])
            self.assertEqual(app.validate_pdf(path, 'A Different Paper')['reason'], 'title_mismatch')

    def test_spoofed_metadata_does_not_override_visible_title(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wrong.pdf'
            path.write_bytes(pdf_bytes('Unrelated Database Research', metadata_title='A Real Paper'))
            self.assertFalse(app.validate_pdf(path, 'A Real Paper')['valid'])

    def test_abstract_with_blank_body_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preview.pdf'
            path.write_bytes(pdf_bytes('A Real Paper', pages=5, blank_body=True))
            self.assertEqual(app.validate_pdf(path, 'A Real Paper')['reason'], 'missing_body_pages')

    def test_invisible_title_is_not_a_visible_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'invisible.pdf'
            with pymupdf.open() as document:
                page = document.new_page()
                page.insert_text((40, 60), 'Unrelated Database Research', fontsize=16)
                page.insert_text((40, 100), 'A Real Paper', render_mode=3)
                document.save(path)
            self.assertFalse(app.validate_pdf(path, 'A Real Paper')['valid'])

    def test_off_page_title_is_not_a_visible_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'outside.pdf'
            with pymupdf.open() as document:
                page = document.new_page()
                page.insert_text((40, 60), 'Unrelated Database Research', fontsize=16)
                page.insert_text((700, 80), 'A Real Paper')
                document.save(path)
            self.assertFalse(app.validate_pdf(path, 'A Real Paper')['valid'])


class DownloadIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = pdf_bytes('A Real Paper')
        cls.wrong = pdf_bytes('Unrelated Database Research')
        cls.hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.hits.append(self.path)
                payload = {'/paper.pdf': cls.payload, '/wrong.pdf': cls.wrong,
                           '/truncated.pdf': cls.payload}.get(self.path)
                if payload is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/pdf')
                self.send_header('Content-Length', str(len(payload) + (1000 if self.path == '/truncated.pdf' else 0)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def run_cli(self, entries, directory, *extra):
        source = Path(directory) / 'entries.json'
        source.write_text(json.dumps(entries))
        return subprocess.run([sys.executable, str(SCRIPT), '--input', str(source),
                               '--out', str(Path(directory) / 'output'), '--backend', 'direct',
                               *extra], text=True, capture_output=True)

    def test_download_deduplicate_and_resume_without_network(self):
        entries = [{'title': 'A Real Paper', 'pdf_urls': [self.origin + '/paper.pdf']}] * 2
        with tempfile.TemporaryDirectory() as directory:
            before = len(self.hits)
            first = self.run_cli(entries, directory)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self.run_cli(entries, directory)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(len(self.hits) - before, 1)
            output = Path(directory) / 'output'
            self.assertEqual(len(list((output / 'pdfs').glob('*.pdf'))), 1)
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(summary['downloaded_unique'], 1)
            self.assertEqual(summary['covered_entries'], 2)

    def test_reject_wrong_pdf_then_try_next_known_url(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli([{'title': 'A Real Paper', 'pdf_urls': [
                self.origin + '/wrong.pdf', self.origin + '/paper.pdf']}], directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = Path(directory) / 'output'
            self.assertEqual(len(list((output / 'rejected').glob('*.pdf'))), 1)
            self.assertTrue((output / 'pdfs/A Real Paper.pdf').exists())

    def test_pending_is_reusable_as_input(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli([{'title': 'Missing', 'pdf_urls': [self.origin + '/404']}], directory)
            self.assertEqual(result.returncode, 1, result.stderr)
            pending = json.loads((Path(directory) / 'output/pending.json').read_text())
            self.assertEqual(pending[0]['title'], 'Missing')
            self.assertEqual(pending[0]['pdf_urls'], [self.origin + '/404'])
            self.assertTrue(app.load_entries(pending))

    def test_unknown_existing_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            pdfs = Path(directory) / 'output/pdfs'
            pdfs.mkdir(parents=True)
            path = pdfs / 'A Real Paper.pdf'
            path.write_bytes(b'user-owned data')
            result = self.run_cli([{'title': 'A Real Paper', 'pdf_urls': [self.origin + '/paper.pdf']}], directory)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(path.read_bytes(), b'user-owned data')

    def test_dry_run_performs_no_network_or_output_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            before = len(self.hits)
            result = self.run_cli([{'title': 'A Real Paper', 'pdf_urls': [self.origin + '/paper.pdf']}],
                                  directory, '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(directory) / 'output').exists())
            self.assertEqual(before, len(self.hits))

    def test_dry_run_uses_the_saved_filename_for_subset_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            entries = [dict(title='A Real Paper', doi=f'10.1234/{name}',
                            pdf_urls=[self.origin + '/paper.pdf']) for name in ('one', 'two')]
            first = self.run_cli(entries, directory)
            self.assertEqual(first.returncode, 0, first.stderr)
            report = json.loads((Path(directory) / 'output/report.json').read_text())
            preview = self.run_cli(entries[:1], directory, '--dry-run')
            self.assertEqual(json.loads(preview.stdout)[0]['filename'], report[0]['filename'])

    def test_report_uses_current_result_over_input_status_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli([{'title': 'A Real Paper', 'reason': 'stale error', 'status': 'pending',
                                   'pdf_urls': [self.origin + '/paper.pdf']}], directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((Path(directory) / 'output/report.json').read_text())
            self.assertEqual(report[0]['reason'], '')

    def test_cross_batch_title_collision_does_not_reuse_another_doi(self):
        with tempfile.TemporaryDirectory() as directory:
            first = dict(title='A Real Paper', doi='10.1234/one', pdf_urls=[self.origin + '/paper.pdf'])
            second = dict(first, doi='10.1234/two')
            self.assertEqual(self.run_cli([first], directory).returncode, 0)
            before = len(self.hits)
            result = self.run_cli([second], directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(self.hits) - before, 1)
            self.assertEqual(len(list((Path(directory) / 'output/pdfs').glob('*.pdf'))), 2)

    def test_pending_retry_preserves_successful_history_and_names(self):
        with tempfile.TemporaryDirectory() as directory:
            one = dict(title='A Real Paper', doi='10.1234/one', pdf_urls=[self.origin + '/paper.pdf'])
            two = dict(title='A Real Paper', doi='10.1234/two', pdf_urls=[self.origin + '/404'])
            self.assertEqual(self.run_cli([one, two], directory).returncode, 1)
            output = Path(directory) / 'output'
            report = json.loads((output / 'report.json').read_text())
            old_filename = report[0]['filename']
            pending = json.loads((output / 'pending.json').read_text())
            pending[0]['pdf_urls'] = [self.origin + '/paper.pdf']
            self.assertEqual(self.run_cli(pending, directory).returncode, 0)
            report = json.loads((output / 'report.json').read_text())
            self.assertEqual(len(report), 2)
            self.assertEqual(report[0]['filename'], old_filename)
            before = len(self.hits)
            self.assertEqual(self.run_cli([one], directory).returncode, 0)
            self.assertEqual(len(self.hits), before)

    def test_content_length_mismatch_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli([dict(title='A Real Paper', pdf_urls=[self.origin + '/truncated.pdf'])], directory)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(list((Path(directory) / 'output/pdfs').glob('*.pdf')))
            pending = json.loads((Path(directory) / 'output/pending.json').read_text())
            self.assertIn('length', pending[0]['last_attempts'][0]['message'].lower())

    def test_adding_doi_to_pending_entry_replaces_its_old_key(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_cli([dict(index=7, title='A Real Paper')], directory).returncode, 1)
            output = Path(directory) / 'output'
            pending = json.loads((output / 'pending.json').read_text())
            pending[0].update(doi='10.1234/one', pdf_urls=[self.origin + '/paper.pdf'])
            result = self.run_cli(pending, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((output / 'pending.json').read_text()), [])
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(summary['unique_papers'], 1)
            self.assertEqual(summary['input_entries'], 1)


if __name__ == '__main__':
    unittest.main()
