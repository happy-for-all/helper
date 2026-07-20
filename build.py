# ============================================================
# 🌿 ヘルパー事業所ナビ build.py
#
# 目的：
#   ① 厚労省CSV（jigyosho_110.csv／訪問介護）と
#      WAM NET CSV（csvdownload011.csv／居宅介護）の
#      2つを読み込み、それぞれ「サービス区分バッジ」付きの
#      統一スキーマに変換する
#   ② AIリサーチ結果（specialty_result.json）と事業所番号で
#      突き合わせ、生活保護対応タグを付与
#   ③ 都道府県別の軽量JSONファイルに分割して dist/ に出力
#
# 👑 重要な設計方針（2026-07-19 実データ検証で判明）：
#   介護保険（jigyosho_110.csv）と障害福祉（csvdownload011.csv）は
#   事業所番号の体系がほぼ別々（35,191件・25,386件のうち共通は
#   わずか9件）のため、無理に1件へ統合（名寄せ）しない。
#   2つを別々のレコードとして扱い、それぞれに
#   「介護保険：訪問介護」「障害福祉：居宅介護」のサービス区分
#   バッジを付けて出力する（訪問看護ナビの安全側フォールバック
#   設計を踏襲：CSV文字コード自動判定／NaN安全変換／
#   JSON形式不備で処理停止しない）。
# ============================================================


# ------------------------------------------------------------
# 1. ライブラリの読み込み
# ------------------------------------------------------------
import os
import re
import json
import shutil
import hashlib
import unicodedata
import pandas as pd


# ------------------------------------------------------------
# 2. 設定（プロジェクトに合わせて調整可能な定数）
# ------------------------------------------------------------
KAIGO_CSV_PATH = "jigyosho_110.csv"          # 厚労省：訪問介護（介護保険）
SHOGAI_CSV_PATH = "csvdownload011.csv"       # WAM NET：居宅介護（障害福祉）
SPECIALTY_JSON_PATH = "specialty_result.json"  # AIリサーチ結果（生活保護対応等）
OUTPUT_DIR = "dist"

# CSVの出典表記（厚労省ページ・WAM NETページの表記に合わせて手動で更新してください）
# 👑 重要（2026-07-20）：訪問介護（厚労省）側は「2025年12月末時点」で
# 確認が取れていますが、居宅介護（WAM NET）側の時点表記は未確認です。
# ダウンロード元ページに記載されている正式な時点表記を確認したうえで、
# 下記のSHOGAI_CSV_SOURCE_LABELを正しい値に更新してください。
KAIGO_CSV_SOURCE_LABEL = "2025年12月末時点（厚労省・介護サービス情報公表システム）"
SHOGAI_CSV_SOURCE_LABEL = "2026年3月時点（WAM NET・障害福祉サービス等情報公表システム）"

# サービス区分バッジ（フロント表示用の固定ラベル）
SERVICE_TYPE_KAIGO = "care_insurance_homehelp"
SERVICE_TYPE_KAIGO_LABEL = "介護保険：訪問介護"
SERVICE_TYPE_SHOGAI = "disability_kyotaku"
SERVICE_TYPE_SHOGAI_LABEL = "障害福祉：居宅介護"

# 都道府県名 → 出力ファイル用スラッグ（訪問看護ナビと同一の対応表を踏襲）
ALL_PREFECTURES = {
    "北海道": "hokkaido", "青森県": "aomori", "岩手県": "iwate", "宮城県": "miyagi",
    "秋田県": "akita", "山形県": "yamagata", "福島県": "fukushima", "茨城県": "ibaraki",
    "栃木県": "tochigi", "群馬県": "gunma", "埼玉県": "saitama", "千葉県": "chiba",
    "東京都": "tokyo", "神奈川県": "kanagawa", "新潟県": "niigata", "富山県": "toyama",
    "石川県": "ishikawa", "福井県": "fukui", "山梨県": "yamanashi", "長野県": "nagano",
    "岐阜県": "gifu", "静岡県": "shizuoka", "愛知県": "aichi", "三重県": "mie",
    "滋賀県": "shiga", "京都府": "kyoto", "大阪府": "osaka", "兵庫県": "hyogo",
    "奈良県": "nara", "和歌山県": "wakayama", "鳥取県": "tottori", "島根県": "shimane",
    "岡山県": "okayama", "広島県": "hiroshima", "山口県": "yamaguchi", "徳島県": "tokushima",
    "香川県": "kagawa", "愛媛県": "ehime", "高知県": "kochi", "福岡県": "fukuoka",
    "佐賀県": "saga", "長崎県": "nagasaki", "熊本県": "kumamoto", "大分県": "oita",
    "宮崎県": "miyazaki", "鹿児島県": "kagoshima", "沖縄県": "okinawa",
}

