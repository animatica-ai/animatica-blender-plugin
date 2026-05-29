# Using Proscenium

**Videos first?** [YouTube tutorial playlist](https://www.youtube.com/watch?v=Wc349qOwjfM&list=PLAJ2UfUYhFQKZpFS8eh1eGUWJ0PAys1n1)

## Before you generate

1. [Install](installation.md) and [sign in](configuration.md)
2. In the 3D View, open the **N** panel → **Proscenium** tab
3. In the **Proscenium** panel, click **Connect**, then pick a model

## Choose your character

**Already have an armature?**  
In the **Main** panel, set **Target armature** to your rig.

**Starting fresh?**  
Use **Import … skeleton** to add Animatica's reference rig, then animate that
armature. You can switch to your own character later once you're comfortable.

With **Animatica Cloud**, you can also generate onto many custom armatures — not
only the imported skeleton.

## Direct the motion

Use any combination that fits your shot:

| Tool | What it does |
|---|---|
| **Prompt blocks** | Text descriptions on the timeline (e.g. "walks forward sadly") |
| **Keyframes** | Pose your character on key frames so the AI knows the start and end |
| **Root path** | Draw a curve on the floor for where the character should go |
| **Effector pins** | Pin a hand or foot to an empty so it stays on an object |

The more direction you give, the closer the result tends to match your intent.

### Prompt blocks on the timeline

Prompt blocks live on Blender's **Timeline** as colored strips in the
**Proscenium** lane:

- **Double-click** an empty part of the lane to add a block
- **Double-click** a block to type its prompt (or right-click → **Edit Prompt**)
- **Drag** a block to move it; **drag its edges** to resize it
- **Drag** the top edge of the lane to make the strips taller
- **Right-click** a block to enable/disable, regenerate, or delete it
- **Delete** / **Backspace** removes the block under the cursor
- The **+ / −** buttons in the Timeline header add/remove blocks too

A block with **no prompt** is *unconditioned* (shown hatched) — the model
fills that span on its own. A *disabled* block is skipped entirely.

> Non-Latin text (e.g. CJK) can't be typed through the on-strip editor — use
> right-click → **Edit Prompt** for those.

## Generate a full clip

1. Set your frame range on the timeline
2. Click **Generate Motion**
3. Wait for the progress indicator to finish
4. Play the result in the viewport
5. **Accept** — keeps the new action on your armature  
   **Reject** — removes it and restores what you had before

You can **Reject** and tweak prompts or poses, then generate again.

## Single pose at one frame

Use **Generate Pose @ Frame N** when you only want one pose at the current
frame — handy for blocking or fixing a single key pose. It won't replace your
entire action the way **Generate Motion** does.

## Sidebar panels (quick reference)

- **Proscenium** (main) — connect, pick a model, target armature, generate, accept / reject
- **Constraints** — root paths and pinned effectors
- **Settings** — generation options for the current shot

## Help

- [Tutorial videos](https://www.youtube.com/watch?v=Wc349qOwjfM&list=PLAJ2UfUYhFQKZpFS8eh1eGUWJ0PAys1n1)
- [Discord community](https://discord.com/invite/A8CrURBewz)
- **Need help?** at the top of the Proscenium sidebar

See also: [Tips & limits](limitations.md)
