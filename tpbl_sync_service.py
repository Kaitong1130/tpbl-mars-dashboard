import csv
import os
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime

import pandas as pd
import requests

# ==========================================
# TPBL 台北台新戰神數據自動更新服務
# 每 60 秒抓取 API 並更新本季、TPBL總計、生涯總計
# ==========================================

BASE_URL = "https://api.tpbl.basketball/api"
TEAM_NAME = "臺北台新戰神"

UPDATE_INTERVAL = 60       # 更新週期（秒）
REQUEST_INTERVAL = 1.1     # 每次 API 呼叫間隔（秒）
LAST_REQUEST_TIME = 0.0

BASE_DIR = Path(__file__).resolve().parent
BASELINE_TPBL_PATH = BASE_DIR / "baseline_tpbl.csv"
BASELINE_CAREER_PATH = BASE_DIR / "baseline_career.csv"
LOG_PATH = BASE_DIR / "tpbl_service.log"

STAT_FIELDS = [
    "score",
    "rebounds",
    "assists",
    "steals",
    "blocks",
    "three_pointers_made",
]

TOTAL_FIELDS = ["球員姓名", "出賽場次", "得分", "籃板", "助攻", "抄截", "阻攻", "三分球命中數"]
AVERAGE_FIELDS = ["球員姓名", "出賽場次", "平均得分", "平均籃板", "平均助攻", "平均抄截", "平均阻攻", "平均三分球命中數"]
TPBL_STAT_COLS = ["出賽場次", "得分", "籃板", "助攻", "抄截", "阻攻", "三分球命中數"]
CAREER_STAT_COLS = ["出賽場次", "得分", "籃板", "助攻", "抄截", "阻攻"]


# ==========================================
# Logging 設定（終端機 + 自動輪替 Log 檔）
# ==========================================

def setup_logger():
    logger = logging.getLogger("TPBLService")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        fmt = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S")

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(fmt)
        logger.addHandler(console_handler)

        file_handler = RotatingFileHandler(
            LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger


logger = setup_logger()

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 TPBL-Stats-Crawler/1.0",
    "Accept": "application/json",
})


# ==========================================
# API 與資料解析函式
# ==========================================

def api_get(endpoint, params=None):
    global LAST_REQUEST_TIME

    elapsed = time.time() - LAST_REQUEST_TIME
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)

    url = f"{BASE_URL}{endpoint}"

    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=30)
            LAST_REQUEST_TIME = time.time()

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue

            response.raise_for_status()
            return response.json()

        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)

    raise RuntimeError(f"API request failed: {url}")


def extract_rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "items", "results", "records", "players", "teams", "divisions"):
            if key in data:
                result = extract_rows(data[key])
                if result:
                    return result
        for value in data.values():
            if isinstance(value, (dict, list)):
                result = extract_rows(value)
                if result:
                    return result
    return []


def pick(data, *keys, default=None):
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def number(value):
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0.0


def clean_number(value):
    value = round(number(value), 2)
    return int(value) if value.is_integer() else value


def get_current_season():
    seasons = extract_rows(api_get("/seasons"))
    if not seasons:
        raise RuntimeError("API 沒有回傳任何賽季資料")

    active = [s for s in seasons if s.get("status") == "IN_PROGRESS"]
    target_list = active if active else seasons
    season = max(target_list, key=lambda x: int(pick(x, "id", "season_id", default=0)))
    return season, int(pick(season, "id", "season_id"))


def get_team(season_id):
    teams = extract_rows(api_get(f"/events/{season_id}/teams"))
    for team in teams:
        name = str(pick(team, "name", default=""))
        normalized = name.replace("臺", "台").replace(" ", "")
        if TEAM_NAME.replace("臺", "台") in normalized or pick(team, "alt_name") == "mars":
            return team, int(pick(team, "team_id", "id"))
    raise RuntimeError("找不到台北台新戰神")