# 障害福祉サービス等情報公表システムのCSVは「都道府県コード又は市区町村コード」
# （JIS都道府県コードの先頭2桁）から都道府県を逆引きする必要があるため、
# 標準のJIS都道府県コード順（01〜47）を定義する。
PREFECTURE_CODE_ORDER = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県",
    "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県",
    "福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]
PREFECTURE_CODE_MAP = {
    str(i + 1).zfill(2): name for i, name in enumerate(PREFECTURE_CODE_ORDER)
}

# AIリサーチのカテゴリ（現時点では生活保護対応のみ。将来的な拡張に備えて
# 訪問看護ナビと同じ「カテゴリ一覧＋抽出関数」の形にしておく）
SPECIALTY_CATEGORIES = ["seikatsu_hogo"]

# 早朝・夜間対応の判定に使うキーワード（自由記述からの推定。断定ではない）
NIGHT_EMERGENCY_KEYWORDS = ["24時間", "緊急", "早朝", "夜間"]

# 👑 バグ修正（2026-07-20 第2回）：「12月30日」のような日付表記に加えて、
# 「祝日」「水曜日」のような、曜日名や祝日を意味する単語に含まれる「日」も
# 曜日の「日曜」と誤検出しないよう、判定前にこれらの単語を除去する。
# ※「土日」「土・日」のような、実際に日曜を意味する表記は除去対象外。
DATE_RANGE_PATTERN = re.compile(r"\d{1,2}月\d{1,2}日|\d{1,2}/\d{1,2}")
NON_SUNDAY_DAY_WORDS_PATTERN = re.compile(r"月曜日?|火曜日?|水曜日?|木曜日?|金曜日?|土曜日?|祝日|祭日|休日|平日")


# ------------------------------------------------------------
# 3. CSV読み込み（文字コード自動判定・訪問看護ナビと同方式）
# ------------------------------------------------------------
def load_csv_with_encoding_fallback(path):
    """
    複数の文字コードを順に試し、読み込めたものを採用する。
    今回の2ファイルはutf-8-sig確認済みだが、万一の文字コード違いに
    備えて防御的な実装にしておく。
    """
    encodings_to_try = ["utf-8-sig", "utf-8", "shift_jis", "cp932"]
    last_error = None

    for enc in encodings_to_try:
        try:
            return pd.read_csv(path, encoding=enc, dtype=str)
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"CSVの読み込みに失敗しました（全ての文字コードで失敗）: {last_error}")


