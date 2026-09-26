"""The Skills panel must not cache a ``runtime_scope="unavailable"`` listing.

When a profile's runtime scope cannot be confirmed (e.g. a chat turn is running),
``/api/skills`` withholds that profile's external roots and reports
``runtime_scope="unavailable"``. Caching that incomplete list keeps the external
skills hidden after the scope recovers, until an unrelated reset (save, delete, or
profile switch) clears the cache. These source-level guards pin the cache writes to
the confirmed-scope case; the repo has no JS runtime harness for ``panels.js``.
"""

from pathlib import Path

PANELS_JS = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(
    encoding="utf-8"
)


def test_load_skills_does_not_cache_unavailable_scope():
    assert "if (data.runtime_scope !== 'unavailable') _skillsData = skills;" in PANELS_JS
    assert "_skillsData = data.skills || [];" not in PANELS_JS


def test_cron_skill_cache_does_not_cache_unavailable_scope():
    guarded = "if(d.runtime_scope!=='unavailable')_cronSkillsCache=d.skills||[];"
    assert PANELS_JS.count(guarded) == 3
    assert "_cronSkillsCache=d.skills||[];" not in PANELS_JS.replace(guarded, "")
