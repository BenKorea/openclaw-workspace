#!/usr/bin/env python3
"""gmail-label-actions — 8-라벨 Gmail 액션 라우터 (예제·템플릿).

라벨이 붙은 메일을 그 라벨이 뜻하는 **Google Workspace 후속작업까지 완결** 하고,
메일은 스레드(대화) 전체 + 첨부를 저장 디렉토리에 캡처한다.

라벨 집합 = {일정,할일,회신} 멱집합(2³) + 저장. 모든 메일이 정확히 한 라벨에 귀속:

  1 저장            → (액션 없음)              + 캡처 → "9 완료"
  2 일정            → Calendar 이벤트          + 캡처 → "9 완료"
  3 일정+할일       → Calendar + Task          + 캡처 → "9 완료"
  6 할일            → Task                     + 캡처 → "9 완료"
  4 일정+회신       → Calendar + 초안          + 캡처 → (라벨 유지)
  5 일정+할일+회신  → Calendar + Task + 초안   + 캡처 → (라벨 유지)
  7 할일+회신       → Task + 초안              + 캡처 → (라벨 유지)
  8 회신            → 초안                     + 캡처 → (라벨 유지)

설계 분담 (2nd-brain 분리 원칙):
  - 이 라우터 = **캡처(판단0) + 좁은 구조화 추출 + GWS 액션 완결**. 추출(일시·할일·
    회신 초안)은 `claude --print` 헤드리스 1회 호출(JSON/STATUS 계약, 툴 비활성) —
    "에이전트 판단" 이 아니라 결정형에 가까운 *바운디드 추출*. 무인 cron 안전.
  - PARA 분류·요약·`[[링크]]` 등 **열린 판단은 brainify(Claude Code 스킬)** 가 나중에.
    이 라우터는 knowledge/ 노트를 만들지 않는다.

멱등성:
  - 캡처: threadId + message_count (스레드별 `_thread.md` frontmatter). 동일하면 재기록 0,
    메시지 수 변동이면 폴더 덮어쓰기(폴더명=불변 스탬프).
  - GWS 액션: 스레드 폴더의 **`_actions.json` 사이드카** 에 생성된 ID 기록
    (calendar_event_id / google_task_id / gmail_draft_id). 캡처가 `_thread.md` 를
    덮어써도 사이드카는 보존 → 크래시-재개 시 이중 생성 0.

종결(commit point):
  - terminal(1·2·3·6): 모든 필수 액션 성공 후에만 라벨 "9 완료" 부착 + 원라벨 제거.
    필수 액션 실패(예: 일시 추출 실패) → 라벨 유지, 다음 사이클 재시도.
  - 회신(4·5·7·8): 초안 생성까지 완결, 라벨은 유지(비-terminal). 실발송 감지→자동
    "9 완료" 승급은 **Phase 2**(awaiting_reply 영속큐 + sent-poll). 지금은 초안만.

사용:
  GMAIL_ROUTER_ACCOUNT=you@gmail.com python3 run.py            # 전체 실행
  GMAIL_ROUTER_ACCOUNT=you@gmail.com python3 run.py --dry-run  # 라벨별 건수만(검색)
  ... python3 run.py --label "2 일정"                          # 특정 라벨만

환경변수:
  GMAIL_ROUTER_ACCOUNT  gog 계정 (필수 — 본인 Gmail 계정. 기본값 없음: 누출 방지)
  GMAIL_ROUTER_INBOX    캡처 저장 위치 (기본: ~/.openclaw/workspace/attachments)

전제:
  - gog CLI 설치 + 해당 계정 인증 (Gmail·Calendar·Tasks scope).
  - 추출(일시·할일·회신)에는 Claude Code CLI(`claude`) 설치·인증 필요.
    `claude` 없으면: 일정·회신은 skip(라벨 유지), 할일은 제목 fallback 으로만 생성.

라벨명("1 저장" … "9 완료")은 이 스킬의 **고정 규칙** — 사용자 수정 대상 아님.
Gmail 에 이 9개 라벨을 그대로 만들어 사용하세요. 자세한 적용·설치는 README.md.
"""
from __future__ import annotations
import base64
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

