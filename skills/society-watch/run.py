"""society-watch — 학회 자료실 polling → 신규 첨부 다운 → 2nd-brain inbox 드롭 + Telegram 알림.

사용법:
    cd ~/.openclaw/workspace/skills/society-watch && uv run run.py <society>             # cron headless
    cd ~/.openclaw/workspace/skills/society-watch && uv run run.py <society> --bootstrap # 1회 헤드 모드 검증

자격증명 (ID/PW, 옵션 OTP) 은 단일 chmod 600 toml 에 통합. 평문 미노출.
Telegram 알림은 stdout 으로 출력 → openclaw stdout-Telegram hook 이 전송.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pyotp
from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

KST = timezone(timedelta(hours=9))

HOME = Path.home()
SECRETS_DIR = HOME / ".openclaw" / "secrets"
PROFILE_ROOT = HOME / ".openclaw" / "skills" / "society-watch" / "chrome-profile"
STATE_PATH = HOME / ".openclaw" / "agents" / "main" / "memory" / "society-watch.json"
VAULT_ROOT = HOME / "projects" / "2nd-brain-vault"
VAULT_SOURCES = VAULT_ROOT / "sources"
VAULT_KNOWLEDGE = VAULT_ROOT / "knowledge"
VAULT_INBOX = VAULT_SOURCES / "00_inbox"  # legacy fallback (para_path 미설정 시)
TMP_DOWNLOAD_ROOT = Path("/tmp/society-watch")

FIRST_RUN_LIMIT = 5  # SKILL.md §Step 3: 첫 호출은 최근 5건만
POLL_LIMIT = 20      # 한 회차 최대 처리. delta 운영 시 사실상 무관, 안전망.

log = logging.getLogger("society-watch")


@dataclass(frozen=True)
class SocietyConfig:
    key: str           # ksnm-insur
    society_label: str # KSNM
    society_full: str  # 대한핵의학회
    board_label: str   # 보험관련
    listing_url: str
    detail_url_tpl: str  # contains {post_id}
    login_url: str
    secret_path: Path
    profile_key: str   # 같은 학회 내 보드끼리 세션 공유 — ksnm-* 들이 ksnm 프로파일 공유
    para_path: str = ""  # vault-root 기준 상대 경로 (예: "02_areas/대한핵의학회/보험관련").
                         # 비어있으면 sources/00_inbox 로만 떨어뜨리고 동반 노트는 안 만듦 (legacy).
    tags: tuple[str, ...] = ()  # 동반 노트 frontmatter 의 tags
    selectors: dict[str, str] = field(default_factory=dict)


KSNM_BASE = "https://www.ksnm.or.kr"
KSNM_LOGIN_URL = f"{KSNM_BASE}/member/?url=%2Fbbs%2Findex.html%3Fcode%3D{{board}}"
KSNM_LISTING_URL = f"{KSNM_BASE}/bbs/?code={{board}}"
KSNM_DETAIL_URL = f"{KSNM_BASE}/bbs/index.html?code={{board}}&number={{post_id}}&mode=view"
KSNM_LISTING_HREF_RE = re.compile(r"number=(\d+)[^>]*mode=view")


def _ksnm_society(
    key: str,
    board_code: str,
    board_label: str,
    para_path: str = "",
    extra_tags: tuple[str, ...] = (),
) -> SocietyConfig:
    return SocietyConfig(
        key=key,
        society_label="KSNM",
        society_full="대한핵의학회",
        board_label=board_label,
        listing_url=KSNM_LISTING_URL.format(board=board_code),
        detail_url_tpl=KSNM_DETAIL_URL.format(board=board_code, post_id="{post_id}"),
        login_url=KSNM_LOGIN_URL.format(board=board_code),
        secret_path=SECRETS_DIR / "society-watch-ksnm.toml",
        profile_key="ksnm",
        para_path=para_path,
        tags=("ksnm", board_label, "society-watch", *extra_tags),
        selectors={
            # login form (확정 from /member/?url=... HTML inspection 2026-05-11)
            "login_id": "#id",
            "login_pw": "#passwd",
            "login_submit": '#login_frm input[type="image"]',
            # OTP 는 KSNM 에 없음 — toml 에 otp_secret 있을 때만 try, 없으면 skip
            "otp_input": 'input[name="otp"]',  # 가설; 실제 발생 시 검증 필요
            "otp_submit": "",
            # post-login marker — bbs listing 에 도달했는지. <table> 의 게시글 row.
            # KSNM bbs listing: 제목 link 가 number= 포함하는 href
            "logged_in_marker": 'a[href*="mode=view"]',
            "login_form_marker": "#login_frm",
            # listing row 안에 "number=N&...&mode=view" 들어있는 link 들
            "listing_post_link": 'a[href*="mode=view"]',
            # detail 페이지 첨부 — KSNM 구조: <tr><th class="th">첨부파일N</th><td><a href="...download.php?code=...">...</a></td></tr>
            # 사이드바·풋터에 동일 download.php URL 이 있어 URL 만으로는 부족 → "첨부파일" 라벨 행 한정.
            "detail_attachment_link": 'tr:has(th.th:has-text("첨부파일")) a[href*="download.php"]',
        },
    )


SOCIETIES: dict[str, SocietyConfig] = {
    "ksnm-insur": _ksnm_society(
        "ksnm-insur", "insur", "보험관련",
        para_path="02_areas/대한핵의학회/보험관련",
    ),
    # 확장 시 — para_path 비워두면 sources/00_inbox 로만 떨어뜨림 (수동 분류).
    # "ksnm-general": _ksnm_society("ksnm-general", "general", "행사 및 기타자료"),
    # "ksnm-gonggo":  _ksnm_society("ksnm-gonggo",  "gonggo",  "정도관리"),
}


@dataclass
class NewPost:
    post_id: int
    title: str = ""
    posted_date: str = ""           # YYYY-MM-DD
    attachments: list[Path] = field(default_factory=list)  # final 절대 경로 (PARA sources/ 또는 inbox/)
    body_md_path: Path | None = None  # 첨부 0인 경우의 markdown 경로
    note_path: Path | None = None     # 동반 노트 (knowledge/<para>/...) — para_path 있을 때만
    error: str | None = None


@dataclass
class Outcome:
    new_posts: list[NewPost] = field(default_factory=list)
    failures: list[NewPost] = field(default_factory=list)
    last_post_id_after: int | None = None
    error: str | None = None       # session_expired / auth_failed / process_failed:...


# ---- state -----------------------------------------------------------------


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open("rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("society-watch.json corrupt — resetting to empty")
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".society-watch.json.", dir=STATE_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---- secrets ---------------------------------------------------------------


def load_secret(society: SocietyConfig) -> dict[str, Any]:
    """society 의 secret toml 의 해당 섹션만 read.

    파일 구조: `[<SOCIETY-OR-PROFILE>]` 섹션(대소문자 무관) 안의 키들. 평탄 구조 fallback.
    호출자는 사용 직후 dict.clear() 로 GC 유도. 절대 log/stdout 으로 흘리지 ✗.
    """
    raw = tomllib.loads(society.secret_path.read_text(encoding="utf-8"))
    candidates = (society.key.lower(), society.profile_key.lower())
    for key, value in raw.items():
        if key.lower() in candidates and isinstance(value, dict):
            return value
    return raw


def totp_code_from_secret(secret: dict[str, Any]) -> str:
    return pyotp.TOTP(
        secret["otp_secret"],
        digits=int(secret.get("otp_digits", 6)),
        interval=int(secret.get("otp_period", 30)),
    ).now()


# ---- browser ---------------------------------------------------------------


def open_context(pw: Playwright, society: SocietyConfig, headless: bool) -> BrowserContext:
    profile_dir = PROFILE_ROOT / society.profile_key
    profile_dir.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        accept_downloads=True,
        viewport={"width": 1280, "height": 900},
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )


def is_logged_in(page: Page, society: SocietyConfig) -> bool:
    """listing url 이동 후, login form 으로 redirect 됐는지 / 게시글 link 가 보이는지로 판정."""
    sel = society.selectors
    try:
        page.wait_for_selector(sel["logged_in_marker"], timeout=5_000)
        return True
    except PlaywrightTimeoutError:
        return False


def perform_login(page: Page, society: SocietyConfig) -> bool:
    """toml 기반 ID/PW 직접 fill + 옵션 TOTP. 성공 시 listing 에 도달."""
    sel = society.selectors

    try:
        page.wait_for_selector(sel["login_form_marker"], timeout=10_000)
    except PlaywrightTimeoutError:
        return False

    secret = load_secret(society)
    try:
        try:
            page.fill(sel["login_id"], secret["login_id"], timeout=5_000)
            page.fill(sel["login_pw"], secret["login_pw"], timeout=5_000)
        except (PlaywrightTimeoutError, KeyError) as e:
            log.error("login fill 실패: %s", type(e).__name__)
            return False

        page.click(sel["login_submit"])

        # 옵션 OTP — secret 에 otp_secret 있을 때만 시도. 없으면 바로 listing 도착 기대.
        if "otp_secret" in secret:
            try:
                page.wait_for_selector(sel["otp_input"], timeout=10_000)
                code = totp_code_from_secret(secret)
                page.fill(sel["otp_input"], code)
                del code
                if sel.get("otp_submit"):
                    page.click(sel["otp_submit"])
                else:
                    page.press(sel["otp_input"], "Enter")
            except PlaywrightTimeoutError:
                # OTP prompt 가 안 떴다 — KSNM 이 OTP 안 쓰는 경우. 정상 통과로 간주.
                pass

        # 로그인 후 listing 으로 redirect 됐는지. login form 의 hidden url 이 동작해야 함.
        try:
            page.wait_for_selector(sel["logged_in_marker"], timeout=15_000)
            return True
        except PlaywrightTimeoutError:
            return False
    finally:
        secret.clear()


# ---- listing & detail parsing ---------------------------------------------


def _extract_post_ids_from_listing(page: Page) -> list[int]:
    """listing 의 모든 'mode=view' link 에서 number= 정수 추출 (중복 제거, 입력 순서 보존)."""
    hrefs = page.locator('a[href*="mode=view"]').evaluate_all(
        "els => els.map(e => e.getAttribute('href'))"
    )
    ids: list[int] = []
    seen: set[int] = set()
    for h in hrefs:
        if not h:
            continue
        m = KSNM_LISTING_HREF_RE.search(h)
        if not m:
            continue
        pid = int(m.group(1))
        if pid not in seen:
            seen.add(pid)
            ids.append(pid)
    return ids


def _slugify_title(title: str, max_len: int = 50) -> str:
    """SKILL.md §Step 5 규칙: 한글·영문·숫자 보존, 공백 → _, 위험문자 제거, leading/trailing dot 제거,
    UTF-8 문자 단위 길이 클램프."""
    if not title:
        return "no-title"
    s = re.sub(r'[\\/:*?"<>|]', "", title)
    s = re.sub(r"\s+", "_", s.strip())
    s = s.strip(".")
    if len(s) > max_len:
        s = s[:max_len]
    return s or "no-title"


def _safe_filename(name: str) -> str:
    """원파일명에서 fs 위험문자만 제거. 한글·공백 보존 (vault 규약은 inbox 단계에선 원파일명 허용)."""
    s = re.sub(r'[\\/:*?"<>|]', "_", name)
    s = s.strip(". ")
    return s or "unnamed"


def _final_filename(date: str, society_key: str, title_slug: str, original: str | None) -> str:
    """SKILL.md §Step 5 의 정규화 파일명. attachment 의 경우 original 포함, body md 의 경우 None."""
    base = f"{date}_{society_key}_{title_slug}"
    if original is None:
        return f"{base}.md"
    safe_orig = _safe_filename(original)
    stem, dot, ext = safe_orig.rpartition(".")
    if not dot:
        return f"{base}_{safe_orig}"
    return f"{base}_{stem}.{ext}"


_DATE_RE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")


def _extract_posted_date(text: str) -> str:
    m = _DATE_RE.search(text)
    if not m:
        return datetime.now(KST).strftime("%Y-%m-%d")
    y, mo, d = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def _process_one_post(
    page: Page, society: SocietyConfig, post_id: int, tmp_dir: Path
) -> NewPost:
    """detail 페이지 fetch → 첨부 다운 (또는 본문 markdown 추출) → 정규화 파일명으로 vault inbox 이동."""
    sel = society.selectors
    np = NewPost(post_id=post_id)

    detail_url = society.detail_url_tpl.format(post_id=post_id)
    try:
        page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError as e:
        np.error = f"detail_goto_timeout"
        return np

    # 세션 만료 재검증 — detail 도 redirect 될 수 있음
    try:
        if page.locator(sel["login_form_marker"]).count() > 0:
            np.error = "session_expired"
            return np
    except Exception:
        pass

    # 제목 추출 — KSNM detail page 의 흔한 패턴: <td>제 목</td><td>...</td> 또는 <h*>
    # 가장 견고: page.title() 에 글 제목 포함되는 경우 많음. fallback: 본문 cell.
    page_title = page.title() or ""
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=5_000)
    except PlaywrightTimeoutError:
        body_text = ""

    np.title = _extract_title(page_title, body_text)
    np.posted_date = _extract_posted_date(body_text)

    # 첨부 link 수집 — society 별 selector. KSNM 은 "첨부파일N" 라벨 행 안의 download.php a 만.
    attach_anchors = page.locator(sel["detail_attachment_link"])
    n_attach = attach_anchors.count()

    # 출력 경로 결정 — para_path 있으면 PARA sources/, 없으면 inbox legacy.
    sources_dir = (VAULT_SOURCES / society.para_path) if society.para_path else VAULT_INBOX
    knowledge_dir = (VAULT_KNOWLEDGE / society.para_path) if society.para_path else None

    if n_attach == 0:
        # Case B — 본문 markdown 저장 (이건 그 자체가 노트 역할)
        md_text = _build_body_markdown(society, post_id, np, body_text)
        out_name = _final_filename(np.posted_date, society.key, _slugify_title(np.title), None)
        # body markdown 은 knowledge 영역으로 (문서가 아니라 노트이므로)
        body_dir = knowledge_dir if knowledge_dir is not None else VAULT_INBOX
        body_dir.mkdir(parents=True, exist_ok=True)
        try:
            dst = _unique_dest(body_dir / out_name)
            dst.write_text(md_text, encoding="utf-8")
            np.body_md_path = dst
            np.note_path = dst  # 동반 노트와 동일 (첨부 0이라 sources 없음)
        except OSError as e:
            np.error = f"io_failed:body_write:{e}"
        return np

    # Case A — 첨부 다운
    title_slug = _slugify_title(np.title)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    seen_basenames: dict[str, int] = {}

    for i in range(n_attach):
        a = attach_anchors.nth(i)
        href = a.get_attribute("href") or ""
        if not href:
            continue
        # download trigger — Playwright 의 expect_download 컨텍스트
        try:
            with page.expect_download(timeout=30_000) as dl_info:
                a.click()
            dl = dl_info.value
            suggested = dl.suggested_filename or f"attachment_{i+1}"
            tmp_path = tmp_dir / _safe_filename(suggested)
            dl.save_as(str(tmp_path))
        except PlaywrightTimeoutError:
            np.error = (np.error + ";" if np.error else "") + f"download_timeout:#{i+1}"
            continue
        except Exception as e:
            np.error = (np.error + ";" if np.error else "") + f"download_failed:#{i+1}:{type(e).__name__}"
            continue

        # 정규화 파일명 + 충돌 회피
        out_name = _final_filename(np.posted_date, society.key, title_slug, suggested)
        # 같은 게시글 내 동일 출력명 → _2, _3 접미
        if out_name in seen_basenames:
            seen_basenames[out_name] += 1
            stem, dot, ext = out_name.rpartition(".")
            out_name = (
                f"{stem}_{seen_basenames[out_name]}.{ext}" if dot else f"{out_name}_{seen_basenames[out_name]}"
            )
        else:
            seen_basenames[out_name] = 1

        sources_dir.mkdir(parents=True, exist_ok=True)
        dst = _unique_dest(sources_dir / out_name)
        try:
            os.replace(tmp_path, dst)
            np.attachments.append(dst)
        except OSError as e:
            np.error = (np.error + ";" if np.error else "") + f"io_failed:mv:{e}"
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    # 동반 노트 작성 — para_path 가 있고 첨부 1개 이상 성공 시
    if knowledge_dir is not None and np.attachments:
        try:
            np.note_path = _write_companion_note(society, post_id, np, knowledge_dir, title_slug)
        except OSError as e:
            np.error = (np.error + ";" if np.error else "") + f"note_write_failed:{e}"

    return np


_TITLE_PREFIX = ":::: 대한핵의학회 ::::"


def _extract_title(page_title: str, body_text: str) -> str:
    # KSNM page <title>: ":::: 대한핵의학회 :::: 보험관련 > 제목" 형태 흔함
    t = page_title or ""
    if _TITLE_PREFIX in t:
        t = t.split(_TITLE_PREFIX, 1)[1]
    if ">" in t:
        t = t.rsplit(">", 1)[1]
    t = t.strip()
    if t and t.lower() not in ("home", "로그인"):
        return t
    # fallback: body 에서 "제 목 :" 패턴
    m = re.search(r"제\s*목[:\s]+(.+)", body_text)
    if m:
        return m.group(1).split("\n", 1)[0].strip()
    return ""


def _build_body_markdown(society: SocietyConfig, post_id: int, np: NewPost, body_text: str) -> str:
    """첨부 0인 경우의 본문 markdown — vault frontmatter 표준 따름. 본문이 곧 노트."""
    fm = _build_frontmatter(society, post_id, np, sources_rel=[])
    detail_url = society.detail_url_tpl.format(post_id=post_id)
    return (
        f"{fm}\n"
        f"# {np.title or '(제목 없음)'}\n\n"
        f"- 학회: {society.society_full} ({society.society_label})\n"
        f"- 보드: {society.board_label}\n"
        f"- 게시번호: {post_id}\n"
        f"- 등록일: {np.posted_date}\n"
        f"- 원본 URL: {detail_url}\n\n"
        f"---\n\n"
        f"{body_text.strip()}\n"
    )


def _build_frontmatter(society: SocietyConfig, post_id: int, np: NewPost, sources_rel: list[str]) -> str:
    """vault CLAUDE.md §동반 노트 프론트매터 표준 (2026-05-01 이후) — sources 필드는 vault-root 상대 경로."""
    detail_url = society.detail_url_tpl.format(post_id=post_id)
    title_safe = (np.title or "(제목 없음)").replace('"', "'")
    tags_yaml = "[" + ", ".join(society.tags) + "]"
    src_yaml = "[]" if not sources_rel else ("\n" + "\n".join(f"  - {s}" for s in sources_rel))
    src_field = f"sources: {src_yaml}" if isinstance(src_yaml, str) and src_yaml.startswith("[") else f"sources:{src_yaml}"
    return (
        "---\n"
        f'title: "{title_safe}"\n'
        f"source: {society.society_full} {society.board_label} 보드 ({detail_url})\n"
        f"date: {np.posted_date}\n"
        f"tags: {tags_yaml}\n"
        f"{src_field}\n"
        "---\n"
    )


def _write_companion_note(
    society: SocietyConfig,
    post_id: int,
    np: NewPost,
    knowledge_dir: Path,
    title_slug: str,
) -> Path:
    """동반 노트 (knowledge/<para>/<note>.md) 작성. 첨부 sources 들을 frontmatter + 본문 첫 줄에 link."""
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    note_name = f"{np.posted_date}_{society.key}_{title_slug}.md"
    note_path = _unique_dest(knowledge_dir / note_name)

    sources_rel = [str(p.relative_to(VAULT_ROOT)) for p in np.attachments]
    fm = _build_frontmatter(society, post_id, np, sources_rel)
    detail_url = society.detail_url_tpl.format(post_id=post_id)

    body_lines = [fm, ""]
    for p, rel in zip(np.attachments, sources_rel):
        body_lines.append(f"- [원본: {p.name}]({rel})")
    body_lines.append("")
    body_lines.append(f"# {np.title or '(제목 없음)'}\n")
    body_lines.append(f"- 학회: {society.society_full} ({society.society_label})")
    body_lines.append(f"- 보드: {society.board_label}")
    body_lines.append(f"- 게시번호: {post_id}")
    body_lines.append(f"- 등록일: {np.posted_date}")
    body_lines.append(f"- 원본 URL: {detail_url}")
    body_lines.append("")
    body_lines.append("## 요약")
    body_lines.append("")
    body_lines.append("(society-watch 자동 생성 stub. 사람·LLM 보강 필요.)")
    body_lines.append("")
    body_lines.append("## 내 생각")
    body_lines.append("")
    body_lines.append("## 관련 노트")
    body_lines.append("")

    note_path.write_text("\n".join(body_lines), encoding="utf-8")
    return note_path


def _unique_dest(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem = dst.stem
    suffix = dst.suffix
    parent = dst.parent
    i = 2
    while True:
        cand = parent / f"{stem}_{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


# ---- main flow -------------------------------------------------------------


def cron_run(society: SocietyConfig) -> Outcome:
    state = load_state()
    last_post_id = int(state.get(society.key, {}).get("last_post_id", 0))
    outcome = Outcome()

    with sync_playwright() as pw:
        ctx = open_context(pw, society, headless=True)
        page = ctx.new_page()

        # listing 으로 직진 — 미인증 상태면 login form 으로 redirect 됨
        try:
            page.goto(society.listing_url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeoutError:
            outcome.error = "listing_goto_timeout"
            ctx.close()
            return outcome

        if not is_logged_in(page, society):
            # login form 인지 확인
            if page.locator(society.selectors["login_form_marker"]).count() == 0:
                outcome.error = "unknown_landing"
                ctx.close()
                return outcome
            if not perform_login(page, society):
                outcome.error = "auth_failed"
                ctx.close()
                return outcome
            # 로그인 성공 후 listing 재방문 (login_ok.php 의 redirect 가 listing 이 아닐 수 있음)
            try:
                page.goto(society.listing_url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_selector(society.selectors["logged_in_marker"], timeout=10_000)
            except PlaywrightTimeoutError:
                outcome.error = "listing_after_login_failed"
                ctx.close()
                return outcome

        # 신규 post id 식별
        all_ids = _extract_post_ids_from_listing(page)
        if not all_ids:
            outcome.error = "listing_empty"
            ctx.close()
            return outcome

        if last_post_id == 0:
            # 첫 호출 — 내림차순 상위 N
            target_ids = sorted(all_ids, reverse=True)[:FIRST_RUN_LIMIT]
        else:
            target_ids = [pid for pid in all_ids if pid > last_post_id]
        target_ids = sorted(target_ids)[:POLL_LIMIT]  # 가장 오래된 것부터

        if not target_ids:
            ctx.close()
            # last_checked 만 갱신
            state.setdefault(society.key, {})
            state[society.key]["last_checked"] = datetime.now(KST).isoformat(timespec="seconds")
            save_state(state)
            return outcome

        # 처리
        tmp_dir = TMP_DOWNLOAD_ROOT / society.key
        for pid in target_ids:
            np = _process_one_post(page, society, pid, tmp_dir)
            if np.error == "session_expired":
                outcome.error = "session_expired"
                break
            if np.error and not (np.attachments or np.body_md_path):
                outcome.failures.append(np)
            else:
                outcome.new_posts.append(np)

        ctx.close()

    # state 갱신 — 성공 처리한 게시글 중 가장 큰 number
    if outcome.new_posts:
        outcome.last_post_id_after = max(np.post_id for np in outcome.new_posts)
        state.setdefault(society.key, {})
        state[society.key]["last_post_id"] = outcome.last_post_id_after
    state.setdefault(society.key, {})
    state[society.key]["last_checked"] = datetime.now(KST).isoformat(timespec="seconds")
    save_state(state)

    return outcome


def bootstrap_run(society: SocietyConfig) -> None:
    print(f"[bootstrap] {society.society_label} {society.board_label} — headed Chrome 실행. WSLg 필요.")
    print(f"[bootstrap] 프로필 경로: {PROFILE_ROOT / society.profile_key}")
    print(f"[bootstrap] secret 파일: {society.secret_path}")
    print("[bootstrap] run.py 가 toml 기반 ID/PW (옵션 OTP) 를 자동 주입합니다.")
    print("[bootstrap] 사람은 모니터링만 — listing 도달 후 창 닫기.")

    if not society.secret_path.exists():
        print(f"[bootstrap] ⚠ secret 파일이 없습니다: {society.secret_path}")
        print("[bootstrap] 형식:")
        print("  [KSNM]")
        print('  login_id = "<your KSNM ID>"')
        print('  login_pw = "<your password>"')
        print("[bootstrap] chmod 600 으로 생성 후 다시 실행하세요.")
        return

    with sync_playwright() as pw:
        ctx = open_context(pw, society, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(society.listing_url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeoutError:
            print("[bootstrap] listing 진입 타임아웃 — 네트워크 확인.")
            ctx.close()
            return

        if is_logged_in(page, society):
            print("[bootstrap] 이미 세션 살아있음 — listing 도달.")
        else:
            ok = perform_login(page, society)
            if ok:
                print("[bootstrap] 자동 로그인 통과 — listing 도달.")
            else:
                print("[bootstrap] 자동 로그인 실패 — selector 또는 secret 점검.")

        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass

        ctx.close()

    print("[bootstrap] 종료.")


# ---- reporting -------------------------------------------------------------


def notify(text: str) -> None:
    """Telegram 알림 — openclaw stdout-Telegram hook 이 발송."""
    print(text, flush=True)


def report(society: SocietyConfig, outcome: Outcome) -> int:
    label = f"{society.society_label} {society.board_label}"

    if outcome.error == "session_expired":
        notify(
            f"⚠ {label} 세션 만료. WSLg 띄워서 'cd ~/.openclaw/workspace/skills/society-watch && uv run run.py {society.key} --bootstrap' 실행 후 재로그인 부탁합니다."
        )
        return 1
    if outcome.error == "auth_failed":
        notify(f"⚠ {label} 자동 로그인 실패 — secret 또는 selector 점검 필요.")
        return 1
    if outcome.error and outcome.error != "listing_empty":
        notify(f"⚠ {label} 처리 실패: {outcome.error}. 다음 회차 재시도.")
        return 1

    if not outcome.new_posts and not outcome.failures:
        # 새 자료 없음 — 알림 보내지 않음 (SKILL.md §Step 8)
        return 0

    if outcome.new_posts:
        n = len(outcome.new_posts)
        lines = [f"📥 {label} 새 자료 {n}개 — 브레인화 완료"]
        for np in outcome.new_posts:
            title = np.title or f'(post #{np.post_id})'
            note_hint = ""
            if np.note_path is not None:
                try:
                    rel = np.note_path.relative_to(VAULT_ROOT)
                    note_hint = f"  → {rel}"
                except ValueError:
                    note_hint = ""
            lines.append(f"- {title}{note_hint}")
        if outcome.failures:
            lines.append(f"⚠ {len(outcome.failures)}개 첨부 다운로드 실패 — 다음 회차 재시도.")
        notify("\n".join(lines))
    elif outcome.failures:
        notify(f"⚠ {label} {len(outcome.failures)}개 처리 실패 — 다음 회차 재시도.")

    return 0 if not outcome.failures else 2


# ---- entry -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="society-watch")
    parser.add_argument("society", choices=sorted(SOCIETIES.keys()))
    parser.add_argument("--bootstrap", action="store_true", help="1회 헤드 모드 검증")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    society = SOCIETIES[args.society]

    if args.bootstrap:
        bootstrap_run(society)
        return 0

    outcome = cron_run(society)
    return report(society, outcome)


if __name__ == "__main__":
    sys.exit(main())
