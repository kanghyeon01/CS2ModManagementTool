# 시놀로지 NAS에 설치하기

## A. Container Manager가 되는 모델 (권장)

Plus 시리즈(x86)와 DSM 7.2의 일부 J 시리즈(ARM64)에서 Container Manager를 쓸 수 있습니다.
이미지는 amd64·arm64를 모두 지원합니다.

1. File Station에 `docker/cs2mmt/config`, `docker/cs2mmt/data/controller`, `docker/cs2mmt/data/sentinel` 폴더를 만듭니다.
2. `config` 폴더에 다음 파일을 넣습니다.
   - `controller.toml` (`config/controller.example.toml`을 고쳐서), `secrets.toml`
     (모드 목록 `mods.toml`은 저장소 안의 것을 그대로 씁니다)
   - `sentinel.conf` (`sentinel/sentinel.conf.example`을 고쳐서)
3. 이 저장소를 NAS에 **git으로** 받습니다(Package Center의 Git Server 패키지가 `git`을 설치합니다).
   zip으로 받으면 `!update_tool`이 동작하지 않습니다.
   ```
   cd /volume1/docker/cs2mmt && git clone https://github.com/kanghyeon01/CS2ModManagementTool
   ```
4. Container Manager → 프로젝트 → 생성에서 경로를 이 저장소의 `deploy` 폴더로 지정하고
   `docker-compose.yml`을 사용합니다. `.env.example`을 `.env`로 복사해 `REPO_DIR`(받은 저장소),
   `CONFIG_DIR`, `DATA_DIR`을 위 폴더(예: `/volume1/docker/cs2mmt/config`)로 맞춥니다.
   컨테이너가 이미지에 든 코드가 아니라 이 저장소를 그대로 쓰므로, 이후 업데이트는 `!update_tool`뿐입니다.
5. 컨테이너는 보안을 위해 일반 사용자(10001, 10002)로 돕니다. `data` 폴더에 쓰기 권한이 필요하므로
   SSH에서 한 번 실행합니다.
   ```
   sudo chown -R 10001 /volume1/docker/cs2mmt/CS2ModManagementTool   # !update_tool이 git pull 할 수 있게
   sudo chown -R 10001 /volume1/docker/cs2mmt/data/controller
   sudo chown -R 10002 /volume1/docker/cs2mmt/data/sentinel
   sudo chmod 644 /volume1/docker/cs2mmt/config/*.toml /volume1/docker/cs2mmt/config/sentinel.conf
   ```
   (`secrets.toml`, `sentinel.conf`은 컨테이너가 읽어야 하므로 644이되, NAS 계정 공유 설정으로
   다른 사람이 볼 수 없는 폴더에 두세요.)
6. `network_mode: host`가 필요합니다(WoL 브로드캐스트). Container Manager가 경고를 보여도 그대로 둡니다.

## B. Container Manager가 없는 모델

Package Center에서 **Python 3.9** 패키지를 설치하면 컨트롤러가 그대로 돕니다(이 도구는
3.9 호환이고 외부 패키지가 필요 없습니다).

- 컨트롤러: 제어판 → 작업 스케줄러 → 생성 → 트리거된 작업 → 사용자 정의 스크립트,
  이벤트 "부트업", 사용자 root:
  ```
  cd /volume1/cs2mmt && /var/packages/Python3.9/target/usr/bin/python3.9 -m cs2mmt controller run --config /volume1/cs2mmt/config/controller.toml >> /volume1/cs2mmt/data/controller.out 2>&1 &
  ```
- C 감시기: 정적 바이너리를 다른 PC에서 만들어(`docker build --target export -o out sentinel/`)
  `/volume1/cs2mmt/bin/cs2mmt-sentinel`로 복사한 뒤, 같은 방식으로 부트업 작업을 등록합니다.
  ```
  /volume1/cs2mmt/bin/cs2mmt-sentinel --config /volume1/cs2mmt/config/sentinel.conf >> /volume1/cs2mmt/data/sentinel.out 2>&1 &
  ```
  (DSM에는 curl이 기본으로 있습니다.)
- 프로세스가 죽었을 때 다시 띄우도록 같은 스크립트를 "예약된 작업"(10분마다)으로 하나 더
  등록합니다. `deploy/synology/keepalive.sh`가 이미 떠 있으면 아무것도 하지 않습니다.
