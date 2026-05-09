---
name: people-meeting-brief
description: 미팅이 다가오면 vault(2nd-brain) 의 인맥 노트에서 예상 참석자와 누적 맥락을 자동 추출해 Telegram으로 브리핑. 사람을 캘린더 attendees에 매번 등록하지 않아도, 미팅 자체가 사람을 호출. 설계 단계 — 구현 미착수.
status: design-only
allowed_tools: [bash]
---

# people-meeting-brief

## 본질

> **vault는 사람과의 맥락을 저장하고, OpenClaw는 미팅을 단서로 어떤 인물이 등장할지 LLM 추론으로 예상한 뒤, 미팅 직전에 그 인물들과의 누적 맥락을 Telegram으로 보내준다.**

운영 계정: `kimbi.kirams@gmail.com` 만.
vault 경로: `~/projects/2nd-brain-vault/knowledge/02_areas/인맥/` (15명 노트, `google_contact_id` 100% 채움).

## 상태

**설계 완료 · 구현 미착수.**
설계 본체: `~/projects/2nd-brain-vault/knowledge/01_projects/openclaw-people-meeting-brief/README.md`.
이 파일은 향후 `run.py` 가 채택할 동작 정책을 미리 못 박아 두는 용도.

## 향후 절차 (구현 후 적용 예정)

```bash
python3 ~/.openclaw/workspace/skills/people-meeting-brief/run.py
python3 ~/.openclaw/workspace/skills/people-meeting-brief/run.py --force-event <eventId>
```

stdout 처리 규칙은 `gws-assistant` 와 동일 — **비어 있으면 답장 금지, 텍스트 있으면 글자 단위 그대로 출력**.

## 3-컴포넌트 (구현 우선순위 순)

### 1. 미팅 맥락 매칭 → 브리프 (헤드라인, 1순위)

