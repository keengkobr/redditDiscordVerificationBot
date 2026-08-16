# Pivot v5: Fully Stateless VPS -- No Persistent Database At All

**Status: IMPLEMENTED.** No outreach to Reddit/Devvit was made before building this -- the SOC2/
persistence question (see "Remaining open question" near the end) is being resolved by letting
Reddit's own app review surface it, consistent with how every other ambiguous rule in this project
has been handled.

## Why this version exists

Devvit Rules' "Guidelines for external services for account linking" require SOC2 Type II compliance
and a recent third-party penetration test for any "external service connecting Reddit user data to
external account data." A self-hosted VPS has no realistic path to that certification. Every prior
version of this pivot (v1-v4) assumed *some* persistent storage on the VPS (`verify.db`), which keeps
this requirement in play regardless of how anonymized or minimized the stored data is -- the
requirement is about the service's compliance posture, not the sensitivity of what it stores.

v5's approach: **remove persistent storage entirely.** The VPS process never writes Reddit-linked data
to disk -- purely transient, in-memory handling during the few minutes a verification is actually in
flight. This is the strongest defensible position found in this process, though not a Reddit-confirmed
fix (see open question at the end).

## The key insight: nothing was actually being stored for its own sake

| What v1-v4 stored in `verify.db` | Where it lives in v5 |
|---|---|
| "This Discord user is verified" | **Discord's own role membership** -- already the durable record. |
| "This Reddit account has already been used to verify" (anti-duplicate) | **Devvit's own Redis/KV store** (`redis.set` with an expiration), keyed `reddit_username -> discord_user_id`. Never touches the VPS. |
| "Code X maps to Discord user Y, awaiting a Devvit verdict" | **In-memory only** -- `SESSIONS`/`CODE_TO_USER` dicts inside the running `discord_bot.py` process. Never written to disk. |
| "Who verified as whom" (audit/mod visibility) | **The verification-log channel's own message history.** |
| Username-mismatch retry count | **In-memory**, part of the same `SESSIONS` entry. Resets if the bot restarts mid-flow -- accepted, not a bug (see below). |

## The problem the first draft of this spec missed

v4's actual flow has **no claimed-username step at all**: `reddit.getCurrentUsername()` resolves
identity for free from Reddit's own session context the moment the user opens the post. An earlier
draft of this document described a `username_match` field and a "3-attempt mismatch retry" as if that
comparison already existed -- it didn't. That language was inherited from a much older (v1-v2,
PRAW-era) design and never reconciled against what v4 actually built.

Colby's requirement, once that was caught: **the Discord bot needs to hold the Reddit username itself**
(so it can show the right `u/name` in DMs and the log channel), but **Devvit must never send the
resolved username back to Discord in plaintext** -- only a boolean confirmation. That meant building
a real claim/match step, not just renaming a field:

1. The bot asks the user, via DM, which Reddit account is theirs (a real question now, not assumed).
2. The user's answer is normalized (`normalize_username()`, strips `u/`/`/u/`, lowercases, validates
   against Reddit's username character rules) and held in memory as `claimed_username`.
3. That claim has to reach Devvit somehow, but there's no channel for Discord-side data to reach the
   Devvit app except through the one field the user manually pastes into the Reddit post. So the code
   DMed to the user isn't a bare 6-character string anymore -- it's `{short_id}.{encoded blob}`, where
   the blob is `{"u": claimed_username, "d": discord_user_id}`, packed (not encrypted -- packed, same
   trust level as the plaintext DM it came from) via URL-safe base64. `decodeClaim()` on the Devvit
   side unpacks it locally.
4. Devvit compares the unpacked claim against `reddit.getCurrentUsername()` and replies with **only**
   `username_ok: true/false` -- the resolved username itself never appears in the webhook payload
   (`VerdictPayload` in `webhook.ts`/`verdict.py` has no `reddit_username` field at all).
5. The Discord bot already has the claimed username from step 2, so it uses that for DMs/log entries.
   It never needs Devvit to hand back the "real" one -- it only needs Devvit's yes/no.

Confirmed with Colby before building: the code the user copies is visibly longer/uglier now (e.g.
`X7K2Q9.eyJ1IjoidGhyb3...`) but is still a single copy-paste, no new steps added to the user's flow.

## Updated architecture

