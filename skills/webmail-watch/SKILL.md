---
name: webmail-watch
description: 외부 forwarding/IMAP 이 차단된 기관·기업 webmail 을 polling 하여 받은편지함의 메일을 webmail UI 의 "전달" 버튼 click sequence 로 본인 Gmail 로 re-forward 하고, 직후 webmail 의 "Gmail" 보관함으로 이동시켜 받은편지함에서 사라지게 한다. 받은편지함이 곧 pending queue. 자격증명 (ID·PW·TOTP) 은 단일 chmod 600 toml 파일로 통합되어 모델 컨텍스트에 평문 미노출.
---

# webmail-watch

기관·기업 webmail (외부 forwarding/IMAP 차단된 인스턴스) 을 정기 polling 하여 받은편지함의 메일을 **webmail UI 의 "전달" 버튼 click sequence** 로 본인 Gmail 계정으로 re-forward. 외부 SMTP/API 발송 ✗ — 사람이 손으로 "전달" 클릭하는 행위 자체를 자동화. **출력 이후** — 재포워딩된 `[KIRAMS-FWD]` 메일은 표준 Gmail 파이프라인으로 흡수: 라벨링 → `gmail-label-actions`(캡처·후속작업) → `brainify`(분류·노트화).

**핵심 정신 모델 — 받은편지함 = pending queue**

받은편지함에 있는 메일 = 미처리. forward 후 webmail 의 "Gmail" 보관함으로 즉시 이동시켜 받은편지함에서 사라지게 한다. 사람이 KIRAMS 받은편지함을 직접 열었을 때도 미처리 메일만 보임. `last_message_id` 류 추적 ✗ — 받은편지함의 모든 row 가 신규.

**Phase 1**: KIRAMS (한국원자력의학원) MailPlug 호스팅 webmail. 안정 검증 후 다중 tenant 확장.

## 입력

- `tenant` (필수): webmail tenant 키. 미지정 시 `kirams` (default).
- `--limit N` (선택, 2026-05-17): **수동 on-demand** — 이번 실행만 forwarding 건수를 N 으로 오버라이드 (미지정 = 스케줄 기본 `tenant.poll_limit`=3). 수동 호출은 본래 *즉시·스케줄 무관* 이므로, **발화시각이 아니어도 `/webmail-watch kirams --limit N` 으로 즉시 N건 포워딩**. cron 자동발화(인자 없음)는 기본 3건 불변.
- `--bootstrap` (선택): 1회 수동 로그인(headed). `--verbose`: 디버그 로그.

### cron 발화 (2026-05-17 변경)

`gmail-label-actions-poll`·cron-inventory 와 별개. schedule = `0 8-18 * * 1-5` (Asia/Seoul) = **평일 08–18시 1시간 간격**(2026-05-17: 30분→1시간, 이어 17→18시 확장; 일 11회). 자동발화 = 기본 3건 forwarding, Telegram 침묵.

## Tenant registry

| tenant 키 | 기관 | 진입 URL | 호스팅 패턴 | secret 파일 |
|---|---|---|---|---|
| `kirams` | 한국원자력의학원 | `https://mail.kirams.re.kr/member/login` | `https://zm\d+.mailplug.com/...?host_domain=kirams.re.kr` | `~/.openclaw/secrets/webmail-watch-kirams.toml` |

확장 시 행 추가. 진입·호스팅 도메인 둘 다 SSRF allowlist 등재 필요.

## 자격증명 — 단일 toml 파일 (절대 모델 노출 ✗)

ID/PW/TOTP 모두 한 파일 (`chmod 600`) 에 통합. run.py 가 read-and-use, 어느 값도 stdout/log 로 흐르지 않음. 외부 발송 채널이 없으므로 SMTP/OAuth 자격증명 자체가 불필요.

### secret 파일 형식

```toml
[KIRAMS]
otp_issuer  = "MailPlug"
otp_account = "<your-id>@kirams.re.kr"
otp_secret  = "<base32 secret>"
otp_type    = "totp"
otp_digits  = 6
otp_period  = 30
login_pw    = "<webmail password>"
```

