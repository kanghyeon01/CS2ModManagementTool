using UnityEngine;

namespace CS2MMTHarness
{
    /// <summary>Drives the test once per frame, in the main menu as well as in a city.</summary>
    public sealed class HarnessBehaviour : MonoBehaviour
    {
        internal HarnessRun Run;

        private void Update()
        {
            Run?.Tick(Time.realtimeSinceStartup);
        }

        private void OnApplicationQuit()
        {
            if (Run != null && !Run.Finished)
                Run.WriteResult(final: true, status: "game quit before the test ended");
        }
    }
}
