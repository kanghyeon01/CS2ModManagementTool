# 설치 안내

순서대로 하시면 됩니다. 한 단계라도 빠지면 무인 동작이 멈출 수 있으니, 마지막의
**리허설**까지 꼭 마치고 떠나셔야 합니다. 예상 소요: 반나절.

준비물: 메인 PC(Windows + Linux 멀티부팅), 시놀로지 NAS, 데비안/우분투 서버(둘 중 하나만 있어도
동작하지만 둘 다 쓰면 이중화됩니다), 디스코드가 되는 휴대폰, Claude Pro/Max 구독(또는 API 키).

---

## 1. 메인 PC: BIOS/UEFI

| 항목 | 설정 | 이유 |
| --- | --- | --- |
| Wake on LAN / Power On By PCI-E / Resume by PME | **켜기** | 꺼진 PC를 랜으로 켭니다 |
| ErP / EuP Ready (Deep Sleep) | **끄기** | 켜져 있으면 꺼진 상태에서 랜카드 전원이 끊겨 WoL이 안 됩니다 |
| Restore on AC Power Loss | **Power On** (권장) | 정전 뒤 다시 켜져 WoL 대기 상태로 돌아옵니다(리눅스가 일이 없으면 다시 끕니다, 5단계) |
| 부팅 순서 | GRUB(리눅스)이 첫 번째 | 평소처럼 리눅스로 켜지고, 작업이 있을 때만 Windows로 1회 재부팅합니다 |

> 스마트 플러그(전원 재투입)를 쓰면 PC가 완전히 멈췄을 때도 복구할 수 있습니다.
> `controller.toml`의 `[dispatch] power_cycle_command`에 플러그를 끄고 켜는 명령을 넣습니다.

## 2. 메인 PC: Windows

1. **빠른 시작 끄기**: 제어판 → 전원 옵션 → 전원 단추 작동 설정 → "빠른 시작 켜기" 해제.
   멀티부팅과 WoL 모두에 문제를 일으킬 수 있습니다.
2. **랜카드 WoL**: 장치 관리자 → 네트워크 어댑터 → 속성
   - 전원 관리: "이 장치를 사용하여 컴퓨터의 대기 모드를 해제할 수 있음", "매직 패킷으로만" 체크
   - 고급: "Wake on Magic Packet" 사용, (있으면) "Shutdown Wake-On-Lan" 사용
3. **자동 로그인**: Steam과 게임은 로그인된 화면이 있어야 돕니다. Microsoft Sysinternals의
   Autologon(`winget install Microsoft.Sysinternals.Autologon`)을 실행해 계정을 지정합니다.
   비밀번호는 LSA 비밀로 암호화되어 저장됩니다(관리자는 꺼낼 수 있으니 집 PC에서만 쓰세요).
4. **Windows 업데이트**: 작업 중 재부팅되지 않도록 사용 시간(활성 시간)을 넓게 잡거나 업데이트를
   일시 중지합니다. 휴가 때 업데이트를 몰아서 하세요.
5. **절전**: 에이전트가 작업 중에는 절전을 막지만, "절전 안 함"으로 두는 편이 안전합니다.
6. **도구 설치** (PowerShell):
   ```powershell
   winget install -e --id Python.Python.3.12
   winget install -e --id Git.Git
   winget install -e --id Microsoft.DotNet.SDK.8
   irm https://claude.ai/install.ps1 | iex
   dotnet tool install -g ilspycmd
   ```
   Git for Windows가 있어야 Claude Code가 Bash 도구를 씁니다.
7. **GitHub 푸시 권한**: 만료가 없는 SSH 키를 권장합니다.
   ```powershell
   ssh-keygen -t ed25519 -C "cs2mmt-pc"
   ```
   공개 키(`~\.ssh\id_ed25519.pub`)를 GitHub → Settings → SSH and GPG keys에 등록하고,
   모드 저장소의 원격 주소를 `git@github.com:kanghyeon01/<저장소>.git`으로 둡니다.
   (세분화 토큰(fine-grained PAT)은 최대 366일이라 군 복무 기간보다 짧습니다.)
8. **Steam**: "로그인 정보 기억", Windows 시작 시 자동 실행은 **끄기**(에이전트가 직접 켭니다.
   그래야 업데이트 전에 옛 게임 DLL을 보관할 수 있습니다). 게임 속성 → 업데이트 →
   "이 게임을 항상 최신 상태로 유지". 다운로드 시간 제한은 두지 않습니다.
