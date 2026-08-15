import { initTRPC } from '@trpc/server';
import { transformer } from '../shared/transformer';
import { Context } from './context';
import { context, reddit } from '@devvit/web/server';
import { z } from 'zod';
import { computeMetrics, evaluate, getThresholds } from './core/verify';
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
    submit: publicProcedure
      .input(z.object({ code: z.string().trim().min(1).max(32) }))
      .mutation(async ({ input }) => {
        const username = await reddit.getCurrentUsername();
        if (!username) {
          console.error('[verify.submit] no username resolved from context');
          return {
            ok: false,
            message: "Couldn't verify your Reddit identity -- try again in a moment.",
          };
        }

        const metrics = await computeMetrics(username);
        if (!metrics) {
          try {
            await postVerdict({
              code: input.code,
              reddit_username: username,
              status: 'failed',
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

        const thresholds = await getThresholds();
        const { passed, failReason } = evaluate(metrics, thresholds);

        // TEMPORARY (PLAN.md Section 11 hidden-profile test) -- postVerdict()
        // is expected to fail until the domain allowlist review clears, which
        // would otherwise hide these numbers entirely. Remove once that's
        // confirmed working, or once this test is done.
        console.log('[verify.submit] metrics:', JSON.stringify({ username, metrics, passed, failReason }));

        try {
          await postVerdict({
            code: input.code,
            reddit_username: username,
            status: passed ? 'verified' : 'failed',
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

        return {
          ok: true,
          passed,
          message: passed
            ? "Submitted! Check Discord in a moment — you're in."
            : "Submitted. If this doesn't look right, you'll get a DM with next steps.",
        };
      }),
  }),
});

export type AppRouter = typeof appRouter;
