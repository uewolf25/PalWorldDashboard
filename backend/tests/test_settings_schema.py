"""ゲーム設定（項目単位の編集）のテスト。"""

from __future__ import annotations

import pytest

from app.settings_schema import (
    FIELDS_BY_NAME,
    build_updates,
    describe,
    format_value,
    infer_spec,
    parse_value,
    spec_for,
    validate_value,
)

# 実機に近い、項目数の多い ini
FULL_INI = """[/Script/Pal.PalGameWorldSettings]
OptionSettings=(Difficulty=None,ExpRate=1.000000,PalCaptureRate=1.000000,DeathPenalty=All,bIsPvP=False,bEnableFastTravel=True,ServerPlayerMaxNum=32,ServerName="Test, Server",ServerDescription="",AdminPassword="secret",PublicPort=8211,RESTAPIEnabled=True,RESTAPIPort=8212,DropItemMaxNum=3000,SomeFutureFlag=True,SomeFutureCount=7,SomeFutureName="x")
"""


@pytest.fixture
def full_ini(tmp_path):
    path = tmp_path / "PalWorldSettings.ini"
    path.write_text(FULL_INI, encoding="utf-8")
    return path


@pytest.fixture
def full_settings(settings, full_ini):
    settings.pal_settings_ini = full_ini
    return settings


@pytest.fixture
async def full_client(full_settings, pal_client, notifier):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app(full_settings, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        yield c


# ---- 型の解釈と書式化 ------------------------------------------------------


@pytest.mark.parametrize(
    "name,raw,expected",
    [
        ("bIsPvP", "False", False),
        ("bEnableFastTravel", "True", True),
        ("ExpRate", "1.500000", 1.5),
        ("ServerPlayerMaxNum", "32", 32),
        ("ServerName", '"My, Server"', "My, Server"),
        ("Difficulty", "None", "None"),
    ],
)
def test_parse_value(name, raw, expected):
    assert parse_value(FIELDS_BY_NAME[name], raw) == expected


@pytest.mark.parametrize(
    "name,value,expected",
    [
        ("bIsPvP", True, "True"),
        ("bIsPvP", False, "False"),
        ("ExpRate", 2.5, "2.500000"),          # 小数6桁に揃える
        ("ExpRate", 3, "3.000000"),
        ("ServerPlayerMaxNum", 16, "16"),
        ("ServerPlayerMaxNum", 16.0, "16"),    # 数値入力から float で来ても int にする
        ("ServerName", "My Server", '"My Server"'),
        ("ServerName", '"既に引用符つき"', '"既に引用符つき"'),
        ("Difficulty", "Hard", "Hard"),        # enum は引用符で囲まない
    ],
)
def test_format_value(name, value, expected):
    assert format_value(FIELDS_BY_NAME[name], value) == expected


def test_format_string_strips_embedded_quotes():
    """引用符を混ぜられて OptionSettings の構文を壊されないこと。"""
    spec = FIELDS_BY_NAME["ServerName"]
    assert format_value(spec, 'a"b') == '"ab"'


def test_bool_accepts_string_from_form():
    assert format_value(FIELDS_BY_NAME["bIsPvP"], "true") == "True"
    assert format_value(FIELDS_BY_NAME["bIsPvP"], "false") == "False"


# ---- 未知キーの型推論 ------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_type",
    [("True", "bool"), ("False", "bool"), ("42", "int"), ("1.500000", "float"), ('"text"', "string")],
)
def test_infer_spec_guesses_type_from_value(raw, expected_type):
    spec = infer_spec("BrandNewOption", raw)
    assert spec.type == expected_type
    assert spec.known is False
    assert spec.category == "other"


def test_spec_for_prefers_schema_over_inference():
    assert spec_for("ExpRate", "1.000000").known is True
    assert spec_for("TotallyNewThing", "1").known is False


# ---- 検証 ------------------------------------------------------------------


def test_validate_rejects_out_of_range():
    assert validate_value(FIELDS_BY_NAME["ExpRate"], 999) is not None
    assert validate_value(FIELDS_BY_NAME["ExpRate"], -1) is not None
    assert validate_value(FIELDS_BY_NAME["ExpRate"], 2.0) is None


def test_validate_rejects_unknown_enum_choice():
    assert validate_value(FIELDS_BY_NAME["Difficulty"], "Insane") is not None
    assert validate_value(FIELDS_BY_NAME["Difficulty"], "Hard") is None


def test_validate_rejects_non_numeric():
    assert validate_value(FIELDS_BY_NAME["ServerPlayerMaxNum"], "たくさん") is not None


def test_build_updates_rejects_keys_not_in_file():
    """タイプミスで存在しない項目を書き足さないこと。"""
    options = {"ExpRate": "1.000000"}
    updates, errors = build_updates({"ExpRat": 2.0}, options)
    assert updates == {}
    assert errors and "存在しない" in errors[0]


# ---- describe（フォーム定義） ----------------------------------------------


def test_describe_groups_into_categories(full_ini):
    from app.settings_ini import parse_options

    categories = describe(parse_options(full_ini.read_text()))
    labels = {c["label"] for c in categories}
    assert "サーバ基本" in labels
    assert "ゲームバランス" in labels
    # 未知キーは「その他」に入る
    other = next(c for c in categories if c["key"] == "other")
    assert {f["name"] for f in other["fields"]} == {
        "SomeFutureFlag", "SomeFutureCount", "SomeFutureName"
    }


