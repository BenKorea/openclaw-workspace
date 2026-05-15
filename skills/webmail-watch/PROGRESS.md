# webmail-watch — PROGRESS

> spec-kit 의 4파일(spec / plan / tasks / checklist)을 한 파일에 압축한 형태. webmail-watch 규모에 맞춰 경량화. 향후 다른 skill 들도 이 거버넌스로 표준화하고 싶어지면 `github/spec-kit` 본체로 마이그레이션 검토.

## 세션 재개 패턴

Dr. Ben 이 "kirams webmail 재개" 또는 비슷한 지시를 주면, 모델은 다음 절차를 따른다.

1. 이 파일 (`skills/webmail-watch/PROGRESS.md`) 을 읽음
2. **§Tasks** 의 첫 미완료 (`- [ ]`) 항목으로 이동
3. 그 항목의 **What / Done when** 만 보고 작업 시작 (그 위 항목은 이미 완료된 것으로 신뢰)
4. 작업 완료 후 **Done when** 충족 시 `[x]` 로 변경 + Notes 갱신 (selector 값 등 발견 정보 누적)
5. 다음 미완료 항목으로 자동 진행 ✗ — 한 항목 끝낼 때마다 Dr. Ben 보고 + 다음 항목 진행 여부 확인

체크리스트 항목 자체의 추가/삭제/분해는 Dr. Ben 승인 후만. 모델은 현재 진행 중인 항목의 **Done when** 정밀화 또는 **Notes** 누적만 자율 수행.

---

## §Spec — 무엇을 만드는가

**Goal**: KIRAMS (한국원자력의학원) MailPlug webmail 의 신규 메일을 polling 하여 본인 Gmail (`kimbi.kirams@gmail.com`) 로 forwarding. 발송 채널은 **KIRAMS webmail UI 의 "전달" 버튼 click-driven** (외부 SMTP/API ✗). 분류·답장·vault 기록은 `gws-assistant` 가 이어받음.

**Why**: KIRAMS 가 IMAP·자동 forwarding 규칙(필터)을 정책으로 차단. 사람이 손으로 "전달" 클릭하는 것은 정상 허용 → browser automation 으로 그 행위 자체를 자동화. society-watch 가 학회 사이트의 "다운로드" 버튼을 클릭하는 것과 동일한 패턴.

**Success criteria** (운영 정상의 정의):
1. 30분 cadence cron 으로 무인 동작
2. TOTP 자동 입력으로 세션 만료 시 자동 재로그인 (사람 개입 없음)
3. 신규 메일이 KIRAMS UI "전달" click sequence 로 `kimbi.kirams@gmail.com` 받은편지함에 첨부 보존된 채 도달
4. 발신자 도메인 (`@kirams.re.kr`) + Subject prefix `[KIRAMS-FWD]` 로 gws-assistant 가 KIRAMS 출처 인식 가능
5. 자격증명 (webmail ID/PW, TOTP secret) 어느 것도 모델 컨텍스트·log·stdout 에 노출되지 않음

**Out of scope**:
- 답장 (gws-assistant 의 Drafts 워크플로우가 담당, 발송은 본인 Gmail 에서)
- vault 기록 (gws-assistant 후속)
- 다중 tenant 동시 운영 (Phase 1 은 KIRAMS 단일)
- KIRAMS 보낸편지함 정리 (운영 후 부담되면 별도 phase 로)

---

## §Plan — 어떻게 만드는가 (확정 사항)

