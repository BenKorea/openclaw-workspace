---
name: gws-assistant
description: Gmail 받은편지함의 미분류 메일(라벨 없는 것)을 평일 근무시간에 3분류(불필요/보류/진행) 으로 분류한 브레인화 plan 을 Telegram 으로 보고하고, 사용자 승인 시 라벨/archive 를 자동 실행하는 비서. 진행 항목은 사용자 액션(확정/답장/답장할일/할일/경로수정)으로 1단계 종결. 답장 류는 Drafts 등록 + awaiting_reply 큐 → 발송 자동 감지 시 라벨 promote. 모든 결정·분류·실행 로직은 외부 Python runner (run.py) 에 있음.
allowed_tools: [bash]
---

# gws-assistant

Gmail 받은편지함 미분류 메일 → 3분류 plan → Telegram 보고 → 사용자 액션 → 자동 종결.

운영 계정: `kimbi.kirams@gmail.com` 만.

## 호출 패턴

agent 가 받는 슬래시 명령 → 그대로 `run.py` 의 첫 인자로 forward.

| 사용자/cron 메시지 | 실행 |
|---|---|
| `/gws-assistant` (cron 폴) | `python3 ~/.openclaw/workspace/skills/gws-assistant/run.py` |
| `/gws-assistant approve` | `python3 … run.py approve` |
| `/gws-assistant confirm [thread_id]` | `python3 … run.py confirm [thread_id]` (생략 시 단일 pending_review 자동 보강) |
| `/gws-assistant edit [thread_id] folder=<경로>` | `python3 … run.py edit [thread_id] folder=…` (경로 변경 후 즉시 종결) |
| `/gws-assistant skip [thread_id]` | `python3 … run.py skip [thread_id]` |
| `/gws-assistant dismiss [thread_id]` | `python3 … run.py dismiss [thread_id]` |
| `/gws-assistant reply [thread_id] [지시]` | `python3 … run.py reply [thread_id] [지시]` (vault+Gmail 검색 → Drafts → awaiting_reply 큐) |
| `/gws-assistant reply-task [thread_id] [YYYY-MM-DD] [지시]` | `python3 … run.py reply-task …` (reply + Google Tasks 등록 합성) |
| `/gws-assistant gtask [thread_id] [YYYY-MM-DD] [note...]` | `python3 … run.py gtask …` (Google Tasks 등록 + 즉시 종결, note 토큰은 Tasks notes 에 append) |
| `/gws-assistant nl [thread_id] <자연어 문장>` | `python3 … run.py nl [thread_id] <자연어…>` (Opus 4.7 → 3종 명령 reply/reply-task/task 변환) |
| `/gws-assistant correct <thread_id> <proceed\|pending\|noise> [메모]` | `python3 … run.py correct …` |
| `/gws-assistant reclassify <thread_id> <new_cat> [메모]` | `python3 … run.py reclassify …` |
| `/gws-assistant bulk-reclassify <id>:<cat> <id>:<cat> …` | `python3 … run.py bulk-reclassify …` |
| `/gws-assistant learn-rules` / `show-rules` | deterministic 규칙 추출/표시 |
| `/gws-assistant cancel` / `snooze N` / `status` | plan 폐기 / 발화 보류 / 상태 출력 |
| `/gws-assistant migrate-inbox [--apply]` | 기존 inbox 노트 일괄 PARA 정리 |
| `/gws-assistant migrate-brainify-labels [--apply]` | 1회성 — legacy `브레인화/진행` 라벨 → `브레인화/완료` 일괄 promote |
| `/gws-assistant pending-review [N]` | 보류 라벨 메일 N건 라벨 제거 후 재분류 plan |
| `/gws-assistant save-drain [--dry-run] [N]` | §11 `1 저장` 라벨 완전무인 드레인 — 노트 생성+PARA 배치+`9 완료` commit. `--dry-run` 은 mutation 없이 계획만 |
| `/gws-assistant schedule-drain [--dry-run] [N]` | §11.5 `2 일정` 라벨 완전무인 드레인 — 노트 + **Google Calendar 이벤트** + PARA 배치 + `9 완료`. `--dry-run` 은 계획만 |
| `/gws-assistant reply-drain [--dry-run] [N]` | §11.5 **보낸메일 브레인화** — 라벨 0마찰. 보낸편지함 폴링 → **회신+콜드 전부**(`[KIRAMS-FWD]` 제외), audit 가치로만 필터해 노트화. `--dry-run` 은 계획만 |

