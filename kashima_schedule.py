#!/usr/bin/env python3
"""
鹿島アントラーズ 日程取得ツール — Phase 1

機能:
  1. Jリーグ公式サイトから鹿島アントラーズの日程・結果を取得
  2. ホーム/アウェイ・対戦相手・日時・会場・チケットURLを解析
  3. SQLite に保存（差分検出の土台。fingerprint付き）
  4. 確認用のシンプルなHTML一覧を出力

Phase 1 では通知は行わない（Phase 2 で追加）。
UIデザインも未着手（動作確認用の素朴な表）。

使い方:
  python3 kashima_schedule.py            # 取得 → 保存 → HTML出力
  python3 kashima_schedule.py --html-only  # DBの内容からHTMLだけ再生成
  python3 kashima_schedule.py --source sample.html  # ローカルHTMLから解析（テスト用）
"""

import argparse
import hashlib
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None
from bs4 import BeautifulSoup

try:
    import notify
except ImportError:
    notify = None

try:
    import tickets as tickets_mod
except ImportError:
    tickets_mod = None

# ---- 設定 -------------------------------------------------------------

SCHEDULE_URL = "https://www.jleague.jp/sp/match/search/all/all/kashima/"
USER_AGENT = (
    "KashimaScheduleBot/0.1 (personal schedule checker; "
    "contact: your-email@example.com)"
)
DB_PATH = Path("kashima.db")
HTML_OUT = Path("index.html")
CLUB_NAME = "鹿島"  # Jリーグ公式での鹿島の表記

# ---- データ取得 -------------------------------------------------------

def fetch_html(url: str) -> str:
    """Jリーグ公式から日程ページのHTMLを取得する。"""
    if requests is None:
        raise RuntimeError("requests が未インストールです: pip install requests")
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


# ---- 解析 -------------------------------------------------------------

DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
TIME_RE = re.compile(r"([0-2]?\d:[0-5]\d)")
TICKET_RE = re.compile(r"jleague-ticket\.jp/\S+")


