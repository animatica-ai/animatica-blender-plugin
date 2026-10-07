# Animatica Choreographer for Blender

[![License: GPL-3.0-or-later](https://img.shields.io/badge/License-GPL--3.0--or--later-blue.svg)](LICENSE)

AI motion generation inside **Blender 5+**. Describe what you want, block out
key poses on the timeline, set waypoints for the character to walk through, pin
a hand to an object — then press **Generate Motion** and get a new take on your
armature. Not happy? **Reject** and try again.

## What you can do

- **Full clips** — generate motion across a frame range from text prompts and key poses
- **Prompt blocks on the timeline** — one prompt per stretch of the shot, each one regenerated on its own if you like
- **Key poses** — pose the character, press **Set Keyframe**, and the motion passes through that pose
- **See the plan** — ghosts of your key poses, and an onion skin of the motion around the playhead
- **Posing and editing by hand** (Animatica Marionette, sold separately) — drag a hand, foot or the hips and the whole body follows; drag the motion trail, spread the onion skin into a zoetrope, lock a foot in place
- **Waypoints and pins** — where the character stands at a frame, and a hand or foot held in place
- **Variations** — several versions of a take from one Generate; flip between them and keep one
- **Several characters at once** — select them and generate them together, each with its own row in the review
- **Loop** — a seamless walk, run or idle cycle, for games
- **In place** — take out the travel and keep the body's sway and bounce; with Marionette the root trajectory is editable
- **Follow Selection** — select a character to animate it
- **Example scenes** — open a finished set, ready to generate
- **Single poses** — **Generate Pose at Frame** for one frame without replacing your whole action
- **Review before committing** — every take waits for **Accept** or **Reject**
- **Built-in updates** — the addon tells you when a new version is out and installs it on one click

Generation runs in the cloud by default ([Animatica Cloud](https://animatica.ai)),
so you don't need a local GPU and there is no generation model to download.
Sign in once in the addon preferences and you're set. Power users can run a
server on their own machine instead; see [configuration](docs/configuration.md).

Nothing runs on your machine but the addon itself, and nothing is downloaded
until you ask (an example scene, the ready-made character). What the addon
sends, and where, is listed under
[Privacy and network](docs/configuration.md#privacy-and-network).

## Install

1. Download the latest **animatica-blender-….zip** from
   [GitHub Releases](https://github.com/animatica-ai/animatica-blender-plugin/releases)
2. In Blender: **Edit → Preferences → Add-ons → Install…** → choose the zip
3. Enable **Animatica Choreographer**
4. Make sure **Edit → Preferences → System → Allow Online Access** is on

You need **Blender 5.0+** and a free [Animatica](https://animatica.ai) account.
Later versions install from inside Blender; see [installation](docs/installation.md).

## Get started in Blender

1. **Edit → Preferences → Add-ons → Animatica** — sign in with your Animatica account
2. Open the **N** panel in the 3D View (**Animatica** tab) — it connects on its own → choose a model
3. Pick your **Armature**, or **Add Ready-Made Character** if you're starting from ours
   (or open an example scene from the icon in the panel's header)
4. Type a prompt, add key poses or waypoints if you like, then **Generate Motion**
5. **Accept** to keep the take, or **Reject** to go back to what you had

New here? Watch the **[video tutorial playlist](https://www.youtube.com/watch?v=Wc349qOwjfM&list=PLAJ2UfUYhFQKZpFS8eh1eGUWJ0PAys1n1)**
on YouTube for a walkthrough in Blender.

Written guide: [docs/usage.md](docs/usage.md) · Sign-in and self-hosted: [docs/configuration.md](docs/configuration.md)

## Help

Stuck or want to share feedback?

- **[Tutorial videos](https://www.youtube.com/watch?v=Wc349qOwjfM&list=PLAJ2UfUYhFQKZpFS8eh1eGUWJ0PAys1n1)** on YouTube
- **[Animatica Discord](https://discord.com/invite/A8CrURBewz)** — or the **?** in the Animatica panel's header

## Documentation

| | |
|---|---|
| [Tutorial videos](https://www.youtube.com/watch?v=Wc349qOwjfM&list=PLAJ2UfUYhFQKZpFS8eh1eGUWJ0PAys1n1) | YouTube walkthrough playlist |
| [Install](docs/installation.md) | Download, enable, and keep the addon up to date |
| [Sign in & setup](docs/configuration.md) | Animatica Cloud or self-hosted, privacy |
| [Using Animatica](docs/usage.md) | Full workflow in Blender |
| [Tips & limits](docs/limitations.md) | What to expect |
| [All guides](docs/README.md) | Documentation index |

Contributors: [docs/developing.md](docs/developing.md) · **License:** [GPL-3.0-or-later](LICENSE) · [Changelog](CHANGELOG.md)