- 섹션 키 (`[KIRAMS]`) 는 tenant key 와 case-insensitive 매칭. 평탄 구조 (섹션 ✗) 도 fallback.
- KeePassXC OTP export 형식과 호환 — `otp_issuer` / `otp_account` / `otp_type` 은 메타로 무해 보존.
- `login_pw` 만 사용. ID 는 webmail 의 "아이디저장" 쿠키로 prefill (Chrome PM 과 별개 — webmail 자체 cookie).
- Dr. Ben 이 직접 생성·관리. 모델은 path 만 알고 평문 미노출.

### Why 이 패턴 (옵션 C, 2026-05-10 채택)

초기 설계는 ID/PW 를 Chrome password manager 에 두고 autofill 의존했으나 headless cron 에서 autofill 발동 보장 어려움 + 환경 의존성. TOTP secret 이 이미 평문 toml 에 있으므로 PW 추가 시 attack surface 거의 ✗. 결정성 + Chrome 환경 의존성 제거가 핵심 이득.

master PW vault (KeePassXC unlock / Bitwarden CLI) 는 자동화 트랙에선 master PW unlock 부담으로 부적합.

## 의존성 — uv project mode

OpenClaw 외부-의존성 skill 의 첫 사례. **정책 선례**: 외부 python 의존성을 쓰는 skill 은 skill 디렉토리 안에 `pyproject.toml` + `uv.lock` 으로 격리.

- **의존성 SoT**: `pyproject.toml` (skill 디렉토리 안)
- **venv 위치**: `<skill>/.venv/` (uv 가 자동 생성·관리)
- **잠김**: `uv.lock` (재현성)
- **외부 의존성**: `pyotp` (TOTP), `playwright` (브라우저 자동화)
- **stdlib 만 쓰는 skill 은 venv 불필요** (gmail-label-actions·society-watch 와 일관)

**최초 설치**:

```bash
cd ~/.openclaw/workspace/skills/webmail-watch
uv sync
uv run playwright install chromium
```

**호출 형식**:

```bash
cd ~/.openclaw/workspace/skills/webmail-watch && uv run run.py <tenant>
cd ~/.openclaw/workspace/skills/webmail-watch && uv run run.py <tenant> --bootstrap
```

> `uv run --project <path> run.py` 만으로는 동작 ✗ — `--project` 는 venv 위치만 지정, script path 는 cwd 기준 lookup. 절대경로로 부르려면 `uv run --project <path> <path>/run.py` 처럼 둘 다 적어야 함. cron wrapper 는 `cd ... && uv run run.py ...` 형태로 prepend 하여 동작.

## Browser 자동화 — Playwright + 전용 프로필

run.py 가 Playwright (Chromium bundled) 로 브라우저를 띄움. OpenClaw 메인 browser 와 별개.

- **userDataDir**: `~/.openclaw/skills/webmail-watch/chrome-profile/<tenant>/`
- **headless**: cron 시 `True`, bootstrap 시 `False`
- **WSL headless 안정성**: `args=["--no-sandbox", "--disable-dev-shm-usage"]`
- **selector 전략**: 안정 ID + `[role]` + `[title]` 우선. class hash (`_root_8pv7d_1` 등 build hash) 는 텍스트 기반으로 회피.

## Bootstrap (1회 수동 로그인)

새 tenant 등록 또는 webmail 의 "아이디저장" 쿠키 갱신 필요 시:

```bash
cd ~/.openclaw/workspace/skills/webmail-watch && uv run run.py kirams --bootstrap
```

동작:
1. WSLg 가 떠 있어야 함 (headed 모드)
2. Playwright 가 webmail-watch 프로필을 headed 로 띄우고 진입 URL 자동 오픈
3. **첫 진입 시**: 사람이 ID 입력 + "아이디저장" 체크 1회 (이후 prefill 영속)
4. run.py 가 secret 파일에서 PW 자동 fill → 로그인 버튼 → OTP 자동 fill → 받은편지함 도달
5. 사람이 창 닫음 → 프로필에 세션 쿠키 + 아이디저장 쿠키 영속화
6. cron headless 호출은 이 프로필 재사용

