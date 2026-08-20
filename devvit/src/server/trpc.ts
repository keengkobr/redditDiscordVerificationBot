import { initTRPC } from '@trpc/server';
import { transformer } from '../shared/transformer';
import { Context } from './context';
import { context, reddit } from '@devvit/web/server';
import { z } from 'zod';
import {
  computeMetrics,
  decodeClaim,
  evaluate,
  getDedupOwner,
  getThresholds,
  normalizeUsername,
  recordDedupLink,
} from './core/verify';
import { postVerdict } from './core/webhook';

/**
 * Initialization of tRPC backend
 * Should be done only once per backend!
 */
const t = initTRPC.context<Context>().create({
  transformer,
});

/**
 * Export reusable router and procedure helpers
 * that can be used throughout the router
 */
export const router = t.router;
export const publicProcedure = t.procedure;

export const appRouter = t.router({
  init: t.router({
    get: publicProcedure.query(async () => {
      const username = await reddit.getCurrentUsername();
      return {
        postId: context.postId,
        username,
      };
    }),
  }),
  verify: t.router({
    // Replaces reddit_poller.py entirely (DEVVIT_PIVOT_SPEC.md). The submitting
    // user's identity comes from context/reddit for free -- no inbox polling.
    //
    // v5: the pasted code carries a claimed username + Discord user ID (see
    // decodeClaim) -- that's the only channel Discord-side data has to reach
    // this app at all. This app never sends the *resolved* username back to
    // Discord, only whether it matched the claim (username_ok) -- see
    // DEVVIT_PIVOT_SPEC.md v5's "Log-scope compliance boundary" section.
    submit: publicProcedure
      .input(z.object({ code: z.string().trim().min(1).max(300) }))
      .mutation(async ({ input }) => {
        const claim = decodeClaim(input.code);
        if (!claim) {
          return {
            ok: false,
            message: "That code doesn't look right -- copy it fresh from your Discord DM and try again.",
          };
        }

        // reddit.getCurrentUsername() isn't a plain context read -- it makes
        // its own API call (User.getById) to fetch full user details, and
        // silently returns undefined if THAT call fails for any reason
        // (rate limit, transient API hiccup, etc.), indistinguishable from
        // "not logged in" (confirmed in practice: a real, non-banned account
        // hit this repeatedly). context.username is populated directly from
        // the request's own auth data with no extra API round-trip, so it's
        // a much more reliable fallback for exactly this failure mode.
        const username = (await reddit.getCurrentUsername()) ?? context.username;
        if (!username) {
          // Reddit's own platform won't resolve an identity here for a few
          // reasons -- a suspended/banned account is one (seen in practice:
          // reddit.getCurrentUsername() came back empty for an account that
          // turned out to be permanently banned), a genuine transient glitch
          // is another. Can't tell which from this alone, so route it
          // through the normal fail path (a real verdict, not just an inline
          // message) rather than leaving the user stuck with no way forward
          // and mods with zero visibility that this happened at all.
          console.error(`[verify.submit] no username resolved from context (userId=${context.userId ?? 'none'})`);
          try {
            await postVerdict({
              code: claim.shortId,
              status: 'failed',
              username_ok: true,
              fail_reason: 'reddit_identity_unavailable',
            });
          } catch (err) {
            console.error('[verify.submit] postVerdict failed:', err);
          }
          return {
            ok: false,
            message: "Couldn't verify your Reddit identity. You'll get a DM from Discord with next steps.",
          };
        }

        if (normalizeUsername(username) !== claim.claimedUsername) {
          try {
            await postVerdict({ code: claim.shortId, status: 'failed', username_ok: false, fail_reason: null });
          } catch (err) {
            console.error('[verify.submit] postVerdict failed:', err);
          }
          return {
            ok: false,
            message:
              "That doesn't match the Reddit username you gave Discord. Make sure you're logged into " +
              'the right account, then try again from your Discord code.',
          };
        }

        const metrics = await computeMetrics(username);
        if (!metrics) {
          try {
            await postVerdict({
              code: claim.shortId,
              status: 'failed',
              username_ok: true,
              fail_reason: 'reddit_account_not_found',
            });
          } catch (err) {
            console.error('[verify.submit] postVerdict failed:', err);
          }
          return {
            ok: false,
            message: 'Something went wrong reading your Reddit profile. Try again shortly.',
          };
        }

        // Anti-duplicate check (DEVVIT_PIVOT_SPEC.md v5): one Reddit account
        // can't link to multiple Discord accounts within the TTL window, but
        // re-linking to the *same* Discord account is always allowed.
        const dedupOwner = await getDedupOwner(claim.claimedUsername);
        if (dedupOwner && dedupOwner !== claim.discordUserId) {
          try {
            await postVerdict({
              code: claim.shortId,
              status: 'failed',
              username_ok: true,
              fail_reason: 'reddit_account_already_linked',
            });
          } catch (err) {
            console.error('[verify.submit] postVerdict failed:', err);
          }
          return {
            ok: false,
            message: 'That Reddit account is already linked to a different Discord account.',
          };
        }

        const thresholds = await getThresholds();
        const { passed, failReason } = evaluate(metrics, thresholds);

        // Written only on an actual pass -- see DEVVIT_PIVOT_SPEC.md v5's
        // "Anti-duplicate KV write timing" section. A failed-threshold
        // attempt (e.g. someone accidentally logged into an alt/admin
        // Reddit account with no history in the subreddit) never locks
        // that Reddit account against retrying under a different Discord
        // account -- there's no self-service recovery for that otherwise,
        // since Discord-side /unlink can't reach this KV entry at all.
        if (passed) {
          await recordDedupLink(claim.claimedUsername, claim.discordUserId);
        }

        try {
          await postVerdict({
            code: claim.shortId,
            status: passed ? 'verified' : 'failed',
            username_ok: true,
            fail_reason: failReason,
            account_age_days: metrics.accountAgeDays,
            total_karma: metrics.totalKarma,
            subreddit_activity_count: metrics.subredditActivityCount,
            subreddit_karma: metrics.subredditKarma,
          });
        } catch (err) {
          console.error('[verify.submit] postVerdict failed:', err);
          return {
            ok: false,
            message: "Couldn't reach the verification server -- try again shortly.",
          };
        }

        // metrics/thresholds returned so the client can render the same
        // requirements checklist Discord DMs show -- one consistent story
        // whether you're looking at this page or your DMs.
        return {
          ok: true,
          passed,
          message: passed
            ? "Submitted! Check Discord in a moment — you're in."
            : "Submitted. If this doesn't look right, you'll get a DM with next steps.",
          metrics,
          thresholds,
        };
      }),
  }),
});

export type AppRouter = typeof appRouter;
