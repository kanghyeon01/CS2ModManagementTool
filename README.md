# CS2 Mod Management Tool

Cities: Skylines II가 업데이트되어 모드가 깨지면, **사람 없이** 고치고 시험하고 배포하는 도구입니다.
관리자가 오랫동안 자리를 비우는 동안(군 복무) 모드가 계속 동작하도록 만든 것입니다.

```
 ┌──────────────── 항상 켜져 있는 기기 (2대로 이중화) ────────────────┐
 │  시놀로지 NAS                         데비안/우분투 서버            │
 │  ├ cs2mmt-sentinel (C, 3 MB)          ├ cs2mmt-sentinel (C)        │
 │  └ 컨트롤러 (Python, 주)  ◄── 서로 감시 ──► 컨트롤러 (Python, 보조)  │
 └───────┬───────────────────────────────────────────────┬────────────┘
         │ ① Steam 빌드 변경 감지 · 포럼/GitHub 새 글 감시     │ 디스코드 알림·승인
         │ ② Wake-on-LAN                                     ▼
         ▼                                                 휴대폰
 ┌──────────── 메인 PC (멀티부팅) ────────────┐
 │ Linux: 작업이 있으면 Windows로 1회 재부팅 (efibootmgr BootNext)     │
 │ Windows (자동 로그인 → 에이전트):                                   │
 │   ③ Steam이 새 빌드를 설치할 때까지 대기, 옛 게임 DLL 보관           │
 │   ④ 디컴파일 비교로 "게임 API에서 바뀐 것" 정리                      │
 │   ⑤ 모드 빌드 → 실패하면 Claude Code(헤드리스)가 수정               │
 │   ⑥ 게임 안 스모크 테스트 (하네스 모드가 도시 로드·오류 수집)        │
 │   ⑦ 보안 정책 검사 → 버전 올림 → Paradox Mods 업로드 → GitHub 푸시   │
 │   ⑧ 보고 후 종료                                                    │
 └────────────────────────────────────────────┘
```

## 무엇을 하나요

| 상황 | 동작 |
| --- | --- |
| 게임 업데이트 (Steam 빌드 변경) | 30분 기다려 핫픽스까지 확인 → PC를 깨워 모든 모드를 빌드·수정·시험 → **자동 배포**(설정으로 승인 대기 가능) |
| 고칠 것이 없을 때 | 정상 동작만 확인. 게임 마이너 버전이 바뀌었으면 Paradox Mods의 GameVersion 표기만 갱신 |
| 포럼·GitHub에 새 글 | Claude가 분류(버그/질문/제안/지시문 주입 시도) → 요약과 답글 초안을 디스코드로 → 명확한 버그는 수정 시도 → **승인 후 배포** |
| 매월 1일 | 배포 없이 전 과정 리허설 (만료된 로그인·고장을 휴가 전에 발견) |
| 매주 일요일 19시 | 주간 보고 |
| 토큰 만료 60·30·14·7·3·1일 전 | 갱신 알림 |
| 한 시스템이 죽음 | 다른 시스템이 계속 감시하고 알림을 넘겨받음 |
| `!maintenance` | PC를 Windows로 켜 두고 원격 접속(Jump Desktop 등)을 기다림, `!done`으로 끔 |
| `!update_tool` | 모든 기기의 도구를 GitHub 최신으로 (실패하면 자동 되돌림). PC는 켜질 때마다 스스로 |

## 구성 요소

| 폴더 | 언어 | 하는 일 |
| --- | --- | --- |
| `sentinel/` | C (libc만) | 24시간 감시 핵심: 빌드 감지, WoL, PC에 작업 전달, 긴급 알림. 메모리 3 MB |
| `cs2mmt/controller/` | Python 3.9+ (표준 라이브러리만) | 전체 기능: 포럼·GitHub 감시, 작업 큐, 승인, 디스코드 봇 명령, 보고, 이중화 |
| `cs2mmt/agent/` | Python | PC 쪽: 리눅스 부팅 전환, Windows 작업 파이프라인 |
| `harness/CS2MMTHarness/` | C# (게임 모드) | 스모크 테스트용 모드. 시험 때만 설치되고 배포되지 않음 |
| `prompts/` | — | Claude에게 주는 작업 지시문 |
| `config/mods.toml` | — | 관리할 모드 목록. **모드 추가 = 표 하나 추가** |

## 문서

1. [docs/SETUP.md](docs/SETUP.md) — 설치 (BIOS, 리눅스, Windows, NAS, 서버, 토큰, 디스코드, 리허설)
2. [docs/OPERATIONS.md](docs/OPERATIONS.md) — 운영 (디스코드 명령, 알림 읽는 법, 휴가 때 할 일, 문제 해결)
3. [docs/SECURITY.md](docs/SECURITY.md) — 보안 (지시문 주입 방어, 비밀 관리, 네트워크)
4. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 구조 (작업 흐름, 이중화, API, C/Python 분담과 실측치)
5. [docs/ADDING_A_MOD.md](docs/ADDING_A_MOD.md) — 모드 추가

## 시험

```bash
python -m pytest -q          # 94개: 단위 + 컨트롤러 HTTP + C 감시기 연동 + PC 파이프라인 전 과정 + 하네스 컴파일 + 업데이트
make -C sentinel test        # C 단위 테스트 (AddressSanitizer·UBSan)
```

`tests/test_pipeline_e2e.py`는 가짜 dotnet·Claude·Steam·게임·ModPublisher와 실제 git으로
"게임 업데이트 → 빌드 실패 → Claude 수정 → 스모크 테스트 → 버전 올림 → 업로드 → 태그"와
"정책 위반 차단", "승인 후 배포", "승인 뒤 변조 거부" 등을 처음부터 끝까지 확인합니다.
실제 게임·Steam·Paradox 계정은 이 저장소의 시험으로 확인할 수 없으므로, 떠나기 전에
반드시 `!run rehearsal`로 실제 PC에서 리허설해야 합니다([설치 문서 마지막 단계](docs/SETUP.md#10-떠나기-전-리허설)).
