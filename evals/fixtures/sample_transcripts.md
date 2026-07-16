# Sample voice transcripts (Phase 5)

Use these with the mic (auto-send) or typed fallbacks. Scope: **Jaipur · 2–4 days**.

## 1. Plan → confirm → generate

```
Plan a 3-day trip to Jaipur next weekend. I like food and culture, relaxed pace.
```

Then confirm:

```
yes
```

(or click **Confirm & plan** in the UI)

## 2. Edit (requires a prior itinerary in the UI session)

```
Make Day 2 more relaxed.
```

```
Swap the Day 1 evening plan to something indoors.
```

```
Reduce travel time.
```

```
Add one famous local food place.
```

## 3. Explain (grounded)

```
Why did you pick this place?
```

```
Is this plan doable?
```

```
What if it rains?
```

## Notes

- Plan turns **must** confirm constraints before specialists run.
- Edits only change the target day/block (`apply_edit_patch`).
- Explain answers use stop reasons, day load/pace, weather adjustments, and RAG citations.
