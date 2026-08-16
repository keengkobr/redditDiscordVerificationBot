import { context, reddit, redis, settings } from '@devvit/web/server';

/**
 * Replaces reddit_poller.py's check_thresholds() (DEVVIT_PIVOT_SPEC.md). Runs
 * server-side inside the form-submit handler, where the submitting user's
 * identity is already known via reddit.getCurrentUsername() -- no inbox, no
 * polling, no separate identity step.
 */

/**
 * How long the anti-duplicate KV entry (see recordDedupLink below) lives
 * before a Reddit account can be linked to a *different* Discord account
 * again. Re-verifying under the *same* Discord account is never blocked by
 * this (DEVVIT_PIVOT_SPEC.md v5's unlink section).
 */
const DEDUP_TTL_MS = 30 * 24 * 60 * 60 * 1000; // 30 days

function dedupKey(username: string): string {
  return `dedup:${username}`;
}

/** Strips an optional u/ or /u/ prefix and lowercases -- mirrors
 * discord_bot.py's normalize_username() so both sides agree on equality. */
export function normalizeUsername(raw: string): string {
  let candidate = raw.trim();
  if (candidate.toLowerCase().startsWith('/u/')) candidate = candidate.slice(3);
  else if (candidate.toLowerCase().startsWith('u/')) candidate = candidate.slice(2);
  return candidate.toLowerCase();
}

export type DecodedClaim = {
  shortId: string;
  claimedUsername: string;
  discordUserId: string;
};

/**
 * Unpacks the code discord_bot.py DMed the user. This is the *only* channel
 * that exists for Discord-side data to reach this app at all -- Devvit can't
 * fetch our VPS (personal domains are never approved), and there's no
 * inbound-callback mechanism either. See DEVVIT_PIVOT_SPEC.md v5.
 */
export function decodeClaim(code: string): DecodedClaim | null {
  const dot = code.indexOf('.');
  if (dot === -1) return null;
  const shortId = code.slice(0, dot);
  const blobB64 = code.slice(dot + 1);
  try {
    const json = Buffer.from(blobB64, 'base64url').toString('utf8');
    const obj = JSON.parse(json) as { u?: unknown; d?: unknown };
    if (typeof obj.u !== 'string' || typeof obj.d !== 'string' || !obj.u || !obj.d) return null;
    return { shortId, claimedUsername: normalizeUsername(obj.u), discordUserId: obj.d };
  } catch {
    return null;
  }
}

/**
 * Returns the Discord user ID this Reddit account is already linked to
 * (still within the TTL window), or null if it's free to link.
 */
export async function getDedupOwner(username: string): Promise<string | null> {
  const owner = await redis.get(dedupKey(username));
  return owner ?? null;
}

/** Writes/refreshes the dedup link. Called once identity is confirmed to
 * match the claim (username_ok), regardless of whether thresholds pass --
 * see DEVVIT_PIVOT_SPEC.md v5's "Anti-duplicate KV write timing" section. */
export async function recordDedupLink(username: string, discordUserId: string): Promise<void> {
  await redis.set(dedupKey(username), discordUserId, {
    expiration: new Date(Date.now() + DEDUP_TTL_MS),
  });
}

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
  // Setting keys prefixed "sub" (subMinAccountAgeDays etc.), not a bare
  // "minAccountAgeDays" -- these thresholds were tuned repeatedly via
  // `devvit settings set` back when they were still global-scope settings
  // (see PLAN.md's threshold-tuning history), which left stale global
  // values behind. Devvit's settings-merge always lets a global value win
  // over a subreddit-scoped one with the same key, forever, with no CLI way
  // to delete it (the exact bug found and fixed for webhookUrl -- see
  // DEVVIT_PIVOT_SPEC.md's "Settings key renamed" section). Renamed these
  // too, preemptively, rather than wait to rediscover the same bug.
  const [minAccountAgeDays, minTotalKarma, minSubredditActivityCount, minSubredditKarma] =
    await Promise.all([
      settings.get<number>('subMinAccountAgeDays'),
      settings.get<number>('subMinTotalKarma'),
      settings.get<number>('subMinSubredditActivityCount'),
      settings.get<number>('subMinSubredditKarma'),
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
