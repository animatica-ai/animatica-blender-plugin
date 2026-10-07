# Installation

## What you need

- **Blender 5.0** or newer
- A free account at [animatica.ai](https://animatica.ai) (for Animatica Cloud)
- **Allow Online Access** turned on in Blender (see below)

## Install the addon

1. Download the latest **animatica-blender-….zip** from
   [GitHub Releases](https://github.com/animatica-ai/animatica-blender-plugin/releases)
2. Open Blender → **Edit → Preferences → Add-ons**
3. Click **Install…** and choose the zip file
4. Search for **Animatica** and enable **Animatica Choreographer**

The Animatica tab appears in the **N** panel of the 3D View once the addon is on.

## Allow Online Access

Blender has one switch for whether add-ons may use the network:
**Edit → Preferences → System → Allow Online Access**. Animatica respects it.
It must be on for:

- Animatica Cloud: listing models, generating, signing in
- update checks and updates
- example scenes and the ready-made character (the first time; they are cached after that)

With it off, the only thing that works is a self-hosted server on this
machine (`localhost`). The panel says *Online access is disabled in
Preferences > System* and offers **Open Preferences** to take you there.

## Updating

The addon keeps itself current. The version you are running is at the top of
its preferences, with a button to check now.

- **It checks by itself.** At most once a day it asks GitHub whether a newer
  release is out. If there is one, the Animatica panel and the preferences
  say so. Untick **Check for updates** to stop it.
- **It installs only when you ask.** Press **Update**. A dialog shows the
  version and its size, and your scene is untouched: only the addon is
  replaced. Restart Blender afterwards to finish.
- **Releases only, unless you ask for previews.** Tick **Include previews** to
  be offered pre-release builds too. It is on by default only if you
  installed a preview build.
- **A bad download is refused.** Anything that is not a complete, working
  build is rejected before your installed version is touched. If the new
  version will not load, the old one is put back and enabled again.
- **It won't cut a generation short.** It will not start while a generation
  is running, and if one starts during the download it waits for it to finish.
- **It will not update a development checkout.** If the addon is linked in
  from a source folder (for example with `make install`), it says so. Update
  that with git instead.

You can always install a zip by hand as above, too.

Next: [Sign in & setup](configuration.md) · [Video tutorials](https://www.youtube.com/watch?v=Wc349qOwjfM&list=PLAJ2UfUYhFQKZpFS8eh1eGUWJ0PAys1n1)
