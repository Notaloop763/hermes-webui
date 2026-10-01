"""Profile transitions own the complete Skills DOM and async continuations."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_production_skills_detail_and_mutation_ownership():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is unavailable')
    probe = subprocess.run([node, '-e', "require('playwright')"], capture_output=True)
    if probe.returncode:
        pytest.skip('Node Playwright is unavailable; set NODE_PATH to its package directory')
    proc = subprocess.run([node, str(ROOT / 'tests/skills_detail_ownership.cjs'), str(ROOT)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    reports = json.loads(proc.stdout)
    assert len(reports) == 127
    for report in reports:
        if 'happy' in report:
            assert report['editing'] is report['pre'] is None, report
            if report['happy'] == 'save':
                assert report['mode'] == 'read', report
                assert 'saved private content' in report['body'], report
                assert ['toast', 'skill_updated'] in report['events'], report
            elif report['happy'] == 'delete-cancel':
                assert report['mode'] == 'read', report
                assert 'linked private content' in report['body'], report
            else:
                assert report['mode'] == 'empty', report
                assert report['detail'] is None, report
                assert report['body'] == '', report
                assert report['data'] == [], report
                assert ['toast', 'skill_deleted'] in report['events'], report
            continue
        if 'scenario' in report:
            assert report['after'] == report['before'], report
            if report['scenario'].startswith('transport-'):
                assert report['fetches'] == 1, report
            continue
        immediate = report['immediate']
        assert immediate['data'] is None, report
        assert immediate['detail'] is None, report
        assert immediate['pre'] is None, report
        assert immediate['editing'] is None, report
        assert immediate['mode'] == 'empty', report
        assert immediate['collapsed'] == [], report
        assert immediate['list'] == immediate['body'] == immediate['title'] == '', report
        assert immediate['bodyDisplay'] == 'none', report
        assert immediate['emptyDisplay'] == '', report
        assert all(display == 'none' for display in immediate['buttons']), report
        if report['oldFirst']:
            assert report['neutral'] == immediate, report
        assert report['after'] == report['before'], report
        assert report['after']['data'][0]['disabled'] is True, report
        expected = 'A-new' if report['returnA'] else 'B'
        assert expected + ' private content' in report['after']['body'], report
        assert report['extraRequests'] == 0, report
