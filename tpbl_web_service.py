import json
import re
import sys
import time
import threading
import webbrowser
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd
import tpbl_sync_service as sync

# ==========================================
# TPBL 台新戰神數據服務 + 本機網頁 API 伺服器
# ==========================================

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8000))
TW_TZ = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML_PATH = BASE_DIR / "index.html"

TPBL_COLS = ["出賽場次", "得分", "籃板", "助攻", "抄截", "阻攻", "三分球命中數"]
SEASON_AVG_COLS = ["出賽場次", "平均得分", "平均籃板", "平均助攻", "平均抄截", "平均阻攻", "平均三分球命中數"]
CAREER_COLS = ["出賽場次", "得分", "籃板", "助攻", "抄截", "阻攻"]

sync_lock = threading.Lock()
state_lock = threading.Lock()

app_state = {
    "season_name": "",
    "last_updated": None,
    "next_update_in": sync.UPDATE_INTERVAL,
    "next_update_ts": None,
    "is_updating": False,
    "last_error": None,
    "players": [],
}


def to_clean_int(val):
    if pd.isna(val):
        return None
    return int(round(float(val)))


def to_clean_float(val):
    if pd.isna(val):
        return None
    return round(float(val), 1)


def calc_avg_stats(totals_dict: dict, stat_cols: list):
    """根據累計數據計算平均數據（保留 None 代表未收錄）"""
    gp = totals_dict.get("出賽場次")
    if gp is None:
        return {("出賽場次" if c == "出賽場次" else f"平均{c}"): None for c in stat_cols}

    result = {"出賽場次": gp}
    for c in stat_cols:
        if c == "出賽場次":
            continue
        val = totals_dict.get(c)
        if val is None:
            result[f"平均{c}"] = None
        elif gp == 0:
            result[f"平均{c}"] = 0.0
        else:
            result[f"平均{c}"] = round(val / gp, 1)
    return result


def find_latest_season_files():
    """尋找最新的本季累計與本季平均 CSV"""
    candidates = [
        p for p in BASE_DIR.glob("mars_players_*_累計.csv")
        if "TPBL_總累計" not in p.name and "生涯_總累計" not in p.name
    ]
    if not candidates:
        return None, None, ""

    total_file = max(candidates, key=lambda p: p.stat().st_mtime)
    match = re.match(r"mars_players_(.+)_累計\.csv", total_file.name)
    season_label = match.group(1) if match else ""
    avg_file = BASE_DIR / f"mars_players_{season_label}_平均.csv"
    return total_file, (avg_file if avg_file.exists() else None), season_label


def build_players_data():
    """從 4 份 CSV 整理出每位球員的 6 組數據"""
    season_total_path, season_avg_path, season_label = find_latest_season_files()
    tpbl_total_path = BASE_DIR / "mars_players_TPBL_總累計.csv"
    career_total_path = BASE_DIR / "mars_players_生涯_總累計.csv"

    if not season_total_path or not tpbl_total_path.exists() or not career_total_path.exists():
        return [], season_label

    df_season_total = pd.read_csv(season_total_path, encoding="utf-8-sig")
    df_season_avg = (
        pd.read_csv(season_avg_path, encoding="utf-8-sig")
        if season_avg_path else pd.DataFrame()
    )
    df_tpbl_total = pd.read_csv(tpbl_total_path, encoding="utf-8-sig")
    df_career_total = pd.read_csv(career_total_path, encoding="utf-8-sig")

    season_total_map = {
        str(row["球員姓名"]).strip(): row for _, row in df_season_total.iterrows()
    }
    season_avg_map = {
        str(row["球員姓名"]).strip(): row for _, row in df_season_avg.iterrows()
    }
    career_total_map = {
        str(row["球員姓名"]).strip(): row for _, row in df_career_total.iterrows()
    }

    players = []
    for _, tpbl_row in df_tpbl_total.iterrows():
        name = str(tpbl_row["球員姓名"]).strip()
        jersey = "" if pd.isna(tpbl_row.get("背號")) else str(tpbl_row["背號"]).strip()

        # 1. 本季累計
        s_tot_row = season_total_map.get(name)
        season_total = (
            {c: to_clean_int(s_tot_row[c]) for c in TPBL_COLS}
            if s_tot_row is not None
            else {c: 0 for c in TPBL_COLS}
        )

        # 2. 本季平均
        s_avg_row = season_avg_map.get(name)
        if s_avg_row is not None:
            season_avg = {
                c: (to_clean_int(s_avg_row[c]) if c == "出賽場次" else to_clean_float(s_avg_row[c]))
                for c in SEASON_AVG_COLS
            }
        else:
            season_avg = calc_avg_stats(season_total, TPBL_COLS)

        # 3. TPBL 累計 & 4. TPBL 平均
        tpbl_total = {c: to_clean_int(tpbl_row[c]) for c in TPBL_COLS}
        tpbl_avg = calc_avg_stats(tpbl_total, TPBL_COLS)

        # 5. 生涯累計 & 6. 生涯平均
        c_tot_row = career_total_map.get(name)
        if c_tot_row is not None:
            career_total = {c: to_clean_int(c_tot_row[c]) for c in CAREER_COLS}
        else:
            career_total = {c: None for c in CAREER_COLS}
        career_avg = calc_avg_stats(career_total, CAREER_COLS)

        players.append({
            "jersey": jersey,
            "name": name,
            "season_total": season_total,
            "season_avg": season_avg,
            "tpbl_total": tpbl_total,
            "tpbl_avg": tpbl_avg,
            "career_total": career_total,
            "career_avg": career_avg,
        })

    return players, season_label


