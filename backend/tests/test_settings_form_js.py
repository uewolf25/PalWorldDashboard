"""ゲーム設定フォームの入力挙動を、node で実際に動かして確かめる。

Issue #13: 1文字入力すると入力前の状態に戻り、続けて打てない。
原因は入力のたびに一覧を作り直し、入力中の要素ごと破棄していたこと。
フォーカスとカーソル位置が失われるので、2文字目以降が行き場を失う。

ブラウザは使えないので、最小限の DOM を用意して index.html の中の
JavaScript をそのまま読み込み、入力イベントを流して確認する。
node が無い環境では丸ごとスキップする。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node が無いのでフロントエンドの検証を飛ばす"
)


def _page_script() -> str:
    html = INDEX.read_text(encoding="utf-8")
    return re.search(r"<script>(.*?)</script>", html, re.S).group(1)


# 画面スクリプトを動かすのに要る最小限の DOM。
# 本物のブラウザではなく、要素の生成・付け外し・イベント配送だけを再現する。
DOM_SHIM = """
class Node {
  constructor(tag) {
    this.tagName = (tag || "").toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    this.style = {};
    this._listeners = {};
    this._text = "";
    this._value = "";
    this.classList = {
      _set: new Set(),
      add: (...c) => c.forEach(x => this.classList._set.add(x)),
      remove: (...c) => c.forEach(x => this.classList._set.delete(x)),
      contains: (c) => this.classList._set.has(c),
      toggle: (c, on) => { on ? this.classList._set.add(c) : this.classList._set.delete(c); },
    };
  }
  get className() { return Array.from(this.classList._set).join(" "); }
  set className(v) {
    this.classList._set = new Set(String(v).split(/\\s+/).filter(Boolean));
  }
  get value() { return this._value; }
  set value(v) { this._value = String(v); }
  get type() { return this.attributes.type || ""; }
  set type(v) { this.attributes.type = v; }
  get min() { return this.attributes.min; } set min(v) { this.attributes.min = v; }
  get max() { return this.attributes.max; } set max(v) { this.attributes.max = v; }
  get step() { return this.attributes.step; } set step(v) { this.attributes.step = v; }
  get checked() { return !!this._checked; } set checked(v) { this._checked = !!v; }
  get firstChild() { return this.childNodes[0] || null; }
  get childElementCount() { return this.childNodes.length; }
  get textContent() {
    if (this._text) return this._text;
    return this.childNodes.map(c => c.textContent).join("");
  }
  set textContent(v) { this._text = String(v); this.childNodes = []; }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === "value") this._value = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  appendChild(c) { c.parentNode = this; this.childNodes.push(c); return c; }
  removeChild(c) {
    const i = this.childNodes.indexOf(c);
    if (i >= 0) this.childNodes.splice(i, 1);
    c.parentNode = null;
    return c;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  removeEventListener() {}
  dispatch(type, ev) { for (const fn of (this._listeners[type] || [])) fn(ev || {}); }
  querySelector() { return null; }
  // 部分木に自分が含まれているか（作り直されて捨てられたかの判定に使う）
  isConnected(root) {
    let n = this;
    while (n) { if (n === root) return true; n = n.parentNode; }
    return false;
  }
  find(pred) {
    if (pred(this)) return this;
    for (const c of this.childNodes) { const r = c.find(pred); if (r) return r; }
    return null;
  }
  findAll(pred, out) {
    out = out || [];
    if (pred(this)) out.push(this);
    for (const c of this.childNodes) c.findAll(pred, out);
    return out;
  }
}

const _byId = {};
globalThis.document = {
  createElement: (t) => new Node(t),
  createTextNode: (t) => { const n = new Node("#text"); n._text = String(t); return n; },
  getElementById: (id) => (_byId[id] ||= new Node("div")),
  addEventListener() {}, removeEventListener() {},
};
globalThis.location = { protocol: "http:", host: "localhost", href: "http://localhost/" };
globalThis.WebSocket = class { constructor() {} close() {} };
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.setTimeout = (fn) => { return 0; };
globalThis.confirm = () => true;
globalThis.prompt = () => "";
globalThis.alert = () => {};

// fetch は使わせない。状態はテスト側で直接組み立てる
globalThis.fetch = async () => { throw new Error("fetch は使いません"); };
"""


def run_js(body: str) -> dict:
    """画面スクリプト + 検証コードを node で実行し、結果の JSON を返す。"""
    script = DOM_SHIM + "\n" + _page_script() + "\n" + body
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node の実行に失敗しました:\n{proc.stderr[-3000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# 「サーバ名」の入力欄を持つ最小のフォームを組み立てて返す共通部分
SETUP = """
const view = document.getElementById("view");
state.config = { restart_announce_template: "", stop_announce_template: "", notice_offsets: [30] };

