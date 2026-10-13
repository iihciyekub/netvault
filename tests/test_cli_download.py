import hashlib
import threading
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from netvault.cli import user
from netvault.cli.http import ApiError


def detail(doi, content=b'%PDF-example', name='paper.pdf'):
    return dict(doi=doi, original_name=name, size=len(content),
                sha256=hashlib.sha256(content).hexdigest())


@pytest.fixture
def download_auth(monkeypatch):
    monkeypatch.setattr(user, 'ensure_logged_in', lambda: None)
    monkeypatch.setattr(user, 'server_url', lambda: 'https://vault.example')
    monkeypatch.setattr(user, 'auth_headers', lambda: {'Authorization': 'Bearer test'})


def test_download_batches_and_starts_before_next_lookup_completes(
    tmp_path, monkeypatch, download_auth,
):
    # The second lookup cannot finish until an actual first-batch download starts.
    # Waiting for all metadata first would fail this test.
    monkeypatch.setattr(user, 'DOWNLOAD_LOOKUP_BATCH_SIZE', 2)
    first_download = threading.Event()
    calls = []
    dois = [f'10.1234/paper-{i}' for i in range(5)]
    source = tmp_path / 'dois.txt'
    source.write_text('\n'.join(dois + [dois[0]]))

    def api_post(path, json):
        assert path == '/pdfs/exists'
        calls.append(json['doi'])
        if len(calls) == 2:
            assert first_download.wait(3), 'download waited for the full DOI list'
        return {'existing_doi': {doi: detail(doi) for doi in json['doi']}}

    def run_plan(plan, base_url, headers, stop=None):
        first_download.set()
        plan.destination.write_bytes(b'%PDF-example')
        return 'downloaded'

    monkeypatch.setattr(user, 'api_post', api_post)
    monkeypatch.setattr(user, 'api_get', lambda *a, **kw: pytest.fail('per-DOI detail request'))
    monkeypatch.setattr(user, 'run_download_plan', run_plan)
    target = tmp_path / 'downloads'
    result = CliRunner().invoke(user.app, [
        'download', '--file', str(source), '--to', str(target), '--workers', '2',
    ])
    assert result.exit_code == 0, result.output
    assert [len(batch) for batch in calls] == [2, 2, 1]
    assert '5 downloaded' in result.output
    assert len(list(target.glob('*.pdf'))) == 5  # Safe allocation of colliding names.


def test_download_bulk_missing_and_existing_files(tmp_path, monkeypatch, download_auth):
    available, existing, missing = '10.1234/new', '10.1234/existing', '10.1234/missing'
    (tmp_path / 'existing.pdf').write_bytes(b'%PDF-example')
    monkeypatch.setattr(user, 'api_post', lambda path, json: {'existing_doi': {
        available: detail(available, name='new.pdf'),
        existing: detail(existing, name='existing.pdf'),
    }})
    plans = []
    monkeypatch.setattr(user, 'run_download_plan', lambda plan, *args: plans.append(plan) or 'downloaded')
    result = CliRunner().invoke(user.app, [
        'download', available, existing, missing, '--to', str(tmp_path),
    ])
    assert result.exit_code == 1
    assert '1 downloaded' in result.output
    assert '1 skipped' in result.output
    assert '1 failed' in result.output
    assert f'failed: {missing}: 404' in result.output
    assert [p.doi for p in plans] == [available]


def test_failed_lookup_batch_keeps_other_downloads(tmp_path, monkeypatch, download_auth):
    monkeypatch.setattr(user, 'DOWNLOAD_LOOKUP_BATCH_SIZE', 1)
    def lookup(path, json):
        doi = json['doi'][0]
        if doi == '10.1234/unavailable':
            raise ApiError(503, '503: temporarily unavailable')
        return {'existing_doi': {doi: detail(doi)}}
    monkeypatch.setattr(user, 'api_post', lookup)
    monkeypatch.setattr(user, 'run_download_plan', lambda *args: 'downloaded')
    result = CliRunner().invoke(user.app, [
        'download', '10.1234/first', '10.1234/unavailable', '10.1234/last',
        '--to', str(tmp_path),
    ])
    assert result.exit_code == 1
    assert '2 downloaded' in result.output
    assert '1 failed' in result.output


@pytest.mark.parametrize('offset,status', [(0, 200), (4, 206), (4, 200)])
def test_download_stream_verifies_new_and_resumed_bytes(tmp_path, monkeypatch, offset, status):
    content = b'%PDF-full-content'
    row = detail('10.1234/test', content)
    target = tmp_path / 'paper.pdf'
    part = user.download_part_path(target)
    if offset:
        part.write_bytes(content[:offset])
    plan = user.DownloadPlan(row['doi'], row['original_name'], row['size'], target, part,
                             row['sha256'], offset)
    class Response:
        status_code = status
        ok = True
        headers = {'Content-Range': f'bytes {offset}-{len(content)-1}/{len(content)}'}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, chunk_size):
            body = content[offset:] if status == 206 else content
            yield body[:3]
            yield b''
            yield body[3:]
    calls = []
    def get(url, **kwargs):
        calls.append(kwargs)
        return Response()
    monkeypatch.setattr(user, 'http_session', lambda: SimpleNamespace(get=get))
    monkeypatch.setattr(user, 'file_sha256', lambda *a: pytest.fail('second full-file hash read'))
    result = user.run_download_plan(plan, 'https://vault.example', {})
    assert result == ('resumed' if status == 206 else 'downloaded')
    assert target.read_bytes() == content
    assert not part.exists()
    assert calls[0]['headers'] == ({'Range': f'bytes={offset}-'} if offset else {})