| 결정 | 내용 |
|---|---|
| Architecture | webmail-watch (KIRAMS UI click-driven re-forward) → gws-assistant (분류·답장·vault) |
| Browser | Playwright Chromium + skill 전용 `userDataDir` (`~/.openclaw/skills/webmail-watch/chrome-profile/<tenant>/`). OpenClaw 메인 프로필과 분리 |
| 자격증명 (통합) | `~/.openclaw/secrets/webmail-watch-<tenant>.toml` (chmod 600) `[<TENANT>]` 섹션에 `login_pw` / `otp_secret` / `otp_digits` / `otp_period` 평문. KeePassXC export 형식과 호환되도록 `otp_issuer` / `otp_account` / `otp_type` 메타 키도 무해하게 허용. run.py 가 PW 만 `page.fill` → 로그인 버튼 → OTP 화면. **ID 는 KIRAMS "아이디저장" 기능 prefill 신뢰** (별도 입력 ✗). **2026-05-10 옵션 C 채택** — Chrome autofill 의존 제거, headless 결정성 확보 |
| 발송 채널 | **KIRAMS UI 의 "전달" 버튼 click sequence** (Playwright). 메시지 열기 → 전달 클릭 → 받는사람 입력 → 제목 prefix 추가 → 전송 클릭. 첨부는 KIRAMS UI 가 자동 포함 |
| Forwarding 식별자 | Subject prefix `[KIRAMS-FWD]` + 발신자 도메인 `@kirams.re.kr`. 임의 헤더 주입 ✗ (KIRAMS 가 발송하므로 불가) |
| Idempotency | `webmail-watch.json` 의 `last_message_id` 단독 보장. KIRAMS 메시지에 marker 기능 없음 (별표·라벨·플래그 ✗) |
| 영속 상태 | `~/.openclaw/agents/main/memory/webmail-watch.json`, atomic write |
| 매 회차 | 받은편지함 위에서부터 `poll_limit=3` 건 처리. **각 회: forward → Gmail 폴더로 이동 → listing 에서 사라짐.** 받은편지함이 비면 break |
| Idempotency | **받은편지함 = pending queue** 모델 (Dr. Ben 결정 2026-05-10). `last_message_id` 폐기. 옵션 1 (dup 허용) — forward 성공 + 이동 실패 시 break, 다음 회차 재시도 시 dup 1건 (kimbi.kirams Gmail) 가능. KIRAMS 트래픽 작아 무해 가정 |
| Cadence | `*/30 * * * *` (Asia/Seoul) — OpenClaw cron job `webmail-watch-kirams` |
| Telegram | 침묵 (`delivery.mode="none"` + `notify_telegram` no-op). gws-assistant 가 분류 시점에 KIRAMS 인식 → 그쪽에서 보고 |
| 코드 위치 | `skills/webmail-watch/{SKILL.md, run.py, requirements.txt, PROGRESS.md}` |

---

## §Tasks — 체크리스트

각 항목: **What** (작업) · **Owner** (담당) · **Depends on** (선행) · **Done when** (검증) · **Notes** (발견 사항 누적).

### Phase 0 — 설계 (완료)

- [x] **P0.1 자격증명 전략 확정** · Owner: Dr. Ben + model · Done when: 메모리에 패턴 A2 기록됨 · Notes: `project_kirams_webmail.md`
- [x] **P0.2 발송 메커니즘 결정 (KIRAMS UI click-driven)** · Owner: Dr. Ben · Done when: §Plan 에 KIRAMS UI click-driven 확정 기록 · Notes: 첨부 자동 포함 확인됨, 메시지 marker 기능 없음 — last_message_id 단독 idempotency
- [x] **P0.3 Browser 자동화 결정 (Playwright + 전용 프로필)** · Owner: model 추천 → Dr. Ben 승인 · Done when: §Plan 기록
- [x] **P0.4 SKILL.md 초안** · Owner: model · Done when: `SKILL.md` 작성 완료, §자격증명·§Bootstrap·§발송 메커니즘·§Failure mode 매핑 포함
- [x] **P0.5 run.py 골격** · Owner: model · Done when: `run.py` 작성 완료. argparse / TOTP / Playwright bootstrap 모드 + cron_run 골격. `fetch_new_messages` 와 `forward_via_webmail` 은 NotImplementedError (P5 에서 구현)
- [x] **P0.6 requirements.txt** · Owner: model · Done when: playwright, pyotp 명시
- [x] **P0.7 project memory 갱신** · Owner: model · Done when: 자격증명·browser·발송 결정 사항 기록

### Phase 1 — 인프라 준비

