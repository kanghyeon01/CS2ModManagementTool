using Colossal.Serialization.Entities;
using Game;
using Game.UI;

namespace CS2MMTHarness
{
    /// <summary>Receives the game's "loading complete" call and tells the running test.</summary>
    public partial class HarnessSystem : UISystemBase
    {
        internal static HarnessRun Run;

        public override GameMode gameMode => GameMode.GameOrEditor;

        protected override void OnGameLoadingComplete(Purpose purpose, GameMode mode)
        {
            base.OnGameLoadingComplete(purpose, mode);
            Run?.OnLoadingComplete(purpose.ToString(), mode.ToString());
        }

        protected override void OnUpdate()
        {
            base.OnUpdate();
        }
    }
}