9. **게임 준비**
   - 게임 안 옵션 → Modding에서 모딩 도구(툴체인)를 설치합니다. ModPublisher가 함께 설치됩니다.
   - 게임 안에서 **Paradox 계정에 로그인**합니다. ModPublisher는 게임에 마지막으로 로그인한
     계정으로 자동 로그인합니다. 업로드할 때 게임은 꺼져 있어야 합니다(도구가 알아서 끕니다).
   - **Paradox Mods에서 구독한 같은 모드는 꺼 둡니다**(로컬 빌드와 겹치면 두 번 로드됩니다).
   - 시험용 도시를 하나 만들어 `CS2MMT-Test`라는 이름으로 저장합니다. 공항 2곳 이상(Realistic
     Aviation과 기상 애드온 시험용)과 교차로가 있으면 좋습니다. 마지막으로 연 세이브가 이것이어야
     `-continuelastsave`로 바로 열립니다. 그렇지 않으면 하네스가 이름으로 찾아 엽니다.
10. **에이전트 설치**:
    ```powershell
    git clone https://github.com/kanghyeon01/CS2ModManagementTool C:\CS2ModManagementTool
    cd C:\CS2ModManagementTool
    powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
    ```
    스크립트가 `%LOCALAPPDATA%\cs2mmt\secrets.toml`과 `config\agent.toml`을 만들고, 모드를
    `mods\` 아래로 내려받고, 로그온 시 실행되는 예약 작업("CS2MMT Agent")을 등록한 뒤 자체 점검을
    실행합니다. `agent.toml`에서 컨트롤러 주소, `test_save`, git 작성자를 맞춥니다.

## 3. Claude 인증 (가장 중요한 만료 항목)

PC의 PowerShell에서:
```powershell
claude setup-token
```
브라우저에서 승인하면 토큰이 화면에 한 번 나옵니다. `%LOCALAPPDATA%\cs2mmt\secrets.toml`의
`claude_oauth_token`에 넣습니다. **이 토큰은 1년 동안만 유효합니다.** 복무 기간이 더 길면
휴가 때 다시 발급해야 합니다. 컨트롤러 설정의 `[[reminders]]`에 만료일을 적어 두면 60일 전부터
알려 줍니다. (만료 걱정 없이 쓰려면 Claude Console의 API 키(`anthropic_api_key`, 사용량 과금)를
대신 넣을 수 있습니다.)

> 이 토큰은 모델 요청만 할 수 있어 Remote Control 세션을 만들 수 없습니다. 그래서 이 도구는
> claude.ai의 특정 세션에 신호를 보내는 대신, PC에서 `claude -p`(헤드리스)로 작업마다 새
> 세션을 엽니다. 지난 맥락은 저장소 문서(README, docs/PLAN.md)와 `config/mods.toml`의
> `notes`로 전달됩니다. 한 세션을 계속 이어 쓰려면 `[claude] session_mode = "resume"`.

## 4. 디스코드

알림, 명령, 승인은 모두 본인만 있는 디스코드 개인 서버의 채널 하나에서 합니다.
컨트롤러는 봇으로 이 채널을 10초마다 읽고, C 감시기는 같은 채널의 웹후크로 알림만 보냅니다.

1. 디스코드에서 개인 서버를 만들고(서버 추가 → 직접 만들기) 알림용 채널을 하나 둡니다. 예: `#cs2-모드관리`.
2. 개발자 모드 켜기: 사용자 설정 → 고급 → 개발자 모드. 이후 채널이나 내 이름을 우클릭(휴대폰은 길게 누르기)하면
   ID 복사가 나옵니다.
3. 봇 만들기: https://discord.com/developers/applications → New Application → 왼쪽 Bot에서 Reset Token을 눌러
   나온 토큰을 컨트롤러 `secrets.toml`의 `discord_bot_token`에 넣습니다(한 번만 보입니다). 같은 화면 아래
   Privileged Gateway Intents에서 **Message Content Intent를 켭니다.** 이게 꺼져 있으면 봇이 채널 메시지 내용을
   빈칸으로 받아 명령을 읽지 못합니다.
4. 봇을 서버에 초대: 왼쪽 OAuth2 → URL Generator에서 scopes는 `bot`, Bot Permissions는 View Channels,
   Send Messages, Read Message History, Add Reactions를 고르고, 아래 생성된 주소를 열어 개인 서버를 고릅니다.
5. `controller.toml`의 `[notify]`에 채널 ID(`discord_channel_id`)와 **내 사용자 ID**(`discord_owner_user_id`)를
   넣습니다. 명령과 ✅/❌ 반응은 이 사용자의 것만 받아들입니다. 두 컨트롤러(NAS, 서버) 모두 같은 값입니다.
6. C 감시기용 웹후크: 채널 설정 → 연동 → 웹후크 → 새 웹후크 → 웹후크 URL 복사 → `sentinel.conf`의
   `discord_webhook_url`. 이 주소를 아는 사람은 채널에 글을 쓸 수 있으니 공유하지 마세요.
