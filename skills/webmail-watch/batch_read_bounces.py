"""읽기 전용 배치 — 'failure notice' 검색결과 전체 페이지를 스캔해서
MAILER-DAEMON@zmx*.mailplug.com (KIRAMS 자체 forward-to-Gmail 반송) 행만 골라
하나씩 열어서 본문(원본 메시지 정보)을 추출·디코딩한다.

절대 클릭하지 않는 것: 체크박스, 전달, 이동, 삭제, 스팸등록. 오직 '제목 클릭해서 열기'만.
페이지 이동은 URL page= 파라미터가 아니라 버튼 클릭으로만 한다(URL 파라미터는 실측상
버튼 클릭과 다른 결과를 준다 — off-by-one/다른 인덱싱 추정).
"""
import base64
import json
import logging
import re
import sys

sys.path.insert(0, "/root/.openclaw/workspace/skills/webmail-watch")
from run import TENANTS, open_context, is_logged_in, perform_login
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("batch")

OUT_DIR = "/root/.openclaw/agents/main/memory"
BASE_SEARCH_URL = "https://mail.kirams.re.kr/mail/inbox?search=failure%20notice&searchTarget=all"
MAX_PAGES = 10
TARGET_RE = re.compile(r"MAILER-DAEMON@zmx\d+\.mailplug\.com", re.I)

tenant = TENANTS["kirams"]
sel = tenant.selectors


class PageNotReachable(Exception):
    pass


def goto_page(page, n):
    page.goto(BASE_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(1200)
    if n > 1:
        btn = page.locator(f'button:has-text("{n}")').first
        if btn.count() == 0:
            raise PageNotReachable(f"page {n} 버튼 없음(...로 접혀 있음)")
        btn.click(timeout=5000)
        page.wait_for_timeout(1500)


def scan_rows(page):
    out = []
    for i in range(10):
        rows = page.locator(f'tbody tr[data-index="{i}"]')
        if rows.count() == 0:
            break
        row = rows.first
        try:
            from_text = (row.locator(sel["row_from_title"]).first.get_attribute("title") or "").strip()
        except PWTimeout:
            from_text = ""
        try:
            subject_text = row.locator(sel["row_subject_text"]).first.inner_text(timeout=5_000).strip()
        except PWTimeout:
            subject_text = ""
        out.append((i, from_text, subject_text))
    return out


def decode_original(body_text: str) -> dict:
    info = {}
    for m in re.finditer(r"(?:[A-Za-z0-9+/]{40,}={0,2}\s*){2,}", body_text):
        chunk = re.sub(r"\s+", "", m.group())
        try:
            raw = base64.b64decode(chunk + "=" * (-len(chunk) % 4))
            text = raw.decode("utf-8", errors="ignore")
        except Exception:
            continue
        if "From :" in text or "Subject :" in text:
            fm = re.search(r"From\s*:\s*(.+?)To\s*:", text)
            to = re.search(r"To\s*:\s*(.+?)Sent\s*:", text)
            sent = re.search(r"Sent\s*:\s*(.+?)Subject\s*:", text)
            subj = re.search(r"Subject\s*:\s*(.+?)(?:\n|<mailplugbody|$)", text)
            info["orig_from"] = fm.group(1).strip() if fm else None
            info["orig_to"] = to.group(1).strip() if to else None
            info["orig_sent"] = sent.group(1).strip() if sent else None
            info["orig_subject"] = subj.group(1).strip() if subj else None
            if any(info.values()):
                break
    return info


with sync_playwright() as pw:
    ctx = open_context(pw, tenant, headless=False)
    page = ctx.new_page()
    page.goto(tenant.entry_url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(1500)
    if "/member/login" not in page.url:
        page.goto(tenant.inbox_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1500)
    if not is_logged_in(page, tenant):
        ok = perform_login(page, tenant)
        log.info("login: %s", ok)

    # 1단계 — 클릭 기반 페이지 이동으로 스캔 (열지 않음)
    targets = []
    consecutive_empty = 0
    for p in range(1, MAX_PAGES + 1):
        try:
            goto_page(page, p)
        except PageNotReachable as e:
            log.info("page %d 도달 불가 — 스캔 중단: %s", p, e)
            break
        rows = scan_rows(page)
        before = len(targets)
        for idx, from_text, subject_text in rows:
            if TARGET_RE.search(from_text):
                targets.append({"page": p, "idx": idx, "from": from_text, "subject": subject_text})
        found_here = len(targets) - before
        log.info("page %d 스캔 (행 %d개), 이 페이지 타겟 %d개, 누적 %d개", p, len(rows), found_here, len(targets))
        if len(rows) == 0:
            break
        if found_here == 0:
            consecutive_empty += 1
            if consecutive_empty >= 2 and len(targets) > 0:
                log.info("연속 %d페이지 타겟 0건 — 스캔 종료(이후는 관련 없는 옛 반송으로 판단)", consecutive_empty)
                break
        else:
            consecutive_empty = 0

    log.info("=== 1단계 완료: 총 타겟 %d개 ===", len(targets))

    # 2단계 — 각 타겟을 다시 그 페이지로 이동 후 열어서 본문 읽기
    results = []
    for t in targets:
        try:
            goto_page(page, t["page"])
        except PageNotReachable as e:
            log.warning("page %d 도달 불가 — skip: %s", t["page"], e)
            results.append({**t, "error": "page_not_reachable"})
            continue
        row = page.locator(f'tbody tr[data-index="{t["idx"]}"]').first
        try:
            from_now = (row.locator(sel["row_from_title"]).first.get_attribute("title") or "").strip()
            if not TARGET_RE.search(from_now):
                log.warning("불일치! page=%d idx=%d 기대=%r 실제=%r — skip",
                            t["page"], t["idx"], t["from"], from_now)
                results.append({**t, "error": "row_mismatch", "actual_from": from_now})
                continue
            row.locator(sel["row_subject_text"]).first.click(timeout=5000)
            page.wait_for_timeout(2000)
            body_text = page.inner_text("body")
            decoded = decode_original(body_text)
            entry = {**t, **decoded}
            results.append(entry)
            log.info("읽음: page=%d idx=%d → orig_subject=%r orig_from=%r orig_sent=%r",
                     t["page"], t["idx"], decoded.get("orig_subject"), decoded.get("orig_from"), decoded.get("orig_sent"))
        except Exception as e:
            log.exception("열기 실패 page=%d idx=%d: %s", t["page"], t["idx"], type(e).__name__)
            results.append({**t, "error": type(e).__name__})

    out_path = f"{OUT_DIR}/wmw-bounce-report.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log.info("리포트 저장: %s (%d건)", out_path, len(results))

    ctx.close()
log.info("done")
