---
name: society-watch
description: 학회 자료실 polling — 새 게시글 자동 다운 → ~/projects/2nd-brain-vault/sources/00_inbox/ 드롭 + Telegram 알림. 1회 수동 로그인 + Chrome password manager 로 세션 영속.
allowed_tools: [browser, bash]
---

# society-watch

학회 홈페이지 자료실을 정기 polling 하여 신규 게시글의 첨부파일(또는 첨부 0인 경우 본문 markdown)을 자동으로 받아 brain-system 의 staging 폴더에 드롭한다. 출력 이후의 처리 (PARA 분류 + 동반 노트 작성)는 기존 brain-system "인박스 브레인화" 워크플로우가 이어받는다.

**Phase 1**: KSNM 보험관련 보드 시범. 안정 검증 후 다중 보드/학회 확장.

## 입력

- `society` (필수): 학회+보드 키. 미지정 시 `ksnm-insur` (default).

## Society registry

| society 키 | 학회 | 보드 | listing URL | download skin |
|---|---|---|---|---|
| `ksnm-insur` | 대한핵의학회 | 보험관련 | `https://www.ksnm.or.kr/bbs/?code=insur` | `default` |

확장 시 이 표에 행 추가. 학회 도메인은 SSRF allowlist 에 등재 필요.

## 영속 상태

JSON 파일로 last-seen 추적:

- 경로: `~/.openclaw/agents/main/memory/society-watch.json`
- 스키마:

```json
{
  "ksnm-insur": {
    "last_post_id": 3528,
    "last_checked": "2026-05-07T07:00:00+09:00"
  }
}
```

읽기 시 파일 또는 키 부재 → `last_post_id: 0` 으로 처리 (= 첫 호출 모드).

## 절차

### Step 1 — Listing snapshot

```
browser open <listing URL>
browser snapshot --format ai
```

세션 만료 감지 (URL 이 `/member/login.php` 류로 redirect 또는 페이지에 "아이디"·"비밀번호" textbox 존재) → Step 8 의 `session_expired` 알림 후 종료.

### Step 2 — last_post_id 로드

`society-watch.json` 에서 `<society>.last_post_id` 읽음. 부재 시 0.

### Step 3 — 새 게시글 식별

listing snapshot 의 `<table>` row 들을 파싱.

각 row 의 제목 link href 패턴:
```
/bbs/index.html?code=<board>&category=&gubun=&page=1&number=<post_id>&mode=view&...
```

`number=` 정수 추출.

분기:
- `last_post_id == 0` (첫 호출): number 내림차순 상위 **5개** 만 대상 (인박스 부담 회피)
- `last_post_id > 0`: `number > last_post_id` 인 모든 글 대상

대상 0개면 → Step 7 의 last_checked 만 갱신 후 알림 없이 종료.

### Step 4 — 각 새 글 detail snapshot

각 대상 글에 대해 (post_id 오름차순, 즉 가장 오래된 것부터):

```
browser open https://www.ksnm.or.kr/bbs/index.html?code=<board>&number=<post_id>&mode=view
browser snapshot --format ai
```

추출:
- **제목**: row `"제 목 : <T>"` 에서 `<T>` 부분
- **등록일**: row `"등록일 YYYY년 MM월 DD일 ..."` → `YYYY-MM-DD` 로 변환
- **첨부파일들**: `rowheader "첨부파일N"` 의 cell 안 link 들. 각 link 의 (a) ref, (b) 표시 파일명 (img alt 또는 link text), (c) href 의 `number=` (download_id) 수집

### Step 5 — 콘텐츠 다운로드 + 파일명 정규화

#### Case A — 첨부 1개 이상

각 첨부 link 에 대해:

```
browser download <ref> /tmp/society-watch/<society>/<원파일명>
```

그 후 파일명 정규화 후 staging 으로 mv:

```
YYYY-MM-DD_<society>_<title-slug>_<원파일베이스>.<ext>
```

- `<society>`: 그대로 (예: `ksnm-insur`)
- `<title-slug>`:
  - 한글·영문·숫자 보존
  - 공백 → `_`
  - 파일시스템 위험 문자 제거: `/ \ : * ? " < > |` 그리고 leading/trailing `.`
  - 길이 50자 클램프 (UTF-8 문자 단위)
- `<원파일베이스>.<ext>`: 다운로드된 파일의 원래 이름. 이미 안전한 형태면 그대로, 위 위험문자 있으면 동일 규칙 적용
- 한 게시글에 같은 이름 중복 시 `_2`, `_3` 접미

#### Case B — 첨부 0개

본문 markdown 추출:

detail snapshot 의 본문 영역 (제목 row 이후, "목록" 버튼 이전의 cell 텍스트들) 을 markdown 으로 정리.

```markdown
# <제목>

- 학회: 대한핵의학회 (ksnm)
- 보드: 보험관련 (insur)
- 게시번호: <post_id>
- 등록일: <등록일>
- 작성자: <작성자>
- 원본 URL: https://www.ksnm.or.kr/bbs/index.html?code=<board>&number=<post_id>&mode=view

---

<본문 텍스트>
```

저장 파일명:
```
YYYY-MM-DD_<society>_<title-slug>.md
```

### Step 6 — `sources/00_inbox/` 로 mv

