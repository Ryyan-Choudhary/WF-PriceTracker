# WF-PriceTracker: Matching Accuracy + Performance Plan

## Root Cause Analysis

### Matcher Accuracy
1. **Scorer `fuzz.WRatio` is too generous with partial matches.** WRatio weights `partial_ratio` and `token_set_ratio` heavily, so a short candidate like "Afuris Prime Link" can score ~90 against the query "Afuris Prime Blueprint" because the shared prefix "afuris prime" gets a partial_ratio of 100. This directly explains the user's complaint: "Afuris Prime Blueprint first (85.7) but lost to Afuris Prime Link." Switching to `fuzz.token_set_ratio` penalizes the missing "blueprint" word and would give the exact match 100 while the wrong match gets ~85.7 — the correct candidate wins by a large margin.

2. **Short/truncated OCR reads fail to anchor**, falling back to scoring against ALL 3837 items. This is slow and produces cascading ambiguous-match refusals for generic words like "Blueprint", "Prime", "Stock". Causes: `FUZZY_ANCHOR_SCORE_CUTOFF=78` is too strict for OCR-truncated tokens (e.g. "ius" from "Afuris" scores 50/100 against "afuris").

3. **No UI-text filter.** The matcher scores known UI strings ("Stop Scan Mode (F10)", "No item", scan toolbar labels, inventory count text "[18]", etc.) against the catalog, producing false matches or wasted ambiguous refusals (app.log lines 119-122, 144, 315, 319-323).

4. **Multi-line join in `price_crop()` is gap-blind.** It joins ALL lines in the crop regardless of vertical distance. If two item tiles are captured, their names get concatenated into junk like "Paris Prime Upper Limb Perigale Prime Receiver" (app.log line 203, 206), which then ambiguously ties between two unrelated items. `price_region()` already has gap-aware partner detection; `price_crop()` does not.

5. **Margin rule refuses legitimately truncated names.** "Titania Prime", "Silva & Aegis Prime", etc. all tie at 90-95 between all parts of the same frame. The margin rule (`FUZZY_MATCH_MIN_MARGIN=3`) correctly refuses to guess, but the user experience is "no match" for items that ARE visible.

### Performance
6. **Sequential disk cache writes on every price fetch.** `market.py:135` calls `_save_disk_cache()` inside the cache lock for EVERY individual price fetch. A 30-item grid scan = up to 30 sequential full-file JSON writes.

7. **Default `PRICE_FETCH_WORKERS=1`.** All cold-cache price lookups are strictly sequential. Even a small grid pays ~3s per item × N items = seconds of pure network latency.

8. **EasyOCR is effectively serialized.** Its PyTorch model is protected by a global lock (ocr.py:42-59), so multi-frame grid scans never parallelize OCR regardless of thread count.

9. **Full-catalog fallback for unanchored queries.** `_anchor_candidates` returns `list(range(len(self._items)))` (all 3837) when no family anchors. Each unanchored query triggers 3837 rapidfuzz comparisons.

10. **Gemini Vision 404** crashes the entire scan worker. The `google.genai.errors.ClientError: 404 NOT_FOUND` for `gemini-2.5-flash` propagates uncaught through `ocr.py:220-228` and kills the scan thread (app.log lines 56-109).

---

## Proposed Changes (ordered by impact)

### P1 — Matcher Accuracy (fixes the exact issues reported)

**1. Switch fuzzy scorer from `fuzz.WRatio` to `fuzz.token_set_ratio`**
- File: `wf_pricer/items_db.py`, line 120
- Change: `scorer=fuzz.WRatio` → `scorer=fuzz.token_set_ratio`
- Why: `token_set_ratio` is purpose-built for "query has extra words that candidate doesn't." An exact match scores 100. A candidate missing words (like "link" vs "blueprint") scores ~85.7. This directly fixes "Afuris Prime Blueprint loses to Afuris Prime Link." It also improves handling of word reordering and OCR punctuation noise without inflating partial matches the way WRatio does.

