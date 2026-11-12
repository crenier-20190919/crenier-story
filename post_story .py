"""
営業開始ストーリーを毎朝自動投稿するプログラム(美容室 crenier 用)

- images/ フォルダの画像(.jpg)と動画(.mp4 / .mov)を使う
- 日曜・祝日は、名前が「00」で始まるファイル(00, 00b …)を交互に投稿
- それ以外の日は、残りのファイルを番号順にローテーション(画像と動画が混ざってOK)
- 休み:毎週月曜 / 2回目・4回目の火曜 / 12月30日〜1月5日 / holidays.txt に書いた日付
- 投稿に成功した日だけ順番を進める(state.json に記録)
"""

import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jpholiday

ROOT = Path(__file__).resolve().parent
IMAGES_DIR = ROOT / "images"
STATE_FILE = ROOT / "state.json"
HOLIDAYS_FILE = ROOT / "holidays.txt"

API = "https://graph.facebook.com/v23.0"
JST = datetime.timezone(datetime.timedelta(hours=9))

# crenierのインスタのユーザーID
IG_USER_ID = "17841449360754731"

# 日曜・祝日用のファイル名の頭
SUNDAY_PREFIX = "00"

IMAGE_EXT = (".jpg", ".jpeg")
VIDEO_EXT = (".mp4", ".mov")


# ---------- 休みの判定 ----------

def load_extra_holidays():
    days = set()
    if HOLIDAYS_FILE.exists():
        for line in HOLIDAYS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if not line:
                continue
            try:
                days.add(datetime.date.fromisoformat(line))
            except ValueError:
                print(f"holidays.txt の書き方が違う行をスキップ: {line}")
    return days


def holiday_reason(d, extra):
    if d.weekday() == 0:
        return "月曜の定休日"
    if d.weekday() == 1:
        nth = (d.day - 1) // 7 + 1
        if nth in (2, 4):
            return f"{nth}回目の火曜の定休日"
    if (d.month == 12 and d.day >= 30) or (d.month == 1 and d.day <= 5):
        return "年末年始のお休み"
    if d in extra:
        return "臨時休業(holidays.txt)"
    return None


# ---------- ファイルの選び方 ----------

def is_sunday_or_holiday(d):
    return d.weekday() == 6 or jpholiday.is_holiday(d)


def number_in_name(path):
    m = re.search(r"\d+", path.stem)
    return int(m.group()) if m else 10**9


def find_media():
    return [p for p in IMAGES_DIR.iterdir()
            if p.suffix.lower() in IMAGE_EXT + VIDEO_EXT]


# ---------- 状態 ----------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"next": 0, "last_posted": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- Instagram API ----------

def api(method, path, params):
    url = f"{API}/{path}"
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Instagram APIエラー ({e.code}): {body}") from None


def post_story(token, media_url, is_video):
    me = api("GET", IG_USER_ID, {"fields": "username", "access_token": token})
    print(f"アカウント: @{me.get('username')}")

    params = {"media_type": "STORIES", "access_token": token}
    params["video_url" if is_video else "image_url"] = media_url
    cid = api("POST", f"{IG_USER_ID}/media", params)["id"]

    # 準備完了を待つ(画像は約2分、動画は約10分まで)
    tries = 120 if is_video else 24
    for _ in range(tries):
        status = api("GET", cid, {"fields": "status_code", "access_token": token}).get("status_code")
        if status == "FINISHED":
            break
        if status == "ERROR":
            raise RuntimeError("Instagram側で処理に失敗しました(形式・長さ・サイズを確認してください)")
        time.sleep(5)
    else:
        raise RuntimeError("Instagram側の処理が時間内に終わりませんでした")

    result = api("POST", f"{IG_USER_ID}/media_publish", {"creation_id": cid, "access_token": token})
    print(f"投稿しました(media id: {result.get('id')})")


# ---------- メイン ----------

def main():
    token = (os.environ.get("IG_TOKEN") or "").strip()
    if not token:
        sys.exit("IG_TOKEN が設定されていません(GitHubのSecretsを確認してください)")

    force = os.environ.get("FORCE") == "true"
    today = datetime.datetime.now(JST).date()
    state = load_state()

    if not force:
        reason = holiday_reason(today, load_extra_holidays())
        if reason:
            print(f"{today} は{reason}なので投稿しません")
            return
        if state.get("last_posted") == str(today):
            print(f"{today} はもう投稿済みなのでスキップします")
            return

    media = find_media()
    if not media:
        sys.exit("images フォルダに画像・動画がありません")

    sunday_files = sorted(p for p in media if p.stem.startswith(SUNDAY_PREFIX))
    rotation = sorted((p for p in media if p not in sunday_files), key=lambda p: (number_in_name(p), p.name))
    if not rotation:
        sys.exit("ローテーション用のファイルがありません")

    use_sunday = is_sunday_or_holiday(today) and bool(sunday_files)
    if use_sunday:
        sidx = state.get("sunday_next", 0) % len(sunday_files)
        item = sunday_files[sidx]
        print(f"今日は日曜・祝日なので {item.name} を使います")
    else:
        if is_sunday_or_holiday(today):
            print("注意:日祝用(00)のファイルが見つからないので、通常のローテーションで投稿します")
        idx = state.get("next", 0) % len(rotation)
        item = rotation[idx]
        print(f"今日のファイル: {item.name}({idx + 1}/{len(rotation)}本目)")

    is_video = item.suffix.lower() in VIDEO_EXT
    repo = os.environ["GITHUB_REPOSITORY"]
    sha = os.environ["GITHUB_SHA"]
    url = f"https://raw.githubusercontent.com/{repo}/{sha}/images/{urllib.parse.quote(item.name)}"
    post_story(token, url, is_video)

    if use_sunday:
        state["sunday_next"] = (sidx + 1) % len(sunday_files)
    else:
        state["next"] = (idx + 1) % len(rotation)
    state["last_posted"] = str(today)
    save_state(state)


if __name__ == "__main__":
    main()