## 영속 상태

`~/.openclaw/agents/main/memory/webmail-watch.json`:

```json
{
  "kirams": {
    "last_checked": "2026-05-10T21:30:36+09:00"
  }
}
```

`last_message_id` 필드 ✗ — 받은편지함 = pending queue 모델에선 "어디까지 처리했는지" 추적이 무의미. 받은편지함의 모든 row 가 신규. `last_checked` 는 운영 모니터링 (cron 정상 동작 확인) 용도만.

## 절차

`run.py <tenant>` 호출 시 내부적으로 수행:

### Step 1 — Webmail 진입 + 세션 확인

진입 URL 오픈 → 받은편지함 marker (`tbody tr[data-index]`) 가 잡히면 Step 3, 로그인 폼 (`#cpw`) 보이면 Step 2.

### Step 2 — 자동 재로그인 (toml 기반)

1. ID 는 webmail "아이디저장" 쿠키로 prefill 신뢰
2. secret toml 의 `login_pw` 를 `#cpw` 에 직접 fill
3. `#btnlogin` click
4. OTP 페이지 (`#otp_code1`) 출현 시:
   - secret toml 의 `otp_secret` 으로 `pyotp.TOTP(...).now()` 6자리
   - 자동 fill → `Enter` 키로 form submit (KIRAMS 는 별도 submit 버튼 ✗)
5. 받은편지함 도달 확인. 실패 → §Step 7 `auth_failed` 알림 후 종료.

### Step 3 — 받은편지함 처리 루프 (poll_limit=3)

각 회차:

1. `_ensure_inbox` — 받은편지함 marker wait + UI idle wait (로딩 오버레이 / headlessui-portal toast 정착)
2. `tbody tr[data-index="0"]` 첫 row 존재 확인. **0건 → 즉시 break** (받은편지함 빔 = 처리 완료)
3. row 의 발신자 (`[title]`) + 제목 (`[role="button"] span.break-all`) 메타 추출
4. row 클릭 (`[role="button"][tabindex="0"]`) → SPA URL `/mail/inbox/messages/<mid>` 변경
5. URL 정규식으로 `mid` 추출 (Telegram 알림 메타용 — Telegram 침묵 정책이라 실제 발송 ✗)
6. **forward** (§Step 4) → **이동** (§Step 5) 통과 후 다음 회차

forward 또는 이동 실패 → break (사람 개입 신호). 다음 cron 회차에 dup forward 가능성 있지만 옵션 1 (dup 허용) 정책.

### Step 4 — KIRAMS UI 전달 click sequence

detail page 진입 상태에서:

1. UI idle wait (detail 진입 직후 로딩 오버레이 정착)
2. `button:has-text("전달")` 클릭
3. 받는사람·제목 form 진입 wait (`#toRecipients-input` 등장)
4. **제목 prefix 먼저** (`#input-subject` 의 자동 입력된 `[FW]<원제목>` 앞에 `[KIRAMS-FWD] ` prepend) — chip Enter 가 우발적 form submit 해도 prefix 적용된 상태로 발송하는 안전망
5. 받는사람 chip 입력 (`#toRecipients-input` 에 `kimbi.kirams@gmail.com` fill → `Tab` 으로 chip 변환). **`Enter` ✗** — Enter 는 chip 변환 + form submit 둘 다 trigger 하여 dup 발송 위험.
6. 본문·첨부는 KIRAMS UI 가 자동 인용·자동 포함 (사람의 "전달" 동작과 동일)
7. `button:has-text("보내기")` 클릭
8. 받은편지함 자동 복귀 wait

### Step 5 — Gmail 보관함으로 이동

받은편지함 복귀 상태에서:

