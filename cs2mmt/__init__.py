"""CS2 Mod Management Tool.

Keeps Cities: Skylines II code mods working across game updates without anyone at the keyboard:

* the controller (an always-on NAS or small server) watches Steam for a new game build and the
  mods' forum threads and GitHub issues, queues jobs and wakes the main PC with Wake-on-LAN;
* the agent on the main PC switches a multi-boot machine from Linux to Windows, waits for Steam
  to install the new build, builds every registered mod, lets Claude Code fix what broke, runs an
  in-game smoke test, and publishes to GitHub and Paradox Mods once every gate has passed.

Only the Python standard library is used at run time, so the tool keeps working for years
without dependency updates.
"""

__version__ = "0.1.0"

USER_AGENT = "CS2ModManagementTool/" + __version__ + " (+https://github.com/kanghyeon01/CS2ModManagementTool)"
