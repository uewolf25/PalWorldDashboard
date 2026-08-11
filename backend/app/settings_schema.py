"""PalWorldSettings.ini の OptionSettings を、項目ごとのフォームとして扱うためのスキーマ。

ini は全項目が1行に詰まった文字列なので、そのままでは編集しづらい。
ここで各キーに「型・ラベル・範囲・カテゴリ」を与えて、UI がチェックボックスや
プルダウンを出せるようにする。

Palworld はアップデートで項目が増える。**スキーマに無いキーも必ず編集できる**よう、
未知のキーは値そのものから型を推論してフォールバックする（知らない項目を
UI から消してしまうと、保存時に消滅するのが一番まずい）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

FieldType = Literal["bool", "int", "float", "enum", "string"]

# カテゴリの表示順とラベル
CATEGORIES: list[tuple[str, str]] = [
    ("server", "サーバ基本"),
    ("network", "接続・API"),
    ("gameplay", "ゲームバランス"),
    ("player", "プレイヤー"),
    ("pal", "パル"),
    ("base", "拠点・建築"),
    ("item", "アイテム・採取"),
    ("guild", "ギルド・PvP"),
    ("other", "その他"),
]


@dataclass
class FieldSpec:
    name: str
    label: str
    type: FieldType
    category: str = "other"
    help: str = ""
    choices: list[str] = field(default_factory=list)
    min: float | None = None
    max: float | None = None
    step: float | None = None
    # パスワードなど、画面で伏せて表示する項目
    secret: bool = False
    # スキーマに定義がある項目かどうか（推論で作った場合は False）
    known: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "category": self.category,
            "help": self.help,
            "choices": self.choices,
            "min": self.min,
            "max": self.max,
            "step": self.step,
            "secret": self.secret,
            "known": self.known,
        }


def _rate(name: str, label: str, category: str, help: str = "") -> FieldSpec:
    """倍率系（0〜20倍、0.1刻み）の共通定義。"""
    return FieldSpec(
        name=name, label=label, type="float", category=category,
        help=help or "1.0 が標準です。", min=0.0, max=20.0, step=0.1,
    )


def _flag(name: str, label: str, category: str, help: str = "") -> FieldSpec:
    return FieldSpec(name=name, label=label, type="bool", category=category, help=help)


FIELDS: list[FieldSpec] = [
    # --- サーバ基本 ---
    FieldSpec("ServerName", "サーバ名", "string", "server", "サーバ一覧に表示される名前。"),
    FieldSpec("ServerDescription", "サーバ説明", "string", "server"),
    FieldSpec("ServerPlayerMaxNum", "最大プレイヤー数", "int", "server",
              "サーバ全体の上限。32 を超えると動作が不安定になることがあります。", min=1, max=32, step=1),
    FieldSpec("CoopPlayerMaxNum", "co-op最大人数", "int", "server", min=1, max=32, step=1),
    FieldSpec("ServerPassword", "参加パスワード", "string", "server",
              "設定すると参加時に入力が必要になります。", secret=True),
    FieldSpec("AdminPassword", "管理者パスワード", "string", "server",
              "REST API / RCON の認証にも使われます。変更すると管理ツールの設定も更新が必要です。",
              secret=True),
    FieldSpec("Region", "リージョン", "string", "server"),
    _flag("bUseAuth", "認証を使う", "server"),
    FieldSpec("BanListURL", "BANリストURL", "string", "server"),
    FieldSpec("Difficulty", "難易度", "enum", "server",
              "None は「カスタム」扱いで、以下の個別設定がそのまま使われます。",
              choices=["None", "Casual", "Normal", "Hard"]),
    _flag("bIsMultiplay", "マルチプレイ", "server"),

    # --- 接続・API ---
    FieldSpec("PublicPort", "公開ポート", "int", "network", min=1, max=65535, step=1),
    FieldSpec("PublicIP", "公開IP", "string", "network"),
    _flag("RCONEnabled", "RCONを有効にする", "network"),
    FieldSpec("RCONPort", "RCONポート", "int", "network", min=1, max=65535, step=1),
    _flag("RESTAPIEnabled", "REST APIを有効にする", "network",
          "この管理ツールが使います。無効にすると操作できなくなります。"),
    FieldSpec("RESTAPIPort", "REST APIポート", "int", "network",
              "この管理ツールの PAL_PORT と一致させてください。", min=1, max=65535, step=1),
    _flag("bShowPlayerList", "プレイヤー一覧を公開する", "network"),
    FieldSpec("ChatPostLimitPerMinute", "チャット投稿制限（毎分）", "int", "network", min=0, max=1000, step=1),
    FieldSpec("AllowConnectPlatform", "接続を許可するプラットフォーム", "string", "network"),
    FieldSpec("CrossplayPlatforms", "クロスプレイ対象", "string", "network"),
    FieldSpec("LogFormatType", "ログ形式", "string", "network"),
    _flag("bIsUseBackupSaveData", "セーブデータのバックアップを取る", "network"),

    # --- ゲームバランス ---
    _rate("DayTimeSpeedRate", "昼の進行速度", "gameplay"),
    _rate("NightTimeSpeedRate", "夜の進行速度", "gameplay"),
    _rate("ExpRate", "経験値倍率", "gameplay"),
    _rate("WorkSpeedRate", "作業速度", "gameplay"),
    FieldSpec("DeathPenalty", "デスペナルティ", "enum", "gameplay",
              "None=なし / Item=アイテムのみ / ItemAndEquipment=装備も / All=パルも全部",
              choices=["None", "Item", "ItemAndEquipment", "All"]),
    _flag("bEnableInvaderEnemy", "襲撃イベントを有効にする", "gameplay"),
    _flag("bActiveUNKO", "UNKOを有効にする", "gameplay"),
    _flag("bEnableFastTravel", "ファストトラベルを許可", "gameplay"),
    _flag("bIsStartLocationSelectByMap", "初期地点をマップから選ぶ", "gameplay"),
    _flag("bExistPlayerAfterLogout", "ログアウト後もキャラを残す", "gameplay"),
    _flag("bEnableNonLoginPenalty", "未ログインペナルティ", "gameplay"),
    _flag("EnablePredatorBossPal", "捕食者ボスパルを有効にする", "gameplay"),
    FieldSpec("SupplyDropSpan", "補給物資の間隔（分）", "int", "gameplay", min=0, max=1440, step=1),
    _flag("bEnableAimAssistPad", "エイムアシスト（パッド）", "gameplay"),
    _flag("bEnableAimAssistKeyboard", "エイムアシスト（キーボード）", "gameplay"),
    FieldSpec("RandomizerType", "ランダマイザ", "enum", "gameplay",
              choices=["None", "Region", "All"]),
    FieldSpec("RandomizerSeed", "ランダマイザのシード", "string", "gameplay"),
    _flag("bIsRandomizerPalLevelRandom", "パルのレベルもランダムにする", "gameplay"),

    # --- プレイヤー ---
    _rate("PlayerDamageRateAttack", "与ダメージ倍率", "player"),
    _rate("PlayerDamageRateDefense", "被ダメージ倍率", "player"),
    _rate("PlayerStomachDecreaceRate", "満腹度の減少速度", "player"),
    _rate("PlayerStaminaDecreaceRate", "スタミナの減少速度", "player"),
    _rate("PlayerAutoHPRegeneRate", "HP自動回復速度", "player"),
    _rate("PlayerAutoHpRegeneRateInSleep", "睡眠中のHP回復速度", "player"),

    # --- パル ---
    _rate("PalCaptureRate", "捕獲率", "pal"),
    _rate("PalSpawnNumRate", "出現数", "pal"),
    _rate("PalDamageRateAttack", "与ダメージ倍率", "pal"),
    _rate("PalDamageRateDefense", "被ダメージ倍率", "pal"),
    _rate("PalStomachDecreaceRate", "満腹度の減少速度", "pal"),
    _rate("PalStaminaDecreaceRate", "スタミナの減少速度", "pal"),
    _rate("PalAutoHPRegeneRate", "HP自動回復速度", "pal"),
    _rate("PalAutoHpRegeneRateInSleep", "睡眠中のHP回復速度", "pal"),
    FieldSpec("PalEggDefaultHatchingTime", "卵の孵化時間（時間）", "float", "pal",
              min=0.0, max=240.0, step=0.5),
    _flag("bAllowGlobalPalboxExport", "パルボックスの持ち出しを許可", "pal"),
    _flag("bAllowGlobalPalboxImport", "パルボックスの持ち込みを許可", "pal"),

    # --- 拠点・建築 ---
    FieldSpec("BaseCampMaxNum", "拠点の最大数", "int", "base", min=1, max=1000, step=1),
    FieldSpec("BaseCampWorkerMaxNum", "拠点あたりの working パル数", "int", "base", min=1, max=50, step=1),
    FieldSpec("MaxBuildingLimitNum", "建築物の上限（0=無制限）", "int", "base", min=0, max=100000, step=1),
    _rate("BuildObjectDamageRate", "建築物への与ダメージ", "base"),
    _rate("BuildObjectDeteriorationDamageRate", "建築物の劣化速度", "base"),
    _flag("bBuildAreaLimit", "建築範囲を制限する", "base"),
    _flag("bInvisibleOtherGuildBaseCampAreaFX", "他ギルド拠点の範囲表示を隠す", "base"),
    FieldSpec("ServerReplicatePawnCullDistance", "描画距離", "float", "base",
              min=100.0, max=20000.0, step=100.0),

    # --- アイテム・採取 ---
    _rate("CollectionDropRate", "採取量", "item"),
    _rate("CollectionObjectHpRate", "採取対象のHP", "item"),
    _rate("CollectionObjectRespawnSpeedRate", "採取対象の再出現速度", "item"),
    _rate("EnemyDropItemRate", "敵のドロップ量", "item"),
    _rate("ItemWeightRate", "アイテム重量", "item"),
    FieldSpec("DropItemMaxNum", "ドロップの最大数", "int", "item", min=0, max=10000, step=1),
    FieldSpec("DropItemMaxNum_UNKO", "UNKOドロップの最大数", "int", "item", min=0, max=10000, step=1),
    FieldSpec("DropItemAliveMaxHours", "ドロップの残存時間（時間）", "float", "item",
              min=0.0, max=240.0, step=0.5),

    # --- ギルド・PvP ---
    _flag("bIsPvP", "PvPを有効にする", "guild"),
    _flag("bEnablePlayerToPlayerDamage", "プレイヤー間ダメージ", "guild"),
    _flag("bEnableFriendlyFire", "フレンドリーファイア", "guild"),
    _flag("bEnableDefenseOtherGuildPlayer", "他ギルドの拠点防衛を許可", "guild"),
    _flag("bCanPickupOtherGuildDeathPenaltyDrop", "他ギルドのデスドロップを拾える", "guild"),
    FieldSpec("GuildPlayerMaxNum", "ギルドの最大人数", "int", "guild", min=1, max=100, step=1),
    _flag("bAutoResetGuildNoOnlinePlayers", "無人ギルドを自動解散する", "guild"),
    FieldSpec("AutoResetGuildTimeNoOnlinePlayers", "自動解散までの時間（時間）", "float", "guild",
              min=0.0, max=8760.0, step=1.0),
]

FIELDS_BY_NAME: dict[str, FieldSpec] = {f.name: f for f in FIELDS}

# 公式の DefaultPalWorldSettings.ini に載っている既定値。
# ファイルに書かれていない項目を追加するとき、初期値としてこれを提示する。
#
# ここに載せるのは既定値が確認できたものだけ。新しめの項目（RESTAPIEnabled など）は
# 手元で裏取りできていないので、あえて空けてある。推測値を「既定値」として
# 出すと、それを信じて追加した結果ゲーム側の挙動が変わる事故になるため。
DEFAULTS: dict[str, Any] = {
    "Difficulty": "None",
    "DayTimeSpeedRate": 1.0,
    "NightTimeSpeedRate": 1.0,
    "ExpRate": 1.0,
    "PalCaptureRate": 1.0,
    "PalSpawnNumRate": 1.0,
    "PalDamageRateAttack": 1.0,
    "PalDamageRateDefense": 1.0,
    "PlayerDamageRateAttack": 1.0,
    "PlayerDamageRateDefense": 1.0,
    "PlayerStomachDecreaceRate": 1.0,
    "PlayerStaminaDecreaceRate": 1.0,
    "PlayerAutoHPRegeneRate": 1.0,
    "PlayerAutoHpRegeneRateInSleep": 1.0,
    "PalStomachDecreaceRate": 1.0,
    "PalStaminaDecreaceRate": 1.0,
    "PalAutoHPRegeneRate": 1.0,
    "PalAutoHpRegeneRateInSleep": 1.0,
    "BuildObjectDamageRate": 1.0,
    "BuildObjectDeteriorationDamageRate": 1.0,
    "CollectionDropRate": 1.0,
    "CollectionObjectHpRate": 1.0,
    "CollectionObjectRespawnSpeedRate": 1.0,
    "EnemyDropItemRate": 1.0,
    "DeathPenalty": "All",
    "bEnablePlayerToPlayerDamage": False,
    "bEnableFriendlyFire": False,
    "bEnableInvaderEnemy": True,
    "bActiveUNKO": False,
    "bEnableAimAssistPad": True,
    "bEnableAimAssistKeyboard": False,
    "DropItemMaxNum": 3000,
    "DropItemMaxNum_UNKO": 100,
    "BaseCampMaxNum": 128,
    "BaseCampWorkerMaxNum": 15,
    "DropItemAliveMaxHours": 1.0,
    "bAutoResetGuildNoOnlinePlayers": False,
    "AutoResetGuildTimeNoOnlinePlayers": 72.0,
    "GuildPlayerMaxNum": 20,
    "PalEggDefaultHatchingTime": 72.0,
    "WorkSpeedRate": 1.0,
    "bIsMultiplay": False,
    "bIsPvP": False,
    "bCanPickupOtherGuildDeathPenaltyDrop": False,
    "bEnableNonLoginPenalty": True,
    "bEnableFastTravel": True,
    "bIsStartLocationSelectByMap": True,
    "bExistPlayerAfterLogout": False,
    "bEnableDefenseOtherGuildPlayer": False,
    "CoopPlayerMaxNum": 4,
    "ServerPlayerMaxNum": 32,
    "ServerName": "Default Palworld Server",
    "ServerDescription": "",
    "AdminPassword": "",
    "ServerPassword": "",
    "PublicPort": 8211,
    "PublicIP": "",
    "RCONEnabled": False,
    "RCONPort": 25575,
    "Region": "",
    "bUseAuth": True,
    "BanListURL": "https://api.palworldgame.com/api/banlist.txt",
}

# 既定値が分からない項目を追加するときの、型ごとの初期値
_NEUTRAL: dict[str, Any] = {"bool": False, "int": 0, "float": 1.0, "enum": "", "string": ""}


def initial_value(spec: FieldSpec) -> Any:
    """未設定の項目を追加するときに、フォームへ最初に入れる値。"""
    if spec.name in DEFAULTS:
        return DEFAULTS[spec.name]
    if spec.type == "enum" and spec.choices:
        return spec.choices[0]
    return _NEUTRAL.get(spec.type, "")


def missing_fields(options: dict[str, str]) -> list[dict[str, Any]]:
    """スキーマにはあるが、設定ファイルに書かれていない項目。

    Palworld は未記載の項目に内蔵の既定値を使う。つまり「書かれていない」＝
    「既定値で動いている」なので、値を変えたいなら追記する必要がある。
    ただし追記は必ずユーザーの明示操作で行う（下の add_fields を参照）。
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for spec in FIELDS:
        if spec.name in options:
            continue
        item = spec.as_dict()
        item["value"] = initial_value(spec)
        item["default"] = DEFAULTS.get(spec.name)
        item["default_known"] = spec.name in DEFAULTS
        buckets.setdefault(spec.category, []).append(item)

    out: list[dict[str, Any]] = []
    for key, label in CATEGORIES:
        items = buckets.get(key, [])
        if items:
            out.append({"key": key, "label": label, "fields": items})
    return out