```
+---------------------------+   HTTPS POST     +----------------------------------+
|  Devvit app (Reddit-      | ---------------->|  Discord's own webhook endpoint   |
|  hosted, TypeScript)      |  discord.com      |  -> posts into                    |
|                           |  (globally        |  verification-log channel         |
|  - decodeClaim() unpacks  |  allow-listed)    +----------------------------------+
|    {claimed_username,                                          |
|    discord_user_id} from                                       v
|    the pasted code                                 discord_bot.py:
|  - Resolves real identity                           - looks up short_id against
|    via reddit.getCurrent-                             SESSIONS/CODE_TO_USER
|    Username(), compares                              (in-memory, no disk write)
|  - Dedup check/write via                            - grants/denies role based on
|    Devvit's own Redis                                 status + username_ok
|    (never touches the VPS)                          - posts the result (using its
|  - Runs threshold checks                              OWN claimed_username, never
|  - POSTs {code, status,                               a username from Devvit) to
|    username_ok, metrics}                              the log channel
+---------------------------+                          - NOTHING persisted to a
                                                          database anywhere on the VPS
```

No `verify.db`. No SQLite file at all. The VPS holds zero Reddit-linked data at rest, at any point.

## What `discord_bot.py` actually does now, end to end

1. User clicks Verify. If they already hold the Verified role (checked live against their Discord
   roles, not a DB flag), tell them so and stop.
2. Bot DMs asking which Reddit account is theirs. Session enters `SESSIONS[discord_user_id] =
   {"stage": "awaiting_username", ...}`.
3. User replies in DM -> `normalize_username()` validates it. Invalid input gets a plain-language
   retry prompt (no code issued yet).
4. Once a username normalizes, the bot generates a `short_id`, builds the compound code (step 3 in
   the "problem" section above), stores `{stage: "awaiting_verdict", claimed_username, short_id,
   mismatch_count: 0, updated_at}` in `SESSIONS`, indexes `CODE_TO_USER[short_id] = discord_user_id`,
   and DMs the code + link.
5. User completes the Devvit form -> Devvit decodes the claim, resolves real identity, compares,
   checks/writes its own Redis dedup entry, runs thresholds, POSTs `{code: short_id, status,
   username_ok, fail_reason, metrics...}` to the Discord webhook.
6. `discord_bot.py`'s webhook listener (`handle_verdict`) looks up `short_id` in `CODE_TO_USER` (not a
   DB query):
   - **Not found** (expired, already resolved, or bot restarted mid-flow): log a bare `short_id` +
     "discarding" line -- no username, no Discord ID -- and take no action. This is the natural
     idempotency guard in a stateless design. The fallback if a user reports "I verified and got
     nothing" is a mod manually granting the role -- a plain Discord action, no tooling needed.
   - **Found, `username_ok == false`**: increment `mismatch_count`. Below 3, re-prompt for the
     username (back to `awaiting_username`, same session). At 3, post a "couldn't confirm after 3
     tries" flag to the mod-review channel and tell the user directly, then drop the session.
   - **Found, `username_ok == true`**: consume the session, then behave exactly like v4's pass/fail
     handling (grant/deny role, DM, log-channel post, soft-fail mod flag for `no_visible_activity`) --
     just reading from the verdict payload + the session's own `claimed_username` instead of a DB row.
7. A background sweep (`sweep_stale_sessions`, replacing the old DB-polling loop entirely -- verdicts
   are now handled the instant the webhook delivers them, nothing to poll for) drops any session older
   than `CODE_EXPIRY_MINUTES` so abandoned attempts don't accumulate in memory.

## Restart/recovery behavior (deliberately not engineered around)

A bot restart mid-flow drops any in-flight session, resetting `mismatch_count` to zero and losing any
issued-but-unsubmitted code. Both are accepted, not bugs:
- Mismatch-count reset is harmless -- restarts are rare, admin-triggered events, not something a user
  can invoke themselves.
- A dropped in-flight code behaves identically to that code expiring on its own; the user's only
  recourse either way is clicking Verify again. No reconnect/resume logic was built for this.

## Anti-duplicate KV write timing (revised after playtest)

**Resolved via playtest, superseding the original plan below.** The first version of this design
wrote the dedup entry whenever `username_ok` was true, regardless of threshold pass/fail. Playtest
surfaced a real gap in that: someone who accidentally verifies against the wrong-but-real Reddit
account (e.g. logged into an alt/admin account with no subreddit history) fails thresholds and gets
that Reddit account locked to their Discord account for the full TTL -- with **no self-service
recovery path at all**, since Discord-side `/unlink` structurally can't reach this KV entry (that's
the whole point of the TTL design -- no inbound callback from the VPS into Devvit). Confirmed the
role-gate on `/unlink` wasn't the real problem here: even an unrestricted `/unlink` couldn't fix this,
because it never touches Devvit's side at all.

Fix: `recordDedupLink` is now called **only when `passed` is true** (`trpc.ts`), not merely on
`username_ok`. A failed-threshold attempt never locks the Reddit account against anything -- any
Discord account (including a corrected one) can retry immediately. The dedup check still does its
job once someone actually passes: at that point the Reddit account is genuinely spoken for, and
locking it against a *different* Discord account for 30 days is the intended anti-abuse behavior.
Same-account retries after a pass are unaffected (still keyed `reddit_username -> discord_user_id`,
so re-linking to yourself was never blocked either way).