- [x] **P1.1 Python 의존성 설치 (uv project mode)**
  - **What**:
    ```bash
    cd ~/.openclaw/workspace/skills/webmail-watch
    uv sync
    uv run playwright install chromium
    ```
  - **Owner**: Dr. Ben (model 은 명령만 안내)
  - **Depends on**: —
  - **Done when**: `uv run --project ~/.openclaw/workspace/skills/webmail-watch python -c "import pyotp, playwright"` 정상 + `~/.openclaw/workspace/skills/webmail-watch/.venv/` + `uv.lock` 생성됨
  - **Notes**:
    - `pyproject.toml` 이 의존성 SoT, `uv sync` 가 `.venv` 생성 + 의존성 설치 + `uv.lock` 잠김
    - cron 호출 형식: `uv run --project ~/.openclaw/workspace/skills/webmail-watch run.py kirams`
    - SSRF allowlist 는 Playwright 직접 launch 라 OpenClaw fetch 레이어 우회 → 등재 불필요 판단 (2026-05-10). bootstrap 시 차단 발생하면 P3.1 Notes 에서 재검토
    - **정책 선례**: OpenClaw 외부-의존성 skill 첫 사례. 향후 외부-의존성 skill 도 같은 패턴 (skill 디렉토리 안 `pyproject.toml` + `uv.lock` + `uv run --project` cron 호출)

### Phase 2 — TOTP 검증

- [x] **P2.1 TOTP secret 파일 코드 생성 검증** *(skipped — P3.1 통합)*
  - **Notes**: 별도 helper 검증 대신 P3.1 bootstrap 에서 자동 OTP 입력 동작으로 통합 검증 (Dr. Ben 결정 2026-05-10). 시간 동기화(NTP) drift 의심 시 `timedatectl` 확인 별도 수행.

### Phase 3 — Bootstrap (1회 수동 로그인)

- [x] **P3.1 Bootstrap 헤더드 로그인 + (P2.1 통합) PW/OTP 자동 주입 검증** *(2026-05-10 통과)*
  - **What**: `uv run --project ~/.openclaw/workspace/skills/webmail-watch run.py kirams --bootstrap` 실행. WSLg 띄운 상태. ID 는 KIRAMS "아이디저장" 기능으로 prefill 된 상태에서 run.py 가 toml 기반 `login_pw` 입력 → 로그인 버튼 → OTP 자동 주입하는지 관찰
  - **Owner**: Dr. Ben (toml 작성) + model (실행·관찰)
  - **Depends on**: P1.1, secret toml 에 `login_pw` 추가, KIRAMS 첫 진입 시 "아이디저장" 체크 1회 수행 (브라우저 쿠키)
  - **Done when**:
    1. ID prefill 확인 + PW 자동 주입 + 로그인 버튼 클릭 → OTP 화면 전환
    2. OTP 자동 주입 → 받은편지함 도달
    3. `is_logged_in()` True 반환
  - **Notes**:
    - redirect 추적 — 진입 URL 이 `zm\d+.mailplug.com` 으로 redirect 되는지 확인
    - 외부 도메인 차단 발생 시 OpenClaw outbound 정책 점검
    - **2026-05-10 첫 실행 (옵션 A)**: Chrome for Testing 은 password manager 비활성 → `channel="chrome"` 으로 정정. OTP selector (`#otp_code1`) 캡처 통과.
    - **2026-05-10 옵션 C 전환**: Chrome autofill 의존 제거 → PW 만 toml 직접 fill. ID 는 KIRAMS webmail 자체 "아이디저장" 기능 prefill 신뢰 (Chrome PM 과 별개 — webmail 쿠키 기반).
    - **2026-05-10 OTP submit 발견**: `page.fill()` 은 input 이벤트만 trigger 하고 keydown/keypress ✗. KIRAMS 는 6자리 입력 후 자동 submit 이 keydown hook 이므로 fill 만으로는 멈춤. 정정: `page.press(otp_input, "Enter")` 로 명시적 submit.
    - **2026-05-10 P4.1 첫 진입 발견**: cron(headless) 에서 "페이지에 접속할 수 없습니다" 출력 — `channel="chrome"` (시스템 Chrome stable) + headless 조합이 WSL 에서 entry URL 도달 실패. 옵션 C 후 Chrome PM 의존이 사라졌으므로 channel 제거 → Playwright bundled Chromium 으로 회귀, `args=["--no-sandbox", "--disable-dev-shm-usage"]` 보강.

- [x] **P3.2 OTP selector 확정 + 자동 입력 통과** *(2026-05-10 통과)*
  - **확정된 selector**: `otp_input = "#otp_code1"`, `otp_submit = ""` (별도 버튼 ✗ — `page.press("Enter")` 로 submit)
  - **Notes**: `page.fill()` 만으로는 KIRAMS 의 keydown 기반 자동 submit 이 발동하지 않아 `page.press(otp_input, "Enter")` 로 명시적 submit 필요. P3.1 Notes 에 사유 누적.