7. 확인: 채널에 `!status`를 보내면 10초 안에 봇이 답합니다. 명령은 `!`로 시작합니다(`/`로 시작하면 디스코드
   자체 명령 선택창이 뜨기 때문입니다). 봇을 멘션해 `@봇 !status`처럼 써도 됩니다.

## 5. 메인 PC: Linux (GRUB 멀티부팅)

```bash
sudo apt install python3 git efibootmgr ethtool
git clone https://github.com/kanghyeon01/CS2ModManagementTool ~/CS2ModManagementTool
cd ~/CS2ModManagementTool
sudo sh deploy/linux-pc/install.sh        # 처음엔 /etc/cs2mmt/agent-linux.toml만 만들고 멈춥니다
sudo nano /etc/cs2mmt/agent-linux.toml    # 컨트롤러 주소, api_token
sudo sh deploy/linux-pc/install.sh        # 서비스 등록 (몇 번 다시 실행해도 됩니다)
sudo python3 -m cs2mmt agent linux-switch --check --config /etc/cs2mmt/agent-linux.toml
```

**GRUB이 잡혀 있어도 Windows로 부팅할 수 있습니다.** 마지막 `--check`가 재부팅 없이 어떤 방법을
쓸지 보여 줍니다(`method = "auto"`).

- **UEFI 설치 (대부분)**: GRUB도 펌웨어에 등록된 부팅 항목 중 하나일 뿐이라, `efibootmgr --bootnext`로
  "다음 한 번만 Windows Boot Manager"를 지정하면 GRUB을 거치지 않고 바로 Windows가 뜹니다.
  GRUB 설정은 건드리지 않고, 그다음 부팅부터는 다시 GRUB(리눅스)입니다. `--check`에
  `감지 결과: efibootmgr → 'Windows Boot Manager'`가 나오면 끝입니다.
- **레거시 BIOS이거나 UEFI 목록에 Windows가 없을 때**: GRUB 메뉴의 Windows 항목으로 `grub-reboot`를
  씁니다. 이때는 `/etc/default/grub`에 `GRUB_DEFAULT=saved`가 필요합니다. 고친 뒤
  `sudo update-grub`, 평소 기본값을 리눅스로 두려면 `sudo grub-set-default 0`. `--check`가
  필요하면 이 안내를 그대로 보여 줍니다.
- **GRUB 메뉴에 Windows가 아예 없을 때**: `/etc/default/grub`에 `GRUB_DISABLE_OS_PROBER=false`를
  넣고 `sudo update-grub`.

그 밖에:
- 리눅스에서 꺼도 WoL이 유지되도록 설정합니다(설치 스크립트가 현재 상태를 보여 줍니다).
  NetworkManager라면 `nmcli connection modify <연결이름> 802-3-ethernet.wake-on-lan magic`.
- 작업이 아닌 부팅(직접 켬, 정전 복구)은 건드리지 않습니다. 정전 복구 뒤 아무도 로그인하지 않으면
  끄게 하려면 `idle_shutdown_minutes = 15`.
- 로그인한 사람이 있으면 재부팅 60초 전에 알리고, `sudo touch /run/cs2mmt-cancel`로 취소됩니다.
- 부팅할 때마다 `/opt/cs2mmt`를 GitHub 최신으로 당겨 오므로 따로 업데이트할 필요가 없습니다.

## 6. 시놀로지 NAS (주 시스템)

[deploy/synology/README.md](../deploy/synology/README.md)를 따릅니다. 요약:
- Container Manager가 되면 `deploy/docker-compose.yml`로 컨트롤러 + C 감시기를 띄웁니다(호스트 네트워크).
- 안 되면 Python 3.9 패키지와 정적 C 바이너리를 작업 스케줄러로 띄웁니다.
- `controller.toml`: `id = "nas"`, `role = "primary"`, `[[peers]]`에 서버 주소.
- `sentinel.conf`: `id = nas-sentinel`, `controller_url = http://127.0.0.1:8765`.

## 7. 데비안/우분투 서버 (보조 시스템)

도커가 있으면 NAS와 같은 compose 파일을 씁니다. 없으면:
```bash
sudo apt install python3 build-essential curl git
sudo useradd --system --home /var/lib/cs2mmt --create-home cs2mmt
sudo git clone https://github.com/kanghyeon01/CS2ModManagementTool /opt/cs2mmt
sudo chown -R cs2mmt /opt/cs2mmt          # !update_tool이 git pull 할 수 있게
sudo install -d -o cs2mmt -m 750 /etc/cs2mmt
# /etc/cs2mmt 에 controller.toml (id="server", role="secondary", data_dir="/var/lib/cs2mmt",
#   mods_file="/opt/cs2mmt/config/mods.toml", prompts_dir="/opt/cs2mmt/prompts"),
#   secrets.toml(600), sentinel.conf(state_file=/var/lib/cs2mmt/sentinel.state)
sudo cp /opt/cs2mmt/deploy/systemd/cs2mmt-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cs2mmt-controller cs2mmt-sentinel
```
systemd의 `WatchdogSec`이 멈춘 프로세스를 다시 시작하고, C 감시기는 시작할 때마다 저장소의 소스로
다시 빌드됩니다(실패하면 이전 바이너리).