```bash
mkdir -p ~/projects/2nd-brain-vault/sources/00_inbox/
mv /tmp/society-watch/<society>/<정규화 파일명> ~/projects/2nd-brain-vault/sources/00_inbox/
```

mv 실패 (디스크 가득, 권한 문제 등) → Step 8 의 `io_failed` 알림, last_post_id 미갱신 (재시도 가능 상태 유지) 후 종료.

### Step 7 — last_post_id + last_checked 갱신

성공 처리한 게시글 중 가장 큰 `number` 로 `last_post_id` 갱신.
`last_checked` 는 ISO 8601 (`YYYY-MM-DDTHH:MM:SS+09:00`) 현재 시각.

Atomic write — temp 파일에 쓴 후 rename:

```bash
TMP=$(mktemp ~/.openclaw/agents/main/memory/.society-watch.json.XXXXXX)
echo '<new JSON>' > "$TMP"
mv "$TMP" ~/.openclaw/agents/main/memory/society-watch.json
```

### Step 8 — Telegram 알림

| 케이스 | 메시지 (단순한 한국어) |
|---|---|
| 새 자료 N≥1 | `📥 <학회표기> <보드표기> 새 자료 N개` 줄바꿈 후 각 제목 한 줄씩 (`- <제목>`), 마지막 줄 `→ sources/00_inbox/ 드롭됨. "인박스 브레인화해줘" 로 처리.` |
| 새 자료 0개 | 알림 보내지 않음 (noise 회피) |
| `session_expired` | `⚠ <학회표기> 세션 만료. WSLg 띄워서 'openclaw browser open https://www.ksnm.or.kr/member/' 후 재로그인 부탁합니다.` |
| `partial_failure` (일부 첨부 다운로드 실패) | 성공분 정상 알림 + 끝에 `⚠ <K>개 첨부 다운로드 실패 — <원인 요약>` |
| `io_failed` (sources/00_inbox 로 mv 실패) | `⚠ <학회표기> 자료 처리 실패: <원인 요약>. 다음 회차 재시도.` |

`<학회표기>` = "KSNM", `<보드표기>` = "보험관련" 식 — society registry 의 학회·보드 컬럼 사용.

## Failure mode 매핑

| 실패 | 처리 |
|---|---|
| 세션 만료 (로그인 페이지 redirect) | `session_expired` 알림 → 종료. last_post_id 미갱신 |
| Captcha/2FA prompt 출현 | 알림 ("captcha 출현, 회차 skip"), 그 회차 종료 |
| listing/detail 페이지 구조 변경 (선택자 무효) | 1회 재시도, 여전히 실패 시 알림 + 종료 |
| 개별 첨부 다운로드 실패 | 그 첨부만 skip, 다른 자료는 정상 처리, `partial_failure` 알림 |
| `sources/00_inbox/` write 실패 | `io_failed` 알림, last_post_id 미갱신 |
| 메모리 JSON 파일 corrupt | 빈 객체로 fallback (=== 첫 호출 모드), 알림에 "메모리 reset" 포함 |

## 다중 보드/학회 확장

새 보드 추가:

1. 위 §Society registry 표에 행 추가 (society 키, listing URL, download skin)
2. SSRF allowlist 에 학회 도메인 등재 (`*.<도메인>`)
3. 1회 수동 로그인 (이미 같은 학회면 skip — 세션 공유)
4. cron 등록: `openclaw cron add "0 7 * * *" "/society-watch <society>"`

후보:
- `ksnm-general`: 행사 및 기타자료
- `ksnm-gonggo`: 정도관리
- `ksnm-add_bbs11`: 수련관련
- `ksnm-add_bbs12`: 학술관련
- 타 학회: `kthyroid-*`, `karp-*` 등 (별도 1회 수동 로그인 필요)

## 운영 메모

- **자격증명**: SKILL 에 노출 ✗. Chrome password manager 안에만 보관 (1회 수동 로그인 시 저장됨). OpenClaw docs 의 *"Do not give the model your credentials"* 준수
- **첫 호출**: 최근 5개만 받음 (인박스 폭주 회피). 운영 시작 후엔 delta 기반
- **기본 cadence**: 하루 1회 07:00 KST. 세션 만료 빈도 측정 후 조정 (만료가 잦으면 cadence 더 자주 + 만료 처리 강화)
- **Single board per call**: 한 호출은 한 society 만 처리. 여러 보드는 cron 다중 등록으로 분리
- **출력 destination**: 반드시 `~/projects/2nd-brain-vault/sources/00_inbox/`. 다른 위치 ✗ (brain-system 의 staging 규약. 후속 브레인화 워크플로우가 받지 못함)
- **파일명 규칙**: brain-system [[CLAUDE.md]] §"파일명 규칙" 의 이벤트·캡처 형식 준수
- **세션 만료 정상**: 영원히 자동 안 됨. 사람이 주기적으로 재로그인하는 게 정상 운영

## 관련 문서

- 설계 본체: `~/projects/2nd-brain-vault/knowledge/02_areas/brain-system/tools/openclaw/notes/logged-in-watch-patterns.md`
- 프로젝트 핸드오프: `~/projects/2nd-brain-vault/knowledge/01_projects/openclaw-society-watch/SESSION-HANDOFF.md`
- brain-system 규약: `~/projects/2nd-brain-vault/CLAUDE.md` §"외부 파일 캡처 워크플로우" + §"파일명 규칙"
