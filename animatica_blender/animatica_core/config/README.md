# config/

Bone maps, characterization profiles, and per-skeleton settings live
here. Currently empty -- entries will land alongside the implementation
of the skeleton registry in `animatica_core/skeleton.py`.

Expected layout once populated:

- `soma77.bones.json` -- canonical Animatica hierarchy + rest pose
- `<skeleton_name>.bones.json` -- one file per registered skeleton
- `mobu_characterization.xml` -- HIK / Characterization template (MotionBuilder)
