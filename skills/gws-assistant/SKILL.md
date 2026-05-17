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
| `/gws-assistant status` | 상태 출력 (last_checked·awaiting_reply·gtasks_list_id 등) |
| ~~approve·confirm·edit·skip·dismiss·reply·reply-task·gtask·schedule·nl·correct·reclassify·bulk-reclassify·learn-rules·show-rules·snooze·cancel·migrate-inbox·migrate-brainify-labels·pending-review~~ | **레거시 — 2026-05-17 삭제.** 호출 시 deprecation stub(stderr+rc=1). §11 8-라벨 스와이프 + `*-drain` 으로 대체 |
| `/gws-assistant save-drain [--dry-run] [N]` | §11 `1 저장` 라벨 완전무인 드레인 — 노트 생성+PARA 배치+`9 완료` commit. `--dry-run` 은 mutation 없이 계획만 |
| `/gws-assistant schedule-drain [--dry-run] [N]` | §11.5 `2 일정` 라벨 완전무인 드레인 — 노트 + **Google Calendar 이벤트** + PARA 배치 + `9 완료`. `--dry-run` 은 계획만 |
| `/gws-assistant task-drain [--dry-run] [N]` | §11.5 `6 할일` 라벨 완전무인 드레인 — 노트 + **Google Tasks 등록**(Brainify 리스트) + PARA 배치 + `9 완료`. 추출 실패해도 제목 fallback 으로 항상 task 1개. `--dry-run` 은 계획만 |
| `/gws-assistant reply-drain [--dry-run] [N]` | §11.5 **보낸메일 브레인화** — 라벨 0마찰. 보낸편지함 폴링 → **회신+콜드 전부**(KIRAMS 포워딩은 `[KIRAMS-FWD]` + `from:kirams AND [FW]` 로 정밀 제외), audit 가치로만 필터해 노트화. `--dry-run` 은 계획만 |
| `/gws-assistant reply-label-drain [--dry-run] [N]` | §11.5 `8 회신` 라벨 드레인 — 노트 + **회신 초안**(Opus, Drafts) + `awaiting_reply` 2단계(발송감지→`9 완료` promote) + 사이클당 1건 Telegram 요약. `reply-drain`(sent-poll)과 별개. `--dry-run` 은 계획만 |

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
- 멱등성: 라벨 변경이 commit point. 그 전 크래시 시 메일이 `1 저장` 잔류 → 다음 사이클이 threadId 가드로 [복구](액션 보강 idempotent + 라벨)/[재개](staging 재배치+라벨) 분기. **가드는 액션-인지(2026-05-17)**: relocated 노트 존재 시에도 `extra_action` 호출 — extra_action 자체 멱등(`google_task_id`/`calendar_event_id` 마커 self-skip)이 액션 중복 차단, 가드는 노트 중복만 차단. (구: 노트존재=extra_action완료 가정 → 무액션 캡처 노트에 액션 라벨 시 Task/이벤트 조용히 누락하던 크로스-핸들러 사각지대, `2 일정`·`6 할일` 공통이었음.)
- PARA 추론 불확실 시 staging 잔류 + `para_review:pending` → 주간 §11.4 PARA 배치감사가 사후 교정 (낙관적 배치).
- PHI 점검 없음 (2026-05-16): 이 Gmail 계정엔 환자정보 송수신 자체가 없어 미수행. CLAUDE.md 2026-04-24 결정과 일관 — 재도입 금지.
- **⚠️ wrong-message 버그 수정 (2026-05-16, `78262c7`)**: `gog gmail search` 는 **thread 단위** 반환(메시지 id 없음, `id`=threadId). 구 `_pick_target_message` 가 항상 `msgs[0]`(최古 메시지) 선택 → 멀티-메시지 스레드서 라벨 붙은 메일 아닌 엉뚱한 메일로 노트 생성. 수정: `_pick_target_in_thread(payload,label)`(단일=그메시지, 멀티=labelIds 에 label/SENT 든 최신, 없으면 None→skip) + `propose_proceed(*,target_label=)`; label핸들러→`target_label=label`, reply→`"SENT"`. **검증상태 정정: 1·2·reply 기존 "검증완료" 는 단일-메시지 운 — 멀티-메시지 스레드로 재검증 필수.** legacy(target_label=None) 무변경.
- 코어 `_run_label_drain` 이 1~8 공용 (label/tag/extra_action 파라미터). `_run_save_drain`(1 저장, extra=없음)·`_run_schedule_drain`(2 일정, extra=Calendar 이벤트)·`_run_task_drain`(6 할일, extra=Google Tasks)·`_schedule_extra_action`/`_task_extra_action`(idempotent — calendar_event_id / google_task_id 있으면 재생성 skip, 크래시-재개 안전).
- **`2 일정` 출시 (2026-05-16)**: audit 노트 + Calendar 이벤트(`_extract_schedule_from_email`→`_create_calendar_event`→`_attach_schedule_to_note`) + `9 완료`. 일시 추출 실패 시 commit 안 함 + 오류 발화(수동 처리).
- **`6 할일` 출시 (2026-05-17)**: audit 노트 + Google Tasks 등록(`_extract_task_from_email`→`create_gtask`/`ensure_gtasks_list` 재사용→`_attach_task_to_note`) + `9 완료`. 멱집합 atomic surface 세 개 중 task surface — 일정(`2`)·회신(무라벨 경로)에 이어 마지막 단위 surface 완성, 복합 라벨 `3·4·5·7` 은 이제 합성만 남음. 일정과 달리 마감일 선택(없어도 등록), 추출 실패해도 제목 fallback 으로 항상 task 1개 → `6 할일` 라벨 자체가 Dr. Ben 의 'task' 결정이므로 commit 막지 않음. `extra_action` 이 `state`(gtasks_list_id 캐시) 필요 → `_run_task_drain` 에서 closure 바인딩((item,now) 콜백 계약 유지). frontmatter `google_task_id`/`google_task_due`(schedule 의 `calendar_event_id` 와 평행 — §10 인터랙티브 `gtask_id`·복수형 `google_task_ids` 와 별개 cron 단발 필드).
- **보낸메일 브레인화 출시 (2026-05-16, `_run_reply_drain`)**: Dr. Ben 결정 — 회신 시 라벨 안 누름(0마찰) + **방향 무관(회신+콜드 전부)**. `8 회신` 라벨 경로 아닌 **보낸편지함 폴링**. `in:sent -subject:"[KIRAMS-FWD]" -(from:kirams.re.kr subject:"[FW]") -label:"9 완료" newer_than:2d` → 보낸 메일 전부(콜드 포함), audit 가치 판단(노트 LLM 단계)으로만 필터. **KIRAMS 포워딩 노이즈 정밀 배제**: ① `[KIRAMS-FWD]` 제목=항상 노이즈 ② `from:kirams AND [FW]`=prefix 없는 변종. (`-in:inbox` 는 archive 후 진짜 보낸 메일과 구분 불가라 폐기, `from:kirams` 전체 배제는 Dr. Ben 의 KIRAMS 별칭 send-as 진짜 메일까지 막아 불가 — 2026-05-16 2차 dry-run 으로 확정.) 노트는 보낸 메시지(회신이면 원문 인용 포함)로 빌드, frontmatter `gmail_threadIds` 를 canonical threadId 로 덮어써 가드 멱등. **라벨 없는 모델**: 제거할 라벨·`9 완료` 부착 없음, 멱등성=노트존재뿐. v1 한계: 노트 있는 스레드의 추가 메일 미포착(§9.2 thread 진화로 위임), 라벨핸들러 교차중복 드묾(주간 §9.2 감사 포착). `brainify_origin: gmail-sent` 마커.
- **`8 회신` 구현 완료 (2026-05-17 dev)**: sent-poll(보낸 답장 사후) ↔ `8 회신`(답장 위임 능동) = 회신 두 절반, 상보적. `_run_label_drain` 에 `commit_fn` seam(`extra_action` 과 평행) — `8 회신` 만 `_commit_reply_label`(archive-only 비-terminal), 1·2·6 기본 terminal. `_reply_extra_action`=LEGACY `cmd_draft_reply` 무인 재배선(`fetch_thread_full`→Opus 초안→`gog gmail drafts create`→`_attach_draft_to_note`→`awaiting_reply`), 멱등=`gmail_draft_id` frontmatter. 모호=`STATUS: ok|review` 1줄 프로토콜(본문 미포함)→`reply_review`+Telegram `[검토필요]`. 종결=기존 2단계 재사용 — `_poll_awaiting_replies` 라벨-인지화(엔트리 `src_label`/`terminal`→`9 완료`+`8 회신` 제거, LEGACY grandfather). `awaiting_reply` skip 필터로 매-사이클 재처리 차단(state 유실→라벨 잔존→가드 멱등 복구 수렴). Telegram=silent-on-success 명문화 예외·`_run_reply_label_drain` 이 사이클당 1건 요약 problem 합성. sent-poll 조율=동반노트 `gmail_threadIds` canonical → `_run_reply_drain` 가드 자동 skip(추가코드 0). 수동 `reply-label-drain`+cron 튜플 1·2·6·8·sent-poll. **회신 2단계 비-terminal 이 복합 `4·5·7` 에 상속**. py_compile·dev clean exit 통과. 첫 실드레인서 멀티-메시지 대상선택 결함 노출 → `_pick_target_in_thread` ID-인지 버그픽스 동반(`_resolve_label_id` 이름→ID, gog 가 per-message labelIds 를 사용자 라벨 ID 로 반환 — 1·2·6·8 공통 결함이었음). 행동검증(ID-픽스 후 재드레인) 대기 — *배포 cutover 불요*(main 세션 on-host 직접실행, §11.5 후속9 정정). 권위=gmail-capture.md §11.5.
- **`3 일정+할일` 구현 완료 (2026-05-17 dev)**: 순수 terminal 합성 실증 — `_sched_task_extra_action`=`_task_extra_action`+`_schedule_extra_action` 조합뿐(새 로직 0). task 먼저(항상 성공) → schedule(실패 시 `2 일정` 계약대로 commit 차단+발화, 할일은 선확정). 둘 다 기존 멱등 가드 → 합성 자동 멱등. terminal 기본 commit. `_run_sched_task_drain`+`schedule-task-drain`+cron 튜플 1·2·6·3·8·sent-poll. py_compile·clean exit, 행동검증 gog-auth 대기.
- **`4·5·7` 구현 완료 (2026-05-17 dev) — 8-라벨 멱집합 전부 완성**: `_run_reply_composite_drain(label,tag,pre)` 일반화(8·4·5·7 단일 경로) — `pre`(비-회신 atomic, 실패=reply 전 bail→고아 0)→`_reply_extra_action(src_label=label)`→`commit_fn=_commit_reply_label`. `_reply_extra_action` 에 `src_label` 파라미터(awaiting_reply 에 복합 라벨 기록→`_poll_awaiting_replies` 가 그 라벨 떼고 `9 완료`). pre: 4=schedule/7=task/5=`_sched_task_extra_action`(3 재사용). 수동 `schedule-reply`/`task-reply`/`schedule-task-reply`-drain + cron 튜플 1·2·6·3·8·4·7·5·sent-poll. py_compile·clean exit, 행동검증 gog-auth 대기.

