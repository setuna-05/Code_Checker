# -*- coding: utf-8 -*-
"""Code Checker

Pythonコードを ruff / mypy / bandit の3つのツールで自動チェックし、
AI(Claudeなど)に貼り付けて判断してもらうためのレポートを作る小さなアプリです。

- 画面(ダークモード)でファイルまたはフォルダを選び、「チェックを実行」を押します。
- 結果は、一番上の短いまとめだけを画面に表示します。
- 詳しい内容は「レポートをコピー」または「ファイルに保存」で取り出します。
- 「レビュー画面を開く」では、どのコードのどこが指摘されたかを人が確認できます。

このアプリは、チェック対象のファイルを書き換えません(読むだけです)。
"""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path

try:  # 画面部品(tkinter)が使えない環境でも、チェック処理だけは読み込めるようにする
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover
    tk = None  # type: ignore[assignment]

APP_NAME = "Code Checker"
APP_VERSION = "1.0"
PY = sys.executable
TOOL_ORDER = ("ruff", "mypy", "bandit")
TIMEOUT_SEC = 900

# チェックの対象から外すフォルダ(ライブラリ置き場や作業用の場所)
EXCLUDE_DIRS = (
    ".git", ".venv", "venv", "node_modules", "site-packages", "__pycache__",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".idea", ".vscode",
    "build", "dist", ".eggs", "__pypackages__",
)
# 3つのツールすべてが同じように調べる拡張子は ".py" だけなので、対象を .py にそろえる。
# (.pyi は bandit が、.pyw は ruff と mypy が調べないため)
PY_SUFFIXES = (".py",)
# 除外フォルダのほかに、3つのツールの対象をそろえるために外すもの
EGG_INFO_PATTERN = "*.egg-info"
OTHER_FILE_PATTERNS = ("*.pyi", "*.pyw", "*.ipynb")

# ruffで調べるルール。ruffのバージョンが変わっても基準がぶれないよう固定する。
# E4/E7/E9 = 書き方の基本的な間違い、F = 使っていない変数・定義していない名前など
RUFF_RULES = "E4,E7,E9,F"

DISCLAIMER = "※問題がなくても、実機で正しく動く保証ではありません。"

# ---------------------------------------------------------------- 色・大きさ
BG = "#1e1f22"
PANEL = "#2b2d31"
PANEL_2 = "#232428"
BORDER = "#3a3d42"
FG = "#e6e6e6"
SUB = "#9aa0a6"
ACCENT = "#3b82f6"
ACCENT_HOVER = "#5b9bff"
ACCENT_DARK = "#2a4f8f"
BTN = "#383b42"
BTN_HOVER = "#474b53"
RED = "#ff6b6b"
GREEN = "#5ad17a"
ORANGE = "#ffb454"
YELLOW = "#e5d36b"
HIT_BG = "#6b2b30"
OTHER_BG = "#3d3524"

SCALE = 1.0  # 画面の拡大率(Full HD・100%表示を1.0とする)


def px(value: float) -> int:
    """100%表示のときの大きさ(ピクセル)を、実際の画面の拡大率に合わせて変換する。"""
    return int(round(value * SCALE))


# ================================================================ チェック処理
@dataclass
class Issue:
    tool: str
    file: str          # 対象からの相対パス
    line: int          # 0 = 行の情報なし
    column: int        # 0 = 列の情報なし
    rule: str
    message: str
    severity: str      # レポートに載せる重大度(ツールの表記のまま)
    severity_ja: str   # 画面に出す日本語の重大度
    level: str         # 色分け用: high / mid / low
    extra: str = ""    # 追加情報(bandit の confidence など)


@dataclass
class ToolResult:
    name: str
    version: str = "取得できませんでした"
    commands: list[str] = field(default_factory=list)
    ok: bool = False                     # 実行でき、結果を読み取れたか
    failure: str = ""
    issues: list[Issue] = field(default_factory=list)
    raw_output: str = ""
    warnings: list[str] = field(default_factory=list)
    memo: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    target_name: str
    target_kind: str                     # "フォルダ" または "ファイル"
    base: Path                           # 相対パスの基準になる場所(画面・レポートには出さない)
    files: list[str]
    started: dt.datetime
    finished: dt.datetime | None = None
    tools: list[ToolResult] = field(default_factory=list)
    python_info: str = ""
    os_info: str = ""


@dataclass
class Summary:
    state: str           # ok / issues / incomplete
    headline: str
    counts: str
    notes: list[str]


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        PYTHONDONTWRITEBYTECODE="1",
        NO_COLOR="1",
    )
    for key in ("FORCE_COLOR", "MYPYPATH", "MYPY_CONFIG_FILE_DIR"):
        env.pop(key, None)
    return env


