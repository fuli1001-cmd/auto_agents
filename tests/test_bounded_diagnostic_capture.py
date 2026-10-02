import json

from auto_agents.diagnostic_output import OutputCapture, TRUNCATED_CAPTURE


def test_capture_is_bounded_after_redaction_and_keeps_utf8_tail(tmp_path, monkeypatch):
    monkeypatch.setattr('auto_agents.diagnostic_output.MAX_CAPTURE_BYTES', 8192)
    failures = []
    capture = OutputCapture(tmp_path / 'output', {}, register=lambda *args:None, failed=failures.append)
    capture.start('test', {'API_KEY':'secret-value'})
    text = 'large diagnostic line 中文\n' * 1000 + 'API_KEY=secret-value\nlast result 完成\n'
    capture('stdout', text)
    capture.finish(ok=False)
    retained = (capture.root / 'stdout.txt').read_bytes()
    assert len(retained) <= 8192 and retained.startswith(TRUNCATED_CAPTURE)
    decoded = retained.decode()
    assert 'secret-value' not in decoded and 'last result 完成' in decoded
    assert json.loads((capture.root / 'attempt.json').read_text())['truncated_streams'] == ['stdout']
    assert not failures
