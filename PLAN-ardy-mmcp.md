# Plan: dodanie modelu ARDY (NVIDIA) jako drugiego motion modelu obok Kimodo

**Cel:** użytkownik Proscenium w Blenderze wybiera z dropdownu model `kimodo-soma-rp`
albo `ardy-core-rp` i generuje animację — przełączanie bez zmiany serwera i bez
reinstalacji addonu.

---

## 0. Ustalenia z analizy kodu (fakty, nie założenia)

- **SDK `motionmcp` 0.2.0 już wspiera wiele modeli**: `build_app()` przyjmuje
  pojedynczy `Backbone`, mapę `{model_id: Backbone}` lub listę; `/capabilities`
  publikuje wszystkie modele, `/generate` dispatchuje po polu `model`
  (`motionmcp/server.py:48-90, 176-180`). **Addon Blendera już dziś obsługuje
  wybór modelu** — `settings.model_id` to dynamiczny EnumProperty zasilany z
  `/capabilities` (`properties.py:501-514`), a cały request-builder czyta
  capabilities per-model. Przełączanie kimodo↔ardy to po stronie klienta
  ~zero zmian funkcjonalnych.
- **ARDY** (github.com/nv-tlabs/ardy, checkpointy `nvidia/ARDY-*` na HF,
  lipiec 2026): autoregresywny model dyfuzyjny; wariant **Core**: szkielet
  27 stawów o nazwach mixamo-podobnych (`Hips`, `LeftHand`, `RightFoot`…),
  **20 fps**, horyzont generacji 40 klatek, max 8 s outputu. Wariant G1 =
  robot Unitree (poza zakresem — addon jest humanoidalny).
- **API ARDY** (wzorzec: `scripts/generate.py`):
  `load_model("core", device)` → model z `.skeleton`, `.fps`, `.motion_rep`;
  constrainty budowane z **tych samych klas co kimodo**
  (`Root2DConstraintSet`, `FullBodyConstraintSet`, `EndEffectorConstraintSet`,
  L/R Hand/Foot — `ardy/constraints.py`) →
  `model.motion_rep.create_conditions_from_constraints_batched(...)` →
  `(observed_motion, motion_mask)` → `model(texts, num_frames,
  num_denoising_steps, pad_mask, first_heading_angle, motion_mask,
  observed_motion, cfg_weight, ...)` → `model.motion_rep.inverse(motion)` →
  dict `{local_rot_mats, root_positions, foot_contacts}` — **ten sam kontrakt
  wyjściowy co kimodo**. Postprocess: `post_process_motion(...)`.
- **Pipeline chmurowy w `mmcp_server.py` jest w ~80 % generyczny**: ekspansja
  pose→text, normalizacja T-pose, forward/reverse retarget (boneclassifier
  działa na dowolnym humanoidzie), kanonizacja originu, client-frame
  postprocess — wszystko operuje na dictach szkieletu i wynikach
  `local_rot_mats/root_positions`, nie na wewnętrzach kimodo.
  Kimodo-specyficzne są tylko: `translate_request()` + wywołanie modelu
  (krok 3 w `generate()`).
- **Klient — jedna realna kolizja**: `looks_like_kimodo_skeleton()`
  (`body_mesh.py:321`, próg 12 wspólnych nazw) dopasuje szkielet ARDY Core
  w 20/20 nazw → addon **błędnie doczepiłby siatkę SOMA77** do rigu Core
  o innych proporcjach. Wymaga bramki po stronie klienta.
- Nazwy end-effectorów Core są zgodne z `constants.END_EFFECTOR_JOINTS`
  (LeftHand/RightHand/LeftFoot/RightFoot) — pinowanie efektorów i
  foot-contacts działają bez zmian.

## 1. Decyzje projektowe