**2. Add UI-text / OCR artifact blacklist**
- File: `wf_pricer/items_db.py`, new method `_reject_query(text)` and call in `match()` before normalization
- Blacklist strings: `"no item"`, `"stop scan mode"`, `"start scan mode"`, `"f10"`, `"f9"`, `"scanning region"`, `"no items recognized"`, `"no return"`, `"no current leap"`, plus any text matching patterns like `\[18\]`, `\[19\]`, `\[30\]`, `===`, `---`, `~~`, `|`, `c2`, `c3` etc.
- Why: Eliminates false matches against UI chrome and prevents wasted ambiguous-match refusals on garbage OCR.

**3. Relax anchor scoring + short-token fallback**
- File: `wf_pricer/items_db.py`, `_anchor_candidates()` and `wf_pricer/config.py`
- Change 1: Use `fuzz.partial_ratio` instead of `fuzz.ratio` for anchor token matching (line 158-161)
- Change 2: Add a SECOND, lower-cutoff pass for tokens that fail the strict pass: `FUZZY_ANCHOR_SCORE_CUTOFF = 78` → strict pass at 78, then a loose pass at 55 for tokens still unanchored. Or simpler: lower `FUZZY_ANCHOR_SCORE_CUTOFF` to 65 and increase `FUZZY_ANCHOR_MAX_FAMILIES` to 10 (still fast, just wider net).
- Change 3: When anchoring completely fails, return a capped set (top 200 items by base-name frequency) instead of all 3837. This preserves the fallback for genuinely unusual reads but cuts the worst-case workload by ~95%.
- Why: "ius Prime Barrel" (from truncated "Afuris Prime") should still anchor to the Afuris/afuris family.

**4. Add gap-aware line joining in `price_crop()`**
- File: `wf_pricer/pipeline.py`, lines 52-56
- Change: Only join two lines if their vertical gap is < line_height, mirroring `price_region()`'s partner logic (lines 121-128). Multi-line names typically wrap with a small gap; items in adjacent tiles have a larger gap.
- Why: Prevents "Paris Prime Upper Limb Perigale Prime Receiver" style cross-tile contamination in single-item mode.

### P2 — Performance (~1-2s faster)

**5. Batch/debounce price cache writes**
- File: `wf_pricer/market.py`, lines 56-60 and 132-136
- Change: Add a background write timer (e.g., `threading.Timer(0.3, _save_disk_cache)`) or a dirty flag + periodic flush. The in-memory cache is updated immediately; disk write is deferred.
- Why: 30-item grid = 30 sequential disk writes eliminated. Cache lock contention drops drastically.

**6. Raise default `PRICE_FETCH_WORKERS` from 1 to 3**
- File: `wf_pricer/config.py`, line 186
- Change: `PRICE_FETCH_WORKERS = 3`
- Rationale: The code already uses `REQUEST_DELAY_SECONDS = 0.3` between requests per worker. With 3 workers staggered over ~0.9s total, the rate is ~3.3 req/s — polite and well within typical rate limits. Sequential (1 worker) means each item adds ~0.3s + network latency.
- Why: Cold-cache multi-item scans are 2-3× faster.

**7. Reduce default `GRID_SCAN_MAX_RETRY_PROFILES` from 6 to 3**
- File: `wf_pricer/config.py`, line 133
- Change: `GRID_SCAN_MAX_RETRY_PROFILES = 3`
- Rationale: 7 profiles × 3 frames = 21 OCR calls per unresolved slot is overkill. Profile 0 ("default") + dim-text + no-binarize covers the common cases. Stats-driven reduction after observing retry hit rate.
- Why: Grid scan retry overhead drops ~50%.

**8. Increase `FUZZY_MATCH_SCORE_CUTOFF` from 84 to 86 after scorer swap**
- File: `wf_pricer/config.py`, line 381
- Change: Raise cutoff slightly since `token_set_ratio` is more discriminative than WRatio.
- Why: Prevents near-miss garbage OCR from matching wrong items.

