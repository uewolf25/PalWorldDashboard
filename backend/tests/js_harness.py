"""index.html の中の JavaScript を node で動かすための共通の足回り。

ブラウザは使えないので、最小限の DOM を用意して画面スクリプトをそのまま
読み込み、イベントを流して確認する。node が無い環境では丸ごとスキップする。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"

requires_node = pytest.mark.skipif(
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
    """画面スクリプト + 検証コードを node で実行し、結果の JSON を返す。

    スクリプトは `-e` で渡さずファイルに書き出す。Linux は argv 1個あたり
    128KB までで、画面スクリプトがそれを超えると
    `OSError: Argument list too long` になる（macOS では上限が高く通ってしまう）。
    拡張子を `.mjs` にしておけば `--input-type=module` と同じくモジュール扱いになる。
    """
    script = DOM_SHIM + "\n" + _page_script() + "\n" + body
    with tempfile.TemporaryDirectory() as tmp:
        entry = Path(tmp) / "run.mjs"
        entry.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(entry)],
            capture_output=True, text=True, timeout=60,
        )
    if proc.returncode != 0:
        raise AssertionError(f"node の実行に失敗しました:\n{proc.stderr[-3000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])