## 출력 처리 규칙

명령 결과 처리는 정확히 두 가지뿐:

- **stdout 이 비어 있음** → 답장하지 마세요. 어떤 텍스트도 출력하지 마세요. `NO_REPLY` 도, "조용히 종료했습니다" 같은 안내도, 빈 줄도 출력 금지.
- **stdout 에 텍스트가 있음** → 그 텍스트를 그대로 출력하세요. 앞뒤에 prefix·suffix·요약·해석·코드블록 마크다운(```) 등 어떤 추가도 금지. 글자 단위 그대로 복제.

이 두 분기 외에는 어떤 추론·판단도 하지 마세요.

## 강제 폴 (수동 테스트용)

```bash
python3 ~/.openclaw/workspace/skills/gws-assistant/run.py --force-poll
```

게이트(평일/근무시간/공휴일/미팅중) 와 snooze 를 모두 무시하고 강제 발화.

## 운영 모델

### 라벨 정책 — "브레인화 작업 진행 상태"

라벨은 콘텐츠 카테고리가 아니라 작업 상태 표시:

- **`브레인화/완료`** — 모든 처리 종결. 영구 보존 (terminal). 확정/할일/경로수정 즉시, 답장 류는 발송 감지 시 자동.
- **`브레인화/진행`** — **임시** — 답장 발송 대기 (awaiting_reply 큐). 폴링이 SENT 메시지 감지 시 자동으로 `브레인화/완료` 로 promote.
- **`브레인화/보류`** — 외부 액션 대기 또는 분류 자신없음 (라벨, inbox 유지)
- **`브레인화/불필요`** — 광고·자동알림·중복 등 (라벨 + archive)

### §11 8-라벨 액션 모델 — `1 저장` 완전무인 드레인

위 3-라벨은 legacy(grandfathered). 신규 메일은 Dr. Ben 이 폰/PC 에서 8-라벨(`1 저장`~`8 회신`)로 직접 분류한다 (gmail-capture.md §11 권위). 본 스킬은 그 중 **`1 저장`만 완전무인 처리**:

- 트리거: cron poll 매 사이클, 게이트 무관 백그라운드. **단일 킬스위치 `state['autodrain_enabled']` (기본 False)** 가 1~8 핸들러 전부 관장 (2026-05-16 Dr. Ben: 라벨별 플래그 아님 — 인지부하 최소·현실 부합). 검증 후 활성화. 수동 서브커맨드(`save-drain [--dry-run]` 등)는 킬스위치 무관 항상 동작.
- **Telegram 정책 (2026-05-16 Dr. Ben)**: 성공(완료/복구)은 **완전 침묵**. **오류 발생 시에만** 1건 발화. 일일 다이제스트 폐기 — 1~8 잔존은 (2~8 핸들러 완성 후엔) 곧 처리될 in-flight 라 보고가 무의미. `_run_save_drain` 은 `(full, problem)` 반환: 수동 `save-drain` 은 full 전체 출력(터미널), cron 은 problem 만 Telegram.
- 파이프라인 (크래시-안전): threadId 멱등 가드 → `propose_proceed`(노트 staging) → 첨부 `parse_attachment` (파서 레지스트리, 현재 internal) → frontmatter `para_review:pending`/`parser_id`/`parser_version` 주입 + 본문 `## 첨부 파싱` append → `_relocate_to_para`(첨부+노트 PARA 이동) → **commit point: `1 저장` 제거 + `9 완료` 부착 (strictly 마지막)**.
- 멱등성: 라벨 변경이 commit point. 그 전 크래시 시 메일이 `1 저장` 잔류 → 다음 사이클이 threadId 가드로 [복구](라벨만)/[재개](staging 재배치+라벨) 분기.
- PARA 추론 불확실 시 staging 잔류 + `para_review:pending` → 주간 §11.4 PARA 배치감사가 사후 교정 (낙관적 배치).
- PHI 점검 없음 (2026-05-16): 이 Gmail 계정엔 환자정보 송수신 자체가 없어 미수행. CLAUDE.md 2026-04-24 결정과 일관 — 재도입 금지.
- 코어 `_run_label_drain` 이 1~8 공용 (label/tag/extra_action 파라미터). `_run_save_drain`(1 저장, extra=없음)·`_run_schedule_drain`(2 일정, extra=Calendar 이벤트)·`_schedule_extra_action`(idempotent — calendar_event_id 있으면 재생성 skip, 크래시-재개 안전).
- **`2 일정` 출시 (2026-05-16)**: audit 노트 + Calendar 이벤트(`_extract_schedule_from_email`→`_create_calendar_event`→`_attach_schedule_to_note`) + `9 완료`. 일시 추출 실패 시 commit 안 함 + 오류 발화(수동 처리).
- **보낸메일 브레인화 출시 (2026-05-16, `_run_reply_drain`)**: Dr. Ben 결정 — 회신 시 라벨 안 누름(0마찰) + **방향 무관(회신+콜드 전부)**. `8 회신` 라벨 경로 아닌 **보낸편지함 폴링**. `in:sent -subject:"[KIRAMS-FWD]" -label:"9 완료" newer_than:2d` → 보낸 메일 전부(내가 먼저 보낸 콜드메일 포함), audit 가치 판단(노트 LLM 단계)으로만 필터. 노트는 보낸 메시지(회신이면 원문 인용 포함)로 빌드, frontmatter `gmail_threadIds` 를 canonical threadId 로 덮어써 가드 멱등. **라벨 없는 모델**: 제거할 라벨·`9 완료` 부착 없음, 멱등성=노트존재뿐. v1 한계: 노트 있는 스레드의 추가 메일 미포착(§9.2 thread 진화로 위임), 라벨핸들러 교차중복 드묾(주간 §9.2 감사 포착). `brainify_origin: gmail-sent` 마커.
- `3~8` **라벨** 핸들러는 deferred — `extra_action` 추가만으로 확장. 단 회신 기능은 위 무라벨 경로가 커버하므로 `8 회신` 라벨 경로는 사실상 우선순위 낮음(Dr. Ben 회신 시 라벨 안 함).

### 전환 상태 / 폐기 게이트 (mini-sdd transition contract)

본 skill 은 **[구 3-라벨 AI분류 모델] → [§11 8-라벨 인간트리아지 모델]** 이행 중. 전환기엔 양 표면 공존(보존하며 이행). 어느 코드가 transitional 인지·언제 삭제하는지 명시해 mini-sdd 합법화 — open-ended 레거시 보존(폐기 트리거 없음)이 안티패턴.

**표면 분류**

| 구분 | 범위 | 운명 |
|---|---|---|
| **TARGET** | `_run_label_drain` 코어·`save-drain`/`schedule-drain`/`reply-drain`·`_run_reply_drain`·§11.5 `3~8` 핸들러·`parse_attachment` 레지스트리·threadId 가드·`_commit_action_label`·`LABEL_SAVE`/`LABEL_SCHEDULE`/`LABEL_DONE_9` | 영구 |
| **SHARED-INFRA** | `propose_proceed`·`_relocate_to_para`·`gog_call/json`·노트/frontmatter/PARA 헬퍼·`fetch_thread_full`·Tasks/Calendar 헬퍼·state | 영구 (레거시 삭제 후 TARGET 전용 잔존) |
| **LEGACY** (게이트서 일괄 삭제) | 3-라벨 classify→plan→approve 흐름 전체: `cmd_poll` 발화경로·`classify_emails_llm`·`build_plan_items`·`merge_plan`·`approve/confirm/edit/skip/dismiss/cancel`·`reply/reply-task/gtask/schedule/nl`·`correct/reclassify/bulk-reclassify/learn-rules/show-rules`·`awaiting_reply` 큐·gates(`check_gates`·`is_busy_now`·`fetch_today_events`·`is_korean_holiday`)·`snooze`·`LABEL_PROCEED/PENDING/NOISE/DONE` 상수·해당 SKILL.md 행 | 게이트 충족 시 |
| **ONE-SHOT** | `migrate-inbox`·`migrate-brainify-labels` | 1회 사용 후 즉시 삭제 (게이트 무관) |

> 2026-05-16 Dr. Ben 확정: AI분류 튜닝군(`correct`/`learn-rules` 등)은 8-라벨선 인간이 분류하므로 전부 LEGACY. gates/`snooze` 도 LEGACY — 자동드레인은 게이트 무관이라 레거시 제거 시 소비자 0.

**폐기 게이트 트리거 (AND — 전부 충족 시에만 LEGACY 삭제)**

1. `autodrain_enabled=true` 로 자동드레인(활성 핸들러 전체) prod **무사고 ≥ 4주** (또는 무사고 사이클 ≥ 20) — 단일 플래그라 1~8 통합 검증 시계 1개
2. §11.5 `3~8` 핸들러 출시·검증 완료 (`2 일정` 2026-05-16 출시 완료; 나머지 회신/할일/복합을 신 모델이 커버 — 선행 안 하면 capability 손실)
3. Dr. Ben 실제 트리아지가 8-라벨 스와이프로 완전 이행 (구 "AI분류+Telegram approve" 가 더 이상 작업흐름 아님 — 운영 확인)
4. 신 모델 SKILL.md manual test commands 전부 통과 (회귀 안전망)
5. (구조 선행조건) `run.py` 관심사별 분해되어 LEGACY 가 **단일 모듈로 격리** → 삭제 = 모듈+dispatch 제거

**삭제 절차 (게이트 충족 후, 독자 mini-sdd 단위)**

- spec: "LEGACY 모듈+dispatch+`LABEL_*` 상수+SKILL.md 레거시 행 제거, 동작보존(TARGET 무영향)"
- acceptance: 신 모델 manual test 전부 통과 + 레거시 명령은 명시적 deprecation stub 반환
- 핸드오프: `/cron off → 삭제 → 단위테스트 → /git-routine sync → /cron on → smoke test` (openclaw-skill-dev.md 워크플로우)

### 폴링 (cron, 평일 30분)

매 사이클:
1. **awaiting_reply 큐 처리** — 게이트 무관, 항상 실행. 각 항목의 thread 에서 drafted_at 이후 SENT 라벨 메시지 검출 시 자동 promote (`브레인화/진행` → `브레인화/완료`) + 노트에 `replied_at` 기록 + 큐에서 제거.
2. **게이트 체크** — 평일/근무시간/공휴일/미팅중-`판독`예외. 실패 시 새 메일 발화 안 함 (단 awaiting_reply 결과는 출력).
3. **분류** — 휴리스틱(from 패턴 매칭) → `noise` 자동. 나머지는 Haiku batch 호출 → `proceed`/`pending`/`noise`. vault `gmail-capture.md` §1·§2 동적 주입.
4. **plan merge & 발화** — 신규 msg 가 plan 에 추가되면 한 메시지로 발화.

### plan 영속

`~/.openclaw/agents/main/memory/gws-assistant.json` 의 `pending_plan` 필드. 명시 폐기/승인까지 살아있음, 자동 만료 없음.

### 승인 흐름 (1단계 종결 모델)

1. `/gws-assistant approve` → `pending`/`noise` 일괄 라벨/archive. `proceed` 항목은 큐로 진입.
2. 첫 `proceed` 항목 → 본문 fetch + Opus 4.7 동반 노트 작성 → vault `sources/00_inbox/` atomic write (staging) → **1건 보고만 (라벨/archive 안 함)**.
3. 사용자가 보고에 다음 중 **하나**로 응답 (7개 canonical 명령):
   - **`/g 확정` (= ok)** — 노트 → `knowledge/<PARA>/`, 첨부 → `sources/<PARA>/`. 라벨 `브레인화/완료` + archive. **즉시 종결**.
   - **`/g 답장` (= reply)** — vault + Gmail 같은 발신자 직전 thread 검색 → Opus 4.7 회신 초안 → Gmail Drafts 등록. 노트 PARA 이동. 라벨 `브레인화/진행` + archive. awaiting_reply 큐 push. **발송 자동 감지 시 종결**. 도움말상 인자 없음 — 추가 지시 토큰을 붙여도 파서는 받아 LLM 컨텍스트로 forward.
   - **`/g 답장할일 [YYYY-MM-DD]`** — `/g 답장` + Google Tasks 등록 (마감일: 인자 → LLM 추출 → 마감 없음 순). 종결 트리거 동일 (발송 감지).
   - **`/g 할일 [YYYY-MM-DD] [note]` (= task)** — Google Tasks 등록만 + 즉시 종결. note 토큰들은 Google Tasks notes 필드 끝에 `Note: …` 로 append.
   - **`/g 경로수정 folder=<경로>`** — PARA 폴더 변경 후 즉시 종결 (재확인 없음).
   - **`/g 보류`** — 노트 삭제 + 라벨 `브레인화/보류` (inbox 유지).
   - **`/g 불필요`** — 노트 삭제 + 라벨 `브레인화/불필요` + archive.

   (도움말은 canonical 7개만 노출. 파서는 추가 한국어/영어 alias — 예/네/맞아/좋아/승인/confirm/approve, skip/나중에, 폐기/dismiss 등 — 도 동일하게 인식.)
4. 다음 큐 1건 propose → 또 1건 보고. 큐 소진 시 완료 메시지.

### Google Tasks 통합

- list: 기존 `Brainify` 또는 `메일 후속` (이름 우선순위) 자동 재사용. 없으면 `Brainify` 신규 생성. list_id 는 state 에 캐시.
- title: 메일 제목 그대로
- notes: From / Gmail thread URL / vault 노트 경로 (+ Drafts ID — 답장할일 시)
- due: `YYYY-MM-DD` (시간 없음, Google API 가 시간 부분 무시)
- 노트 frontmatter 에 `gtask_id:` / `gtask_due:` 기록
- OAuth: `gog` CLI 가 이미 tasks scope 보유 — 재인증 불필요

### 답장 자동 감지 메커니즘

awaiting_reply 큐의 각 항목 (thread_id, drafted_at) 에 대해 매 폴 사이클마다:
- `gog gmail thread get <thread_id>` 로 thread 의 모든 메시지 메타 fetch
- 각 메시지의 `labelIds` 에 `SENT` 가 포함되고 `internalDate >= drafted_at` 이면 → 발송 감지
- 라벨 `브레인화/진행` → `브레인화/완료` promote, 노트에 `replied_at` 기록, 큐에서 제거
- TTL 미설정 — 사용자가 Drafts 폐기/장기 미발송이어도 큐에 남아 있음 (수동 정리는 향후 필요시 추가)

### 노트 작성 모델

- 분류: Haiku 4.5 (`classify_emails_llm`)
- 동반 노트: Opus 4.7 (`build_companion_note_llm`) — frontmatter PARA 추론, ## 요약 작성
- 회신 초안: Opus 4.7 (`cmd_draft_reply`) — vault 검색 + Gmail 같은 발신자 직전 thread 검색을 컨텍스트로 주입
- 마감일 추출: Haiku 4.5 (`_extract_due_from_email`) — 명시적 표현만, 비명시적 ("가급적 빨리" 등) 은 None

## 자세한 사양

전부 `run.py` 의 모듈 docstring 과 상수 정의에 있음. vault 의 [01_projects/openclaw-gws-assistant/SESSION-HANDOFF.md](~/projects/2nd-brain-vault/knowledge/01_projects/openclaw-gws-assistant/SESSION-HANDOFF.md) 가 Phase 1 분류 사양의 권위 문서.