### P3 — Robustness

**9. Graceful Gemini Vision 404 handling**
- File: `wf_pricer/ocr.py`, lines 220-228
- Change: Wrap `client.models.generate_content` in try/except for `google.genai.errors.ClientError` (404) and return empty list.
- Why: `gemini-2.5-flash` returning 404 crashes the worker thread (app.log lines 56-109). The app should degrade gracefully.

**10. Fix `scan_count` race condition**
- File: `wf_pricer/main.py`, lines 393-396, 464
- Change: Use `threading.Lock()` or `itertools.count()` for `scan_count`, or simply accept that it's cosmetic. Low priority — only affects log numbering.

---

## Scanning Multithreading Analysis

**Question: Is more multithreading possible in the scanning path to make it faster?**

Short answer: No meaningful gain. The current threading is already correctly aligned with the actual bottlenecks.

### What the scanning pipeline does today
```
Screen capture (0-50ms) → OCR (50ms-2s) → Matching (<1ms) → Pricing (0-3s network)
```

### Already parallelized
| Component | Mechanism | Notes |
|-----------|-----------|-------|
| Grid frame OCR | `ThreadPoolExecutor` in `_read_frames_texts()` | Tesseract only; EasyOCR serialized by PyTorch lock |
| Price fetching | `ThreadPoolExecutor` in `market.get_prices()` | Configurable 1-8 workers |
| Scan workers | `threading.Thread` per trigger | Each scan runs independently |

### Why more scanning threads won't help

1. **Screen capture is already fast and intentionally sequential.** Grid frames are captured with a 0.12s delay between captures to get different animation frames — parallelizing captures defeats the purpose of multi-frame voting.

2. **OCR is the dominant cost but can't be sped up with more threads.** Tesseract already parallelizes across frames (one thread per frame). EasyOCR is GPU-bound and protected by a global PyTorch lock — more threads just queue up behind it.

3. **Matching is microseconds per item.** rapidfuzz is a C++ backend; scoring against ~20 anchored candidates takes < 1ms. Threading overhead (spawn, queue, marshall) exceeds the work.

4. **The real bottleneck is network I/O (pricing), which is already parallelized.** With `PRICE_FETCH_WORKERS=1` (current default), cold-cache scans are network-bound. Raising this to 3 gives 2-3× speedup with no architectural changes.

5. **Overlapping OCR + pricing across scans isn't worth it.** The user triggers scans manually (F9 / drag). Between scans, the app is idle. Pipelining within a single scan adds complexity for < 100ms gain.

### Conclusion
No additional scanning multithreading is warranted. The proposed P2 changes (workers=3, debounced writes, fewer retry profiles) address the actual bottlenecks.

---

## Files Changed

| File | Changes |
|------|---------|
| `wf_pricer/items_db.py` | Scorer, anchor logic, UI blacklist, fallback cap |
| `wf_pricer/config.py` | Threshold tweaks, worker count, retry profiles |
| `wf_pricer/market.py` | Debounced cache writes |
| `wf_pricer/pipeline.py` | Gap-aware line join in `price_crop()` |
| `wf_pricer/ocr.py` | Gemini 404 graceful fallback |

## Validation Plan

1. **Unit-level**: Run the app and verify "Afuris Prime Blueprint" matches correctly when "Afuris Prime Link" is also in the catalog (manual test with EasyOCR + debug prints).
2. **Regression**: Run existing grid scan workflow; confirm no false matches on UI strings (e.g. "Stop Scan Mode (F10)").
3. **Performance**: Time a full-grid scan cold-cache before/after; target 1-2s improvement.
4. **Edge cases**: Test truncated names ("Titania Prime"), wrapped names ("Silva & Aegis Prime"), and noisy OCR ("ius Prime Barrel").
