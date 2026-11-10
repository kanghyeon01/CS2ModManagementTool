# 모드 추가하기

코드는 고칠 필요가 없습니다. `config/mods.toml`에 `[[mod]]` 표를 하나 추가하고, 이 저장소에
커밋한 뒤 NAS·서버의 `mods.toml`과 PC의 체크아웃을 갱신하면 됩니다.

```toml
[[mod]]
id = "MyNewMod"                                   # 영문·숫자·._- (브랜치 이름에도 쓰임)
display_name = "My New Mod"
repo = "https://github.com/kanghyeon01/MyNewMod"
dir = "mods/MyNewMod"                             # PC에 없으면 자동으로 clone
project = "MyNewMod.csproj"
assembly = "MyNewMod"                             # DLL 이름 = 로컬 Mods 폴더 이름(업로드 대상)
depends_on = []                                   # 이 모드가 참조하는 다른 모드
release_with = []                                 # 함께 배포해야 하는 모드
forum_threads = ["https://forum.paradoxplaza.com/forum/threads/....12345/"]
identifiers = ["MyNewMod.", "ui-mods/MyNewMod.mjs"]   # 로그에서 이 모드의 오류를 알아보는 문자열
smoke_expect = ['Game \S+, My New Mod']           # 스모크 테스트 때 모드 로그에 꼭 나와야 하는 줄
version_files = [
    { path = "UI/MyNewMod.mjs", pattern = '^ \* Version: (?P<v>[0-9.]+)\s*$' },
]
readme_game_version = { path = "README.md", pattern = 'Built and tested on game version \*\*(?P<v>[^*]+)\*\*' }
notes = """
Claude에게 전할 설계 규칙 (공개 API, 하면 안 되는 일, 저장 형식 등).
"""
```

확인:
```bash
python -m cs2mmt tools validate --mods config/mods.toml --workspace .
```
빌드 순서, 버전 위치(UI 모듈 헤더와 ModVersion이 같은지), 배포 설정의 문제를 보여 줍니다.

## 모드 쪽에서 해 두면 좋은 것

- `Mod.OnLoad`에서 `"Game " + Game.Version.current.fullVersion + ", <모드 이름> " + 버전`을 로그에
  남기기(기존 모드들처럼). 스모크 테스트가 이 줄로 로드를 확인합니다.
- `Properties/PublishConfiguration.xml`의 `ModId`: 첫 등록(Publish)은 직접 하고 ID를 적어 두세요.
  비어 있으면 도구는 빌드·시험만 하고 업로드하지 않습니다.
- `Thumbnail`에 적힌 파일이 실제로 있어야 업로드됩니다. (현재 No Jaywalking은
  `Properties/Thumbnail.png`가 저장소에 없어 `tools validate`가 경고합니다.)
- `README.md`·`docs/`에 의도한 동작을 적어 두면, Claude가 "의도된 동작"을 버그로 오해하지 않습니다.
