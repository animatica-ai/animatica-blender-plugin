# Sign in & setup

Open **Edit → Preferences → Add-ons → Animatica**. The page has three parts:
**Account**, **Server** and **Poser**. The version you are running, and any
update, is at the top (see [Updating](installation.md#updating)).

## Animatica Cloud (recommended)

This is the default. Generation runs on Animatica's servers, so you don't need
a powerful GPU or any model download for it.

1. Make sure **Self-hosted** is **off** (under **Server**)
2. Under **Account**, press **Sign in** and enter your Animatica email and password
3. The addon connects on its own and lists the available models

The **Server** box says *Connected* and how many models there are. If it
cannot reach the server it says why and offers **Try again**. Your session
renews itself; if it ends (for example, it expired), the panel says so and
asks you to sign in again. **Sign out** forgets the session on this machine.

No account yet? Sign up at [animatica.ai](https://animatica.ai), then come back
and sign in here.

## Your own server (advanced)

Only if you already run an MMCP motion server on your computer or local network:

1. Turn **Self-hosted** **on**
2. Enter your server address in **URL** (often `http://localhost:8000`)
3. It connects on its own — no Animatica sign-in needed

Your Animatica session is never sent to a self-hosted server. It only ever
goes to Animatica Cloud, over HTTPS. Switching **Self-hosted** on keeps you
signed in; the session just stays home.

A server on this machine (`localhost`) works even with Blender's online access
off. A server anywhere else, including your local network, needs it on.

Most artists can skip this and stay on Animatica Cloud.

## Posing by hand

Posing with handles, and editing a take by hand, is **Animatica Autoposer
Pro**, a separate add-on with its own preferences (its pose model, where it is
kept, and the thread count). This add-on downloads nothing for it.

## Privacy and network

Everything the addon does online goes through Blender's **Allow Online Access**
switch (**Edit → Preferences → System**). With it off, nothing leaves your
machine; the one exception is a self-hosted server on `localhost`, which is
not online.

**What goes out without you pressing anything**

- **api.animatica.ai** — the list of models, when the addon connects (on
  startup, after opening a file, after signing in). If you are signed in, your
  session goes with it.
- **api.github.com** — the update check, at most once a day. Turn it off with
  **Check for updates**.

**What goes out only when you ask**

- **GitHub** — example scenes and the ready-made character (from
  `animatica-ai/animatica-assets-public`, at a pinned commit, each file checked
  against its hash), and updates (from `animatica-ai/animatica-blender-plugin`).
  Example scenes open with scripts turned off, even if you have Auto Run on.
- **api.animatica.ai** — signing in, and each generation.

**What a generation sends**

- your prompts and prompt blocks, key poses, waypoints and pins
- your rig's bone names and rest offsets (the skeleton, without the mesh)
- the generation settings (seed, quality, guidance and so on)
- the Blender version, the addon version and a session id that is new each
  time the addon starts

It does not send file paths, scene names or object names. On Animatica Cloud
your session goes with it; to a self-hosted server it goes to that server
only, with no Animatica session.

Next: [Using Animatica](usage.md)
