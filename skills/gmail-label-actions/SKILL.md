---
name: gmail-label-actions
description: Gmail 라벨(1~8)에 따라 Google Workspace 후속작업(저장·일정·할일·회신초안)을 완결하고 메일 스레드+첨부를 캡처하는 라우터. **자연어 트리거**: "라벨 메일 처리해줘"·"인박스 라벨 정리해줘"·"오늘 라벨 단 메일 받아줘" → run.py 실행 / "먼저 몇 건인지 봐줘"·"확인만" → run.py --dry-run / "2 일정만 돌려줘" → run.py --label "2 일정". 라벨별 후속작업(Calendar·Tasks·Drafts)은 run.py 가 gog + claude --print 바운디드 추출로 처리한다. 에이전트는 고른 명령만 1회 실행 — **직접 실행, 단계 추론·하위 에이전트 위임 금지**.
allowed_tools: [bash]
---

# gmail-label-actions

Gmail 라벨이 뜻하는 **다음 행동**({일정,할일,회신} 멱집합 + 저장)을 Google Workspace 안에서 **완결**하고, 메일은 스레드+첨부로 캡처한다. 모든 로직은 외부 Python runner(`run.py`)에 있고, 에이전트는 고른 명령만 1회 실행한다.

> 📦 이건 **예제·템플릿**입니다. 적용 전 `run.py` 의 env(`GMAIL_ROUTER_ACCOUNT`)·저장경로를 본인 환경에 맞게 고치세요. **라벨명("1 저장"…"9 완료")은 고정 규칙** — 코드를 고치지 말고 Gmail 에 그 9개 라벨을 만들어 쓰세요. 설치·적응법은 같은 폴더 [README.md](./README.md).

## 호출

자연어 트리거는 frontmatter `description` 이 담당한다(에이전트가 거기서 의도 매칭). 아래는 에이전트가 고를 **실행 모드** — 사용자 발화에 맞는 모드를 골라 **그 명령만 1회** 실행한다.

| 모드 | 사용자 발화 예 | 명령 |
|---|---|---|
| 실행(전체) | "라벨 메일 처리해줘", "인박스 정리해줘" | `python3 ~/.openclaw/workspace/skills/gmail-label-actions/run.py` |
| 특정 라벨만 | "2 일정만 돌려줘" | `… run.py --label "2 일정"` |
| 확인(검색만) | "먼저 몇 건인지 봐줘", "확인만" | `… run.py --dry-run` |

> 에이전트는 고른 명령을 **그대로 1회 실행**한다. 단계를 직접 추론·반복하거나 하위 에이전트로 위임하지 말 것. 비가역(라벨 변경·이벤트/할일/초안 생성) 동작이라 망설여지면 먼저 `--dry-run` 으로 건수만 보고한 뒤 실행.

## 라우트 (8 라벨 = {일정,할일,회신} 멱집합 + 저장)

| 라벨 | GWS 후속작업 (run.py 완결) | 캡처 | 종결 |
|---|---|---|---|
| `1 저장` | — | 스레드+첨부 | → `9 완료` |
| `2 일정` | Calendar 이벤트 | 〃 | → `9 완료` |
| `3 일정+할일` | Calendar + Task | 〃 | → `9 완료` |
| `6 할일` | Task | 〃 | → `9 완료` |
| `4 일정+회신` | Calendar + 초안 | 〃 | 라벨 유지 |
| `5 일정+할일+회신` | Calendar + Task + 초안 | 〃 | 라벨 유지 |
| `7 할일+회신` | Task + 초안 | 〃 | 라벨 유지 |
| `8 회신` | 초안 | 〃 | 라벨 유지 |

## 설계 원칙

- **캡처는 결정형, 추출은 바운디드**: 스레드·첨부 저장은 판단0. 일시/할일/회신 초안만 `claude --print` 헤드리스 1회 호출(JSON/STATUS 계약, 툴 비활성) — "에이전트 판단" 이 아니라 결정형에 가까운 *좁은 추출*. 무인 cron 안전.
- **판단 분리**: PARA 분류·요약·`[[링크]]` 같은 열린 판단은 이 라우터가 하지 않는다 — brainify(Claude Code 스킬)가 나중에. 이 라우터는 knowledge/ 노트를 만들지 않는다.
- **fail-safe**: terminal 라벨(1·2·3·6)은 필수 액션 *전부 성공* 후에만 `9 완료` 부착. 일시 추출 실패 등은 라벨 유지 → 다음 사이클 재시도(유실 0).
- **멱등성 2층**: 캡처 = `_thread.md` 의 `gmail_thread_id`+`message_count`. GWS 액션 = 스레드 폴더 `_actions.json` 사이드카(`calendar_event_id`/`google_task_id`/`gmail_draft_id`) — 캡처가 `_thread.md` 를 덮어써도 마커 보존 → 이중 생성 0.
- **회신은 비-terminal(Phase 1)**: 초안 생성까지 완결, 라벨 유지. 실발송 감지→자동 `9 완료` 승급은 **Phase 2**(영속 awaiting_reply 큐 + sent-poll).

## 전제

- gog CLI 설치·인증 (Gmail·Calendar·Tasks scope). 무인 실행 시 keyring 비번(`GOG_KEYRING_PASSWORD`) 환경 제공.
- 추출(일정·할일·회신)에는 Claude Code CLI(`claude`) 설치·인증 필요. `claude` 없으면 일정·회신은 skip(라벨 유지), 할일은 제목 fallback 으로만 생성.

## 알려진 한계

- 멱등성은 **저장 디렉토리 범위**의 버퍼다. vault 전체 dedup 은 brainify 단계가 담당.
- 회신 발송 감지(자동 `9 완료`)는 Phase 2 — 현재는 초안 생성 후 사용자가 검토·발송.
- `+` 포함 라벨은 `label:3-일정+할일`(공백→하이픈 unquoted), 그 외는 `label:"1 저장"` 쿼리. 0건이면 `--dry-run` 으로 확인.
