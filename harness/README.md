# CS2MMTHarness (스모크 테스트용 게임 모드)

에이전트가 스모크 테스트를 할 때만 로컬 Mods 폴더에 설치하고, 끝나면 지웁니다. Paradox Mods에
올리지 않습니다. 요청 파일이 없거나 3시간보다 오래되면 아무 일도 하지 않으므로, 실수로 남아
있어도 평소 플레이에 영향이 없습니다.

요청: `%USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\CS2MMTHarness\request.json`
```json
{"job": "gameupda-...", "save": "CS2MMT-Test", "run_seconds": 360, "load_timeout_seconds": 900,
 "menu_wait_seconds": 90, "sim_speed": 3, "quit": true}
```
결과: 같은 폴더의 `result.json` (진행 중에는 `result.partial.json`)
```json
{"final": true, "status": "ok", "city_loaded": true, "game_version": "1.6.2f1",
 "simulated_frames": 21600, "error_count": 0, "errors": [{"type": "Exception", "message": "...", "stack": "..."}]}
```

빌드는 기존 모드들과 같은 방식(게임의 Managed 폴더를 직접 참조, 툴체인 불필요)입니다.
```
dotnet build -c Release -p:CS2ManagedPath="<게임 폴더>\Cities2_Data\Managed"
```
