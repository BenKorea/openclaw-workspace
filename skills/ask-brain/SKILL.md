---
name: ask-brain
description: 텔레그램에서 2nd-brain vault 의 지식을 검색·추론해 답을 받는다(원격 인출). **자연어 트리거**: "브레인에 물어봐 …"·"2nd-brain 에서 찾아줘 …"·"내 노트에서 … 정리해줘"·"vault 에서 … 추론해줘". 무거운 검색·추론은 게이트웨이에서 직접 돌리지 않고(watchdog·vault 미마운트), **호스트 Claude Code 에 비동기로 위임**한다 — 이 스킬은 질문을 공유 큐에 job 으로 떨구기만 하고 즉시 끝낸다. 답은 잠시 뒤 호스트 러너가 같은 대화로 회신한다.
allowed_tools: [bash]
metadata: {"clawdbot":{"emoji":"🧠","requires":{"bins":["python3"]}}}
---

# ask-brain

텔레그램 질문을 **호스트 Claude Code 의 vault 검색·추론**으로 위임하는 *얇은 트리거*다.
무거운 일은 하지 않는다 — 질문을 공유 마운트 큐에 적고 즉시 반환한다(게이트웨이 watchdog 회피).
실제 검색·추론·회신은 호스트 `ask-brain.sh`(systemd path 유닛) 가 비동기로 한다.

> 설계 근거: vault 는 OpenClaw 컨테이너에 **마운트하지 않는다**(재무·PHI·인맥 보호). vault 는 호스트에
> 남고 **답만** 텔레그램으로 건너간다. 슬20 공격면(인젝션→egress)·watchdog 을 구조적으로 회피.
> 전체 그림은 MERGE 덱 슬22(2nd-brain keystone) + vault 노트 `01_projects/.../ask-brain`.

## 호출

자연어 트리거는 frontmatter `description` 이 담당한다. 에이전트는 사용자 질문 본문을 그대로 넘겨
**아래 명령을 1회만** 실행하고 끝낸다 — 직접 vault 를 뒤지거나 추론하려 하지 말 것(컨테이너엔 vault 가 없다).

```bash
~/.openclaw/workspace/skills/ask-brain/enqueue.sh "<사용자 질문 그대로>"
```

- `enqueue.sh` 가 `~/.openclaw/workspace/ask-brain-queue/jobs/<id>.json` 에 질문 + 회신 대상을 적는다(원자적 mv).
- 회신 대상(`reply_target`)은 **현재 대화의 텔레그램 chat id**여야 한다. 가능하면 그 chat id 를
  `enqueue.sh "<질문>" "<chat_id>"` 2번째 인자로 넘겨라. 못 넘기면 호스트 러너가 설정 기본값
  (`ASK_BRAIN_TARGET`, 단일 사용자 MVP)으로 회신한다.
- enqueue 후 사용자에겐 "브레인에 물어보는 중입니다 — 곧 답이 옵니다" 정도만 알리고 끝낸다.

## 하지 말 것 (엄수)

- **직접 답하려 하지 말 것.** 너(컨테이너)에겐 vault 가 없어 네가 답하면 *틀린다*. memory·웹·다른 도구로
  답을 지어내지 말 것. "찾아볼게요/먼저 ~부터" 식으로 스스로 조사 시작 금지.
- **이 턴에 할 일은 단 하나**: `enqueue.sh "<질문>" "<현재 chat id>"` 를 1회 실행 → 한 줄 ack → **즉시 종료**.
  vault 검색·추론·요약은 전부 호스트가 한다. 너는 우체통에 편지를 넣을 뿐이다.
- 하위 에이전트 위임·단계 반복·도구 탐색 금지. enqueue 외 어떤 행동도 하지 말 것.
- ack 예: "🧠 브레인에 물어보는 중입니다 — 잠시 뒤 답을 보내드릴게요." (그리고 끝. 추가 작업 없음)
