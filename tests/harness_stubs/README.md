# API stubs for compiling the harness without the game

Minimal stand-ins for the game assemblies the harness references, with only the members it uses
at compile time. Their shapes are taken from the owner's mods, which compile against the real game
(`GameSystemBase`, `UISystemBase.gameMode`, `OnGameLoadingComplete(Purpose, GameMode)`,
`World.DefaultGameObjectInjectionWorld`, `GetExistingSystemManaged<T>()`, `SimulationSystem.frameIndex`,
`Game.Version.current.fullVersion`, `LogManager.GetLogger(...).SetShowsErrorsInUI(false)`, ...).

`tests/test_harness_build.py` builds these and then the harness against them. A pass means the
harness is valid C# for those shapes; the real check is the rehearsal on the PC with the game.
