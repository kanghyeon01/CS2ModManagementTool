using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using Colossal.IO.AssetDatabase;
using Colossal.Serialization.Entities;
using Game;
using Game.SceneFlow;
using Game.Simulation;
using Unity.Entities;
using UnityEngine;

namespace CS2MMTHarness
{
    /// <summary>The request the agent writes before starting the game.</summary>
    public sealed class Request
    {
        public string Job = "";
        public string Save = "";
        public float RunSeconds = 360f;
        public float LoadTimeoutSeconds = 900f;
        public float MenuWaitSeconds = 90f;
        public float SimSpeed = 3f;
        public bool Quit = true;

        public static Request TryRead(string path, TimeSpan maxAge, out string why)
        {
            why = "";
            try
            {
                if (!File.Exists(path))
                {
                    why = "no request file";
                    return null;
                }
                if (DateTime.UtcNow - File.GetLastWriteTimeUtc(path) > maxAge)
                {
                    why = "request file is stale";
                    return null;
                }
                string text = File.ReadAllText(path);
                var r = new Request
                {
                    Job = Str(text, "job") ?? "",
                    Save = Str(text, "save") ?? "",
                    RunSeconds = Num(text, "run_seconds", 360f),
                    LoadTimeoutSeconds = Num(text, "load_timeout_seconds", 900f),
                    MenuWaitSeconds = Num(text, "menu_wait_seconds", 90f),
                    SimSpeed = Num(text, "sim_speed", 3f),
                    Quit = !Regex.IsMatch(text, "\"quit\"\\s*:\\s*false"),
                };
                return r;
            }
            catch (Exception e)
            {
                why = e.GetType().Name + ": " + e.Message;
                return null;
            }
        }

        private static string Str(string json, string key)
        {
            Match m = Regex.Match(json, "\"" + Regex.Escape(key) + "\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"");
            return m.Success ? Regex.Unescape(m.Groups[1].Value) : null;
        }

        private static float Num(string json, string key, float fallback)
        {
            Match m = Regex.Match(json, "\"" + Regex.Escape(key) + "\"\\s*:\\s*(-?[0-9.]+)");
            return m.Success && float.TryParse(m.Groups[1].Value, NumberStyles.Float, CultureInfo.InvariantCulture, out float v) ? v : fallback;
        }
    }

    /// <summary>One smoke test: timeline, error capture and the result file.</summary>
    public sealed class HarnessRun
    {
        private const int kMaxErrors = 200;

        private readonly Request m_Request;
        private readonly string m_Dir;
        private readonly object m_Lock = new object();
        private readonly System.Diagnostics.Stopwatch m_Clock = new System.Diagnostics.Stopwatch();
        private readonly List<string[]> m_Errors = new List<string[]>(); // type, message, stack, time
        private int m_ErrorCount;
        private float m_Start = -1f;
        private float m_LoadedAt = -1f;
        private float m_LastPartial;
        private bool m_LoadAttempted;
        private string m_LoadSource = "";
        private string m_LoadNote = "";
        private uint m_FrameAtLoad;
        private uint m_FrameAtEnd;
        private bool m_SpeedSet;
        private string m_Phase = "starting";
        private volatile bool m_CityLoaded;
        private bool m_QuitRequested;

        public bool Finished { get; private set; }

        public HarnessRun(Request request, string dir)
        {
            m_Request = request;
            m_Dir = dir;
        }

        public void Start()
        {
            m_Clock.Start();
            Application.logMessageReceivedThreaded += OnLog;
        }

        public void Stop()
        {
            Application.logMessageReceivedThreaded -= OnLog;
        }

        private void OnLog(string condition, string stackTrace, LogType type)
        {
            if (type != LogType.Exception && type != LogType.Error && type != LogType.Assert)
                return;
            lock (m_Lock)
            {
                m_ErrorCount++;
                if (m_Errors.Count < kMaxErrors)
                {
                    double t = m_Clock.Elapsed.TotalSeconds; // Unity's Time is main-thread only
                    m_Errors.Add(new[] { type.ToString(), Cut(condition, 2000), Cut(stackTrace, 6000), t.ToString("0.0", CultureInfo.InvariantCulture) });
                }
            }
        }

