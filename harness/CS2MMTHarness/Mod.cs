using System;
using System.IO;
using Colossal.Logging;
using Game;
using Game.Modding;
using UnityEngine;

namespace CS2MMTHarness
{
    /// <summary>
    /// Smoke-test harness. Does nothing unless the agent left a fresh request file in
    /// ModsData/CS2MMTHarness/request.json; then it waits for a city (loading one itself if the
    /// game stays in the main menu), lets the simulation run, records every error Unity logs,
    /// writes result.json and quits the game.
    /// </summary>
    public sealed class Mod : IMod
    {
        public const string kName = "CS2MMTHarness";

        public static readonly ILog Log = LogManager.GetLogger(kName).SetShowsErrorsInUI(false);

        private HarnessRun m_Run;

        public void OnLoad(UpdateSystem updateSystem)
        {
            string dir = Path.Combine(Application.persistentDataPath, "ModsData", kName);
            Request request = Request.TryRead(Path.Combine(dir, "request.json"), TimeSpan.FromHours(3), out string why);
            if (request == null)
            {
                Log.Info("No test requested (" + why + "); the harness stays idle.");
                return;
            }
            Log.Info("Smoke test requested for job " + request.Job + "; game " + Game.Version.current.fullVersion);
            m_Run = new HarnessRun(request, dir);
            m_Run.Start();

            var host = new GameObject("CS2MMTHarness");
            UnityEngine.Object.DontDestroyOnLoad(host);
            host.AddComponent<HarnessBehaviour>().Run = m_Run;

            // OnGameLoadingComplete is the game's own signal that a city finished loading.
            updateSystem.UpdateAt<HarnessSystem>(SystemUpdatePhase.UIUpdate);
            HarnessSystem.Run = m_Run;
        }

        public void OnDispose()
        {
            m_Run?.Stop();
        }
    }
}