def add_fields(values: dict[str, Any], options: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """設定ファイルに無い項目を、新しく書き足すための値を作る。

    スキーマに定義がある項目だけ許可する。未知の名前で好き勝手に
    キーを増やせると、タイプミスが黙って通ってしまうため。
    """
    updates: dict[str, str] = {}
    errors: list[str] = []

    for name, value in values.items():
        if name in options:
            errors.append(f"{name}: すでに設定ファイルに存在します")
            continue
        spec = FIELDS_BY_NAME.get(name)
        if spec is None:
            errors.append(f"{name}: この管理ツールが把握していない項目のため追加できません")
            continue
        error = validate_value(spec, value)
        if error:
            errors.append(error)
            continue
        try:
            updates[name] = format_value(spec, value)
        except (TypeError, ValueError):
            errors.append(f"{spec.label}: 値の形式が正しくありません（{value!r}）")

    return updates, errors

_QUOTED = re.compile(r'^"(.*)"$', re.S)
_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?\d+\.\d+$")


def infer_spec(name: str, raw: str) -> FieldSpec:
    """スキーマに無いキーの型を、ini 上の値から推測する。

    Palworld の更新で増えた項目でも編集できるようにするための保険。
    """
    value = raw.strip()
    if value in ("True", "False"):
        ftype: FieldType = "bool"
    elif _INT.match(value):
        ftype = "int"
    elif _FLOAT.match(value):
        ftype = "float"
    else:
        ftype = "string"
    return FieldSpec(
        name=name,
        label=name,
        type=ftype,
        category="other",
        help="この管理ツールが把握していない項目です。値の形式を変えずに編集してください。",
        known=False,
    )


def parse_value(spec: FieldSpec, raw: str) -> Any:
    """ini の文字列を Python の値にする。壊れていたら文字列のまま返す。"""
    value = raw.strip()
    if spec.type == "bool":
        return value == "True"
    if spec.type in ("int", "float"):
        stripped = value.strip('"')
        try:
            return int(stripped) if spec.type == "int" else float(stripped)
        except ValueError:
            return value
    # enum / string は引用符を外して返す
    m = _QUOTED.match(value)
    return m.group(1) if m else value


def format_value(spec: FieldSpec, value: Any) -> str:
    """Python の値を ini の文字列にする。

    元ファイルの書式に合わせる:
      bool  -> True / False
      float -> 1.000000（小数6桁）
      文字列 -> "..." で囲む（enum は囲まない）
    """
    if spec.type == "bool":
        if isinstance(value, str):
            return "True" if value.lower() in ("true", "1", "yes", "on") else "False"
        return "True" if value else "False"

    if spec.type == "int":
        return str(int(float(value)))

    if spec.type == "float":
        return f"{float(value):.6f}"

    if spec.type == "enum":
        # 列挙値は引用符なし（Difficulty=None など）
        return str(value).strip().strip('"')

    text = "" if value is None else str(value)
    text = text.strip()
    if _QUOTED.match(text):
        return text
    # ini の値としてカンマや括弧を安全に持たせるため常に引用符で囲む
    return '"' + text.replace('"', "") + '"'


def validate_value(spec: FieldSpec, value: Any) -> str | None:
    """範囲や選択肢の検証。問題なければ None を返す。"""
    if spec.type == "enum" and spec.choices:
        if str(value).strip().strip('"') not in spec.choices:
            return f"{spec.label}: {value!r} は選択肢にありません（{'/'.join(spec.choices)}）"
    if spec.type in ("int", "float"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return f"{spec.label}: 数値を入力してください"
        if spec.min is not None and number < spec.min:
            return f"{spec.label}: {spec.min} 以上で指定してください"
        if spec.max is not None and number > spec.max:
            return f"{spec.label}: {spec.max} 以下で指定してください"
    return None


def spec_for(name: str, raw: str) -> FieldSpec:
    return FIELDS_BY_NAME.get(name) or infer_spec(name, raw)


def describe(options: dict[str, str]) -> list[dict[str, Any]]:
    """現在の ini の内容を、カテゴリ分けしたフォーム定義に変換する。

    ファイルに実在するキーだけを返す。スキーマにあってもファイルに無い項目は
    出さない（勝手に書き足すと、サーバ側の既定値が変わったときに追従できなくなる）。
    """
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in CATEGORIES}

    for name, raw in options.items():
        spec = spec_for(name, raw)
        item = spec.as_dict()
        item["value"] = parse_value(spec, raw)
        item["raw"] = raw
        item["default"] = DEFAULTS.get(name)
        item["default_known"] = name in DEFAULTS
        buckets.setdefault(spec.category, []).append(item)

    out: list[dict[str, Any]] = []
    for key, label in CATEGORIES:
        items = buckets.get(key, [])
        if items:
            out.append({"key": key, "label": label, "fields": items})
    return out


def build_updates(values: dict[str, Any], options: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """フォームからの値を ini 文字列に直す。

    戻り値は (更新するキーと ini 文字列, エラー一覧)。
    ini に存在しないキーは受け付けない（タイプミスで別項目を増やさないため）。
    """
    updates: dict[str, str] = {}
    errors: list[str] = []

    for name, value in values.items():
        if name not in options:
            errors.append(f"{name}: 設定ファイルに存在しない項目です")
            continue
        spec = spec_for(name, options[name])
        error = validate_value(spec, value)
        if error:
            errors.append(error)
            continue
        try:
            updates[name] = format_value(spec, value)
        except (TypeError, ValueError):
            errors.append(f"{spec.label}: 値の形式が正しくありません（{value!r}）")

    return updates, errors
