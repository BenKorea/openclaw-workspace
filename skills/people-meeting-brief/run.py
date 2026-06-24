#!/usr/bin/env python3
"""people-meeting-brief — 주간 인맥 감사 브리핑 (MVP, Phase 3-A).

vault(읽기 전용 마운트)의 인맥 노트를 scan해 *검토/보강 대기* 항목만 추려
Telegram 브리핑 텍스트로 stdout 출력한다.

규칙 (gws-assistant 동일):
  - 대기 항목 있음 → 포맷 텍스트 출력 (cron delivery 가 Telegram 발송)
  - 대기 항목 없음 → stdout 비움 = 발송 안 함 (조용)

입력 = brainify `audit --inmaek-only` 와 같은 신호(gcontacts_review:flagged
+ 동기 미완 노트). brainify.py 는 호스트 skill 이라 컨테이너에서 못 보므로
로직을 자족 포팅 — vault 만 ro 로 읽으면 동작 (쓰기 없음).

경로: 컨테이너는 VAULT_ROOT=/vault (extra.yml ro 마운트), 호스트 테스트는
      VAULT_ROOT=~/projects/2nd-brain-vault 로 override.
"""
import os
import re
import glob

VAULT_ROOT = os.environ.get("VAULT_ROOT", "/vault")
INMAEK_DIR = os.path.join(VAULT_ROOT, "knowledge", "02_areas", "인맥")

# flat `key: value` 만 추출 (list/multiline 필드는 무시 — 판정에 불필요)
_FM_BLOCK = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_FM_LINE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$")


def parse_frontmatter(text: str) -> dict:
    m = _FM_BLOCK.match(text)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        mm = _FM_LINE.match(line)
        if mm:
            fm[mm.group(1)] = mm.group(2).strip().strip('"').strip("'")
    return fm


def scan():
    """(review, incomplete) — 검토 대기 / 보강 필요 인물명 리스트."""
    review, incomplete = [], []
    for path in sorted(glob.glob(os.path.join(INMAEK_DIR, "*.md"))):
        try:
            with open(path, encoding="utf-8") as f:
                fm = parse_frontmatter(f.read())
        except OSError:
            continue
        if not fm:
            continue
        name = fm.get("title") or os.path.splitext(os.path.basename(path))[0]
        # 1) 자동 트랙이 잠정 생성해 Dr. Ben 검토 대기 (gcontacts_review: flagged)
        if fm.get("gcontacts_review") == "flagged":
            review.append(name)
            continue
        # 2) 동기 대상(affiliation_scope 지정)인데 미완 — pending + 필수필드 공백
        if fm.get("affiliation_scope") in ("internal", "external"):
            if fm.get("gcontacts_sync") == "pending" and (
                not fm.get("contacts_display_name") or not fm.get("title_role")
            ):
                incomplete.append(name)
    return review, incomplete


def main() -> None:
    review, incomplete = scan()
    if not review and not incomplete:
        return  # 빈 stdout → 발송 안 함
    lines = ["📇 주간 인맥 브리핑"]
    if review:
        lines.append(f"\n🔵 검토 대기 (자동 생성 {len(review)}건) — 승격/폐기 결정:")
        lines += [f"  • {n}" for n in review]
    if incomplete:
        lines.append(f"\n🟡 보강 필요 ({len(incomplete)}건) — 부서·보직 미입력으로 미동기:")
        lines += [f"  • {n}" for n in incomplete]
    lines.append("\n→ Claude Code 에서 `/brainify audit` 또는 인맥 노트 보강 후 동기.")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