- [x] **P3.3 받은편지함 marker selector 확정** *(2026-05-10 통과)*
  - **확정된 selector**: `inbox_marker = "tbody tr[data-index]"`, `login_form_marker = "#cpw"`
  - **Notes**: P3.1 통과 시점에 `perform_login()` 내부에서 `wait_for_selector(inbox_marker)` 가 받은편지함 도달을 확인하면서 자연스럽게 검증됨. 로그인 폼에서 False 반환 동작은 P4.1 cron_run 첫 진입(이미 로그인 상태) 또는 P4.2(만료 후 재로그인) 에서 부속 검증.

### Phase 4 — 자동 재로그인 검증

- [x] **P4.1 Headless 모드 첫 진입 (이미 로그인 상태)** *(2026-05-10 통과)*
  - **결과**: `[telegram] ⚠ KIRAMS webmail unimplemented: fetch_new_messages: P5.1·P5.2 에서 listing/detail DOM 확정 후 구현` 출력. `is_logged_in()` True, NotImplementedError 가 fetch 단계에서 발생 — 기대한 흐름.
  - **Notes**: bootstrap headed 직후 cron headless 시 첫 시도에 "페이지에 접속할 수 없습니다" 발생. `channel="chrome"` 제거 + WSL args 보강 후 통과.

- [ ] **P4.2 세션 만료 후 자동 재로그인** *(Phase 8 운영 자연 발생으로 위임)*
  - **What**: 세션 쿠키 자연 만료 시 headless cron_run 이 perform_login 자동 발동 → 재로그인 통과 확인
  - **Owner**: model + Dr. Ben (운영 모니터링)
  - **Depends on**: P4.1, P8.1 (cron 등록)
  - **Done when**: 운영 중 첫 자연 만료 회차에서 `auth_failed` ✗ + 받은편지함 도달 로그
  - **Notes**: 인위적 시뮬레이션 (`context.clear_cookies()`) 은 KIRAMS "아이디저장" long-lived 쿠키도 함께 날아가 ID prefill 실패 → ID-PW 분리 흐름과 맞지 않음. Phase 8 운영 자연 만료로 검증.

### Phase 5 — Listing 파싱 + Forward UI 자동화

- [x] **P5.1 받은편지함 listing DOM snapshot** *(2026-05-10 통과)*
  - **확정**: MailPlug SPA — row attribute 에 영구 ID ✗. **message_id 는 row click 후 URL `/mail/inbox/messages/(\d+)` 에서 정수 추출**.
  - **listing URL**: `https://mail.kirams.re.kr/mail/inbox` (KIRAMS 도메인 자체. mailplug 인스턴스 호스트는 로그인만 reverse-proxy)
  - **listing row selector**: `tbody tr[data-index]`
  - **row open trigger**: `[role="button"][tabindex="0"]` (제목 셀의 div) — 클릭 시 SPA navigation 으로 detail URL 변경

- [x] **P5.2 메시지 열람 + 제목·발신자 추출 selector** *(2026-05-10 통과)*
  - **listing 단계에서 메타 추출 가능** — detail page 추출 불필요.
  - **발신자 (이메일 포함)**: row 안의 `[title]` attribute. 형식: `이름 <user@domain>`
  - **제목**: `[role="button"] span.break-all` 의 textContent. 단 listing truncated 가능.
  - 본문/첨부 추출 ✗ (KIRAMS forward 가 자동 처리)

- [x] **P5.3 "전달" UI click sequence selector 확정** *(2026-05-10 통과)*
  - **forward_button**: `button:has-text("전달")` — class 가 build hash (`_root_8pv7d_1` 등) 라 텍스트 기반.
  - **forward_to_input**: `#toRecipients-input` — chip 입력. `fill` 후 `press("Enter")` 로 chip 변환.
  - **forward_subject_input**: `#input-subject` — KIRAMS 가 자동 `[FW]` prefix 추가. 그 앞에 `[KIRAMS-FWD] ` prepend.
  - **forward_send_button**: `button:has-text("보내기")` — svg + 텍스트.
  - **forward 후 복귀**: 받은편지함 URL `/mail/inbox` 도달 wait 또는 inbox_marker 재등장 (P6.1 에서 정확 동작 확인).
  - 첨부는 KIRAMS UI 자동 포함 (확인 완료)