1. 첫 row 의 체크박스 label (`label[for="toggle-0"]`) click → 선택 (input 자체는 sr-only)
2. toolbar 의 `button:has-text("이동")` 클릭 → dropdown 열림
3. dropdown 컨테이너 (`[aria-labelledby="dropdown-toggle-move"][role="menu"]`) 등장 wait
4. `[aria-labelledby="dropdown-toggle-move"] a:has-text("Gmail")` 클릭
5. dropdown 닫힘 + 받은편지함 marker 재안정 wait (그 row 가 사라지고 다음 row 가 위로 올라옴)

### Step 6 — 영속 상태 갱신

`webmail-watch.json` 의 `last_checked` 만 갱신. `last_message_id` 가 살아있으면 pop. atomic write (temp + rename).

### Step 7 — Telegram 알림

**침묵 정책** — `notify_telegram` 은 stderr `log.info` 로만 흘림 (stdout 침묵). cron job `delivery.mode="none"` 으로 이중 안전. KIRAMS forwarding 결과는 gmail-label-actions Gmail 브리핑에 자연 흡수.

| 케이스 | 메시지 | 발송 |
|---|---|---|
| 정상 N≥1 forwarding | `📨 KIRAMS webmail 신규 N건 → forwarding 완료` | log.info (stderr) |
| 정상 0건 (받은편지함 빔) | — | 알림 ✗ |
| `auth_failed` | `⚠ KIRAMS webmail auth_failed` | log.info |
| `process_failed: <ExceptionType>` | `⚠ KIRAMS webmail process_failed: ...` | log.info |
| partial failure | `⚠ KIRAMS K건 forwarding 실패 — 다음 회차 재시도` | log.info |

verbose 모드 (`--verbose`) 시 stderr 에 노출. 운영 모니터링은 cron run history (`mcp__openclaw__cron action=runs`) 와 `webmail-watch.json` 의 `last_checked` 갱신 여부로.

## 발송 메커니즘 — KIRAMS UI 전달 click-driven + 보관함 이동

외부 SMTP/API 발송 ✗. KIRAMS webmail UI 의 "전달" 버튼 + "이동" 버튼을 Playwright 가 사람처럼 클릭. society-watch 가 학회 사이트의 "다운로드" 버튼을 클릭하는 것과 동일 패턴.

**식별 방식 (gmail-label-actions 인식용)**:
- 발신자 도메인 `@kirams.re.kr` (KIRAMS 가 직접 발송)
- Subject prefix `[KIRAMS-FWD]`
- 임의 헤더 (`X-Webmail-Source` 등) 주입 ✗ — KIRAMS 가 발송 주체라 외부 헤더 추가 불가

**Idempotency**: 받은편지함 = pending queue 모델로 자연 보장. forward + 이동 성공 = 받은편지함에서 사라짐 = 다음 회차에 재처리 ✗. 이동 실패 시 받은편지함에 남음 → 다음 회차 재 forward → kimbi.kirams 측 dup 1건 (KIRAMS 트래픽 작아 무해 가정).

**보낸편지함 누적**: forward 한 메일이 KIRAMS 보낸편지함에 쌓임. 운영 후 부담되면 별도 정리 phase 추가.

**자격증명**: 외부 발송 채널 없음 → SMTP/OAuth 자격증명 ✗.

## Failure mode 매핑

| 실패 | 처리 |
|---|---|
| webmail 세션 만료 | 자동 재로그인 (toml 기반 PW + TOTP). 실패 → `auth_failed` 종료 |
| `login_pw` fill 실패 (selector 변경 또는 KIRAMS form 변경) | `auth_failed` |
| TOTP 코드 거부 (시간 동기화 drift) | `auth_failed` (재시도는 cron 다음 회차) |
| Captcha 출현 | 받은편지함 marker 미도달 → `auth_failed` (실제로는 사람 개입 필요) |
| listing/detail 페이지 구조 변경 | `process_failed: PlaywrightTimeoutError`. selector 점검 |
| chip 입력 변환 실패 (Tab 으로도 안 변환) | 보내기 click 시 form validation → `forward` 단계 실패 → 다음 회차 재시도 |
| `forward` 실패 (메시지 listing 에 남음, 다음 회차 재시도 — dup ✗) | 그 회차 break, partial failure |
| `forward` 성공 + `move` 실패 (메시지 listing 에 남음, 다음 회차 재 forward — dup 1건) | 그 회차 break. KIRAMS 트래픽 작아 무해 가정 |
| MailPlug 인스턴스 번호 변경 (zm132 → zm133 등) | 진입 URL `mail.kirams.re.kr` 고정 → redirect 자동 흡수 |
| 메모리 JSON corrupt | 빈 객체 fallback (= 첫 호출 모드와 동일 흐름) |
| KIRAMS 가 forward 차단 (To 가 외부 도메인이라 거부) | "보내기" 후 에러 페이지 → 받은편지함 복귀 wait timeout → `forward` 단계 실패 처리 |

