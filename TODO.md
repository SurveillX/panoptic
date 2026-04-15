## Panoptic Next 4 Milestones Plan

### Goal

Catch or surpass NVIDIA in the areas that matter most to Panoptic:

1. search
2. summarization
3. report generation
4. event / alert architecture
5. later, agent layer / UI

This plan assumes:

* Panoptic already has bucket ingest, selected image ingest, image captions + caption embeddings, search, verify, and period summarization
* retrieval service is up with Qwen3-Embedding-8B and Qwen3-Reranker-0.6B
* Spark migration is coming, so model-heavy improvements should be timed accordingly

---

# Milestone 1 — First-class `panoptic_events`

## Why

Right now “event” is partly a view over alert/anomaly images and partly hidden inside bucket-derived summary fields. That is not strong enough long-term.

A real `panoptic_events` model will improve:

* search
* filtering
* verification
* period summaries
* future report generation
* future agent behavior

## Outcome

Create a first-class event layer in Panoptic.

## Scope

Build:

* `panoptic_events` table
* event ingest/backfill from:

  * bucket `event_markers`
  * image-backed alert/anomaly events
* event linking to:

  * `(serial_number, camera_id)`
  * time window
  * related `summary_id`
  * related `image_id` where applicable
* event retrieval in Search API

## Event types to support first

Start with the things Panoptic already knows how to derive:

* alert_created
* anomaly
* after_hours
* spike
* drop
* late_start
* underperforming

## Requirements

* exact composite identity usage:

  * `serial_number`
  * `camera_id`
* normalized event rows
* event severity / confidence
* event source field:

  * `bucket_marker`
  * `image_trigger`
  * later maybe `verification`
* time-bounded event linkage

## Search impact

After this milestone:

* “events” become real search results
* no more pretending events are just filtered images
* summaries can still expose events, but events become independently searchable

## Deliverables

* `panoptic_events` schema
* backfill script
* Search API updated to return real event records
* Verify and period summarization updated to consume real events

---

# Milestone 2 — Report generation

## Why

Search + verify + period summary are useful, but users eventually want a consumable artifact.

This is where Panoptic can become clearly product-like.

## Outcome

Generate grounded operator/customer-facing reports from existing evidence.

## Scope

Build:

* on-demand report endpoint
* HTML output first
* PDF later if needed
* report sections grounded in:

  * summaries
  * verified findings
  * events
  * supporting images

## Report types

Start with:

* trailer daily report
* trailer weekly report
* selected-camera period report

## Report structure

Minimum report sections:

* headline
* executive summary
* activity summary
* progress/change summary
* notable events
* supporting images
* caveats / missing coverage

## Rules

* every nontrivial claim should be backed by stored evidence
* use supporting IDs throughout
* do not invent events/progress not present in evidence
* include uncertainty/coverage notes when appropriate

## Suggested execution model

* on-demand first
* synchronous HTML generation acceptable initially
* PDF can be added after HTML is useful

## Deliverables

* `POST /v1/report/period`
* structured report response
* HTML renderer
* support for real event references and supporting image references

---

# Milestone 3 — Visual image embeddings

## Why

Current image search is caption-first.
That is good, but not enough to fully catch up on search.

To improve search quality, Panoptic needs:

* caption embeddings
* visual embeddings
* later, hybrid ranking between the two

## Outcome

Images become searchable by both:

* caption semantics
* visual similarity

## Scope

Build:

* image embedding worker
* `image_vectors` Qdrant collection
* image embedding status fields on `panoptic_images`
* visual search mode in Search API

## Requirements

* use already stored pushed images only
* no trailer pull for this phase
* preserve current caption-based search
* add visual retrieval as an additional path, not a replacement

## Search behavior after this

Search can support:

* text → caption embeddings
* image-like / visually similar retrieval → image embeddings
* later fusion of caption + image results

## Deliverables

* image embedding pipeline
* Qdrant `image_vectors`
* Search API support for image-vector retrieval
* evaluation of whether hybrid retrieval improves real operator queries

---

# Milestone 4 — Agent layer / UI

## Why

Once search, verify, events, summaries, and reports are solid, Panoptic can expose them through a strong operator experience.

An agent/UI before the underlying data model is strong would mostly be a demo.
After the first 3 milestones, it can become a real product surface.

## Outcome

Provide a user-facing agent/UI layer on top of Panoptic capabilities.

## Scope

Build:

* initial web UI
* search interface
* evidence review
* verification entry point
* report generation/download
* later, conversational agent behavior

## Initial UI modules

Start with:

1. Search

   * text query
   * structured filters
   * grouped results:

     * summaries
     * images
     * events

2. Verification

   * “verify this result” action
   * show verdict + supporting evidence

3. Period summary / reports

   * pick trailer/cameras/time range
   * generate summary
   * generate/download report

4. Evidence browser

   * images
   * summaries
   * events
   * links between them

## Agent behavior

Only after UI basics exist:

* natural-language question answering over Panoptic search
* follow-up questions
* report refinement
* investigation assistance

## Rules

* agent should call Panoptic APIs, not bypass them
* agent should work over:

  * search
  * verify
  * summarize
  * report
* no hidden side channels

## Deliverables

* initial UI
* minimal agent layer on top of existing APIs
* evidence-grounded workflows only

---

# Recommended order relative to Spark

## Before / during Spark migration

Best done now or immediately after:

* Milestone 1 planning
* Milestone 2 planning
* retrieval improvements already underway
* env/process cleanup

## After Spark

Best implemented after Spark is stable:

* Milestone 1 implementation
* Milestone 2 implementation
* Milestone 3 implementation
* Milestone 4 implementation

Reason:

* events and reports benefit from stable retrieval + synthesis
* visual embeddings and richer UI/agent behavior benefit from more available compute

---

# Priority order

## Highest priority

1. `panoptic_events`
2. report generation

## Next

3. visual image embeddings

## Then

4. agent layer / UI

---

# Success criteria

## After Milestone 1

* events are first-class records
* Search API returns real event results
* verify and period summaries consume real events cleanly

## After Milestone 2

* users can generate grounded reports from stored evidence
* reports are actually useful to operators/customers

## After Milestone 3

* image retrieval is better than caption-only
* visual search adds real value on real queries

## After Milestone 4

* Panoptic feels like a product, not just a backend
* users can search, verify, summarize, and report through one coherent interface

---

# Short version

If the goal is to catch or surpass NVIDIA where it matters most, the next sequence should be:

1. **build `panoptic_events`**
2. **build report generation**
3. **add visual image embeddings**
4. **build the agent/UI layer**

That is the cleanest path from “strong backend” to “better product.”