        public void OnLoadingComplete(string purpose, string mode)
        {
            Mod.Log.Info("Loading complete: " + purpose + " / " + mode);
            if (mode == "Game")
            {
                if (string.IsNullOrEmpty(m_LoadSource))
                    m_LoadSource = m_LoadAttempted ? "harness" : "game";
                m_CityLoaded = true;
            }
        }

        public void Tick(float now)
        {
            if (Finished)
            {
                return;
            }
            if (m_Start < 0)
                m_Start = now;
            float elapsed = now - m_Start;
            if (!m_CityLoaded && GameModeIs("Game") && !IsLoading())
                m_CityLoaded = true; // in case the loading callback was missed
            if (!m_CityLoaded)
            {
                m_Phase = m_LoadAttempted ? "loading the save" : "main menu";
                if (!m_LoadAttempted && elapsed >= m_Request.MenuWaitSeconds && !IsLoading())
                {
                    m_LoadAttempted = true;
                    m_LoadNote = SaveLoader.TryLoad(m_Request.Save);
                    Mod.Log.Info("Save load attempt: " + m_LoadNote);
                }
                if (elapsed > m_Request.LoadTimeoutSeconds)
                {
                    m_Phase = "no city";
                    End("no_city");
                }
            }
            else
            {
                if (m_LoadedAt < 0)
                {
                    m_LoadedAt = now;
                    m_FrameAtLoad = FrameIndex();
                    m_Phase = "city running";
                    Mod.Log.Info("City loaded after " + (now - m_Start).ToString("0", CultureInfo.InvariantCulture) + " s");
                }
                if (!m_SpeedSet && now - m_LoadedAt > 5f)
                {
                    m_SpeedSet = true;
                    Mod.Log.Info("Simulation speed: " + SetSimulationSpeed(m_Request.SimSpeed));
                }
                if (now - m_LoadedAt >= m_Request.RunSeconds)
                {
                    m_FrameAtEnd = FrameIndex();
                    End("ok");
                }
            }
            if (!Finished && now - m_LastPartial > 10f)
            {
                m_LastPartial = now;
                WriteResult(final: false, status: null);
            }
        }

        private void End(string status)
        {
            Finished = true;
            WriteResult(final: true, status: status);
            try
            {
                File.Delete(Path.Combine(m_Dir, "request.json"));
            }
            catch (Exception)
            {
                // the agent removes it too
            }
            if (m_Request.Quit && !m_QuitRequested)
            {
                m_QuitRequested = true;
                Mod.Log.Info("Smoke test finished (" + status + "); quitting the game");
                Application.Quit();
            }
        }

        public void WriteResult(bool final, string status)
        {
            try
            {
                var sb = new StringBuilder(4096);
                sb.Append('{');
                Field(sb, "version", 1);
                Field(sb, "job", m_Request.Job);
                Field(sb, "final", final);
                Field(sb, "status", status ?? (final ? "ok" : "running"));
                Field(sb, "phase", m_Phase);
                Field(sb, "game_version", SafeGameVersion());
                Field(sb, "city_loaded", m_CityLoaded);
                Field(sb, "load_source", m_LoadSource);
                Field(sb, "load_note", m_LoadNote);
                Field(sb, "loaded_after_s", m_LoadedAt < 0 ? -1 : m_LoadedAt - m_Start);
                Field(sb, "frames_at_load", m_FrameAtLoad);
                Field(sb, "frames_at_end", m_FrameAtEnd);
                Field(sb, "simulated_frames", m_FrameAtEnd > m_FrameAtLoad ? m_FrameAtEnd - m_FrameAtLoad : 0);
                lock (m_Lock)
                {
                    Field(sb, "error_count", m_ErrorCount);
                    sb.Append("\"errors\":[");
                    for (int i = 0; i < m_Errors.Count; i++)
                    {
                        if (i > 0)
                            sb.Append(',');
                        sb.Append('{');
                        Field(sb, "type", m_Errors[i][0]);
                        Field(sb, "message", m_Errors[i][1]);
                        Field(sb, "stack", m_Errors[i][2]);
                        Str(sb, "time_s", m_Errors[i][3], last: true);
                        sb.Append('}');
                    }
                    sb.Append(']');
                }
                sb.Append('}');
                string name = final ? "result.json" : "result.partial.json";
                string path = Path.Combine(m_Dir, name);
                File.WriteAllText(path + ".tmp", sb.ToString());
                if (File.Exists(path))
                    File.Delete(path);
                File.Move(path + ".tmp", path);
            }
            catch (Exception e)
            {
                Mod.Log.Info("Could not write the result: " + e.Message);
            }
        }

