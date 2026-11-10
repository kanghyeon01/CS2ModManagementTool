namespace Game {
  public enum SystemUpdatePhase { MainLoop, UIUpdate, GameSimulation, Rendering, PreCulling, ModificationEnd, ToolUpdate }
  [System.Flags] public enum GameMode { None = 0, Other = 1, Game = 2, Editor = 4, MainMenu = 8, GameOrEditor = 6, All = 15 }
  public class UpdateSystem { public void UpdateAt<T>(SystemUpdatePhase phase) where T : GameSystemBase {} public Unity.Entities.World World => null; }
  public abstract class GameSystemBase : Unity.Entities.SystemBase { protected override void OnUpdate() {} protected virtual void OnGameLoadingComplete(Colossal.Serialization.Entities.Purpose purpose, GameMode mode) {} }
  public class VersionInfo { public string fullVersion => ""; }
  public static class Version { public static VersionInfo current => null; }
}
namespace Game.Modding { public interface IMod { void OnLoad(Game.UpdateSystem updateSystem); void OnDispose(); } }
namespace Game.UI { public abstract class UISystemBase : Game.GameSystemBase { public virtual Game.GameMode gameMode => Game.GameMode.All; } }
namespace Game.SceneFlow { public class GameManager { public static GameManager instance => null; } }
namespace Game.Simulation { public class SimulationSystem : Game.GameSystemBase { public uint frameIndex => 0; } }