def test_describe_only_returns_keys_present_in_file(full_ini):
    """スキーマにあってもファイルに無い項目は出さない（勝手に書き足さない）。"""
    from app.settings_ini import parse_options

    options = parse_options(full_ini.read_text())
    categories = describe(options)
    names = {f["name"] for c in categories for f in c["fields"]}
    assert names == set(options)
    assert "ItemWeightRate" not in names  # スキーマにはあるがファイルには無い


def test_secret_fields_are_flagged(full_ini):
    from app.settings_ini import parse_options

    categories = describe(parse_options(full_ini.read_text()))
    admin = next(f for c in categories for f in c["fields"] if f["name"] == "AdminPassword")
    assert admin["secret"] is True


# ---- API -------------------------------------------------------------------


async def test_fields_endpoint_returns_typed_form(full_client):
    resp = await full_client.get("/api/settings-ini/fields")
    assert resp.status_code == 200
    body = resp.json()
    assert body["exists"] is True

    fields = {f["name"]: f for c in body["categories"] for f in c["fields"]}
    assert fields["bIsPvP"]["type"] == "bool"
    assert fields["bIsPvP"]["value"] is False
    assert fields["Difficulty"]["choices"] == ["None", "Casual", "Normal", "Hard"]
    assert fields["ExpRate"]["min"] == 0.0 and fields["ExpRate"]["max"] == 20.0
    assert fields["ServerName"]["value"] == "Test, Server"


async def test_fields_endpoint_when_file_missing(settings, pal_client, notifier, tmp_path):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    settings.pal_settings_ini = tmp_path / "missing.ini"
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        body = (await c.get("/api/settings-ini/fields")).json()
    assert body["exists"] is False
    assert body["categories"] == []


async def test_update_fields_writes_formatted_values(full_client, full_ini):
    resp = await full_client.put(
        "/api/settings-ini/fields",
        json={"values": {"ExpRate": 2.5, "bIsPvP": True, "Difficulty": "Hard", "ServerName": "改名"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["restart_required"] is True
    assert body["backup"]

    text = full_ini.read_text()
    assert "ExpRate=2.500000" in text
    assert "bIsPvP=True" in text
    assert "Difficulty=Hard" in text
    assert 'ServerName="改名"' in text
    # 触っていない項目はそのまま
    assert "PalCaptureRate=1.000000" in text


async def test_update_fields_keeps_other_keys_intact(full_client, full_ini):
    before = len((await full_client.get("/api/settings-ini/fields")).json()["categories"])
    await full_client.put("/api/settings-ini/fields", json={"values": {"ExpRate": 3.0}})
    after = (await full_client.get("/api/settings-ini/fields")).json()

    names = {f["name"] for c in after["categories"] for f in c["fields"]}
    assert len(after["categories"]) == before
    assert "SomeFutureFlag" in names  # 未知キーが消えていない


async def test_update_unknown_key_is_rejected(full_client, full_ini):
    original = full_ini.read_text()
    resp = await full_client.put("/api/settings-ini/fields", json={"values": {"NopeNope": 1}})
    assert resp.status_code == 400
    assert full_ini.read_text() == original


async def test_update_out_of_range_is_rejected(full_client, full_ini):
    original = full_ini.read_text()
    resp = await full_client.put("/api/settings-ini/fields", json={"values": {"ExpRate": 999}})
    assert resp.status_code == 400
    assert "20.0 以下" in resp.json()["detail"]
    assert full_ini.read_text() == original


async def test_update_invalid_enum_is_rejected(full_client, full_ini):
    resp = await full_client.put("/api/settings-ini/fields", json={"values": {"Difficulty": "Insane"}})
    assert resp.status_code == 400
    assert "選択肢" in resp.json()["detail"]


async def test_update_requires_values(full_client):
    assert (await full_client.put("/api/settings-ini/fields", json={"values": {}})).status_code == 422


async def test_unchanged_values_do_not_create_backup(full_client):
    """同じ値を送っただけならバックアップを増やさない。"""
    resp = await full_client.put("/api/settings-ini/fields", json={"values": {"ExpRate": 1.0}})
    assert resp.json()["result"] == "unchanged"

    backups = (await full_client.get("/api/settings-ini")).json()["backups"]
    assert backups == []


async def test_unknown_key_can_still_be_edited(full_client, full_ini):
    """Palworld の更新で増えた項目も、型推論で編集できること。"""
    resp = await full_client.put(
        "/api/settings-ini/fields",
        json={"values": {"SomeFutureFlag": False, "SomeFutureCount": 99}},
    )
    assert resp.status_code == 200

    text = full_ini.read_text()
    assert "SomeFutureFlag=False" in text
    assert "SomeFutureCount=99" in text


async def test_update_notifies_discord_with_diff(full_client, notifier):
    await full_client.put("/api/settings-ini/fields", json={"values": {"ExpRate": 2.0}})
    sent = [n for n in notifier.sent if n["title"] == "サーバ設定を更新しました"]
    assert sent and "ExpRate=2.000000" in sent[-1]["description"]


async def test_form_and_raw_editor_stay_consistent(full_client, full_ini):
    """フォームで保存した内容が、全文表示側にもそのまま出ること。"""
    await full_client.put("/api/settings-ini/fields", json={"values": {"ExpRate": 4.0}})

    raw = (await full_client.get("/api/settings-ini")).json()
    assert "ExpRate=4.000000" in raw["text"]
    assert raw["options"]["ExpRate"] == "4.000000"
