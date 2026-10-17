#!/usr/bin/env python3
"""Download a JSON paper list through scihub-downloader and verify every PDF."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import threading
import unicodedata
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pymupdf


def compact(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value).casefold() if c.isalnum())


def normalize_doi(value):
    value = unquote(str(value or '').strip())
    value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', value, flags=re.I)
    if value and not re.fullmatch(r'10\.\d{4,9}/\S+', value):
        raise ValueError(f'Invalid DOI: {value}')
    return value.casefold()


def load_entries(raw):
    if isinstance(raw, dict):
        raw = raw.get('entries')
    if not isinstance(raw, list) or not raw:
        raise ValueError('Input must be a nonempty JSON array or an object containing entries[].')
    rows = []
    for position, item in enumerate(raw, 1):
        if not isinstance(item, dict) or not isinstance(item.get('title'), str) or not item['title'].strip():
            raise ValueError(f'Entry {position} needs a nonempty title.')
        urls = item.get('pdf_urls', [])
        if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
            raise ValueError(f'Entry {position}: pdf_urls must be a list of URLs.')
        if any(urlsplit(url).scheme not in ('http', 'https') or not urlsplit(url).netloc for url in urls):
            raise ValueError(f'Entry {position}: only absolute HTTP(S) PDF URLs are supported.')
        row = dict(item, title=item['title'].strip(), doi=normalize_doi(item.get('doi') or item.get('resolved_doi')),
                   pdf_urls=list(dict.fromkeys(urls)), position=position)
        rows.append(row)
    return rows


def safe_stem(title):
    title = unicodedata.normalize('NFKC', title)
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', title)
    title = re.sub(r'\s+', ' ', title).strip('._ ')
    title = title.encode('utf-8')[:220].decode('utf-8', errors='ignore').rstrip('._ ') or 'untitled'
    if re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', title, re.I):
        title = '_' + title
    return title


def plan_papers(entries):
    papers = {}
    for entry in entries:
        key = entry['doi'] or 'title:' + compact(entry['title']) + ':' + str(entry.get('year', ''))
        if key not in papers:
            papers[key] = dict(key=key, title=entry['title'], doi=entry['doi'], members=[], pdf_urls=[],
                               filename=safe_stem(entry['title']) + '.pdf')
        paper = papers[key]
        paper['members'].append(entry)
        paper['pdf_urls'] = list(dict.fromkeys(paper['pdf_urls'] + entry['pdf_urls']))
    collisions = Counter(paper['filename'].casefold() for paper in papers.values())
    for paper in papers.values():
        if collisions[paper['filename'].casefold()] > 1:
            suffix = hashlib.sha256(paper['key'].encode()).hexdigest()[:10]
            paper['filename'] = safe_stem(paper['title']) + f' ({suffix}).pdf'
    return list(papers.values())


def direct_urls(paper):
    urls = list(paper['pdf_urls'])
    if paper['doi'].startswith('10.48550/arxiv.'):
        urls.append('https://arxiv.org/pdf/' + paper['doi'].split('arxiv.', 1)[1])
    return list(dict.fromkeys(urls))


def merge_plan(entries, previous):
    """Keep the complete batch history and reserve filenames across separate runs."""
    incoming = plan_papers(entries)
    requested = {paper['key'] for paper in incoming}
    known = {row['key']: row for row in previous}
    output_fields = {'key', 'filename', 'file', 'status', 'reason', 'attempts', 'valid', 'resumed',
                     'source', 'url', 'duplicate_of', 'pages', 'bytes', 'metadata_title', 'blank_pages'}
    old_entries = [{key: value for key, value in row.items() if key not in output_fields} for row in previous]
    papers = {paper['key']: paper for paper in plan_papers(load_entries(old_entries))} if old_entries else {}
    used = set()
    for paper in papers.values():
        filename = known[paper['key']]['filename']
        if Path(filename).name != filename or '\\' in filename or not filename.endswith('.pdf'):
            raise ValueError('Unsafe filename in previous report.json.')
        if filename.casefold() in used:
            raise ValueError('Duplicate filename in previous report.json.')
        paper['filename'] = filename
        used.add(filename.casefold())
        for member in paper['members']:
            member.setdefault('_download_record_id', f"entry-{member['position']}")

    def same_member(new, old):
        if '_download_record_id' in new:
            return new['_download_record_id'] == old.get('_download_record_id')
        if 'index' in new:
            return 'index' in old and new['index'] == old['index']
        fields = ('title', 'year', 'authors')
        return all(new.get(field, '') == old.get(field, '') for field in fields)

    next_position = len(previous) + 1
    for paper in incoming:
        if paper['key'] not in papers and paper['doi']:
            # Filling a missing DOI changes the key, but not the original record.
            matches = [old for old in papers.values() if not old['doi']
                       and compact(old['title']) == compact(paper['title'])
                       and any(same_member(new, member) for new in paper['members'] for member in old['members'])]
            if len(matches) == 1:
                old = matches[0]
                old_key = old['key']
                del papers[old_key]
                old['key'], old['doi'] = paper['key'], paper['doi']
                for member in old['members']:
                    member['doi'] = paper['doi']
                papers[paper['key']] = old
                known[paper['key']] = known.pop(old_key)
        if paper['key'] in papers:
            old = papers[paper['key']]
            old['pdf_urls'] = list(dict.fromkeys(paper['pdf_urls'] + old['pdf_urls']))
            updated = set()
            for member in paper['members']:
                matches = [offset for offset, stored in enumerate(old['members'])
                           if offset not in updated and same_member(member, stored)]
                if matches:
                    offset = matches[0]
                    stored = old['members'][offset]
                    old['members'][offset] = dict(member, position=stored['position'],
                                                  _download_record_id=stored['_download_record_id'])
                    updated.add(offset)
                else:
                    old['members'].append(dict(member, position=next_position,
                                                _download_record_id=f'entry-{next_position}'))
                    updated.add(len(old['members']) - 1)
                    next_position += 1
        else:
            if paper['filename'].casefold() in used:
                suffix = hashlib.sha256(paper['key'].encode()).hexdigest()[:10]
                paper['filename'] = safe_stem(paper['title']) + f' ({suffix}).pdf'
            if paper['filename'].casefold() in used:
                raise ValueError('Filename collision; use a fresh output directory.')
            used.add(paper['filename'].casefold())
            for member in paper['members']:
                member['position'] = next_position
                member['_download_record_id'] = f'entry-{next_position}'
                next_position += 1
            papers[paper['key']] = paper
    return list(papers.values()), requested, known


def visible_text(page):
    bounds = page.rect * page.derotation_matrix
    parts = []
    for span in page.get_texttrace():
        if span['type'] == 3 or span['opacity'] <= 0 or not bounds.intersects(span['bbox']):
            continue
        characters = span['chars']
        if not bounds.contains(span['bbox']):
            characters = [character for character in characters if bounds.intersects(character[3])]
        parts.append(''.join(chr(character[0]) for character in characters))
    return ' '.join(parts)


def validate_pdf(path, title):
    """Conservative local checks; this function runs only on the main thread."""
    path = Path(path)
    if not path.is_file():
        return dict(valid=False, reason='missing_file')
    try:
        with path.open('rb') as handle:
            if handle.read(4) != b'%PDF':
                return dict(valid=False, reason='invalid_pdf')
        with pymupdf.open(path) as document:
            if not document.is_pdf or document.needs_pass or not document.page_count:
                return dict(valid=False, reason='invalid_or_encrypted_pdf')
            first = visible_text(document[0])
            blank_pages = 0
            for page in document:
                text = visible_text(page).strip()
                if len(text) < 50 and not page.get_images() and not page.get_drawings():
                    blank_pages += 1
            result = dict(valid=False, pages=document.page_count, bytes=path.stat().st_size,
                          blank_pages=blank_pages, metadata_title=document.metadata.get('title', ''))
            if blank_pages and blank_pages >= math.ceil(document.page_count / 2):
                return dict(result, reason='missing_body_pages')
            if not compact(first):
                return dict(result, reason='title_unverifiable')
            if compact(title) not in compact(first[:7000]):
                return dict(result, reason='title_mismatch')
            return dict(result, valid=True, reason='')
    except Exception as error:
        return dict(valid=False, reason='invalid_pdf', message=str(error))


def load_backend(repository, timeout):
    if repository:
        root = Path(repository).expanduser().resolve()
        if not (root / 'scihub_dl/downloader.py').is_file():
            raise ValueError('--scihub-repo must point to a scihub-downloader checkout.')
        sys.path.insert(0, str(root))
    try:
        from scihub_dl.downloader import download_pdf, resolve_pdf_url
        from scihub_dl.mirrors import MirrorPool
        from scihub_dl.net import HttpSession, RateLimiter, RequestError
    except ImportError as error:
        raise ValueError('Install requirements.txt or pass --scihub-repo /path/to/scihub-downloader.') from error

    class LengthCheckedReader:
        def __init__(self, response):
            self.response = response
            self.headers = response.headers
            length = self.headers.get('Content-Length')
            self.expected = int(length) if length is not None else None
            self.received = 0

        def read(self, size=-1):
            data = self.response.read(size)
            self.received += len(data)
            if not data and self.expected is not None and self.received != self.expected:
                self.close()
                raise RequestError(f'Content length mismatch: expected {self.expected}, received {self.received}')
            return data

        def close(self):
            self.response.close()

    class Session(HttpSession):
        def __init__(self, verify):
            super().__init__(timeout=timeout)
            self.verify = verify

        def open(self, url, *, strict=False, timeout=None):
            return LengthCheckedReader(super().open(url, strict=self.verify, timeout=self.timeout))

    return dict(download=download_pdf, resolve=resolve_pdf_url, pool=MirrorPool(), limiter=RateLimiter(),
                mirror_session=Session(False), direct_session=Session(True))


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def write_reports(out, papers, results, final=False):
    report, pending = [], []
    for paper in papers:
        result = results.get(paper['key'], dict(status='pending', reason='not_attempted', attempts=[]))
        for offset, member in enumerate(paper['members']):
            row = dict(member)
            row.update(result)
            row.update(key=paper['key'], filename=paper['filename'], status=result['status'],
                       duplicate_of=paper['members'][0]['position'] if offset else None,
                       file=str(out / 'pdfs' / paper['filename']) if result['status'] == 'downloaded' else '')
            report.append(row)
            if result['status'] != 'downloaded':
                pending.append(dict(member, last_reason=result.get('reason', ''),
                                    last_attempts=result.get('attempts', [])))
    report.sort(key=lambda row: row['position'])
    pending.sort(key=lambda row: row['position'])
    downloaded = sum(result['status'] == 'downloaded' for result in results.values())
    summary = dict(input_entries=len(report), unique_papers=len(papers), duplicate_entries=len(report)-len(papers),
                   downloaded_unique=downloaded, covered_entries=len(report)-len(pending), pending_entries=len(pending))
    atomic_json(out / 'report.json', report)
    atomic_json(out / 'pending.json', pending)
    atomic_json(out / 'summary.json', summary)
    if final:
        fields = ['position', 'title', 'doi', 'status', 'file', 'duplicate_of', 'source', 'reason', 'pages', 'bytes']
        for name, rows in [('report.csv', report), ('pending.csv', [r for r in report if not r['file']])]:
            with (out / name).open('w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(rows)
    return summary


def run(args):
    entries = load_entries(json.loads(Path(args.input).expanduser().read_text(encoding='utf-8-sig')))
    out = Path(args.out).expanduser().resolve()
    # Keep names stable when previewing or retrying a subset with collisions.
    previous_path = out / 'report.json'
    previous = json.loads(previous_path.read_text(encoding='utf-8')) if previous_path.exists() else []
    papers, requested, known = merge_plan(entries, previous)
    if args.dry_run:
        print(json.dumps([dict(title=p['title'], doi=p['doi'], filename=p['filename'],
                               duplicates=len(p['members'])-1) for p in papers if p['key'] in requested], ensure_ascii=False, indent=2))
        return 0
    backend = load_backend(args.scihub_repo, args.timeout)
    out.mkdir(parents=True, exist_ok=True)
    pdfs, staging, rejected = (out / name for name in ('pdfs', '.staging', 'rejected'))
    for directory in (pdfs, staging, rejected):
        directory.mkdir(exist_ok=True)
    results = {}
    jobs = []
    for paper in papers:
        target = pdfs / paper['filename']
        checked = validate_pdf(target, paper['title'])
        if checked['valid']:
            results[paper['key']] = dict(status='downloaded', source=known.get(paper['key'], {}).get('source', 'existing'),
                                          url=known.get(paper['key'], {}).get('url', ''), reason='',
                                          attempts=known.get(paper['key'], {}).get('attempts', []), resumed=True,
                                          **{k: v for k, v in checked.items() if k != 'reason'})
        elif target.exists():
            results[paper['key']] = dict(status='pending', reason='existing_file_conflict', attempts=[])
        elif paper['key'] not in requested:
            prior = known[paper['key']]
            results[paper['key']] = dict(status='pending', reason=prior.get('reason', 'not_requested'),
                                          attempts=prior.get('attempts', []))
        else:
            sources = []
            if args.backend != 'direct' and paper['doi']:
                sources.append(('scihub', ''))
            if args.backend != 'scihub':
                sources.extend(('direct', url) for url in direct_urls(paper))
            if sources:
                jobs.append(dict(paper=paper, sources=sources, attempts=[]))
            else:
                results[paper['key']] = dict(status='pending', reason='no_usable_source', attempts=[])
    write_reports(out, papers, results)
    cancel = threading.Event()

    def fetch(job):
        source, url = job['sources'].pop(0)
        attempt = dict(source=source, url=url)
        candidate = staging / (hashlib.sha256(job['paper']['key'].encode()).hexdigest() + '.pdf')
        if source == 'scihub':
            resolved = backend['resolve'](job['paper']['doi'], pool=backend['pool'],
                                          session=backend['mirror_session'], limiter=backend['limiter'], cancel_event=cancel)
            if not resolved.ok:
                return job, candidate, dict(attempt, status=resolved.reason)
            url = resolved.pdf_url
            attempt['url'] = url
        session = backend['mirror_session'] if source == 'scihub' else backend['direct_session']
        download = backend['download'](url, candidate, session=session, cancel_event=cancel)
        return job, candidate, dict(attempt, status='received' if download.ok else download.reason or 'cancelled',
                                    message=download.error)

    interrupted = False
    executor = ThreadPoolExecutor(max_workers=args.concurrency)
    futures = {executor.submit(fetch, job): job for job in jobs}
    try:
        with (out / 'events.jsonl').open('a', encoding='utf-8') as events:
            while futures:
                completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    job = futures.pop(future)
                    try:
                        job, candidate, attempt = future.result()
                    except Exception as error:
                        candidate = None
                        attempt = dict(status='download_error', message=str(error))
                    paper = job['paper']
                    checked = validate_pdf(candidate, paper['title']) if candidate and attempt['status'] == 'received' else None
                    if checked:
                        attempt.update(status='accepted' if checked['valid'] else checked['reason'])
                    job['attempts'].append(attempt)
                    events.write(json.dumps(dict(key=paper['key'], title=paper['title'], **attempt), ensure_ascii=False) + '\n')
                    events.flush()
                    if checked and checked['valid']:
                        target = pdfs / paper['filename']
                        if target.exists():
                            raise FileExistsError(f'Refusing to replace existing file: {target}')
                        candidate.replace(target)
                        results[paper['key']] = dict(status='downloaded', source=attempt['source'], url=attempt['url'],
                                                      reason='', attempts=job['attempts'], **{k: v for k, v in checked.items() if k != 'reason'})
                    else:
                        if candidate and candidate.exists():
                            suffix = hashlib.sha256((paper['key'] + str(len(job['attempts'])) + candidate.name).encode()).hexdigest()[:12]
                            destination = rejected / f'{candidate.stem}-{suffix}.pdf'
                            number = 1
                            while destination.exists():
                                destination = rejected / f'{candidate.stem}-{suffix}-{number}.pdf'
                                number += 1
                            candidate.replace(destination)
                        if job['sources']:
                            futures[executor.submit(fetch, job)] = job
                            continue
                        results[paper['key']] = dict(status='pending', reason=attempt['status'], attempts=job['attempts'])
                    print(f"[{len(results)}/{len(papers)}] {results[paper['key']]['status']}: {paper['title']}", flush=True)
                    write_reports(out, papers, results)
    except KeyboardInterrupt:
        interrupted = True
        cancel.set()
        for future in futures:
            future.cancel()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        summary = write_reports(out, papers, results, final=True)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 130 if interrupted else (1 if summary['pending_entries'] else 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='JSON array or object containing entries[].')
    parser.add_argument('--out', required=True, help='Output folder for pdfs/ and reports.')
    parser.add_argument('--backend', choices=['auto', 'scihub', 'direct'], default='auto',
                        help='auto: Sci-Hub, then the supplied public PDF URLs; direct: supplied URLs/arXiv only.')
    parser.add_argument('--scihub-repo', help='Optional path to an existing scihub-downloader checkout.')
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--dry-run', action='store_true', help='Preview names without downloads or file writes.')
    args = parser.parse_args(argv)
    if not 1 <= args.concurrency <= 8 or not 0 < args.timeout <= 120:
        parser.error('concurrency must be 1–8 and timeout must be greater than 0 and at most 120 seconds.')
    try:
        return run(args)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
