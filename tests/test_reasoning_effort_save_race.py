"""Drive real dropdown/command callbacks with delayed reasoning responses."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const root = process.argv[2];
const [entry, change] = JSON.parse(process.argv[3]);
function el() {
  return {style:{}, dataset:{}, textContent:'', value:'',
    classList:{toggle(){},remove(){},add(){},contains(){return false}},
    setAttribute(){}, querySelectorAll(){return []}};
}
const els = Object.fromEntries(['modelSelect','composerReasoningWrap',
  'composerReasoningLabel','composerReasoningChip','composerReasoningDropdown',
  'composerMobileReasoningLabel','composerMobileReasoningAction'].map(k=>[k,el()]));
const clicks = [];
const requests = [];
const context = vm.createContext({
  S:{activeProfile:'default',session:{session_id:'A',model:'gpt-5',model_provider:'openai',profile:'default'}},
  window:{}, URLSearchParams,
  document:{addEventListener(type, fn){if(type==='click') clicks.push(fn);}},
  $:id=>els[id]||null, showToast(){},
  api(url, options){
    const request = {url,options}; requests.push(request);
    return {then(fn){request.ok=fn;return {catch(fn){request.fail=fn;}};}};
  }
});
const ui = fs.readFileSync(root+'/static/ui.js','utf8');
vm.runInContext(ui.slice(ui.indexOf('// ── Reasoning effort chip'),
  ui.indexOf('// ── Session toolsets chip')),context);
const commands = fs.readFileSync(root+'/static/commands.js','utf8');
vm.runInContext(commands.slice(commands.indexOf('function cmdReasoning('),
  commands.indexOf('function cmdVoice(')),context);
const run = code=>vm.runInContext(code,context);
const status = effort=>({reasoning_effort:effort,supported_efforts:['low','high']});
run('syncReasoningChip()'); requests.at(-1).ok(status('low'));
function pick(effort) {
  if(entry==='dropdown') {
    const option={dataset:{effort}};
    clicks[0]({target:{closest(selector){return selector==='.reasoning-option'?option:null;}}});
  } else run(`cmdReasoning('${effort}')`);
  return requests.filter(r=>r.options?.method==='POST').at(-1);
}
const post = pick('high');
// A newer pick in the same chat resolves first; the older save lands last.
if(change==='newer_save') pick('low').ok(status('low'));
if(change==='newer_save_failed') {
  pick('low').fail(new Error('boom'));
}
let oldGet;
if(change==='old_get') {
  run('fetchReasoningChip()'); oldGet=requests.at(-1);
}
if(!post) throw new Error('POST not dispatched');
const payload = JSON.parse(post.options.body);
if(payload.session_id!=='A'||payload.model!=='gpt-5'||payload.provider!=='openai')
  throw new Error('wrong request owner');
if(change==='session') run("S.session={...S.session,session_id:'B'}");
if(change==='model') run("S.session.model='gpt-5.5'");
if(change==='provider') run("S.session.model_provider='custom:test'");
if(change==='profile') run("S.activeProfile='work'");
if(change==='session'||change==='model'||change==='provider') {
  run('syncReasoningChip()'); requests.at(-1).ok(status('low'));
}
post.ok(status('high'));
if(oldGet) oldGet.ok(status('low'));
const afterSave = els.composerReasoningLabel.textContent;
const beforeSync = requests.length;
run('syncReasoningChip()');
if(change==='newer_save_failed') {
  // The failed newest save must not leave a cached chip: resync reads the server.
  if(requests.length!==beforeSync+1) throw new Error('no refetch after failed save');
  requests.at(-1).ok(status('low'));
}
const afterSync = els.composerReasoningLabel.textContent;
// Returning to A must read its persisted setting instead of borrowing B's cache.
if(change==='session') {
  run("S.session.session_id='A';syncReasoningChip()");
  requests.at(-1).ok(status('high'));
}
process.stdout.write(JSON.stringify({afterSave,afterSync,
  mobile:els.composerMobileReasoningLabel.textContent,
  restored:els.composerReasoningLabel.textContent}));
"""


@pytest.mark.parametrize("entry", ["dropdown", "command"])
@pytest.mark.parametrize(
    "change",
    ["session", "model", "provider", "profile", "unchanged", "old_get",
     "newer_save", "newer_save_failed"],
)
def test_delayed_save_keeps_its_context(tmp_path, entry, change):
    driver = tmp_path / "driver.js"
    driver.write_text(DRIVER)
    result = subprocess.run(
        [NODE, str(driver), str(ROOT), json.dumps([entry, change])],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    expected = "High" if change in ("unchanged", "old_get") else "Low"
    assert out["afterSave"] == expected
    assert out["afterSync"] == expected
    restored = "High" if change == "session" else expected
    assert out["restored"] == restored
    assert out["mobile"] == restored