### 이행 완료 — 구 3-라벨 레거시 삭제됨 (2026-05-17)

본 skill 은 **[구 3-라벨 AI분류 모델] → [§11 8-라벨 인간트리아지 모델]** 이행을 **완료**. 전환기 공존 종료 — 레거시 일괄 삭제.

**Dr. Ben 결단 (2026-05-17)**: 원 폐기 게이트 5조건(autodrain prod 무사고 4주 / 8-라벨 구현 / 스와이프 완전이행 / manual test / 구조격리) 중 1·4·5 우회. 근거: 신 모델 핵심(`6 할일`·`8 회신`·`3 일정+할일` end-to-end 검증, `4·5·7` = 검증 부품 순수 합성) 충분 + **라벨-구동 단일화 수용**(받은편지함은 스와이프 라벨만 브레인화, AI 자동분류+approve 폐지; 보낸메일 sent-poll 은 유지). 감수 리스크: 신 모델 잠복버그 시 폴백 0. 검증-우선 후 삭제.

**삭제분 (~2150줄, run.py 4934→2611, grep 실측 경계)**: 3-라벨 classify→plan→approve 전체 — `classify_emails_llm`·`build_plan_items`·`merge_plan`·`fetch_inbox_pending`·`format_plan_message`·`cmd_approve/confirm/edit/skip/dismiss/cancel`·`cmd_draft_reply`·`cmd_gtask`·`cmd_schedule`(인터랙티브)·`cmd_nl`·`cmd_correct/reclassify/bulk_reclassify/learn_rules/show_rules`·`cmd_pending_review`·`finalize_proceed`·gates(`check_gates`·`is_busy_now`·`fetch_today_events`·`is_korean_holiday`)·`is_snoozed`·`cmd_snooze`·`LABEL_PROCEED/PENDING/NOISE/DONE`·`LEGACY_LABEL_DUPLICATE`·`CATEGORY_*`·`migrate-inbox/-brainify-labels`(ONE-SHOT) + 전 dispatch.

