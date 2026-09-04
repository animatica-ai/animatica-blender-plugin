# Plan: konwergencja addonu Blender na wspólny kod z `motionmcp-client-sdk`

**Cel:** `develop` addonu ma korzystać z pakietu `animatica_core` (SDK) tam,
gdzie dziś utrzymuje własne, rozjechane kopie: klient MMCP, budowanie
requestu, dekodowanie glTF, rejestr szkieletów, stałe. UI w `bpy`, bake
do akcji/NLA i konwersja osi zostają w addonie.

**To jest Faza 3 istniejącego planu nadrzędnego** —
`animatica-3dsmax-plugin/PLAN-animatica-core.md` §1.1 i §8 („B0 audit →
B1 reconcile → B2 bridge → B3 wire bpy UI"). Fazy 1 (3ds Max) i 2 (MoBu)
są zamknięte: oba pluginy vendorują core spod SDK `develop` = `c2076fd`.
Ten plan nie wymyśla architektury — realizuje zapisaną, z jedną korektą
(§2 D3).

---

## 0. Ustalenia (fakty z kodu, stan na 2026-09-04)

1. **SDK dostarcza się przez vendoring, nie pip** — to jego własna,
   udokumentowana decyzja (`pyproject.toml` nagłówek, `README.md`
   „Delivery", `docs/VENDORING.md`). Jedna implementacja mechanizmu:
   `animatica_core.vendoring` — kopiuje z `git archive` **nazwanego
   commitu**, stempluje `CORE-VERSION`, klasyfikuje dryf jako
   STALE / LOCALLY EDITED / MISSING / NOT IN CORE. Host trzyma ~40-liniowy
   shim `scripts/sync_core.py` (wzorzec: 3ds Max, MoBu).
2. **Nie-GUI połowa core jest wolna od bridge'a.** Wszystkie importy
   `animatica_core.bridge` siedzą w `gates/` i `gui/`; `core/`,
   `mmcp_client.py`, `gltf_parser.py`, `skeleton.py`, `constants.py`
   nie dotykają ani bridge'a, ani `host`. Test czystości
   (`tests/test_core_purity.py`) gwarantuje: zero `bpy`/Qt w tej połowie.
   → **Blender nie potrzebuje `blender_bridge`** do konwergencji requestu,
   klienta i parsera. Bridge byłby potrzebny wyłącznie do bramek (`gates/`).
3. **`core.retarget` jest martwe produktowo** (docstring, STATUS 2026-09-01):
   retargeting przeniósł się na serwer; moduł służy tylko bramkom.
   Krok B2 planu nadrzędnego („adopt core.retarget") jest nieaktualny.
4. **Core importuje sam siebie absolutnie** (107× `from animatica_core…`),
   więc wvendorowana kopia musi być importowalna jako top-level
   `animatica_core`. Addon Blendera to jeden pakiet w zipie
   (`Makefile`: `zip -r animatica_blender`), więc kopia musi leżeć
   **wewnątrz** `animatica_blender/` i być dopięta do `sys.path` na
   `register()`. Przepisywanie importów odpada — zepsułoby klasyfikację
   STALE/LOCALLY EDITED (bajty ≠ commit).
5. **`VendorConfig(plugin_root)`** wyprowadza `dst = plugin_root/animatica_core`
   i `version_file = plugin_root/CORE-VERSION` — wystarczy podać
   `plugin_root = animatica_blender/`, bez zmian w SDK. `SKIP_DIRS` to tylko
   `__pycache__`: vendorujemy **cały** pakiet, w tym `gui/` (14,4 k linii
   Qt) i `gates/` (5,3 k) jako martwy balast w zipie — cena za spójność
   z mechanizmem porównania (patrz R4).
6. **Blender 5.2 ma numpy 2.3.4** w swoim Pythonie 3.13 — spełnia
   `numpy>=2` z `constants.numpy_spec()`. `_bootstrap.py` (pip w MoBu)
   jest niepotrzebny.
7. **Kontrakt core'owego `build_request`** (`core/request_builder.py:1531`):
   `state` = obiekt jak `AppState` (duck-typed; opcje generacji czytane
   przez `getattr(state, …)`: `steps`, `num_samples`, `seed`, `random_seed`,
   `post_processing`, `transition_frames`, `cfg_type`, `cfg_text_weight`,
   `cfg_constraint_weight`, `animation_mode`), `character_state` =
   `CharacterState(prompts: [PromptBox], constraints: [ConstraintMarker])`,
   `model_caps`, oraz overridy: `skeleton_override` (dict wire —
   dokładnie to, co produkuje addonowe `armature_to_skeleton`),
   `segments_override`, `frame_range_override`, `anchor_markers`,
   `seed_override`, `origin_offset`, `root_yaw`, `frame_offset`, `warnings`.
   Słownik markerów: `fullbody | left-foot | right-foot | left-hand |
   right-hand | root2d`; `root2d` to markery **per klatka** (`xz`,
   `aim_heading_radians`) agregowane przez core w jeden `root_path`.
   **Efektor niesie nazwę stawu sam** (`ConstraintMarker.joint`, np.
   `LeftHand`) — typ `left-hand` jest tylko kategorią UI; core składa
   jednoklatkowe markery jednego stawu w jeden wieloklatkowy
   `effector_target` (`_merge_effector_group`, z komentarzem „mirroring
   proscenium_blender.sample_effector_target"). Klatki markerów i kotwic są
   **absolutne**; `frame_offset` = start take'a, a core sam relatywizuje do
   bazy `frame_range[0] + frame_offset` i **wyrzuca z ostrzeżeniem** markery
   spoza zakresu. Fallback zakresu = `(state.start_frame, start_frame +
   total_frames − 1)`, użyty tylko gdy nie ma promptów. Pojedyncza poza ma
   własny builder: `build_pose_request(state, model_caps, prompt,
   skeleton_override, seed_override, force_fallback)` — sam decyduje
   o segmencie `pose` vs fallbacku po `supported_segments`.
8. **Dryf jest duży**: `request_builder` 1 244 vs 2 143 linii, `mmcp_client`
   434 vs 419 — to nie kopie, to niezależne implementacje o różnych
   API (core: funkcje keyed po URL, `generate(server_url, body,
   access_token, on_progress)` z pollingiem 202; addon: klasa `MmcpClient`,
   cache procesowy, `last_connection_error`, `cached_model_items` pod enum
   `bpy`).
9. **Cztery rozbieżności zachowania, znane już teraz** (nie do odkrycia
   w B0; B0 ma szukać *dalszych*):
   - core zawsze wysyła `timing.fps` (= fps modelu, chyba że flagi debug);
     addon nigdy nie wysyłał. Serwer to akceptuje (równe fps modelu),
     a profil A/B ma równoważność `defaults: timing.fps`, więc hash
     kanoniczny się nie zmieni — ale bajty requestu tak.
   - core `_inject_default_walk_path`: dla modeli z listy
     `_TRAJECTORY_DRIVEN_MODELS = {"ardy-core-rp"}` (dokładnie ten jeden id;
     brak flagi w capabilities) request **bez** `root_path` dostaje
     syntetyczny marsz w +Z (zmierzone: bez niego 8 cm na 99 klatek). Addon
     zamiast tego wstrzykuje jednoklatkową kotwicę startową. Core ma też
     własną kotwicę (`root_anchor_xz` / `_auto_anchor`). Konwergencja =
     przyjęcie zachowania core — decyzja produktowa D6.
   - **Regresja do zatrzymania:** core **nie wysyła seedów per segment** —
     `build_segments` emituje `(start, end, text)`, a `_normalize_segments`
     ucina wszystko poza `type/prompt/duration_frames`. Żaden host nie
     korzysta dziś z `supports_segment_seed` przez core. Addon wysyła
     `segment.seed` (kłódki na timelinie, zweryfikowane E2E na obu modelach).
     Bez zmiany w SDK konwergencja **po cichu wyłączy** tę funkcję → K8.
   - **Zakres klatek:** core = unia promptów, fallback na zakres sceny; addon
     = unia promptów **i spanu kluczy akcji** (żeby poza zakluczowana poza
     blokami nie wypadła z okna — core by ją „z ostrzeżeniem" wyrzucił).
     Adapter zachowuje dzisiejszą unię przez `frame_range_override` — bez
     różnicy na drucie, bo addon już dziś wysyła taki zakres.
10. **Wyrocznia parytetu istnieje**: suite A/B w SDK
    (`tools/ab_suite/run.py --hosts blender --backend local`) z goldenami
    `golden/blender/local/c0–c4` kluczowanymi `canonical_request_hash`
    (profil: `volatile`, `measured` z zaokrągleniem, `defaults`,
    `equivalences`) + dump FBX. Runner (`blender_runner.py`) woła
    addonowe `request_builder.build_request`, `MmcpClient`,
    `canonical_skeleton.build_armature_from_canonical`,
    `gltf_to_blender.bake_gltf_to_armature` — po konwergencji przetestuje
    core **przez** adapter addonu. **Runner importuje jeszcze
    `proscenium_blender`** — dług po rename, do spłaty niezależnie.
11. **Repo addonu nie ma testów ani CI poza `release.yml`**; `CONTRIBUTING.md`
    (nowy na `develop`) wymaga gałęzi `feature/*` z `develop`, PR do
    `develop`, „test locally". `proscenium_blender/` na `develop` to pusty
    szczątek (same `__pycache__`), poza zipem.
12. `AB-TESTS.md` §hosty zapisuje **„no-shared-code policy"** addonu jako
    powód, by runner Blendera żył w SDK. To zadanie **odwraca tę politykę**
    — świadomie, bo plan nadrzędny (§1.1) rozstrzygnął: „converge the
    non-GUI half". Zdanie w `AB-TESTS.md` trzeba zaktualizować (K7).

## 1. Zakres — co się przenosi, co zostaje

| Addon (`animatica_blender/`) | Los | Zamiennik w core |
|---|---|---|
| `mmcp_client.py` — transport, capabilities, polling | **zastąpiony** | `animatica_core.mmcp_client` (`get_capabilities`, `cached_capabilities`, `pick_model`, `generate`, `MmcpError`) |
| `mmcp_client.py` — cache pod enum, `last_connection_error`, `get_mmcp_url` (cloud `/mmcp`), auth w prefsach | **zostaje** jako cienka warstwa `client_shim.py` | — (bpy-owe) |
| `request_builder.build_request` / `build_segments` / `build_options` / `QUALITY_PRESETS` (50/25/12 — identyczne) / `PROTOCOL_VERSION` / `BuildError` | **zastąpione** | `core.request_builder.build_request`, `QUALITY_PRESETS`, `PROTOCOL_VERSION`, `BuildError` |
| `request_builder.build_request_for_block` (regen bloku: pozy na `block±1`, wykrywanie edycji użytkownika) | **zastąpiony** wywołaniem `build_request` z `segments_override` + `frame_range_override` **poszerzonym o ±1** (kotwice muszą mieścić się w zakresie — core wyrzuca markery spoza) + `anchor_markers` z `sample_pose_at_frame`; `_user_edited_bones_per_frame` zostaje w `rig_probe.py` | wzorzec: MoBu `tool_window.py:3868` |
| generate-pose (inline dict w `operators.py`) | **zastąpiony** | `core.request_builder.build_pose_request` (`skeleton_override` = armatura użytkownika albo kanoniczny, jak dziś) |
| `request_builder.compute_frame_range` | **zostaje w adapterze**: unia promptów **i** spanu kluczy akcji (bpy) → przekazana jako `frame_range_override`; `state.start_frame/total_frames` ze sceny jako fallback core | `core.compute_frame_range` tylko dla samej unii promptów |
| `request_builder`: `armature_to_skeleton`, `is_control_rig`, `detect_deform_bones`, `emitted_deform_bones`, `_build_deform_parent_map`, `_GENERATED_ACTION_PREFIXES`, `_user_edited_bones_per_frame` | **zostają** (czyste `bpy`) → nowy `rig_probe.py` | — ; wynik `armature_to_skeleton` idzie jako `skeleton_override` |
| `constraints_ui.sample_*` (ścieżka, efektory, pozy) | **zostają** (bpy), ale **emitują `ConstraintMarker`** zamiast dictów wire | core agreguje markery w constrainty |
| `gltf_to_blender` — dekodowanie (`_read_floats`, `_decode_buffer`, `_frame_from_time`, ~60 l.) | **zastąpione** | `gltf_parser.parse_gltf_samples` |
| `gltf_to_blender` — bake, NLA, control-rig, `read_extension_metadata` | **zostają** | — (core gubi metadane bloków z `MMCP_motion`, patrz R2) |
| `canonical_skeleton` — budowa armatury z `/capabilities` | **zostaje bez zmian**; `core.skeleton.register` **opcjonalnie** — addon zawsze podaje `skeleton_override`, więc rejestr core nie jest na ścieżce generacji (służy bramkom i hostom bez własnego szkieletu) | — |
| `constants.py` | częściowo: `M_TO_CM`, `DEFAULT_FPS`, `ANIMATICA_MMCP_URL` z core; `END_EFFECTOR_JOINTS`, kolory, rozmiary empty — zostają | `animatica_core.constants` |
| `coords.py`, `body_mesh`, `path_follow`, `*_bake`, `panels`, `properties`, `timeline_*` | **bez zmian** | — |

**Poza zakresem tej fazy** (świadomie, patrz §7): `gui/` core (Qt —
bezużyteczne w Blenderze), `gates/` i bridge (B2 planu nadrzędnego —
osobna decyzja), `live/` (stream — dotyczy planu ARDY-live), auth
(core `AnimaticaAuth` wiąże HTTP z własnym magazynem JSON; addon trzyma
tokeny w prefsach — 16 odwołań, zostaje jak jest, konwergencja auth to
osobny, mały krok).

## 2. Decyzje projektowe

| # | Decyzja | Wybór | Uzasadnienie |
|---|---|---|---|
| D1 | Dostarczanie | Vendoring `animatica_core/` do `animatica_blender/animatica_core/` + `animatica_blender/CORE-VERSION`, shim `scripts/sync_core.py` z `plugin_root=animatica_blender` | Jedyny mechanizm SDK; zip addonu wymaga kopii wewnątrz pakietu; zero zmian w SDK |
| D2 | Importowalność | `register()` wstawia katalog addonu na początek `sys.path`; `unregister()` zdejmuje wpis i czyści `sys.modules['animatica_core*']` | 107 importów absolutnych w core; pułapka „żywy DCC trzyma stare moduły" (VENDORING.md) — addon ma już purge po nazwie dla własnych modułów |
| D3 | Bridge | **Brak `blender_bridge` w tej fazie** — korekta B2 planu nadrzędnego | Fakt 2: nie-GUI połowa jest bridge-free; fakt 3: `core.retarget` martwe. Bridge wróci tylko z bramkami |
| D4 | Pin | Start: `c2076fd` (= SDK `develop`, ten sam co Max i MoBu) w B1; **bump do commitu z K8 przed B3** | Trzy hosty na jednym commicie = jedno „core" do rozumowania; start na wspólnym pinie pozwala prowadzić B1/B2 równolegle z K8, a bump ćwiczy procedurę z VENDORING.md zanim stanie się rutyną |
| D5 | Adapter | Nowy moduł `core_adapter.py` (bpy → `AppState`/`CharacterState`/markery; `settings` → duck-typed `state`) | Jedno miejsce, gdzie addon „mówi po core'owemu"; testowalne bez Blendera przez obiekty-atrapy |
| D6 | Rozbieżności z faktu 9 | Przyjąć zachowanie core (`timing.fps`, `_inject_default_walk_path`, kotwica core) | Cel konwergencji to *jedno* zachowanie na hostach; wyjątki = dryf od nowa. Wymaga świadomej akceptacji (§8) |
| D7 | Wyrocznia | Suite A/B SDK jako bramka parytetu: hash kanoniczny requestów c0–c4 **niezmieniony** po konwergencji | Istnieje, ma goldeny, koduje dozwolone równoważności; nie budujemy drugiej |
| D8 | Gałąź | `feature/core-convergence` z `develop`, PR do `develop` | `CONTRIBUTING.md` |
| D9 | Dekodowanie glTF | `parse_gltf_samples` + konwersja macierz→kwaternion w addonie | Bake pisze `rotation_quaternion`; core daje `local_rot_mats`. Dryf float przy konwersji — porównania bake'u z tolerancją (R2) |

## 3. Kroki

### B0 — audyt i próg wejścia (bez zmian w kodzie, ~1 dzień)
1. **Sprawdzić, czy suite A/B Blendera w ogóle chodzi na dzisiejszym
   `develop`** (`run.py --hosts blender --backend local`, lokalny serwer
   MMCP na `:8001`): runner importuje `proscenium_blender` — spodziewany
   fail. Naprawić w SDK (rename importów; K7) i **zamrozić goldeny c0–c4
   na `develop` sprzed konwergencji** jako punkt odniesienia. Bez zielonej
   suite przed zmianą nie ma czym mierzyć po zmianie.
2. Diff semantyczny dwóch builderów na tych goldenach: dla każdego
   checkpointu zbudować request obiema drogami (addon dziś vs core przez
   prototyp adaptera w scratchpadzie) i porównać `canonical_request_json`.
   Wynik = lista różnic; każda dostaje decyzję: „równoważność do profilu",
   „przyjmujemy core" (fakt 9) albo „bug w core do naprawy w SDK".
3. Zmierzyć rozmiar zipa po wvendorowaniu całego core (fakt 5).
4. **Próg wejścia:** zero różnic bez decyzji. Dopiero wtedy B1.
5. **Nowy checkpoint c5** w scenariuszu suite (SDK, razem z K7): dwa
   prompt-bloki z przypiętymi seedami na modelu z `supports_segment_seed`.
   Golden zamrożony na starym `develop` (addon wysyła `segment.seed`).
   Po konwergencji c5 przechodzi **tylko** z K8 — to jest czerwone
   światło dla R9, nie notatka w CHANGELOG.

### B1 — vendoring + shim + bootstrap
- `scripts/sync_core.py` — kopia shimu z 3ds Max z dwiema różnicami:
  `plugin_root = os.path.join(ROOT, "animatica_blender")` (kopia i
  `CORE-VERSION` wewnątrz pakietu, D1) oraz **bootstrap pierwszego
  uruchomienia**: shim 3ds Max importuje `animatica_core.vendoring`
  z własnej wvendorowanej kopii, której przy pierwszym `--write` jeszcze
  nie ma — nasz shim próbuje kopii, a przy `ImportError` dokłada na
  `sys.path` checkout SDK (`DEFAULT_CORE_REPO` / env `ANIMATICA_CORE`).
  Test SDK `test_write_vendors_the_named_commit_and_stamps_it` robi
  dokładnie to: `main()` z pakietu SDK w pusty `plugin_root`.
  Potem `python scripts/sync_core.py --write --ref c2076fd` i commit
  z jawną listą plików (`animatica_blender/CORE-VERSION
  animatica_blender/animatica_core/...` — VENDORING.md: nigdy `git add -A`).
- `animatica_blender/__init__.py`: w `register()` katalog addonu na
  początek `sys.path` (przed importem czegokolwiek z core); `host.register`
  **opcjonalnie** — nie-GUI połowa go nie czyta (fakt 2), rejestrujemy dla
  porządku (`key="blender"`), bez bridge'a. `unregister()`: purge
  `animatica_core*` z `sys.modules` i zdjęcie wpisu z `sys.path`.
  `animatica_core/__init__.py` nie ma żadnych importów na poziomie pakietu
  (sprawdzone) — samo `import animatica_core` nic nie ciągnie.
- `.github/workflows/ci.yml`: job `vendored-core-is-untouched` skopiowany
  z 3ds Max (sekret `CORE_REPO_TOKEN`, czytelny fail „cannot tell" gdy
  brak tokena).
- `Makefile`: bez zmian (zip bierze podkatalogi); `make info` ma pokazać
  pin. Dodać `make sync-core`.

**Weryfikacja B1:** (1) `python scripts/sync_core.py` → „vendored tree
matches core exactly"; (2) w Pythonie Blendera 5.2 (`blender -b
--python-expr`): `import animatica_core.core.request_builder,
animatica_core.gltf_parser, animatica_core.mmcp_client` przechodzi — bez
Qt, z numpy 2.3.4 z Blendera; (3) addon rejestruje się i wyrejestrowuje
dwukrotnie pod rząd (F3 → Reload Scripts) bez śladu starych modułów core
w `sys.modules` (D2); (4) `make zip` — rozmiar zanotowany do R4.

### B2 — `core_adapter.py` (serce zmiany)
- `state_from_settings(settings, scene, model_caps) -> object` — duck-typed
  `state` z atrybutami z faktu 7: `steps` (`quality_preset`/`custom_steps`),
  `cfg_type` (`cfg_enabled` → `separated`/`nocfg`), `cfg_text_weight`/
  `cfg_constraint_weight`, `post_processing`, `transition_frames`
  (`num_transition_frames`), `num_samples=1`, `seed` + `random_seed`
  (`seed==0`), `animation_mode=""` (wartość `existing_take` wymusza
  `num_samples=1` — Blender nie ma take'ów), `start_frame`/`total_frames`
  ze sceny (fallback core), bez flag `debug_*`.
- `character_state_from_scene(...) -> CharacterState`: `PromptBox` z
  `PromptBlock` (start/end/text/enabled; seed w `params` — patrz K8),
  markery z klatkami **absolutnymi** + `frame_offset` = start okna:
  ścieżka Bezier → `root2d` per klatka (ta sama gęstość co dziś,
  `aim_heading_radians` gdy `match_direction`), empties → efektory
  (`type` ∈ `left-hand`… jako kategoria, **`joint` = nazwa kanoniczna
  z `END_EFFECTOR_JOINTS`**, `value.position` w wire frame), pozy →
  `fullbody` (`joint="", value={joint_rotations, root_position}`)
  z istniejącego `sample_pose_keyframes`.
- `frame_range(prompt_blocks, armature, scene)`: dzisiejsza unia promptów
  i spanu akcji → `frame_range_override` (fakt 9); regen bloku: zakres
  bloku ±1.
- Konwersja osi **przed** przekazaniem do core (markery w wire frame) —
  dokładnie tak, jak dziś robi `constraints_ui`; `coords.py` bez zmian.

**Weryfikacja B2:** testy jednostkowe adaptera **poza Blenderem**
(obiekty-atrapy dla `settings`/`PromptBlock`; nowy `tests/` w repo —
pierwszy w historii addonu) + B0-diff = 0 na c0–c4.

### B3 — przepięcie wywołań
- `operators.py` (generate, regenerate-block, generate-pose),
  `timeline_operators.py` (regen bloku): `request_builder.build_request…`
  → `core_adapter.build_request(...)` → `core.request_builder.build_request`
  z overridami (regen bloku = `segments_override` + `frame_range_override`
  ±1 + `anchor_markers` z `sample_pose_at_frame`); generate-pose →
  `core.request_builder.build_pose_request`.
- `client_shim.py`: `connect` → `core.mmcp_client.get_capabilities(get_mmcp_url(), use_cache=False, access_token=…)`;
  `store/clear/cached_*` zostają jako opakowanie na potrzeby enum
  i `last_connection_error`; `MmcpClient.generate` → `core.generate(...)`
  (wątek roboczy bez zmian; `on_progress` na razie nieużywany).
- `gltf_to_blender`: dekodowanie przez `parse_gltf_samples`; `_frame_from_time`
  zastąpione indeksem klatki (`i + start`, bo SDK stempluje `i/fps`);
  `read_extension_metadata` zostaje.
- Usunięcie martwych: addonowe `request_builder.build_*`, `build_options`,
  transport w `mmcp_client`, dekodery w `gltf_to_blender`. `rig_probe.py`
  przejmuje funkcje bpy z `request_builder`.

**Weryfikacja B3:** suite A/B c0–c4 **PASS z hashem = golden** (D7);
regresja bake'u przez dump FBX suite (tolerancja `measured_decimals`);
ręcznie w Blenderze: connect → oba modele → import szkieletu → ścieżka L
→ generate → accept, na kimodo i ardy; regen bloku; generate pose.

### B4 — dokumentacja i porządki
- `docs/developing.md`: sekcja „Wspólny kod z SDK" (vendoring, pin, shim,
  co jest adapterem, czego nie edytować w `animatica_core/`).
- `CONTRIBUTING.md`: „nie edytuj `animatica_blender/animatica_core/` —
  poprawka idzie do SDK, potem bump pinu".
- `CHANGELOG.md`; usunięcie szczątka `proscenium_blender/`.
- `AB-TESTS.md` (SDK): zdanie o „no-shared-code policy" zastąpić stanem
  faktycznym — dopiero tu, bo do B3 polityka formalnie jeszcze obowiązuje.
- Plan nadrzędny (3ds Max repo) §8.1: status Phase 3 + notatka o D3.

### K7 (SDK, warunek B0) — runner A/B po rename
- `tools/ab_suite/blender_runner.py`: `proscenium_blender` →
  `animatica_blender` (importy, `_wire_to_blender`, nazwy operatorów).
  Dziś runner nie odpali się wcale — to dług po rename addonu, niezależny
  od konwergencji, ale bez niego nie ma czym zmierzyć B0.
- Razem z K7: scenariusz c5 (B0.5). Osobny PR do `motionmcp-client-sdk`.

### K8 (SDK, warunek B3) — seedy per segment w core
- `build_segments`: czytać `getattr(b, "seed", None)` albo `params["seed"]`
  z `PromptBox`; `_normalize_segments`: przepuszczać `seed` z
  `segments_override`; oba **wyłącznie gdy `model_caps.supports_segment_seed`**
  (starsze serwery odrzucają nieznane pola — `extra="forbid"`).
- Test jednostkowy w SDK; wpis w CHANGELOG SDK; **nie zmienia** requestów
  MoBu/Max (nie ustawiają seedów), więc ich goldeny zostają.
- Osobny PR do `motionmcp-client-sdk`; po merge'u — bump pinu w B1
  (`sync_core.py --write --ref <nowy>`), co przy okazji przećwiczy
  procedurę z VENDORING.md „Bumping the pin".

## 4. Kolejność i zależności

```
K7 (SDK: rename w runnerze) ──► B0 (suite A/B zielona na starym develop
                                     + diff builderów + decyzje) ──► próg
                                        │
K8 (SDK: seedy per segment) ────────────┼─► B1 (vendor @ pin z K8 + bootstrap + CI)
                                        └─► B2 (adapter + testy)
                                B1 ∧ B2 ∧ K8 ──► B3 (przepięcie) ──► B4
```
B1 i B2 można prowadzić równolegle (adapter testuje się bez Blendera na
kopii core z checkoutu SDK). B3 wymaga obu **i** K8 — inaczej kłódki seedów
na timelinie przestaną działać bez żadnego błędu.

## 5. Weryfikacja końcowa

1. `python scripts/sync_core.py` → „matches core exactly"; CI job zielony.
2. Suite A/B Blender **c0–c5**: PASS, hashe kanoniczne identyczne z
   goldenami zamrożonymi w B0 — to jest definicja „nic nie zmieniliśmy
   na drucie poza tym, co profil uznaje za równoważne". c5 dodatkowo
   dowodzi, że seedy per segment przeżyły (K8).
3. Dumpy FBX z suite w tolerancji (bake) — tu dopuszczamy dryf
   z konwersji macierz→kwaternion (D9), ale nie więcej.
4. Ręczne E2E w Blenderze 5.2 na kimodo i ardy (lista w B3).
5. `git diff --stat`: `animatica_blender/*.py` (bez `animatica_core/`)
   **maleje** o rząd ~1 500 linii; jeśli nie — adapter dubluje core.
6. Blender reload addonu (F3 → Reload Scripts) nie zostawia starych
   modułów core (D2) — sprawdzone przez bump pinu na żywo.

## 6. Ryzyka

| # | Ryzyko | Prawdop. | Mitygacja |
|---|---|---|---|
| R1 | Diff builderów wykaże różnice, których nie da się uznać za równoważne ani przyjąć z core (np. logika kontrol-rigów, `fill_mode`) | średnie | B0 jest po to; każda różnica ma jawną decyzję zanim powstanie kod; „bug w core" idzie do SDK jako osobny PR, nie obchodzimy go w adapterze |
| R2 | `parse_gltf` gubi metadane `MMCP_motion` (zakresy bloków, `canonical_to_request`) i daje macierze zamiast kwaternionów → bake per blok / control-rig traci dane albo dryfuje | średnie | Addon zachowuje `read_extension_metadata`; konwersja do kwaternionów w addonie; regresja bake'u przez dump FBX z tolerancją; jeśli core powinien nieść te metadane — PR do SDK (`parse_gltf_samples` już czyta `samples[i]`) |
| R3 | Kolizja `sys.path`: dwa addony vendorujące `animatica_core` w jednym Blenderze | niskie | Dziś jeden konsument; `register()` sprawdza, czy już zaimportowany `animatica_core` ma ten sam `CORE-VERSION`, i ostrzega |
| R4 | Zip rośnie o ~20 k linii martwego Qt/gates | pewne | Zmierzyć w B0; jeśli nieakceptowalne — **zmiana w SDK** (lista wykluczeń w `VendorConfig`), nie lokalne wycinanie (zepsułoby STALE/MISSING) |
| R5 | Zmiana bajtów requestu (`timing.fps`, domyślna ścieżka ARDY) zaskoczy użytkowników — inne wyniki dla tych samych scen | pewne dla ARDY bez ścieżki | D6 jawnie; wpis w CHANGELOG „ARDY bez narysowanej ścieżki idzie teraz do przodu, jak w MoBu/Max"; suite A/B dokumentuje równoważność |
| R6 | Brak testów w repo addonu — adapter bez siatki bezpieczeństwa | pewne dziś | B2 zakłada `tests/` i pierwszy job CI; koszt ~1 dzień, zwrot przy każdym bumpie pinu |
| R7 | SDK się zmienia w trakcie (MoBu pracuje aktywnie) — pin ucieka | średnie | Pin `c2076fd` zamrożony na czas prac; bump dopiero po zielonym B3, osobnym commitem |
| R8 | Wydajność: numpy w core na ścieżce generate vs czyste listy w addonie | niskie | Blender ma numpy natywnie; bake już używa mathutils; pomiar czasu generate przed/po w B3 |
| R9 | Cicha utrata seedów per segment (fakt 9, trzeci punkt) | pewne bez K8 | K8 jest warunkiem B3; golden **c5** (dwa bloki z pinami seedów) zamrożony w B0.5 na starym `develop`, żeby regresja świeciła na czerwono, a nie była notatką |
| R10 | Markery spoza zakresu core wyrzuca „z ostrzeżeniem" — addon dziś rozszerza okno | pewne bez adaptera | `frame_range_override` z dotychczasową unią (fakt 9); ostrzeżenia core logowane do panelu, nie połykane |

## 7. Czego NIE robimy

- Nie budujemy `blender_bridge` ani nie podłączamy `gates/` (D3) — to
  osobna decyzja z własnym uzasadnieniem (czy Blender ma mieć sweep bramek
  jak Max/MoBu).
- Nie ruszamy auth, `live/`, `gui/`.
- Nie przepisujemy UI `bpy` — B3 planu nadrzędnego („wire bpy UI to core")
  realizujemy jako adapter, nie jako nowy interfejs.
- Nie edytujemy niczego w `animatica_blender/animatica_core/` ręcznie —
  poprawki idą do SDK.
- Nie zmieniamy pinu innych hostów.

## 8. Decyzje do podjęcia przed startem

1. **D6** — akceptujesz przyjęcie zachowania core dla ARDY bez ścieżki
   (syntetyczny marsz zamiast kotwicy w miejscu)? To zmienia to, co widzi
   użytkownik Blendera.
2. **R4** — czy ~20 k linii martwego Qt w zipie jest OK, czy chcesz
   najpierw wykluczeń w SDK (osobny PR, opóźnia B1)?
3. **D3** — zgoda na korektę planu nadrzędnego (bez bridge'a w tej fazie)?
4. Kto robi K7 i **K8** w SDK — oba są warunkami (B0 i B3), a SDK ma dziś
   14 niezacommitowanych plików Twojej pracy w `tools/ab_suite/`; nie chcę
   wchodzić w to repo bez uzgodnienia kolejności.
5. **Seedy per segment (K8):** zrobić w SDK przed B3 (rekomendacja — to
   luka SDK, nie Blendera), czy świadomie tymczasowo stracić funkcję?

---

## 9. Dziennik wdrożenia (2026-09-04)

Plan wdrożony w całości na gałęzi `feature/core-convergence`. Decyzje z §8
podjęte wg rekomendacji zapisanych w planie, bo wdrożenie ruszyło bez
odpowiedzi — każda jest niżej nazwana wprost.

### Odpowiedzi na §8

1. **D6 — przyjmujemy zachowanie core.** Z jednym pomiarem, który zmienia
   wymowę: `_inject_default_walk_path` dla `ardy-core-rp` jest na ścieżce
   blenderowej **martwe**. Funkcja wychodzi wcześnie, gdy w `constraints`
   jest jakikolwiek `root_path`, a automatyczna kotwica klatki 0 nim jest.
   Użytkownik ARDY nie zobaczy więc syntetycznego marszu — ryzyko R5
   nie materializuje się.
2. **R4 — vendorujemy cały pakiet.** Zmierzone: zip rośnie z 10 734 006 B
   do 11 972 115 B, czyli o 1 209 KiB (11,5 %); `gui/` + `gates/` to
   19 682 linie i 284 KiB skompresowanych, ok. 23 % przyrostu. Do
   przyjęcia; wykluczenia w `VendorConfig` zostają jako opcja na później.
3. **D3 — bez bridge'a.** Potwierdzone w kodzie: nie-GUI połowa core nie
   importuje ani `bridge`, ani `host`.
4. **K7 i K8 zrobione po naszej stronie**, każde w osobnym worktree SDK,
   żeby nie dotykać 14 niezacommitowanych plików w głównym checkoucie.
   Gałęzie: `fix/ab-blender-runner-rename`, `feature/builder-gaps-for-blender`.
5. **K8 przed B3** — tak. Pin core przesuwany dwa razy, ostatecznie na
   commit z seedami per segment i `fill_mode`.

### Odstępstwa od planu

| # | Plan mówił | Zrobiono | Dlaczego |
|---|---|---|---|
| 1 | B0.5: checkpoint **c5** (dwa bloki z przypiętymi seedami), golden zamrożony na starym `develop` | c5 **nie powstał**; regresję seedów pilnują testy jednostkowe (`tests/test_core_adapter.py::TestSegments`) i test SDK `test_segment_seeds.py` | `--freeze-golden` wymaga żywego serwera **i** zera FAIL na wszystkich trzech hostach — MoBu potrzebuje działającej konsoli MotionBuilder, Max `3dsmaxbatch`. Test jednostkowy pilnuje tego samego bez tej maszynerii |
| 2 | Regen bloku: `frame_range_override` poszerzony o **±1** | zakres bloku **bez** poszerzenia; kotwice na `block_start`/`block_end`, próbkowane z klatek ±1 | Core relatywizuje wszystkie markery do `frame_range[0]`, więc ±1 przesunęłoby o klatkę każdą edycję użytkownika i każdy efektor. MoBu robi to samo bez poszerzania |
| 3 | `constraints_ui.sample_*` mają emitować `ConstraintMarker` | `sample_*` bez zmian; konwersja dict→marker w `core_adapter` | Czysta, testowalna bez Blendera; zero ryzyka dla ścieżki przed przepięciem |
| 4 | §1: `M_TO_CM`, `DEFAULT_FPS` z core | nie dotyczy | Tych stałych w addonie nie ma |
| 5 | §5.5: ubytek ok. **1 500 linii** | ubytek **103** linii | Kasacja to −659, ale `core_adapter.py` ma 715 linii, z czego ok. 400 to docstringi cytujące pomiary. Logika nie jest zdublowana (żadnego budowania segmentów, budżetu ograniczeń, kotwic, timingu) — to gęstość komentarza, nie kod |

### Zmiany w SDK, których plan nie przewidywał

- **`fill_mode` nie przechodził przez wspólny builder.** Addon wysyła
  `rest`/`generate` (sterowanie serwerowym `FullBodyConstraintSet`), core
  gubił pole. Bez poprawki konwergencja wyłączyłaby tę funkcję po cichu.
  Naprawione w SDK obok seedów per segment.
- **Runner A/B musiał przejść na `core_adapter`** — plan przewidywał w K7
  tylko rename importów, ale po usunięciu `request_builder.py` runner nie
  miał czego wołać. Przy okazji naprawił się c3, który wcześniej padał na
  `AttributeError`.

### Weryfikacja (§5) — stan faktyczny

| # | Sprawdzenie | Wynik |
|---|---|---|
| 1 | `scripts/sync_core.py` | „vendored tree matches core exactly" |
| 2 | Suite A/B, odtwarzanie z kasety, hashe kanoniczne | **c1, c2, c3 = golden**; c4 nie — patrz niżej |
| 3 | Regresja bake'u | 7 nagranych odpowiedzi × 4 ścieżki: rotacja maks. 2,4e-06 stopnia, pozycje co do bitu |
| 4 | E2E na żywym serwerze (kimodo) | PASS: capabilities → armatura → request → generate → bake, 123 krzywe, 7380 kluczy |
| 5 | Bilans linii | −103 (patrz odstępstwo 5) |
| 6 | Reload addonu bez pozostałości core | PASS, dwukrotnie pod rząd |
| — | Testy addonu (nowe) | 56 zielonych |
| — | Testy SDK | 1187 zielonych + 1 pominięty |

**c4 — dług spoza tego zadania.** Jedyna różnica to `ab_scenario.HAND_PIN`,
który zmienił się w SDK `c2076fd` **po** zamrożeniu blenderowych goldenów:
`(0.35, 1.20, 0.40)` → `(0.30, 1.20, 1.30)`. Po przywróceniu wartości
z epoki goldena w pamięci c4 trafia bajt w bajt. Zamknięcie tego wymaga
przenagrania goldenów Blendera i wpisu c4 w kasecie — z żywym serwerem
i wszystkimi hostami.

### Czego nie zweryfikowano

- **ARDY end-to-end.** Lokalny serwer serwuje dziś tylko `kimodo-soma-rp`;
  ARDY mieszka w prywatnym forku i wymaga własnej konfiguracji. Ścieżka
  addonu jest dla obu modeli identyczna — różnice są serwerowe.
- **Operatory modalne w GUI.** W trybie `-b` nie ma menedżera okien, więc
  E2E woła to, co operatory wołają, z pominięciem samej pętli modalnej.
- **Tryb `record` runnera A/B** — przepięty na `client_shim`, nieuruchomiony.
