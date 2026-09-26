# Using Animatica

**Videos first?** [YouTube tutorial playlist](https://www.youtube.com/watch?v=Wc349qOwjfM&list=PLAJ2UfUYhFQKZpFS8eh1eGUWJ0PAys1n1)

## Before you generate

1. [Install](installation.md) and [sign in](configuration.md)
2. In the 3D View, open the **N** panel → **Animatica** tab — it connects on
   its own

## Choose your character

**Starting fresh?**  
Click **Add Ready-Made Character** in the **Animatica** panel, then animate
that character. You can switch to your own later once you're comfortable.

**Already have an armature?**  
Pick it under **or animate your own rig**. The **Model** and **Armature** stay
at the top of the Animatica panel, so you can change either at any time.

With **Animatica Cloud**, you can also generate onto many custom armatures — not
only the imported skeleton.

## Direct the motion

Use any combination that fits your shot:

| Tool | What it does |
|---|---|
| **Prompt** | What the character does (e.g. "walks forward sadly"). The sidebar shows the selected block's prompt; more blocks go on the Timeline |
| **Key poses** | Pose your character and press **Set Keyframe** — the motion is made to pass through each pose |
| **Waypoints** | **Add Waypoint** at a frame, then drag the circle to where the character should stand then |
| **Pins** | **Add Pin** holds a hand or foot on an empty, so it stays on an object |

Key poses are made in the **Pose** panel (the **Autoposer** at its top lets
you drag hands, feet or hips and have the body follow); waypoints and pins are
in the **Constraints** panel below it. The status line under Generate Motion says
how many key poses the next take will hit. The more direction you give, the
closer the result tends to match your intent.

### Prompt blocks on the timeline

Prompt blocks live on Blender's **Timeline** as colored strips in the
**Animatica** lane:

- **Double-click** an empty part of the lane to add a block
- **Double-click** a block to type its prompt (or right-click → **Edit Prompt**)
- **Drag** a block to move it; **drag its edges** to resize it
- **Drag** the top edge of the lane to make the strips taller
- **Right-click** a block to enable/disable, regenerate, or delete it
- **Delete** / **Backspace** removes the block under the cursor
- The **+ / −** buttons in the Timeline header add/remove blocks too

A block with **no prompt** is shown hatched ("Double-click to describe the
motion") — the model fills that span on its own. A *disabled* block is skipped
entirely. The first block is also in the sidebar while it is empty, so your
first prompt can be typed there.

> Non-Latin text (e.g. CJK) can't be typed through the on-strip editor — use
> right-click → **Edit Prompt** for those.

## See the plan — ghosts

Your key poses *are* the plan: each one becomes a full-body constraint the
motion has to pass through. Open **Settings → Viewport** and tick its header checkbox
to see them.

In the viewport, each pose you keyed appears as a ghost where it sits in the
scene — tinted with the colour of the **prompt block** it falls under and
labelled with its **frame number**. On the timeline, a diamond marks each pose
in the **Animatica** lane, so you can see which block each one lands in.

Running through them is the **motion trail**: the path the animation actually
takes, frame by frame. It carries the same colours, so the curve changes colour
where the prompt blocks change — you can see which stretch of the motion belongs
to which instruction. The dots are one per frame, so their spacing is the timing
(bunched is slow, spread is fast); the larger diamonds are your key poses, and
the white one is the playhead. It traces the joints the model is steered by —
**hands, feet, root and head** — so the foot lines tell you about sliding and
footfalls, the hand lines about arcs, and the root line about the trajectory.

**Click a ghost to edit that pose.** The playhead goes to its frame, the rig
goes into pose mode, and — where the Autoposer is driving that rig — the pose
is handed to it so you can push the body around with its controls. When you
are happy, **Set Keyframe** (in the **Pose** panel, under the handles)
writes the pose onto that frame's keyframe, so the pose the next generation is
asked to hit is the one you just made. Without the Autoposer the click still
takes you there, and Set Keyframe still keys what you posed by hand.

**Drag a point on a motion trail** and that end effector moves at *that*
frame — the playhead stays where it is. The other traced joints stay pinned
where they were, the Autoposer solves the body around the one you moved, and a
yellow skeleton shows the pose you are about to commit. Let go and it is keyed
there, as a pose of yours: one more full-body constraint for the next
generation. It solves at about 100 Hz, so the body follows the cursor.

That holds for the hips as much as for a hand: dragging the root curve shifts
the pelvis while the feet and hands stay where they are, which is a weight
shift. **Hold Shift to move the whole pose instead** — every joint travels
together, so the character is carried to a new place with its shape intact.
Shift can be taken up or let go mid-drag; the header names it while you are
dragging.

A control always wins the click: the controls sit on their joints and the
trail runs through those same joints, so the two overlap by construction.
Clicking a control selects it, as it would anywhere in Blender.

Your key poses are the big diamonds on the curve, and they are what a click
reaches for: a key pose wins over the frames either side of it even when they
are a pixel apart. To bend the curve between keys instead, click exactly on
the small dot you want. The label by the cursor names the frame you grabbed
while you drag, so a mis-grab is one **Esc** away.

A pose **outside the generating range is not sent** at all — those are greyed
out in the viewport, red on the timeline, and named in the panel. Widen a prompt
block to bring one back into the plan, or move the pose.

The Autoposer works on the character you picked for generation — there is one
armature in the app, chosen once. The first time you open a pose for editing it
gives that rig its control bones, so there is nothing to build by hand; the
Pose panel's **Add Pose Handles** button is only there for a rig that has none
yet.

The controls follow the playhead, so you can start posing at any frame: scrub
to where you want a pose and grab one. Posing with them keys the pose where you
made it, so it survives frame changes on its own — there is no mode to enter or leave. (A rig left detached
by an older session can still be handed back from the Pose panel.)

> **Use Set Keyframe rather than `I` on top of a generated take.** Blender
> keeps a keyframe's existing type when you key over one, so pressing `I` on a
> frame a previous generation baked leaves a key the addon reads as the
> model's own output — it gets no ghost and is left out of the next request.
> **Set Keyframe** marks the pose as yours.

The posed bodies and the trail are independent — show either on its own
(**Settings → Viewport**):

| Setting | What it does |
|---|---|
| **Ghosts** | Draw the body at each pose you keyed |
| **Trail** | Trace the path the motion takes, through the hands, feet, root and head |
| **Frame Numbers** | Label each pose with the frame it sits on |
| **X-Ray** | Draw poses through the character instead of behind it |
| **Ghost Style** | *Auto* uses the skinned character if the rig has one, the skeleton otherwise. Force either with *Mesh* / *Bones* |
| **Auto Refresh** | Re-read the plan when you key a pose or move the rig. Turn off on a heavy character and use **Refresh Ghosts** |

Refreshing re-reads the poses by stepping the playhead, so it costs a short
pause — about a tenth of a second on a normal character — and it happens even
while the animation is playing: playback picks up exactly where it was. Only a
running generation makes a refresh wait, since it owns the playhead itself;
until it finishes you keep seeing the last one, and the panel says so.

## Generate a full clip

1. Type the prompt (the block's length on the Timeline is the clip's length),
   and direct it if you like
2. Click **Generate Motion** — usually under 30 seconds; the first run after
   a while can take a minute
3. Play the result in the viewport
4. **Accept** — keeps the take: it moves to the NLA track *Animatica: Motion*
   (replacing a take kept before), and your own keys stay  
   **Reject** — throws the take away and goes back to what you had; takes
   you kept earlier stay on the NLA

Not happy? **Generate Again** — every run is a new take. The take box shows
the seed a take used; **Lock** it to keep getting that one. While a kept take
exists, Accept reads **Replace Kept**.

## Loops for games

Tick **Loop** under Generate Motion and the model makes the block a seamless
cycle — a walk, a run, an idle. **In place** ticks with it, so the cycle plays
on the spot. Loop needs a single prompt block, and cycles work best at 2–4
seconds: the block's length is the loop's length. It appears only when the
connected model supports it.

## Single pose at one frame

Use **Generate Pose at Frame N** (in the **Pose** panel) when you only want one
pose at the current frame — handy for blocking or fixing a single key pose. It
won't replace your entire action the way **Generate Motion** does.

## Sidebar panels (quick reference)

- **Animatica** — model and armature, the prompt, generate, Loop and In
  place, accept / reject; the header icons open an example scene and help
- **Pose** — the **Autoposer** (drag hands, feet or hips; the body follows),
  Generate Pose, Set Keyframe, and **Fingers**: each hand's shape, since the
  model does not move fingers (Relaxed, Gripping, Straight)
- **Constraints** — waypoints (where to stand when) and pins (a hand or foot held in place)
- **Settings** — seed, quality, motion cleanup
  - **Viewport** — what the viewport draws of your key poses and the trail, how ghosts are drawn
  - **Posing** — the solid floor, Rest Pose
  - **Advanced** — guidance, blending between blocks

## Help

- [Tutorial videos](https://www.youtube.com/watch?v=Wc349qOwjfM&list=PLAJ2UfUYhFQKZpFS8eh1eGUWJ0PAys1n1)
- [Discord community](https://discord.com/invite/A8CrURBewz)
- The **?** in the Animatica panel's header

See also: [Tips & limits](limitations.md)