**수술 보존 (TARGET, 레거시 분기만 절제)**: `cmd_poll`(레거시 classify 분기 제거 → `_poll_awaiting_replies`+autodrain 만), `_poll_awaiting_replies`(grandfather fallback 제거 → new-only `9 완료` promote).

**경계 도출의 교훈**: mini-sdd LEGACY 목록이 *stale* 했음 — 이번 세션 신 모델이 `awaiting_reply`·`_poll_awaiting_replies`·`cmd_poll`·`propose_proceed`·`_pick_target_in_thread`·`_attach_draft_to_note`·`fetch_thread_full` 을 load-bearing 으로 co-opt. 목록 맹신 시 신 모델 파손. **AST transitive 자동폐쇄도 과삭제**(main/dispatch 중심성으로 신모델 명령·core 까지 흡수) → 폐기, 명시 큐레이션 60함수 + KEEP-참조 안전검증으로 확정. (이번 세션 "추측이 매번 졌다" 7번째.)

**검증**: py_compile 통과 + 회귀 스모크(legacy stub rc=1 / status / 8 핸들러 dry-run NameError 0 / cmd_poll 무크래시). **잔여 = gog-auth 행동검증 1개뿐**(Dr. Ben 검증-충분 판단). ※ "배포 cutover" 는 거짓 게이트로 정정됨(2026-05-17): cron→main 세션, `sandbox.mode=non-main` 이라 main 은 on-host(direct) → 정본 workspace `run.py`(신코드) 직접 실행. 배포 갭·cutover 단계 없음. 상세 = gmail-capture.md §11.5 후속9 / cron-inventory.md 메타.

**잔존 정리(선택)**: `cmd_status` 가 vestigial `pending_plan`/`snooze_until` 표시(graceful, 무해 — cosmetic).

### 폴링 (cron, 평일 30분)

매 사이클:
1. **awaiting_reply 큐 처리** — 게이트 무관, 항상 실행. 각 항목의 thread 에서 drafted_at 이후 SENT 라벨 메시지 검출 시 자동 promote (트리거 라벨 `8/4/5/7` 제거 + `9 완료` 부착) + 노트에 `replied_at` 기록 + 큐에서 제거.
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
