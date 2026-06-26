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
# 試合詳細リンク: /match/<大会>/<年>/<6桁ID>/live のような形
MATCH_LINK_RE = re.compile(r"/match/[a-z0-9]+/\d{4}/\d{6}/(?:live|ticket)")
# クラブへのリンク
CLUB_LINK_RE = re.compile(r"/club/([a-z0-9]+)/")


def parse_matches(html: str) -> list[dict]:
    """
    日程ページHTMLを解析して試合のリストを返す。

    Jリーグ公式の実構造に合わせ、「試合詳細リンク(/match/.../live)」を
    含む要素を1試合の単位として拾う。各試合の対戦カードは
    「| 鹿島 | 得点 | 時刻 状態 会場 | 得点 | 相手 |」のように
    1行に潰れたテーブル形式なので、テキストとリンク順から復元する。

    日付・大会名は試合要素の直前に見出し(h4/h3)として現れるため、
    文書順に走査して直近の日付・大会を各試合に紐づける。
    """
    soup = BeautifulSoup(html, "html.parser")
    matches: list[dict] = []
    seen_keys = set()

    current_date = ""   # 直近に見つかった日付 (YYYY-MM-DD)
    current_comp = ""   # 直近に見つかった大会名

    # 文書順に全要素を走査
    for el in soup.find_all(["h2", "h3", "h4", "h5", "a"]):
        name = el.name
        text = el.get_text(" ", strip=True)

        # 見出し: 日付か大会名かを判定して記憶
        if name in ("h2", "h3", "h4", "h5"):
            dm = DATE_RE.search(text)
            if dm:
                y, mo, d = (int(dm.group(i)) for i in (1, 2, 3))
                current_date = f"{y:04d}-{mo:02d}-{d:02d}"
            elif any(k in text for k in ("リーグ", "カップ", "天皇杯",
                                         "プレーオフ", "ＡＣ", "AC",
                                         "スーパーカップ", "チャレンジ")):
                current_comp = text
            continue

        # リンク: 試合詳細リンクを持つものを試合の単位とする
        if name == "a":
            href = el.get("href", "")
            if not MATCH_LINK_RE.search(href):
                continue
            rec = _parse_match_link(el, text, current_date, current_comp)
            if rec is None:
                continue
            key = (rec["date"], rec["home"], rec["away"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            matches.append(rec)

    return matches


def _parse_match_link(el, text: str, date_str: str, comp: str) -> dict | None:
    """
    試合詳細リンク要素から1試合の情報を抽出する。
    リンク内のテキストは
      「| 鹿島 | 1 | 15:00 試合終了 メルスタ | 0 | 横浜FM | DAZN |」
    のような形。内側の club リンクからホーム・アウェイ名を取る。
    """
    if not date_str:
        return None

    # 対戦クラブ名: リンク内の /club/ リンクを順に拾う（先=ホーム, 後=アウェイ）
    clubs = []
    for a in el.find_all("a", href=True):
        m = CLUB_LINK_RE.search(a["href"])
        nm = a.get_text(strip=True)
        if m and nm:
            clubs.append(nm)
    # リンク内に club リンクが無い場合がある（テキストだけ）のでフォールバック
    if len(clubs) < 2:
        return None
    home_name, away_name = clubs[0], clubs[1]

    # 時刻: 「未定」も許容。HH:MM があれば採用、なければ "未定" を判定
    time_m = TIME_RE.search(text)
    if time_m:
        kickoff_time = time_m.group(1)
    elif "未定" in text:
        kickoff_time = "未定"
    else:
        kickoff_time = ""

    venue = _guess_venue(text)

    ticket_m = TICKET_RE.search(str(el))
    ticket_url = ("https://www." + ticket_m.group(0)) if ticket_m else ""

    # ホーム/アウェイ判定（鹿島視点）
    if CLUB_NAME in home_name:
        home_or_away = "HOME"
    elif CLUB_NAME in away_name:
        home_or_away = "AWAY"
    else:
        home_or_away = "UNKNOWN"

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
        "メルカリスタジアム", "メルスタ", "Ｋｓスタ", "カシマ",
        "MUFG国立", "国立", "埼玉", "日産ス", "味スタ", "フクアリ",
        "三協Ｆ柏", "Ｕ等々力", "等々力", "豊田ス", "パナスタ",
        "ヨドコウ", "ノエスタ", "エディオンピース", "ベススタ",
        "トラスタ", "ニッパツ", "レモンガススタジアム",
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

    today = datetime.now().strftime("%Y-%m-%d")

    # 曜日を求める補助
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]

    def fmt_date(date_str):
        """YYYY-MM-DD → 「8/7(金)」形式に。失敗時はそのまま返す。"""
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            return f"{d.month}/{d.day}（{weekdays[d.weekday()]}）"
        except Exception:
            return date_str

    def match_card(r, is_next=False):
        date, t, comp, hoa, opp, venue, ticket, status = r
        hoa_label = {"HOME": "HOME", "AWAY": "AWAY"}.get(hoa, "—")
        hoa_class = {"HOME": "home", "AWAY": "away"}.get(hoa, "")
        time_label = t if t else "時刻未定"
        # 対戦表記: ホームなら「鹿島 vs 相手」、アウェイなら「相手 vs 鹿島」風に主役を出す
        vs = f'<span class="opp">{esc(opp)}</span>'
        ticket_btn = (f'<a class="ticket-btn" href="{esc(ticket)}" '
                      f'target="_blank" rel="noopener">チケット</a>') if ticket else ""
        next_badge = '<span class="next-badge">NEXT</span>' if is_next else ""
        comp_label = esc(comp) if comp else ""
        return f"""<div class="card {hoa_class}{' next' if is_next else ''}">
  <div class="card-top">
    <span class="date">{fmt_date(date)}</span>
    <span class="hoa {hoa_class}">{hoa_label}</span>
    {next_badge}
  </div>
  <div class="card-main">
    <span class="vs-label">vs</span> {vs}
  </div>
  <div class="card-meta">
    <span class="time">{esc(time_label)}</span>
    <span class="venue">{esc(venue) if venue else ''}</span>
  </div>
  <div class="card-comp">{comp_label}</div>
  {ticket_btn}
</div>"""

    # 今後/終了に分ける
    upcoming, finished = [], []
    for r in rows:
        date, t, comp, hoa, opp, venue, ticket, status = r
        if status == "FINISHED" or (date and date < today):
            finished.append(r)
        else:
            upcoming.append(r)

    # 今後の試合カード（先頭をNEXTとして強調）
    up_cards = []
    for i, r in enumerate(upcoming):
        up_cards.append(match_card(r, is_next=(i == 0)))
    fin_cards = [match_card(r) for r in reversed(finished)]

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    # チケット発売情報
    if ticket_rows:
        tcards = []
        for label, stype, start, url in ticket_rows:
            btn = (f'<a class="ticket-btn" href="{esc(url)}" target="_blank" '
                   f'rel="noopener">購入ページ</a>') if url else ""
            stype_label = f'<span class="sale-type">{esc(stype)}</span>' if stype else ""
            tcards.append(f"""<div class="ticket-card">
  <div class="sale-head">{stype_label}<span class="sale-start">発売 {esc(start)}</span></div>
  <div class="sale-match">{esc(label)}</div>
  {btn}
</div>""")
        ticket_section = f"""<section>
  <h2><span class="h2-mark"></span>チケット発売情報</h2>
  {chr(10).join(tcards)}
</section>"""
    else:
        ticket_section = """<section>
  <h2><span class="h2-mark"></span>チケット発売情報</h2>
  <p class="empty">いまは発売中のチケットはありません。発売が決まると、ここに表示されます。</p>
</section>"""

    if up_cards:
        upcoming_section = f"""<section>
  <h2><span class="h2-mark"></span>今後の試合 <span class="count">{len(up_cards)}</span></h2>
  {chr(10).join(up_cards)}
</section>"""
    else:
        upcoming_section = """<section>
  <h2><span class="h2-mark"></span>今後の試合</h2>
  <p class="empty">今後の試合はまだ取得できていません。</p>
</section>"""

    if fin_cards:
        finished_section = f"""<section>
  <h2><span class="h2-mark"></span>終了した試合 <span class="count">{len(fin_cards)}</span></h2>
  {chr(10).join(fin_cards)}
</section>"""
    else:
        finished_section = ""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>鹿島アントラーズ 日程・チケット</title>
<style>
  :root {{
    --red: #B7183F;       /* アントラーズレッド */
    --red-dark: #8E0E2E;
    --navy: #1A2238;      /* サポートカラー ネイビー */
    --ink: #1c1c20;
    --paper: #f6f4f2;
    --line: #e4dfdb;
    --muted: #8a8378;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, "Hiragino Kaku Gothic ProN", "Noto Sans JP", sans-serif;
    background: var(--paper);
    color: var(--ink);
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 560px; margin: 0 auto; padding: 0 0 48px; }}

  /* ヘッダー */
  header {{
    background: linear-gradient(135deg, var(--red) 0%, var(--red-dark) 100%);
    color: #fff;
    padding: 28px 20px 22px;
    position: relative;
    overflow: hidden;
  }}
  header::after {{
    /* 鹿の角を想起させる斜めのサッシュ（懸章） */
    content: "";
    position: absolute;
    top: -40px; right: -30px;
    width: 140px; height: 200px;
    background: rgba(255,255,255,0.06);
    transform: rotate(20deg);
  }}
  header .club {{ font-size: 13px; letter-spacing: 0.18em; opacity: 0.85; font-weight: 600; }}
  header h1 {{ font-size: 26px; font-weight: 800; letter-spacing: 0.02em; margin-top: 2px; }}
  header .sub {{ font-size: 12px; opacity: 0.8; margin-top: 8px; }}

  /* セクション見出し */
  section {{ padding: 0 16px; margin-top: 28px; }}
  h2 {{
    font-size: 16px; font-weight: 800; color: var(--navy);
    display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
  }}
  .h2-mark {{
    width: 5px; height: 18px; background: var(--red);
    border-radius: 3px; display: inline-block;
  }}
  .count {{
    font-size: 12px; font-weight: 700; color: #fff; background: var(--navy);
    border-radius: 10px; padding: 1px 9px; margin-left: 2px;
  }}

  /* 試合カード */
  .card {{
    background: #fff; border-radius: 14px; padding: 14px 16px;
    margin-bottom: 10px; border: 1px solid var(--line);
    box-shadow: 0 1px 2px rgba(0,0,0,0.03);
    position: relative;
  }}
  .card.next {{
    border: 1.5px solid var(--red);
    box-shadow: 0 4px 16px rgba(183,24,63,0.13);
  }}
  .card-top {{ display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }}
  .date {{ font-size: 14px; font-weight: 700; color: var(--ink); }}
  .hoa {{
    font-size: 10px; font-weight: 800; letter-spacing: 0.08em;
    padding: 2px 7px; border-radius: 4px; color: #fff;
  }}
  .hoa.home {{ background: var(--red); }}
  .hoa.away {{ background: var(--navy); }}
  .next-badge {{
    margin-left: auto; font-size: 10px; font-weight: 800; letter-spacing: 0.1em;
    color: var(--red); border: 1.5px solid var(--red); border-radius: 4px;
    padding: 1px 7px;
  }}
  .card-main {{ font-size: 20px; font-weight: 800; color: var(--ink); margin: 2px 0; }}
  .vs-label {{ font-size: 13px; font-weight: 600; color: var(--muted); margin-right: 4px; }}
  .card-meta {{ display: flex; gap: 14px; font-size: 13px; color: #555; margin-top: 4px; }}
  .card-meta .time::before {{ content: "🕐 "; }}
  .card-meta .venue::before {{ content: "📍 "; }}
  .card-comp {{ font-size: 11px; color: var(--muted); margin-top: 7px; }}
  .ticket-btn {{
    display: inline-block; margin-top: 12px;
    background: var(--red); color: #fff; text-decoration: none;
    font-size: 13px; font-weight: 700; padding: 8px 18px; border-radius: 8px;
  }}
  .ticket-btn:active {{ background: var(--red-dark); }}

  /* 終了した試合は少し控えめに */
  section:last-of-type .card {{ opacity: 0.82; }}

  /* チケットカード */
  .ticket-card {{
    background: #fff; border-radius: 14px; padding: 14px 16px;
    margin-bottom: 10px; border: 1px solid var(--line);
    border-left: 4px solid var(--red);
  }}
  .sale-head {{ display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }}
  .sale-type {{
    font-size: 10px; font-weight: 800; color: #fff; background: var(--navy);
    padding: 2px 7px; border-radius: 4px;
  }}
  .sale-start {{ font-size: 14px; font-weight: 700; color: var(--red); }}
  .sale-match {{ font-size: 13px; color: #444; }}

  .empty {{
    background: #fff; border: 1px dashed var(--line); border-radius: 14px;
    padding: 18px 16px; font-size: 13px; color: var(--muted);
  }}

  footer {{
    text-align: center; font-size: 11px; color: var(--muted);
    margin-top: 32px; padding: 0 16px;
  }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="club">KASHIMA ANTLERS</div>
    <h1>鹿島アントラーズ</h1>
    <div class="sub">日程・チケット発売情報</div>
  </header>
  {ticket_section}
  {upcoming_section}
  {finished_section}
  <footer>自動更新 / 最終更新 {generated}<br>データ提供：Ｊリーグ公式</footer>
</div>
</body>
</html>"""


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
