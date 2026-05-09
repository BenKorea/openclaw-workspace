# USER.md - About Your Human

- **Name:** Ben Korea (benkorea.ai@gmail.com)
- **What to call them:** **Dr. Ben** (한국어·영어 모두 동일)
- **Language:** 한국어 존댓말 사용. 반말 금지.
- **Timezone:** Asia/Seoul (KST, GMT+9)
- **Host:** WSL2 Ubuntu on Windows (`/home/ben`)

## Context

Dr. Ben은 의사이자 개발자로, 다음 프로젝트들을 운영하신다.

- **2nd-brain-vault** — `~/projects/2nd-brain-vault/` (WSL2 ext4 native)
  PARA 기반 개인 지식 시스템. `knowledge/`(Obsidian 마크다운) + `sources/`(원본 바이너리) 짝 폴더 구조. 동반 노트 패턴.
- **OpenClaw** — 이 환경 자체. `~/.openclaw/`에 상태, `~/.openclaw/workspace/`가 메인 에이전트의 홈.
- **2nd-brain-docker** — `~/projects/2nd-brain-docker/` (실행환경 자동화)
- **2nd-brain-vault-guide** — `~/projects/2nd-brain-vault-guide/` (공개 가이드·템플릿)

## 작업 트리거 — 외부 규약 로드

다음 키워드/주제가 메시지에 등장하면 **해당 외부 CLAUDE.md를 먼저 Read한 뒤** 그 규약대로 작업한다.

| 트리거 | 읽을 파일 |
|---|---|
| 브레인화, 인박스, 2nd-brain, second-brain, vault, 동반 노트, PARA, knowledge/, sources/ | `/home/ben/projects/2nd-brain-vault/CLAUDE.md` |

이 외부 파일들은 OpenClaw의 자동 시작 로드 대상이 아니므로, **트리거 발생 시 명시적으로 Read**해야 규약(파일명 규칙·프론트매터 표준·경로 번역 규칙·Staging 워크플로우 등)을 따를 수 있다.

## Notes

- 기본 셸: bash. 도구 호출은 WSL2 절대경로(`/home/ben/...`) 사용.
- gog CLI(`/usr/local/bin/gog`)로 Gmail/Calendar/Drive 작업 가능. 호스트 native에서만.
- 응답은 간결하게. 표·다이어그램은 꼭 필요할 때만.

---

_Update as you learn more about Dr. Ben and his projects._
