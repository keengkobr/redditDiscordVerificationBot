# Features

A plain reference list of what this system actually does today. For *why* it's built this way, see
[PLAN.md](PLAN.md) (design rationale/roadmap) and [DEVVIT_PIVOT_SPEC.md](DEVVIT_PIVOT_SPEC.md)
(architecture deep dive, including the live-testing history behind several of these).

## Verification flow

- **Pinned "Verify" post in `#verify-here`** — the only channel visible before verification, listing
  the pass requirements up front.
- **Identity claim, not a guess** — clicking Verify prompts a DM asking which Reddit account is
  theirs; that claim is packed (with their Discord ID) into the one-time code they take to Reddit,
  so Devvit can confirm it against who's actually logged in rather than trusting it blindly.
- **Mobile-safe code delivery** — the code is sent as its own plain-text message, separate from the
  instructions embed, so "tap and hold → copy" grabs exactly the code on mobile (an embed field or
  fenced code block would copy extra characters along with it).
- **Identity mismatch handling** — up to 3 retries if the logged-in account doesn't match the claim,
  then automatically routed to the mod-review channel (system flag, no thread) instead of endless
  retries.
- **Unresolvable-identity handling** — if Reddit's platform won't resolve an identity at all (e.g. a
  suspended/banned account), the user still gets a real fail DM with a manual-review path instead of
  being stuck on a dead-end message.
- **Threshold checks** (configurable per subreddit): account age, total karma, subreddit-specific
  post/comment count, subreddit-specific karma.
- **Curated/hidden-profile detection** — the Devvit app runs with moderator scope, which grants
  visibility into a user's subreddit-specific content for 28 days after they interact with the
  community, even if their public profile is set to curated/hidden. Confirmed working via direct
  testing (hidden posts/comments still detected exactly).
- **No-activity soft fail** — zero detected activity (even accounting for the above) doesn't hard-fail
  the user; it's flagged for a mod instead, since it could be someone genuinely new to the subreddit.
- **Code expiry** — codes expire after 30 minutes; clicking Verify again while a code is still valid
  re-sends the same one instead of silently starting over.
- **Anti-duplicate protection** — one real Reddit account can't be used to unlock more than one
  Discord account. Enforced entirely on Devvit's own infrastructure (a TTL'd Redis entry, 30 days),
  written only on a confirmed identity match that also passes thresholds — a failed attempt never
  locks the account, and re-verifying under the *same* Discord account is never blocked either way.

## Pass / fail outcomes

- **Pass** — Verified role granted, Unverified role removed (if configured), green success DM with a
  per-requirement checklist, logged to the verification-log channel.
- **Fail** — red (or amber, for the no-activity soft-fail case) DM with the same checklist marking
  which requirement(s) fell short, plus a "Request Manual Review" button.
- **Manual review request** — clicking that button opens a form asking why the user thinks the result
  was wrong (required). Submitting it opens a **private thread** in a dedicated review channel, with
  the requesting user added directly to it (so they can actually answer follow-up questions even
  though they can't see the channel itself), optionally pinging one or more mod roles. Falls back to
  a plain embed post if thread creation fails for any reason, so a request is never silently lost.
- **Close Thread button** — lets anyone with `Manage Threads` in the review channel mark a thread
  resolved (posts a confirmation, then archives + locks it). The requesting user can't close their
  own thread.
- **Self-service unlink** — `/unlink` removes the Verified role (re-adds Unverified if configured) so
  someone can re-verify, e.g. after fixing a mistake. Instant if re-verifying under the same Discord
  account; the 30-day anti-duplicate window only applies to moving a Reddit account to a *different*
  Discord account.

## Visibility for mods

- **Verification log channel** — an embed for every completed attempt, pass or fail, with the same
  requirements checklist shown to the user. Every mention in this channel (and the mod-review/manual-
  review channels) includes a plaintext username fallback alongside the `@mention`, since Discord's
  client-side mention rendering sometimes lags and shows a raw numeric ID instead.
- **Mod-review channel** — automatic flags for the two system-triggered soft-fail cases (repeated
  identity mismatch, no detected activity), separate from the dedicated manual-review channel used
  for user-initiated requests.

## Privacy / data handling

- **No database, no persistent storage on the VPS at all** — every piece of in-flight state lives in
  plain in-memory dicts for the few minutes a verification is actually in flight. Durable state lives
  where it already belongs: Discord's own role membership, Discord's own channel history, and
  Devvit's own Redis/KV store.
- **The resolved Reddit username is never sent from Devvit back to Discord** — only a match/no-match
  boolean. Discord already has whatever username the user self-reported in their DM; Devvit never
  needs to repeat it back.
- **Terms & Privacy Policy** (`docs/terms.html`, `docs/privacy.html`) — describe the actual v5 data
  flow accurately, including what's kept (the 30-day pass-only anti-duplicate link) and what isn't.

## Multi-subreddit support

- **Per-subreddit Devvit settings** — the Discord webhook URL and all four thresholds are configured
  independently by each subreddit that installs the app, not shared globally. A single VPS/Discord
  bot deployment corresponds to one subreddit's settings.
- **Moderator menu action** — "Create Verify for Discord post" lets a mod (re)create the pinned post
  on demand, without needing to reinstall the app.

## Operational

- **Discord webhook relay, not a self-hosted server** — the Devvit app posts verdicts to a Discord
  Incoming Webhook on a hidden relay channel; `discord_bot.py` reads it directly. No custom domain,
  no nginx/TLS, works within Reddit's HTTP Fetch Policy (which only allows a fixed global allowlist
  that `discord.com` happens to be on).
- **Session housekeeping** — a periodic sweep drops abandoned in-memory verification sessions so they
  don't accumulate over a long uptime.
- **Slash-command sync is opt-in**, not automatic on every restart — registered commands persist
  server-side regardless of restarts, and Discord's command-sync endpoint has a much stricter rate
  limit than normal API calls.
