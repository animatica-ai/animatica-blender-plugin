# Tips & limits

Things that are useful to know up front — not bugs, just how Animatica works today.

## Your character vs. the ready-made character

If you use a **self-hosted** server on your own machine, it works best with
Animatica's **ready-made character** or a rig that matches it closely.

**Animatica Cloud** can generate onto many of your own armatures. If the rig is
very different from the model's skeleton, results may vary — try the
ready-made character first to learn the workflow.

## Body only

The model animates the body. It does not move the face, and it does not move
fingers: each hand gets one of three shapes laid over the take (**Relaxed**,
**Gripping** or **Straight**, in the **Pose** panel).

## Every frame is keyed

A take is written as a key on every frame for every bone. That is exact, but
it is dense to edit by hand in the Graph Editor. Sparse keys are not available
yet. Use **Set Keyframe** and **Jump to Key Pose** to work with your own poses
among them.

## Control rigs

Takes are keyed on the controls of **Rigify**, **Auto-Rig Pro**, **CloudRig**
and **Mixamo Control Rig** rigs, and of any rig connected with Marionette's
Rig Connection. Other control rigs get the motion on their deform bones, which
their constraints override, so it doesn't show: connect the rig in Marionette
first.

On Rigify, Auto-Rig Pro and CloudRig, arms and legs are keyed in FK over the
take. Rigify's IK is matched to it on every frame; Auto-Rig Pro's and
CloudRig's IK controls are left where they were.
Rigify's FK spine turns about the middle of its bones, so the spine lands a few
millimetres short of the take.

The model has no fingers. A take leaves a control rig's finger controls as they
are, and the **Hand Pose** setting only reaches fingers named the Mixamo way
(``LeftHandIndex1``).

On a Mixamo Control Rig, **In place** and **Loop**'s spot don't move the take
yet.

## Loop needs one block

**Loop** works on a single prompt block. With more than one, it greys out and
says so. It also appears only when the connected model supports it.

## Linked characters

A take on an action linked from another file, or on a library override, can't
be edited in place: **In place** and key poses leave it alone. Make the action
local first if you want to change it.

## Generation takes a moment

Blender stays responsive while the server works. On Animatica Cloud, the first
run after a long break can take a minute or more while GPUs wake up; later
runs are usually faster. Use **Cancel** if you need to stop.

## Online access

Everything except a self-hosted server on `localhost` needs Blender's **Allow
Online Access** (**Edit → Preferences → System**). See
[Privacy and network](configuration.md#privacy-and-network).
