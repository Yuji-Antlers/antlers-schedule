# 鹿島アントラーズ 日程通知アプリ — GitHub Actions 自動運用

サーバ不要・無料で、Jリーグ公式から鹿島の日程を定期取得し、
新規・変更があればメール通知、最新一覧を GitHub Pages で公開します。

---

## 仕組み

```
GitHub Actions (cron, 1日2回)
   └ kashima_schedule.py を実行
       ├ Jリーグ公式から日程取得・解析
       ├ チケット集約ページから発売情報を取得（ホーム/アウェイ共通）
       ├ SQLite(kashima.db) と比較して差分検出
       ├ 差分があれば notify.py でメール送信（二重送信防止つき）
       │   ・新しい試合 / 日時・会場の変更
       │   ・チケット発売日の確定・変更
       └ index.html を生成（日程一覧＋チケット発売情報）
   └ kashima.db / index.html をリポジトリにコミット
   └ index.html を GitHub Pages に公開
```

すべて GitHub の無料枠で完結します（パブリックリポジトリなら Actions 無制限）。

### チケット情報の取得元

鹿島がらみのチケット（ホーム・アウェイ問わず）は、Jリーグ公式の集約ページ
`https://www.jleague.jp/sp/ticket/all/kashima/latest.html` の「今後の5試合」に
まとまって掲載されます。個別の発売ページ（`sales/perform/<ID>`）は試合後に
失効するため、この固定URLの集約ページを主ソースにしています。
20クラブを個別に追う必要がありません。

---

## セットアップ手順

### 1. リポジトリを作成してファイルを置く

GitHubで新規リポジトリを作成し、以下の構成で push します。

```
your-repo/
├── kashima_schedule.py    # メイン（日程・チケット取得、保存、通知、HTML生成）
├── notify.py              # メール送信（日程・チケット）
├── tickets.py             # チケット発売情報の取得・解析
├── .gitignore
└── .github/
    └── workflows/
        └── check.yml
```

```bash
git init
git add .
git commit -m "初期構成"
git branch -M main
git remote add origin https://github.com/<あなた>/<リポジトリ>.git
git push -u origin main
```

### 2. メール送信の準備（どちらか選ぶ）

#### 方式A: Gmail（自分宛てだけなら最も手軽）

1. Googleアカウントで2段階認証を有効化
2. [アプリパスワード](https://myaccount.google.com/apppasswords)を生成（16桁）
3. このアプリパスワードを後述の `SMTP_PASS` に使う

#### 方式B: Resend（複数人に送る・送信専用ドメインを使いたい場合）

1. [resend.com](https://resend.com) で登録
2. APIキーを発行
3. 送信元は検証なしなら `onboarding@resend.dev` が使える（自分のドメイン検証も可）

### 3. Secrets を登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret**
で以下を登録します。**使う方式の分だけでOK。**

共通:

| 名前 | 値の例 | 説明 |
|---|---|---|
| `MAIL_METHOD` | `smtp` または `resend` | 送信方式 |
| `MAIL_TO` | `you@example.com` | 通知の宛先（カンマ区切りで複数可） |
| `MAIL_FROM` | `you@gmail.com` | 送信元 |

方式A（Gmail/SMTP）を使う場合に追加:

| 名前 | 値の例 |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `you@gmail.com` |
| `SMTP_PASS` | 生成した16桁のアプリパスワード |

方式B（Resend）を使う場合に追加:

| 名前 | 値の例 |
|---|---|
| `RESEND_API_KEY` | `re_xxxxxxxx` |

### 4. GitHub Pages を有効化

**Settings → Pages → Build and deployment** で
**Source を「GitHub Actions」** に設定します。
（ワークフローが Pages へデプロイするため、ブランチ指定は不要）

### 5. ワークフローの権限を確認

**Settings → Actions → General → Workflow permissions** で
**「Read and write permissions」** を選択して保存。
（DBとHTMLをコミットするのに必要）

### 6. 動かしてみる

**Actions タブ → 「鹿島日程チェック」→ Run workflow** で手動実行できます。
初回は全試合が「新規」となり通知メールが届きます。
以降は cron（JST 8:00 / 20:00）で自動実行され、差分があるときだけ通知が来ます。

公開URLは `https://<あなた>.github.io/<リポジトリ>/` です。

---

## 実行タイミングの調整

`.github/workflows/check.yml` の cron を編集します。UTC基準なので注意。

```yaml
- cron: '0 23 * * *'   # JST 8:00
- cron: '0 11 * * *'   # JST 20:00
```

頻度を上げたい時期（チケット発売が近いなど）は行を足します。
例: 3時間ごと → `- cron: '0 */3 * * *'`

---

## 重要な注意

- **cronの起動はGitHub側の混雑で数分〜十数分ずれることがあります。**
  「発売日を見逃さない」用途には十分ですが、秒単位の即時性はありません。
- **60日間リポジトリにpushが無いとスケジュールが自動停止します。**
  定期実行でDBがコミットされるので通常は問題ありません。
- スクレイピング前に
  [Jリーグの利用規約](https://www.jleague.jp/sp/general/kiyaku.html)を確認し、
  `kashima_schedule.py` の `USER_AGENT` を自分の連絡先に書き換えてください。
- 取得元のHTML構造が変わると `解析できた試合数: 0` になります。
  その場合は解析部分の調整が必要です（Phase 4で自動アラート化予定）。

---

## ローカルでのテスト

```bash
pip install requests beautifulsoup4

# ネット接続して実取得（通知は環境変数が無ければスキップ）
python3 kashima_schedule.py --no-notify

# ローカルHTMLから解析（ネット不要）
python3 kashima_schedule.py --source sample.html --no-notify

# チケット取得を止めて日程だけ確認
python3 kashima_schedule.py --no-notify --no-tickets

# チケット解析だけ単体テスト
python3 tickets.py            # 実ページ取得
python3 tickets.py some.html  # ローカルHTMLから
```

### チケット解析の最終調整について

チケット集約ページは、発売予定がない時期は中身が空です（解析は0件を返します）。
発売中の試合がある時期に実ページを `python3 tickets.py` で取得し、発売日・種別が
正しく抽出できているか確認してください。表記が想定と違う場合は `tickets.py` の
`_extract_sale_datetime()` と `SALE_KEYWORDS` を調整します。
解析ロジックはクラス名に依存せず、日付と「発売」等のキーワードで拾う方式なので、
多少の構造変更には耐えます。

---

## 今後の拡張（Phase 4以降）

- 取得失敗時（解析0件など）に管理者へアラートメール
- 一覧HTMLのデザイン改善・カレンダー表示
- 発売直前は実行頻度を上げる（cron追加）
- アウェイで集約ページに載らないケースの補完（対戦相手公式へのフォールバック）
