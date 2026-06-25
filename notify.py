#!/usr/bin/env python3
"""
メール通知モジュール — Phase 2

kashima_schedule.py の差分検出結果（new / changed）を受け取り、
新しい試合や変更をメールで通知する。

送信方式は2つを環境変数で切り替え:
  - SMTP（Gmail等）   : MAIL_METHOD=smtp
  - Resend API        : MAIL_METHOD=resend

GitHub Actions では Secrets を環境変数として渡す想定。
二重送信を防ぐため、送ったかどうかは呼び出し側(DB)で管理する。
"""

import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formataddr

try:
    import requests
except ImportError:
    requests = None


def build_ticket_message(new_sales: list[dict], changed_sales: list[dict]) -> tuple[str, str]:
    """チケット発売の通知メール。差分が無ければ ('', '')。"""
    if not new_sales and not changed_sales:
        return "", ""

    lines = []
    if new_sales:
        lines.append("■ チケット発売情報（新規）")
        for t in new_sales:
            stype = f"[{t['sale_type']}] " if t.get("sale_type") else ""
            start = f" 発売: {t['sale_start']}" if t.get("sale_start") else ""
            lines.append(f"  {stype}{t['match_label']}{start}")
            if t.get("ticket_url"):
                lines.append(f"    {t['ticket_url']}")
        lines.append("")

    if changed_sales:
        lines.append("■ チケット発売情報（変更）")
        for t in changed_sales:
            stype = f"[{t['sale_type']}] " if t.get("sale_type") else ""
            start = f" 発売: {t['sale_start']}" if t.get("sale_start") else ""
            lines.append(f"  {stype}{t['match_label']}{start}")
            if t.get("ticket_url"):
                lines.append(f"    {t['ticket_url']}")
        lines.append("")

    total = len(new_sales) + len(changed_sales)
    subject = f"[鹿島チケット] 発売情報 {total}件（新規{len(new_sales)} / 変更{len(changed_sales)}）"
    return subject, "\n".join(lines).strip()


def build_message(new_matches: list[dict], changed_matches: list[dict]) -> tuple[str, str]:
    """通知メールの件名と本文を組み立てる。差分が無ければ ('', '') を返す。"""
    if not new_matches and not changed_matches:
        return "", ""

    lines = []
    if new_matches:
        lines.append("■ 新しい試合")
        for m in new_matches:
            hoa = {"HOME": "ホーム", "AWAY": "アウェイ"}.get(m["home_or_away"], m["home_or_away"])
            ticket = f"\n    チケット: {m['ticket_url']}" if m.get("ticket_url") else ""
            lines.append(
                f"  {m['date']} {m['kickoff_time']} [{hoa}] vs {m['opponent']}"
                f" @ {m['venue']}{ticket}"
            )
        lines.append("")

    if changed_matches:
        lines.append("■ 変更があった試合")
        for m in changed_matches:
            hoa = {"HOME": "ホーム", "AWAY": "アウェイ"}.get(m["home_or_away"], m["home_or_away"])
            ticket = f"\n    チケット: {m['ticket_url']}" if m.get("ticket_url") else ""
            lines.append(
                f"  {m['date']} {m['kickoff_time']} [{hoa}] vs {m['opponent']}"
                f" @ {m['venue']}{ticket}"
            )
        lines.append("")

    total = len(new_matches) + len(changed_matches)
    subject = f"[鹿島日程] 更新 {total}件（新規{len(new_matches)} / 変更{len(changed_matches)}）"
    body = "\n".join(lines).strip()
    return subject, body


def send_email(subject: str, body: str) -> bool:
    """環境変数の設定に従ってメールを送る。成功で True。"""
    if not subject:
        return False

    method = os.environ.get("MAIL_METHOD", "smtp").lower()
    to_addr = os.environ.get("MAIL_TO", "")
    if not to_addr:
        print("MAIL_TO が未設定のため送信スキップ")
        return False

    if method == "resend":
        return _send_resend(subject, body, to_addr)
    return _send_smtp(subject, body, to_addr)


def _send_smtp(subject: str, body: str, to_addr: str) -> bool:
    """SMTP送信（Gmailなど）。"""
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    from_addr = os.environ.get("MAIL_FROM", user)

    if not user or not password:
        print("SMTP_USER / SMTP_PASS が未設定のため送信スキップ")
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("鹿島日程通知", from_addr))
    msg["To"] = to_addr

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.sendmail(from_addr, [a.strip() for a in to_addr.split(",")], msg.as_string())
    print(f"SMTP送信完了: {to_addr}")
    return True


def _send_resend(subject: str, body: str, to_addr: str) -> bool:
    """Resend API送信。"""
    if requests is None:
        print("requests 未インストールのため Resend 送信不可")
        return False
    api_key = os.environ.get("RESEND_API_KEY", "")
    from_addr = os.environ.get("MAIL_FROM", "onboarding@resend.dev")
    if not api_key:
        print("RESEND_API_KEY が未設定のため送信スキップ")
        return False

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "from": from_addr,
            "to": [a.strip() for a in to_addr.split(",")],
            "subject": subject,
            "text": body,
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"Resend送信失敗: {resp.status_code} {resp.text[:200]}")
        return False
    print(f"Resend送信完了: {to_addr}")
    return True
