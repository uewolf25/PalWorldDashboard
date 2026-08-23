"""上部バーのアップデートバッジ（issue #30 / Phase 1）。

「気づいたら勝手に落ちていた」を無くすのがこの機能の目的なので、
**検知がどの画面からでも見えること**を画面側でも確かめておく。
"""

from __future__ import annotations

from js_harness import requires_node, run_js

pytestmark = requires_node

BASE = """
state.config = {
  supports_update: %s, pal_service_backend: "lgsm",
  auth_required: false, authenticated: true, pal_base_url: "http://127.0.0.1:8212",
};
state.status = { online: true, metrics: { uptime: 60 }, restart: {} };
state.update = %s;
buildTopbar();
const bar = document.getElementById("topbar");
const pill = bar.find(n => n.classList.contains("updpill"));
console.log(JSON.stringify({ pill: pill ? pill.textContent : null }));
"""

AVAILABLE = """{
  supports_update: true, available: true, checked_at: 1, detail: "Update available",
  last_error: null, fail_streak: 0, checking: false,
}"""
UP_TO_DATE = """{
  supports_update: true, available: false, checked_at: 1, detail: "No update available",
  last_error: null, fail_streak: 0, checking: false,
}"""


def test_badge_shows_when_an_update_is_waiting():
    assert run_js(BASE % ("true", AVAILABLE))["pill"] == "アップデートあり"


def test_no_badge_when_up_to_date():
    assert run_js(BASE % ("true", UP_TO_DATE))["pill"] is None


def test_no_badge_before_the_first_check():
    assert run_js(BASE % ("true", "null"))["pill"] is None