- [x] **P5.4 fetch_new_messages + forward_via_webmail 구현** *(2026-05-10 통과 — P6.1 검증 통과)*
  - 초기 구현: `fetch_new_messages` (listing → row click → URL → goBack 반복, last_id 비교) + `forward_via_webmail` (detail goto → 전달 → 보내기) — P6.1 에서 동작 확인.
  - **2026-05-10 23:22 재설계 (Dr. Ben 의 pending-queue 모델 채택)**: `fetch_new_messages` 폐기 → `process_inbox` 통합 함수로 대체. `forward_via_webmail` 시그니처 단순화 (detail goto·message 인자 제거). `move_to_gmail_folder` 신설. `last_message_id` 영속 상태에서 폐기. 자세히는 P6.1 시즌2 Notes 참조.

### Phase 6 — Forwarding 검증

- [x] **P6.1 첨부 없는 메시지 forwarding 도달** *(2026-05-10 통과 + 시즌2 통과)*
  - **결과 (시즌 1 — fetch+forward 모델)**: `first_call_limit=1` 임시 변경 후 cron 모드 1회 실행 → kimbi.kirams 받은편지함에 `[KIRAMS-FWD] [FW]<원제목>` 도달 확인. 발신자 `@kirams.re.kr`.
  - **시즌 2 통과 (pending-queue 모델)**: 3건 forward + 3건 Gmail 이동 완료. Dr. Ben 확인 23:57.
    - 새 selector 4개: `row_checkbox=#toggle-0`, `toolbar_move_button=button:has-text("이동")`, `move_dropdown=[aria-labelledby="dropdown-toggle-move"][role="menu"]`, `move_target_gmail=[aria-labelledby="dropdown-toggle-move"] a:has-text("Gmail")`
    - 검증 시: KIRAMS 받은편지함에서 첫 row 가 forward 후 Gmail 폴더로 사라지고, listing 의 다음 row 가 위로 올라옴.
    - **2026-05-10 첫 시도 발견**: chip `to_input.press("Enter")` 가 chip 변환 + form submit 둘 다 trigger → dup 2회 발송 + KIRAMS 자동 logout redirect (`/member/do_logout`) 로 받은편지함 복귀 wait timeout. 정정: chip 변환을 `Tab` 으로 변경 + subject prefix 를 to_input 입력보다 먼저 (우발적 submit 시 안전망).
    - **2026-05-10 두번째 시도 발견**: forward 1회 정상 발송 후 `#toggle-0` 체크박스 click 이 "element not visible" 로 timeout. Tailwind 가 input 자체를 sr-only 처리, 시각 click target 은 `<label for="toggle-0">`. 정정: `row_checkbox` selector 를 `label[for="toggle-0"]` 로 변경.
    - **2026-05-10 세번째 시도 발견**: 첫 row 처리 (forward + Gmail 이동) 통과. 두 번째 row 의 forward_button click 시 두 가지 overlay 가 차례로 intercept — (a) detail 진입 직후 로딩 오버레이 `<div class="!absolute inset-0 z-[29] bg-white">`, (b) 직전 Gmail 이동의 toast/modal 잔재 (`#headlessui-portal-root` 자식). 정정: `_wait_for_ui_idle` helper 추가 (overlay 없음 + portal 자식 0 wait), `forward_via_webmail` 진입 시점 + `_ensure_inbox` 마지막에 호출.

- [ ] **P6.2 첨부 포함 메시지 forwarding 도달**
  - **What**: 첨부 1개 이상 메시지 forwarding → 첨부 파일명·내용 보존 확인
  - **Owner**: model + Dr. Ben (다운로드 검증)
  - **Depends on**: P6.1
  - **Done when**: 원본과 첨부 binary diff 0

- [ ] **P6.3 gws-assistant 출처 인식**
  - **What**: gws-assistant 가 발신자 도메인 + Subject prefix 로 KIRAMS 출처 인식하도록 룰 확장 (필요 시)
  - **Owner**: model + Dr. Ben (gws-assistant 수정 승인)
  - **Depends on**: P6.1
  - **Done when**: KIRAMS-FWD 메일이 분류 단계에서 `kirams` 출처 태그 부여

### Phase 7 — 영속 상태 + 알림

