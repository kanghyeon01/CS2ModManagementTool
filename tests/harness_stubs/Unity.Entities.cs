namespace Unity.Entities {
  public class World { public static World DefaultGameObjectInjectionWorld; public T GetExistingSystemManaged<T>() where T : class => null; public T GetOrCreateSystemManaged<T>() where T : class => null; }
  public abstract class ComponentSystemBase { protected virtual void OnCreate() {} public World World => null; }
  public abstract class SystemBase : ComponentSystemBase { protected abstract void OnUpdate(); }
}