def parse_matches(html: str) -> list[dict]:
    """
    日程ページHTMLを解析して試合のリストを返す。

    クラス名に強く依存せず、「日付見出し→大会名→対戦カード」という
    出現順のパターンで拾うため、多少のHTML変更に耐えやすい。
    各試合ブロックは div でまとまっている前提。
    """
    soup = BeautifulSoup(html, "html.parser")
    matches: list[dict] = []

    # 試合ブロックの候補: 日付テキストを含む見出しを起点にする。
    # 構造が読みやすいよう、まずブロック単位（divやsection）を走査する。
    blocks = soup.find_all(["div", "section", "li"])
    seen_keys = set()

    for block in blocks:
        text = block.get_text(" ", strip=True)
        if CLUB_NAME not in text:
            continue
        date_m = DATE_RE.search(text)
        if not date_m:
            continue
        # 対戦カード行（VS または スコア）が含まれるブロックだけを対象にする
        if "ＶＳ" not in text and "VS" not in text and "試合終了" not in text:
            continue

        rec = _parse_block(block, text, date_m)
        if rec is None:
            continue

        key = (rec["date"], rec["home"], rec["away"], rec["kickoff_time"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        matches.append(rec)

    return matches


def _parse_block(block, text: str, date_m) -> dict | None:
    """1つの試合ブロックから情報を抽出する。"""
    year, month, day = (int(date_m.group(i)) for i in (1, 2, 3))
    date_str = f"{year:04d}-{month:02d}-{day:02d}"

    # 大会名（節を含む見出し）
    comp = ""
    comp_tag = block.find(["h3", "h2"])
    if comp_tag:
        comp = comp_tag.get_text(" ", strip=True)

    # 対戦クラブ: club/<slug>/ へのリンクから2チームを拾う
    clubs = []
    for a in block.find_all("a", href=True):
        m = re.search(r"/club/([a-z0-9]+)/", a["href"])
        name = a.get_text(strip=True)
        if m and name and name not in ("",):
            clubs.append((m.group(1), name))
    # 重複を除きつつ順序維持（最初に出る2つがホーム・アウェイ）
    uniq = []
    for slug, name in clubs:
        if (slug, name) not in uniq:
            uniq.append((slug, name))
    if len(uniq) < 2:
        return None
    (home_slug, home_name), (away_slug, away_name) = uniq[0], uniq[1]

    # キックオフ時刻
    time_m = TIME_RE.search(text)
    kickoff_time = time_m.group(1) if time_m else ""

    # 会場: 時刻とVS/結果の後ろにある短い語を会場として推定
    venue = _guess_venue(text)

    # チケットURL
    ticket_m = TICKET_RE.search(str(block))
    ticket_url = ("https://www." + ticket_m.group(0)) if ticket_m else ""

    # ホーム/アウェイ判定（鹿島から見て）
    if CLUB_NAME in home_name:
        home_or_away = "HOME"
    elif CLUB_NAME in away_name:
        home_or_away = "AWAY"
    else:
        home_or_away = "UNKNOWN"

    # 試合状態: スコアが入っていれば終了扱い
    status = "FINISHED" if "試合終了" in text else "SCHEDULED"

    rec = {
        "competition": comp,
        "date": date_str,
        "kickoff_time": kickoff_time,
        "home": home_name,
        "away": away_name,
        "opponent": away_name if home_or_away == "HOME" else home_name,
        "home_or_away": home_or_away,
        "venue": venue,
        "ticket_url": ticket_url,
        "status": status,
    }
    rec["fingerprint"] = _fingerprint(rec)
    return rec


def _guess_venue(text: str) -> str:
    """テキストから会場名らしき短い語を推定する。"""
    # よく出る会場略称（鹿島の対戦で頻出のもの）。網羅でなく補助。
    known = [
        "メルスタ", "メルカリスタジアム", "Ｋｓスタ", "カシマ",
        "埼玉", "日産ス", "味スタ", "フクアリ", "三協Ｆ柏",
        "Ｕ等々力", "等々力", "MUFG国立", "国立",
    ]
    for v in known:
        if v in text:
            return v
    return ""


def _fingerprint(rec: dict) -> str:
    """差分検出用ハッシュ。通知トリガにしたい項目だけを連結する。"""
    basis = "|".join([
        rec["date"], rec["kickoff_time"], rec["home"], rec["away"],
        rec["venue"], rec["ticket_url"], rec["status"],
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# ---- 保存 -------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            competition   TEXT,
            date          TEXT NOT NULL,
            kickoff_time  TEXT,
            home          TEXT,
            away          TEXT,
            opponent      TEXT,
            home_or_away  TEXT,
            venue         TEXT,
            ticket_url    TEXT,
            status        TEXT,
            fingerprint   TEXT,
            updated_at    TEXT,
            UNIQUE(date, home, away, kickoff_time)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_url  TEXT UNIQUE,
            match_label TEXT,
            match_date  TEXT,
            sale_type   TEXT,
            sale_start  TEXT,
            fingerprint TEXT,
            updated_at  TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT,
            match_key   TEXT,
            fingerprint TEXT,
            sent_at     TEXT,
            UNIQUE(match_key, fingerprint)
        )
    """)
    conn.commit()


def upsert_matches(conn: sqlite3.Connection, matches: list[dict]) -> dict:
    """
    試合を保存。fingerprint比較で新規/変更/不変を判定して返す。
    （Phase 2 の通知判定はこの戻り値を使う想定）
    """
    init_db(conn)
    now = datetime.now().isoformat(timespec="seconds")
    result = {"new": [], "changed": [], "unchanged": []}

    for m in matches:
        cur = conn.execute(
            "SELECT id, fingerprint FROM matches WHERE date=? AND home=? AND away=? AND kickoff_time=?",
            (m["date"], m["home"], m["away"], m["kickoff_time"]),
        )
        row = cur.fetchone()
        if row is None:
            conn.execute("""
                INSERT INTO matches
                (competition,date,kickoff_time,home,away,opponent,home_or_away,
                 venue,ticket_url,status,fingerprint,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                m["competition"], m["date"], m["kickoff_time"], m["home"], m["away"],
                m["opponent"], m["home_or_away"], m["venue"], m["ticket_url"],
                m["status"], m["fingerprint"], now,
            ))
            result["new"].append(m)
        elif row[1] != m["fingerprint"]:
            conn.execute("""
                UPDATE matches SET competition=?,opponent=?,home_or_away=?,venue=?,
                ticket_url=?,status=?,fingerprint=?,updated_at=? WHERE id=?
            """, (
                m["competition"], m["opponent"], m["home_or_away"], m["venue"],
                m["ticket_url"], m["status"], m["fingerprint"], now, row[0],
            ))
            result["changed"].append(m)
        else:
            result["unchanged"].append(m)

    conn.commit()
    return result


# ---- HTML出力（動作確認用・デザインは後回し） --------------------------

def render_html(conn: sqlite3.Connection) -> str:
    rows = conn.execute("""
        SELECT date,kickoff_time,competition,home_or_away,opponent,venue,
               ticket_url,status FROM matches ORDER BY date, kickoff_time
    """).fetchall()

    ticket_rows = conn.execute("""
        SELECT match_label, sale_type, sale_start, ticket_url
        FROM tickets ORDER BY sale_start
    """).fetchall()

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    trs = []
    for date, t, comp, hoa, opp, venue, ticket, status in rows:
        hoa_label = {"HOME": "ホーム", "AWAY": "アウェイ"}.get(hoa, hoa or "—")
        ticket_cell = f'<a href="{esc(ticket)}">チケット</a>' if ticket else "—"
        status_label = {"FINISHED": "終了", "SCHEDULED": "予定"}.get(status, status)
        trs.append(
            f"<tr><td>{esc(date)}</td><td>{esc(t)}</td>"
            f"<td>{hoa_label}</td><td>{esc(opp)}</td>"
            f"<td>{esc(venue)}</td><td>{ticket_cell}</td>"
            f"<td>{status_label}</td><td>{esc(comp)}</td></tr>"
        )

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    # チケット発売情報セクション
    if ticket_rows:
        ticket_trs = []
        for label, stype, start, url in ticket_rows:
            link = f'<a href="{esc(url)}">購入</a>' if url else "—"
            ticket_trs.append(
                f"<tr><td>{esc(start)}</td><td>{esc(stype)}</td>"
                f"<td>{esc(label)}</td><td>{link}</td></tr>"
            )
        ticket_section = f"""
<h2>チケット発売情報</h2>
<table>
<thead><tr><th>発売日</th><th>種別</th><th>試合</th><th>購入</th></tr></thead>
<tbody>
{chr(10).join(ticket_trs)}
</tbody></table>
"""
    else:
        ticket_section = '<h2>チケット発売情報</h2><p class="note">現在、発売情報はありません。</p>'

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<title>鹿島アントラーズ 日程・チケット</title>
<style>
  body {{ font-family: sans-serif; margin: 2rem; color: #222; }}
  h1 {{ font-size: 1.2rem; }}
  h2 {{ font-size: 1.05rem; margin-top: 2rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  th, td {{ border: 1px solid #ccc; padding: 6px 8px; text-align: left; }}
  th {{ background: #f2f2f2; }}
  .note {{ color: #777; font-size: 12px; }}
</style></head>
<body>
<h1>鹿島アントラーズ 日程・チケット</h1>
<p class="note">自動更新 / 生成: {generated} / 試合{len(rows)}件</p>
{ticket_section}
<h2>日程一覧</h2>
<table>
<thead><tr><th>日付</th><th>時刻</th><th>H/A</th><th>対戦相手</th>
<th>会場</th><th>チケット</th><th>状態</th><th>大会</th></tr></thead>
<tbody>
{chr(10).join(trs)}
</tbody></table>
</body></html>"""


# ---- メイン -----------------------------------------------------------

def _ticket_fingerprint(t: dict) -> str:
    basis = "|".join([t.get("sale_type", ""), t.get("sale_start", ""),
                      t.get("match_date", "")])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def upsert_tickets(conn, ticket_list):
    """
    チケット発売情報を保存。新規/変更を返す。
    新規 = これまで発売情報がなかった試合に発売情報が出た（発売日確定）
    変更 = 発売日や種別が変わった
    """
    init_db(conn)
    now = datetime.now().isoformat(timespec="seconds")
    result = {"new": [], "changed": []}

    for t in ticket_list:
        fp = _ticket_fingerprint(t)
        row = conn.execute(
            "SELECT id, fingerprint FROM tickets WHERE ticket_url=?",
            (t["ticket_url"],),
        ).fetchone()
        if row is None:
            conn.execute("""
                INSERT INTO tickets
                (ticket_url,match_label,match_date,sale_type,sale_start,fingerprint,updated_at)
                VALUES (?,?,?,?,?,?,?)
            """, (t["ticket_url"], t["match_label"], t["match_date"],
                  t["sale_type"], t["sale_start"], fp, now))
            result["new"].append(t)
        elif row[1] != fp:
            conn.execute("""
                UPDATE tickets SET match_label=?,match_date=?,sale_type=?,
                sale_start=?,fingerprint=?,updated_at=? WHERE id=?
            """, (t["match_label"], t["match_date"], t["sale_type"],
                  t["sale_start"], fp, now, row[0]))
            result["changed"].append(t)

    conn.commit()
    return result


def filter_unnotified(conn, matches, kind):
    """まだ通知していない試合だけを返す（match_key + fingerprint で判定）。"""
    pending = []
    for m in matches:
        key = f"{m['date']}|{m['home']}|{m['away']}|{m['kickoff_time']}"
        row = conn.execute(
            "SELECT 1 FROM notifications_log WHERE match_key=? AND fingerprint=?",
            (key, m["fingerprint"]),
        ).fetchone()
        if row is None:
            pending.append((key, m))
    return pending


def mark_notified(conn, kind, pending):
    """通知済みとして記録する。"""
    now = datetime.now().isoformat(timespec="seconds")
    for key, m in pending:
        conn.execute(
            "INSERT OR IGNORE INTO notifications_log (kind, match_key, fingerprint, sent_at) VALUES (?,?,?,?)",
            (kind, key, m["fingerprint"], now),
        )
    conn.commit()


def maybe_notify(conn, res):
    """差分があり、未通知のものがあればメール送信する。"""
    if notify is None:
        print("notify モジュールが無いため通知スキップ")
        return

    new_pending = filter_unnotified(conn, res["new"], "new_match")
    changed_pending = filter_unnotified(conn, res["changed"], "changed")

    new_list = [m for _, m in new_pending]
    changed_list = [m for _, m in changed_pending]

    subject, body = notify.build_message(new_list, changed_list)
    if not subject:
        print("通知対象なし（未通知の差分なし）")
        return

    ok = notify.send_email(subject, body)
    if ok:
        mark_notified(conn, "new_match", new_pending)
        mark_notified(conn, "changed", changed_pending)
        print(f"通知済みに記録: 新規{len(new_pending)} / 変更{len(changed_pending)}")
    else:
        print("送信できなかったため通知ログは更新せず（次回再試行）")


def maybe_notify_tickets(conn, ticket_url=None, source=None):
    """チケット発売情報を取得し、新規・変更があれば通知する。"""
    if tickets_mod is None:
        print("tickets モジュールが無いためチケット取得スキップ")
        return
    try:
        if source:
            html = Path(source).read_text(encoding="utf-8")
        else:
            html = tickets_mod.fetch_ticket_html()
        ticket_list = tickets_mod.parse_tickets(html)
    except Exception as e:
        print(f"チケット取得失敗: {e}")
        return

    print(f"チケット発売情報: {len(ticket_list)}件")
    res = upsert_tickets(conn, ticket_list)
    print(f"  発売新規: {len(res['new'])}  発売変更: {len(res['changed'])}")

    if notify is None:
        return

    # 二重送信防止（ticket_url + fingerprint をキーに）
    new_pending, changed_pending = [], []
    for t in res["new"]:
        key = f"ticket|{t['ticket_url']}"
        fp = _ticket_fingerprint(t)
        if conn.execute("SELECT 1 FROM notifications_log WHERE match_key=? AND fingerprint=?",
                        (key, fp)).fetchone() is None:
            new_pending.append((key, fp, t))
    for t in res["changed"]:
        key = f"ticket|{t['ticket_url']}"
        fp = _ticket_fingerprint(t)
        if conn.execute("SELECT 1 FROM notifications_log WHERE match_key=? AND fingerprint=?",
                        (key, fp)).fetchone() is None:
            changed_pending.append((key, fp, t))

    subject, body = notify.build_ticket_message(
        [t for _, _, t in new_pending], [t for _, _, t in changed_pending])
    if not subject:
        print("チケット通知対象なし")
        return

    if notify.send_email(subject, body):
        now = datetime.now().isoformat(timespec="seconds")
        for key, fp, _ in new_pending + changed_pending:
            conn.execute(
                "INSERT OR IGNORE INTO notifications_log (kind, match_key, fingerprint, sent_at) VALUES (?,?,?,?)",
                ("ticket_sale", key, fp, now))
        conn.commit()
        print(f"チケット通知済みに記録: {len(new_pending + changed_pending)}件")
    else:
        print("チケット通知の送信失敗（次回再試行）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="ローカルHTMLファイルから解析（テスト用）")
    ap.add_argument("--html-only", action="store_true",
                    help="取得せずDBからHTMLだけ再生成")
    ap.add_argument("--no-notify", action="store_true",
                    help="メール通知を行わない")
    ap.add_argument("--no-tickets", action="store_true",
                    help="チケット発売情報の取得を行わない")
    ap.add_argument("--ticket-source",
                    help="チケット情報をローカルHTMLから解析（テスト用）")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)

    if args.html_only:
        init_db(conn)
        HTML_OUT.write_text(render_html(conn), encoding="utf-8")
        print(f"HTML を再生成: {HTML_OUT}")
        return

    if args.source:
        html = Path(args.source).read_text(encoding="utf-8")
        print(f"ローカルHTMLから解析: {args.source}")
    else:
        print(f"取得中: {SCHEDULE_URL}")
        html = fetch_html(SCHEDULE_URL)

    matches = parse_matches(html)
    print(f"解析できた試合数: {len(matches)}")

    res = upsert_matches(conn, matches)
    print(f"  新規: {len(res['new'])}  変更: {len(res['changed'])}  "
          f"不変: {len(res['unchanged'])}")
    if res["new"]:
        for m in res["new"][:5]:
            print(f"    [新規] {m['date']} {m['home_or_away']} vs {m['opponent']} @ {m['venue']}")

    if not args.no_notify:
        maybe_notify(conn, res)

    # チケット発売情報の取得・通知（Phase 3）
    if not args.no_tickets:
        ticket_source = args.ticket_source if hasattr(args, "ticket_source") else None
        maybe_notify_tickets(conn, source=ticket_source)

    HTML_OUT.write_text(render_html(conn), encoding="utf-8")
    print(f"HTML を出力: {HTML_OUT}")


if __name__ == "__main__":
    main()