def run_command(args: list[str], cwd: Path, timeout: int = TIMEOUT_SEC) -> tuple[int | None, str, str]:
    """コマンドを実行して (終了コード, 標準出力, 標準エラー出力) を返す。起動できなければ終了コードは None。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windowsで黒い画面が出ないようにする
    try:
        proc = subprocess.run(
            args, cwd=str(cwd), capture_output=True, env=clean_env(),
            timeout=timeout, creationflags=flags,
        )
    except subprocess.TimeoutExpired:
        return None, "", f"{timeout}秒以内に終わらなかったため、中止しました。"
    except OSError as exc:
        return None, "", f"実行を始められませんでした: {exc}"
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def show_cmd(args: list[str], hide: dict[str, str] | None = None) -> str:
    """レポートに載せるコマンド表記(自分のパソコンの場所を含めないよう python に置き換える)。
    長すぎる部分(除外フォルダの一覧)は hide で短い言葉に置き換える。"""
    shown = [(hide or {}).get(a, a) for a in args[1:]]
    return subprocess.list2cmdline(["python", *shown])


def tool_version(module: str, cwd: Path) -> str:
    rc, out, err = run_command([PY, "-m", module, "--version"], cwd, timeout=120)
    text = (out or err).strip()
    first = text.splitlines()[0].strip() if text else ""
    if rc not in (0, None) or not first:
        return "取得できませんでした"
    if not first.lower().startswith(module):  # bandit は "__main__.py 1.9.4" と表示することがある
        number = first.split()[-1]
        return f"{module} {number}"
    return first


def raw_with_stderr(out: str, err: str) -> str:
    text = out.rstrip()
    if err.strip():
        text += ("\n\n" if text else "") + "[標準エラー出力]\n" + err.rstrip()
    return text


def note_raw_failure(res: ToolResult, rc_raw: int | None, err_raw: str) -> None:
    """原文表示用の再実行が失敗したとき、レポートで分かるように知らせる(判定そのものは変えない)。"""
    if rc_raw in (0, 1):
        return
    reason = short(err_raw, 300) if err_raw.strip() else "理由は取得できませんでした。"
    res.warnings.append(
        "「ツールの出力(そのまま)」を取得できなかったため、原文が欠けている、または不完全な可能性があります。"
        "(問題の一覧は取得できています。) " + reason
    )
    res.raw_output = ("(原文表示用の再実行に失敗したため、以下は不完全な可能性があります)\n"
                      + res.raw_output).rstrip()


def short(text: str, limit: int = 1500) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " …(以下省略)"


def to_rel(path_str: str, base: Path) -> str:
    """ツールが出したファイルの場所を、対象からの相対パスにそろえる。"""
    try:
        p = Path(path_str)
        if p.is_absolute():
            return str(p.resolve().relative_to(base.resolve()))
        return str(p)
    except (ValueError, OSError):
        return path_str


def exclude_regex() -> str:
    names = "|".join(re.escape(n) for n in EXCLUDE_DIRS)
    # フォルダ名の一致、名前が .egg-info で終わるフォルダ、.py 以外のファイル(.pyi など)を外す
    return rf"(^|[\\/])({names}|[^\\/]*\.egg-info)([\\/]|$)|\.(pyi|pyw)$"


def exclude_globs() -> str:
    parts: list[str] = []
    for n in EXCLUDE_DIRS:
        parts.append(f"*/{n}")
        parts.append(f"*/{n}/*")
    parts += ["*/*.egg-info", "*/*.egg-info/*", "*.pyi", "*.pyw"]
    return ",".join(parts)


def ruff_excludes() -> str:
    """ruff に渡す除外の一覧(mypy・bandit と調べる範囲をそろえるため)。"""
    return ",".join((*EXCLUDE_DIRS, EGG_INFO_PATTERN, *OTHER_FILE_PATTERNS))


def list_python_files(base: Path, target: Path) -> list[str]:
    if target.is_file():
        return [target.name]
    found: list[str] = []
    for root, dirs, files in os.walk(target):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS and not d.endswith(".egg-info"))
        for name in sorted(files):
            if name.lower().endswith(PY_SUFFIXES):
                found.append(str((Path(root) / name).relative_to(base)))
    return found


# ---------------------------------------------------------------- ruff
def run_ruff(base: Path, target_arg: str) -> ToolResult:
    res = ToolResult("ruff")
    res.version = tool_version("ruff", base)
    common = [
        PY, "-m", "ruff", "check", "--isolated", "--no-cache", "--no-respect-gitignore",
        "--select", RUFF_RULES, "--extend-exclude", ruff_excludes(),
    ]
    cmd_json = common + ["--output-format=json", target_arg]
    cmd_raw = common + ["--output-format=full", target_arg]
    hide = {ruff_excludes(): "<除外フォルダの一覧>"}
    res.commands = [
        "問題の一覧づくりに使用: " + show_cmd(cmd_json, hide),
        "ツールの出力(そのまま)に使用: " + show_cmd(cmd_raw, hide),
    ]
    res.memo = [
        "設定ファイルは読み込みません(--isolated)。自動修正はしません。",
        "mypy・bandit と調べる範囲をそろえるため、除外フォルダの指定を付けています。",
        f"調べるルール: {RUFF_RULES}(書き方の基本的な間違い、使っていない変数、定義していない名前など)",
        "ruffには重大度の区別がないため、重大度はすべて「指摘」と表示します。",
    ]

    rc, out, err = run_command(cmd_json, base)
    rc_raw, out_raw, err_raw = run_command(cmd_raw, base)
    res.raw_output = raw_with_stderr(out_raw, err_raw)
    note_raw_failure(res, rc_raw, err_raw)

    if rc not in (0, 1):
        res.failure = short(err or out or "ruff を実行できませんでした。")
        return res
    try:
        data = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        res.failure = "ruff の出力を読み取れませんでした。\n" + short(err or out)
        return res
    if rc == 1 and not data:
        res.failure = "ruff の結果を読み取れませんでした。\n" + short(err or out)
        return res

    for d in data:
        loc = d.get("location") or {}
        res.issues.append(Issue(
            tool="ruff",
            file=to_rel(d.get("filename") or "", base),
            line=int(loc.get("row") or 0),
            column=int(loc.get("column") or 0),
            rule=d.get("code") or "syntax-error",
            message=str(d.get("message") or ""),
            severity="指摘", severity_ja="指摘", level="mid",
        ))
    res.ok = True
    return res


# ---------------------------------------------------------------- mypy
MYPY_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)? (?P<sev>error|warning|note): "
    r"(?P<msg>.*?)(?:\s+\[(?P<code>[A-Za-z0-9_-]+)\])?$"
)


def run_mypy(base: Path, target_arg: str) -> ToolResult:
    res = ToolResult("mypy")
    res.version = tool_version("mypy", base)
    cmd = [
        PY, "-m", "mypy", "--config-file", "", "--ignore-missing-imports",
        "--explicit-package-bases", "--show-column-numbers", "--show-error-codes",
        "--no-pretty", "--no-color-output", "--cache-dir", os.devnull,
        "--exclude", exclude_regex(), target_arg,
    ]
    res.commands = [
        "問題の一覧づくりと、ツールの出力(そのまま)に使用: "
        + show_cmd(cmd, {exclude_regex(): "<除外フォルダの条件>"})
    ]
    res.memo = [
        "設定ファイルは読み込みません。",
        "--ignore-missing-imports を付けています。"
        "このアプリの専用環境にない外部ライブラリを読み込む行について、「見つからない」という警告を出さない設定です。",
        "mypyの「note(補足)」の行は、件数と問題一覧には含めず、ツールの出力(そのまま)にだけ含まれます。",
    ]

    rc, out, err = run_command(cmd, base)
    res.raw_output = raw_with_stderr(out, err)
    if rc is None:
        res.failure = short(err or "mypy を実行できませんでした。")
        return res

    notes = 0
    for line in out.splitlines():
        m = MYPY_RE.match(line.strip())
        if not m:
            continue
        sev = m.group("sev")
        if sev == "note":
            notes += 1
            continue
        res.issues.append(Issue(
            tool="mypy",
            file=to_rel(m.group("file"), base),
            line=int(m.group("line")),
            column=int(m.group("col") or 0),
            rule=m.group("code") or "-",
            message=m.group("msg"),
            severity=sev,
            severity_ja="エラー" if sev == "error" else "警告",
            level="high" if sev == "error" else "mid",
        ))
    if notes:
        res.memo.append(f"note の行: {notes}行(件数に含めていません)")

    if "prevented further checking" in out:
        res.warnings.append("構文エラーのため、mypyは全ファイルのチェックを最後まで行えていません。")

    if rc not in (0, 1, 2):
        res.failure = short(err or out or "mypy を実行できませんでした。")
        return res
    if rc in (1, 2) and not res.issues:
        res.failure = "mypy の出力を読み取れませんでした。\n" + short(err or out)
        return res
    res.ok = True
    return res


# ---------------------------------------------------------------- bandit
BANDIT_JA = {"HIGH": ("高", "high"), "MEDIUM": ("中", "mid"), "LOW": ("低", "low")}


def run_bandit(base: Path, target_arg: str) -> ToolResult:
    res = ToolResult("bandit")
    res.version = tool_version("bandit", base)
    common = [PY, "-m", "bandit", "-r", target_arg, "-q", "-x", exclude_globs()]
    cmd_json = common + ["-f", "json"]
    cmd_raw = common + ["-f", "txt"]
    hide = {exclude_globs(): "<除外フォルダの一覧>"}
    res.commands = [
        "問題の一覧づくりに使用: " + show_cmd(cmd_json, hide),
        "ツールの出力(そのまま)に使用: " + show_cmd(cmd_raw, hide),
    ]
    res.memo = [
        "設定ファイルは読み込みません(標準設定)。",
        "severity は問題の重大度、confidence は「本当に問題である確からしさ」です(どちらも HIGH / MEDIUM / LOW)。",
    ]

    rc, out, err = run_command(cmd_json, base)
    rc_raw, out_raw, err_raw = run_command(cmd_raw, base)
    res.raw_output = raw_with_stderr(out_raw, err_raw)
    note_raw_failure(res, rc_raw, err_raw)

    if rc not in (0, 1):
        res.failure = short(err or out or "bandit を実行できませんでした。")
        return res
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        res.failure = "bandit の出力を読み取れませんでした。\n" + short(err or out)
        return res

    for d in data.get("results", []):
        sev = str(d.get("issue_severity") or "").upper()
        ja, level = BANDIT_JA.get(sev, (sev or "-", "mid"))
        res.issues.append(Issue(
            tool="bandit",
            file=to_rel(d.get("filename") or "", base),
            line=int(d.get("line_number") or 0),
            column=int((d.get("col_offset") or 0)),
            rule=str(d.get("test_id") or "-"),
            message=str(d.get("issue_text") or ""),
            severity=sev or "-", severity_ja=ja, level=level,
            extra=f"confidence={d.get('issue_confidence', '-')}",
        ))
    for d in data.get("errors", []):
        res.issues.append(Issue(
            tool="bandit",
            file=to_rel(d.get("filename") or "", base),
            line=0, column=0, rule="解析エラー",
            message="このファイルは bandit が読み取れず、チェックされていません: " + str(d.get("reason") or ""),
            severity="ERROR", severity_ja="エラー", level="high",
        ))
    res.ok = True
    return res


# ---------------------------------------------------------------- 全体の流れ
def run_check(target: Path, progress=None) -> CheckResult:
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    target = target.resolve()
    if target.is_file():
        base, target_arg, kind = target.parent, os.path.join(".", target.name), "ファイル"
        name = target.name
    else:
        base, target_arg, kind = target, ".", "フォルダ"
        name = target.name or str(target)

    say("対象のファイルを調べています…")
    result = CheckResult(
        target_name=name, target_kind=kind, base=base,
        files=list_python_files(base, target),
        started=dt.datetime.now().astimezone(),
        python_info=f"{platform.python_implementation()} {platform.python_version()}",
        os_info=platform.platform(),
    )
    if not result.files:
        result.finished = dt.datetime.now().astimezone()
        return result

    for runner in (run_ruff, run_mypy, run_bandit):
        say(f"{runner.__name__.replace('run_', '')} を実行しています…")
        try:
            result.tools.append(runner(base, target_arg))
        except Exception:  # どのツールで失敗しても、他のツールは続けて実行する
            tool = ToolResult(runner.__name__.replace("run_", ""))
            tool.failure = short(traceback.format_exc())
            result.tools.append(tool)
    result.finished = dt.datetime.now().astimezone()
    return result


def summarize(result: CheckResult) -> Summary:
    total = sum(len(t.issues) for t in result.tools)
    failed = [t for t in result.tools if not t.ok]
    counts = " / ".join(
        f"{t.name}: {len(t.issues)}件" if t.ok else f"{t.name}: 実行できず" for t in result.tools
    )
    notes: list[str] = []
    for t in failed:
        if "No module named" in t.failure:
            notes.append(f"{t.name} が見つかりません。「1_setup.bat」を実行してください。")
        else:
            notes.append(f"{t.name} は実行できなかったため、チェックできていません。")
    for t in result.tools:
        notes.extend(f"{t.name}: {w}" for w in t.warnings)
    if total:
        return Summary("issues", f"問題あり({total}件)", counts, notes)
    if failed:
        return Summary("incomplete", "チェック未完了", counts, notes)
    return Summary("ok", "問題なし", counts, notes)


# ---------------------------------------------------------------- レポート作成
def fence(text: str) -> str:
    body = text.rstrip() if text.strip() else "(出力なし)"
    ticks = "```"
    while ticks in body:
        ticks += "`"
    return f"{ticks}text\n{body}\n{ticks}"


def one_line(text: str) -> str:
    return re.sub(r"\s*\n\s*", " ", text).strip()


def issue_line(i: int, issue: Issue) -> str:
    parts = [
        f"[{i}] file={issue.file}",
        f"line={issue.line if issue.line > 0 else '-'}",
        f"col={issue.column if issue.column > 0 else '-'}",
        f"rule={issue.rule}",
        f"severity={issue.severity}",
    ]
    if issue.extra:
        parts.append(issue.extra)
    parts.append(f"message={one_line(issue.message)}")
    return "- " + " | ".join(parts)


def build_report(result: CheckResult) -> str:
    s = summarize(result)
    out: list[str] = []
    add = out.append

    add(f"# {APP_NAME} レポート")
    add("")
    add("このレポートは、AIに貼り付けて内容を判断してもらうための資料です。")
    add("読み方:")
    add("- 各ツールの「問題一覧」は、1行に1つの問題を `項目=値` の形で並べています(file=場所, line=行, col=列, rule=ルール名, severity=重大度, message=内容)。")
    add("- 「ツールの出力(そのまま)」は、各ツールが出した文字列を加工せずに載せています。")
    add("- file は、チェック対象からの相対パスです。")
    add("- このレポートは機械的なチェックの結果であり、実機で正しく動くことの保証ではありません。")
    add("")

    add("## 1. まとめ")
    add("")
    add(f"- 結果: {s.headline}")
    add(f"- 内訳: {s.counts}")
    for note in s.notes:
        add(f"- 注意: {note}")
    add(f"- {DISCLAIMER}")
    add("")

    add("## 2. チェック情報")
    add("")
    finished = result.finished or result.started
    add(f"- チェック日時: {result.started.strftime('%Y-%m-%d %H:%M:%S %z')}")
    add(f"- 所要時間: 約{max(0, int((finished - result.started).total_seconds()))}秒")
    add(f"- 対象: {result.target_name}({result.target_kind})")
    add(f"- Python: {result.python_info}")
    add(f"- OS: {result.os_info}")
    add(f"- {APP_NAME} のバージョン: {APP_VERSION}")
    for t in result.tools:
        add(f"- ツール: {t.version}")
    add(f"- 対象から外したフォルダ名: {', '.join(EXCLUDE_DIRS)}")
    add("- 3つのツールとも、対象のフォルダにある設定ファイルは読み込まず、同じ基準で調べています。")
    add("")
    add(f"### 対象ファイル({len(result.files)}個)")
    add("")
    for f in result.files:
        add(f"- {f}")
    add("")

    for n, t in enumerate(result.tools, start=3):
        add(f"## {n}. {t.name}")
        add("")
        add(f"- バージョン: {t.version}")
        for c in t.commands:
            add(f"- 実行コマンド({c.split(': ', 1)[0]}): `{c.split(': ', 1)[1]}`")
        for m in t.memo:
            add(f"- 補足: {m}")
        if t.ok:
            add(f"- 結果: {len(t.issues)}件")
        else:
            add("- 結果: **実行できなかった、または結果を読み取れなかった(このツールのチェックはできていません)**")
        for w in t.warnings:
            add(f"- 注意: {w}")
        if not t.ok:
            add("")
            add("### 失敗の内容")
            add("")
            add(fence(t.failure))
        add("")
        add("### 問題一覧")
        add("")
        if t.issues:
            ordered = sorted(t.issues, key=lambda x: (x.file, x.line, x.column, x.rule))
            for i, issue in enumerate(ordered, start=1):
                add(issue_line(i, issue))
        elif t.ok:
            add("(問題は見つかりませんでした)")
        else:
            add("(ツールが実行できなかったため、一覧はありません)")
        add("")
        add("### ツールの出力(そのまま)")
        add("")
        add(fence(t.raw_output))
        add("")

    return "\n".join(out).rstrip() + "\n"


# ================================================================ 画面(tkinter)
F_TITLE: tuple = ("Arial", 12)
F_HUGE: tuple = ("Arial", 12)
F_BIG: tuple = ("Arial", 12)
F_UI: tuple = ("Arial", 12)
F_UI_BOLD: tuple = ("Arial", 12)
F_SMALL: tuple = ("Arial", 12)
F_CODE: tuple = ("Courier", 12)


def init_fonts() -> None:
    global F_TITLE, F_HUGE, F_BIG, F_UI, F_UI_BOLD, F_SMALL, F_CODE
    families = set(tkfont.families())
    ui = next((f for f in ("Yu Gothic UI", "Meiryo UI", "Segoe UI", "Arial") if f in families), "Arial")
    mono = next((f for f in ("Consolas", "MS Gothic", "Courier New") if f in families), "Courier")
    F_TITLE = (ui, 24, "bold")
    F_HUGE = (ui, 30, "bold")
    F_BIG = (ui, 16)
    F_UI = (ui, 12)
    F_UI_BOLD = (ui, 12, "bold")
    F_SMALL = (ui, 11)
    F_CODE = (mono, 12)


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def make_button(parent, text, command, primary=False, big=False):
    bg = ACCENT if primary else BTN
    hover = ACCENT_HOVER if primary else BTN_HOVER
    btn = tk.Button(
        parent, text=text, command=command, bg=bg, fg="#ffffff",
        activebackground=hover, activeforeground="#ffffff",
        disabledforeground="#6b6f76", relief="flat", bd=0,
        padx=px(22 if big else 18), pady=px(14 if big else 9),
        font=F_BIG if big else F_UI, cursor="hand2", highlightthickness=0,
    )

    def enter(_e):
        if str(btn["state"]) == "normal":
            btn.config(bg=hover)

    btn.bind("<Enter>", enter)
    btn.bind("<Leave>", lambda _e: btn.config(bg=bg))
    return btn


def center_window(win, width: int, height: int) -> None:
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    width, height = min(width, int(sw * 0.95)), min(height, int(sh * 0.9))
    x, y = (sw - width) // 2, max(0, (sh - height) // 2 - px(20))
    win.geometry(f"{width}x{height}+{x}+{y}")


def setup_styles(root) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                    rowheight=px(30), borderwidth=0, font=F_UI)
    style.configure("Treeview.Heading", background=BTN, foreground=FG, relief="flat",
                    font=F_UI_BOLD, padding=px(6))
    style.map("Treeview", background=[("selected", ACCENT_DARK)], foreground=[("selected", "#ffffff")])
    style.map("Treeview.Heading", background=[("active", BTN_HOVER)])
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background=BTN, troughcolor=PANEL_2,
                        bordercolor=PANEL_2, arrowcolor=FG, lightcolor=BTN, darkcolor=BTN)
        style.map(f"{orient}.TScrollbar", background=[("active", BTN_HOVER)])
    style.configure("TCombobox", fieldbackground=BTN, background=BTN, foreground=FG,
                    arrowcolor=FG, bordercolor=BORDER, lightcolor=BTN, darkcolor=BTN,
                    selectbackground=BTN, selectforeground=FG, padding=px(4))
    style.map("TCombobox", fieldbackground=[("readonly", BTN)], foreground=[("readonly", FG)],
              selectbackground=[("readonly", BTN)], selectforeground=[("readonly", FG)])
    root.option_add("*TCombobox*Listbox.background", PANEL)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_DARK)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.font", F_UI)


STATE_COLOR = {"ok": GREEN, "issues": RED, "incomplete": ORANGE}
LEVEL_COLOR = {"high": RED, "mid": ORANGE, "low": YELLOW}


class App:
    def __init__(self, root) -> None:
        self.root = root
        self.target: Path | None = None
        self.result: CheckResult | None = None
        self.report_text = ""
        self.review: ReviewWindow | None = None
        self.queue: queue.Queue = queue.Queue()
        self._build()

    # ---- 画面づくり
    def _build(self) -> None:
        r = self.root
        r.title(APP_NAME)
        r.configure(bg=BG)
        center_window(r, px(1040), px(740))
        r.minsize(px(820), px(620))

        outer = tk.Frame(r, bg=BG, padx=px(36), pady=px(28))
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text=APP_NAME, font=F_TITLE, bg=BG, fg=FG).pack(anchor="w")
        tk.Label(outer, text="Pythonコードを ruff / mypy / bandit で自動チェックします",
                 font=F_SMALL, bg=BG, fg=SUB).pack(anchor="w", pady=(px(2), px(18)))

        card = tk.Frame(outer, bg=PANEL, padx=px(22), pady=px(16))
        card.pack(fill="x")
        card.columnconfigure(0, weight=1)
        tk.Label(card, text="チェックする対象", font=F_SMALL, bg=PANEL, fg=SUB).grid(row=0, column=0, sticky="w")
        self.target_label = tk.Label(card, text="まだ選んでいません", font=F_BIG, bg=PANEL, fg=SUB, anchor="w")
        self.target_label.grid(row=1, column=0, sticky="w", pady=(px(4), 0))
        btns = tk.Frame(card, bg=PANEL)
        btns.grid(row=0, column=1, rowspan=2, sticky="e")
        make_button(btns, "ファイルを選ぶ", self.pick_file).pack(side="left", padx=(0, px(8)))
        make_button(btns, "フォルダを選ぶ", self.pick_folder).pack(side="left")

        self.run_btn = make_button(outer, "チェックを実行", self.start_check, primary=True, big=True)
        self.run_btn.pack(fill="x", pady=(px(18), px(6)))
        self.run_btn.config(state="disabled")
        self.status_label = tk.Label(outer, text="", font=F_SMALL, bg=BG, fg=SUB, anchor="w")
        self.status_label.pack(fill="x")

        sumcard = tk.Frame(outer, bg=PANEL, padx=px(24), pady=px(18))
        sumcard.pack(fill="both", expand=True, pady=(px(12), px(16)))
        tk.Label(sumcard, text="まとめ", font=F_SMALL, bg=PANEL, fg=SUB).pack(anchor="w")
        self.sum_main = tk.Label(sumcard, text="—", font=F_HUGE, bg=PANEL, fg=SUB, anchor="w")
        self.sum_main.pack(anchor="w", pady=(px(6), px(2)))
        self.sum_counts = tk.Label(sumcard, text="", font=F_BIG, bg=PANEL, fg=FG, anchor="w")
        self.sum_counts.pack(anchor="w")
        self.sum_note = tk.Label(sumcard, text="", font=F_UI, bg=PANEL, fg=SUB, anchor="w",
                                 justify="left", wraplength=px(900))
        self.sum_note.pack(anchor="w", pady=(px(10), 0))

        bottom = tk.Frame(outer, bg=BG)
        bottom.pack(fill="x")
        self.copy_btn = make_button(bottom, "レポートをコピー", self.copy_report)
        self.save_btn = make_button(bottom, "ファイルに保存", self.save_report)
        self.review_btn = make_button(bottom, "レビュー画面を開く", self.open_review)
        for b in (self.copy_btn, self.save_btn, self.review_btn):
            b.pack(side="left", padx=(0, px(10)))
        self._set_result_buttons(False)
        self.notice = tk.Label(outer, text="", font=F_SMALL, bg=BG, fg=GREEN, anchor="w")
        self.notice.pack(fill="x", pady=(px(10), 0))

    def _set_result_buttons(self, enabled: bool) -> None:
        for b in (self.copy_btn, self.save_btn, self.review_btn):
            b.config(state="normal" if enabled else "disabled")

    def _reset_summary(self) -> None:
        self.sum_main.config(text="—", fg=SUB)
        self.sum_counts.config(text="")
        self.sum_note.config(text="")
        self.notice.config(text="")
        self._set_result_buttons(False)

    # ---- 対象の選択
    def _set_target(self, path: str, kind: str) -> None:
        if not path:
            return
        self.target = Path(path)
        name = self.target.name or str(self.target)
        self.target_label.config(text=f"{kind}: {name}", fg=FG)
        self.run_btn.config(state="normal")
        self.status_label.config(text="")
        self.result = None
        self._reset_summary()

    def pick_file(self) -> None:
        path = filedialog.askopenfilename(
            title="チェックするファイルを選んでください",
            filetypes=[("Pythonファイル", "*.py")],
        )
        self._set_target(path, "ファイル")

    def pick_folder(self) -> None:
        path = filedialog.askdirectory(title="チェックするフォルダを選んでください", mustexist=True)
        self._set_target(path, "フォルダ")

    # ---- チェックの実行
    def start_check(self) -> None:
        if not self.target:
            return
        self.run_btn.config(state="disabled")
        self._reset_summary()
        self.status_label.config(text="チェックを始めます…", fg=SUB)
        threading.Thread(target=self._worker, args=(self.target,), daemon=True).start()
        self.root.after(100, self._poll)

    def _worker(self, target: Path) -> None:
        try:
            result = run_check(target, progress=lambda m: self.queue.put(("progress", m)))
            self.queue.put(("done", result))
        except Exception:
            self.queue.put(("error", traceback.format_exc()))

    def _poll(self) -> None:
        finished = False
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "progress":
                    self.status_label.config(text=payload, fg=SUB)
                elif kind == "done":
                    self._on_done(payload)
                    finished = True
                elif kind == "error":
                    self.status_label.config(text="")
                    self.run_btn.config(state="normal")
                    messagebox.showerror(APP_NAME, "チェック中に予期しないエラーが起きました。\n\n" + short(payload, 1200))
                    finished = True
        except queue.Empty:
            pass
        if not finished:
            self.root.after(100, self._poll)

    def _on_done(self, result: CheckResult) -> None:
        self.run_btn.config(state="normal")
        self.status_label.config(text="")
        if not result.files:
            messagebox.showinfo(APP_NAME, "チェックできるPythonファイル(.py)が見つかりませんでした。\n別のフォルダやファイルを選んでください。")
            return
        self.result = result
        self.report_text = build_report(result)
        s = summarize(result)
        self.sum_main.config(text=s.headline, fg=STATE_COLOR[s.state])
        self.sum_counts.config(text=s.counts)
        self.sum_note.config(text="\n".join([*s.notes, DISCLAIMER]),
                             fg=ORANGE if s.notes else SUB)
        self._set_result_buttons(True)
        self.status_label.config(text=f"チェックが終わりました({len(result.files)}ファイル)", fg=SUB)
        if self.review is not None:
            self.review.close()
            self.review = None

    # ---- レポートの取り出し
    def copy_report(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.report_text)
        self.root.update()
        self.notice.config(text="レポートをコピーしました。AIの入力欄に貼り付けてください。")

    def save_report(self) -> None:
        stamp = (self.result.started if self.result else dt.datetime.now()).strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            title="レポートの保存先を選んでください",
            defaultextension=".txt",
            initialfile=f"code_checker_report_{stamp}.txt",
            filetypes=[("テキストファイル", "*.txt"), ("Markdown", "*.md")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self.report_text, encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"保存できませんでした。\n{exc}")
            return
        self.notice.config(text=f"保存しました: {Path(path).name}")

    def open_review(self) -> None:
        if not self.result:
            return
        if self.review is not None and self.review.alive():
            self.review.win.lift()
            self.review.win.focus_force()
            return
        self.review = ReviewWindow(self, self.result)


class ReviewWindow:
    """どのファイルのどこが指摘されたかを、人が確認するための画面。"""

    FILTERS = ("すべて", *TOOL_ORDER)

    def __init__(self, app: App, result: CheckResult) -> None:
        self.app = app
        self.result = result
        self.issues = sorted(
            (i for t in result.tools for i in t.issues),
            key=lambda i: (TOOL_ORDER.index(i.tool) if i.tool in TOOL_ORDER else 99, i.file, i.line, i.column),
        )
        self.cache: dict[str, list[str] | None] = {}

        self.win = tk.Toplevel(app.root)
        win = self.win
        win.title(f"{APP_NAME} - レビュー({result.target_name})")
        win.configure(bg=BG)
        center_window(win, px(1600), px(900))
        win.minsize(px(900), px(520))

        top = tk.Frame(win, bg=BG, padx=px(20), pady=px(14))
        top.pack(fill="x")
        tk.Label(top, text="絞り込み", font=F_UI, bg=BG, fg=SUB).pack(side="left")
        self.filter_var = tk.StringVar(value=self.FILTERS[0])
        combo = ttk.Combobox(top, textvariable=self.filter_var, values=self.FILTERS,
                             state="readonly", width=10, font=F_UI)
        combo.pack(side="left", padx=(px(10), px(16)))
        combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_list())
        self.count_label = tk.Label(top, text="", font=F_UI, bg=BG, fg=FG)
        self.count_label.pack(side="left")

        # 下: 選んだ問題の詳しい内容
        self.detail = tk.Text(win, height=6, bg=PANEL, fg=FG, font=F_UI, relief="flat", bd=0,
                              padx=px(16), pady=px(10), wrap="word", state="disabled",
                              highlightthickness=0)
        self.detail.pack(side="bottom", fill="x", padx=px(20), pady=(px(10), px(18)))
        self.detail.tag_configure("label", foreground=SUB)
        self.detail.tag_configure("value", foreground=FG)
        for level, color in LEVEL_COLOR.items():
            self.detail.tag_configure(f"sev_{level}", foreground=color, font=F_UI_BOLD)

        paned = tk.PanedWindow(win, orient="horizontal", bg=BG, bd=0, sashwidth=px(8), sashrelief="flat")
        paned.pack(fill="both", expand=True, padx=px(20))

        # 左: 問題の一覧
        left = tk.Frame(paned, bg=BG)
        columns = ("tool", "rule", "where", "sev")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        for col, title, width, stretch in (
            ("tool", "ツール", 80, False), ("rule", "ルール", 110, False),
            ("where", "場所", 260, True), ("sev", "重大度", 70, False),
        ):
            self.tree.heading(col, text=title, anchor="w")
            self.tree.column(col, width=px(width), minwidth=px(50), stretch=stretch, anchor="w")
        for level, color in LEVEL_COLOR.items():
            self.tree.tag_configure(level, foreground=color)
        ysb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.on_select())
        paned.add(left, minsize=px(420), width=px(620))

        # 右: コード
        right = tk.Frame(paned, bg=PANEL_2)
        self.file_label = tk.Label(right, text="", font=F_UI_BOLD, bg=PANEL_2, fg=FG, anchor="w",
                                   padx=px(14), pady=px(8))
        self.file_label.pack(fill="x")
        cx = ttk.Scrollbar(right, orient="horizontal")
        cx.pack(side="bottom", fill="x")
        body = tk.Frame(right, bg=PANEL_2)
        body.pack(fill="both", expand=True)
        self.code = tk.Text(body, bg=PANEL_2, fg=FG, font=F_CODE, relief="flat", bd=0, wrap="none",
                            padx=px(10), pady=px(6), insertbackground=FG, highlightthickness=0,
                            state="disabled", tabs=(tkfont.Font(font=F_CODE).measure(" " * 4),))
        cy = ttk.Scrollbar(body, orient="vertical", command=self.code.yview)
        cx.config(command=self.code.xview)
        self.code.configure(yscrollcommand=cy.set, xscrollcommand=cx.set)
        cy.pack(side="right", fill="y")
        self.code.pack(side="left", fill="both", expand=True)
        self.code.tag_configure("other", background=OTHER_BG)
        self.code.tag_configure("hit", background=HIT_BG)
        self.code.tag_configure("msg", foreground=SUB)
        self.code.tag_raise("hit")
        paned.add(right, minsize=px(500))

        win.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_list()

    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def close(self) -> None:
        if self.alive():
            self.win.destroy()
        if self.app.review is self:
            self.app.review = None

    def refresh_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        flt = self.filter_var.get()
        shown = 0
        for idx, issue in enumerate(self.issues):
            if flt != "すべて" and issue.tool != flt:
                continue
            where = f"{issue.file}:{issue.line}" if issue.line > 0 else issue.file
            self.tree.insert("", "end", iid=str(idx), tags=(issue.level,),
                             values=(issue.tool, issue.rule, where, issue.severity_ja))
            shown += 1
        self.count_label.config(text=f"{shown}件を表示中(全{len(self.issues)}件)")
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[0])
            self.tree.focus(children[0])
            self.on_select()
        else:
            self.show_message("表示できる問題はありません。" if self.issues else "問題は見つかりませんでした。")

    def show_message(self, text: str) -> None:
        self.file_label.config(text="")
        self._set_code(text + "\n")
        self._set_detail([])

    def _set_code(self, text: str) -> None:
        self.code.config(state="normal")
        self.code.delete("1.0", "end")
        self.code.insert("1.0", text)
        self.code.config(state="disabled")

    def _set_detail(self, rows: list[tuple[str, str, str]]) -> None:
        d = self.detail
        d.config(state="normal")
        d.delete("1.0", "end")
        for label, value, tag in rows:
            d.insert("end", f"{label}  ", "label")
            d.insert("end", value + "\n", tag)
        d.config(state="disabled")

    def _read_lines(self, rel: str) -> list[str] | None:
        if rel in self.cache:
            return self.cache[rel]
        lines: list[str] | None = None
        try:
            data = (self.result.base / rel).read_bytes()
            text = None
            for enc in ("utf-8-sig", "cp932"):
                try:
                    text = data.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                text = data.decode("utf-8", "replace")
            lines = re.split(r"\r\n|\r|\n", text)
            if lines and lines[-1] == "":
                lines.pop()
        except OSError:
            lines = None
        self.cache[rel] = lines
        return lines

    def on_select(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        issue = self.issues[int(sel[0])]
        lines = self._read_lines(issue.file)

        place = f"{issue.file}  {issue.line}行目" if issue.line > 0 else issue.file
        self.file_label.config(text=place)
        if lines is None:
            self._set_code("このファイルを読み込めませんでした(移動・削除された可能性があります)。\n")
        else:
            self.code.config(state="normal")
            self.code.delete("1.0", "end")
            width = max(4, len(str(len(lines))))
            self.code.insert("1.0", "\n".join(f"{n:>{width}} │ {text}" for n, text in enumerate(lines, 1)) + "\n")
            same_file = {i.line for i in self.issues if i.file == issue.file and i.line > 0}
            for n in same_file:
                if n <= len(lines):
                    self.code.tag_add("other", f"{n}.0", f"{n}.end+1c")
            if 0 < issue.line <= len(lines):
                self.code.tag_add("hit", f"{issue.line}.0", f"{issue.line}.end+1c")
                self.code.yview_moveto(max(0, issue.line - 12) / max(1, len(lines)))
            else:
                self.code.yview_moveto(0)
            self.code.xview_moveto(0)
            self.code.config(state="disabled")

        sev_text = issue.severity_ja
        if issue.extra:
            sev_text += f"({issue.extra})"
        self._set_detail([
            ("ツール  ", f"{issue.tool}", "value"),
            ("ルール  ", issue.rule, "value"),
            ("重大度  ", sev_text, f"sev_{issue.level}"),
            ("場所    ", place + (f"  {issue.column}列目" if issue.column > 0 else ""), "value"),
            ("内容    ", one_line(issue.message), "value"),
        ])


def main() -> int:
    if tk is None:
        print("画面の部品(tkinter)が見つかりません。Pythonを入れ直すときに「tcl/tk and IDLE」を選んでください。")
        input("Enterキーを押すと閉じます。")
        return 1
    enable_dpi_awareness()
    root = tk.Tk()
    global SCALE
    SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
    init_fonts()
    setup_styles(root)
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