        // ---------------------------------------------------------------- game state
        private static string SafeGameVersion()
        {
            try
            {
                return Game.Version.current.fullVersion;
            }
            catch (Exception)
            {
                return "";
            }
        }

        private static uint FrameIndex()
        {
            try
            {
                return World.DefaultGameObjectInjectionWorld?.GetExistingSystemManaged<SimulationSystem>()?.frameIndex ?? 0u;
            }
            catch (Exception)
            {
                return 0u;
            }
        }

        private static readonly PropertyInfo s_GameMode = typeof(GameManager).GetProperty("gameMode", BindingFlags.Public | BindingFlags.Instance);
        private static readonly PropertyInfo s_IsLoading = typeof(GameManager).GetProperty("isGameLoading", BindingFlags.Public | BindingFlags.Instance);

        private static bool GameModeIs(string name)
        {
            try
            {
                object gm = GameManager.instance;
                return gm != null && s_GameMode != null && s_GameMode.GetValue(gm)?.ToString() == name;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static bool IsLoading()
        {
            try
            {
                object gm = GameManager.instance;
                return gm != null && s_IsLoading != null && s_IsLoading.GetValue(gm) is bool b && b;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static string SetSimulationSpeed(float speed)
        {
            try
            {
                SimulationSystem sim = World.DefaultGameObjectInjectionWorld?.GetExistingSystemManaged<SimulationSystem>();
                if (sim == null)
                    return "no simulation system";
                PropertyInfo p = typeof(SimulationSystem).GetProperty("selectedSpeed", BindingFlags.Public | BindingFlags.Instance);
                if (p == null || !p.CanWrite)
                    return "selectedSpeed not available; left as it is";
                p.SetValue(sim, speed);
                return "set to " + speed.ToString(CultureInfo.InvariantCulture);
            }
            catch (Exception e)
            {
                return "failed: " + e.Message;
            }
        }

        // ---------------------------------------------------------------- JSON
        private static string Cut(string s, int max) => s == null ? "" : (s.Length <= max ? s : s.Substring(0, max));

        private static void Field(StringBuilder sb, string key, object value)
        {
            sb.Append('"').Append(key).Append("\":");
            switch (value)
            {
                case null:
                    sb.Append("null");
                    break;
                case bool b:
                    sb.Append(b ? "true" : "false");
                    break;
                case int i:
                    sb.Append(i.ToString(CultureInfo.InvariantCulture));
                    break;
                case uint u:
                    sb.Append(u.ToString(CultureInfo.InvariantCulture));
                    break;
                case float f:
                    sb.Append(f.ToString("0.###", CultureInfo.InvariantCulture));
                    break;
                default:
                    Escape(sb, value.ToString());
                    break;
            }
            sb.Append(',');
        }

        private static void Str(StringBuilder sb, string key, string value, bool last)
        {
            sb.Append('"').Append(key).Append("\":");
            Escape(sb, value);
            if (!last)
                sb.Append(',');
        }

        private static void Escape(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (char c in s ?? "")
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20)
                            sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else
                            sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }
    }

    /// <summary>
    /// Loads a save from the main menu when the game did not continue the last one by itself.
    /// Everything here is looked up at run time, so a game update that renames these members
    /// only turns this fallback off instead of breaking the harness build.
    /// </summary>
    internal static class SaveLoader
    {
        public static string TryLoad(string wanted)
        {
            try
            {
                Type metaType = typeof(GameManager).Assembly.GetType("Game.Assets.SaveGameMetadata");
                if (metaType == null)
                    return "SaveGameMetadata type not found";
                object global = typeof(AssetDatabase).GetProperty("global", BindingFlags.Public | BindingFlags.Static)?.GetValue(null)
                    ?? typeof(AssetDatabase).GetField("global", BindingFlags.Public | BindingFlags.Static)?.GetValue(null);
                if (global == null)
                    return "AssetDatabase.global not found";
                MethodInfo getAssets = global.GetType().GetMethods(BindingFlags.Public | BindingFlags.Instance)
                    .FirstOrDefault(m => m.Name == "GetAssets" && m.IsGenericMethodDefinition && m.GetParameters().Length == 1);
                if (getAssets == null)
                    return "AssetDatabase.GetAssets not found";
                MethodInfo typed = getAssets.MakeGenericMethod(metaType);
                Type filterType = typed.GetParameters()[0].ParameterType;
                object filter = filterType.IsValueType ? Activator.CreateInstance(filterType) : null;
                var assets = ((IEnumerable)typed.Invoke(global, new[] { filter })).Cast<object>().ToList();
                if (assets.Count == 0)
                    return "no saves found";
                object chosen = null;
                DateTime newest = DateTime.MinValue;
                foreach (object a in assets)
                {
                    string name = Member(a, "name")?.ToString() ?? a.ToString();
                    if (!string.IsNullOrEmpty(wanted) && name != null && name.IndexOf(wanted, StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        chosen = a;
                        break;
                    }
                    object target = Member(a, "target");
                    if (target != null && Member(target, "lastModified") is DateTime modified && modified > newest)
                    {
                        newest = modified;
                        chosen = a;
                    }
                }
                chosen ??= assets[0];
                MethodInfo load = typeof(GameManager).GetMethods(BindingFlags.Public | BindingFlags.Instance).FirstOrDefault(m =>
                {
                    ParameterInfo[] ps = m.GetParameters();
                    return m.Name == "Load" && ps.Length >= 3 && ps[0].ParameterType == typeof(GameMode)
                        && ps[1].ParameterType == typeof(Purpose) && ps[2].ParameterType.IsAssignableFrom(metaType);
                });
                if (load == null)
                    return "GameManager.Load(GameMode, Purpose, asset) not found";
                ParameterInfo[] parameters = load.GetParameters();
                var args = new object[parameters.Length];
                args[0] = Enum.Parse(typeof(GameMode), "Game");
                args[1] = Enum.Parse(typeof(Purpose), "LoadGame");
                args[2] = chosen;
                for (int i = 3; i < args.Length; i++)
                    args[i] = parameters[i].HasDefaultValue ? parameters[i].DefaultValue : (parameters[i].ParameterType.IsValueType ? Activator.CreateInstance(parameters[i].ParameterType) : null);
                load.Invoke(GameManager.instance, args);
                return "loading " + (Member(chosen, "name") ?? chosen);
            }
            catch (TargetInvocationException e)
            {
                return "load failed: " + (e.InnerException?.Message ?? e.Message);
            }
            catch (Exception e)
            {
                return "load failed: " + e.GetType().Name + ": " + e.Message;
            }
        }

        private static object Member(object o, string name)
        {
            if (o == null)
                return null;
            Type t = o.GetType();
            PropertyInfo p = t.GetProperty(name, BindingFlags.Public | BindingFlags.Instance);
            if (p != null && p.GetIndexParameters().Length == 0)
                return p.GetValue(o);
            FieldInfo f = t.GetField(name, BindingFlags.Public | BindingFlags.Instance);
            return f?.GetValue(o);
        }
    }
}