## 8. 외부 생존 신호 (선택)

두 시스템이 동시에 꺼지면(정전, 인터넷 끊김) 도구 스스로는 알릴 방법이 없습니다. 이때를 대비하려면
직접 고른 감시 서비스(정해진 주소로 신호가 끊기면 알려 주는 "하트비트" 방식이면 무엇이든 됩니다)에
확인 주소를 4개(NAS 컨트롤러·감시기, 서버 컨트롤러·감시기) 만들어 컨트롤러의 `[health] healthcheck_url`과
감시기의 `healthcheck_url`에 넣습니다. 5분마다 그 주소를 부르고, 신호가 끊기면 그 서비스가 알려 줍니다.
Steam 확인이 오래 실패하면 신호를 멈추고, 서비스가 "실패" 주소를 따로 주면 `healthcheck_fail_url`에 넣어
그 주소를 대신 부르게 할 수 있습니다. 쓰지 않으려면 비워 두면 됩니다.

## 9. 업데이트: 다시 설치하지 않습니다

설치는 한 번만 하고, 이후 도구를 고치면 **GitHub의 `main`에 푸시한 뒤 디스코드에서 `!update_tool`**
한 번이면 됩니다.

| 기기 | 업데이트되는 때 |
| --- | --- |
| NAS·서버 컨트롤러 | `!update_tool` → `git pull`(빨리감기만) → 새 코드 검사 → 다시 시작. 피어에도 전달 |
| NAS·서버 C 감시기 | 같은 때 재시작 요청 → 새 소스로 다시 컴파일 (실패하면 이전 바이너리로 계속) |
| PC Windows 에이전트 | 켜질 때마다 스스로 `git pull` 후 새 버전으로 실행 |
| PC 리눅스 전환기 | 부팅할 때마다 `git pull` |

- 새 코드가 불러와지지 않으면(문법 오류 등) **이전 커밋으로 자동으로 되돌리고** 알려 드립니다.
- 추적 중인 파일을 기기에서 직접 고쳐 두었으면 덮어쓰지 않고 업데이트를 건너뜁니다. 설정·비밀
  파일·`mods/`·`cache/`·`work/`는 git이 무시하므로 영향이 없습니다.
- 모드 추가도 같습니다: `config/mods.toml`을 고쳐 푸시하고 `!update_tool`.
- SSH로 직접 할 때는 기기에서 `sh deploy/update.sh`.
- 업데이트는 `main` 브랜치를 따라갑니다(`[update] branch`로 변경 가능). 지금 작업 브랜치를 먼저
  `main`에 합쳐 두세요. 저장소가 비공개라면 각 기기에 읽기 전용 배포 키(deploy key)를 등록해야
  `git pull`이 됩니다. 이 저장소에는 비밀 값이 없으므로 공개로 두는 편이 간단합니다.
- 시험하다 꼬였을 때: `!clear`(대기 작업 모두 취소), `!cancel <id>`(진행 중 작업), 설치 스크립트는
  다시 실행해도 기존 설정을 지우지 않습니다. 상태를 완전히 처음으로 돌리려면 NAS의 `data` 폴더만
  지우면 됩니다(설정은 그대로).

## 10. 떠나기 전 리허설

1. 디스코드 채널에 `!status` → 두 시스템이 서로 "응답"으로 보이는지 확인합니다.
2. PC를 끈 상태에서 `!run rehearsal` → PC가 켜지고, 리눅스가 Windows로 재부팅하고, 빌드·스모크
   테스트까지 하고 꺼지는지 봅니다(배포는 하지 않습니다). 결과 메시지의 ❌ 항목을 모두 고칩니다.
3. `!run update` → 실제 게임 업데이트 대응을 한 번 끝까지 실행합니다. 고칠 것이 없으면 "변경 없음"으로
   끝나고 아무것도 배포하지 않습니다.
4. NAS 컨테이너를 잠시 멈춰 보고(`docker stop`), 15분 뒤 서버가 "응답하지 않습니다"라고 알리는지 확인합니다.
5. 휴대폰 디스코드에서 승인 요청 메시지의 ✅ 반응이 처리되는지 확인합니다(버그 수정 시험은 GitHub에 시험용 이슈를 올려 볼 수 있습니다).
6. 휴대폰 데이터(집 와이파이 말고)로 `!maintenance` → Jump Desktop 접속 → `!done`까지 해 봅니다.
