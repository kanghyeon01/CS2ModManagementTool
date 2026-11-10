# 구조

## 왜 C와 Python을 나눴나

24시간 켜져 있어야 하는 부분(빌드 감지, WoL, PC에 작업 전달)은 C로 만든 `cs2mmt-sentinel`이
맡고, 나머지는 Python이 맡습니다.

같은 일(2초마다 Steam 빌드 확인, 44 KB 응답, 60초간 30회)을 시켜 실측한 값입니다.

| | 메모리(RSS) | 확인 1회당 CPU | 15분 주기로 환산한 하루 CPU |
| --- | --- | --- | --- |
| C 감시기 | **3.1 MB** | 11.0 ms (대부분 curl 프로세스 실행 비용) | 2분 미만 (CPU 사용률 0.1% 미만) |
| Python 컨트롤러 3.13 | 30.9 MB | **1.3 ms** | 1분 미만 |
| Python 컨트롤러 3.9 (시놀로지 패키지) | 26.4 MB | 1.7 ms | 1분 미만 |

- 메모리는 C가 약 10배 적습니다. 램이 작은 NAS에 유리합니다.
- CPU는 둘 다 무시할 수준입니다. C는 HTTPS를 위해 매번 curl을 실행하므로 1회당 비용이 오히려 큽니다.
- 일의 대부분(빌드, 게임 실행, Claude)은 PC에서 다른 프로그램을 **기다리는** 시간입니다. 그래서 PC
  쪽을 C로 바꿔도 빨라지지 않고, C의 메모리 오류 위험만 늘어납니다. 포럼 HTML 해석, 승인, 디스코드
  명령처럼 복잡하지만 드물게 도는 기능은 Python에 두었습니다.
- 두 구현이 같은 API를 말하므로, **한쪽 코드에 버그가 있어도 다른 쪽이 PC를 깨웁니다**(서로 다른
  구현에 의한 이중화).

## 기기 배치와 이중화

```
NAS:    sentinel(C) :8766   controller(Python, primary)   :8765
Server: sentinel(C) :8766   controller(Python, secondary) :8765
PC agent: 위 4곳 모두에 작업을 묻고, 같은 work_key는 한 번만 실행
```

- 네 곳이 각자 감시하고 각자 큐를 가집니다(공유 DB 없음, 한쪽 장애가 다른 쪽에 번지지 않음).
- 같은 게임 업데이트는 `work_key = game_update:<빌드>`로 식별됩니다. 에이전트가 끝내면 네 곳 모두에
  "완료"를 알려 나머지 사본을 닫습니다. 알림을 놓친 곳의 사본은 다음에 받아도 에이전트가 이미 한
  일로 알아보고 바로 닫습니다(더 높은 빌드를 처리했으면 그것으로도 닫힘).
- 알림: 주 컨트롤러만 말합니다. 보조는 주 시스템이 10분 넘게 응답하지 않으면 넘겨받고 그 사실을
  알립니다. C 감시기는 같은 기기의 Python 컨트롤러가 15분 넘게 응답하지 않을 때만 말합니다.
- 디스코드 채널의 명령 읽기와 ✅/❌ 반응 확인도 말하는 쪽 하나만 합니다(두 곳이 같은 명령을 두 번 처리하지 않도록).
- 승인 결정은 받은 쪽이 다른 컨트롤러로 전달합니다(상대가 죽어 있으면 보관했다가 다시 보냄).
  에이전트는 배포 전에 모든 컨트롤러에 물어 **하나라도 거절이면 배포하지 않습니다**.
- 멈춤 대비: Python 컨트롤러는 메인 루프가 10분간 돌지 않으면 스스로 종료하고, 도커 재시작 정책이나
  systemd가 다시 띄웁니다(도커는 "응답 없음" 상태만으로는 재시작하지 않기 때문입니다). systemd에서는
  두 프로그램 모두 `WATCHDOG=1`을 보냅니다. 외부 생존 신호(직접 고른 감시 서비스, 선택)는 둘 다 꺼진 경우에 대비합니다.

## 작업 종류와 흐름