@pytest.mark.parametrize('content,error', [(b'corrupt!', 'checksum mismatch'), (b'short', 'incomplete')])
def test_download_never_finalizes_invalid_bytes(tmp_path, monkeypatch, content, error):
    expected = b'%PDF-ok!'
    target = tmp_path / 'paper.pdf'
    part = user.download_part_path(target)
    plan = user.DownloadPlan('10.1234/test', 'paper.pdf', len(expected), target, part,
                             hashlib.sha256(expected).hexdigest())
    class Response:
        status_code = 200
        ok = True
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, chunk_size): yield content
    monkeypatch.setattr(user, 'http_session', lambda: SimpleNamespace(get=lambda *a, **kw: Response()))
    with pytest.raises(RuntimeError, match=error):
        user.run_download_plan(plan, 'https://vault.example', {})
    assert not target.exists()
    assert part.read_bytes() == content


def test_download_lookup_retries_transient_error(monkeypatch):
    calls = []
    def lookup(dois):
        calls.append(dois)
        if len(calls) < 3:
            raise ApiError(503, 'temporarily unavailable')
        return {dois[0]: detail(dois[0])}
    delays = []
    monkeypatch.setattr(user, 'get_existing_pdfs_by_doi', lookup)
    monkeypatch.setattr(user, 'sleep', delays.append)
    assert '10.1234/test' in user.fetch_download_batch(['10.1234/test'])
    assert len(calls) == 3
    assert delays == [0.4, 0.8]


def test_download_lookup_does_not_retry_auth_error(monkeypatch):
    def lookup(dois):
        raise ApiError(401, 'login required')
    monkeypatch.setattr(user, 'get_existing_pdfs_by_doi', lookup)
    monkeypatch.setattr(user, 'sleep', lambda delay: pytest.fail('retried authentication error'))
    with pytest.raises(ApiError, match='login required'):
        user.fetch_download_batch(['10.1234/test'])


def test_download_rejects_wrong_resume_range(tmp_path, monkeypatch):
    target = tmp_path / 'paper.pdf'
    part = user.download_part_path(target)
    part.write_bytes(b'%PDF')
    plan = user.DownloadPlan('10.1234/test', 'paper.pdf', 8, target, part, 'a' * 64, 4)
    class Response:
        status_code = 206
        ok = True
        headers = {'Content-Range': 'bytes 0-3/8'}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, chunk_size): pytest.fail('consumed an invalid resumed response')
    monkeypatch.setattr(user, 'http_session', lambda: SimpleNamespace(get=lambda *a, **kw: Response()))
    with pytest.raises(RuntimeError, match='invalid Content-Range'):
        user.run_download_plan(plan, 'https://vault.example', {})
    assert part.read_bytes() == b'%PDF'
    assert not target.exists()


def test_interrupted_stream_can_resume_on_next_run(tmp_path, monkeypatch):
    content = b'%PDF-complete-article'
    target = tmp_path / 'paper.pdf'
    part = user.download_part_path(target)
    checksum = hashlib.sha256(content).hexdigest()
    stop = threading.Event()
    class InterruptedResponse:
        status_code = 200
        ok = True
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, chunk_size):
            yield content[:5]
            stop.set()
            yield content[5:]
    monkeypatch.setattr(user, 'http_session', lambda: SimpleNamespace(
        get=lambda *a, **kw: InterruptedResponse()))
    plan = user.DownloadPlan('10.1234/test', 'paper.pdf', len(content), target, part, checksum)
    with pytest.raises(InterruptedError):
        user.run_download_plan(plan, 'https://vault.example', {}, stop)
    assert part.read_bytes() == content[:5]
    assert not target.exists()

    class ResumedResponse(InterruptedResponse):
        status_code = 206
        headers = {'Content-Range': f'bytes 5-{len(content)-1}/{len(content)}'}
        def iter_content(self, chunk_size): yield content[5:]
    monkeypatch.setattr(user, 'http_session', lambda: SimpleNamespace(
        get=lambda *a, **kw: ResumedResponse()))
    destination, partial, offset, complete = user.plan_download_destination(
        tmp_path, 'paper.pdf', len(content), set(), expected_sha256=checksum,
    )
    assert offset == 5 and not complete
    resumed = user.DownloadPlan('10.1234/test', 'paper.pdf', len(content), destination,
                                partial, checksum, offset)
    assert user.run_download_plan(resumed, 'https://vault.example', {}) == 'resumed'
    assert target.read_bytes() == content


def test_ctrl_c_cancels_queued_downloads_and_keeps_partial(tmp_path, monkeypatch, download_auth):
    started = threading.Event()
    transferred = []
    monkeypatch.setattr(user, 'api_post', lambda path, json: {'existing_doi': {
        doi: detail(doi, name=f'{doi.rsplit("/", 1)[-1]}.pdf') for doi in json['doi']
    }})
    def run_plan(plan, base_url, headers, stop):
        if stop.is_set():
            raise InterruptedError('cancelled before network request')
        transferred.append(plan.doi)
        plan.part_path.write_bytes(b'%PDF')
        started.set()
        assert stop.wait(3)
        raise InterruptedError('partial kept')
    def wait_for_interrupt(*args, **kwargs):
        assert started.wait(3)
        raise KeyboardInterrupt()
    monkeypatch.setattr(user, 'run_download_plan', run_plan)
    monkeypatch.setattr(user, 'wait', wait_for_interrupt)
    result = CliRunner().invoke(user.app, [
        'download', '10.1234/first', '10.1234/second', '10.1234/third',
        '--to', str(tmp_path), '--workers', '1',
    ])
    assert result.exit_code == 130, result.output
    assert 'Run the same download command to continue' in ' '.join(result.output.split())
    assert transferred == ['10.1234/first']
    assert (tmp_path / 'first.pdf.part').read_bytes() == b'%PDF'
    assert not list(tmp_path.glob('*.pdf'))
