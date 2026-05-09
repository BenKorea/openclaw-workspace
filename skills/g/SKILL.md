---
name: g
description: Gmail/gws-assistant 자연어 피드백 entry point. 사용자가 입력한 짧은 한국어를 정규식 fast-path 또는 Haiku 4.5 의도 파싱으로 gws-assistant 명령에 매핑해 실행. 비가역 동작은 1턴 확인 후 실행 (옵션 A). 본 SKILL.md 는 의도적으로 짧음 — agent reasoning 자유도를 0 에 가깝게 줄여 결정성 보장.
allowed_tools: [bash]
---

# g — gws-assistant 자연어 entry point

`gws-assistant` 명령을 짧은 한국어로 호출하기 위한 alias 스킬. 모든 의도 파싱·실행 로직은 외부 Python runner (`run.py`) 에 있음.

## 호출 패턴

agent 가 받는 슬래시 명령 → 그대로 `run.py` 에 forward. 인자 여러 토큰이면 모두 그대로 forward — Python 쪽에서 join 해 단일 텍스트로 처리.

| 사용자 메시지 | 실행 |
|---|---|
| `/g <한국어 텍스트>` | `python3 ~/.openclaw/workspace/skills/g/run.py <한국어 텍스트>` |
| `/g` (인자 없음) | `python3 ~/.openclaw/workspace/skills/g/run.py` (도움말 출력) |

## 출력 처리 규칙

명령 결과 처리는 정확히 두 가지뿐 (gws-assistant 와 동일):

- **stdout 이 비어 있음** → 답장하지 마세요. 어떤 텍스트도 출력하지 마세요.
- **stdout 에 텍스트가 있음** → 그 텍스트를 그대로 출력하세요. 앞뒤에 prefix·suffix·요약·해석·코드블록 마크다운(```) 등 어떤 추가도 금지. 글자 단위 그대로 복제.

이 두 분기 외에는 어떤 추론·판단도 하지 마세요.

## 지원 입력 예시 (참고용 — 강제 아님)

```
/g 맞아                    — 현재 plan 일괄 처리 (= /gws-assistant approve)
/g 진행 19c55ca2          — 메일을 진행으로 교정 (= correct)
/g 보류 19c55ca2 학회 공문 — 교정 + 메모
/g 다시 분류              — 강제 폴링
/g 취소                   — plan 폐기
/g 상태                   — 현재 plan 상태
/g 두 번째는 보류로        — Tier 2 LLM 파싱 (1턴 확인 후 실행)
```

## 자세한 사양

`run.py` 의 모듈 docstring 참조.
