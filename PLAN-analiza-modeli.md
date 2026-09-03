# Plan: dokument „Analiza modeli ruchu" (kimodo / ARDY / MotionBricks / HIL)

**Cel:** jeden dokument, do którego wracasz przy decyzjach produktowych
o modelach — co każdy potrafi, czym się różnią, co jest zmierzone,
a co tylko deklarowane.

**Ma spinać, nie zastępować.** Wiedza jest dziś rozproszona po
`PLAN-ardy-mmcp.md`, `PLAN-motionbricks-mmcp.md`, `PLAN-hil-mmcp.md`,
`RAPORT-nocny-2026-08-06.md` i historii tej sesji. Te pliki zostają jako
źródła szczegółów; nowy dokument daje przekrój i odsyła do nich
(spójne z ryzykiem R4 — bez kopiowania treści).

**Odbiorca:** właściciel produktu (Ty), nie implementator. Stąd nacisk na
funkcje i decyzje, a nie na sygnatury API.

**Rozmiar docelowy:** ok. 150–200 linii — cztery tabele plus zwięzły
komentarz. Dłuższy dokument przestaje być przekrojem.

---

## 1. Produkt i lokalizacja

- Plik: **`ANALIZA-modeli-ruchu.md`** w katalogu głównym
  `proscenium-blender` — tam gdzie pozostałe dokumenty analityczne
  (`PLAN-*.md`, `RAPORT-*.md`). Szczegóły implementacyjne zostają
  w `BACKBONES.md` po stronie serwera; ten dokument ich nie dubluje,
  tylko linkuje.
- Język: polski (jak pozostałe dokumenty analityczne w tym repo;
  dokumentacja kodu i commity pozostają po angielsku).

## 2. Zakres — co obejmujemy

| Model | Status u nas | Skąd wiedza |
|---|---|---|
| `kimodo-soma-rp` | **wdrożony, działa** | kod + własne pomiary |
| `ardy-core-rp` | **wdrożony, działa** | kod + własne pomiary + patche |
| MotionBricks | zbadany, niewdrożony | kod (sparse clone) + strona |
| HIL | zbadany, rekomendacja NO-GO | kod (klon) + arXiv 2505.12619v2 |

Wspominamy też **warianty rodzin**, bo wpływają na przyszłe decyzje:
kimodo (`smplx`, `g1`, `seed`), ARDY (`core8`, `g1`, `g152`),
MotionBricks (G1 wyłącznie), HIL (G1/T1).

## 3. Struktura dokumentu

1. **Streszczenie decyzyjne** — 5–8 zdań: co mamy, co odrzuciliśmy i dlaczego.
2. **Tabela główna: funkcje** — wszystkie cztery modele; kolumny:
   kategoria narzędzia, status u nas, embodiment, warunkowanie,
   constrainty, pose, retargeting. Dodatkowo kolumna **„status
   wdrożenia"**, bo „model to potrafi" a „mamy to zaimplementowane
   i przetestowane" to dwie różne rzeczy i mylenie ich byłoby
   najkosztowniejszym błędem tego dokumentu.
3. **Tabela: sterowanie i workflow** — najważniejsza dla animatora.
   Wiersze ustalone z góry, żeby nie zgubić różnic, które realnie bolą
   w pracy: granica prompt-bloków (co do klatki vs okno 2 s),
   przejścia między blokami, seed per blok, generacja pojedynczej pozy,
   ścieżka roota, piny efektorów, pozy kluczowe, siatka ciała,
   wpływ suwaka jakości (kroki dyfuzji).
4. **Tabela: dane techniczne** — szkielet i liczba stawów, fps, limit
   długości, licencja wag, dostępność wag, **czy wymaga patchowanego
   forka** (ARDY wymaga — to realny koszt utrzymania), zależności
   ciężkie (MuJoCo/Isaac).
5. **Tabela: wyniki pomiarów** — wyłącznie [P]; jawnie puste kolumny dla
   MotionBricks i HIL, żeby brak danych był widoczny, a nie ukryty.
6. **Kategorie, nie liga** — sekcja tekstowa wyjaśniająca, że HIL
   i MotionBricks nie są „konkurencją" dla kimodo/ARDY, tylko innym
   rodzajem narzędzia (patrz §5 — ryzyko R2).
7. **Charakterystyka każdego modelu** — po ~10 zdań, z tym, co wynikło
   z kodu, a nie ze strony marketingowej.
8. **Rekomendacje** — który model do czego; co dalej.
9. **Czego nie wiemy** — jawna lista luk (patrz §4).
10. **Źródła i gdzie szukać szczegółów** — linki do `BACKBONES.md`
    (architektura), trzech planów i raportu nocnego. To ta sekcja czyni
    dokument węzłem spinającym, a nie kopią (ryzyko R4).

W nagłówku dokumentu: **data sprawdzenia faktów i commit**, na którym
powstał — bez tego czytelnik za pół roku nie odróżni faktu aktualnego
od nieaktualnego (ryzyko R3).

## 4. Reżim rzetelności: trzy poziomy pewności

Największe ryzyko takiego dokumentu to zrównanie faktów o różnej wadze.
Każda liczba w tabelach dostaje jednoznaczne oznaczenie:

| Oznaczenie | Znaczenie | Przykład |
|---|---|---|
| **[P]** pomiar | zmierzyliśmy sami, jest skrypt | odchyłka od ścieżki 4,0 cm |
| **[K]** kod | wyczytane z kodu/konfiguracji modelu | ARDY `num_base_steps=10` |
| **[D]** deklaracja | twierdzenie autorów, niezweryfikowane | MotionBricks „15 000 FPS" |

**Zasada twarda:** żadnej liczby [D] nie stawiamy w tej samej rubryce co
[P] bez oznaczenia. Jeśli czegoś nie wiemy — wpisujemy „nie badane",
nigdy nie zgadujemy.

**Jawne luki do wypisania w §9 dokumentu:**
- Jakość podążania za promptem **nieprzetestowana dla obu wdrożonych
  modeli** — serwer chodzi na `TEXT_ENCODER_MODE=dummy`, prompty są
  ignorowane. Wszystko, co wiemy o sterowaniu, dotyczy constraintów.
- Retargeting na rigi użytkownika nieprzetestowany lokalnie (brak
  `boneclassifier_best.pt`).
- Jakość ruchu **nie była oceniana** — mierzyliśmy zgodność z
  constraintami i wydajność, nie „czy wygląda dobrze".
- MotionBricks i HIL nieuruchamiane — brak jakichkolwiek [P].

## 5. Ryzyka dokumentu

| # | Ryzyko | Mitygacja |
|---|---|---|
| R1 | Przepisanie marketingu jako faktów | System oznaczeń [P]/[K]/[D] z §4 |
| R2 | Tabela sugeruje, że cztery modele są porównywalne, a HIL to inna kategoria (polityka sterowania, nie generator) | Kolumna „kategoria" w tabeli głównej + osobna sekcja §6 dokumentu. Wiersze HIL w tabelach funkcji wypełniane „n/d — inna kategoria", nie „nie" |
| R3 | Dokument zestarzeje się po cichu (preview'y, nasze wdrożenia) | Data i commit w nagłówku; w §9 zdanie o tym, kiedy fakty były sprawdzane |
| R4 | Duplikacja `BACKBONES.md` → rozjazd przy zmianach | Ten dokument = funkcje i decyzje; `BACKBONES.md` = architektura i kontrakt. Linkujemy, nie kopiujemy |
| R5 | Wnioski „który lepszy" bez oceny jakości ruchu | Rekomendacje formułujemy przez zastosowania i twarde różnice (fps, limity, sterowanie), nigdy „X generuje ładniej" |

## 6. Kolejność kroków

1. Zebrać fakty [P] ze scratchpadu i historii sesji do jednej listy
   (pomiary: adherencja ścieżki, czasy, VRAM, seedy, testy błędów).
2. Zebrać fakty [K] z czterech planów — bez ponownego badania repo.
3. Napisać tabele (3–5), potem sekcje tekstowe wokół nich.
4. Przegląd własny pod kątem §4 (czy każda liczba ma oznaczenie)
   i §5 R2 (czy HIL nie udaje konkurenta).
5. Zapisać plik; nie commitować bez Twojej decyzji.

**Zależności:** brak zewnętrznych. Cała wiedza jest już zebrana w tej
sesji — dokument nie wymaga uruchamiania serwera ani ponownego
klonowania repozytoriów. Gdyby jakiegoś faktu zabrakło, wpisujemy
„nie badane" zamiast doczytywać (i odnotowujemy w §9).

## 7. Weryfikacja

- **Kompletność:** każdy z 4 modeli występuje w każdej tabeli (lub ma
  jawne „n/d — inna kategoria").
- **Oznaczenia:** przejść tabele wiersz po wierszu i potwierdzić, że
  każda liczba ma [P], [K] albo [D]. To sprawdzian ręczny — nie da się
  go zautomatyzować gerpem, bo „komórka z liczbą" nie jest wzorcem
  tekstowym. Wsparciem (nie dowodem) jest przegląd wszystkich liczb:
  `grep -o "[0-9][0-9.,]*" ANALIZA-modeli-ruchu.md | sort -u`.
- **Spójność z pomiarami:** wartości [P] zgodne z tym, co w
  `RAPORT-nocny-2026-08-06.md` (4,0 cm; 13/13 testów; 3,9/24,5 GB).
- **Brak duplikacji:** żadna sekcja nie powtarza architektury z
  `BACKBONES.md` — tylko odsyła.
- **Test użyteczności:** czy z samego dokumentu da się odpowiedzieć na
  pytania: „którego modelu użyć do 20-sekundowej sceny?", „czemu nie
  wdrożyliśmy MotionBricks?", „co blokuje prompty?". Jeśli nie —
  dokument jest niekompletny.

## 8. Czego NIE robimy

- Nie uruchamiamy MotionBricks ani HIL, żeby dorobić pomiary — to
  osobne przedsięwzięcia opisane we własnych planach.
- Nie oceniamy jakości estetycznej ruchu — nie mamy metodyki ani
  materiału porównawczego.
- Nie dublujemy `BACKBONES.md` ani planów; dokument ma je spinać.
- Nie commitujemy bez decyzji.