// load() は fetch を使うので呼ばない。描画に必要な状態だけ直接入れる
const fields = {
  categories: [{ key: "server", label: "サーバ基本", fields: [
    { name: "ServerName", label: "サーバ名", type: "string", category: "server",
      help: "", choices: [], min: null, max: null, step: null,
      secret: false, known: true, locked: false, overrides_others: false,
      value: "Old", raw: '"Old"', default: null, default_known: false },
    { name: "ExpRate", label: "経験値倍率", type: "float", category: "server",
      help: "", choices: [], min: 0, max: 20, step: 0.1,
      secret: false, known: true, locked: false, overrides_others: false,
      value: 1.0, raw: "1.000000", default: 1.0, default_known: true },
  ]}],
  available: [], discovered: [], server_running: false, pending_total: 0,
};
"""


def test_typing_does_not_destroy_the_input():
    """Issue #13 の本体: 入力のたびに要素が作り直されないこと。"""
    result = run_js(SETUP + """
renderGameSettings(view);
// 描画に必要な内部状態を入れて一覧を作る
setFormStateForTest(fields);

const grid = view.find(n => n.classList.contains("fieldgrid"));
const input = grid.find(n => n.tagName === "INPUT" && n.type === "text");

// 1文字ずつ打つ。ブラウザなら input イベントが毎回飛ぶ
const typed = [];
for (const ch of "New") {
  input.value += ch;
  input.dispatch("input");
  typed.push({ value: input.value, alive: input.isConnected(view) });
}

console.log(JSON.stringify({
  typed,
  finalValue: input.value,
  stillInTree: input.isConnected(view),
}));
""")

    # どの打鍵のあとも同じ要素が生き残っていること（＝フォーカスが外れない）
    assert all(step["alive"] for step in result["typed"]), result["typed"]
    assert result["stillInTree"] is True
    assert result["finalValue"] == "OldNew"


def test_typed_value_is_kept_in_the_model():
    """打った内容が保存対象として溜まること。"""
    result = run_js(SETUP + """
renderGameSettings(view);
setFormStateForTest(fields);
const grid = view.find(n => n.classList.contains("fieldgrid"));
const input = grid.find(n => n.tagName === "INPUT" && n.type === "text");

input.value = "";
input.dispatch("input");
for (const ch of "うちのサーバ") { input.value += ch; input.dispatch("input"); }

console.log(JSON.stringify({ payload: getEditsForTest() }));
""")
    assert result["payload"]["ServerName"] == "うちのサーバ"


def test_badge_updates_while_typing():
    """要素は作り直さないが、「変更」バッジは追従すること。"""
    result = run_js(SETUP + """
renderGameSettings(view);
setFormStateForTest(fields);
const grid = view.find(n => n.classList.contains("fieldgrid"));
const input = grid.find(n => n.tagName === "INPUT" && n.type === "text");
const box = input.parentNode;

const before = box.classList.contains("changed");
input.value = "Changed";
input.dispatch("input");
const after = box.classList.contains("changed");
const badge = box.find(n => n.classList.contains("tag-changed"));

// 元の値に戻すとバッジも消えること
input.value = "Old";
input.dispatch("input");
const reverted = box.classList.contains("changed");

console.log(JSON.stringify({ before, after, hasBadge: !!badge, reverted }));
""")
    assert result["before"] is False
    assert result["after"] is True
    assert result["hasBadge"] is True
    assert result["reverted"] is False


def test_partial_number_input_is_not_swallowed():
    """"1." のような入力途中でも、打った文字が消えないこと。"""
    result = run_js(SETUP + """
renderGameSettings(view);
setFormStateForTest(fields);
const grid = view.find(n => n.classList.contains("fieldgrid"));
const num = grid.find(n => n.tagName === "INPUT" && n.type === "number");

const steps = [];
for (const ch of "2.5") {
  num.value += ch;
  num.dispatch("input");
  steps.push({ shown: num.value, alive: num.isConnected(view) });
}
console.log(JSON.stringify({ steps, payload: getEditsForTest() }));
""")
    # 画面に出ている文字列は打った通りのまま
    assert [s["shown"] for s in result["steps"]] == ["12", "12.", "12.5"]
    assert all(s["alive"] for s in result["steps"])
    assert result["payload"]["ExpRate"] == 12.5


def test_locked_field_is_read_only():
    """locked 項目は入力を受け付けないこと。"""
    result = run_js(SETUP.replace('"locked": false', '"locked": false') + """
fields.categories[0].fields[0].locked = true;
renderGameSettings(view);
setFormStateForTest(fields);
const grid = view.find(n => n.classList.contains("fieldgrid"));
const input = grid.find(n => n.tagName === "INPUT" && n.attributes.readonly);
console.log(JSON.stringify({ readonly: !!input, payload: getEditsForTest() }));
""")
    assert result["readonly"] is True
    assert result["payload"] == {}
