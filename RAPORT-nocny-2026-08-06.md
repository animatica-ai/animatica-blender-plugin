# Raport nocny — integracja ARDY (2026-08-06/07)

## TL;DR

Integracja **ARDY (NVIDIA) jako drugiego modelu obok Kimodo jest ukończona
i przetestowana E2E** — Fazy 1 i 2 planu, łącznie z multi-segmentem, pose,
pełnym postprocessingiem i E2E w Blenderze. Wszystkie testy automatyczne
zielone (13/13 przypadków błędów/limitów/seedów + adherencja ścieżki 4 cm
na obu modelach + regresja kimodo bitowo identyczna). **Jedyna rzecz
nietestowalna bez Ciebie: prompty tekstowe** — enkoder LLM2Vec wymaga
tokena HF z dostępem do gated `meta-llama/Meta-Llama-3-8B-Instruct`
(szczegóły niżej).

## Dostarczone playblasty

1. `proscenium_playblast.mp4` (wcześniejszy) — obie postacie z pierwszej
   sesji E2E (unconditioned).
2. **`playblast_rootpath_L.mp4` (nowy, główny)** — obie postacie idą
   równolegle po **tej samej ścieżce L** narysowanej krzywą w addonie:
   z lewej ARDY (szkielet punktowy), z prawej Kimodo (ciało SOMA77).
   Cały łańcuch: krzywa → constraint root_path → serwer → NLA.

## Wyniki testów (dziś w nocy)

| Test | ardy-core-rp | kimodo-soma-rp |
|---|---|---|
| Limit czasu (request > max) → czysty `invalid_options` | PASS | PASS |
| Nieznany model → `unknown_model` 400 | PASS¹ | PASS¹ |
| pose + text w segmentach → `schema_validation` 422 | PASS | PASS |
| Nieznany staw w constraincie → `unknown_joint` 400 | PASS | PASS |
| Ten sam seed 2× → **bitowo identyczny** wynik | PASS | PASS |
| Inny seed → inny wynik | PASS | PASS |
| **Adherencja root_path (ścieżka L, 3 piny)** | **max 4,0 cm** | **max 4,0 cm** |
| Przemieszczenie końcowe (oczekiwane ~2,12 m) | 2,05 m | 2,10 m |
| Multi-segment 2×80 klatek (8 s) | 200 / 4,3 s | n/d (natywnie) |
| Pose (1 klatka) | 200 / 2,3 s | 200 (wcześniej) |
| Regresja golden po wszystkich zmianach | — | **bitowo identyczna** |

¹ Po naprawie znalezionego dziś **buga SDK**: envelope błędów schematu
zawierał obiekt `ValueError` z pydantic i nie serializował się do JSON —
każdy błąd schematu wychodził jako gołe 500. Serwer ma teraz sanityzujący
handler (kandydat na fix w motionmcp-sdk).

**VRAM:** oba modele załadowane = **3,9 / 24,5 GB** (dummy enkodery).
Czasy generacji na RTX 3090: kimodo ~3 s, ardy ~2–4 s.

## Co przybyło dziś w nocy (poza testami)

- **Sanityzacja envelope błędów** w `mmcp_server.py` (bug SDK, wyżej).
- **Graceful brak pakietu ardy**: serwer bez zainstalowanego ardy serwuje
  samo kimodo z logiem (chyba że `MMCP_MODELS` żąda ardy — wtedy fail).
- **S6**: Dockerfile (boneclassifier + nowe moduły + vendored `ardy_src/`),
  `deploy/modal_app.py` (`ARDY_LOCAL_PATH`, fallback kimodo-only),
  CHANGELOG serwera.
- Dodatkowy przypadek w suicie N3 (pusty request → czysty 422).

## Blokery wymagające Twojej decyzji/ingerencji

1. **Prompty tekstowe (jedyny brakujący element user-facing):**
   `TEXT_ENCODER_MODE=local` wywala się na gated repo
   `meta-llama/Meta-Llama-3-8B-Instruct` (401, brak tokena HF na maszynie;
   publiczny endpoint modal Animatiki nie odpowiada). Żeby domknąć:
   `hf auth login` + zaakceptowany dostęp do Llama-3 na HF → wtedy
   odpalam testy jakościowe promptów i multi-segment "walk→jump"
   (ścieżka kodu jest gotowa i przetestowana na dummy). Uwaga na VRAM:
   enkoder ~16 GB bf16 — oba modele + enkoder powinny się zmieścić
   (jest 20 GB luzu), w razie czego `MMCP_MODELS` per model.
2. **`boneclassifier_best.pt`** — nadal brak (nietrackowany, nie ma na HF);
   bez niego retarget na rigi Mixamo/Rigify nie działa lokalnie.
3. **Deploy (Docker/modal)** — zaktualizowany, ale nieuruchamiany (brak
   środowiska deployowego na tej maszynie).

## Addendum (rano): per-segment seed na ARDY — WDROŻONY

Po raporcie nocnym dokończyłem ostatni element Fazy 2: `seed_schedule`
w pętli AR ardy (reseed torch na pierwszym oknie segmentu z pinem).
`supports_segment_seed=true` → kłódki seedów per blok w addonie działają
też dla ardy. Testy E2E (3/3 PASS): identyczność przy powtórce, zmiana
przy innym seedzie, oraz **bitowa stabilność segmentu 1 przy zmianie
seeda segmentu 2**. +4 testy jednostkowe mapowania okno→segment (razem
14/14). Wpisy w CHANGELOG serwera i docs addonu zaktualizowane.

## Pozostałe znane luki (niższy priorytet)
- Granulacja promptów na ardy = okno 2 s (udokumentowane w docs/limitations).
- Upstream PR-y do `nv-tlabs/ardy`: DummyTextEncoder + tryb dummy,
  `SKIP_MOTION_CORRECTION_IN_SETUP`, `text_feat_schedule` (wszystkie
  addytywne, gotowe w klonie `C:\_CODE\ardy`).
- Zbłąkane duplikaty armatur przy imporcie w Blenderze (`.001/.002` —
  do zbadania, czy operator importu nie odpala się podwójnie przez MCP).

## Jak to odpalić rano

```bash
cd C:\_CODE\motionmcp-kimodo-cloud
TEXT_ENCODER_MODE=dummy uv run --no-sync python mmcp_server.py --port 8000
```

Blender: addon jest junctionem do repo (zmiany repo widoczne po reload);
Self-hosted `http://localhost:8000`. Most MCP: `port_proxy.py` 9876→9878
w scratchpadzie sesji (albo przestaw port addonu MCP na 9876).

Pełna historia zmian i pułapek: `DEVLOG-local-windows.md` (serwer),
`CHANGELOG.md` (oba repa), `PLAN-ardy-mmcp.md` (plan — zrealizowany
w zakresie S1–S6 + Faza 2).