- [x] **P7.1 last_message_id 갱신 동작** *(2026-05-10 통과)*
  - **결과**: 두 번째 호출에서 `last_message_id="29005"` 변동 ✗ + `last_checked` 만 갱신. forwarded/failures 빈 리스트 → report() 침묵 (exit 0). §Checklist "신규 0건 회차에서 알림 미발생" 항목 동시 통과.

- [ ] **P7.2 partial_failure 시 last_id 보수적 갱신**
  - **What**: forwarding 중간 실패 시뮬레이션 (예: 받는사람 입력 selector 일시 무효화) → last_id 가 실패 직전까지만 갱신되는지 확인
  - **Owner**: model
  - **Depends on**: P7.1
  - **Done when**: 실패 후 재시도 시 누락된 메시지가 다시 forwarding 됨

- [x] **P7.3 Telegram 알림 채널 연결** *(2026-05-10 통과)*
  - **확정**: OpenClaw 가 cron 실행의 **stdout 을 자동 캡처해 Telegram 발송** (gws-assistant 와 동일 패턴). 비어있으면 침묵.
  - **변경**: `notify_telegram()` 을 `print(text)` 로 정정 — `[telegram]` prefix 제거, stderr → stdout 전환.
  - **검증**: 실제 Telegram 도착은 P8.1 cron 등록 후 신규 메일 발생 시 자연 검증 (코드 패턴은 동일 hook 검증된 gws-assistant 와 일치).
  - **외부 의존성 ✗**: bot_token / chat_id 는 OpenClaw 내부 처리, skill 코드 미노출.

### Phase 8 — 운영

- [x] **P8.1 cron 등록** *(2026-05-10 완료)*
  - **메커니즘**: `mcp__openclaw__cron` action=add. payload `agentTurn` + message `/webmail-watch kirams` → isolated session 이 SKILL 의 §의존성 §호출 형식 따라 `uv run --project ... run.py kirams` 실행.
  - **job id**: `7f87feac-5c20-4675-98c2-948330d4e708`
  - **schedule**: `*/30 * * * *` Asia/Seoul
  - **delivery**: `mode="none"` (Telegram 침묵 — gws-assistant 브리핑에 흡수)
  - **Done when**: 첫 자동 회차 (21:30 KST) 완료 후 webmail-watch.json 의 last_checked 갱신 확인

- [ ] **P8.2 1주일 운영 모니터링**
  - **What**: 세션 만료 빈도, TOTP 거부 빈도, partial_failure 빈도, KIRAMS 보낸편지함 누적량 측정
  - **Owner**: Dr. Ben
  - **Depends on**: P8.1
  - **Done when**: 1주 통계 수집, cadence·재시도 정책 조정 필요 여부 판단

