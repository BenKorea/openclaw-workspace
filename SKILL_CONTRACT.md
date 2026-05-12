---
name: SKILL_CONTRACT
description: 모든 OpenClaw workspace skill 이 따르는 공통 기술 계약 — frontmatter·출력·인자·테스트·영속 상태·안전·vault 통합 invariants. spec-driven 의 L1 layer.
---

# SKILL_CONTRACT — workspace skill 공통 계약

이 workspace (`~/.openclaw/workspace/`) 안 모든 skill 이 *암묵적으로* 따르는 기술적 invariant. 각 SKILL.md 는 이 계약을 *반복 명시할 필요 없이* 따른다. 본 파일이 권위 원본 — invariant 가 바뀌면 본 파일이 먼저, skill SKILL.md 들이 이후 정렬.

> 본 계약은 *어디서 개발할지·dev/prod 분리* 같은 **방법론·정책** 과는 다른 layer. 방법론 측 권위 원본은 `~/projects/2nd-brain-vault/knowledge/02_areas/brain-system/workflows/openclaw-skill-dev.md` (vault).

## 적용 범위

- `~/.openclaw/workspace/skills/<name>/` 아래 *모든* skill.
- 본 계약과 다른 동작이 필요한 skill 은 *그 SKILL.md 에서 명시적으로 예외 선언* (아래 § 예외 선언 패턴 참조).

---

## L1 invariants

### 1. SKILL.md frontmatter

**필수 필드**:

- `name` — 폴더명과 동일. hyphen-case (소문자·숫자·하이픈).
- `description` — 1줄 요약. agent 가 트리거 판단의 1차 자료이므로 *동작 목적 + 호출 트리거* 가 한 문장에 담겨야 함.
- `allowed_tools` — Bash 기반 skill 은 `[bash]` 명시. 다른 도구가 필요하면 명시.

**선택 필드**:

- `status: design-only` — 설계 완료·구현 미착수 표시. agent 호출 차단 신호.
- `metadata` — OS 필터·필요 bin·필요 config 키 (OpenClaw 공식 권장 형식).

### 2. 호출 패턴 (Python 기반 skill 표준)

표준 패턴:

```
python3 ~/.openclaw/workspace/skills/<name>/run.py [args...]
```

agent 는 사용자의 슬래시 명령을 **글자 단위 그대로** run.py 에 forward. 의도 파싱·분류·실행 로직은 모두 run.py 에 위치 — *agent reasoning 자유도 최소화* 가 결정성 보장의 핵심.

**예외 패턴**:

- 외부 CLI 래핑 (예: `gog`) — run.py 없이 SKILL.md 가 CLI 사용 안내.
- 비-Python 런너 (예: `web-fetch` 의 `node ./fetch.js`) — 자체 호출 패턴.

이 예외들은 SKILL.md 에 *명시적으로 호출 패턴* 을 적는다.

### 3. stdout 출력 규칙 (가장 엄격한 invariant)

agent 가 run.py 의 stdout 을 처리하는 분기는 정확히 둘. 그 외 *어떤 추론·판단도 금지*:

- **stdout 이 비어 있음** → agent 는 *어떤 텍스트도 출력 금지*. `NO_REPLY`·"조용히 종료했습니다" 류 안내·빈 줄 모두 금지. 침묵이 정상 종료를 의미한다.
- **stdout 에 텍스트** → agent 는 그 텍스트를 *글자 단위 그대로* 출력. prefix·suffix·요약·해석·코드블록 마크다운(```) 어떤 추가도 금지.

이 규칙은 skill 의 **결정성** 보장. 모델 버전·온도·세션 컨텍스트 변화에도 *동일 입력 → 동일 출력* 유지. 사용자 알림이 필요하면 *run.py 가 stdout 으로* 출력하고 agent 는 forward 만.

### 4. exit code

- `0` — 정상 종료 (성공 또는 정상 무동작).
- `0` 외 — 비정상. agent 는 *exit code 만으로는 사용자에게 알리지 않음* — stdout 이 비어 있으면 위 §3 에 따라 침묵. 사용자 알림이 필요한 비정상이면 run.py 가 stdout 으로 메시지를 출력하고 그 뒤 exit.

### 5. 수동 테스트 (dev 단위 테스트)

각 run.py 는 *agent 매개 없이* 직접 호출 가능해야 한다:

```bash
python3 ~/.openclaw/workspace/skills/<name>/run.py [--force-...] [args...]
```

`--force-...` 류 플래그로 *cron 게이트·snooze·시간 조건* 등의 자동 동작 우회 가능. 이게 dev/prod 분리의 단위 테스트 단계. 자세한 워크플로우는 vault 의 `workflows/openclaw-skill-dev.md`.

기존 패턴:

- `gws-assistant/run.py --force-poll` — cron 게이트 무시.
- `people-meeting-brief/run.py --force-event <eventId>` — 캘린더 시간 조건 무시.

### 6. 영속 상태

상태 저장이 필요한 skill 은 다음 표준 위치에 단일 JSON 파일로:

```
~/.openclaw/agents/main/memory/<skill-name>.json
```

- 읽기 시 *파일 부재* 또는 *키 부재* = 첫 호출. graceful default.
- 이 위치는 OpenClaw agent 메모리 layer — git 미추적, 머신별 독립 (다중 PC 동기 안 됨).
- skill 마다 *한 파일* 권장. 너무 커지면 subdirectory 로 분할.

### 7. 안전·secret

**입력 escape**:

- run.py 가 인자·표준입력·파일로 받는 텍스트를 shell·SQL·외부 API 에 *명령으로 주입* 하지 않도록 처리. 특히 `subprocess`·`os.system` 사용 시 인자 분리 (`shell=False` + 리스트 인자).

**자격증명**:

- OAuth token·비밀번호·TOTP·API key 는 `~/.openclaw/secrets/<skill-name>.toml` 같은 **chmod 600** 파일에 저장. run.py 가 read-and-use.
- 자격증명은 *어떤 경로로도 stdout·log·session 으로 흐르지 않음* — 모델 컨텍스트 비노출이 절대 invariant.
- 운영 계정이 한정된 skill 은 SKILL.md 에 명시 (예: gws-assistant 의 "운영 계정: kimbi.kirams@gmail.com 만").

### 8. vault 통합

- vault (`~/projects/2nd-brain-vault/`) 의 콘텐츠 (knowledge 노트·sources 원본) 는 **단일 권위 원본 (SSOT)**. skill 은 vault 를 *읽거나 쓸 수 있지만*, vault 의 의미·구조를 *권위로 인정* 하고 자기 안에 중복 정의하지 않는다.
- vault 파일을 *동적 로드* 하는 경우 (예: gws-assistant 가 `gmail-capture.md` 의 §1·§2 인입), 파일·섹션 부재 시 graceful fallback (빈 문자열 + 호출자 측 default).
- vault 경로는 `~/projects/2nd-brain-vault/...` 사용 (호스트·컨테이너 양쪽에서 동일 상대 경로로 작동).

---

## L2 — per-skill SKILL.md 권장 구조

본 L1 invariant 위에서, 각 SKILL.md 는 다음 섹션을 권장한다 (spec-driven 의 *행동 정의*):

1. `# <name>` — 짧은 식별·정신 모델 (2~3줄).
2. **호출 패턴** — 사용자 메시지 → run.py 인자 매핑 표.
3. **Acceptance examples** — 3개 정도: 정상 path, 빈 입력/0건, 에러 케이스. *입력 → 기대 stdout* 명시.
4. **Manual test commands** — 위 §5 의 직접 호출 명령. acceptance examples 를 *재현* 할 수 있어야 함.
5. (선택) **출력 규칙 예외** — L1 §3 와 다르면 명시.
6. (선택) **상태·자격증명·외부 의존** — skill 특유 사항.

§3 (Acceptance examples) 와 §4 (Manual test) 가 **spec-driven 의 핵심**. 구현 전에 §3 를 적고, §4 로 검증 가능하게 둠. SKILL.md 가 곧 spec — 별도 spec doc·별도 test 파일 만들지 않음.

---

## L3 — smoke check (배포 후)

dev/prod 워크플로우의 `/cron on` 직후 Telegram 으로 *가장 짧은 명령* 한 번 발사 → 응답이 정상이면 통합 OK. 무응답·이상이면 dev 단계 누락 — `/cron off` 로 돌아가 재진단.

(자세한 dev/prod 워크플로우: vault `workflows/openclaw-skill-dev.md`)

---

## 예외 선언 패턴

본 L1 과 다른 행동이 정당한 경우, 해당 SKILL.md 에 *명시적으로 예외 선언*:

```markdown
## 예외 — L1 §3 (stdout 규칙)

본 skill 은 L1 §3 (빈 stdout → 침묵) 을 따르지 않는다. 빈 출력 시에도 agent 는
"(완료, 보고 없음)" 을 명시적으로 출력한다. 이유: <왜 — 예: 사용자 안전 신호 필요>.
```

예외 없는 한 L1 이 자동 적용된다. 예외 선언이 누적되면 본 파일을 갱신할 신호 — L1 invariant 가 더 이상 *공통* 이 아니라는 뜻.

---

## 메타

- 2026-05-12 — 최초 작성. 기존 7개 skill (`g`·`gog`·`gws-assistant`·`people-meeting-brief`·`shikamaru-web-fetch`·`society-watch`·`webmail-watch`) 의 SKILL.md 에서 *반복 등장* 하는 invariant 를 추출·명문화. spec-driven 워크플로우의 L1 layer 로 도입.

- 출처 매핑:
  - §1 frontmatter: 7개 skill 의 frontmatter 형식 분석.
  - §2 호출 패턴: 5개 Python skill 의 공통 `python3 .../run.py <args>` 패턴.
  - §3 출력 규칙: `gws-assistant/SKILL.md` 에 명시·`people-meeting-brief` 가 "동일" 참조·`g` 가 SKILL.md 단축 의도로 함축.
  - §5 수동 테스트: `--force-poll` (gws-assistant) / `--force-event` (people-meeting-brief) 패턴.
  - §6 영속 상태: `society-watch`·`gws-assistant` 가 모두 `~/.openclaw/agents/main/memory/<name>.json` 사용.
  - §7 자격증명: `webmail-watch` 의 toml + chmod 600 + 모델 비노출 패턴 (2026-05-10 채택).
  - §8 vault 통합: `gws-assistant` 의 vault `gmail-capture.md` 동적 인입·`society-watch` 의 vault `sources/00_inbox/` 드롭·`people-meeting-brief` 의 vault 인맥 노트 스캔.

- 본 파일은 openclaw-workspace repo 의 일부 — 다중 PC 자동 동기 (git), main 에이전트 컨텍스트 자동 로드.

- 수정 시: invariant 가 바뀌면 본 파일 먼저, 그 다음 기존 SKILL.md 들 정렬. 새 skill 작성 시 본 파일 + L2 권장 구조 따름.

- 작성자: Dr. Ben + Claude.