# ── 설정 ────────────────────────────────────────────────────────────────
ACCOUNT = os.environ.get("GMAIL_ROUTER_ACCOUNT", "").strip()  # 필수 (기본값 없음)
INBOX = pathlib.Path(os.path.expanduser(
    os.environ.get("GMAIL_ROUTER_INBOX", "~/.openclaw/workspace/attachments")))
DONE_LABEL = "9 완료"          # 터미널 표식 (고정 규칙)
GTASK_LIST_NAMES = ("Brainify", "메일 후속")  # 우선순위 — 기존 list 재사용
MAX = 8                         # 라벨당 1회 드레인 상한 (멱등 — 잔여분 다음 사이클)
STAGING = pathlib.Path("/tmp/gmail-label-actions")
CLAUDE_MODEL = "claude-opus-4-7"

# 라벨 디스패치 테이블: (라벨명, 필수 액션 집합, terminal?). 고정 규칙.
#   액션은 항상 [task, schedule, reply] 순으로 실행(reply 가 마지막 → 선행 실패 시 고아 초안 0).
LABELS: list[tuple[str, tuple[str, ...], bool]] = [
    ("1 저장",           (),                            True),
    ("2 일정",           ("schedule",),                 True),
    ("3 일정+할일",      ("task", "schedule"),          True),
    ("6 할일",           ("task",),                     True),
    ("4 일정+회신",      ("schedule", "reply"),         False),
    ("5 일정+할일+회신", ("task", "schedule", "reply"), False),
    ("7 할일+회신",      ("task", "reply"),             False),
    ("8 회신",           ("reply",),                    False),
]
_ACTION_ORDER = ("task", "schedule", "reply")
_STATE: dict = {}               # gtasks_list_id 캐시 등 런타임 상태


# ── gog 래퍼 ───────────────────────────────────────────────────────────────
def gog_json(*args: str, timeout: int = 30, results_only: bool = True):
    """`gog ... --account ACCOUNT -j [--results-only]`. JSON 파싱, [] 빈출력, None 실패.
    results_only=False: 봉투(envelope) 보존 — `thread get --download` 시 thread+downloaded 둘 다 필요."""
    cmd = ["gog", *args, "--account", ACCOUNT, "-j"]
    if results_only:
        cmd.append("--results-only")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def gog_call(*args: str, timeout: int = 30) -> tuple[bool, str]:
    """변경(mutating) 명령용. (ok, msg). gog 가 exit0 으로 googleapi 에러 흘리는 케이스 방어."""
    cmd = ["gog", *args, "--account", ACCOUNT]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return (False, "timeout")
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        return (False, (err or out).strip())
    combined = f"{out}\n{err}".strip()
    if "googleapi: Error" in combined:
        return (False, combined)
    return (True, out)


# ── claude --print 바운디드 추출 (헤드리스·툴 비활성) ─────────────────────────
def claude_print(prompt: str, timeout: int = 180) -> str | None:
    """`claude --print` 1회 호출. stdout 텍스트 또는 None(미설치·실패·timeout).
    툴 전부 비활성 — 순수 텍스트→텍스트 추출 함수로만 사용."""
    cmd = ["claude", "--print", "--permission-mode", "bypassPermissions",
           "--model", CLAUDE_MODEL,
           "--disallowedTools", "Bash,Read,Edit,Write,Glob,Grep,Agent,WebFetch,WebSearch"]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip()


def _strip_fence(out: str) -> str:
    """선행 ```json 펜스 제거."""
    if out.startswith("```"):
        nl = out.find("\n")
        out = out[nl + 1:] if nl >= 0 else out[3:]
        if out.rstrip().endswith("```"):
            out = out.rstrip()[:-3]
    return out.strip()