```
캘린더 이벤트 (앞으로 ~6시간 내) 조회
    ↓
이벤트 제목·설명에서 키워드 추출
    ↓
vault `02_areas/인맥/*.md` frontmatter scan
   - relationship_tags 매칭 (가중치 3)
   - organization 매칭 (가중치 2)
   - secondary_roles 매칭 (가중치 2)
   - tags 매칭 (가중치 1)
    ↓
score >= 2 후보 5~10명
    ↓
LLM 정제 — "실제 참석할 것 같은 사람" 골라내기
    ↓
선택자별 노트 fetch
   - 미완료 약속 (`- [ ]` 체크박스)
   - 상대 관심사
   - 마지막 만남 컨텍스트
    ↓
Telegram 브리프 발송 (미팅 1시간 전)
```

**브리프 포맷**: `~/projects/2nd-brain-vault/knowledge/01_projects/openclaw-people-meeting-brief/README.md` 의 출력 예시 참조.

**신뢰도 안내 필수**: "vault에 없는 사람도 올 수 있음" 한 줄을 항상 푸터에 포함.

### 2. 명함 메일 첨부 처리 (2순위, gws-assistant 분기로 통합)

- 메일 제목 prefix `@명함` 또는 라벨 `명함` 으로 분류
- `gog gmail attachment` → 첨부 다운로드
- Claude vision OCR → {이름, 소속, 직책, 메일, 폰}
- `gog contacts search` → 중복 확인 (이메일/폰)
- 신규: `gog contacts create` + vault `_template.md` 복제
- 중복: vault 기존 노트에 만남 기록만 추가

**중요**: 등록은 항상 `gog contacts` (CRUD). `gog people` 은 read-only 라 사용 금지.

### 3. 인터랙티브 브레인화 (3순위, Telegram bot 안정화 후)

타이밍:
- 등록 당일 21시 1차 발송
- 미응답 시 다음날 08:00 1회 재발송
- 48시간 무응답 시 vault 노트는 "초안 상태" 로 보류

질문 순서 (Telegram Q&A):
1. "어디서 만나셨어요?" → vault 이벤트 노트 wikilink 자동 채움
2. "대화 핵심은?" → 3~5줄 요약 LLM 정제
3. "후속 약속 있나요?" → `- [ ]` 체크박스 항목

LLM 역할: 사용자 자연어 한 줄을 받아 vault 노트의 6섹션 표준에 맞춰 구조화.

## vault 데이터 계약 (frontmatter 의존성)

다음 필드가 노트에 있어야 매칭 가능:

| 필드 | 용도 | 상태 |
|---|---|---|
| `google_contact_id` | Contacts ↔ vault 양방향 키 | 표준 (15/15) |
| `relationship_tags` | 매칭 가중치 3 | 표준 |
| `organization` | 매칭 가중치 2 — **항상 current** | 표준 |
| `secondary_roles` | 위원회/이사회 매칭 가중치 2 | 표준 |
| `tags` | 매칭 가중치 1 | 표준 |
| `title_role` | 브리프 출력용 — **항상 current** | 표준 |
| `related_events` | 마지막 만남 컨텍스트 추출 | 표준 |
| `last_role_change` | 서명 추적·stale 감지 트리거 | **신규 (2026-05-08)** |
| `career_history_in_body` | 본문 Career Timeline 섹션 존재 여부 | **신규 (2026-05-08)** |

추가 필요 필드 (현재 vault 표준에 없음, 도입 시 마이그레이션 필요):
- `last_contact: YYYY-MM-DD` — Gmail/Calendar 이력에서 자동 갱신

## 소속/직책 변경 처리 정책 (2026-05-08)

**SCD Type 1 (덮어쓰기) + 본문 Timeline 누적 = Hybrid**.

- frontmatter `organization`/`title_role` 은 **항상 current** — 매칭 알고리즘이 항상 최신 소속을 봐야 호환됨
- 이력은 본문 `## Career Timeline` 섹션에 한 줄씩 누적
- 변경 감지 시 갱신 절차:
  1. frontmatter `organization`/`title_role` 덮어쓰기
  2. 본문 Timeline 에 한 줄 추가
  3. `last_role_change: YYYY-MM-DD` 갱신
  4. `career_history_in_body: true` 토글
- LLM 정제 단계에서는 본문 Timeline 도 fetch — "이전엔 X 교수님이셨음" 컨텍스트 자동 추출 가능

자세한 결정 근거: vault 프로젝트 노트의 "소속/직책 변경 처리 — Hybrid (Option D)" 섹션.

## 자동 감지 신호 (서명 추적은 gws-assistant 분기로 통합)

| 신호 | 처리 위치 |
|---|---|
| Gmail 발신자 서명 변경 | gws-assistant — `memory/signatures.json` 누적 |
| 학회 명단 PDF | society-watch |
| 메일 도메인 변경 | gws-assistant |
| 6개월 정체 (`last_role_change` 노화) | 본 skill heartbeat — 다음 만남 시 Q&A 제안 |

자동 감지 후에도 **사용자 confirm 1회 필수**, vault 에만 기록 후 Contacts sync 는 별도 결정.

## 결정성 보장 — gws-assistant 정책 준용

- 모든 결정·데이터수집·메시지 조립 로직은 외부 Python runner (`run.py`) 에 둔다
- agent reasoning 자유도는 0 에 가깝게 줄인다
- 같은 입력 + 같은 vault 상태 → 같은 출력
- LLM 추론이 필요한 단계 (매칭 후보 정제, vault 노트 요약) 는 **명시적 LLM 호출 단계**로 분리

## Red lines

- vault `_template.md`, `README.md` 는 매칭 후보에서 제외
- PHI 가 노트에 들어가지 않도록 등록 단계에서 차단 (vault 인맥 README 의 프라이버시 규칙 준수)
- "예상 참석자" 라는 것을 브리프에 항상 명시 (사용자 확신을 단정하지 않게)
- 등록은 항상 `gog contacts`, 조회만 `gog people` 사용 가능

## 자세한 사양

전부 vault 프로젝트 노트에 있음:
[[01_projects/openclaw-people-meeting-brief/README.md]]
