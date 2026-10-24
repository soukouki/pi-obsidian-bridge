# pi-obsidian-bridge

Obsidianのノート内の `@PI` タグを検出し、Pi ([pi-web](https://github.com/jmfederico/pi-web)) にリクエストを送り、結果を callout としてノートに書き戻すデーモンです。

## pi-webについて

ここで使う `pi-web` は [jmfederico/pi-web](https://github.com/jmfederico/pi-web)
(Pi Coding AgentのWeb UI)です。同名かつ似た役割のより広く知られたプロジェクト(agegr/pi-web)や、別のPI AgentをWebから扱うためのプロジェクトとは別物です。

pi-web の HTTP API を使います。認証は不要です。

Pi側のプロジェクトはフォルダ(パス)で識別されます。設定にはプロジェクト名ではなく絶対パスを書きます。プロジェクトの登録やID管理は不要ですが、**Piから見て存在する絶対パス**である必要があります。保管庫のパスではありません。Piは保管庫にアクセスできないため、文脈はプロンプト内の抜粋のみで渡します。

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
      - PI_PROJECT=/workspace    # Pi側で存在する絶対パス
```

## 依存

```sh
pip install requests inotify_simple websocket-client pyyaml
```
