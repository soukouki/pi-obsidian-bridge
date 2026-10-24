# hermes-obsidian-bridge

Obsidianのノート内の `@PI` タグを検出し、Pi ([pi-web](https://github.com/jmfederico/pi-web)) にリクエストを送り、結果を callout としてノートに書き戻すデーモンです。

## pi-webについて

ここで使う `pi-web` は [jmfederico/pi-web](https://github.com/jmfederico/pi-web)
(Pi Coding AgentのWeb UI)です。同名かつ似た役割のより広く知られたプロジェクト(agegr/pi-web)や、別のPI AgentをWebから扱うためのプロジェクトとは別物です。

pi-web の HTTP API を使います。認証は不要です。

`cwd` は単なる文字列パラメータです。プロジェクトの登録やID管理は不要ですが、**Piから見て存在する絶対パス**である必要があります。保管庫のパスではありません。Piは保管庫にアクセスできないため、文脈はプロンプト内の抜粋のみで渡します。

## `compose.yml`の設定例

pi-web の**公式Dockerイメージは公開されていません**。

pi-web の既定のバインドは `127.0.0.1` なので、別コンテナから到達するには `0.0.0.0` で
リッスンさせる(またはポートを公開する)必要があります。

```compose.yml
services:
  # pi-web側は各自の導入方法(npmまたは docker/install.sh)で起動したものとします
  hermes-obsidian:
    build:
      context: .
      dockerfile: Dockerfile
    restart: unless-stopped
    volumes:
      - ${OBSIDIAN_VAULT}:/vault
    environment:
      - VAULT_DIR=/vault
      - PI_BASE=http://pi:8504   # pi-webに到達できるURL(同じネットワークならサービス名)
      - PI_CWD=/workspace        # Pi側で存在する絶対パス
```

## 最終結果の書き戻し形式(callout / 地の文)

既定では最終結果は callout(`> [!note]`)として書き戻されます。処理中の経過表示
(`> [!example] Pi 実行中...`)は常に callout で、最終結果が届いた時点でその部分だけを置き換えます。

`config.yaml` の `plain_output: true` を設定すると、その置き換えを callout なしの地の文(プレーンなMarkdown)で行います。処理中のcalloutはそのまま、最終結果の時点で引用が外れます。

```yaml
default: &default
  plain_output: false   # 全体の既定値

agents:
  "@Plain":
    <<: [*default, *m_main, *d_quick, *f_answer_plain]
    plain_output: true  # このエージェントだけ地の文で書き戻す
```

- 前後の空行(段落区切り)は保たれます。
- エラー時の書き戻しは視認性のため callout(`> [!error]`)のままです。地の文にしたい場合は `insert_result()` 内の `if plain and not error:` を `if plain:` に変更してください。
- LLMが自分でcalloutを返した場合、`strip_wrapping_callout()` が引用を剥がすので、どちらの設定でも二重引用にはなりません。

## 依存

```sh
pip install requests inotify_simple websocket-client pyyaml
```