## 다중 tenant 확장

새 webmail 추가:

1. §Tenant registry 행 추가
2. SSRF allowlist 등재 (진입 + 호스팅 도메인)
3. secret 파일 생성 (`~/.openclaw/secrets/webmail-watch-<tenant>.toml`, chmod 600). KeePassXC OTP export + `login_pw` 한 줄 추가 형식.
4. §Bootstrap 1회 수동 로그인 → 전용 프로필에 세션 쿠키 + "아이디저장" 쿠키 영속
5. tenant 별 selector (login / OTP / listing / forward / move) 확정 → run.py 의 `TENANTS["<key>"].selectors` 갱신
6. cron 등록 (§운영 메모)

## 운영 메모

- **자격증명 노출 ✗**: 단일 toml (chmod 600) 만. 어떤 stdout/log 로도 평문 ✗.
- **TOTP secret 영구성**: 만료 없는 영구 자격증명. 백업 채널 격리 (예: Bitwarden Secure Note 사본) 필수.
- **매 회차**: 받은편지함 위에서부터 `poll_limit=3` 건. 받은편지함 빔 → 즉시 break.
- **Single tenant per call**: 한 호출은 한 tenant. 다중 tenant 는 cron job 분리.
- **답장 정체성**: 답장은 본인 Gmail 에서 직접 발송 (외부 webmail 거치지 않음). 사용자가 수용한 결정 (KIRAMS project memory 참조).
- **본문 형식**: HTML 그대로 보존. markdown 변환은 vault 기록 단계 (gmail-label-actions 또는 후속) 담당.
- **세션 만료**: TOTP 자동 입력으로 사람 개입 최소화. webmail 정책 변경 시 `auth_failed` → 사람 개입.
- **Telegram 침묵**: `notify_telegram` no-op (stderr log.info), cron `delivery.mode="none"`. gmail-label-actions Gmail 브리핑이 KIRAMS-FWD 메일 분류로 결과 노출.

## OpenClaw cron 설정

mcp tool 로 등록 (예: `webmail-watch-kirams` job):

```jsonc
{
  "name": "webmail-watch-kirams",
  "schedule": { "kind": "cron", "expr": "*/30 8-17 * * 1-5", "tz": "Asia/Seoul" },
  "sessionTarget": "isolated",
  "wakeMode": "now",
  "payload": { "kind": "agentTurn", "message": "/webmail-watch kirams" },
  "delivery": { "mode": "none" }
}
```

cron 회차 시 isolated session 의 agent 가 `/webmail-watch <tenant>` 슬래시 커맨드를 받아 §의존성 §호출 형식 의 Bash 명령으로 실행.

운영 모니터링:
- `mcp__openclaw__cron action=runs jobId=<id>` — 최근 실행 결과
- `cat ~/.openclaw/agents/main/memory/webmail-watch.json` — `last_checked` 갱신 확인
- KIRAMS 받은편지함의 누적 row 수 (3건 이하 정착 = 정상)
- KIRAMS 보낸편지함의 누적량 (운영 부담 평가)

## 관련 문서

- 자격증명 전략: project memory `KIRAMS webmail-watch skill`
- 거버넌스: `PROGRESS.md` (spec-kit 4파일 압축 형태)
- 패턴 본체: `society-watch` SKILL.md (logged-in-watch 패턴)
- 후속 처리: `gmail-label-actions` skill