- [x] **P8.3 데스크탑(kimbi) 첫 운영 + timing race fix** *(2026-05-15 완료)*
  - **트리거**: 노트북(ai4lt) P8.1 (2026-05-10) 통과 후 데스크탑(kimbi) 첫 cron 시도. 5회 cron + 3회 수동 trigger 모두 fail. 4단계 진단으로 원인 분리.
  - **진단 흐름 (4단계, 각각 별개 원인)**:
    1. **Chromium 시스템 deps 미설치** — `process_failed: Playwright Chromium libnspr4.so 등 ENOENT`. fix: `sudo /home/ben/.local/bin/uv run playwright install-deps chromium`. 부수 발견 — **sudo PATH sanitization trap**: `~/.local/bin` 이 sudo 의 secure_path 에 없어 `sudo uv ...` 가 `command not found`. uv 절대경로 또는 `sudo env "PATH=$PATH" uv ...` 필요.
    2. **secret 파일 미배치** — `FileNotFoundError: ~/.openclaw/secrets/webmail-watch-kirams.toml`. 노트북 secret 을 nano paste 로 데스크탑에 배치 (chmod 600). 검증: pyotp 로 생성한 TOTP 코드가 폰 인증기와 일치 (`scripts/totp-check.py` 류).
    3. **`auth_failed` × 4회** — ID 입력 단계 fail. 진단: PW length·invisible char·시계 drift·동시 접속 모두 reject. 원인: KIRAMS **"아이디저장" cookie 가 chrome-profile/kirams 에 없음** (노트북엔 누적, 데스크탑은 첫 진입). run.py 는 PW 만 fill 하고 ID 는 cookie prefill 가정. fix: `--bootstrap` headed 1회 + Dr. Ben 직접 "아이디저장" 체크 → cookie 정착. 객관 검증: Cookies sqlite 에 `rememberid` cookie 정착 확인.
    4. **`1건 forwarding 실패` × 3회** — `forward_via_webmail` 단계 fail (메일이 listing 에 still 남아있음으로 분리). selector stale 가설 의심됐으나 **reject** — Dr. Ben 의 수동 forward 정상 작동. demo 모드 (headed + `slow_mo=800ms`) 에서 3건 모두 OK → **timing race 확정** ([[playwright-headless-timing-race]]). 빠른 환경(데스크탑 RTX 3060) 이 chip 변환·reactive UI 보다 빠르게 다음 action 발생 → race. 느린 환경(노트북 Intel Arc CPU, GPU qwen 점유) 은 자연 slow_mo 효과로 미발현. [[user-machines-spec]] 비대칭이 운영 신뢰성에 영향 준 첫 사례.
  - **fix**: `run.py` 의 `open_context` 에 `slow_mo=int(os.environ.get("WEBMAIL_SLOW_MO", "300"))` 추가. default 300ms — race 안전망 + poll_limit=3 회차당 5~10초 추가 (무해). env 로 0 override 가능 (디버깅·노트북 OFF).
  - **demo 자료 영속화**: `scripts/demo.py + demo.sh` — headed + slow_mo + Playwright tracing + 사람 wait. 시각 검증·교육·향후 selector 변경 자동 탐지 자산. trace 사후 review: `npx playwright show-trace /tmp/webmail-trace.zip`.
  - **데스크탑 신규 셋업 체크리스트** (PC 추가 시):
    1. `sudo /home/ben/.local/bin/uv run playwright install-deps chromium`
    2. `~/.openclaw/secrets/webmail-watch-kirams.toml` 배치 (chmod 600)
    3. `bash scripts/demo.sh` 1회 (또는 `uv run run.py kirams --bootstrap`) — KIRAMS "아이디저장" 체크 + 받은편지함 도달 → cookie 정착
  - **부수 발견 — `log.exception` 의 traceback 미캡처**: `PYTHONUNBUFFERED=1` 으로도 stack 누락. uv run subprocess wrapper 의 stderr buffer 또는 logging-Playwright 의 미상 race. 진단 흐름에선 *대안 분리* (시간선 분석 + Dr. Ben 수동 검증 + demo 모드) 로 우회. Phase 9 후보: log.exception 출력 보장 (file handler 추가 또는 sys.excepthook 명시).
  - **부수 발견 — `report()` 의 partial failure 메시지 불분리**: `"K건 forwarding 실패"` 가 *forward* 와 *move* 양쪽 fail 을 같은 메시지로 보고. Phase 9 후보: outcome.failures 의 `error` prefix (`forward:` / `move:`) 를 메시지에 반영.
  - **부수 발견 — `bootstrap_run` print 메시지 부정확**: "ID/PW + OTP 를 자동 주입" 이라고 표시하지만 실제로 perform_login 은 PW 만 fill (ID 는 cookie prefill 가정). Phase 9 후보: print 메시지 정정 + bootstrap UX 개선 (ID 자동 입력 옵션 추가 등).

---

## §Checklist — 운영 사이클 검증

`P8.1` 후 첫 운영 회차에서 한 번 돌려보는 sanity check.

- [ ] 30분 주기 정시 트리거됨 (cron log)
- [ ] 신규 0건 회차에서 알림 미발생, last_checked 만 갱신
- [ ] 신규 N≥1 회차에서 Telegram 알림 도달
- [ ] kimbi.kirams 받은편지함에 메시지 도달 + Subject prefix 정상 + 발신자 `@kirams.re.kr`
- [ ] 원본 첨부 보존 (binary diff 0) — KIRAMS UI 자동 포함 검증
- [ ] gws-assistant 분류 결과에 KIRAMS 출처 태그 부여
- [ ] 모델 컨텍스트·log 어디에도 평문 자격증명 미노출
- [ ] webmail-watch.json atomic write (corruption 시 fallback 동작)
- [ ] 세션 만료 시 자동 재로그인 (1회 의도적 만료 시뮬레이션)
- [ ] `auth_failed` / `totp_invalid` / `partial_failure` 알림 메시지 정상 (각 1회 강제 시뮬레이션)
- [ ] KIRAMS 보낸편지함 누적 확인 (운영 부담 평가)
