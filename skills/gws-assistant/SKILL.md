---
name: gws-assistant
description: Gmail 의 unread 메일을 평일 근무시간에 3분류(불필요/보류/진행) 으로 분류한 브레인화 plan 을 Telegram 으로 보고하고, 사용자 승인 시 라벨/archive 를 자동 실행하는 비서. 모든 결정·분류·실행 로직은 외부 Python runner (run.py) 에 있음. 본 SKILL.md 는 의도적으로 짧음 — agent reasoning 자유도를 0 에 가깝게 줄여 결정성 보장.
allowed_tools: [bash]
---

# gws-assistant

Gmail 의 unread 메일을 평일 근무시간에 3분류(불필요/보류/진행) 으로 분류한 브레인화 plan 을 Telegram 으로 보고하고, 사용자 승인 시 라벨/archive 를 자동 실행하는 비서. 모든 결정·분류·실행 로직은 외부 Python runner (run.py) 에 있음. 본 SKILL.md 는 의도적으로 짧음 — agent reasoning 자유도를 0 에 가깝게 줄여 결정성 보장.

운영 계정: `kimbi.kirams@gmail.com` 만.

## 호출 패턴

agent 가 받는 슬래시 명령 → 그대로 `run.py` 의 첫 인자로 forward. 인자가 여러 개면 모두 그대로 forward.

| 사용자/cron 메시지 | 실행 |
|---|---|
| `/gws-assistant` (cron 폴) | `python3 ~/.openclaw/workspace/skills/gws-assistant/run.py` |
| `/gws-assistant approve` | `python3 ~/.openclaw/workspace/skills/gws-assistant/run.py approve` |
| `/gws-assistant confirm <thread_id>` | `python3 … run.py confirm <thread_id>` |
| `/gws-assistant edit <thread_id> folder=<경로> links=<[[A]],[[B]]>` | `python3 … run.py edit <thread_id> folder=… links=…` |
| `/gws-assistant skip <thread_id>` | `python3 … run.py skip <thread_id>` |
| `/gws-assistant cancel` | `python3 … run.py cancel` |
| `/gws-assistant snooze 60` | `python3 … run.py snooze 60` |
| `/gws-assistant status` | `python3 … run.py status` |
| `/gws-assistant migrate-inbox` | `python3 … run.py migrate-inbox` (dry-run, 계획만 출력) |
| `/gws-assistant migrate-inbox --apply` | `python3 … run.py migrate-inbox --apply` (실제 이동) |

## 출력 처리 규칙

명령 결과 처리는 정확히 두 가지뿐:

- **stdout 이 비어 있음** → **답장하지 마세요.** 어떤 텍스트도 출력하지 마세요. `NO_REPLY` 도, "조용히 종료했습니다" 같은 안내도, 빈 줄도 출력 금지.
- **stdout 에 텍스트가 있음** → **그 텍스트를 그대로 출력하세요.** 앞뒤에 prefix·suffix·요약·해석·코드블록 마크다운(```) 등 어떤 추가도 금지. 글자 단위 그대로 복제.

이 두 분기 외에는 어떤 추론·판단도 하지 마세요.

## 강제 폴 (수동 테스트용)

```bash
python3 ~/.openclaw/workspace/skills/gws-assistant/run.py --force-poll
```

게이트(평일/근무시간/공휴일/미팅중) 와 snooze 를 모두 무시하고 강제 발화. 위 출력 처리 규칙 동일.

## 운영 모델 요약

- **폴링**: 평일 10분 cron. 게이트(평일/근무시간/공휴일/미팅 중-`판독`예외) 통과 시에만 활성.
- **분류 (Phase 1)**: 휴리스틱(from 패턴 매칭) → `noise` 자동 식별 + 나머지는 Haiku batch 호출로 `proceed`/`pending`/`noise` 3 카테고리. vault `gmail-capture.md` §1·§2 동적 주입.
- **plan 영속**: `~/.openclaw/agents/main/memory/gws-assistant.json` 의 `pending_plan` 필드. 명시 폐기/승인까지 살아있음, 자동 만료 없음.
- **머지**: 신규 메일이 도착하면 기존 pending plan 에 머지하여 한 메시지로 발화. 같은 msg_id 반복 발화 안 함.
- **승인 흐름 (Phase 2 — 큐 모델)**:
  1. `/gws-assistant approve` 시 `pending`/`noise` 항목은 라벨/archive 일괄 처리. `proceed` 항목은 큐로 진입.
  2. 첫 `proceed` 항목 → 본문 fetch + Opus 4.7 동반 노트 작성(PARA 좌표·연결 후보 LLM 추론 포함) → vault `sources/00_inbox/` atomic write (staging) → **1건 보고만 출력 (라벨/archive 안 함)**.
  3. 사용자가 보고에 `confirm`/`edit`/`skip` 중 하나로 응답 →
     - confirm/edit 시: **노트 → `knowledge/<PARA>/`, 첨부 → `sources/<PARA>/` 로 이동** (gog prefix 제거 + 동일 (filename, size) dedupe → 중복분은 `_attachments/_dup/` 격리), frontmatter `sources:` 필드를 새 경로로 갱신, 라벨+archive 수행.
     - skip 시: 노트 삭제 + 라벨 `브레인화/보류` (inbox 유지).
  4. 다음 큐 1건 propose → 또 1건 보고. 큐 소진 시 완료 메시지.
- **노트 작성 모델**: Opus 4.7 (`build_companion_note_llm`). 분류는 Haiku 4.5.
- **마이그레이션**: `migrate-inbox` 명령으로 `sources/00_inbox/` 잔존 노트들을 `proposed_para_path` 기반 일괄 정리. dry-run 기본, `--apply` 시 실행.

## 자세한 사양

전부 `run.py` 의 모듈 docstring 과 상수 정의에 있음. vault 의 [01_projects/openclaw-gws-assistant/SESSION-HANDOFF.md](~/projects/2nd-brain-vault/knowledge/01_projects/openclaw-gws-assistant/SESSION-HANDOFF.md) 가 Phase 1 사양의 권위 문서.