def get_regular_division(season_id):
    divisions = extract_rows(api_get(f"/events/{season_id}/divisions"))
    regular = [d for d in divisions if "例行賽" in str(pick(d, "name", default=""))]
    if not regular:
        raise RuntimeError("找不到例行賽 Division")

    division = next((d for d in regular if d.get("status") == "IN_PROGRESS"), regular[0])
    return int(pick(division, "division_id", "id"))


def get_roster(division_id, team_id):
    return extract_rows(api_get(f"/divisions/{division_id}/teams/{team_id}/players"))


def get_player_games(player_id, division_id):
    endpoint = f"/players/{player_id}/games/stats"
    try:
        raw = api_get(endpoint, params={"division_id": division_id})
        rows = extract_rows(raw)
        for row in rows:
            stats = row.get("accumulated_stats") or {}
            if isinstance(stats, dict):
                row.update(stats)
        return rows
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 404:
            return []
        raise


def calculate_stats(player, games):
    totals = {field: sum(number(game.get(field)) for game in games) for field in STAT_FIELDS}
    games_played = len(games)
    player_name = str(pick(player, "player", "name", default="未知球員")).strip()

    total_data = {
        "球員姓名": player_name,
        "出賽場次": games_played,
        "得分": clean_number(totals["score"]),
        "籃板": clean_number(totals["rebounds"]),
        "助攻": clean_number(totals["assists"]),
        "抄截": clean_number(totals["steals"]),
        "阻攻": clean_number(totals["blocks"]),
        "三分球命中數": clean_number(totals["three_pointers_made"]),
    }

    def avg(field):
        return 0.0 if games_played == 0 else round(totals[field] / games_played, 1)

    average_data = {
        "球員姓名": player_name,
        "出賽場次": games_played,
        "平均得分": avg("score"),
        "平均籃板": avg("rebounds"),
        "平均助攻": avg("assists"),
        "平均抄截": avg("steals"),
        "平均阻攻": avg("blocks"),
        "平均三分球命中數": avg("three_pointers_made"),
    }
    return total_data, average_data


# ==========================================
# 安全原子寫入（避免讀寫衝突 / Excel 鎖定）
# ==========================================

