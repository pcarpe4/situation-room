# TODOS

## Monthly "where firedrills come from" page

- **What:** One page showing, by month, which groups send the most firedrills, how long each kind takes to close, and how long assignments sit stale.
- **Why:** It gives the boss numbers to push back on the heaviest sources. The board collects this history as a side effect of normal use.
- **Context:** Proposed as X5 in the CEO review of [docs/designs/situation-room.md](docs/designs/situation-room.md) on 2026-11-10. The author deferred it because it is only useful once there is a month or two of data. It reuses the firedrill, assignment and update tables.
- **Effort:** M (human: ~1 week / CC: ~half a day). Estimated.
- **Revisit when:** Milestone 1 has been live for a month.

## Post asks to Teams

- **What:** Let the board send an ask (and later the Monday digest) to a manager in Teams as well as by email.
- **Why:** Some managers answer chat faster than mail. The approved design said the Milestone 2 ask "can post to Teams" but named no mechanism, permission or test.
- **Context:** Removed from Milestone 2 by the /autoplan review of [docs/designs/situation-room.md](docs/designs/situation-room.md) on 2026-11-11 as a provisional decision, which the author confirms or reverses at that review's final gate. It needs its own design: which Teams mechanism the org allows on a standard license, what permission it takes, and whether a Teams reply should count as an update.
- **Effort:** M (human: ~1 week / CC: ~half a day), not counting the approval. Estimated.
- **Priority:** P3.
- **Depends on:** Milestone 2 mail sending, and an approved way to post to Teams.
- **Revisit when:** Milestone 2 has been live for a month and the share of asks answered within one working day is known.

## Tell a manager by app mail when their need is handled

- **What:** When a Lead or Triage marks a need handled, the app sends the manager who raised it a short notice with the handler's note.
- **Why:** "Needs you" is what managers get back for updating. Without a notice they learn a need was handled only by revisiting the board, or from an email the Lead chooses to send.
- **Pros:** Closes the loop without anyone remembering to. **Cons:** One more kind of app mail to template, cap and test.
- **Context:** Raised by the design pass of the /autoplan review of [docs/designs/situation-room.md](docs/designs/situation-room.md) on 2026-11-11 (finding F17). Milestone 1 covers it with an optional note on Handled and a prefilled "tell them" email. This item is the automatic version.
- **Effort:** S (human: ~1 day / CC: ~30 min). Estimated.
- **Priority:** P3.
- **Depends on:** Milestone 2 mail sending.

## CSV export of firedrills, assignments and updates

- **What:** A Triage-only download of firedrills, assignments and updates as CSV.
- **Why:** The plan rates itself easy to reverse because the data can be exported, but today that means writing SQL against the database.
- **Pros:** Makes the Microsoft List fallback and any later move real. **Cons:** One more screen and a file that may hold sensitive update text.
- **Context:** Raised by the DX pass of the /autoplan review of [docs/designs/situation-room.md](docs/designs/situation-room.md) on 2026-11-11 (finding F22). No original email content belongs in the export.
- **Effort:** S (human: ~1 day / CC: ~20 min). Estimated.
- **Priority:** P3.
- **Depends on:** Milestone 1.

## One summary email when a manager would get many asks on one morning

- **What:** When more than a set number of automatic asks would go to one manager in one 7:00 run, send a single email that lists them and links to "My assignments".
- **Why:** After a break, every assignment a manager owns can be stale on the same morning. One email per assignment reads as a flood and can hit the mail system's sending limits.
- **Pros:** Fewer emails on the worst mornings. **Cons:** A reply to a summary cannot be matched to one assignment, so reply-to-update does not work for it, and it is one more kind of mail to template and test.
- **Context:** Raised by the engineering pass of the /autoplan review of [docs/designs/situation-room.md](docs/designs/situation-room.md) on 2026-11-11 (finding E19). The plan already limits how many mails one run sends and, if the author keeps it, reads a list of non-working dates. Revisit if managers complain about volume.
- **Effort:** S (human: ~2 days / CC: ~30 min). Estimated.
- **Priority:** P3.
- **Depends on:** Milestone 2 automatic asks.

## JSON API, with its first caller

- **What:** A JSON form of the board's handlers (list and read firedrills, post an update, create an ask), addressed to the assignment, with a published OpenAPI document, idempotency keys on POST and problem bodies for errors.
- **Why:** The author asked for an API other tools can call. Nothing calls it today, and no outside tool could until service tokens exist.
- **Pros:** Lets ServiceNow or another tool read or post status. **Cons:** A second interface to authorize, test and version.
- **Context:** Moved out of Milestone 1 by the author at the final gate of the /autoplan review of [docs/designs/situation-room.md](docs/designs/situation-room.md) on 2026-11-11. Three reviewers advised it. The pages call one domain module, so this is a thin layer over it. The contract is in the plan's "Developer experience amendments".
- **Effort:** S (human: ~3 days / CC: ~2 hours), plus service tokens if the caller is not a signed-in person. Estimated.
- **Priority:** P3.
- **Depends on:** Milestone 1, and a named caller.
- **Revisit when:** A named system needs to read or post firedrill status.

## Tell On track apart from Done at a glance

- **What:** Give the Done badge its own look (neutral or outlined) instead of the same green as On track.
- **Why:** Both badges share `--changed` / `--changed-bg`, so scanning a firedrill you cannot tell finished owners from ones still working. Found by the /design-review run on 2026-11-12 (FINDING-018, medium, color).
- **Context:** Deferred by the author: it changes the status color language people already read. `.status-on_track, .status-done` in `app.css`.
- **Effort:** S (human: ~1 hour / CC: ~10 min). Estimated.
- **Priority:** P3.

## Shorter header on phones

- **What:** On narrow screens, fit the header into one or two rows, for example by moving the theme picker out of the header.
- **Why:** On a 375px phone the header takes about 180px (four rows) before any firedrill shows. "Sent it?" also ends up alone at the end of a line above Yes / Cancel ask. Found by the /design-review run on 2026-11-12 (FINDING-012, medium, responsive).
- **Context:** Deferred by the author because it moves controls people know; needs a layout choice first.
- **Effort:** S (human: ~half a day / CC: ~20 min). Estimated.
- **Priority:** P3.

## A small design system: DESIGN.md, type and spacing scale

- **What:** Write a DESIGN.md that records the tokens `app.css` already has, picks a body typeface or deliberately keeps `system-ui`, and adds spacing and font-size tokens.
- **Why:** Colors are tokens, but spacing uses about 15 one-off rem values and font sizes are ad hoc, so each new screen invents its own. The /design-review run on 2026-11-12 also kept the 4px red edge on stale cards on purpose (the detector flags it as a common AI-generated pattern); a DESIGN.md is where that choice should be written down.
- **Effort:** S (human: ~1 day / CC: ~30 min with /design-consultation). Estimated.
- **Priority:** P3.

## Completed

### Version the stylesheet URL

- **What:** Serve `app.css` with a content hash in its URL (for example `/static/app.css?v=<hash>`) set at startup.
- **Why:** The page links the stylesheet with no version, so a browser can keep the old CSS after a deploy and show new templates with old styles. The /design-review run on 2026-11-12 hit this: fixes did not show until a forced reload.
- **Effort:** S (human: ~1 hour / CC: ~10 min). Estimated.
- **Priority:** P2.
- **Depends on:** Nothing.
- **Completed:** unversioned (2026-11-12), on design-review-fixes: `app.css` is linked as `/static/app.css?v=<content hash>`. Open review note: `/static` still sends no Cache-Control, so a rolling multi-replica deploy can briefly pair a new hash with old CSS.