| # | Decyzja | Wybór | Uzasadnienie |
|---|---------|-------|--------------|
| D1 | Gdzie serwować ARDY | Ten sam serwer `motionmcp-kimodo-cloud`, drugi backbone w `build_app` | SDK ma gotowy rejestr; klient łączy się z jednym URL; przełączanie = wybór z dropdownu |
| D2 | Wariant ARDY | `ARDY-Core-RP-20FPS-Horizon40` (nickname `core`) | Humanoid, najdłuższy horyzont; G1 poza zakresem (robot) |
| D3 | Id modelu na wire | `ardy-core-rp` | Spójne z konwencją `kimodo-soma-rp` |
| D4 | Zakres faz | Faza 1: pojedynczy segment text/unconditioned + wszystkie 3 constrainty + retarget. Faza 2: multi-segment (prompt blocki), pose, per-segment seed | Ogranicza ryzyko; Faza 1 daje działające przełączanie E2E |
| D5 | Wybór załadowanych modeli | Env `MMCP_MODELS` (default: oba) | VRAM: kimodo (SOMA + LLM2Vec/Llama-3) + ARDY (326 M + własny LLM2Vec) mogą nie zmieścić się razem na jednej karcie |

## 2. Zmiany serwerowe (`C:\_CODE\motionmcp-kimodo-cloud`)

### Krok S1 — zależność na pakiet `ardy`
- Dodać do `pyproject.toml`: `ardy @ git+https://github.com/nv-tlabs/ardy`
  (lub vendored path-dep, jeśli git-dep nie przejdzie przez uv).
- **Ryzyko kolizji**: oba repa wykorzystują pakiet `motion_correction`
  (katalog `MotionCorrection/` istnieje w obu, ten sam rodowód). Zweryfikować,
  czy `ardy` deklaruje go jako instalowaną zależność; jeśli tak — użyć jednej
  kopii (kimodo-cloud) i wykluczyć drugą, albo potwierdzić zgodność API
  (`motion_postprocess.correct_motion`).
- Weryfikacja: `uv sync` przechodzi; `python -c "import ardy, kimodo"` w tym
  samym venv bez konfliktu.

### Krok S2 — refaktor `mmcp_server.py`: wydzielenie wspólnego pipeline'u
- Wyciągnąć z `KimodoCloudBackbone.generate()` część generyczną do klasy
  bazowej `RetargetingCloudBackbone` (nowy plik `cloud_backbone.py`):
  pose-expansion, `compute_tpose_q_matrices` + normalizacja, forward retarget,
  `_normalize_origin`, uruchomienie modelu (hook), `_unnormalize_output`,
  reverse retarget + inverse T-pose, `_post_process_client_motion`,
  budowa `MotionResult`.
- Abstrakcyjne hooki: `_load_model()`, `_run_model(req) ->
  {local_rot_mats, root_positions, foot_contacts}`, `_model_spec()` oraz
  właściwości `skeleton` / `output_skeleton` / `fps` (baza dziś czyta
  `self.model.skeleton`, `getattr(model, "output_skeleton", ...)` — po
  refaktorze przez property, żeby nie zakładać kształtu `self.model`).
- Kontrakt: `_run_model` odpowiada też za postprocess **w ramce
  kanonicznej** (kimodo robi to wewnątrz modelu; ardy woła
  `post_process_motion` jawnie — patrz S4). Baza robi wyłącznie pass
  client-frame po reverse-retargecie, jak dziś.
- `KimodoCloudBackbone` staje się podklasą; jego `_run_model` =
  dzisiejszy `translate_request` + `self.model(**kwargs)`.
- **Weryfikacja regresji** (repo nie ma testów): dwa poziomy, bo pełny
  golden na GPU nie jest bitowo deterministyczny (cuDNN):
  1. testy jednostkowe czysto-numpy'owych etapów, które refaktor przenosi:
     `_normalize_origin`/`_unnormalize_output`, tpose fwd/inv, slice
     szkieletu wyjściowego — stałe wejścia, dokładne asserty
     (`tests/test_cloud_backbone.py`);
  2. E2E przed/po refaktorze: ten sam request (stały seed,
     `TEXT_ENCODER_MODE=dummy`) na kanonicznym + na rigu Mixamo,
     porównanie gltf z tolerancją; przy niezgodności — inspekcja wizualna.

