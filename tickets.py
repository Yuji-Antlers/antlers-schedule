#!/usr/bin/env python3
"""
チケット発売情報の取得モジュール — Phase 3

Jリーグ公式のチケット集約ページ（鹿島）から、発売予定・発売中の情報を取得する。
  https://www.jleague.jp/sp/ticket/all/kashima/latest.html

このページの「今後の5試合」セクションには、鹿島がらみの試合（ホーム/アウェイ問わず）
のチケット情報が集約される。個別の sales/perform/<ID> ページは試合後に404で失効するため、
固定URLの集約ページを主ソースとする。

発売情報がまだ無い時期はセクションが空（「チケット情報はありません」）になる。
その場合は空リストを返す。

注意:
  発売情報の細かなHTML表記（発売日ラベル等）は、発売中の試合がある時期に
  実データで最終調整する前提。ここではテキストパターンで頑健に拾う。
"""

import re
from datetime import datetime

try:
    import requests
except ImportError:
    requests = None
from bs4 import BeautifulSoup

TICKET_URL = "https://www.jleague.jp/sp/ticket/all/kashima/latest.html"
USER_AGENT = (
    "KashimaScheduleBot/0.1 (personal schedule checker; "
    "contact: your-email@example.com)"
)

# 「チケット情報はありません」「現在発売予定のチケットはありません」等
EMPTY_MARKERS = ["チケット情報はありません", "発売予定のチケットは現在ありません",
                 "発売予定のチケットはありません"]

DATE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")
DATETIME_RE = re.compile(r"(\d{1,2})月(\d{1,2})日.*?(\d{1,2}):(\d{2})")
# 発売を表す語（先行・一般など）
SALE_KEYWORDS = ["発売", "先行", "一般販売", "販売開始", "受付開始"]


def fetch_ticket_html(url: str = TICKET_URL) -> str:
    if requests is None:
        raise RuntimeError("requests が未インストールです")
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def parse_tickets(html: str) -> list[dict]:
    """
    チケット集約ページを解析し、発売情報のリストを返す。
    発売情報が無ければ空リスト。

    返す各要素:
      {
        "match_label": 試合の表記（対戦相手・日付など、ページ記載のまま）,
        "match_date":  試合日 (MM-DD 推定, 取れれば),
        "sale_type":   発売種別（先行/一般など、取れれば）,
        "sale_start":  発売開始日時の文字列（取れれば）,
        "ticket_url":  購入リンク,
        "raw":         元テキスト（デバッグ・差分用）,
      }
    """
    soup = BeautifulSoup(html, "html.parser")

    # 「今後の5試合」セクションを起点に探す。見つからなければ全体から拾う。
    body_text = soup.get_text(" ", strip=True)
    for marker in EMPTY_MARKERS:
        if marker in body_text:
            # ただし発売リンクが別途あれば空ではない可能性もあるので、リンク有無も見る
            if not soup.find("a", href=re.compile(r"jleague-ticket\.jp/sales/perform")):
                return []

    tickets = []
    seen = set()

    # チケット購入リンク（sales/perform）を持つ要素を起点にする
    for a in soup.find_all("a", href=re.compile(r"jleague-ticket\.jp/sales/perform")):
        # リンクを含む試合ブロックを遡って取得
        block = a
        for _ in range(4):
            if block.parent is not None:
                block = block.parent
            text = block.get_text(" ", strip=True)
            if any(k in text for k in SALE_KEYWORDS) or DATE_RE.search(text):
                break

        text = block.get_text(" ", strip=True)
        ticket_url = a["href"]
        if ticket_url in seen:
            continue
        seen.add(ticket_url)

        rec = _build_ticket_record(text, ticket_url)
        tickets.append(rec)

    return tickets


def _build_ticket_record(text: str, ticket_url: str) -> dict:
    # 発売種別
    sale_type = ""
    for k in ["先行", "一般販売", "一般", "受付"]:
        if k in text:
            sale_type = k
            break

    # 発売開始日時: 「発売」「販売開始」等の語の前後の日付を優先して拾う。
    # これにより試合日（先頭の日付）と発売日を取り違えにくくする。
    sale_start = _extract_sale_datetime(text)

    # 試合日（テキスト中の最初の日付を試合日と推定）
    match_date = ""
    d2 = DATE_RE.search(text)
    if d2:
        match_date = f"{int(d2.group(1)):02d}-{int(d2.group(2)):02d}"

    return {
        "match_label": text[:80],
        "match_date": match_date,
        "sale_type": sale_type,
        "sale_start": sale_start,
        "ticket_url": ticket_url,
        "raw": text[:200],
    }


def _extract_sale_datetime(text: str) -> str:
    """
    「発売」「販売開始」等の語の近くにある日付（＋時刻）を発売開始として抽出する。
    語が見つからなければテキスト中の2つ目の日付（1つ目は試合日と仮定）を使う。
    """
    # 「○月○日 ... HH:MM ... 発売」のように、発売語の手前にある日時を探す
    for kw in ["発売", "販売開始", "受付開始"]:
        idx = text.find(kw)
        if idx == -1:
            continue
        window = text[max(0, idx - 40):idx + 2]
        dt = DATETIME_RE.search(window)
        if dt:
            mm, dd, hh, mi = dt.groups()
            return f"{int(mm):02d}-{int(dd):02d} {int(hh):02d}:{mi}"
        d = DATE_RE.search(window)
        if d:
            return f"{int(d.group(1)):02d}-{int(d.group(2)):02d}"

    # フォールバック: 2つ目の日付を発売日とみなす
    dates = DATE_RE.findall(text)
    if len(dates) >= 2:
        mm, dd = dates[1]
        return f"{int(mm):02d}-{int(dd):02d}"
    return ""


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        html = open(sys.argv[1], encoding="utf-8").read()
        print(f"ローカルHTMLから解析: {sys.argv[1]}")
    else:
        html = fetch_ticket_html()
    tickets = parse_tickets(html)
    print(f"取得した発売情報: {len(tickets)}件")
    for t in tickets:
        print(f"  {t['sale_type']} 発売:{t['sale_start']} {t['match_label']}")
