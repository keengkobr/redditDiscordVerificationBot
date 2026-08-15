import { context, reddit, settings } from '@devvit/web/server';

/**
 * Replaces reddit_poller.py's check_thresholds() (DEVVIT_PIVOT_SPEC.md). Runs
 * server-side inside the form-submit handler, where the submitting user's
 * identity is already known via reddit.getCurrentUsername() -- no inbox, no
 * polling, no separate identity step.
 */

export type Metrics = {
  accountAgeDays: number;
  totalKarma: number;
  subredditActivityCount: number;
  subredditKarma: number;
};

export type Thresholds = {
  minAccountAgeDays: number;
  minTotalKarma: number;
  minSubredditActivityCount: number;
  minSubredditKarma: number;
};

export type Verdict = {
  passed: boolean;
  failReason: string | null;
};

/** Mirrors PLAN.md Section 4 defaults -- overridden by app settings, same idea as .env on the PRAW side. */
const DEFAULT_THRESHOLDS: Thresholds = {
  minAccountAgeDays: 30,
  minTotalKarma: 50,
  minSubredditActivityCount: 1,
  minSubredditKarma: 50,
};

export async function getThresholds(): Promise<Thresholds> {
  const [minAccountAgeDays, minTotalKarma, minSubredditActivityCount, minSubredditKarma] =
    await Promise.all([
      settings.get<number>('minAccountAgeDays'),
      settings.get<number>('minTotalKarma'),
      settings.get<number>('minSubredditActivityCount'),
      settings.get<number>('minSubredditKarma'),
    ]);

  return {
    minAccountAgeDays: minAccountAgeDays ?? DEFAULT_THRESHOLDS.minAccountAgeDays,
    minTotalKarma: minTotalKarma ?? DEFAULT_THRESHOLDS.minTotalKarma,
    minSubredditActivityCount:
      minSubredditActivityCount ?? DEFAULT_THRESHOLDS.minSubredditActivityCount,
    minSubredditKarma: minSubredditKarma ?? DEFAULT_THRESHOLDS.minSubredditKarma,
  };
}

/**
 * Pulls account age, total karma, and subreddit-specific post/comment count +
 * karma for the given username. Returns null if the account can't be found
 * (suspended/deleted/typo'd -- reddit.getUserByUsername returns undefined
 * rather than throwing for these cases, confirmed during the spike).
 */
export async function computeMetrics(username: string): Promise<Metrics | null> {
  const user = await reddit.getUserByUsername(username);
  if (!user) return null;

  const accountAgeDays = Math.floor((Date.now() - user.createdAt.getTime()) / 86_400_000);
  const totalKarma = user.linkKarma + user.commentKarma;
  const subredditName = context.subredditName ?? '';

  const [comments, posts] = await Promise.all([
    user.getComments({ sort: 'new', limit: 200 }).all(),
    user.getPosts({ sort: 'new', limit: 200 }).all(),
  ]);

  const subComments = comments.filter(
    (c) => c.subredditName.toLowerCase() === subredditName.toLowerCase()
  );
  const subPosts = posts.filter(
    (p) => p.subredditName.toLowerCase() === subredditName.toLowerCase()
  );

  return {
    accountAgeDays,
    totalKarma,
    subredditActivityCount: subComments.length + subPosts.length,
    subredditKarma:
      subComments.reduce((sum, c) => sum + c.score, 0) +
      subPosts.reduce((sum, p) => sum + p.score, 0),
  };
}

/** Same threshold-comparison + soft-fail logic as reddit_poller.py's check_thresholds(). */
export function evaluate(metrics: Metrics, thresholds: Thresholds): Verdict {
  const failed: string[] = [];
  if (metrics.accountAgeDays < thresholds.minAccountAgeDays) {
    failed.push(`account_age:${metrics.accountAgeDays}d<${thresholds.minAccountAgeDays}d`);
  }
  if (metrics.totalKarma < thresholds.minTotalKarma) {
    failed.push(`total_karma:${metrics.totalKarma}<${thresholds.minTotalKarma}`);
  }
  if (metrics.subredditActivityCount < thresholds.minSubredditActivityCount) {
    failed.push(
      `subreddit_activity:${metrics.subredditActivityCount}<${thresholds.minSubredditActivityCount}`
    );
  }
  if (metrics.subredditKarma < thresholds.minSubredditKarma) {
    failed.push(`subreddit_karma:${metrics.subredditKarma}<${thresholds.minSubredditKarma}`);
  }

  if (failed.length === 0) return { passed: true, failReason: null };

  // Curated/hidden-profile false negative (PLAN.md Section 11): zero visible
  // subreddit activity despite an old-enough account is a soft fail --
  // discord_bot.py routes this to mod review instead of hard-rejecting.
  if (metrics.subredditActivityCount === 0 && metrics.accountAgeDays >= thresholds.minAccountAgeDays) {
    return { passed: false, failReason: 'no_visible_activity' };
  }

  return { passed: false, failReason: failed.join(';') };
}