Considered and rejected: adding a Devvit-side moderator menu action to manually clear a stuck dedup
entry (mirroring the existing "Create Verify for Discord post" action). Would still be a reasonable
backstop for edge cases the write-timing fix doesn't cover, but Colby chose the simpler write-timing
fix alone for now -- revisit if a scenario surfaces that this doesn't handle.

## Unverified role handoff (new functional fix, not present in any prior version)

Discord's own member-join flow auto-applies an "Unverified" role to everyone who joins the server. No
prior version of this bot (v1-v4) ever removed that role on a pass -- verified users were accumulating
both roles simultaneously, an unnoticed gap, not intentional design. v5 adds `UNVERIFIED_ROLE_ID`
(parallel to `VERIFIED_ROLE_ID`); on a genuine pass, `handle_result()` calls both `add_roles(verified)`
and `remove_roles(unverified)`, wrapped in the same permission-aware try/except pattern used elsewhere
in this bot.

## Unlink / disconnect

A real, shippable feature in v5 -- no prior version had an unlink command at all. Implemented as a
`/unlink` slash command (self-service only for now; a mod-triggered variant wasn't requested):
- Removes the Verified role, re-adds the Unverified role (same pairing as the pass handoff), confirms
  via an ephemeral reply. That's the entire user-facing action -- nothing else to do, because nothing
  else was ever stored.
- The Devvit-side dedup entry is **not** cleared on demand -- it simply expires on its own TTL (30
  days, chosen to match the "delete within 30 days" framing used elsewhere in this project), via
  Redis's own `expiration` option on `set()`. This sidesteps the inbound-callback problem entirely:
  `discord_bot.py` never needs to reach into the Devvit app at all.
- Practical effect: since the KV key is `reddit_account -> discord_user_id`, a user who unlinks and
  re-verifies under the *same* Discord account is never blocked -- it's recognized as a refresh, not a
  duplicate. The 30-day wait only applies to moving that Reddit account to a *different* Discord
  account. Deliberate tradeoff: instant self-service unlink/reverify for the common case, throttled
  only for the actual abuse case (account-hopping one Reddit account across Discord identities).
- Nothing to delete on the VPS either way -- "delete within 30 days" is satisfied by construction.

## Log-scope compliance boundary

The SOC2/persistence rule targets a service connecting *authoritative Reddit-platform data* to an
external account. A claimed username typed into a Discord DM is not Reddit data -- it's arbitrary
Discord message content, no different in kind from anything else a user types into the bot, until
Devvit resolves and confirms it against the real account. Likewise, the verdict Devvit POSTs back
(`status`, `username_ok`) is a derived boolean signal, never the resolved username itself. Logging
either of those on the Discord side, into Discord-owned infrastructure (stdout a mod reads, or the
log channel), stays within Discord's own data boundary and doesn't reintroduce the persistent-storage
problem v5 exists to eliminate.

One concrete consequence of this, applied during implementation: `on_message`'s JSON-parse-failure log
line never prints `message.content` (the raw, untrusted relay payload) -- only a bare notice that
parsing failed, so a malformed payload can't leak a claimed username into journald. Journald retention
hardening (`SystemMaxUse=`/`MaxRetentionSec=`) remains a reasonable future improvement, not a blocker.

## What the log channel needs to support, now that it's the sole record

Since the verification-log channel is now the only place "who's linked to whom" is visible at all:
- Every resolved verification (pass or fail) still gets logged, per the earlier decision to log
  everything including fails.
- The claimed username is shown exactly as before -- it's Discord's own data, sourced from the
  session, never from Devvit.
- Colby has already created the channel and set its permissions (confirmed separately from this doc).

## What stays the same from prior versions

- Discord webhook as transport, `webhook_id` authenticity check.
- Role assignment logic, mod-review routing for soft-fails -- unchanged.
- Explicit opt-in consent prompt on the Devvit form -- unchanged.

## What got deleted from the codebase entirely

- `db.py` and its entire schema.
- `verify.db` (and all migration scripts written for it across v1-v4).
- Any `chmod 600`/permissions concerns specific to the database file -- moot, there's no file.
- `verdict.py`'s DB-writing role -- it's now pure payload validation (`VerdictPayload`/`parse_verdict`),
  no `db` import at all.
- The DB-polling `process_results` background loop -- verdicts are now handled the instant the relay
  webhook delivers them; the only remaining background task is the in-memory session sweep.

## Remaining open question -- resolved via design, not outreach

The inbound-callback question is moot (TTL expiry replaces the need for a callback entirely, see
"Unlink" above). The SOC2/persistence question remains genuinely unresolved in the abstract -- no
public documentation reviewed in this process states definitively whether a fully stateless,
non-persisting service falls outside the "external service connecting Reddit user data to external
account data" trigger. Per Colby's decision, this isn't being escalated to Reddit directly; v5's
design is the strongest, most defensible position achievable through architecture alone, and Reddit's
own app review process is what will surface whether this specific point needs further changes.

## Multi-tenant settings (post-playtest addition)

Settings (`webhookUrl` + all four thresholds) moved from Devvit's `global` scope to `subreddit`
scope in `devvit.json`, so each subreddit installing this app configures its own Discord webhook
and its own thresholds instead of sharing one app-wide value across every install. This was always
implicitly needed for "other subreddits can run this with their own Discord server" to actually
work -- a single global `webhookUrl` meant every install shared one Discord destination, which
only ever worked by accident for a single-subreddit deployment.

Consequences worth remembering:
- The `devvit settings set`/`list` CLI is explicitly global-scope only (per `devvit settings
  --help`) -- it can no longer configure any of these at all. Each install's settings are now set
  through that subreddit's own Mod Tools -> Apps -> Verify for Discord -> Settings page.
- Subreddit-scoped string settings can't be marked `isSecret` (a Devvit schema limitation -- that
  flag only exists for global settings), so `webhookUrl` is no longer masked on the settings page.
  Accepted tradeoff: it's each subreddit's own credential, visible only to its own mods with
  settings access, never to anyone outside that install.
