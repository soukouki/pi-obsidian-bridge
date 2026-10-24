#!/usr/bin/env python3
"""
Pi Obsidian Bridge

Obsidianノート内の `@PI` タグを検出し、Pi (pi-web) にリクエストを送り、
結果をcalloutとしてノートに書き戻すデーモン。検出は inotify (Linux) 使用。

依存: requests, inotify_simple, websocket-client, pyyaml
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests
import websocket
import yaml
from inotify_simple import INotify, flags


# ============================================================
# 設定(環境に合わせて変更してください)
# ============================================================

VAULT_DIR = Path(os.getenv("VAULT_DIR", "/vault"))
PI_BASE = os.getenv("PI_BASE", "http://localhost:8504").rstrip("/")
# Piから見て存在する絶対パスである必要がある(保管庫のパスではない)
# config.yaml のエージェント側 `cwd` で上書きできる(未設定のものはこれが使われる)
PI_CWD = os.getenv("PI_CWD", "/workspace")

EMOJI_SEEN = "👀"     # 処理開始
EMOJI_DONE = "✅️"     # 処理完了
EMOJI_ERROR = "⚠️"    # エラー(手動でトリガータグに書き戻せば再試行される)

# 監視・スキャンから除外するディレクトリ名
EXCLUDE_DIRS = {".git", ".obsidian", ".trash", "node_modules"}

# Piへの同時リクエスト数上限(=同時セッション数)
MAX_WORKERS = 4

# 1リクエストの完了待ちポーリング(ファイル検出はinotifyでイベント駆動)
POLL_INTERVAL = 2
POLL_TIMEOUT = 600

# 実行中calloutの書き戻し間隔(秒)
PROGRESS_UPDATE_INTERVAL = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pi-watcher")

# ============================================================
# エージェント設定
# ============================================================

@dataclass(frozen=True)
class AgentConfig:
    """config.yaml から読み込む1エージェントの設定。"""
    tag: str
    aliases: tuple[str, ...]
    model: str | None  # "provider/model-id" または model-id 単独
    provider: str # default: "auto"
    thinking: str | None
    front: str
    back: str
    plain_output: bool = False  # 最終結果をcalloutでなく地の文として書き戻す
    cwd: str | None = None      # Pi側のプロジェクト(作業ディレクトリ)の絶対パス。NoneならPI_CWD

# デフォルト設定(config.yaml が存在しない場合のフォールバック)
DEFAULT_FRONT = (
    "あなたはObsidianのノート「{note_title}」内で呼び出されたアシスタントです。"
    "以下はそのノートの該当箇所の抜粋です。"
)
DEFAULT_BACK = (
    "上記の文脈を踏まえて、必要であれば検索やプログラム実行などを行った上で、"
    "結果を簡潔に(目安3〜6行)日本語でまとめてください。"
    "必要に応じて、ObsidianのMCPツールを用いてこのノートや関連する他のノートを参照(閲覧)しても構いませんが、"
    "このノート自体を編集・書き換えるツールは使わないでください。回答はノートを直接編集するのではなく、"
    "単純にテキストとして返してください。ノートへの反映はこの後スクリプト側で自動的に行います。"
    "Markdown形式で、前置きや後置きなしに本文のみを返してください。"
    "絶対に「結果はこのとおりです」「以下Markdown形式で回答します」「```markdown」などの前置き文は書かないでください。"
    "コールアウトの引用は自動で付与されるので不要です。"
)

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", str(Path(__file__).parent / "config.yaml")))

# 既定エージェントを示すエイリアス。default_agent が未指定のとき、これを持つエージェントが既定になる
DEFAULT_ALIAS = "@PI"

def load_config() -> tuple[list[AgentConfig], AgentConfig]:
    """
    config.yaml からエージェント設定を読み込む。

    戻り値: (エージェントリスト, 既定エージェント)
    優先度順: 明示的 @ タグ > エイリアス
    既定エージェントは default_agent で指定した名前(agents のキーまたは aliases の値)。
    未指定なら DEFAULT_ALIAS を持つエージェントが使われる。
    """
    if not CONFIG_PATH.exists():
        log.warning("config.yaml が見つかりません: %s (デフォルト設定を使用)", CONFIG_PATH)
        agents = _default_agents()
        return agents, agents[0]

    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except Exception:
        log.exception("config.yaml の解析に失敗しました: %s (デフォルト設定を使用)", CONFIG_PATH)
        agents = _default_agents()
        return agents, agents[0]

    agents_data = raw.get("agents") or {}
    default_section = raw.get("default") or {}
    default_front = default_section.get("front") or DEFAULT_FRONT
    default_back = default_section.get("back") or DEFAULT_BACK
    default_plain = bool(default_section.get("plain_output", False))
    default_cwd = default_section.get("cwd") or PI_CWD
    default_agent_name = default_section.get("default_agent")

    agents: list[AgentConfig] = []
    default_alias_holder: str | None = None

    def _interp(s: str) -> str:
        return s

    for tag, cfg in agents_data.items():
        if not isinstance(cfg, dict):
            continue
        front = cfg.get("front") or _interp(default_front)
        back = cfg.get("back") or _interp(default_back)
        aliases = tuple(cfg.get("aliases") or [])
        model = cfg.get("model")
        provider = cfg.get("provider") or "auto"
        thinking = cfg.get("thinking")
        cwd = cfg.get("cwd") or default_cwd
        # Pi側で存在する絶対パスでないとセッション作成がHTTP 400になる。このコンテナ
        # からは実在確認できないため、絶対パスかどうかだけ見て警告する。
        if cwd and not str(cwd).startswith("/"):
            log.warning("cwdは絶対パスである必要があります: tag=%s cwd=%s", tag, cwd)
        agents.append(AgentConfig(
            tag=tag,
            aliases=aliases,
            model=model,
            provider=provider,
            thinking=thinking,
            front=front,
            back=back,
            plain_output=bool(cfg.get("plain_output", default_plain)),
            cwd=str(cwd) if cwd else None,
        ))
        if (tag == DEFAULT_ALIAS or DEFAULT_ALIAS in aliases) and default_alias_holder is None:
            default_alias_holder = tag

    if not agents:
        agents = _default_agents()
        return agents, agents[0]

    # 既定エージェント: default_agent で指定した名前 > DEFAULT_ALIAS を持つエージェント > 最初のエージェント
    by_name: dict[str, AgentConfig] = {}
    for a in agents:
        by_name[a.tag] = a
        for alias in a.aliases:
            by_name[alias] = a

    default_agent: AgentConfig | None = None
    if default_agent_name:
        default_agent = by_name.get(str(default_agent_name))
        if default_agent is None:
            log.warning(
                "default_agent が見つかりません: %s (既定エイリアス %s を使用)",
                default_agent_name, DEFAULT_ALIAS,
            )
    if default_agent is None and default_alias_holder is not None:
        default_agent = by_name[default_alias_holder]
    if default_agent is None:
        default_agent = agents[0]

    return agents, default_agent

def _default_agents() -> list[AgentConfig]:
    return [AgentConfig(
        tag="@PI",
        aliases=(),
        model=None,
        provider="auto",
        thinking=None,
        front=DEFAULT_FRONT,
        back=DEFAULT_BACK,
        plain_output=False,
        cwd=None,
    )]

# 設定を1回だけロード (モジュールレベル)
_AGENTS, DEFAULT_AGENT = load_config()

# すべてのトリガータグ + エイリアス (検出/サニタイズ対象)
_ALL_TRIGGERS = set()
_TAG_TO_AGENT: dict[str, AgentConfig] = {}
for _a in _AGENTS:
    _ALL_TRIGGERS.add(_a.tag)
    _TAG_TO_AGENT[_a.tag] = _a
    for _alias in _a.aliases:
        _ALL_TRIGGERS.add(_alias)
        _TAG_TO_AGENT[_alias] = _a

# 生の @<Tag> を検出(マーカー付きのものは除外)
_MARKER_PREFIX = f"<!-- pi"
_TRIBUTE_PATTERN = r"(?<!@)(" + "|".join(re.escape(t) for t in _ALL_TRIGGERS) + r")"
# タグを書き忘れた `@` は段落の先頭に書かれたものだけ検出する(本文中の孤立した@に反応しないため)
_BARE_TRIGGER = r"^(?:@(?=\s|$))"
TRIGGER_RE = re.compile(
    r"(?<!@)(" + "|".join(sorted((re.escape(t) for t in _ALL_TRIGGERS), key=len, reverse=True)) + r"|" + _BARE_TRIGGER + r")"
    + r"(?![👀✅⚠️])",
    re.MULTILINE,
)


def seen_marker_re(tag: str, request_id: str) -> re.Pattern:
    """処理中マーカー(IDつき)を探すための正規表現。"""
    return re.compile(
        re.escape(f"{tag}{EMOJI_SEEN}<!-- pi-id:{request_id} -->")
    )


# 実行中calloutを識別するためのIDコメント(プレビューでは非表示)
def progress_id_comment(request_id: str) -> str:
    return f"<!-- pi-progress-id:{request_id} -->"


def progress_callout_re(request_id: str) -> re.Pattern:
    """実行中callout全体(タイトル行〜本文の連続する`>`行)を探す正規表現。"""
    comment = re.escape(progress_id_comment(request_id))
    return re.compile(
        r"^> \[![a-zA-Z-]+\][+-]?[^\n]*\n" # callout タイトル行
        r"> " + comment + r"\n"            # ID コメント行
        r"(?:^>.*\n)*",                    # 続く本文行(すべて`>`始まり、改行込み)
        re.MULTILINE,
    )


# ============================================================
# ファイルロック(同一ファイルへの同時書き込みを防止)
# ============================================================

_file_locks: dict[str, threading.Lock] = {}
_file_locks_guard = threading.Lock()


def get_file_lock(path: str) -> threading.Lock:
    with _file_locks_guard:
        if path not in _file_locks:
            _file_locks[path] = threading.Lock()
        return _file_locks[path]


# ============================================================
# 段落抽出(空行区切り)
# ============================================================

HEADING_RE = re.compile(r"^#{1,6}\s")


def split_paragraphs(text: str) -> list[tuple[int, int, str]]:
    """空行区切りで段落を (start, end, content) として列挙する。
    見出し行は前後に空行がなくても単独で1段落として切り出す。"""
    paragraphs: list[tuple[int, int, str]] = []
    for m in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]*)*", text):
        block_start = m.start()
        lines = m.group().split("\n")

        pos = block_start
        cur_lines: list[str] = []
        cur_start = pos

        def flush() -> None:
            nonlocal cur_lines
            if cur_lines:
                content = "\n".join(cur_lines)
                paragraphs.append((cur_start, cur_start + len(content), content))
                cur_lines = []

        for line in lines:
            if HEADING_RE.match(line):
                flush()
                paragraphs.append((pos, pos + len(line), line))
                cur_start = pos + len(line) + 1
            else:
                if not cur_lines:
                    cur_start = pos
                cur_lines.append(line)
            pos += len(line) + 1
        flush()
    return paragraphs


def extract_context(
    text: str, match_start: int, match_end: int
) -> tuple[list[str] | None, str, list[str] | None]:
    """(前段落 or None, 対象段落, 次段落 or None)。
    前:最小3段落/3000字、後:最小1段落/1000字。"""
    paragraphs = split_paragraphs(text)
    target_idx = None
    for i, (s, e, _) in enumerate(paragraphs):
        if s <= match_start < e:
            target_idx = i
            break

    if target_idx is None:
        # 段落として拾えなかった場合は行単位にフォールバック
        line_start = text.rfind("\n", 0, match_start) + 1
        line_end = text.find("\n", match_end)
        if line_end == -1:
            line_end = len(text)
        return None, text[line_start:line_end], None
    prev_p = []
    next_p = []
    # 前方段落は最小3段落、最大3000文字まで
    for i in range(target_idx - 1, -1, -1):
        prev_p.insert(0, paragraphs[i][2])
        if len(prev_p) >= 3 and sum(len(p) for p in prev_p) >= 3000:
            break
    # もしも前方段落でファイル先頭までたどり着いていれば、`<begin of file>`を追加しておく
    if target_idx - len(prev_p) == 0:
        prev_p.insert(0, "<begin of file>")
    # 後方段落は最小1段落、最大1000文字まで
    for i in range(target_idx + 1, len(paragraphs)):
        next_p.append(paragraphs[i][2])
        if len(next_p) >= 1 and sum(len(p) for p in next_p) >= 1000:
            break
    # もしも後方段落でファイル末尾までたどり着いていれば、`<end of file>`を追加しておく
    if target_idx + len(next_p) == len(paragraphs) - 1:
        next_p.append("<end of file>")
    return prev_p, paragraphs[target_idx][2], next_p


# ============================================================
# プロンプト構築
# config.yaml の front/back でカスタマイズできる
# ============================================================

def extract_user_request(target_paragraph: str) -> str:
    """対象段落から @<Tag> 以降のユーザー要望テキストを取り出す。"""
    for tag in _ALL_TRIGGERS:
        idx = target_paragraph.find(tag)
        if idx != -1:
            return target_paragraph[idx + len(tag):].strip()
    return target_paragraph.strip()


def build_prompt(
    agent: AgentConfig,
    note_title: str,
    prev_paragraphs: list[str] | None,
    target_paragraph: str,
    next_paragraphs: list[str] | None,
) -> str:
    """config.yaml の front/back を補間して最終指示文を組み立てる。"""
    parts = [agent.front.replace("{note_title}", note_title), ""]
    if prev_paragraphs:
        parts += ["--- 前方の段落 ---", *prev_paragraphs, ""]
    parts += [f"--- {agent.tag} が書かれた段落 ---", target_paragraph, ""]
    if next_paragraphs:
        parts += ["--- 後方の段落 ---", *next_paragraphs, ""]
    parts.append(agent.back)
    return "\n".join(parts)


# ============================================================
# Markdownエスケープ
# ============================================================

# callout本文に差し込む文字列のMarkdown特殊文字をエスケープする
_MD_ESCAPE_RE = re.compile(r"([\\`*_{}\[\]()#+\-.!>~|])")


def escape_markdown(text: str) -> str:
    if not text:
        return text
    return _MD_ESCAPE_RE.sub(r"\\\1", text)


# ============================================================
# トリガー文字列のサニタイズ
# ============================================================
# LLM出力に生の @<Tag> が含まれていると `TRIGGER_RE` に再ヒットして
# 無限ループになるため、書き込み時に完了マークを付けて中立化する。
# 以降のスキャンでは無視される。
_SANITIZE_TRIGGER_RE = re.compile(
    _TRIBUTE_PATTERN + r"(?![👀✅⚠️])"
)

def sanitize_trigger(text: str) -> str:
    """LLM出力に含まれる生の @<Tag> を @<Tag>✅️ に置換して中立化する。"""
    if not text:
        return text
    return _SANITIZE_TRIGGER_RE.sub(lambda m: m.group(0) + EMOJI_DONE, text)


# ============================================================
# 実行中の様子(thinking / tool呼び出し)の蓄積
# ============================================================

class RunActivity:
    """WebSocketフレームから「実行中」calloutの行を組み立てる。
    受信スレッドと更新スレッドからアクセスされるためロックで保護する。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: list[str] = []
        # deltaフレームは断片で届くためstreamIdごとにバッファする
        self._buffers: dict[str, str] = {}
        self._last_label: str | None = None
        self._dirty = False

    def _add(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            self._dirty = True

    def handle_frame(self, frame: dict) -> None:
        """フレーム1件を処理する。ツールは名のみ表示し、最終回答は書かない
        (最終結果側でcalloutを上書きするため重複表示になる)。"""
        ftype = frame.get("type")

        if ftype == "agent.start":
            self._add("🚀 実行を開始しました")

        elif ftype == "activity.update":
            label = ((frame.get("activity") or {}).get("label") or "").strip()
            # 繰り返し届くため変化分だけ表示する
            if label and label != self._last_label:
                self._last_label = label
                self._add(f"📌 {sanitize_trigger(escape_markdown(label))}")

        elif ftype == "assistant.thinking.delta":
            stream_id = frame.get("streamId") or "_"
            text = frame.get("text") or ""
            if not text:
                return
            with self._lock:
                buf = self._buffers.get(stream_id, "") + text
                parts = buf.split("\n")
                for part in parts[:-1]:
                    if part.strip():
                        self._lines.append(f"🧠 {sanitize_trigger(escape_markdown(part))}")
                self._buffers[stream_id] = parts[-1]
                self._dirty = True

        elif ftype == "tool.start":
            tool = frame.get("toolName") or "tool"
            self._add(f"🔧 {sanitize_trigger(escape_markdown(tool))} を実行中...")

        elif ftype == "tool.end":
            tool = frame.get("toolName") or "tool"
            mark = "⚠️" if frame.get("isError") else "✅"
            self._add(f"{mark} {sanitize_trigger(escape_markdown(tool))} 完了")

        elif ftype == "message.end":
            message = frame.get("message") or {}
            if message.get("role") != "assistant":
                return
            # 改行で確定しなかった末尾の断片を1行として確定する
            stream_id = frame.get("streamId") or message.get("streamId") or "_"
            with self._lock:
                rest = self._buffers.pop(stream_id, "").strip()
                if rest:
                    self._lines.append(f"🧠 {sanitize_trigger(escape_markdown(rest))}")
                    self._dirty = True

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def pop_dirty(self) -> bool:
        """前回更新以降に新しい行が追加されたかを返し、フラグを下ろす。"""
        with self._lock:
            was_dirty = self._dirty
            self._dirty = False
            return was_dirty


def stream_pi_progress(session_id: str, activity: RunActivity, stop_event: threading.Event) -> None:
    """WebSocketでフレームをactivityに書き込み続ける。
    接続できない/切れた場合は静かに諦める(結果取得はポーリング側で行う)。"""
    url = f"{PI_BASE}/api/machines/local/sessions/{session_id}/events"
    try:
        ws = websocket.create_connection(url, timeout=10)
    except Exception:
        log.debug("WebSocket接続に失敗しました session_id=%s", session_id, exc_info=True)
        return
    try:
        while not stop_event.is_set():
            try:
                ws.settimeout(5)
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break  # 切断
            if not raw:
                continue
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            activity.handle_frame(frame)
    except Exception:
        log.debug("WebSocket購読を終了しました session_id=%s", session_id, exc_info=True)
    finally:
        try:
            ws.close()
        except Exception:
            pass


def run_progress_updater(
    path: Path, request_id: str, activity: RunActivity, done_event: threading.Event
) -> None:
    """新しい行が溜まっていれば「実行中」calloutに反映する。"""
    while True:
        if done_event.wait(PROGRESS_UPDATE_INTERVAL):
            return
        if activity.pop_dirty():
            try:
                update_progress_callout(path, request_id, activity.snapshot())
            except Exception:
                log.exception("進捗calloutの更新に失敗しました id=%s", request_id)


# ============================================================
# Pi API 呼び出し
# ============================================================

def _api_post(path: str, body: dict) -> dict:
    r = requests.post(f"{PI_BASE}{path}", json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def _api_get(path: str, params: dict) -> dict:
    r = requests.get(f"{PI_BASE}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


# pending / toolUse は途中のターンなので最終結果ではない
_TERMINAL_STOP_REASONS = {"stop", "length", "error", "aborted"}


def extract_final_text(messages: dict) -> str | None:
    """text blockだけを結合して最終assistant回答を取り出す。途中ならNone。"""
    last: dict | None = None
    for msg in messages.get("messages") or []:
        if msg.get("role") == "assistant" and msg.get("stopReason") in _TERMINAL_STOP_REASONS:
            last = msg
    if last is None:
        return None
    if last.get("stopReason") in ("error", "aborted"):
        raise RuntimeError(last.get("errorMessage") or f"run {last.get('stopReason')}")
    parts = [
        c.get("text") for c in (last.get("content") or [])
        if c.get("type") == "text" and c.get("text")
    ]
    return "\n".join(parts).strip() or None


def call_pi_api(prompt: str, agent: AgentConfig, activity: RunActivity) -> str:
    """リクエストごとに新しいセッションを作り、プロンプトを送って最終結果を返す。"""
    # Pi側のプロジェクト(作業ディレクトリ)。エージェントごとのcwd、未設定ならPI_CWD
    cwd = agent.cwd or PI_CWD
    session_id = _api_post("/api/machines/local/sessions", {"cwd": cwd})["id"]
    log.info("セッションを作成しました id=%s cwd=%s", session_id, cwd)

    model = agent.model
    provider = agent.provider
    if model and "/" in model:
        provider, model = model.split("/", 1)
    # 設定値が不正でもリクエスト全体を失敗させないよう、警告して続行する
    if model and provider and provider != "auto":
        try:
            _api_post(
                f"/api/machines/local/sessions/{session_id}/model",
                {"cwd": cwd, "provider": provider, "modelId": model},
            )
        except Exception as e:
            log.warning("モデル固定に失敗しました session_id=%s: %s", session_id, e)
    if agent.thinking:
        try:
            _api_post(
                f"/api/machines/local/sessions/{session_id}/thinking-level",
                {"cwd": cwd, "level": agent.thinking},
            )
        except Exception as e:
            log.warning("thinkingレベル設定に失敗しました session_id=%s: %s", session_id, e)

    _api_post(
        f"/api/machines/local/sessions/{session_id}/prompt",
        {"cwd": cwd, "text": prompt},
    )

    stop_stream = threading.Event()
    stream_thread = threading.Thread(
        target=stream_pi_progress,
        args=(session_id, activity, stop_stream),
        daemon=True,
    )
    stream_thread.start()

    try:
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            answer = extract_final_text(
                _api_get(
                    f"/api/machines/local/sessions/{session_id}/messages",
                    {"cwd": cwd, "limit": 50},
                )
            )
            if answer is not None:
                return answer
            time.sleep(POLL_INTERVAL)

        raise TimeoutError("Piからの応答がタイムアウトしました")
    finally:
        stop_stream.set()
        stream_thread.join(timeout=5)


# calloutタイトル行(`[!note]` 等)を、`>` を剥がした文字列に対して検出する
_CALLOUT_HEADER_RE = re.compile(r"^\[![a-zA-Z][a-zA-Z0-9_-]*\][+-]?.*$")


def strip_wrapping_callout(text: str) -> str:
    """出力がcallout形式で返ってきた場合、引用を1段階剥がしてプレーンな
    テキストに戻す。callout内calloutは表示が崩れるため。"""
    if not text:
        return text
    lines = text.split("\n")

    start = 0
    while start < len(lines) and lines[start].strip() == "":
        start += 1
    if start >= len(lines):
        return text

    first = lines[start].strip()
    if not first.startswith(">"):
        return text
    header_body = first[1:].strip()
    if not _CALLOUT_HEADER_RE.match(header_body):
        return text

    rest = lines[start + 1:]
    non_empty = [l for l in rest if l.strip() != ""]
    quoted = [l for l in non_empty if l.lstrip().startswith(">")]
    if non_empty and len(quoted) < len(non_empty) * 0.8:
        return text

    dequoted: list[str] = []
    for line in rest:
        s = line.lstrip()
        if s.startswith("> "):
            dequoted.append(s[2:])
        elif s.startswith(">"):
            dequoted.append(s[1:])
        else:
            dequoted.append(line)

    result = "\n".join(dequoted).strip("\n")
    return result if result else text


# ============================================================
# ファイル書き換え
# ============================================================

def build_progress_callout(request_id: str, lines: list[str]) -> str:
    """「実行中」calloutのMarkdown断片(タイトル行を含む)を組み立てる。"""
    header = "> [!example] Pi 実行中...\n"
    id_line = f"> {progress_id_comment(request_id)}\n"
    body = "\n".join(f"> {line}" for line in (lines or ["⏳ 準備中..."]))
    return header + id_line + body + "\n"


def update_progress_callout(path: Path, request_id: str, lines: list[str]) -> None:
    """既存の「実行中」calloutの本文を、現時点までの経過で置き換える(追記)。"""
    lock = get_file_lock(str(path))
    with lock:
        text = path.read_text(encoding="utf-8")
        m = progress_callout_re(request_id).search(text)
        if not m:
            log.warning("進捗calloutが見つかりません: id=%s path=%s", request_id, path)
            return
        new_block = build_progress_callout(request_id, lines)
        new_text = text[: m.start()] + new_block + text[m.end():]
        path.write_text(new_text, encoding="utf-8")


def mark_seen(path: Path) -> tuple[str, str, str, str] | None:
    """
    ファイル内の最初の未処理 @<Tag> を見つけ、👀マーカーに即座に置き換えて
    書き込む。同時に、その段落の直後に「実行中」calloutを挿入する。
    段落の先頭に `@` だけがある場合は既定エージェントが使われ、マーカーには
    そのエージェントのタグが書かれる。
    戻り値: (request_id, prompt, agent, tag) または 未検出ならNone。
    """
    lock = get_file_lock(str(path))
    with lock:
        text = path.read_text(encoding="utf-8")
        m = TRIGGER_RE.search(text)
        if not m:
            return None

        matched_tag = m.group(1)
        agent = _TAG_TO_AGENT.get(matched_tag)
        if agent is None:
            agent = DEFAULT_AGENT
            matched_tag = agent.tag
        log.info("未処理トリガーを検出: tag=%s file=%s", matched_tag, path)

        prev_p, target_p, next_p = extract_context(text, m.start(), m.end())
        request_id = uuid.uuid4().hex[:8]
        marker = f"{agent.tag}{EMOJI_SEEN}<!-- pi-id:{request_id} -->"

        paragraphs = split_paragraphs(text)
        target_idx = next(
            (i for i, (s, e, _) in enumerate(paragraphs) if s <= m.start() < e), None
        )
        callout = build_progress_callout(request_id, ["⏳ 準備中..."])

        if target_idx is not None:
            para_end = paragraphs[target_idx][1]
            new_text = (
                text[: m.start()] + marker + text[m.end():para_end]
                + "\n\n" + callout
                + text[para_end:]
            )
        else:
            new_text = text[: m.start()] + marker + "\n\n" + callout + text[m.end():]
        log.info("👀マーカーを挿入し、進捗calloutを追加しました: id=%s file=%s", request_id, path)

        path.write_text(new_text, encoding="utf-8")

        prompt = build_prompt(
            agent=agent,
            note_title=path.stem,
            prev_paragraphs=prev_p,
            target_paragraph=target_p,
            next_paragraphs=next_p,
        )
        return request_id, prompt, agent, matched_tag


def insert_result(
    path: Path, request_id: str, tag: str, result_text: str,
    error: bool = False, plain: bool = False,
) -> None:
    """マーカーを完了/エラーの絵文字に置き換え、「実行中」calloutを
    最終結果で上書きする(途中経過は残さない)。
    plain=True の場合はcalloutではなく地の文(プレーンなMarkdown)として書き戻す。"""
    lock = get_file_lock(str(path))
    with lock:
        text = path.read_text(encoding="utf-8")

        sm = seen_marker_re(tag, request_id).search(text)
        final_emoji = EMOJI_ERROR if error else EMOJI_DONE
        if sm:
            replaced_marker = f"{tag}{final_emoji}<!-- pi-id:{request_id} -->"
            text = text[: sm.start()] + replaced_marker + text[sm.end():]
        else:
            log.warning("トリガーマーカーが見つかりません: id=%s path=%s", request_id, path)

        if not error:
            result_text = strip_wrapping_callout(result_text)
        result_text = sanitize_trigger(result_text)
        body_lines = result_text.splitlines() or [""]

        if plain and not error:
            # 地の文としてそのまま載せる(前後の空行は段落区切りが保たれる)
            final_block = "\n".join(body_lines) + "\n"
        else:
            callout_type = "error" if error else "note"
            final_block = (
                "> [!" + callout_type + "]\n"
                + "\n".join(f"> {line}" for line in body_lines)
                + "\n"
            )

        pm = progress_callout_re(request_id).search(text)
        if pm:
            text = text[: pm.start()] + final_block + text[pm.end():]
        else:
            paragraphs = split_paragraphs(text)
            anchor = sm.start() if sm else 0
            target_idx = next(
                (i for i, (s, e, _) in enumerate(paragraphs) if s <= anchor < e), None
            )
            if target_idx is not None:
                para_end = paragraphs[target_idx][1]
                text = text[:para_end] + "\n\n" + final_block + text[para_end:]
            else:
                text = text + "\n\n" + final_block

        path.write_text(text, encoding="utf-8")


# ============================================================
# ワーカー
# ============================================================

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


def process_file(path: Path) -> None:
    try:
        result = mark_seen(path)
    except Exception:
        log.exception("mark_seenに失敗しました: %s", path)
        return
    if result is None:
        return  # 未処理のトリガーなし

    request_id, prompt, agent, tag = result
    log.info("処理開始 id=%s file=%s tag=%s", request_id, path, tag)

    def worker() -> None:
        activity = RunActivity()
        done_event = threading.Event()
        updater_thread = threading.Thread(
            target=run_progress_updater,
            args=(path, request_id, activity, done_event),
            daemon=True,
        )
        updater_thread.start()
        try:
            answer = call_pi_api(prompt, agent, activity)
            insert_result(path, request_id, tag, answer, error=False, plain=agent.plain_output)
            log.info("処理完了 id=%s file=%s", request_id, path)
        except Exception as e:
            log.exception("Pi呼び出しに失敗しました id=%s", request_id)
            insert_result(path, request_id, tag, f"エラーが発生しました: {e}", error=True)
        finally:
            done_event.set()
            updater_thread.join(timeout=5)

    executor.submit(worker)
    # 同一ファイルに複数の未処理@<Tag>がある場合に備え、続けて再スキャン
    executor.submit(process_file, path)


# ============================================================
# inotify 監視
# ============================================================

WATCH_FLAGS = flags.CLOSE_WRITE | flags.CREATE | flags.MOVED_TO | flags.DELETE_SELF


def add_watches_recursive(inotify: INotify, root: Path, wd_to_path: dict[int, str]) -> None:
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        wd = inotify.add_watch(dirpath, WATCH_FLAGS)
        wd_to_path[wd] = dirpath


def main() -> None:
    if not VAULT_DIR.exists():
        log.error("VAULT_DIRが存在しません: %s", VAULT_DIR)
        sys.exit(1)

    inotify = INotify()
    wd_to_path: dict[int, str] = {}
    add_watches_recursive(inotify, VAULT_DIR, wd_to_path)
    log.info("監視開始: %s (%d ディレクトリ)", VAULT_DIR, len(wd_to_path))

    # 起動時に既存ファイル内の未処理@<Tag>も一度スキャンしておく
    for md_path in VAULT_DIR.rglob("*.md"):
        if any(part in EXCLUDE_DIRS for part in md_path.parts):
            continue
        text = md_path.read_text(encoding="utf-8", errors="ignore")
        if TRIGGER_RE.search(text):
            process_file(md_path)

    while True:
        for event in inotify.read():
            dir_path = wd_to_path.get(event.wd)
            if dir_path is None:
                continue

            full_path = Path(dir_path) / event.name

            if event.mask & flags.ISDIR:
                if event.mask & flags.CREATE and event.name not in EXCLUDE_DIRS:
                    add_watches_recursive(inotify, full_path, wd_to_path)
                continue

            if not event.name.endswith(".md"):
                continue

            if event.mask & (flags.CLOSE_WRITE | flags.MOVED_TO):
                executor.submit(process_file, full_path)


if __name__ == "__main__":
    main()
