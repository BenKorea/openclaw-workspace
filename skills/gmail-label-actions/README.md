# gmail-label-actions — 8-라벨 Gmail 액션 라우터 (예제 스킬)

OpenClaw 용 **커스텀 스킬 예제**. Gmail 라벨이 뜻하는 **다음 행동**({일정,할일,회신} 멱집합 + 저장)을 Google Workspace 안에서 **완결**하고, 메일은 스레드(본문+첨부)로 캡처한다.

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

라벨 집합 = 세 행동 {일정,할일,회신} 의 완전 멱집합(2³=8) + 저장. 모든 메일이 정확히 한 라벨에 귀속되고, 숫자 접두사가 picker 정렬을 1→8 결정트리(일정?→2-5 / 할일?→6,7 / else→8)로 강제한다.

## 캡처는 결정형, 추출은 바운디드

봇에게 *프롬프트로* "라벨 메일 처리해줘" 를 시키면 매 실행 LLM 이 단계를 추론한다 — 느리고, 첫 토큰이 늦으면 **no-output watchdog** 에 걸려 turn 이 죽거나 고아(미아) run 이 된다. 이 스킬은 절차를 **고정 코드**로 박는다:

- **캡처는 판단0** — 스레드·첨부 저장은 LLM 없음. 빠르고 watchdog/고아 위험 없음.
- **추출만 바운디드 LLM** — 일시·할일·회신 초안은 `claude --print` 헤드리스 1회 호출(JSON/STATUS 계약, 툴 전부 비활성). "에이전트 판단" 이 아니라 결정형에 가까운 *좁은 구조화 추출* — 무인 cron 에 안전.
- **fail-safe** — terminal 라벨(1·2·3·6)은 필수 액션 *전부 성공* 후에만 `9 완료` 부착(일시 추출 실패 등은 라벨 유지 → 다음 사이클 재시도, 유실 0).
- **판단 분리** — PARA 분류·요약·`[[링크]]` 같은 열린 판단은 이 라우터가 하지 않는다(knowledge/ 노트 생성 X). 그건 brainify(Claude Code 스킬)가 나중에.
- **thread 단위 + 건별 한 줄 출력** → 출력이 흘러 watchdog 타이머가 리셋됨.

배경 설명: [`docs/openclaw-skills.md`](../../../../docs/openclaw-skills.md) 의 "실습 — 라벨 자동화" 절.

## 멱등성 (2층)

- **캡처**: `_thread.md` 프론트매터의 `gmail_thread_id`+`message_count`. 같은 스레드가 같은 메시지 수로 다시 와도 재캡처 0(라벨만 정리), 메시지 수가 늘면 폴더 덮어쓰기 갱신(폴더명=첫 캡처 날짜 스탬프 유지).
- **GWS 액션**: 스레드 폴더의 **`_actions.json` 사이드카** 에 생성된 ID 기록(`calendar_event_id`/`google_task_id`/`gmail_draft_id`). 캡처가 `_thread.md` 를 덮어써도 사이드카는 보존 → 크래시-재개 시 이벤트/할일/초안 이중 생성 0.
- *vault 전체* 중복(이미 brainify 된 스레드 재유입)은 brainify 단계의 노트 프론트매터 dedup 이 담당(별도).

## 전제

- **OpenClaw 게이트웨이** 가동 중 ([`docs/openclaw-docker-install.md`](../../../../docs/openclaw-docker-install.md)).
- **gog CLI** 설치 + 본인 Gmail 계정 인증 (Gmail·Calendar·Tasks scope) ([`docs/openclaw-docker-add-gog.md`](../../../../docs/openclaw-docker-add-gog.md)). 무인 실행 시 keyring 비번(`GOG_KEYRING_PASSWORD`) 환경 제공 필요.
- **Claude Code CLI(`claude`)** 설치·인증 — 일시·할일·회신 추출용. 없으면 일정·회신은 skip(라벨 유지), 할일은 제목 fallback 으로만 생성.
- Python 3.

## 적용 (2곳만 본인 환경에 맞게)

