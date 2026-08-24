# Animatica Blender Plugin

[![License: GPL-3.0-or-later](https://img.shields.io/badge/License-GPL--3.0--or--later-blue.svg)](LICENSE)

AI motion generation inside **Blender 5+**. Describe what you want, block out
keyframes on the timeline, draw a path for the character to follow, pin a hand
to an object — then hit **Generate Motion** and get a new action on your
armature. Not happy? **Reject** and try again.

## What you can do

- **Full clips** — generate motion across a frame range from text prompts and key poses
- **Single poses** — **Generate Pose @ Frame** for one frame without replacing your whole action
- **Direct the performance** — prompt blocks on the timeline, floor paths, pinned hands/feet
- **Your character** — work on your own armature, or import our reference skeleton to start fast
- **Preview before committing** — review the result, then **Accept** or **Reject**

Generation runs in the cloud by default ([Animatica Cloud](https://animatica.ai)) —
no model to download into Blender, no local GPU required. Sign in once in
addon preferences and you're set. Power users can run a server on their own
machine instead; see [configuration](docs/configuration.md).

## Install

1. Download the latest **proscenium-blender-….zip** from
   [GitHub Releases](https://github.com/animatica-ai/proscenium-blender/releases)
2. In Blender: **Edit → Preferences → Add-ons → Install…** → choose the zip
3. Enable **Proscenium — AI Motion Generation**

You need **Blender 5.0+** and a free [Animatica](https://animatica.ai) account.

## Get started in Blender

1. **Edit → Preferences → Add-ons → Proscenium** — sign in with your Animatica account
2. Open the **N** panel in the 3D View (**Proscenium** tab) → **Connect** → choose a model
3. Pick your **target armature** (or **Import skeleton** if you're starting from ours)
4. Add prompts and constraints, then **Generate Motion**
5. **Accept** to keep the animation, or **Reject** to undo

New here? Watch the **[video tutorial playlist](https://www.youtube.com/watch?v=Wc349qOwjfM&list=PLAJ2UfUYhFQKZpFS8eh1eGUWJ0PAys1n1)**
on YouTube for a walkthrough in Blender.

Written guide: [docs/usage.md](docs/usage.md) · Sign-in and self-hosted: [docs/configuration.md](docs/configuration.md)

## Help

Stuck or want to share feedback?

- **[Tutorial videos](https://www.youtube.com/watch?v=Wc349qOwjfM&list=PLAJ2UfUYhFQKZpFS8eh1eGUWJ0PAys1n1)** on YouTube
- **[Animatica Discord](https://discord.com/invite/A8CrURBewz)** — or **Need help?** in the Proscenium sidebar

## Documentation

| | |
|---|---|
| [Tutorial videos](https://www.youtube.com/watch?v=Wc349qOwjfM&list=PLAJ2UfUYhFQKZpFS8eh1eGUWJ0PAys1n1) | YouTube walkthrough playlist |
| [Install](docs/installation.md) | Download and enable the addon |
| [Sign in & setup](docs/configuration.md) | Animatica Cloud or self-hosted |
| [Using Proscenium](docs/usage.md) | Full workflow in Blender |
| [Tips & limits](docs/limitations.md) | What to expect |
| [All guides](docs/README.md) | Documentation index |

Contributors: [docs/developing.md](docs/developing.md) · **License:** [GPL-3.0-or-later](LICENSE)