def save_csv_atomic(filename: str, data: list, fields: list):
    if not data:
        logger.warning(f"沒有資料可輸出：{filename}")
        return

    filepath = BASE_DIR / filename
    tmp_path = filepath.with_suffix(".csv.tmp")

    try:
        with open(tmp_path, "w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(data)
        os.replace(tmp_path, filepath)
    except PermissionError:
        logger.warning(f"檔案可能正被 Excel 開啟鎖定，略過本次更新：{filename}")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def save_df_atomic(df: pd.DataFrame, filename: str):
    filepath = BASE_DIR / filename
    tmp_path = filepath.with_suffix(".csv.tmp")

    try:
        df.to_csv(tmp_path, index=False, encoding="utf-8-sig")
        os.replace(tmp_path, filepath)
    except PermissionError:
        logger.warning(f"檔案可能正被 Excel 開啟鎖定，略過本次更新：{filename}")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


# ==========================================
# 結合 Baseline 更新 TPBL & 生涯總累計
# ==========================================

def update_with_baselines(total_data: list, roster: list):
    if not BASELINE_TPBL_PATH.exists() or not BASELINE_CAREER_PATH.exists():
        logger.warning("找不到 baseline_tpbl.csv 或 baseline_career.csv，僅更新本季數據。")
        return

    tpbl_base = pd.read_csv(BASELINE_TPBL_PATH, encoding="utf-8-sig")
    career_base = pd.read_csv(BASELINE_CAREER_PATH, encoding="utf-8-sig")
    season_df = pd.DataFrame(total_data)

    # 若季中有新球員，從 roster 補上背號
    roster_jersey_map = {}
    for p in roster:
        p_name = str(pick(p, "player", "name", default="")).strip()
        p_num = pick(p, "jersey_number", "number", "背號", default="")
        if p_name and p_num != "":
            roster_jersey_map[p_name] = f"#{p_num}" if not str(p_num).startswith("#") else str(p_num)

    # 1. 更新 TPBL 總累計（outer merge 可自動容納季中新簽約球員）
    tpbl_merged = pd.merge(
        tpbl_base, season_df, on="球員姓名", how="outer", suffixes=("_base", "_api")
    )
    tpbl_merged["背號"] = tpbl_merged["背號"].fillna(
        tpbl_merged["球員姓名"].map(roster_jersey_map)
    ).fillna("")

    latest_tpbl = tpbl_merged[["背號", "球員姓名"]].copy()
    for col in TPBL_STAT_COLS:
        b = tpbl_merged[f"{col}_base"].fillna(0).astype("Int64")
        a = tpbl_merged[f"{col}_api"].fillna(0).astype("Int64")
        latest_tpbl[col] = (b + a).astype("Int64")

    # 2. 更新生涯總累計（僅累加有紀錄的球員，洋將維持空白）
    career_merged = pd.merge(
        career_base,
        season_df[["球員姓名"] + CAREER_STAT_COLS],
        on="球員姓名",
        how="left",
        suffixes=("_base", "_api"),
    )
    latest_career = career_merged[["背號", "球員姓名"]].copy()
    for col in CAREER_STAT_COLS:
        b = career_merged[f"{col}_base"].astype("Int64")
        a = career_merged[f"{col}_api"].fillna(0).astype("Int64")
        latest_career[col] = (b + a).astype("Int64")

    save_df_atomic(latest_tpbl, "mars_players_TPBL_總累計.csv")
    save_df_atomic(latest_career, "mars_players_生涯_總累計.csv")


# ==========================================
# 單次同步任務
# ==========================================

def run_once():
    season, season_id = get_current_season()
    _, team_id = get_team(season_id)
    division_id = get_regular_division(season_id)
    roster = get_roster(division_id, team_id)

    total_data = []
    average_data = []

    for player in roster:
        player_id = pick(player, "player_id", "id")
        games = get_player_games(player_id, division_id)
        player_total, player_average = calculate_stats(player, games)
        total_data.append(player_total)
        average_data.append(player_average)

    total_data.sort(key=lambda row: number(row["得分"]), reverse=True)
    player_order = {row["球員姓名"]: idx for idx, row in enumerate(total_data)}
    average_data.sort(key=lambda row: player_order.get(row["球員姓名"], 999))

    season_name = str(season.get("name", "TPBL")).replace(" 賽季", "").replace(" ", "")

    save_csv_atomic(f"mars_players_{season_name}_累計.csv", total_data, TOTAL_FIELDS)
    save_csv_atomic(f"mars_players_{season_name}_平均.csv", average_data, AVERAGE_FIELDS)
    update_with_baselines(total_data, roster)

    logger.info(
        f"更新完成｜賽季：{season.get('name')}｜球員數：{len(total_data)}｜"
        f"檔案：累計 / 平均 / TPBL_總累計 / 生涯_總累計"
    )


# ==========================================
# 服務主迴圈
# ==========================================

def start_service():
    logger.info("=" * 55)
    logger.info("TPBL 台北台新戰神數據自動更新服務已啟動（每 1 分鐘更新）")
    logger.info("按 Ctrl + C 可安全停止服務")
    logger.info("=" * 55)

    try:
        while True:
            cycle_start = time.time()
            logger.info("開始同步最新數據...")

            try:
                run_once()
            except Exception as exc:
                logger.error(f"本次更新失敗（將於下個週期重試）：{exc}", exc_info=True)

            elapsed = time.time() - cycle_start
            # 扣掉本次抓取花費的 ~22 秒，讓下次執行精準落在約 1 分鐘後（最少冷卻 5 秒）
            sleep_seconds = max(5, int(round(UPDATE_INTERVAL - elapsed)))
            logger.info(f"本次耗時 {elapsed:.1f} 秒，等待 {sleep_seconds} 秒後進行下次更新...\n")

            # 分段 sleep，確保按 Ctrl+C 時能於 1 秒內立即中斷
            for _ in range(sleep_seconds):
                time.sleep(1)

    except KeyboardInterrupt:
        logger.info("收到停止訊號，服務已安全關閉。")


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        start_service()