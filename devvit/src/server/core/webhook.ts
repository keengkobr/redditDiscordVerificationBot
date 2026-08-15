import { settings } from '@devvit/web/server';

/**
 * Sends the resolved verdict to a Discord Incoming Webhook (DEVVIT_PIVOT_SPEC.md
 * v4). discord_bot.py reads this same channel directly and writes the verdict
 * into verify.db itself (verdict.py) -- no separate HTTP server on our end.
 *
 * Why a Discord webhook rather than our own endpoint: Reddit's HTTP Fetch
 * Policy states personal/custom domains "will not be approved" -- only a
 * fixed global allowlist skips review, and discord.com is on it (see
 * devvit.json's permissions.http.domains). A self-hosted VPS domain is a
 * dead end under that policy.
 */
export type VerdictPayload = {
  code: string;
  reddit_username: string;
  status: 'verified' | 'failed';
  fail_reason: string | null;
  account_age_days?: number;
  total_karma?: number;
  subreddit_activity_count?: number;
  subreddit_karma?: number;
};

export async function postVerdict(payload: VerdictPayload): Promise<void> {
  const webhookUrl = await settings.get<string>('webhookUrl');

  if (!webhookUrl) {
    throw new Error(
      'webhookUrl is not configured -- run `devvit settings set webhookUrl` with the Discord webhook URL from discord_bot.py\'s startup logs.'
    );
  }

  // Discord webhooks expect their own payload shape (content/embeds/etc), not
  // an arbitrary JSON body -- so the verdict travels as a JSON string inside
  // `content`. discord_bot.py's on_message handler parses it back out.
  const res = await fetch(webhookUrl, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ content: JSON.stringify(payload) }),
  });

  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`webhook responded ${res.status}: ${body}`);
  }
}