# ------------------------------------------------------------
# 4. 文字列・数値の安全な取得（訪問看護ナビと同方式）
#
#    pandasはCSVの空欄を「NaN（float型）」として読み込むため、
#    そのまま str(値) とすると文字列 "nan" が入ってしまう不具合が
#    過去に発生した。全角英数字・記号はNFKCで半角に正規化する。
# ------------------------------------------------------------
def safe_str(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return text if text else None


def safe_float(value):
    """
    NaN（pandasの欠損値）を確実にNoneとして扱う、安全なfloat変換。
    float(nan) は例外を出さずに nan を返してしまい、そのまま
    json.dump すると不正な値（NaN）が出力されてしまうため、
    事前にNaN判定を行ってから変換する。
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------
# 5. 電話番号・FAX番号の正規化（訪問看護ナビと同方式）
# ------------------------------------------------------------
def clean_phone(raw):
    """
    表示用の電話番号はそのまま活かしつつ、tel:リンク用に数字だけの
    文字列を別途生成する。
    """
    display = safe_str(raw)
    if not display:
        return None, None

    digits_only = re.sub(r"[^\d]", "", display)
    if not digits_only:
        return None, None

    return display, digits_only


# ------------------------------------------------------------
# 6. URLの補正（訪問看護ナビと同方式）
# ------------------------------------------------------------
def build_url(raw_url):
    """
    全角文字混入・スキーム抜けを補正し、正しく開けるURLに整える。
    どうしても直せない・空欄の場合は None を返す（フロント側で
    「ホームページ情報なし」として扱われる）。
    """
    url = safe_str(raw_url)
    if not url:
        return None

    url = re.sub(r"^(https?):(?!//)", r"\1://", url)
    url = re.sub(r"^(https?)//", r"\1://", url)

    if not re.match(r"^https?://", url):
        url = "https://" + url

    return url


# ------------------------------------------------------------
# 6-1. レコードごとの一意なID生成（👑 バグ修正：2026-07-20）
#
#   実データ検証で判明：csvdownload011.csv（障害福祉：居宅介護）には、
#   本部＋支所・サテライト構成の事業所が「同じ事業所番号」で複数レコード
#   存在するケースが106件（48事業所番号）ある（例：本所・橿原支所・
#   高田支所が全て同一の事業所番号）。
#   事業所番号だけをキーにすると、index.html側の候補リスト機能で
#   別の支所を追加したつもりが同じ事業所番号を持つ他の支所も
#   「追加済み」表示になってしまう不具合が起きる。
#   そのため、事業所番号に加えて名称・住所のハッシュ値を組み合わせた
#   record_id を生成し、これを候補リスト機能の一意キーとして使う。
#   名称・住所が変わらない限りビルドのたびに同じIDになるため、
#   ユーザーがブラウザに保存した候補リストとの整合性も保たれる。
# ------------------------------------------------------------
def make_record_id(jigyosho_no, name, address):
    basis = f"{jigyosho_no}|{name}|{address}"
    short_hash = hashlib.md5(basis.encode("utf-8")).hexdigest()[:8]
    return f"{jigyosho_no}_{short_hash}"


# ------------------------------------------------------------
# 7. AIリサーチ結果（specialty_result.json）からのタグ付与
#    ★注意：これはAIによるホームページ内容の推定であり、断定ではない。
#    サイト側には必ず「AI調査・要確認」の免責を併記すること。
# ------------------------------------------------------------
def build_specialty_tags(jigyosho_no, specialty_data):
    """
    事業所番号をキーにAIリサーチ結果を検索し、各カテゴリの
    status（specialized / mentioned / None）だけをフロント用に抽出する。
    未リサーチの場合は全カテゴリNoneのまま返す（＝「情報なし」として
    安全に表示される）。
    """
    tags = {cat: None for cat in SPECIALTY_CATEGORIES}

    entry = specialty_data.get(jigyosho_no)
    if not entry or entry.get("error") or not entry.get("tags"):
        return tags

    for cat in SPECIALTY_CATEGORIES:
        cat_result = entry["tags"].get(cat)

        # 1件の形式不備でビルド全体を止めないよう、辞書以外は
        # 警告を出したうえで安全に「情報なし」として扱う
        # （訪問看護ナビのbuild_specialty_tagsと同方式）。
        if isinstance(cat_result, dict):
            tags[cat] = cat_result.get("status")  # "specialized" / "mentioned" / None
        elif cat_result:
            print(
                f"  警告：事業所番号{jigyosho_no}のカテゴリ「{cat}」の"
                f"形式が不正なためスキップしました：{cat_result!r}"
            )

    return tags


# ------------------------------------------------------------
# 8. 早朝・夜間対応の簡易判定（自由記述からのキーワード検出）
#    ★注意：自由記述からの推定であり、断定ではない。
# ------------------------------------------------------------
def detect_keyword_hint(text, keywords):
    if not text or not isinstance(text, str):
        return False
    return any(kw in text for kw in keywords)


def detect_early_late_from_time_range(time_range_str):
    """
    csvdownload011.csv の「利用可能な時間帯」（例："09:00-18:00"）から
    早朝（7:00以前開始）・夜間（19:00以降終了）対応の可能性を判定する。
    形式が不正な場合は安全側（False）に倒す。
    """
    text = safe_str(time_range_str)
    if not text:
        return False

    match = re.match(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$", text)
    if not match:
        return False

    start_h, start_m, end_h, end_m = (int(g) for g in match.groups())
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    # 👑 改善（2026-07-20）：「09:00-08:59」のように開始時刻より終了時刻が
    # 早い（＝日を跨いで翌日まで対応）表記は、実データで約100件確認された
    # 24時間対応・夜間対応のパターンであるため、確実に早朝・夜間対応として扱う。
    # （終了が00:00ちょうどのケースは、次の分岐で別途24時対応として判定される）
    if start_minutes > end_minutes and end_minutes != 0:
        return True

    is_early = start_minutes <= 7 * 60
    is_late = end_minutes >= 19 * 60 or end_minutes == 0  # 00:00終了＝24時対応とみなす

    return is_early or is_late


# ------------------------------------------------------------
# 9. 利用可能曜日のパース
# ------------------------------------------------------------
def parse_available_days_from_kaigo(raw):
    """
    jigyosho_110.csv の「利用可能曜日」（例："平日,土曜日,日曜日,祝日"）を
    フロント側で扱いやすいbool形式の辞書に変換する。
    """
    days = {"weekday": False, "saturday": False, "sunday": False, "holiday": False}

    if not raw or not isinstance(raw, str):
        return days

    tokens = [t.strip() for t in raw.split(",")]

    if "平日" in tokens:
        days["weekday"] = True
    if "土曜日" in tokens:
        days["saturday"] = True
    if "日曜日" in tokens:
        days["sunday"] = True
    if "祝日" in tokens:
        days["holiday"] = True

    return days


def parse_available_days_from_shogai(teikyuubi_raw):
    """
    csvdownload011.csv には「利用可能曜日」ではなく「定休日」
    （休みの日を書く自由記述）しかないため、ロジックが逆になる。
    「定休日」に曜日名が含まれていれば、その曜日は「対応不可」とみなす。
    空欄・「なし」「無し」「年中無休」の場合は全曜日対応とみなす
    （※自由記述からの推定であり、断定ではない）。
    """
    days = {"weekday": True, "saturday": True, "sunday": True, "holiday": True}

    text = safe_str(teikyuubi_raw)
    if not text:
        return days
    if any(kw in text for kw in ["なし", "無し", "年中無休"]):
        return days

    # 👑 バグ修正（2026-07-20 第2回）：日付表記に加えて「祝日」「水曜日」等の
    # 曜日名・祝日を意味する単語も取り除いたテキストで「日」の有無を判定する。
    # これにより「祝日」だけが定休日の事業所（日曜は営業）を、誤って
    # 「日曜非対応」と判定してしまう問題を解消する。
    # 「土日」「土・日」等の実在する表記は、この単語除去の対象外なので、
    # 引き続き正しく「日曜非対応」と判定される。
    text_for_sunday_check = DATE_RANGE_PATTERN.sub("", text)
    text_for_sunday_bare_check = NON_SUNDAY_DAY_WORDS_PATTERN.sub("", text_for_sunday_check)

    if "土曜" in text or "土日" in text or "土" in text:
        days["saturday"] = False
    if "日曜" in text_for_sunday_check or "土日" in text_for_sunday_check or "日" in text_for_sunday_bare_check:
        days["sunday"] = False
    if "祝" in text:
        days["holiday"] = False

    return days


# ------------------------------------------------------------
# 10. 都道府県コードからの都道府県名の逆引き（csvdownload011.csv用）
# ------------------------------------------------------------
def prefecture_name_from_code(code_raw):
    code = safe_str(code_raw)
    if not code or len(code) < 2:
        return None
    return PREFECTURE_CODE_MAP.get(code[:2])


# ------------------------------------------------------------
# 11. 1事業所分のレコードを組み立てる（介護保険：訪問介護）
# ------------------------------------------------------------
def build_kaigo_record(row, specialty_data):
    jigyosho_no = safe_str(row.get("事業所番号")) or ""

    tel_display, tel_clean = clean_phone(row.get("電話番号"))
    fax_display, fax_clean = clean_phone(row.get("FAX番号"))

    lat = safe_float(row.get("緯度"))
    lon = safe_float(row.get("経度"))

    try:
        capacity = int(float(row.get("定員")))
    except (TypeError, ValueError):
        capacity = None

    # 「障害福祉の通常の指定基準を満たしている」列から、障害福祉サービスにも
    # 対応可能かどうかの一次情報を得る（AIリサーチ不要・公式データのみ）。
    shogai_kijun = safe_str(row.get("障害福祉の通常の指定基準を満たしている"))
    disability_welfare_hint = shogai_kijun in ("通常の指定", "共生型")

    remarks = safe_str(row.get("利用可能曜日特記事項"))
    record_name = safe_str(row.get("事業所名")) or ""
    record_address = safe_str(row.get("住所")) or ""

    record = {
        "record_id": make_record_id(jigyosho_no, record_name, record_address),
        "jigyosho_no": jigyosho_no,
        "service_type": SERVICE_TYPE_KAIGO,
        "service_type_label": SERVICE_TYPE_KAIGO_LABEL,
        "name": record_name,
        "name_kana": safe_str(row.get("事業所名カナ")) or "",
        "corporation_name": safe_str(row.get("法人の名称")) or "",
        "prefecture": row.get("都道府県名"),
        "city": row.get("市区町村名"),
        "address": record_address,
        "lat": lat,
        "lon": lon,
        "tel": tel_display,
        "tel_clean": tel_clean,
        "fax": fax_display,
        "fax_clean": fax_clean,
        "url": build_url(row.get("URL")),
        "capacity": capacity,
        "available_days": parse_available_days_from_kaigo(row.get("利用可能曜日")),
        "remarks": remarks,
        "early_late_hint": detect_keyword_hint(remarks, NIGHT_EMERGENCY_KEYWORDS),
        "disability_welfare_hint": disability_welfare_hint,
        "specialty_tags": build_specialty_tags(jigyosho_no, specialty_data),
    }

    return record


# ------------------------------------------------------------
# 12. 1事業所分のレコードを組み立てる（障害福祉：居宅介護）
# ------------------------------------------------------------
def build_shogai_record(row, specialty_data):
    jigyosho_no = safe_str(row.get("事業所番号")) or ""

    tel_display, tel_clean = clean_phone(row.get("事業所電話番号"))
    fax_display, fax_clean = clean_phone(row.get("事業所FAX番号"))

    lat = safe_float(row.get("事業所緯度"))
    lon = safe_float(row.get("事業所経度"))

    try:
        capacity = int(float(row.get("定員")))
    except (TypeError, ValueError):
        capacity = None

    city_part = safe_str(row.get("事業所住所（市区町村）")) or ""
    address_part = safe_str(row.get("事業所住所（番地以降）")) or ""
    full_address = (city_part + address_part).strip()

    prefecture = prefecture_name_from_code(row.get("都道府県コード又は市区町村コード"))
    # 市区町村名は「事業所住所（市区町村）」の先頭から都道府県名を除いた部分を採用
    city = city_part
    if prefecture and city_part.startswith(prefecture):
        city = city_part[len(prefecture):]

    # 早朝・夜間対応：平日・土曜・日曜・祝日いずれかの時間帯が早朝／夜間なら true
    early_late_hint = any(
        detect_early_late_from_time_range(row.get(col))
        for col in [
            "利用可能な時間帯（平日）",
            "利用可能な時間帯（土曜）",
            "利用可能な時間帯（日曜）",
            "利用可能な時間帯（祝日）",
        ]
    )

    remarks = safe_str(row.get("利用可能曜日特記事項（留意事項）"))
    record_name = safe_str(row.get("事業所の名称")) or ""

    record = {
        "record_id": make_record_id(jigyosho_no, record_name, full_address),
        "jigyosho_no": jigyosho_no,
        "service_type": SERVICE_TYPE_SHOGAI,
        "service_type_label": SERVICE_TYPE_SHOGAI_LABEL,
        "name": record_name,
        "name_kana": safe_str(row.get("事業所の名称_かな")) or "",
        "corporation_name": safe_str(row.get("法人の名称")) or "",
        "prefecture": prefecture,
        "city": city,
        "address": full_address,
        "lat": lat,
        "lon": lon,
        "tel": tel_display,
        "tel_clean": tel_clean,
        "fax": fax_display,
        "fax_clean": fax_clean,
        "url": build_url(row.get("事業所URL")),
        "capacity": capacity,
        "available_days": parse_available_days_from_shogai(row.get("定休日")),
        "remarks": remarks,
        "early_late_hint": early_late_hint,
        "disability_welfare_hint": True,  # 障害福祉サービスそのものなので常にtrue
        "specialty_tags": build_specialty_tags(jigyosho_no, specialty_data),
    }

    return record


# ------------------------------------------------------------
# 13. メインのビルド処理
# ------------------------------------------------------------
def main():
    print("==========================================")
    print("🌿 ヘルパー事業所ナビ ビルド開始")
    print("==========================================")

    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # --- AIリサーチ結果の読み込み（存在しなくても安全に継続） ---
    if os.path.exists(SPECIALTY_JSON_PATH):
        try:
            with open(SPECIALTY_JSON_PATH, "r", encoding="utf-8") as f:
                specialty_data = json.load(f)
            print(f"AIリサーチ結果を読み込み：{len(specialty_data)}件分")
        except json.JSONDecodeError as e:
            print(
                f"警告：{SPECIALTY_JSON_PATH} の形式が不正なため、"
                f"AIタグ無しでビルドします：{e}"
            )
            specialty_data = {}
    else:
        specialty_data = {}
        print(f"警告：{SPECIALTY_JSON_PATH} が見つからないため、AIタグ無しでビルドします")

    # --- ①介護保険：訪問介護 CSVの読み込み ---
    df_kaigo = load_csv_with_encoding_fallback(KAIGO_CSV_PATH)
    print(f"訪問介護CSV読み込み完了：全国{len(df_kaigo)}件")

    # --- ②障害福祉：居宅介護 CSVの読み込み ---
    df_shogai = load_csv_with_encoding_fallback(SHOGAI_CSV_PATH)
    print(f"居宅介護CSV読み込み完了：全国{len(df_shogai)}件")

    # --- AIリサーチ済み都道府県一覧の自動導出（訪問看護ナビと同方式）---
    researched_jigyosho_nos = set(specialty_data.keys())

    df_kaigo["_jigyosho_no_normalized"] = df_kaigo["事業所番号"].map(safe_str)
    researched_pref_names_kaigo = set(
        df_kaigo.loc[
            df_kaigo["_jigyosho_no_normalized"].isin(researched_jigyosho_nos),
            "都道府県名",
        ]
    )
    del df_kaigo["_jigyosho_no_normalized"]

    df_shogai["_jigyosho_no_normalized"] = df_shogai["事業所番号"].map(safe_str)
    df_shogai["_prefecture_derived"] = df_shogai["都道府県コード又は市区町村コード"].map(
        prefecture_name_from_code
    )
    researched_pref_names_shogai = set(
        df_shogai.loc[
            df_shogai["_jigyosho_no_normalized"].isin(researched_jigyosho_nos),
            "_prefecture_derived",
        ]
    )

    researched_pref_names = researched_pref_names_kaigo | researched_pref_names_shogai
    researched_prefectures = sorted(
        ALL_PREFECTURES[name] for name in researched_pref_names if name in ALL_PREFECTURES
    )

    # --- 都道府県ごとにレコードを組み立てて出力 ---
    manifest = {
        "kaigo_csv_source": KAIGO_CSV_SOURCE_LABEL,
        "shogai_csv_source": SHOGAI_CSV_SOURCE_LABEL,
        "specialty_research_count": len(specialty_data),
        "specialty_researched_prefectures": researched_prefectures,
        "prefectures": {},
        "total_count": 0,
    }

    for pref_name, pref_slug in ALL_PREFECTURES.items():
        df_pref_kaigo = df_kaigo[df_kaigo["都道府県名"] == pref_name]
        df_pref_shogai = df_shogai[df_shogai["_prefecture_derived"] == pref_name]

        records = (
            [build_kaigo_record(row, specialty_data) for _, row in df_pref_kaigo.iterrows()]
            + [build_shogai_record(row, specialty_data) for _, row in df_pref_shogai.iterrows()]
        )

        output_path = os.path.join(OUTPUT_DIR, f"data_{pref_slug}.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)

        manifest["prefectures"][pref_slug] = {
            "name": pref_name,
            "count": len(records),
            "kaigo_count": len(df_pref_kaigo),
            "shogai_count": len(df_pref_shogai),
        }
        manifest["total_count"] += len(records)

        print(f"  {pref_name}（{pref_slug}）：{len(records)}件 → {output_path}")

    del df_shogai["_jigyosho_no_normalized"]
    del df_shogai["_prefecture_derived"]

    manifest_path = os.path.join(OUTPUT_DIR, "data_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # --- 静的ファイルのコピー（CF Workerのassets配信はdist/配下のみ対象）---
    static_files_to_copy = ["index.html", "favicon.ico", "ads.txt"]
    for filename in static_files_to_copy:
        if os.path.exists(filename):
            shutil.copy(filename, os.path.join(OUTPUT_DIR, filename))
            print(f"  静的ファイルをコピー：{filename} → {OUTPUT_DIR}/{filename}")
        else:
            print(f"  ⚠️ {filename} が見つからないため、コピーをスキップしました（後日追加予定）")

    print("==========================================")
    print(f"✅ ビルド完了：合計{manifest['total_count']}件")
    print(f"マニフェスト：{manifest_path}")
    print("==========================================")


# ------------------------------------------------------------
# 14. 実行
# ------------------------------------------------------------
if __name__ == "__main__":
    main()