1. **계정** — 환경변수 `GMAIL_ROUTER_ACCOUNT` 에 본인 Gmail 계정 (코드에 기본값 없음 — 누출 방지). *어디에 넣는지는 아래 [계정·env 설정 위치](#계정env-설정-위치--봇으로-쓸-때-핵심).*
2. **저장 위치** — 환경변수 `GMAIL_ROUTER_INBOX` (기본 `~/.openclaw/workspace/attachments`).

> **라벨명은 고정 규칙입니다 (코드 수정 대상 아님).** 이 스킬은 `1 저장`…`8 회신` + 완료 `9 완료` 를 쓴다 — Gmail 에 **이 9개 라벨을 그대로 만들어** 사용하세요(없으면 봇에게 "gog 로 라벨 만들어줘"). `+` 포함 라벨은 검색 시 `label:3-일정+할일`(공백→하이픈), 그 외는 `label:"1 저장"` 쿼리.

## 설치 (워크스페이스에 복사)

OpenClaw 는 `~/.openclaw/workspace/skills/<이름>/` 에서 스킬을 로드한다:

```bash
cp -r templates/skills/openclaw/gmail-label-actions ~/.openclaw/workspace/skills/
```

> Docker 컨테이너로 운영 중이면 워크스페이스가 마운트된 호스트 경로에 복사. 복사 후 `skills list` 로 재스캔 확인.

## 계정·env 설정 위치 (★ 봇으로 쓸 때 핵심)

env 를 **어디에 넣느냐는 실행 방식에 따라 다릅니다.**

### Docker 컨테이너 + 봇 (실사용)

봇이 부르면 `run.py` 는 **게이트웨이 컨테이너 안**에서 실행되므로, 계정은 *컨테이너의 환경변수*로 들어가야 한다. 이 repo 가 아니라 컨테이너 쪽 두 파일에 (기존 `GOG_KEYRING_PASSWORD` 과 똑같은 방식):

1. `~/projects/openclaw-docker/.env` (repo 밖·비공개) 에 값:
   ```
   GMAIL_ROUTER_ACCOUNT=you@gmail.com
   ```
2. `~/projects/openclaw-docker/docker-compose.extra.yml` 의 `openclaw-gateway`·`openclaw-cli` 두 `environment:` 블록에 참조:
   ```yaml
         - GMAIL_ROUTER_ACCOUNT=${GMAIL_ROUTER_ACCOUNT}
   ```
3. 재생성 + 확인:
   ```bash
   cd ~/projects/openclaw-docker
   docker compose -f docker-compose.yml -f docker-compose.extra.yml up -d --force-recreate openclaw-gateway
   GW=$(docker ps --filter name=openclaw-gateway --format '{{.Names}}' | head -1)
   docker exec "$GW" printenv GMAIL_ROUTER_ACCOUNT   # 계정이 나오면 OK
   ```

> ⚠️ **이 repo 안에는 계정을 넣지 마세요** — 공개 repo 라 commit 시 누출됩니다. 계정은 컨테이너 `.env`(repo 밖)에만.
> ⚠️ 이 계정은 **gog 인증 계정과 일치**해야 합니다. 다르면 `OAuth client credentials missing` 류로 실패합니다.

### 수동 CLI 테스트 (네이티브 또는 컨테이너 안)

스킬 동작만 빠르게 확인할 땐 인라인 env 로 — 아래 "실행·검증".

## 실행·검증 (수동 테스트)

먼저 **라벨별 건수만** 확인(저장·생성·라벨변경 없음):

```bash
GMAIL_ROUTER_ACCOUNT=you@gmail.com python3 ~/.openclaw/workspace/skills/gmail-label-actions/run.py --dry-run
```

특정 라벨만 / 전체 실제 처리:

```bash
… run.py --label "2 일정"     # 한 라벨만
… run.py                      # 전체 1~8
```

**봇(텔레그램 등) 실사용**: 위 "계정·env 설정 위치"로 컨테이너에 env 를 넣었으면 SKILL.md 의 자연어 트리거("라벨 메일 처리해줘" / "먼저 몇 건인지 봐줘")로 부르면 된다.

## 알려진 한계

- **회신 발송 감지는 Phase 2** — 회신 라벨(4·5·7·8)은 초안 생성까지만 완결하고 라벨을 유지한다(비-terminal). 사용자가 임시보관함에서 검토·발송. 실발송→자동 `9 완료` 승급(영속 awaiting_reply 큐 + sent-poll)은 후속 단계.
- **첨부 없는 본문-only 메일**도 캡처됨(`_thread.md` 만). 일정/할일 추출은 본문에서 수행.
- `claude` CLI 미설치 시 일정·회신 라벨은 액션 미완으로 라벨 유지(유실 아님).

## 파일

| 파일 | 역할 |
|---|---|
| `run.py` | 모든 로직. gog 호출은 `gog_json`(조회)·`gog_call`(변경), 추출은 `claude_print`(바운디드 LLM) 래퍼로 수렴. 라벨→액션은 `LABELS` 디스패치 테이블. |
| `SKILL.md` | OpenClaw 스킬 매니페스트 — 자연어 트리거 + 실행 모드. |
| `README.md` | 이 문서. |

> 런타임 산출물: 스레드 폴더의 `_thread.md`(캡처 본문·멱등키) + 첨부 원본 + `_actions.json`(GWS 액션 멱등 마커). `_actions.json` 은 캡처와 독립이라 재캡처에도 보존된다.