| 종류 | 만드는 곳 | PC에서 하는 일 |
| --- | --- | --- |
| `game_update` | Steam 감시 (컨트롤러, 감시기), `!run update` | 아래 전체 |
| `triage` | 포럼·GitHub 새 글 (컨트롤러) | 도구 없는 Claude 세션으로 분류 → 컨트롤러가 버그 수정 작업 생성 |
| `bugfix` | 분류 결과 | 제보 기반 수정 → 빌드 → 스모크 → 정책 → 승인 요청 |
| `publish_approved` | 승인 | 승인된 커밋 그대로 빌드·(게임이 바뀌었으면 재시험)·업로드 |
| `rehearsal` | 매월, `!run rehearsal` | 도구·인증·Steam·푸시 권한·빌드·스모크·ModPublisher 위치 점검, 배포 없음 |

`game_update` 순서 (`cs2mmt/agent/pipeline.py`):
1. 사전 점검(dotnet, git, claude, 디스크).
2. **Steam을 켜기 전에** 현재 빌드의 게임 DLL을 `cache/managed/<빌드>`에 복사합니다.
3. Steam을 켜고 새 빌드가 설치될 때까지 기다립니다(15분간 변화가 없으면 Steam 재시작, 2시간 제한).
4. 모드 저장소 동기화(남은 변경은 `git stash`로 보관).
5. 옛/새 DLL을 ilspycmd로 디컴파일하고, 모드 소스가 이름을 쓰는 타입만 골라 차이를 정리합니다.
6. 의존 순서대로 빌드(RA → 기상 애드온 → No Jaywalking). 실패하면 Claude 세션(최대 2회).
7. 하네스와 함께 게임 실행 → 도시 로드 → 6분 시뮬레이션 → 로그에서 모드별 오류 판정.
   실패하면 스택 트레이스를 Claude에게 주고 1회 더.
8. Claude의 변경만 정책 검사 → 통과하면 도구가 버전(PATCH +1), 변경 기록(영/한), GameVersion,
   README의 시험 버전을 고치고 다시 빌드 → `cs2mmt/<작업>-<모드>` 브랜치로 커밋·푸시.
9. 승인 정책 `auto`면 ModPublisher `NewVersion` → 성공하면 기본 브랜치를 그 커밋으로 옮기고
   `vX.Y.Z` 태그를 푸시. 아니면 승인 요청.
10. 결과 보고(한국어 요약) → PC 종료.

## 컨트롤러 API (공유 토큰 필요, `/health` 제외)

| 메서드·경로 | 용도 | C 감시기 |
| --- | --- | --- |
| `GET /api/v1/health` | 생존 확인 | ✓ |
| `GET /api/v1/status` | 사람이 읽는 상태 | ✓ |
| `GET /api/v1/boot-decision?boot_time=` | 리눅스가 Windows로 갈지 | ✓ |
| `POST /api/v1/jobs/claim` | 작업 받기 (같은 PC가 다시 물으면 이어받기) | ✓ |
| `POST /api/v1/jobs/<id>/heartbeat` | 진행 보고, 취소 여부 | ✓ |
| `POST /api/v1/jobs/<id>/complete` | 결과 (`result.retry`면 나중에 재시도) | ✓ |
| `POST /api/v1/dedupe/complete` | 다른 곳에서 끝난 일 닫기 | ✓ |
| `POST /api/v1/jobs` | 수동 작업 | game_update만 |
| `POST /api/v1/approvals`, `GET /api/v1/approvals/<id>` | 승인 요청·상태 | — (501) |
| `POST /api/v1/peer/approval-decision` | 컨트롤러 간 결정 전달 | — |
| `POST /api/v1/notify` | PC가 보내는 알림 | ✓ |

## 게임 안 하네스

`harness/CS2MMTHarness`는 요청 파일(`ModsData/CS2MMTHarness/request.json`, 3시간 안에 만든 것)이
있을 때만 동작합니다. 도시 로드는 게임의 `OnGameLoadingComplete`와 `GameManager` 상태로 확인하고,
게임이 메뉴에 머무르면 지정한 세이브를 직접 엽니다. Unity 로그의 오류·예외를 스택과 함께 모아
`result.json`에 쓰고 게임을 종료합니다. 기존 모드들이 실제로 쓰는(컴파일되는) API만 직접 쓰고,
세이브 열기·시뮬레이션 속도처럼 확인하지 못한 API는 리플렉션으로 감싸, 게임이 바뀌어도 하네스
빌드가 깨지지 않게 했습니다.