def _loose_json(out: str):
    """엄격 파싱 실패 시 첫 { … 마지막 } 구간 재시도. 실패 None."""
    out = _strip_fence(out)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        try:
            return json.loads(out[out.index("{"): out.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError):
            return None


# ── 스레드 본문 파싱 (결정형) ───────────────────────────────────────────────
def _b64url(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except Exception:
        return ""


def _headers(payload: dict) -> dict:
    """payload.headers → {from,to,subject,date,reply-to} (소문자 키)."""
    h = {x.get("name", "").lower(): x.get("value", "") for x in payload.get("headers", [])}
    return {k: h.get(k, "") for k in ("from", "to", "subject", "date", "reply-to")}


def _find_text(payload: dict) -> str:
    """본문 추출: text/plain 우선, 없으면 text/html(raw). MIME 트리 재귀."""
    def walk(p: dict, want: str):
        if p.get("mimeType") == want and p.get("body", {}).get("data"):
            return _b64url(p["body"]["data"])
        for c in p.get("parts", []) or []:
            r = walk(c, want)
            if r:
                return r
        return None
    return walk(payload, "text/plain") or walk(payload, "text/html") or ""


def build_thread_md(thread: dict, tid: str) -> str:
    """스레드 전 메시지 → 마크다운. 프론트매터(멱등성 키) + 헤더 + 본문(시간순)."""
    msgs = sorted(thread.get("messages", []), key=lambda m: int(m.get("internalDate", "0") or 0))
    last_ts = max((int(m.get("internalDate", "0") or 0) for m in msgs), default=0)
    last_iso = datetime.datetime.fromtimestamp(last_ts / 1000).strftime("%Y-%m-%d %H:%M") if last_ts else ""
    top = _headers(msgs[-1].get("payload", {})) if msgs else {"from": "", "to": "", "subject": "", "date": ""}
    fm = (
        "---\n"
        f"gmail_thread_id: {tid}\n"
        f"message_count: {len(msgs)}\n"
        f"last_internal_date: \"{last_iso}\"\n"
        f"subject: {json.dumps(top['subject'], ensure_ascii=False)}\n"
        f"from: {json.dumps(top['from'], ensure_ascii=False)}\n"
        "---\n\n"
    )
    blocks = []
    for m in msgs:
        h = _headers(m.get("payload", {}))
        body = (_find_text(m.get("payload", {})) or m.get("snippet", "")).strip()
        blocks.append(
            f"### {h['date']}\n"
            f"**From:** {h['from']}  \n**To:** {h['to']}  \n**Subject:** {h['subject']}\n\n"
            f"{body}"
        )
    return fm + "\n\n---\n\n".join(blocks)


def thread_headers(thread: dict) -> dict:
    """스레드 최신 메시지 헤더 — 액션 추출용 (subject/from/reply-to)."""
    msgs = thread.get("messages", [])
    if not msgs:
        return {"from": "", "to": "", "subject": "", "date": "", "reply-to": ""}
    last = max(msgs, key=lambda m: int(m.get("internalDate", "0") or 0))
    return _headers(last.get("payload", {}))


def latest_msg_id(thread: dict) -> str:
    """스레드 최신 메시지의 *messageId* (회신 초안의 --reply-to-message-id 용 —
    threadId 가 아님: gog 는 message ID 를 요구, threadId 넘기면 404)."""
    msgs = thread.get("messages", [])
    if not msgs:
        return ""
    return max(msgs, key=lambda m: int(m.get("internalDate", "0") or 0)).get("id", "")


def _slug(s: str, maxlen: int = 50) -> str:
    s = re.sub(r"\s+", "-", (s or "").strip())
    s = re.sub(r'[\\/:*?"<>|]', "", s)
    return s.strip("-. ")[:maxlen] or "no-subject"


def thread_folder_name(thread: dict, tid: str) -> str:
    """스레드 → 결정형 폴더명 'YYYY-MM-DD_발신_제목'. staging 용 — brainify 가 정식 명명."""
    msgs = thread.get("messages", [])
    if not msgs:
        return tid
    last = max(msgs, key=lambda m: int(m.get("internalDate", "0") or 0))
    H = _headers(last.get("payload", {}))
    try:
        date = datetime.datetime.fromtimestamp(int(last.get("internalDate", "0") or 0) / 1000).strftime("%Y-%m-%d")
    except Exception:
        date = "0000-00-00"
    frm = H["from"]
    m = re.search(r"<([^>]+)>", frm)
    sender = (frm.split("<")[0].strip() or (m.group(1) if m else frm)) if frm else "unknown"
    return f"{date}_{_slug(sender, 24)}_{_slug(H['subject'])}"


# ── 캡처 헬퍼 ──────────────────────────────────────────────────────────────
def _label_query(label: str) -> str:
    """Gmail `q` 의 label: 형식. `+` 포함 라벨은 공백→하이픈 unquoted(`label:3-일정+할일`),
    그 외는 인용형(`label:"1 저장"`). (gog q 파서 실측 규칙)"""
    if "+" in label:
        return "label:" + label.replace(" ", "-")
    return f'label:"{label}"'


def search_threads(label: str):
    """라벨로 검색 → (고유 thread_id 목록, 디버그용 첫 항목). 실패 시 (None, None)."""
    res = gog_json("gmail", "search", _label_query(label), "--max", str(MAX))
    if res is None:
        return None, None
    items = res if isinstance(res, list) else (res.get("messages") or res.get("items") or [])
    tids: list[str] = []
    for m in items:
        if isinstance(m, dict):
            tid = m.get("threadId") or m.get("id")
            if tid and tid not in tids:
                tids.append(tid)
    return tids, (items[0] if items else None)


def extract_attachments(result, out_dir: pathlib.Path) -> list[tuple[pathlib.Path, str]]:
    """`thread get --download` 반환 → (실제경로, 저장이름) 목록."""
    objs = []
    if isinstance(result, list):
        objs = result
    elif isinstance(result, dict):
        d = result.get("downloaded")
        objs = d if isinstance(d, list) else (d.get("files") or [] if isinstance(d, dict) else [])
    out: list[tuple[pathlib.Path, str]] = []
    for a in objs:
        if isinstance(a, dict) and a.get("path"):
            p = pathlib.Path(a["path"])
            out.append((p, a.get("filename") or p.name))
    if not out and out_dir.exists():
        for c in out_dir.iterdir():
            if c.is_file() and not c.name.startswith("."):
                out.append((c, c.name))
    return out


def unique_dest(dst: pathlib.Path) -> pathlib.Path:
    if not dst.exists():
        return dst
    i = 1
    while (cand := dst.with_name(f"{dst.stem}_{i}{dst.suffix}")).exists():
        i += 1
    return cand


def _read_front(path: pathlib.Path) -> dict:
    out: dict = {}
    try:
        txt = path.read_text(encoding="utf-8")
    except Exception:
        return out
    if not txt.startswith("---"):
        return out
    end = txt.find("\n---", 3)
    if end == -1:
        return out
    for line in txt[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def find_existing_capture(tid: str) -> tuple:
    """INBOX 에서 이 threadId 기존 캡처 폴더 탐색. 반환 (폴더|None, message_count|None)."""
    if not INBOX.exists():
        return (None, None)
    for d in sorted(INBOX.iterdir()):
        if d.is_dir() and _read_front(d / "_thread.md").get("gmail_thread_id") == tid:
            mc = _read_front(d / "_thread.md").get("message_count", "")
            return (d, int(mc) if mc.isdigit() else None)
    return (None, None)


# ── 액션 멱등 사이드카 (_actions.json — 캡처와 독립, 덮어쓰기에 안전) ──────────
def _read_actions(folder: pathlib.Path) -> dict:
    try:
        return json.loads((folder / "_actions.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_action(folder: pathlib.Path, key: str, value: str) -> None:
    d = _read_actions(folder)
    d[key] = value
    (folder / "_actions.json").write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def process_thread(tid: str) -> tuple[bool, str, list[str], pathlib.Path | None, dict]:
    """스레드를 INBOX 폴더에 캡처: _thread.md(본문) + 첨부 원본.
    멱등(threadId+message_count): 동일=재기록 skip / 변동=폴더 덮어쓰기 / 없음=신규.
    반환 (ok, msg, 저장목록, 폴더, thread). thread 는 액션 추출에 재사용."""
    staging = STAGING / tid
    staging.mkdir(parents=True, exist_ok=True)
    result = gog_json("gmail", "thread", "get", tid, "--full",
                      "--download", "--out-dir", str(staging), timeout=60, results_only=False)
    if result is None:
        return (False, "thread get 실패", [], None, {})
    thread = result.get("thread", {}) if isinstance(result, dict) else {}
    cur_mc = len(thread.get("messages", []))
    existing, prev_mc = find_existing_capture(tid)

    if existing is not None and prev_mc is not None and prev_mc == cur_mc:
        files = [p.name for p in existing.iterdir() if p.is_file()]
        return (True, "멱등 skip (동일)", files, existing, thread)

    if existing is not None:
        for c in existing.iterdir():
            if c.is_file() and c.name != "_actions.json":   # 액션 마커는 보존
                c.unlink()
        dest_dir = existing
    else:
        dest_dir = INBOX / thread_folder_name(thread, tid)
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    (dest_dir / "_thread.md").write_text(build_thread_md(thread, tid), encoding="utf-8")
    saved.append("_thread.md")
    for src, name in extract_attachments(result, staging):
        if src.exists():
            dst = unique_dest(dest_dir / name)
            shutil.copy2(src, dst)
            saved.append(dst.name)
    return (True, "갱신(덮어쓰기)" if existing is not None else "ok", saved, dest_dir, thread)


# ── 추출 (claude --print) ───────────────────────────────────────────────────
def _thread_text(folder: pathlib.Path) -> str:
    try:
        return (folder / "_thread.md").read_text(encoding="utf-8")[:6000]
    except Exception:
        return ""


def extract_schedule(folder: pathlib.Path, H: dict, now: datetime.datetime) -> dict | None:
    """메일에서 캘린더 이벤트 1개 추출(Opus). 명확한 시작 일시 없으면 None."""
    prompt = (
        f"오늘 날짜: {now.date().isoformat()} (KST, +09:00)\n\n"
        "다음 메일에서 캘린더 이벤트 1개를 추출하라. 명확한 시작 일시가 있어야 함.\n"
        "- 시작·종료 모두 시간 포함이면 RFC3339 (예: '2026-05-15T14:00:00+09:00').\n"
        "- 시작만 있고 종료 없음 → 종료 = 시작 + 1시간.\n"
        "- 시간 없이 날짜만 → all_day=true, start/end='YYYY-MM-DD'.\n"
        "- 명확한 일시 없거나 모호하면 {\"event\": null}.\n"
        "- summary 는 한국어 한 줄. location 은 본문에 명시된 경우만.\n"
        "응답은 JSON 객체 한 줄만. 코드블록·해설 금지.\n"
        '예: {"summary":"학회 이사회","start":"2026-05-15T14:00:00+09:00",'
        '"end":"2026-05-15T16:00:00+09:00","location":"서울대병원","all_day":false}\n\n'
        f"[제목] {H.get('subject','')}\n[발신] {H.get('from','')}\n\n[메일]\n{_thread_text(folder)}\n"
    )
    out = claude_print(prompt, timeout=120)
    if not out:
        return None
    p = _loose_json(out)
    if not isinstance(p, dict) or p.get("event") is None and "start" not in p:
        return None
    start = p.get("start")
    if not isinstance(start, str) or not start.strip():
        return None
    return {"summary": (p.get("summary") or H.get("subject") or "(제목 없음)").strip(),
            "start": start.strip(), "end": (p.get("end") or start).strip(),
            "location": (p.get("location") or "").strip(), "all_day": bool(p.get("all_day"))}


def extract_task(folder: pathlib.Path, H: dict, now: datetime.datetime) -> dict:
    """할 일 1개 추출(Opus). 추출 실패해도 제목 fallback 으로 항상 dict 반환
    (`6 할일` = 이미 'task' 로 분류된 입력이라 비대칭 — 항상 1건 생성)."""
    fallback = {"title": (H.get("subject") or "메일 후속").strip()[:120], "due": None}
    prompt = (
        f"오늘 날짜: {now.date().isoformat()} (KST)\n\n"
        "다음 메일에서 사용자(계정 주인)가 해야 할 일(action) 1개를 추출하라.\n"
        "- title: 한국어 한 줄 동사형 (예: '학회 등록비 송금').\n"
        "- due: 마감이 명확하면 'YYYY-MM-DD', 없으면 null.\n"
        "응답은 JSON 한 줄만. 코드블록·해설 금지.\n"
        '예: {"title":"의학위원회 회의자료 검토","due":"2026-05-20"}\n\n'
        f"[제목] {H.get('subject','')}\n[발신] {H.get('from','')}\n\n[메일]\n{_thread_text(folder)}\n"
    )
    out = claude_print(prompt, timeout=120)
    p = _loose_json(out) if out else None
    if isinstance(p, dict) and (p.get("title") or "").strip():
        due = p.get("due")
        return {"title": p["title"].strip()[:120],
                "due": due.strip() if isinstance(due, str) and due.strip() else None}
    return fallback


def draft_reply(folder: pathlib.Path, H: dict) -> tuple[str, bool] | None:
    """회신 초안 본문 추출(Opus). 반환 (body, review_needed) 또는 None(실패).
    STATUS: ok|review 첫 줄 프로토콜 — 본문엔 미포함, 누락 시 보수적 review."""
    prompt = (
        "다음 Gmail 메일에 대한 한국어 회신 초안을 작성하라.\n"
        "사용자(계정 주인)가 보낼 회신이며 정중한 존댓말. 무인 자동 초안 — 사용자가 검토 후 직접 발송한다.\n"
        "사실관계를 임의 단정하지 말 것.\n\n"
        "출력 형식(엄격):\n"
        "- 첫 줄에 정확히 `STATUS: ok` 또는 `STATUS: review`.\n"
        "  · ok = 의도·정보 명확, 그대로 보낼 만함.  · review = 불분명/가정 과다 → 본문은 확인 요청 형태.\n"
        "- 둘째 줄부터 회신 본문만 (인사·서명 가능, 코드블록·메타설명 금지).\n\n"
        f"[원본 from: {H.get('from','')}]\n[subject: {H.get('subject','')}]\n\n[원본 본문]\n{_thread_text(folder)}\n"
    )
    out = claude_print(prompt, timeout=180)
    if not out:
        return None
    out = _strip_fence(out)
    review = True
    lines = out.split("\n", 1)
    if lines and lines[0].strip().upper().startswith("STATUS:"):
        review = lines[0].split(":", 1)[1].strip().lower() != "ok"
        out = lines[1].strip() if len(lines) > 1 else ""
    return (out.strip(), review) if out.strip() else None


# ── GWS 액션 (gog) ──────────────────────────────────────────────────────────
def ensure_task_list() -> str | None:
    """Tasks list id 보장(캐시 → 기존 이름 매칭 → 'Brainify' 신규). 실패 None."""
    if _STATE.get("gtasks_list_id"):
        return _STATE["gtasks_list_id"]
    lists = gog_json("tasks", "lists", "list")
    if lists is None:
        return None
    for L in (lists.get("items", []) if isinstance(lists, dict) else lists) or []:
        if isinstance(L, dict) and L.get("title") in GTASK_LIST_NAMES:
            _STATE["gtasks_list_id"] = L.get("id")
            return _STATE["gtasks_list_id"]
    out = gog_json("tasks", "lists", "create", GTASK_LIST_NAMES[0])
    if isinstance(out, dict) and out.get("id"):
        _STATE["gtasks_list_id"] = out["id"]
        return out["id"]
    return None


def create_task(title: str, notes: str, due: str | None) -> str | None:
    list_id = ensure_task_list()
    if not list_id:
        return None
    args = ["tasks", "add", list_id, "--title", title, "--notes", notes]
    if due:
        args += ["--due", due]
    out = gog_json(*args)
    return out["id"] if isinstance(out, dict) and out.get("id") else None


def create_calendar_event(ev: dict, tid: str) -> str | None:
    """primary 캘린더에 이벤트 생성 → event_id. all-day 종료는 +1(Google EXCLUSIVE 보정)."""
    start, end = ev["start"], (ev.get("end") or ev["start"])
    if ev.get("all_day"):
        try:
            end = (datetime.date.fromisoformat(end[:10]) + datetime.timedelta(days=1)).isoformat()
        except (ValueError, TypeError):
            pass
    desc = f"Gmail thread: https://mail.google.com/mail/u/0/#all/{tid}"
    args = ["calendar", "create", "primary", "--summary", ev.get("summary") or "(제목 없음)",
            "--from", start, "--to", end, "--description", desc]
    if ev.get("location"):
        args += ["--location", ev["location"]]
    if ev.get("all_day"):
        args.append("--all-day")
    out = gog_json(*args)
    return out["id"] if isinstance(out, dict) and out.get("id") else None


def create_draft(reply_msg_id: str, reply_to: str, subject: str, body: str) -> str | None:
    """Gmail 임시보관함에 회신 초안 생성 → draftId (스레드 in-reply).
    reply_msg_id = 회신 대상 *messageId* (threadId 아님 — gog 가 message ID 요구)."""
    re_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    fd, body_file = tempfile.mkstemp(prefix=".gla-reply.", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        out = gog_json("gmail", "drafts", "create", "--to", reply_to,
                       "--subject", re_subject, "--body-file", body_file,
                       "--reply-to-message-id", reply_msg_id)
    finally:
        try:
            os.unlink(body_file)
        except OSError:
            pass
    if not isinstance(out, dict):
        return None
    return out.get("draftId") or (out.get("message") or {}).get("id") or out.get("id") or None


# ── 라벨별 액션 디스패치 ─────────────────────────────────────────────────────
def run_actions(actions: tuple[str, ...], folder: pathlib.Path, thread: dict,
                tid: str, now: datetime.datetime) -> tuple[bool, list[str], bool]:
    """필수 액션을 [task, schedule, reply] 순으로 실행 (멱등 — _actions.json 마커).
    반환 (all_ok, 메시지목록, reply_pending). 선행 실패 시 reply 전 bail(고아 초안 0)."""
    H = thread_headers(thread)
    done = _read_actions(folder)
    notes: list[str] = []
    reply_pending = False

    for act in _ACTION_ORDER:
        if act not in actions:
            continue

        if act == "task":
            if done.get("google_task_id"):
                notes.append("task=멱등skip")
                continue
            te = extract_task(folder, H, now)
            tid_task = create_task(te["title"], f"Gmail: {tid}", te.get("due"))
            if not tid_task:
                return (False, notes + ["task 생성 실패"], reply_pending)
            _write_action(folder, "google_task_id", tid_task)
            notes.append(f"task✓({te['title'][:20]})")

        elif act == "schedule":
            if done.get("calendar_event_id"):
                notes.append("일정=멱등skip")
                continue
            ev = extract_schedule(folder, H, now)
            if not ev:                       # 일시 추출 실패 → 필수인데 실패 → bail
                return (False, notes + ["일시 추출 실패(라벨 유지·재시도)"], reply_pending)
            eid = create_calendar_event(ev, tid)
            if not eid:
                return (False, notes + ["calendar 생성 실패"], reply_pending)
            _write_action(folder, "calendar_event_id", eid)
            notes.append(f"일정✓({ev['summary'][:20]})")

        elif act == "reply":
            if done.get("gmail_draft_id"):
                notes.append("초안=멱등skip")
                reply_pending = True
                continue
            res = draft_reply(folder, H)
            if not res:
                return (False, notes + ["회신 초안 생성 실패"], reply_pending)
            body, review = res
            rmid = latest_msg_id(thread) or tid
            did = create_draft(rmid, H.get("reply-to") or H.get("from", ""), H.get("subject", ""), body)
            if not did:
                return (False, notes + ["drafts create 실패"], reply_pending)
            _write_action(folder, "gmail_draft_id", did)
            _write_action(folder, "reply_status", "review" if review else "draft")
            notes.append("초안✓" + ("[검토필요]" if review else ""))
            reply_pending = True

    return (True, notes, reply_pending)


# ── main ──────────────────────────────────────────────────────────────────
def drain_label(label: str, actions: tuple[str, ...], terminal: bool,
                now: datetime.datetime, dry: bool) -> tuple[int, int, int]:
    """한 라벨 드레인 → (ok, skip, pending)."""
    tids, _ = search_threads(label)
    if tids is None:
        print(f"  ✋ '{label}' 검색 실패 (인증·네트워크·라벨명).")
        return (0, 0, 0)
    if dry:
        print(f"  · '{label}'  대상 {len(tids)}건  액션={actions or '없음'}  {'terminal' if terminal else '회신(유지)'}")
        return (0, 0, 0)
    if not tids:
        return (0, 0, 0)

    n_ok = n_skip = n_pend = 0
    for tid in tids:
        ok, msg, saved, folder, thread = process_thread(tid)
        if not ok or folder is None:
            n_skip += 1
            print(f"  ↷ [{label}] {tid} 캡처 실패 — {msg} (라벨 유지)")
            continue

        aok, anotes, pending = run_actions(actions, folder, thread, tid, now)
        suffix = f"  [{' '.join(anotes)}]" if anotes else ""
        if not aok:
            n_skip += 1
            print(f"  ⚠️ [{label}] {tid} 액션 미완 — 라벨 유지{suffix}")
            continue

        if terminal:
            rok, rerr = gog_call("gmail", "labels", "modify", tid, "--add", DONE_LABEL, "--remove", label)
            if not rok:
                n_skip += 1
                print(f"  ⚠️ [{label}] {tid} 라벨변경 실패: {rerr}{suffix}")
                continue
            n_ok += 1
            print(f"  ✅ [{label}] {tid} {msg} → '{DONE_LABEL}'  ({', '.join(saved)}){suffix}")
        else:
            n_pend += 1 if pending else 0
            n_ok += 1
            print(f"  ✍️ [{label}] {tid} {msg} (라벨 유지·초안 검토대기)  ({', '.join(saved)}){suffix}")
    return (n_ok, n_skip, n_pend)


def main() -> None:
    if not ACCOUNT:
        print("✋ 환경변수 GMAIL_ROUTER_ACCOUNT 가 비어 있습니다.\n"
              "   예: GMAIL_ROUTER_ACCOUNT=you@gmail.com python3 run.py")
        sys.exit(2)

    dry = "--dry-run" in sys.argv
    only = None
    if "--label" in sys.argv:
        i = sys.argv.index("--label")
        only = sys.argv[i + 1] if i + 1 < len(sys.argv) else None

    now = datetime.datetime.now()
    print(f"▶ gmail-label-actions  계정={ACCOUNT}")
    print(f"  저장 대상: {INBOX}")
    STAGING.mkdir(parents=True, exist_ok=True)

    tot_ok = tot_skip = tot_pend = 0
    for label, actions, terminal in LABELS:
        if only and label != only:
            continue
        ok, skip, pend = drain_label(label, actions, terminal, now, dry)
        tot_ok += ok
        tot_skip += skip
        tot_pend += pend

    if not dry:
        tail = f" / 초안 검토대기 {tot_pend}" if tot_pend else ""
        print(f"🎉 완료: 처리 {tot_ok} / 건너뜀 {tot_skip}{tail}")


if __name__ == "__main__":
    main()
