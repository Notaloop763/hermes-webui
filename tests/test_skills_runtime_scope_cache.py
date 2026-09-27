"""Runtime coverage for incomplete profile-scoped skill listings.

An unavailable external-skill scope still returns safe profile-local skills. The
browser must keep that result usable for the current interaction while retrying
the incomplete listing when the panel or cron form is opened again.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PANELS_JS_PATH = ROOT / "static" / "panels.js"
PANELS_JS = PANELS_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

NODE_PRELUDE = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');

function extract(name){
  const re = new RegExp('(async\\s+)?function\\s+' + name + '\\s*\\(');
  const match = re.exec(src);
  if(!match) throw new Error(name + ' not found');
  const start = match.index;
  let i = src.indexOf('{', start);
  let depth = 0;
  while(i < src.length){
    const ch = src[i];
    if(ch === '{') depth += 1;
    else if(ch === '}') {
      depth -= 1;
      if(depth === 0) break;
    }
    i += 1;
  }
  if(depth !== 0) throw new Error(name + ' parse failed');
  return src.slice(start, i + 1);
}
"""


def _run_node(script: str) -> dict:
    assert NODE is not None
    proc = subprocess.run(
        [NODE, "-e", script, str(PANELS_JS_PATH)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node probe failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_unavailable_skills_remain_usable_and_retry_after_recovery():
    """Toggle uses the local working set, and the next load replaces it after recovery."""
    script = (
        NODE_PRELUDE
        + r"""
let _skillsData = null;
let _skillsDataIncomplete = false;
const _collapsedCats = new Set();
const rendered = [];
let skillsGets = 0;

global.window = global;
window.invalidateSlashSkillCaches = () => {};
global.$ = () => ({innerHTML: ''});
global.esc = (value) => String(value);
global.t = (key) => key;
global.setStatus = () => {};
global.renderSkills = (skills) => rendered.push(skills.map((s) => ({...s})));
global.api = async (path) => {
  if(path === '/api/skills') {
    skillsGets += 1;
    if(skillsGets === 1) {
      return {runtime_scope:'unavailable', skills:[{name:'local-one', disabled:false}]};
    }
    return {runtime_scope:'profile', skills:[
      {name:'local-one', disabled:true},
      {name:'external-one', disabled:false},
    ]};
  }
  if(path === '/api/skills/toggle') return {ok:true};
  throw new Error('unexpected api call: ' + path);
};

eval(extract('loadSkills'));
eval(extract('toggleSkill'));

(async () => {
  await loadSkills();
  const firstIncomplete = _skillsDataIncomplete;
  await toggleSkill('local-one', true);
  const afterToggle = rendered[rendered.length - 1];
  await loadSkills();
  console.log(JSON.stringify({
    firstIncomplete,
    afterToggle,
    skillsGets,
    finalIncomplete:_skillsDataIncomplete,
    finalNames:_skillsData.map((s) => s.name),
  }));
})();
"""
    )
    result = _run_node(script)

    assert result["firstIncomplete"] is True
    assert result["afterToggle"] == [{"name": "local-one", "disabled": True}]
    assert result["skillsGets"] == 2
    assert result["finalIncomplete"] is False
    assert result["finalNames"] == ["local-one", "external-one"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_cron_picker_uses_local_skills_and_retries_incomplete_result():
    """The current cron form can search local skills while a later form refreshes them."""
    script = (
        NODE_PRELUDE
        + r"""
let _cronSelectedSkills = [];
let _cronSkillsCache = null;
let _cronSkillsCacheIncomplete = false;
let skillsGets = 0;

const search = {value:'', style:{}, oninput:null};
const dropdown = {
  children:[], style:{}, _html:'',
  set innerHTML(value){this._html=value;if(value==='')this.children=[];},
  get innerHTML(){return this._html;},
  appendChild(child){this.children.push(child);},
};
global.$ = (id) => id === 'cronFormSkillSearch' ? search :
  (id === 'cronFormSkillDropdown' ? dropdown : null);
global.document = {createElement: () => ({className:'', textContent:'', onclick:null})};
global.api = async (path) => {
  if(path !== '/api/skills') throw new Error('unexpected api call: ' + path);
  skillsGets += 1;
  if(skillsGets === 1) {
    return {runtime_scope:'unavailable', skills:[{name:'local-one', category:null}]};
  }
  return {runtime_scope:'profile', skills:[
    {name:'local-one', category:null},
    {name:'external-one', category:'shared'},
  ]};
};

eval(extract('_bindCronSkillPicker'));
eval(extract('_loadCronSkills'));

(async () => {
  _loadCronSkills(true);
  await new Promise((resolve) => setImmediate(resolve));
  search.value = 'local';
  search.oninput();
  const firstOptions = dropdown.children.map((item) => item.textContent);
  const firstIncomplete = _cronSkillsCacheIncomplete;

  _loadCronSkills();
  await new Promise((resolve) => setImmediate(resolve));
  search.value = 'external';
  search.oninput();
  console.log(JSON.stringify({
    firstOptions,
    firstIncomplete,
    skillsGets,
    finalIncomplete:_cronSkillsCacheIncomplete,
    finalOptions:dropdown.children.map((item) => item.textContent),
  }));
})();
"""
    )
    result = _run_node(script)

    assert result["firstOptions"] == ["local-one"]
    assert result["firstIncomplete"] is True
    assert result["skillsGets"] == 2
    assert result["finalIncomplete"] is False
    assert result["finalOptions"] == ["external-one (shared)"]


def test_all_cron_form_entry_points_use_the_shared_scope_aware_loader():
    """Create, edit, and duplicate must not grow independent cache policies again."""
    for function_name in ("openCronCreate", "openCronEdit", "duplicateCurrentCron"):
        start = PANELS_JS.index(f"function {function_name}(")
        next_function = PANELS_JS.find("\nfunction ", start + 1)
        body = PANELS_JS[start : next_function if next_function >= 0 else None]
        assert "_loadCronSkills(" in body, f"{function_name} must use _loadCronSkills"


def test_profile_switch_invalidates_both_skill_working_sets():
    """Neither panel may reuse another profile's complete or incomplete result."""
    start = PANELS_JS.index("async function switchToProfile(")
    next_function = PANELS_JS.find("\nasync function ", start + 1)
    body = PANELS_JS[start : next_function if next_function >= 0 else None]
    for reset in (
        "_skillsData = null;",
        "_skillsDataIncomplete = false;",
        "_cronSkillsCache = null;",
        "_cronSkillsCacheIncomplete = false;",
    ):
        assert reset in body
