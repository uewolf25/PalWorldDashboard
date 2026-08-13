"""ゲーム設定フォームの入力挙動を、node で実際に動かして確かめる。

Issue #13: 1文字入力すると入力前の状態に戻り、続けて打てない。
原因は入力のたびに一覧を作り直し、入力中の要素ごと破棄していたこと。
フォーカスとカーソル位置が失われるので、2文字目以降が行き場を失う。

DOM の用意と node の起動は js_harness に置いてある。
"""

from __future__ import annotations

import textwrap

from js_harness import requires_node, run_js

pytestmark = requires_node


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
