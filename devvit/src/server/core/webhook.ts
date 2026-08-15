import { settings } from '@devvit/web/server';

/**
 * Sends the resolved verdict to the VPS (DEVVIT_PIVOT_SPEC.md's
 * webhook_receiver.py). This is the one HTTP egress this app makes --
 * requires the target host to be listed in devvit.json's
 * permissions.http.domains.
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
  const webhookSecret = await settings.get<string>('webhookSecret');

  if (!webhookUrl || !webhookSecret) {
    throw new Error(
      'webhookUrl/webhookSecret are not configured -- run `devvit settings set webhookUrl` and `webhookSecret`.'
    );
  }

  const res = await fetch(webhookUrl, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Devvit-Secret': webhookSecret,
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`webhook responded ${res.status}: ${body}`);
  }
}