def refresh_cache_from_csv():
    players, season_label = build_players_data()
    with state_lock:
        app_state["players"] = players
        if season_label:
            app_state["season_name"] = season_label


def perform_sync():
    """執行一次 API 爬取與 CSV 更新（含鎖保護避免重複並發）"""
    if not sync_lock.acquire(blocking=False):
        return False

    try:
        with state_lock:
            app_state["is_updating"] = True
            app_state["last_error"] = None

        sync.run_once()
        players, season_label = build_players_data()

        with state_lock:
            app_state["players"] = players
            app_state["season_name"] = season_label
            app_state["last_updated"] = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
            app_state["last_error"] = None
    except Exception as exc:
        sync.logger.error(f"背景同步失敗：{exc}", exc_info=True)
        with state_lock:
            app_state["last_error"] = str(exc)
    finally:
        with state_lock:
            app_state["is_updating"] = False
        sync_lock.release()
    return True


def background_sync_loop(stop_event: threading.Event):
    """每 60 秒自動打一次 API 更新數據"""
    while not stop_event.is_set():
        cycle_start = time.time()
        perform_sync()

        elapsed = time.time() - cycle_start
        sleep_seconds = max(5, int(round(sync.UPDATE_INTERVAL - elapsed)))

        with state_lock:
            app_state["next_update_ts"] = time.time() + sleep_seconds

        # 每 1 秒檢查一次 stop_event，方便按 Ctrl+C 立即結束
        for _ in range(sleep_seconds):
            if stop_event.is_set():
                break
            time.sleep(1)


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 靜音一般 HTTP 請求 log，保持終端機畫面乾淨
        pass

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/api/players"):
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8" if path == "/api/players" else "text/html; charset=utf-8",
            )
            self.end_headers()
            return
        self.send_error(404, "Not Found")

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            if not INDEX_HTML_PATH.exists():
                self.send_error(404, "找不到 index.html")
                return
            html_bytes = INDEX_HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)
            return

        if path == "/api/players":
            with state_lock:
                next_ts = app_state["next_update_ts"]
                remaining = max(0, int(round(next_ts - time.time()))) if next_ts else None
                payload = {
                    "season_name": app_state["season_name"],
                    "last_updated": app_state["last_updated"],
                    "next_update_in": remaining,
                    "is_updating": app_state["is_updating"],
                    "last_error": app_state["last_error"],
                    "players": app_state["players"],
                }
            self._send_json(200, payload)
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        if self.path == "/api/refresh":
            with state_lock:
                already_updating = app_state["is_updating"]
            if not already_updating:
                threading.Thread(target=perform_sync, daemon=True).start()
            self._send_json(202, {"status": "accepted", "already_updating": already_updating})
            return

        self.send_error(404, "Not Found")


def main():
    # 先載入既有 CSV，讓網頁打開瞬間就有資料可看
    refresh_cache_from_csv()

    stop_event = threading.Event()
    worker = threading.Thread(target=background_sync_loop, args=(stop_event,), daemon=True)
    worker.start()

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    url = f"http://127.0.0.1:{PORT}"

    sync.logger.info("=" * 55)
    sync.logger.info(f"戰神數據儀表板已啟動：{url}")
    sync.logger.info("背景每 1 分鐘自動打 TPBL API 更新數據，按 Ctrl + C 關閉")
    sync.logger.info("=" * 55)

    if "--no-browser" not in sys.argv and not os.environ.get("RENDER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sync.logger.info("收到停止訊號，正在關閉服務...")
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        sync.logger.info("服務已安全關閉。")


if __name__ == "__main__":
    main()