---
name: g
description: Gmail/gws-assistant 자연어 피드백 entry point. 사용자가 입력한 짧은 한국어를 정규식 fast-path 또는 Haiku 4.5 의도 파싱으로 gws-assistant 명령에 매핑해 실행. 비가역 동작은 1턴 확인 후 실행. **자연어 트리거 (슬래시 없는 발화)**: 사용자가 "지메일 브리핑해줘", "메일 브리핑", "메일 다시 분류해줘", "인박스 brief" 류로 슬래시 없이 자연어만 보내도 그대로 `/g 다시 분류` 호출 (= 강제 폴링·게이트 무시 후 분류 plan 출력). 본 SKILL.md 는 의도적으로 짧음 — agent reasoning 자유도를 0 에 가깝게 줄여 결정성 보장.
allowed_tools: [bash]
---

# g — gws-assistant 자연어 entry point

`gws-assistant` 명령을 짧은 한국어로 호출하기 위한 alias 스킬. 모든 의도 파싱·실행 로직은 외부 Python runner (`run.py`) 에 있음.

## 호출 패턴

agent 가 받는 슬래시 명령 → 그대로 `run.py` 에 forward.

| 사용자 메시지 | 실행 |
|---|---|
| `/g <한국어 텍스트>` | `python3 ~/.openclaw/workspace/skills/g/run.py <한국어 텍스트>` |
| `/g` (인자 없음) | 도움말 출력 |

## 자연어 트리거 (슬래시 없는 발화)

사용자가 슬래시 없이 다음 의미의 한국어를 보낼 경우, agent 는 다른 어떤 응답도 추가하지 말고 **즉시 `python3 ~/.openclaw/workspace/skills/g/run.py 다시 분류`** 를 호출하라 (= `/gws-assistant --force-poll` 과 동일).

| 자연어 발화 (예시) | 매핑 |
|---|---|
| "지메일 브리핑해줘" | `python3 … g/run.py 다시 분류` |
| "메일 브리핑", "메일 브리핑해줘" | 동일 |
| "받은편지함 정리해줘", "받은편지함 분류" | 동일 |
| "메일 다시 분류해줘", "다시 분류" | 동일 |
| "인박스 brief", "인박스 브리핑" | 동일 |

판단 원칙:
- "Gmail/메일/지메일/받은편지함/인박스" 어휘 + "브리핑/요약/분류/정리" 어휘가 함께 나오면 위 매핑.
- 모호하면 (예: 단순 "메일?", "오늘 뭐 와있나") 매핑하지 말고 평문 응답.
- 위 매핑에 해당하면 agent 의 다른 추론·해석·prefix·suffix 추가 없이 위 bash 한 줄만 실행.

## 출력 처리 규칙

명령 결과 처리는 정확히 두 가지뿐 (gws-assistant 와 동일):

- **stdout 이 비어 있음** → 답장하지 마세요. 어떤 텍스트도 출력하지 마세요.
- **stdout 에 텍스트가 있음** → 그 텍스트를 그대로 출력하세요. 앞뒤에 prefix·suffix·요약·해석·코드블록 마크다운(```) 등 어떤 추가도 금지. 글자 단위 그대로 복제.

이 두 분기 외에는 어떤 추론·판단도 하지 마세요.

## 지원 입력 예시 (참고용 — 강제 아님)

```
# 검토 항목에 답할 때 (모두 즉시 종결 또는 자동 종결) — 7개 canonical 명령
/g 확정 (= ok)                       — 노트 PARA 이동 + '브레인화/완료'
/g 답장 (= reply)                    — Drafts 등록 + awaiting_reply (발송 자동 감지로 종결)
/g 답장할일 [YYYY-MM-DD]             — 답장 + Google Tasks 등록 (마감일 인자/LLM 추출/없음)
/g 할일 [YYYY-MM-DD] [note] (= task) — Google Tasks 등록 + 즉시 종결 (메모는 Tasks notes 에 append)
/g 경로수정 folder=knowledge/02_areas/grants/ — PARA 변경 + 즉시 종결
/g 보류                              — 노트 삭제 + 보류 라벨 (inbox 유지)
/g 불필요                            — 노트 삭제 + 불필요 + archive

# 위 표는 도움말용 canonical 만 노출. 파서는 더 많은 한국어/영어 alias 도 인식 —
# 예/네/맞아/좋아/승인/confirm/approve, skip/나중에, 폐기/dismiss, reply-task 등.

# plan/분류 일괄
/g 맞아                     — 현재 plan 일괄 처리 (= /gws-assistant approve)
/g 다시 분류                — 강제 폴링
/g 취소                     — plan 폐기
/g 상태                     — 현재 plan/awaiting_reply 상태

# 다른 메일 교정
/g 진행 19c55ca2            — id 의 메일을 진행으로 교정
/g 보류 19c55ca2 학회공문    — 교정 + 메모

# 자연어 (Tier 2 LLM 파싱, 1턴 확인 후 실행)
/g 두 번째는 보류로
/g 정중하게 거절 답장
/g 5월 15일까지 답장하고 할일도 등록
```

## 자세한 사양

`run.py` 의 모듈 docstring 참조.
