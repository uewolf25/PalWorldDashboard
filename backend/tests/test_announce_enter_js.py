"""Enter で確定する入力欄が、IME の変換確定で誤爆しないこと。

Issue #26: 日本語を打っている途中、変換候補を Enter で確定すると、
その時点の文字列がアナウンスとして送信されてしまう。
変換確定の Enter も keydown として飛んでくるので、composition 中かどうかを
見て弾く必要がある。

DOM の用意と node の起動は js_harness に置いてある。
"""

from __future__ import annotations

from js_harness import requires_node, run_js

pytestmark = requires_node


# アナウンス入力欄と同じ配線（onEnterKey）だけを取り出したもの
SETUP = """
const input = document.createElement("input");
let sent = 0;
onEnterKey(input, () => { sent += 1; });
"""


def test_plain_enter_sends():
    """変換を挟まない Enter は今まで通り送信すること。"""
    result = run_js(SETUP + """
input.value = "こんにちは";
input.dispatch("keydown", { key: "Enter" });
console.log(JSON.stringify({ sent }));
""")

    assert result["sent"] == 1


def test_enter_while_composing_does_not_send():
    """Issue #26 の本体: 変換確定の Enter では送信しないこと。"""
    result = run_js(SETUP + """
// 「へんかん」を変換して確定するときの Enter
input.value = "へんかん";
input.dispatch("keydown", { key: "Enter", isComposing: true });

// 確定後、あらためて押した Enter は送信になる
input.dispatch("keydown", { key: "Enter", isComposing: false });

console.log(JSON.stringify({ sent }));
""")

    assert result["sent"] == 1


def test_enter_with_keycode_229_does_not_send():
    """isComposing が立たない環境向けの保険（keyCode 229）も効くこと。"""
    result = run_js(SETUP + """
input.dispatch("keydown", { key: "Enter", keyCode: 229 });
console.log(JSON.stringify({ sent }));
""")

    assert result["sent"] == 0


def test_other_keys_do_not_send():
    """Enter 以外では何も起きないこと。"""
    result = run_js(SETUP + """
for (const key of ["a", "Escape", "Shift"]) input.dispatch("keydown", { key });
console.log(JSON.stringify({ sent }));
""")

    assert result["sent"] == 0
