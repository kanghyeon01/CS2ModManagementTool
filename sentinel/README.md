# cs2mmt-sentinel (C)

24시간 도는 핵심 감시기입니다. libc만 쓰고, HTTPS는 시스템의 `curl` 실행 파일에 맡깁니다
(쉘을 거치지 않고 실행, 비밀 값은 표준 입력의 curl 설정으로 넘겨 프로세스 목록에 나타나지 않음).

- Steam 빌드 확인(api.steamcmd.net) → 30분 안정 대기 → `game_update` 작업 생성
- Wake-on-LAN, 응답 없으면 재전송·재시도
- PC 에이전트용 API(컨트롤러와 같은 형식)
- 같은 기기의 Python 컨트롤러가 죽으면 디스코드 웹후크로 알림
- 상태는 원자적으로 파일에 저장, systemd 워치독 지원

```bash
make            # 빌드 (경고 0, -Wall -Wextra -Wformat=2 -Wshadow, 스택 보호, FORTIFY)
make test       # 단위 테스트 (AddressSanitizer + UBSan)
make static     # 정적 바이너리
./cs2mmt-sentinel --config sentinel.conf          # 실행
./cs2mmt-sentinel --config sentinel.conf --once   # 빌드 확인 1회 후 종료 (설정 확인용)
```

설정은 `sentinel.conf.example`을 보세요. 파이썬 쪽과의 호환은
`tests/test_sentinel_integration.py`가 실제 프로세스·소켓으로 확인합니다(WoL 패킷 수신 포함).
