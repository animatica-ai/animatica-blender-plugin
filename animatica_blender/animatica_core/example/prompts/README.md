# Example prompt files

Portable Animatica prompt exports (schema v2 — matches
`animatica_core/gui/timeline/prompt_store_json.py`). Host-neutral: these are
text scenarios, not scene files, so every DCC plugin that vendors core ships
them. Import any of these through the timeline UI's load action.

## Files

- `single_prompt.json` — minimal one-segment example.
- `multiple_prompts.json` — three chained segments (walk, turn, wave).
- `scenario_military_patrol.json` — advance, take cover, aim.
- `scenario_adventure_climb.json` — climb a ledge, look around, jump a gap.
- `scenario_fist_fight.json` — boxing stance, punch, dodge.
- `scenario_stealth_escape.json` — sneak, peek, sprint away.
- `scenario_tavern.json` — approach a bar, drink, walk away.
- `scenario_sword_fight.json` — guard, overhead strike, step back to block.
- `scenario_parkour.json` — sprint and vault, land and roll, keep running.
- `scenario_hand_combat.json` — jab, cross, block.
- `scenario_edge_balance.json` — balance along a narrow edge, wobble, step off.
- `scenario_drunk.json` — stumble, stagger, lean on a wall.

### Character-style examples

Same style applied across a short self-contained sequence:

- `style_big_guy.json` — large, heavy, lumbering gait.
- `style_slim_guy.json` — slim, light, springy step.
- `style_woman.json` — relaxed walk and a wave.
- `style_old_person.json` — slow hunched walk, sits down.
- `style_monster.json` — person moving monstrously. NOTE: "monster" is outside
  Kimodo's trained human categories, so results are the least reliable here; it is
  phrased as a *person moving like a monster* to stay as close to human data as possible.

## Format

```json
{
  "version": 2,
  "segments":   [{"text": "...", "start_frame": 0, "end_frame": 48, "color_idx": 0}],
  "constraints": []
}
```

Constraint `type` / `value` shapes: `root2d` → `{"xz":[x,z]}`; `left-foot` /
`right-foot` / `left-hand` / `right-hand` → `{"position":[x,y,z]}`; `fullbody` →
`{"joint_rotations":{name:[qx,qy,qz,qw]}, "root_position":[x,y,z]}`. All positions
are meters, Y-up.

Every example here now ships with an **empty `constraints` array** — the prompts
alone drive generation. Add pins yourself in the timeline UI when you need them;
the shapes above are the reference for when you do.

## Prompt best practices (Kimodo)

Applied to every scenario above. Source:
research.nvidia.com/labs/sil/projects/kimodo/docs/key_concepts/limitations.html

- Start each prompt with "A person…" (stylizable: "A tired person…", "A scared person…").
- One or at most two behaviors per prompt; split longer action across segments.
- Medium level of detail — not "A person walks" (too vague), not a per-limb script (too much).
- Stay inside trained categories: locomotion, gestures, everyday activities, common
  object interactions, videogame combat, dancing, and styles (tired, angry, happy, sad,
  scared, drunk, injured, stealthy, old, childlike). Actions outside these (e.g. sport-specific
  swings) give poor results.
- Make each prompt self-contained. A follow-on should restate context
  ("A crouching soldier raises a rifle…"), not rely on "then he…".
- Keep segments under 10 s each (≤ 240 frames at 24 fps).
- Keep constraints sparse — under 20 constrained frames per constraint type
  (the root path is the exception and can be dense).
- Multi-prompt transitions happen at the *start* of the next segment, so give each
  follow-on segment room to blend before its new action.