### Krok S3 — `ardy_backbone.py`: `ArdyCloudBackbone(RetargetingCloudBackbone)`
- **Uwaga na dwie przestrzenie nazw**: atrybut `self.model_id` musi być
  **id MMCP** (`"ardy-core-rp"`) — SDK czyta go w `_backbone_id()` do
  rejestru. Nick z rejestru ardy (`"core"`, `"core8"`…) trzymać osobno,
  np. `self._ardy_variant = os.environ.get("ARDY_MODEL", "core")`.
  (W kimodo te dwa pojęcia przypadkiem się pokrywają — u ardy nie.)
- `setup()`: `ardy.model.load_model(self._ardy_variant)`,
  idempotentnie (wzór z kimodo — podwójne wywołanie setup() w deploy'u).
- `_model_spec()`: `ModelSpec(id="ardy-core-rp", fps=20.0,
  canonical_skeleton=skeleton_to_mmcp(model.skeleton),
  supports_retargeting=True, supports_segment_seed=False,
  supported_segments=["text","unconditioned"] (Faza 1),
  supported_constraints=["root_path","effector_target","pose_keyframe"],
  limits=Limits(max_duration_seconds=8.0),
  recommended_max_duration_seconds=8.0,
  predicted_contact_joints=_resolve_foot_contact_joints(...))`.
- **Weryfikacja założenia**: `skeleton_to_mmcp()` i retarget pipeline czytają
  atrybuty `bone_order_names`, `root_idx`, `hip_joint_idx`, offsety — sprawdzić
  parytet klasy `CoreSkeleton27(SkeletonBase)` z ardy vs `SkeletonBase` kimodo
  (wspólny rodowód, ale to trzeba potwierdzić importem i smoke-testem, nie
  na oko).

### Krok S4 — `ardy_translate.py`: MMCP request → wywołanie ARDY
- Mapowanie wzorowane 1:1 na `kimodo/mmcp/translate.py` (te same klasy
  constraintów, tym razem z `ardy.constraints`):
  - `root_path` → `Root2DConstraintSet`
  - `effector_target` → odpowiednie `*ConstraintSet` po stawie
  - `pose_keyframe` → `FullBodyConstraintSet` / EE-sety wg `fill_mode`
- Handshake fps jak w kimodo (`timing.fps` musi == 20, inaczej
  `invalid_options`).
- Dalej: `create_conditions_from_constraints_batched` → maski;
  `num_denoising_steps` z `options.diffusion_steps`; postprocess w ramce
  kanonicznej przez `ardy.postprocess.post_process_motion` (jak w
  `scripts/generate.py`; sterowany `options.post_processing`);
  seed przez `seed_everything` (już w bazie).
- **Guidance**: ARDY przyjmuje skalar `cfg_weight` + `cfg_type`; addon przy
  włączonym CFG wysyła `{"type":"separated","weight":[text, constraint]}`.
  Decyzję podjąć w S4 po lekturze `ardy/model/cfg.py`: jeśli ardy ma
  odpowiednik separated — zmapować wprost; jeśli nie — mapować
  `weight[0]` (tekst) na `cfg_weight` i **odnotować w docs**, że waga
  constraintów jest ignorowana (SDK nie waliduje guidance, więc backbone
  musi to obsłużyć sam, bez wyjątku).
- **Request bez segmentów**: przy 0 włączonych blokach addon wysyła samo
  `duration_frames` — jak w kimodo utworzyć syntetyczny pusty segment
  (unconditioned) o długości `req.total_frames`.
- Faza 1: `len(segments) > 1` → `ProtocolError("invalid_options",
  "ardy-core-rp obsługuje na razie jeden prompt block — scal bloki lub
  przełącz na kimodo")` — komunikat widoczny w panelu addonu.
- `first_heading_angle=0` współgra z kanonizacją originu z bazy (postać
  patrzy w +Z po `_normalize_origin` — dokładnie ta konwencja).

### Krok S5 — rejestracja i konfiguracja
- `mmcp_server.py` — rejestrować **listą**, nie mapą (SDK sam czyta
  `backbone.model_id`, więc nie dublujemy id, które w kimodo pochodzi z env
  `KIMODO_MODEL` i mogłoby się rozjechać z kluczem mapy):
  ```python
  _all = [KimodoCloudBackbone(), ArdyCloudBackbone()]
  enabled = os.environ.get("MMCP_MODELS")  # np. "ardy-core-rp"
  _backbones = [b for b in _all if not enabled or b.model_id in enabled.split(",")]
  app = build_app(_backbones, title="MMCP server (Kimodo + ARDY cloud)")
  ```
- `/health`: raportować listę modeli + `model_loaded` per backbone.
- Tekst-encoder ARDY: wyrównać do trybu kimodo (`TEXT_ENCODER_MODE`
  dummy|local|api; ardy ma bliźniacze `text_encoder_api.py`). W deployu
  zdecydować: dwa lokalne enkodery (VRAM!) czy `api`. Zmierzyć VRAM przy obu
  modelach załadowanych — wynik decyduje o defaultach `MMCP_MODELS`.

### Krok S6 — deployment i dokumentacja
- `deploy/modal_app.py`: prefetch checkpointów ARDY z HF (licencja NVIDIA
  Open Model Agreement — repo nie jest gated, ale odnotować licencję w
  ATTRIBUTIONS), env dla `MMCP_MODELS`/`ARDY_MODEL`.
- `Dockerfile` / `docker-compose.gpu.yaml`: analogicznie.
- README + CHANGELOG.

### Faza 2 (osobne PR-y, po akceptacji Fazy 1)
- **Multi-segment** (parytet z prompt blockami Proscenium): ARDY jest
  autoregresywny — `autoregressive_step()` (`ardy_model.py:706`) pozwala
  zmieniać prompt między oknami. Mapowanie: harmonogram segmentów →
  tekst per okno AR. Po wdrożeniu dodać `"pose"` (trick z ekspansją do
  krótkiego textu i środkową klatką — jak kimodo, kontekst wyrównany do
  horyzontu 40).
- Per-segment seed → `supports_segment_seed=True`.

## 3. Zmiany klienckie (`C:\_CODE\proscenium-blender`)

### Krok K1 — bramka siatki ciała SOMA77 (bugfix wyprzedzający)
- W operatorze importu szkieletu kanonicznego
  (`canonical_skeleton.PROSCENIUM_OT_import_canonical_skeleton`) i/lub w
  `body_mesh.looks_like_kimodo_skeleton`: doczepiać siatkę **tylko dla modeli
  rodziny SOMA** — warunek: `"soma" in model_id.lower()` (id jest znane w
  operatorze) zamiast/obok heurystyki nazw stawów. Checkbox `with_body`
  ukryty/wyłączony dla `ardy-core-rp` z krótkim labelem „body mesh
  niedostępny dla tego szkieletu".
- Test manualny: import `ardy-core-rp` → armatura 27 kości, bez siatki;
  import `kimodo-soma-rp` → siatka jak dotąd.

### Krok K2 — ostrzeżenie o niezgodności armatura↔model (małe UX)
- Panel główny: gdy `target_armature["proscenium_canonical_model"]` istnieje
  i ≠ `settings.model_id` → wiersz z ikoną INFO „Armatura zaimportowana dla
  modelu X — wybrany model Y; wygeneruj przez retarget albo zaimportuj
  szkielet Y". Bez mutacji stanu w draw (wzór: istniejące hinty).
- To pokrywa główny scenariusz przełączania: user ma rig SOMA30, wybiera
  ardy → serwer i tak zretargetuje (poprawne), ale user wie, co się dzieje.

### Krok K3 — dokumentacja i wersja
- `docs/usage.md` + `docs/limitations.md`: sekcja o wyborze modelu,
  różnice (fps 20 vs 30, limit 8 s, brak body mesh, Faza 1: jeden prompt
  block na ardy).
- Bump `bl_info["version"]` → 0.4.1 + wpis w CHANGELOG (zmiany K1/K2 są
  klienckie i wchodzą do zipa releasu).

### Poza zakresem klienta (świadomie)
- Żadnych zmian w `request_builder`, `gltf_to_blender`, bake'ach — fps i
  szkielet płyną z capabilities/gltf i są już per-model.
- Przycisk **Generate Pose** sam zniknie dla ardy w Fazie 1 — panel pokazuje
  go tylko, gdy model ogłasza `"pose"` w `supported_segments`
  (`panels.py`); klient degraduje się poprawnie bez zmian.
- `_EE_CHAIN_BONES` zawiera `LeftHandMiddleEnd` (SOMA), a Core ma
  `LeftHandEnd` — skutek: keyframe na palcach Core wymusi `fill_mode="rest"`;
  zachowanie poprawne, tylko konserwatywne. Odnotować w docs, nie ruszać.

## 4. Kolejność i zależności

```
S1 (dep ardy)  ──►  S2 (refaktor + golden test)  ──►  S3+S4 (backbone+translate)  ──►  S5 (rejestr)  ──►  E2E
                                                                                                   │
K1 (bramka mesh) ── niezależne, można równolegle ──────────────────────────────────────────────────┤
K2, K3, S6 ── po działającym E2E ──────────────────────────────────────────────────────────────────┘
```
Refaktor S2 musi poprzedzać S3 (backbone ardy buduje na klasie bazowej).
K1 jest niezależny i wart wdrożenia nawet bez serwera.

## 5. Weryfikacja końcowa (E2E)

1. `uv run python mmcp_server.py` (oba modele, `TEXT_ENCODER_MODE=dummy` do
   smoke'a, potem realny) → `GET /capabilities` zawiera 2 wpisy o poprawnych
   szkieletach (30 vs 27 stawów, 30 vs 20 fps).
2. Blender: Connect → dropdown pokazuje oba modele.
3. `ardy-core-rp`: Import skeleton (bez siatki) → pose keyframe + root path →
   Generate → Accept; sprawdzić brak ślizgania stóp (postprocess działa)
   i heading zgodny z krzywą.
4. Rig Mixamo: Generate na ardy (forward+reverse retarget) → animacja ląduje
   na rigu użytkownika.
5. Przełączenie z powrotem na kimodo bez restartu Blendera → Generate OK
   (brak stanu zaśmieconego przez ardy).
6. Limity: request > 8 s na ardy → czytelny błąd w panelu (banner), nie traceback.
7. Regresja kimodo: golden test z S2 zielony.

## 6. Ryzyka i mitygacje

| Ryzyko | Prawdop. | Mitygacja |
|--------|----------|-----------|
| Kolizja pakietu `motion_correction` (oba repa) | średnie | Krok S1: jedna kopia; potwierdzić zgodność API przed S3 |
| Parytet atrybutów `CoreSkeleton27` vs oczekiwania `skeleton_to_mmcp`/retargetu | średnie | Smoke-test importu + jednostkowy test spec-a na początku S3 |
| VRAM: dwa modele + dwa enkodery tekstu | wysokie | `MMCP_MODELS`, tryb `api` dla enkoderów; pomiar w S5 decyduje o defaultach |
| Refaktor S2 zmienia zachowanie kimodo | średnie | Golden test przed/po (S2), stały seed |
| Multi-block na ardy w Fazie 1 → błąd serwera | pewne (by design) | Czytelny komunikat błędu; K3 dokumentuje; Faza 2 domyka parytet |
| Jakość retargetu ARDY na rigi userów (model trenowany na Core) | nieznane | E2E pkt 4; w razie problemów ograniczyć `supports_retargeting=False` w Fazie 1 (wtedy generacja tylko na kanonicznym Core) |

## 7. Czego NIE robimy (świadome cięcia)

- Wariant G1 (robot) — bez sensu dla workflow Blender/humanoid.
- Zmiany w SDK `motionmcp` — niepotrzebne, 0.2.0 wystarcza.
- Multi-serwer po stronie addonu (profile URL-i) — niepotrzebne przy D1.
- Unifikacja enkoderów tekstu kimodo/ardy — osobny temat optymalizacyjny.
- **Proxy Animatica Cloud (auth/quota)** — leży poza oboma repozytoriami;
  plan pokrywa serwer MMCP i addon. Wdrożenie na `api.animatica.ai`
  (routing, ewentualne stawki quota per model) to osobne zadanie ops.
  Self-hosted działa od razu po S5.
