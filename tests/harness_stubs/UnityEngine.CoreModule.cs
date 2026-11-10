namespace UnityEngine {
  public class Object { public static void DontDestroyOnLoad(Object target) {} }
  public class Component : Object { }
  public class Behaviour : Component { }
  public class MonoBehaviour : Behaviour { }
  public sealed class GameObject : Object { public GameObject(string name) {} public T AddComponent<T>() where T : Component => null; public static GameObject Find(string n) => null; }
  public enum LogType { Error = 0, Assert = 1, Warning = 2, Log = 3, Exception = 4 }
  public static class Time { public static float realtimeSinceStartup => 0f; }
  public static class Application {
    public delegate void LogCallback(string condition, string stackTrace, LogType type);
    public static event LogCallback logMessageReceivedThreaded { add {} remove {} }
    public static string persistentDataPath => "";
    public static void Quit() {}
  }
}