- No code changes needed in `verify.ts`/`webhook.ts` beyond the manifest change -- `settings.get()`
  resolves against whichever scope a setting is declared under, using the current request's
  subreddit context automatically.

## Settings key renamed: webhookUrl -> discordWebhookUrl (found via playtest)

Discovered a real, undocumented platform limitation while playtesting the subreddit-scope move
above: Devvit's settings-merge code (`@devvit/settings/SettingsClient.js`'s `getAll()`) spreads
`installationSettings` (subreddit-scoped) first, then `appSettings` (global) second --

```js
return {
  ...getSettingsValues(response.installationSettings.settings, ...),
  ...getSettingsValues(response.appSettings.settings, ...),
};
```

Object spread means a later key always wins. Any key that still has a stored *global* value
unconditionally overwrites the same key's subreddit-scoped value, forever, regardless of what the
current manifest declares. `webhookUrl` had exactly this problem: it was set as a global value
early in this project (before the subreddit-scope move), and that stale value kept winning no
matter what got saved on the new per-subreddit settings page -- confirmed by adding a temporary
debug log of just the resolved webhook's ID (never the token), which kept reporting the old local
dev webhook's ID no matter what was saved to the subreddit setting.

There is no CLI delete/unset for a global setting (`devvit settings set` only creates/updates,
confirmed via `--help`), and re-declaring the key as global just to overwrite it with an empty or
placeholder value doesn't help either -- the key still exists in `appSettings.settings`, so it
still wins the merge, just with different content (confirmed: setting it to a placeholder string
made `postVerdict` fail trying to parse that string as a URL, not "webhookUrl is not configured").

Fix: renamed the setting to `discordWebhookUrl` everywhere (manifest, `webhook.ts`, all docs). A
key that was never set globally has nothing stored to win the merge, so the subreddit-scoped value
comes through cleanly. This sidesteps the platform limitation rather than fighting it -- there may
be a real way to clear a stale global value through Reddit/Devvit support channels, but renaming
was faster and didn't require outside help.

**If this project ever needs to rename another setting for a different reason, don't -- this
specific rename was a one-time fix for stale data from before subreddit scope existed. A schema
change alone (moving scope, changing type) does not retroactively clear old stored values.**

## Known limitation: no "code expired" message on the Reddit side

Submitting an expired/already-resolved code still shows the normal "Submitted. If this doesn't
look right, you'll get a DM" message on Reddit -- Devvit has no way to know a code expired, since
that's tracked entirely in the Discord bot's in-memory session (`CODE_TO_USER`/`SESSIONS`), never
shared with Devvit at all. The bot silently discards the resulting verdict (logged as `no session
found for code ... -- discarding`), so the user just never gets a DM and has no indication why.

A real fix would mean packing a timestamp into the code alongside the claimed username + Discord
ID, so Devvit could estimate elapsed time and warn before submitting -- rejected as not worth the
extra code-length/complexity for what's considered an edge case (codes expire after 30 minutes;
most users complete the flow well within that). Accepted as-is. Revisit if this turns out to be a
common support question in practice.

## Explicitly out of scope for this pass

- Any change to the classic-PRAW-path decision -- still parked on the `channelLogging` branch.
- The custom-domain approach -- still superseded by the Discord-webhook design.
- No outreach to Reddit/Devvit support planned before resubmission.
